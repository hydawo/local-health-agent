"""Building a corpus from parsed articles, and describing what it holds.

`coverage()` is not a status nicety. It is what the search tool returns when it
finds nothing, so that "my corpus does not cover this" is a better-supported
answer than reaching for recalled medical knowledge — which is the failure this
whole feature exists to fix.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ..logging_setup import get_logger
from . import schema
from .medline import ParsedArticle

log = get_logger("literature.corpus")

# Abstracts are short. One chunk per abstract keeps a finding whole, which is
# what makes it quotable with a single citation; only unusually long ones split.
MAX_CHUNK_CHARS = 2000


@dataclass
class BuildStats:
    articles: int = 0
    chunks: int = 0
    skipped: int = 0


def _chunks(text: str) -> list[str]:
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    parts, current = [], ""
    for paragraph in text.split("\n\n"):
        if current and len(current) + len(paragraph) + 2 > MAX_CHUNK_CHARS:
            parts.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        parts.append(current)
    return parts


def build(conn: sqlite3.Connection, articles: list[ParsedArticle], *,
          slug: str, version: str, license: str,
          full_text: bool = False) -> BuildStats:
    """Write a pack and its articles. Re-running with the same pmid updates."""
    stats = BuildStats()
    built_at = datetime.now().astimezone().isoformat(timespec="seconds")

    conn.execute(
        "INSERT INTO pack(slug, title, version, built_at, article_count) "
        "VALUES(?, ?, ?, ?, 0) ON CONFLICT(slug, version) DO UPDATE SET "
        "built_at = excluded.built_at",
        (slug, slug, version, built_at),
    )
    pack_id = conn.execute(
        "SELECT id FROM pack WHERE slug = ? AND version = ?",
        (slug, version)).fetchone()["id"]

    for article in articles:
        body = article.abstract.strip()
        if not body:
            # Nothing to retrieve means nothing to cite. A title-only record
            # would be findable and then useless.
            stats.skipped += 1
            continue

        conn.execute(
            "INSERT INTO article(pack_id, pmid, doi, title, journal, "
            "pub_year, publication_types, evidence_tier, evidence_rank, "
            "tier_source, license, full_text_available, retracted, "
            "retraction_note, fetched_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            # Every mutable column is refreshed here, not just abstract/tier. A
            # rebuild that silently kept a stale `license` would defeat the
            # column's whole purpose: the PMC Open Access subset mixes CC-BY,
            # CC-BY-NC and CC-BY-NC-ND, so a corrected license has to land.
            "ON CONFLICT(pack_id, pmid) DO UPDATE SET "
            "doi = excluded.doi, title = excluded.title, "
            "journal = excluded.journal, "
            "pub_year = excluded.pub_year, "
            "publication_types = excluded.publication_types, "
            "evidence_tier = excluded.evidence_tier, "
            "evidence_rank = excluded.evidence_rank, "
            "tier_source = excluded.tier_source, license = excluded.license, "
            "full_text_available = excluded.full_text_available, "
            "retracted = excluded.retracted, "
            "retraction_note = excluded.retraction_note, "
            "fetched_at = excluded.fetched_at",
            (pack_id, article.pmid, article.doi, article.title,
             article.journal, article.pub_year,
             json.dumps(article.publication_types), article.evidence_tier,
             article.evidence_rank, article.tier_source, license,
             int(full_text), int(article.retracted), article.retraction_note,
             built_at),
        )
        article_id = conn.execute(
            "SELECT id FROM article WHERE pack_id = ? AND pmid = ?",
            (pack_id, article.pmid)).fetchone()["id"]

        conn.execute("DELETE FROM article_chunk WHERE article_id = ?",
                     (article_id,))
        conn.execute("DELETE FROM mesh_term WHERE article_id = ?", (article_id,))
        major = set(article.major_terms)
        conn.executemany(
            "INSERT INTO mesh_term(article_id, term, major) VALUES(?, ?, ?)",
            [(article_id, term, int(term in major))
             for term in article.mesh_terms])

        for index, chunk in enumerate(_chunks(body)):
            conn.execute(
                "INSERT INTO article_chunk(article_id, section, chunk_index, "
                "text, char_count) VALUES(?, ?, ?, ?, ?)",
                (article_id, "abstract", index, chunk, len(chunk)))
            stats.chunks += 1
        stats.articles += 1

    conn.execute(
        "UPDATE pack SET article_count = "
        "(SELECT COUNT(*) FROM article WHERE pack_id = ?) WHERE id = ?",
        (pack_id, pack_id))
    # rebuild_chunk_fts() issues its own commit, and calling it here — before
    # any commit of ours — makes that commit the one that closes out the whole
    # build. A crash between a separate `conn.commit()` here and the rebuild
    # would leave article_chunk holding new rows the FTS index doesn't know
    # about yet, a real window where search and the source table disagree.
    schema.rebuild_chunk_fts(conn)
    log.info("built pack %s@%s: %d articles, %d chunks, %d skipped",
             slug, version, stats.articles, stats.chunks, stats.skipped)
    return stats


def coverage(conn: sqlite3.Connection, *, max_topics: int = 20) -> dict:
    """What this corpus actually holds. Returned whenever a search misses.

    `topics` lists subjects, not populations. Counting every MeSH heading
    alike gave, on 2,000 real abstracts, `Humans, Male, Female, Middle Aged,
    Adult, Aged, ...`: true of the corpus and useless as an answer to "what
    does your literature cover?". MEDLINE marks the subject headings itself
    (`MajorTopicYN="Y"`, stored as `mesh_term.major`), so only those are
    counted. A corpus with no major flags at all (an export that omits the
    attribute, or a hand-written one) falls back to every heading rather than
    reporting nothing; `topics_from` says which list the reader is looking at,
    and `mesh_terms` / `major_topics` give the distinct counts behind it.

    Ties are broken alphabetically, on purpose: the previous version left the
    order to whichever index SQLite happened to scan.
    """
    packs = [f"{r['slug']}@{r['version']}" for r in conn.execute(
        "SELECT slug, version FROM pack ORDER BY slug")]
    total = conn.execute("SELECT COUNT(*) AS n FROM article").fetchone()["n"]
    if not total:
        return {"packs": packs, "article_count": 0, "topics": [],
                "topics_from": "major_topics", "mesh_terms": 0,
                "major_topics": 0, "tiers": {},
                "year_range": [None, None], "built": None}

    counts = conn.execute(
        "SELECT COUNT(DISTINCT term) AS all_terms, "
        "COUNT(DISTINCT CASE WHEN major = 1 THEN term END) AS major_terms "
        "FROM mesh_term").fetchone()
    topics_from = "major_topics" if counts["major_terms"] else "all_mesh_terms"
    topics = [r["term"] for r in conn.execute(
        "SELECT term, COUNT(*) AS n FROM mesh_term "
        + ("WHERE major = 1 " if topics_from == "major_topics" else "")
        + "GROUP BY term ORDER BY n DESC, term LIMIT ?", (max_topics,))]
    tiers_seen = {r["evidence_tier"]: r["n"] for r in conn.execute(
        "SELECT evidence_tier, COUNT(*) AS n FROM article "
        "GROUP BY evidence_tier ORDER BY n DESC")}
    years = conn.execute(
        "SELECT MIN(pub_year) AS lo, MAX(pub_year) AS hi FROM article "
        "WHERE pub_year IS NOT NULL").fetchone()
    built = conn.execute("SELECT MAX(built_at) AS b FROM pack").fetchone()["b"]

    return {
        "packs": packs,
        "article_count": total,
        "topics": topics,
        "topics_from": topics_from,
        "mesh_terms": counts["all_terms"],
        "major_topics": counts["major_terms"],
        "tiers": tiers_seen,
        # As the source states it: ahead-of-print records can carry a future
        # year, and medline._year() explains why that is left alone.
        "year_range": [years["lo"], years["hi"]],
        "built": built,
    }
