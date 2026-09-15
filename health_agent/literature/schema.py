"""`literature.db` — the corpus index.

Versioned independently of the personal index (`store/sqlite_schema.py`, v4),
because the two have unrelated lifecycles: a corpus refresh is not an ingest,
and a personal schema change must not invalidate a large download.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

LITERATURE_SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS corpus_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per installed pack. Present from v1 even though slice 1 builds only
-- local packs: without pack identity, both distributable topic packs and corpus
-- versioning need a migration later.
CREATE TABLE IF NOT EXISTS pack (
    id                     INTEGER PRIMARY KEY,
    slug                   TEXT NOT NULL,
    title                  TEXT,
    version                TEXT NOT NULL,
    built_at               TEXT NOT NULL,
    article_count          INTEGER NOT NULL DEFAULT 0,
    source_manifest_sha256 TEXT,
    UNIQUE(slug, version)
);

CREATE TABLE IF NOT EXISTS article (
    id                  INTEGER PRIMARY KEY,
    pack_id             INTEGER NOT NULL REFERENCES pack(id) ON DELETE CASCADE,
    pmid                TEXT,
    doi                 TEXT,
    nct_id              TEXT,
    title               TEXT NOT NULL,
    -- No abstract column. article_chunk.text is the retrievable copy; a
    -- second copy here was written by build() and read by nothing, and on a
    -- real corpus it was a quarter of the database. (v1 had it.)
    journal             TEXT,
    pub_year            INTEGER,
    -- The raw MEDLINE value, kept verbatim beside the derived tier so the
    -- tiering is auditable against its source rather than merely trusted.
    publication_types   TEXT NOT NULL DEFAULT '[]',
    evidence_tier       TEXT NOT NULL,
    evidence_rank       INTEGER,
    -- Enforced, not documented: a tier is read off the source's own metadata or
    -- it is 'unknown'. There is no third option, and 'inferred' is not a value.
    tier_source         TEXT NOT NULL
        CHECK (tier_source IN ('publication_type', 'unmapped')),
    -- Per article, never global: the PMC Open Access subset mixes CC-BY,
    -- CC-BY-NC and CC-BY-NC-ND, so one blanket claim would be false.
    license             TEXT NOT NULL,
    full_text_available INTEGER NOT NULL DEFAULT 0,
    -- A snapshot can cite a retracted paper forever. Retracted articles are
    -- kept rather than deleted, so an answer given before a retraction stays
    -- explicable after it.
    retracted           INTEGER NOT NULL DEFAULT 0,
    retraction_note     TEXT,
    fetched_at          TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_article_pmid ON article(pack_id, pmid);
CREATE INDEX IF NOT EXISTS idx_article_tier ON article(evidence_rank, pub_year);

CREATE TABLE IF NOT EXISTS mesh_term (
    article_id INTEGER NOT NULL REFERENCES article(id) ON DELETE CASCADE,
    term       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mesh_term ON mesh_term(term);
CREATE INDEX IF NOT EXISTS idx_mesh_article ON mesh_term(article_id);

CREATE TABLE IF NOT EXISTS article_chunk (
    id            INTEGER PRIMARY KEY,
    article_id    INTEGER NOT NULL REFERENCES article(id) ON DELETE CASCADE,
    section       TEXT,
    chunk_index   INTEGER NOT NULL,
    text          TEXT NOT NULL,
    char_count    INTEGER NOT NULL,
    embedded_with TEXT,
    embedded_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_achunk_article ON article_chunk(article_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_achunk_pending ON article_chunk(embedded_with);

CREATE VIRTUAL TABLE IF NOT EXISTS article_chunk_fts USING fts5(
    text,
    content='article_chunk',
    content_rowid='id',
    tokenize='porter unicode61'
);
"""


class CorpusNotFound(RuntimeError):
    """Raised when a command needs a corpus that hasn't been built yet."""


class CorpusSchemaVersionMismatch(RuntimeError):
    """Raised when a corpus was written by a different schema version."""


def connect(path: Path, *, create: bool = False) -> sqlite3.Connection:
    """Open a corpus, refusing one written by another schema version.

    The version check lives here rather than in callers because every caller
    forgot it: the ask path, `status`, and `build` all opened a corpus blind
    while `check_version` sat unused. A fresh file (create=True, nothing on
    disk yet) has no version to check and is stamped by `initialize`.
    """
    path = Path(path)
    existed = path.exists()
    if not existed:
        if not create:
            raise CorpusNotFound(
                f"No literature corpus at {path}. Run "
                f"`health-agent literature build` first."
            )
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    if existed:
        try:
            check_version(conn)
        except CorpusSchemaVersionMismatch:
            conn.close()
            raise
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    """Create tables and stamp the version. Idempotent."""
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO corpus_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(LITERATURE_SCHEMA_VERSION),),
    )
    conn.commit()


def read_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute(
            "SELECT value FROM corpus_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return int(row["value"]) if row else None


def check_version(conn: sqlite3.Connection) -> None:
    found = read_version(conn)
    if found != LITERATURE_SCHEMA_VERSION:
        raise CorpusSchemaVersionMismatch(
            f"Corpus schema version is {found}, this build expects "
            f"{LITERATURE_SCHEMA_VERSION}. Run "
            f"`health-agent literature build --from <medline.xml> --rebuild` "
            f"to recreate it."
        )


def rebuild_chunk_fts(conn: sqlite3.Connection) -> None:
    """Repopulate the external-content FTS index after a bulk load."""
    conn.execute("INSERT INTO article_chunk_fts(article_chunk_fts) "
                 "VALUES('rebuild')")
    conn.commit()
