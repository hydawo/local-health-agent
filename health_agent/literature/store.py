"""Retrieval over the corpus.

Two paths, mirroring `store/vector_store.py`: semantic when the corpus has been
embedded with the active model, FTS5 keyword otherwise, and the result says
which ran rather than quietly returning worse hits.

**Results are ordered by evidence strength first, relevance second.** This is
the one place the corpus deliberately departs from ordinary search ranking. A
case report that matches the query wording closely should not outrank a
meta-analysis that matches it slightly less well, because the question being
answered is "what does the evidence say", not "what text is most similar".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..logging_setup import get_logger
from . import tiers
from .embed import TABLE_NAME

if TYPE_CHECKING:
    from ..embeddings import Embedder
    from ..store.vector_store import VectorStore

log = get_logger("literature.store")


@dataclass
class Finding:
    article_id: int
    pmid: str
    title: str
    text: str
    method: str
    doi: str | None = None
    journal: str | None = None
    year: int | None = None
    evidence_tier: str = tiers.UNKNOWN
    evidence_rank: int | None = None
    tier_source: str = "unmapped"
    license: str = ""
    full_text_available: bool = False
    retracted: bool = False
    retraction_note: str | None = None

    @property
    def citation(self) -> str:
        """Dated and identified, so a claim can be checked at its source.

        ROADMAP's requirement is that "a 2019 meta-analysis found..." reads
        honestly rather than as evergreen fact, so the year is never optional
        in the rendered form — an undated citation says so explicitly.
        """
        parts = [self.journal or "unknown journal",
                 str(self.year) if self.year else "year not stated",
                 f"PMID {self.pmid}"]
        return ", ".join(parts)


_SELECT = """
SELECT a.id AS article_id, a.pmid, a.doi, a.title, a.journal, a.pub_year,
       a.evidence_tier, a.evidence_rank, a.tier_source, a.license,
       a.full_text_available, a.retracted, a.retraction_note, c.text,
       c.id AS chunk_id
