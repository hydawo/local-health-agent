"""Local vector store over LanceDB, plus the keyword fallback.

**Why LanceDB and not Chroma.** The plan names either (§2). Chroma's install
pulls 76 packages including an OpenTelemetry OTLP exporter, a Kubernetes client,
uvicorn, and onnxruntime, and its `anonymized_telemetry` setting defaults to on.
For a project whose central claim is "no telemetry, no phone-home, verifiable"
(§5), that is a large and awkward surface to have to defend. LanceDB installs 15
packages, runs as a library against a local directory with no server, and has no
telemetry enabled by default — its OpenTelemetry bridge is opt-in and requires
the caller to pass a MeterProvider. We also pass precomputed vectors rather than
using LanceDB's embedding-function registry, which is the only part of the
package that would ever fetch anything.

**Two retrieval paths, on purpose.** Semantic search needs an embedding model;
`health-agent search` still has to do something useful on a machine where Ollama
isn't installed yet. So chunks always land in SQLite with an FTS5 index, and
vectors are an additive layer. Semantic search is used when the chunks have been
embedded with the active model; otherwise the search falls back to keyword
matching and *says so* in the result, rather than quietly returning worse hits.

**Vectors from different models are never mixed.** Each row records the embedder
name; a query embedded with model A only searches rows embedded with model A.
Cosine distance between two different models' vector spaces is meaningless, and
silently blending them degrades results in a way that is nearly impossible to
notice from the outside.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..logging_setup import get_logger

if TYPE_CHECKING:
    from ..embeddings import Embedder

log = get_logger("store.vector")

TABLE_NAME = "chunks"
EMBED_BATCH = 64
DELETE_BATCH = 500


@dataclass
class SearchHit:
    chunk_id: int
    document_id: int
    path: str
    title: str | None
    page_no: int | None
    doc_date: str | None
    text: str
    score: float
    method: str  # 'semantic' | 'keyword'
    kind: str = "pdf"  # 'pdf' (records) | 'note' | 'image' (OCR'd photo)
    section: str | None = None

    @property
    def citation(self) -> str:
        """How this snippet should be attributed in an answer.

        A page number is the right locator for a PDF and meaningless for a note,
        so notes cite their heading trail instead. Both always carry the
        filename and, when known, the date.
        """
        parts = [Path(self.path).name]
        if self.kind == "image":
            # Provenance the reader must see: OCR text can be wrong in ways a
            # typed note cannot, and the answer should carry that caveat.
            parts[0] += " (read by OCR)"
        elif self.kind == "note":
            if self.section:
                parts.append(self.section)
        elif self.page_no:
            parts.append(f"p.{self.page_no}")
        if self.doc_date:
            parts.append(self.doc_date)
        return ", ".join(parts)


def _table_names(db) -> list[str]:
    """Names of the tables in a LanceDB connection.

    `list_tables()` returns a `ListTablesResponse` object, not a list of strings,
    so the obvious `name in db.list_tables()` is silently always False — which
    manifests as vectors being written and then never found again, with no
    error anywhere. (The deprecated `table_names()` did return a plain list.)
    `getattr` covers both shapes so this works either way.
    """
    listing = db.list_tables()
    return list(getattr(listing, "tables", listing))


def _import_lancedb():
    try:
        import lancedb
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError(
            "lancedb is required for semantic search. Install with `pip install -e .`"
        ) from exc
    return lancedb


class VectorStore:
    """LanceDB-backed vector index living beside the SQLite index."""

    def __init__(self, path: Path, table_name: str = TABLE_NAME) -> None:
        self.path = Path(path)
        # Corpus vectors and personal vectors live in one LanceDB directory but
        # never in one table. Mixing them would let a literature chunk be
        # hydrated as though it were the user's own record.
        self.table_name = table_name
        self._db = None
        self._table = None

    def _connect(self):
        if self._db is None:
            lancedb = _import_lancedb()
            self.path.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self.path))
        return self._db

    def _open_table(self, create_dim: int | None = None):
        db = self._connect()
        if self._table is not None:
            return self._table
        if self.table_name in _table_names(db):
            self._table = db.open_table(self.table_name)
        elif create_dim is not None:
            import pyarrow as pa

            schema = pa.schema([
                pa.field("vector", pa.list_(pa.float32(), create_dim)),
                pa.field("chunk_id", pa.int64()),
                pa.field("document_id", pa.int64()),
                pa.field("page_no", pa.int64()),
                pa.field("embedder", pa.string()),
                pa.field("text", pa.string()),
            ])
            self._table = db.create_table(self.table_name, schema=schema)
        return self._table

    def count(self) -> int:
        table = self._open_table()
        return 0 if table is None else int(table.count_rows())

    def add(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        table = self._open_table(create_dim=len(rows[0]["vector"]))
        table.add(rows)
        return len(rows)

    def drop(self) -> None:
        db = self._connect()
        if self.table_name in _table_names(db):
            db.drop_table(self.table_name)
        self._table = None

    def chunk_ids(self) -> set[int]:
        """Every chunk_id the table holds, across all embedders.

        Reads one column, not the rows: a vector table is mostly vectors,
        and the only question a caller asks here is which SQLite chunks the
        table still refers to.
        """
        table = self._open_table()
        if table is None:
            return set()
        column = table.search().select(["chunk_id"]).limit(None).to_arrow()
        return {int(v) for v in column.column("chunk_id").to_pylist()}

    def delete_chunks(self, chunk_ids: set[int] | list[int],
                      batch_size: int = DELETE_BATCH) -> int:
        """Delete every row keyed by one of these chunk ids. Returns how
        many ids were sent.

        Batched because the filter is an `IN (...)` literal and a corpus
        reinstall can leave thousands of stale ids behind at once.
        """
        ids = sorted({int(c) for c in chunk_ids})
        table = self._open_table()
        if table is None or not ids:
            return 0
        for start in range(0, len(ids), batch_size):
            batch = ids[start:start + batch_size]
            table.delete(f"chunk_id IN ({', '.join(str(c) for c in batch)})")
        return len(ids)

    def search(self, vector: list[float], *, embedder_name: str,
               limit: int = 5) -> list[dict]:
        table = self._open_table()
        if table is None or table.count_rows() == 0:
            return []
        # Restrict to vectors from the same model; see module docstring.
        query = (
            table.search(vector)
            .where(f"embedder = '{_escape(embedder_name)}'", prefilter=True)
            .limit(limit)
        )
        return query.to_list()


def _escape(value: str) -> str:
    return value.replace("'", "''")


# --------------------------------------------------------------------------- #
# Embedding pending chunks
# --------------------------------------------------------------------------- #

def pending_count(conn: sqlite3.Connection, embedder_name: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM chunk WHERE embedded_with IS NOT ? ",
        (embedder_name,),
    ).fetchone()["n"]


# table -> (parent column, page expression). The page expression is per-table
# because an article has no page; interpolating a literal 0 for the corpus
# keeps the LanceDB row shape identical without inventing a column.
_ALLOWED_CHUNK_TABLES = {
    "chunk": ("document_id", "page_no"),
    "article_chunk": ("article_id", "0"),
}


def embed_pending(conn: sqlite3.Connection, store: VectorStore,
                  embedder: "Embedder", *, table: str = "chunk",
                  parent_column: str = "document_id",
                  batch_size: int = EMBED_BATCH, progress=None) -> int:
    """Embed every chunk not yet embedded with this model. Returns count.

    Resumable by construction: progress is recorded per batch on the chunk rows,
    so an interrupted run (or an Ollama restart) picks up where it stopped
    instead of re-embedding a whole corpus.
    """
    # `table` and `parent_column` are interpolated rather than bound because
    # SQLite cannot bind identifiers. Both are module-internal constants,
    # never user input — but this guard keeps that true.
    allowed = _ALLOWED_CHUNK_TABLES.get(table)
    if allowed is None or allowed[0] != parent_column:
        raise ValueError(f"refusing to embed from unknown table {table!r}")
    _, page_expr = allowed

    rows = conn.execute(
        f"SELECT id, {parent_column} AS parent_id, {page_expr} AS page_no, text "  # noqa: S608
        f"FROM {table} WHERE embedded_with IS NOT ? ORDER BY id",
        (embedder.name,),
    ).fetchall()
    if not rows:
        return 0

    done = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        vectors = embedder.embed([r["text"] for r in batch])
        store.add([
            {
                "vector": vector,
                "chunk_id": int(row["id"]),
                "document_id": int(row["parent_id"]),
                "page_no": int(row["page_no"] or 0),
                "embedder": embedder.name,
                "text": row["text"],
            }
            for row, vector in zip(batch, vectors)
        ])
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        conn.executemany(
            f"UPDATE {table} SET embedded_with = ?, embedded_at = ? "  # noqa: S608
            f"WHERE id = ?",
            [(embedder.name, stamp, int(row["id"])) for row in batch],
        )
        conn.commit()
        done += len(batch)
        if progress is not None:
            progress(done, len(rows))
    return done


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

def _hydrate(conn: sqlite3.Connection, chunk_ids: list[int]) -> dict[int, sqlite3.Row]:
    if not chunk_ids:
        return {}
    placeholders = ",".join("?" * len(chunk_ids))
    rows = conn.execute(
        f"SELECT c.id AS chunk_id, c.document_id, c.page_no, c.section, c.text, "
        f"d.path, d.title, d.doc_date, d.kind FROM chunk c "
        f"JOIN document d ON d.id = c.document_id "
        f"WHERE c.id IN ({placeholders})",
        chunk_ids,
    ).fetchall()
    return {int(r["chunk_id"]): r for r in rows}


def semantic_search(conn: sqlite3.Connection, store: VectorStore,
                    embedder: "Embedder", query: str, limit: int = 5,
                    kind: str | None = None) -> list[SearchHit]:
    vector = embedder.embed([query])[0]
    # Over-fetch when filtering by kind: the filter is applied after ranking, so
    # asking for exactly `limit` could return fewer than requested.
    fetch = limit * 4 if kind else limit
    raw = store.search(vector, embedder_name=embedder.name, limit=fetch)
    hydrated = _hydrate(conn, [int(r["chunk_id"]) for r in raw])

    hits: list[SearchHit] = []
    for row in raw:
        meta = hydrated.get(int(row["chunk_id"]))
        if meta is None:
            continue  # chunk deleted since embedding; skip rather than 404
        if kind and meta["kind"] != kind:
            continue
        distance = float(row.get("_distance", 0.0))
        hits.append(SearchHit(
            chunk_id=int(row["chunk_id"]),
            document_id=int(meta["document_id"]),
            path=meta["path"],
            title=meta["title"],
            page_no=meta["page_no"],
            doc_date=meta["doc_date"],
            text=meta["text"],
            # L2 distance -> a bounded, larger-is-better score for display.
            score=1.0 / (1.0 + distance),
            method="semantic",
            kind=meta["kind"],
            section=meta["section"],
        ))
        if len(hits) >= limit:
            break
    return hits


def keyword_search(conn: sqlite3.Connection, query: str, limit: int = 5,
                   kind: str | None = None) -> list[SearchHit]:
    """FTS5 fallback used when no embeddings exist for the active model."""
    match = _fts_query(query)
    if not match:
        return []
    sql = (
        "SELECT c.id AS chunk_id, c.document_id, c.page_no, c.section, c.text, "
        "d.path, d.title, d.doc_date, d.kind, bm25(chunk_fts) AS rank "
        "FROM chunk_fts "
        "JOIN chunk c ON c.id = chunk_fts.rowid "
        "JOIN document d ON d.id = c.document_id "
        "WHERE chunk_fts MATCH ? "
    )
    params: list = [match]
    if kind:
        sql += "AND d.kind = ? "
        params.append(kind)
    sql += "ORDER BY rank LIMIT ?"
    params.append(limit)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        log.warning("keyword search failed (%s)", type(exc).__name__)
        return []

    return [
        SearchHit(
            chunk_id=int(r["chunk_id"]),
            document_id=int(r["document_id"]),
            path=r["path"],
            title=r["title"],
            page_no=r["page_no"],
            doc_date=r["doc_date"],
            text=r["text"],
            # bm25 returns negative numbers, better is more negative.
            score=-float(r["rank"]),
            method="keyword",
            kind=r["kind"],
            section=r["section"],
        )
        for r in rows
    ]


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Every token is quoted so punctuation in a user's question can't be parsed as
    FTS operator syntax (a bare `-` or `*` would otherwise change the query's
    meaning or raise).
    """
    import re

    tokens = re.findall(r"[A-Za-z0-9]+", query)
    return " OR ".join(f'"{t}"' for t in tokens if len(t) > 1)