FROM article_chunk c JOIN article a ON a.id = c.article_id
"""


def _finding(row: sqlite3.Row, method: str) -> Finding:
    return Finding(
        article_id=int(row["article_id"]),
        pmid=row["pmid"],
        title=row["title"],
        text=row["text"],
        method=method,
        doi=row["doi"],
        journal=row["journal"],
        year=row["pub_year"],
        evidence_tier=row["evidence_tier"],
        evidence_rank=row["evidence_rank"],
        tier_source=row["tier_source"],
        license=row["license"],
        full_text_available=bool(row["full_text_available"]),
        retracted=bool(row["retracted"]),
        retraction_note=row["retraction_note"],
    )


def _filters(min_tier: str | None, since_year: int | None) -> tuple[str, list]:
    clauses, params = [], []
    if min_tier:
        floor = tiers.rank_of(min_tier)
        if floor is None:
            raise ValueError(f"unknown tier {min_tier!r}")
        # `IS NOT NULL` is what excludes `unknown` rather than admitting it at
        # the bottom of the ranking.
        clauses.append("a.evidence_rank IS NOT NULL AND a.evidence_rank <= ?")
        params.append(floor)
    if since_year:
        clauses.append("(a.pub_year IS NULL OR a.pub_year >= ?)")
        params.append(int(since_year))
    return (" AND ".join(clauses), params)


def _ordered(findings: list[Finding], limit: int) -> list[Finding]:
    """Evidence strength first, retrieval order second. See module docstring.

    Unranked findings sort last; they are the ones whose strength is unknown,
    and an unknown credential should never lead.
    """
    ordered = sorted(
        enumerate(findings),
        key=lambda pair: (pair[1].evidence_rank
                          if pair[1].evidence_rank is not None else 99,
                          pair[0]),
    )
    return [finding for _, finding in ordered][:limit]


def keyword_search(conn: sqlite3.Connection, query: str, *, limit: int = 5,
                   min_tier: str | None = None,
                   since_year: int | None = None) -> list[Finding]:
    match = _fts_query(query)
    if not match:
        return []
    where, params = _filters(min_tier, since_year)
    sql = (f"{_SELECT} JOIN article_chunk_fts f ON f.rowid = c.id "
           f"WHERE article_chunk_fts MATCH ? "
           f"{('AND ' + where) if where else ''} "
           f"ORDER BY bm25(article_chunk_fts) LIMIT ?")
    try:
        rows = conn.execute(sql, [match, *params, limit * 4]).fetchall()
    except sqlite3.OperationalError as exc:
        log.warning("corpus keyword search failed (%s)", type(exc).__name__)
        return []
    return _ordered([_finding(r, "keyword") for r in rows], limit)


def search(conn: sqlite3.Connection, store: "VectorStore", embedder: "Embedder",
           query: str, *, limit: int = 5, min_tier: str | None = None,
           since_year: int | None = None) -> list[Finding]:
    """Semantic search, falling back to keyword when nothing is embedded."""
    embedded = conn.execute(
        "SELECT COUNT(*) AS n FROM article_chunk WHERE embedded_with = ?",
        (embedder.name,)).fetchone()["n"]
    if not embedded:
        return keyword_search(conn, query, limit=limit, min_tier=min_tier,
                              since_year=since_year)

    vector = embedder.embed([query])[0]
    # Over-fetch: filters and tier ordering are applied after ranking, so
    # asking for exactly `limit` could return fewer than requested.
    raw = store.search(vector, embedder_name=embedder.name, limit=limit * 6)
    chunk_ids = [int(r["chunk_id"]) for r in raw]
    if not chunk_ids:
        # The SQLite side says this corpus is embedded, but the vector table
        # returned nothing — missing, emptied, or never written. Degrade to
        # keyword search rather than reporting a silent `no_matches` for a
        # corpus that is still perfectly searchable; mirrors the
        # not-yet-embedded branch above and `_search_records`' handling of
        # the same situation for the personal store.
        return keyword_search(conn, query, limit=limit, min_tier=min_tier,
                              since_year=since_year)

    where, params = _filters(min_tier, since_year)
    placeholders = ",".join("?" * len(chunk_ids))
    sql = (f"{_SELECT} WHERE c.id IN ({placeholders}) "
           f"{('AND ' + where) if where else ''}")
    # Keyed on chunk_id, not article_id: an article can contribute several
    # chunks, and collapsing them into one dict entry per article would
    # silently drop all but the last-fetched chunk before de-duplication
    # even gets a chance to pick the best one.
    by_chunk_id = {int(r["chunk_id"]): r
                   for r in conn.execute(sql, [*chunk_ids, *params]).fetchall()}
    if not by_chunk_id:
        return []

    # Walk hits in the vector store's own relevance order and keep only the
    # first (= strongest-relevance) chunk per article, so relevance survives
    # as the tie-breaker within a tier once `_ordered` re-sorts by strength.
    seen_articles: set[int] = set()
    findings: list[Finding] = []
    for chunk_id in chunk_ids:
        row = by_chunk_id.get(chunk_id)
        if row is None:
            continue  # filtered out by min_tier/since_year, or chunk deleted
        article_id = int(row["article_id"])
        if article_id in seen_articles:
            continue
        seen_articles.add(article_id)
        findings.append(_finding(row, "semantic"))
    return _ordered(findings, limit)


def _fts_query(query: str) -> str:
    """Free text to a safe FTS5 MATCH expression, as `store/vector_store.py`."""
    import re

    tokens = re.findall(r"[A-Za-z0-9]+", query)
    return " OR ".join(f'"{t}"' for t in tokens if len(t) > 1)


# Papers the tool must never present as evidence: a retraction, a trial
# protocol (a plan, not a result), or a record whose design MEDLINE did not
# state. Shared by visit prep and by the lab context block.
UNCITABLE_TIERS = frozenset({tiers.PROTOCOL, tiers.UNKNOWN})


def citable(finding: Finding) -> bool:
    return not finding.retracted and finding.evidence_tier not in UNCITABLE_TIERS


# Evidence tier key -> how a rendered page says it. A key outside the map is
# printed with its underscores replaced by spaces.
TIER_LABELS = {
    "meta_analysis": "meta-analysis",
    "systematic_review": "systematic review",
    "rct": "randomized trial",
    "clinical_trial": "clinical trial",
    "guideline": "guideline",
    "narrative_review": "review",
    "scoping_review": "scoping review",
    "observational": "observational study",
    "case_report": "case report",
}


def tier_label(tier: str) -> str:
    return TIER_LABELS.get(tier, tier.replace("_", " "))


class _OnceEmbedder:
    """`hits` decides semantic-versus-keyword on every call, so a corpus
    whose embedder is down would log the same warning once per question.
    This proxy sits between `hits` and the real embedder: the first failed
    `embed` trips a flag, and `factory` then hands `hits` nothing at all,
    so the rest of the run goes straight to keyword search. When the
    embedder works, `hits` sees exactly what it would have seen."""

    def __init__(self, embedder_factory) -> None:
        self._make = embedder_factory
        self._embedder = None
        self.failed = False

    @property
    def factory(self):
        return None if self.failed or self._make is None else self

    def __call__(self):
        if self._embedder is None:
            try:
                self._embedder = self._make()
            except Exception:
                self.failed = True
                raise
        return self

    @property
    def name(self) -> str:
        return self._embedder.name

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            return self._embedder.embed(texts)
        except Exception:
            self.failed = True
            raise


def once_embedder(embedder_factory):
    """See `_OnceEmbedder`. Returns the proxy; read `.factory` on each call."""
    return _OnceEmbedder(embedder_factory)


def hits(conn: sqlite3.Connection, query: str, *, limit: int = 5,
         min_tier: str | None = None, since_year: int | None = None,
         vector_path=None, embedder_factory=None) -> list[Finding]:
    """Semantic where possible, keyword otherwise. Never raises for
    retrieval; a bad `min_tier` (ValueError) still propagates.

    Shared by the agent's `search_medical_literature` tool and by visit
    prep, so both see the same corpus the same way."""
    if embedder_factory is not None and vector_path:
        from ..store import vector_store
        from . import embed as lit_embed
        try:
            embedder = embedder_factory()
            store = vector_store.VectorStore(vector_path, table_name=lit_embed.TABLE_NAME)
            return search(conn, store, embedder, query, limit=limit,
                          min_tier=min_tier, since_year=since_year)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - retrieval must not kill a turn
            log.warning("corpus semantic search unavailable (%s)", type(exc).__name__)
    return keyword_search(conn, query, limit=limit, min_tier=min_tier,
                          since_year=since_year)


__all__ = ["Finding", "keyword_search", "search", "hits", "TABLE_NAME"]
