# Literature Packs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Distributable single-topic literature packs: a pack file format, a maintainer command that builds one from NCBI E-utilities, a user command that installs one from a GitHub Release behind a consent notice, cross-pack deduplication in the corpus schema, and the threat-model and test changes that keep the network surface to exactly one new file.

**Architecture:** `literature/packs.py` (catalog + file format, no network) and `literature/fetch/` (`client.py` the only transport, `eutils.py`, `packs.py` installer) are new; `corpus.build` moves to a `article_pack` link table (schema v4); `consent.py` gains a second notice; `cli.py` gains `literature packs|install|build-pack` and `literature-consent`. The ask path never imports `fetch`, enforced by the existing import-graph test.

**Tech Stack:** Python 3.11+, stdlib `urllib.request` (the one transport), `gzip`, `json`, `hashlib`, SQLite, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-20-literature-packs-design.md`. Read it first.

## Global Constraints

- **Exactly one file under `health_agent/literature/` may import a transport:** `literature/fetch/client.py`. `eutils.py` and `fetch/packs.py` import `client`, never `urllib`. Only two hosts are allowed: `eutils.ncbi.nlm.nih.gov` and `github.com` (with `objects.githubusercontent.com` as the redirect target).
- **The ask path never imports `health_agent.literature.fetch`.** `tests/test_no_network.py::test_the_query_path_cannot_reach_a_fetcher`'s import-graph assertion stays unchanged and must pass.
- **No test opens a socket.** `client.get` is the seam; tests monkeypatch it.
- **Tiers are re-derived at install** from `publication_types` via `tiers.resolve`; the pack's stored tier is never trusted.
- **Consent before any fetch.** `install` and `build-pack` show the literature notice and stop unless accepted; `--yes` records consent for scripted use, exactly as the cloud tier does.
- **Packs stay at body-system level.** The catalog holds `sample`, `cardiovascular`, `metabolic`, `sleep`, `exercise` and nothing finer.
- **Schema v4**: `article.pmid` globally unique, `article_pack(article_id, pack_id)` link table, `article.pack_id` removed. `--rebuild` and the version check on open handle migration.
- **Voice**: docstrings say why; CLI output and docs have no em dashes; tools return data.
- **Commit messages** end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- **`pytest -q` green before every commit.** Baseline 538.

---

### Task 1: Schema v4 and the link table

**Files:**
- Modify: `health_agent/literature/schema.py` (version 4; `article` loses `pack_id`; `article_pack`; drop `idx_article_pmid` in favour of a unique index on `pmid`)
- Modify: `health_agent/literature/corpus.py` (`build()` links through `article_pack`; new `remove_pack()`; `coverage()` gains `shared_articles`)
- Modify: `health_agent/cli.py` `cmd_literature_status` (print shared count)
- Test: `tests/test_literature_schema.py`, `tests/test_literature_corpus.py`

**Interfaces:**
- Produces: `corpus.build(conn, articles, *, slug, version, license, full_text=False) -> BuildStats` (signature unchanged; semantics: articles upserted by pmid, linked to the pack; articles previously in this pack but absent from `articles` are unlinked, and deleted if no other pack links them); `corpus.remove_pack(conn, slug) -> int` (articles removed); `coverage()["shared_articles"]: int`.

- [ ] **Step 1: Failing schema tests**

Append to `tests/test_literature_schema.py`:

```python
def test_schema_version_is_4():
    assert schema.LITERATURE_SCHEMA_VERSION == 4


def test_articles_are_unique_by_pmid_and_linked_to_packs(tmp_path):
    """One paper, many packs. An 'exercise and hypertension' article belongs
    to both `exercise` and `cardiovascular`; storing it twice would return
    the same finding twice under two pack names."""
    import sqlite3

    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(article)")}
    assert "pack_id" not in columns
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "article_pack" in names
    conn.execute("INSERT INTO article(pmid, title, evidence_tier, tier_source, "
                 "license) VALUES('1', 't', 'rct', 'publication_type', 'x')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO article(pmid, title, evidence_tier, "
                     "tier_source, license) VALUES('1', 't2', 'rct', "
                     "'publication_type', 'x')")
    conn.close()
```

- [ ] **Step 2: Failing corpus tests**

Append to `tests/test_literature_corpus.py`:

```python
def _one(pmid: str, title: str = "t") -> medline.ParsedArticle:
    return medline.ParsedArticle(pmid=pmid, title=title, abstract="Some abstract text.",
                                 publication_types=["Randomized Controlled Trial"],
                                 evidence_tier="rct", evidence_rank=3,
                                 tier_source="publication_type")


def test_two_packs_sharing_a_pmid_store_it_once(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    corpus.build(conn, [_one("1"), _one("2")], slug="exercise", version="1", license="x")
    corpus.build(conn, [_one("2"), _one("3")], slug="cardiovascular", version="1", license="x")

    assert conn.execute("SELECT COUNT(*) AS n FROM article").fetchone()["n"] == 3
    links = conn.execute("SELECT COUNT(*) AS n FROM article_pack").fetchone()["n"]
    assert links == 4
    counts = {r["slug"]: r["article_count"] for r in conn.execute("SELECT slug, article_count FROM pack")}
    assert counts == {"exercise": 2, "cardiovascular": 2}
    assert corpus.coverage(conn)["shared_articles"] == 1
    conn.close()


def test_rebuilding_a_pack_unlinks_articles_it_no_longer_holds(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    corpus.build(conn, [_one("1"), _one("2")], slug="sleep", version="1", license="x")
    corpus.build(conn, [_one("2")], slug="sleep", version="1", license="x")
    assert conn.execute("SELECT COUNT(*) AS n FROM article").fetchone()["n"] == 1
    assert conn.execute("SELECT pmid FROM article").fetchone()["pmid"] == "2"
    conn.close()


def test_removing_a_pack_keeps_articles_another_pack_still_links(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    corpus.build(conn, [_one("1"), _one("2")], slug="exercise", version="1", license="x")
    corpus.build(conn, [_one("2")], slug="cardiovascular", version="1", license="x")
    removed = corpus.remove_pack(conn, "exercise")
    assert removed == 1
    pmids = {r["pmid"] for r in conn.execute("SELECT pmid FROM article")}
    assert pmids == {"2"}
    assert [r["slug"] for r in conn.execute("SELECT slug FROM pack")] == ["cardiovascular"]
    conn.close()


def test_a_newer_pack_version_replaces_the_older_one(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    corpus.build(conn, [_one("1")], slug="sleep", version="2026.09", license="x")
    corpus.build(conn, [_one("1"), _one("2")], slug="sleep", version="2026.10", license="x")
    packs = [(r["slug"], r["version"]) for r in conn.execute("SELECT slug, version FROM pack")]
    assert packs == [("sleep", "2026.10")]
    assert conn.execute("SELECT COUNT(*) AS n FROM article").fetchone()["n"] == 2
    conn.close()
```

Check the existing `_articles` helper and imports at the top of that file; `medline` is imported there already.

- [ ] **Step 3: Run to see them fail**

Run: `pytest tests/test_literature_schema.py tests/test_literature_corpus.py -q`
Expected: failures on version 3, `pack_id` present, `article_pack` missing, `remove_pack` missing, `shared_articles` missing.

- [ ] **Step 4: Schema**

In `schema.py`: `LITERATURE_SCHEMA_VERSION = 4`. In `SCHEMA_SQL`, remove `pack_id INTEGER NOT NULL REFERENCES pack(id) ON DELETE CASCADE,` from `article` and add a comment where it was:

```sql
    -- No pack_id. An article belongs to every pack that holds it, through
    -- article_pack below, and is stored once by PMID: an "exercise and
    -- hypertension" paper installed from two packs must be one finding,
    -- not two. (v4; v1-v3 keyed articles per pack.)
```

Replace `CREATE UNIQUE INDEX IF NOT EXISTS idx_article_pmid ON article(pack_id, pmid);` with `CREATE UNIQUE INDEX IF NOT EXISTS idx_article_pmid ON article(pmid);` and add after the `article` indexes:

```sql
CREATE TABLE IF NOT EXISTS article_pack (
    article_id INTEGER NOT NULL REFERENCES article(id) ON DELETE CASCADE,
    pack_id    INTEGER NOT NULL REFERENCES pack(id) ON DELETE CASCADE,
    PRIMARY KEY (article_id, pack_id)
);

CREATE INDEX IF NOT EXISTS idx_article_pack_pack ON article_pack(pack_id);
```

Change `pack`'s `UNIQUE(slug, version)` to `UNIQUE(slug)`: one installed version per pack; a newer version replaces the row. Update the `pack` table comment accordingly.

- [ ] **Step 5: `corpus.build`, `remove_pack`, `coverage`**

Rewrite `build()`:

```python
def build(conn: sqlite3.Connection, articles: list[ParsedArticle], *,
          slug: str, version: str, license: str,
          full_text: bool = False) -> BuildStats:
    """Write a pack and its articles. One row per PMID, linked to the pack.

    Re-running with the same slug replaces the pack's membership: articles
    it no longer holds are unlinked, and deleted if no other pack links
    them. A newer version replaces the older one's row, so the `pack` table
    always says which version of each pack is installed. Every mutable
    article column is refreshed on conflict, not just abstract/tier, because
    a corrected license or a retraction has to land.
    """
    stats = BuildStats()
    built_at = datetime.now().astimezone().isoformat(timespec="seconds")

    conn.execute(
        "INSERT INTO pack(slug, title, version, built_at, article_count) "
        "VALUES(?, ?, ?, ?, 0) ON CONFLICT(slug) DO UPDATE SET "
        "version = excluded.version, built_at = excluded.built_at",
        (slug, slug, version, built_at),
    )
    pack_id = conn.execute("SELECT id FROM pack WHERE slug = ?",
                           (slug,)).fetchone()["id"]
    previously_linked = {r["article_id"] for r in conn.execute(
        "SELECT article_id FROM article_pack WHERE pack_id = ?", (pack_id,))}
    now_linked: set[int] = set()

    for article in articles:
        body = article.abstract.strip()
        if not body:
            stats.skipped += 1
            continue
        conn.execute(
            "INSERT INTO article(pmid, doi, title, journal, pub_year, "
            "publication_types, evidence_tier, evidence_rank, tier_source, "
            "license, full_text_available, retracted, retraction_note, "
            "fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(pmid) DO UPDATE SET "
            "doi = excluded.doi, title = excluded.title, "
            "journal = excluded.journal, pub_year = excluded.pub_year, "
            "publication_types = excluded.publication_types, "
            "evidence_tier = excluded.evidence_tier, "
            "evidence_rank = excluded.evidence_rank, "
            "tier_source = excluded.tier_source, license = excluded.license, "
            "full_text_available = excluded.full_text_available, "
            "retracted = excluded.retracted, "
            "retraction_note = excluded.retraction_note, "
            "fetched_at = excluded.fetched_at",
            (article.pmid, article.doi, article.title, article.journal,
             article.pub_year, json.dumps(article.publication_types),
             article.evidence_tier, article.evidence_rank, article.tier_source,
             license, int(full_text), int(article.retracted),
             article.retraction_note, built_at),
        )
        article_id = conn.execute("SELECT id FROM article WHERE pmid = ?",
                                  (article.pmid,)).fetchone()["id"]
        conn.execute("INSERT OR IGNORE INTO article_pack(article_id, pack_id) "
                     "VALUES(?, ?)", (article_id, pack_id))
        now_linked.add(article_id)

        conn.execute("DELETE FROM article_chunk WHERE article_id = ?", (article_id,))
        conn.execute("DELETE FROM mesh_term WHERE article_id = ?", (article_id,))
        major = set(article.major_terms)
        conn.executemany(
            "INSERT INTO mesh_term(article_id, term, major) VALUES(?, ?, ?)",
            [(article_id, term, int(term in major)) for term in article.mesh_terms])
        for index, chunk in enumerate(_chunks(body)):
            conn.execute(
                "INSERT INTO article_chunk(article_id, section, chunk_index, "
                "text, char_count) VALUES(?, ?, ?, ?, ?)",
                (article_id, "abstract", index, chunk, len(chunk)))
            stats.chunks += 1
        stats.articles += 1

    for article_id in previously_linked - now_linked:
        conn.execute("DELETE FROM article_pack WHERE article_id = ? AND pack_id = ?",
                     (article_id, pack_id))
    _delete_orphans(conn)
    _refresh_count(conn, pack_id)
    schema.rebuild_chunk_fts(conn)
    log.info("built pack %s@%s: %d articles, %d chunks, %d skipped",
             slug, version, stats.articles, stats.chunks, stats.skipped)
    return stats


def _delete_orphans(conn: sqlite3.Connection) -> int:
    """An article no pack links is gone with the pack that brought it."""
    cursor = conn.execute(
        "DELETE FROM article WHERE id NOT IN (SELECT article_id FROM article_pack)")
    return cursor.rowcount


def _refresh_count(conn: sqlite3.Connection, pack_id: int) -> None:
    conn.execute(
        "UPDATE pack SET article_count = "
        "(SELECT COUNT(*) FROM article_pack WHERE pack_id = ?) WHERE id = ?",
        (pack_id, pack_id))


def remove_pack(conn: sqlite3.Connection, slug: str) -> int:
    """Drop a pack and the articles only it held. Returns articles removed."""
    row = conn.execute("SELECT id FROM pack WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise KeyError(slug)
    conn.execute("DELETE FROM pack WHERE id = ?", (row["id"],))
    removed = _delete_orphans(conn)
    schema.rebuild_chunk_fts(conn)
    return removed
```

Note the existing `DELETE FROM article_chunk` for a re-linked article: chunks are rebuilt every time an article is written by any pack, so the vector rows for that chunk id set become stale. Check how `embed_pending` keys rows (`chunk_id`); since chunks are deleted and re-inserted with new ids, the old LanceDB rows are orphaned. Add to `build()` a note and, after the loop, call `store`-free cleanup is impossible here (no store handle); instead have `cmd_literature_install`/`build` in the CLI drop and re-embed only when `stats.articles > 0`, as the CLI already re-embeds. Record this in the task report; the whole-branch review should judge whether orphaned vector rows are a hazard (they are keyed by chunk id and `search` looks chunk ids up in SQLite, so an orphan is skipped, which `store.search` already tolerates).

In `coverage()`, add to the returned dict:

```python
        "shared_articles": conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT article_id FROM article_pack "
            "GROUP BY article_id HAVING COUNT(*) > 1)").fetchone()["n"],
```

(and `0` in the empty-corpus branch). In `cmd_literature_status`, after the `packs:` line print `f"shared:   {report['shared_articles']} article(s) in more than one pack"` when the count is nonzero.

- [ ] **Step 6: Run everything**

Run: `pytest -q`. Expected: all pass, including `tests/test_cli.py`'s rebuild test and `offline-check` tests. `grep -rn "pack_id" health_agent/` must show only `article_pack` usages and the `pack` table.

- [ ] **Step 7: Commit**

```bash
git add health_agent/literature/schema.py health_agent/literature/corpus.py health_agent/cli.py tests/test_literature_schema.py tests/test_literature_corpus.py
git commit -m "Corpus schema v4: articles are unique by PMID and linked to packs

An 'exercise and hypertension' paper belongs to both packs. Keyed per
pack, installing both stored it twice and search returned the same
finding twice under two names. One row per PMID now, with article_pack
recording membership; a pack that no longer holds an article unlinks it,
and an article no pack links is deleted. One installed version per pack,
so the pack table says what is installed rather than accumulating.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: The catalog and the pack file format

**Files:**
- Create: `health_agent/literature/packs.py`
- Test: `tests/test_literature_packs.py`

**Interfaces:**
- Produces: `packs.CATALOG: dict[str, PackSpec]`; `PackSpec(slug, title, description, query, evidence_filter: tuple[str, ...], since_year: int | None, max_articles: int | None)`; `packs.write_pack(path, spec, version, articles, *, license) -> Manifest`; `packs.read_pack(path) -> tuple[Manifest, list[ParsedArticle]]`; `packs.sha256_file(path) -> str`; `packs.PACK_LICENSE: str`; `packs.asset_name(slug, version) -> str`; `Manifest` dataclass with the fields in spec §2.

- [ ] **Step 1: Failing tests**

Create `tests/test_literature_packs.py`:

```python
"""Pack files: the catalog is code, the file is parsed articles, digests hold."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from health_agent.literature import medline, packs


def _articles():
    return [
        medline.ParsedArticle(pmid="1", title="A", abstract="Alpha text.",
                              journal="J", pub_year=2024, doi="10.1/a",
                              publication_types=["Journal Article", "Meta-Analysis"],
                              mesh_terms=["Sleep", "Humans"], major_terms=["Sleep"],
                              evidence_tier="meta_analysis", evidence_rank=1,
                              tier_source="publication_type"),
        medline.ParsedArticle(pmid="2", title="B", abstract="Beta text.",
                              publication_types=["Journal Article"],
                              retracted=True, retraction_note="withdrawn"),
    ]


def test_catalog_holds_exactly_the_five_body_system_packs():
    assert set(packs.CATALOG) == {"sample", "cardiovascular", "metabolic", "sleep", "exercise"}
    for spec in packs.CATALOG.values():
        assert spec.query and spec.title and spec.description
    assert packs.CATALOG["sample"].max_articles == 2000
    assert packs.CATALOG["sample"].evidence_filter == ()
    for slug in ("cardiovascular", "metabolic", "sleep", "exercise"):
        assert "Meta-Analysis" in packs.CATALOG[slug].evidence_filter
        assert packs.CATALOG[slug].since_year == 2015


def test_write_then_read_round_trips_articles_and_manifest(tmp_path):
    path = tmp_path / packs.asset_name("sleep", "2026.09")
    manifest = packs.write_pack(path, packs.CATALOG["sleep"], "2026.09", _articles(),
                                license=packs.PACK_LICENSE)
    assert path.name == "sleep-2026.09.jsonl.gz"
    assert manifest.article_count == 2
    assert manifest.slug == "sleep" and manifest.format == 1

    read_manifest, articles = packs.read_pack(path)
    assert read_manifest == manifest
    assert articles == _articles()


def test_first_line_is_the_manifest_and_the_digest_covers_the_articles(tmp_path):
    path = tmp_path / "x.jsonl.gz"
    manifest = packs.write_pack(path, packs.CATALOG["sleep"], "1", _articles(),
                                license="L")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        first = json.loads(fh.readline())
        rest = fh.read()
    assert first["format"] == 1 and first["slug"] == "sleep"
    assert manifest.sha256_of_articles == packs.sha256_text(rest)


def test_read_refuses_a_pack_whose_article_digest_does_not_match(tmp_path):
    path = tmp_path / "x.jsonl.gz"
    packs.write_pack(path, packs.CATALOG["sleep"], "1", _articles(), license="L")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    lines[1] = lines[1].replace("Alpha text.", "Tampered text.")
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with pytest.raises(packs.PackError):
        packs.read_pack(path)


def test_read_refuses_an_unknown_format_version(tmp_path):
    path = tmp_path / "x.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"format": 99, "slug": "sleep"}) + "\n")
    with pytest.raises(packs.PackError):
        packs.read_pack(path)


def test_sha256_file_matches_hashlib(tmp_path):
    import hashlib
    p = tmp_path / "f"
    p.write_bytes(b"abc")
    assert packs.sha256_file(p) == hashlib.sha256(b"abc").hexdigest()
```

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_literature_packs.py -q`. Expected: `ImportError`.

- [ ] **Step 3: Write `packs.py`**

```python
"""Literature packs: what they are, and the file that carries one.

A pack is a single-topic slice of PubMed that a person installs as one
download. Two rules from the design are enforced by this module's shape:

**The catalog is code.** One `PackSpec` per pack drives both the
maintainer's build (the query) and the user's install (the asset name), so
the two can never describe different packs.

**Packs stay at the body-system level.** The download is itself a
disclosure: which pack a person fetches is visible to the host. `sleep` says
almost nothing about a person; a `type-2-diabetes` pack would say a lot.
Adding a pack below that line is a design change, not a catalog edit.

The file is gzipped JSON lines: a manifest, then one parsed article per
line. Parsed, not raw MEDLINE XML, because the parsed record is ~3 KB and
the XML ~25 KB, and nothing the tool uses is lost: `publication_types` stays
verbatim so the installer re-derives tiers itself rather than trusting the
pack. A parser fix is the maintainer's problem and means a new pack
version, which is the lineage unit the `pack` table was designed for.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .medline import ParsedArticle

FORMAT = 1

# NLM's terms for MEDLINE citation records and abstracts. Not a Creative
# Commons licence: packs carry no full text. Written into article.license
# for every row of a pack, per LICENSES.md's rule that licenses are
# recorded per article, never asserted globally.
PACK_LICENSE = "MEDLINE/PubMed citation record and abstract; NLM terms of use"

# The evidence slice that keeps a topical pack shippable. `sample` uses no
# filter so the tiers show as they really are, `unknown` included.
STRONG_EVIDENCE: tuple[str, ...] = (
    "Meta-Analysis", "Systematic Review", "Practice Guideline", "Guideline",
    "Randomized Controlled Trial",
)


@dataclass(frozen=True)
class PackSpec:
    slug: str
    title: str
    description: str
    query: str                      # PubMed search term, MeSH based
    evidence_filter: tuple[str, ...] = ()
    since_year: int | None = 2015
    max_articles: int | None = None

    def search_term(self) -> str:
        """The full E-utilities term: topic, evidence slice, years, abstracts."""
        parts = [f"({self.query})"]
        if self.evidence_filter:
            types = " OR ".join(f'"{t}"[Publication Type]' for t in self.evidence_filter)
            parts.append(f"({types})")
        if self.since_year:
            parts.append(f"{self.since_year}:3000[dp]")
        parts.append("hasabstract[text]")
        return " AND ".join(parts)


_ALL_AREAS = (
    '"Cholesterol"[MeSH] OR "Hypertension"[MeSH] OR "Cardiovascular Diseases"[MeSH] '
    'OR "Diabetes Mellitus, Type 2"[MeSH] OR "Blood Glucose"[MeSH] OR "Obesity"[MeSH] '
    'OR "Sleep"[MeSH] OR "Exercise"[MeSH]'
)

CATALOG: dict[str, PackSpec] = {
    "sample": PackSpec(
        slug="sample", title="Sample",
        description="About 2,000 recent papers across all four areas, for a first try.",
        query=_ALL_AREAS, evidence_filter=(), since_year=None, max_articles=2000),
    "cardiovascular": PackSpec(
        slug="cardiovascular", title="Cardiovascular",
        description="Blood pressure, cholesterol, HDL, LDL, cardiovascular disease.",
        query='"Hypertension"[MeSH] OR "Cholesterol"[MeSH] OR "Cholesterol, HDL"[MeSH] '
              'OR "Cholesterol, LDL"[MeSH] OR "Cardiovascular Diseases"[MeSH]',
        evidence_filter=STRONG_EVIDENCE),
    "metabolic": PackSpec(
        slug="metabolic", title="Metabolic",
        description="Blood glucose, HbA1c, type 2 diabetes, obesity, thyroid.",
        query='"Diabetes Mellitus, Type 2"[MeSH] OR "Blood Glucose"[MeSH] '
              'OR "Glycated Hemoglobin"[MeSH] OR "Obesity"[MeSH] OR "Thyroid Diseases"[MeSH]',
        evidence_filter=STRONG_EVIDENCE),
    "sleep": PackSpec(
        slug="sleep", title="Sleep",
        description="Sleep duration, sleep quality, sleep disorders.",
        query='"Sleep"[MeSH] OR "Sleep Wake Disorders"[MeSH] OR "Sleep Quality"[MeSH]',
        evidence_filter=STRONG_EVIDENCE),
    "exercise": PackSpec(
        slug="exercise", title="Exercise",
        description="Physical activity, fitness, training, recovery.",
        query='"Exercise"[MeSH] OR "Physical Fitness"[MeSH] OR "Exercise Therapy"[MeSH]',
        evidence_filter=STRONG_EVIDENCE),
}


@dataclass(frozen=True)
class Manifest:
    format: int
    slug: str
    version: str
    title: str
    description: str
    built_at: str
    article_count: int
    query: str
    evidence_filter: list[str]
    since_year: int | None
    source: str
    license: str
    sha256_of_articles: str


class PackError(RuntimeError):
    """A pack file that cannot be trusted: wrong format, or a digest mismatch."""


def asset_name(slug: str, version: str) -> str:
    return f"{slug}-{version}.jsonl.gz"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _article_line(article: ParsedArticle) -> str:
    return json.dumps(asdict(article), sort_keys=True, ensure_ascii=False)


def write_pack(path: Path, spec: PackSpec, version: str,
               articles: list[ParsedArticle], *, license: str) -> Manifest:
    """Write manifest + articles. The digest covers the article lines only,
    so the manifest can carry it."""
    body = "".join(_article_line(a) + "\n" for a in articles)
    manifest = Manifest(
        format=FORMAT, slug=spec.slug, version=version, title=spec.title,
        description=spec.description,
        built_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        article_count=len(articles), query=spec.search_term(),
        evidence_filter=list(spec.evidence_filter), since_year=spec.since_year,
        source="PubMed/MEDLINE via NCBI E-utilities", license=license,
        sha256_of_articles=sha256_text(body),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(manifest), sort_keys=True) + "\n")
        fh.write(body)
    return manifest


def read_pack(path: Path) -> tuple[Manifest, list[ParsedArticle]]:
    """Read and verify. A digest mismatch or unknown format raises before any
    article is returned, so an installer cannot half-trust a file."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        first = fh.readline()
        body = fh.read()
    try:
        raw = json.loads(first)
    except json.JSONDecodeError as exc:
        raise PackError(f"{path.name}: first line is not a manifest") from exc
    if raw.get("format") != FORMAT:
        raise PackError(f"{path.name}: pack format {raw.get('format')!r}, "
                        f"this build reads format {FORMAT}")
    manifest = Manifest(**raw)
    if sha256_text(body) != manifest.sha256_of_articles:
        raise PackError(f"{path.name}: article digest does not match the manifest")
    articles = [ParsedArticle(**json.loads(line)) for line in body.splitlines() if line]
    if len(articles) != manifest.article_count:
        raise PackError(f"{path.name}: manifest says {manifest.article_count} "
                        f"articles, file holds {len(articles)}")
    return manifest, articles


__all__ = ["CATALOG", "FORMAT", "Manifest", "PackError", "PackSpec",
           "PACK_LICENSE", "STRONG_EVIDENCE", "asset_name", "read_pack",
           "sha256_file", "sha256_text", "write_pack"]
```

`ParsedArticle(**json.loads(line))` requires every field to be a constructor argument; it is a plain dataclass, so `asdict` round-trips. If a field is added to `ParsedArticle` later, old packs still load because missing keys take defaults only if you build the kwargs with `{f: raw.get(f, default)}`; do that instead of `**raw` (iterate `dataclasses.fields(ParsedArticle)`), so a format-1 pack survives a new optional field.

- [ ] **Step 4: Run, then full suite, commit**

Run: `pytest tests/test_literature_packs.py -q` then `pytest -q`.

```bash
git add health_agent/literature/packs.py tests/test_literature_packs.py
git commit -m "Pack catalog and file format

Five packs at the body-system level, because the download itself reveals
which one a person picked. The file is gzipped JSON lines of parsed
articles (about 3 KB each against 25 KB of MEDLINE XML), manifest first,
with a digest over the article lines that read_pack verifies before
returning a single article. publication_types stays verbatim so the
installer re-derives tiers rather than trusting the pack.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: The network package

**Files:**
- Create: `health_agent/literature/fetch/__init__.py`, `client.py`, `eutils.py`, `packs.py`
- Create: `tests/fixtures/literature/esearch.xml`, `tests/fixtures/literature/efetch_batch.xml` (synthetic, tiny)
- Modify: `tests/test_no_network.py::test_the_query_path_cannot_reach_a_fetcher` (text scan exemption for `fetch/client.py`, plus the in-package assertion), `tests/test_embeddings_and_search.py::test_the_network_surface_is_exactly_two_modules` → three
- Test: `tests/test_literature_fetch.py`

**Interfaces:**
- Produces: `client.get(url: str, *, timeout: float = 60.0) -> bytes` (raises `client.FetchError(host, status, message)`; refuses hosts outside `client.ALLOWED_HOSTS`); `client.USER_AGENT`; `eutils.search(term, *, get=client.get) -> SearchHandle(count, webenv, query_key)`; `eutils.fetch_all(handle, *, max_articles, get=client.get, sleep=time.sleep, batch=500) -> list[ParsedArticle]`; `fetch.packs.release_url(slug, version) -> str`; `fetch.packs.download(slug, version, dest_dir, *, get=client.get) -> Path` (downloads asset and `.sha256`, verifies, returns the pack path); `fetch.packs.PACK_RELEASE_TAG`, `fetch.packs.REPO`.

- [ ] **Step 1: Fixtures**

`tests/fixtures/literature/esearch.xml`:

```xml
<?xml version="1.0"?>
<eSearchResult><Count>3</Count><RetMax>0</RetMax><RetStart>0</RetStart>
<QueryKey>1</QueryKey><WebEnv>MCID_synthetic_webenv</WebEnv><IdList></IdList></eSearchResult>
```

`tests/fixtures/literature/efetch_batch.xml`: copy the first two `<PubmedArticle>` records from `tests/fixtures/literature/corpus.xml` into a `<PubmedArticleSet>` (synthetic PMIDs 40000001 and 40000002; they are already invented).

- [ ] **Step 2: Failing tests**

Create `tests/test_literature_fetch.py`:

```python
"""The corpus's only network code. Nothing here opens a socket: `get` is the seam."""
from __future__ import annotations

from pathlib import Path

import pytest

from health_agent.literature import packs as pack_format
from health_agent.literature.fetch import client, eutils
from health_agent.literature.fetch import packs as fetch_packs

FIX = Path(__file__).parent / "fixtures" / "literature"


def test_client_refuses_hosts_outside_the_allow_list(monkeypatch):
    calls = []
    monkeypatch.setattr(client, "_open", lambda url, timeout: calls.append(url) or b"")
    with pytest.raises(client.FetchError):
        client.get("https://example.com/anything")
    assert calls == []


def test_client_user_agent_names_the_tool():
    from health_agent import __version__
    assert "health-agent" in client.USER_AGENT and __version__ in client.USER_AGENT


def test_esearch_parses_count_and_history_handle():
    handle = eutils.search("x", get=lambda url, **kw: (FIX / "esearch.xml").read_bytes())
    assert handle.count == 3
    assert handle.webenv == "MCID_synthetic_webenv"
    assert handle.query_key == "1"


def test_efetch_pages_in_batches_with_a_polite_delay():
    seen_urls, slept = [], []

    def get(url, **kw):
        seen_urls.append(url)
        return (FIX / "efetch_batch.xml").read_bytes()

    handle = eutils.SearchHandle(count=1200, webenv="W", query_key="1")
    articles = eutils.fetch_all(handle, max_articles=1000, get=get,
                                sleep=slept.append, batch=500)
    assert len(seen_urls) == 2                     # 1000 capped, 500 per batch
    assert "retstart=500" in seen_urls[1]
    assert all("WebEnv=W" in u and "query_key=1" in u for u in seen_urls)
    assert len(slept) == 1                         # between batches, not after the last
    assert len(articles) == 4                      # 2 per fixture batch, deduped by PMID? no: 2 per batch, both batches same fixture -> deduped to 2
```

Decide and pin the dedupe rule in the last assertion: `fetch_all` returns unique PMIDs in first-seen order, so with the same fixture returned twice the result is 2 articles. Write `assert [a.pmid for a in articles] == ["40000001", "40000002"]` and delete the trailing comment.

```python
def test_efetch_reports_a_non_200_as_a_typed_error():
    def get(url, **kw):
        raise client.FetchError("eutils.ncbi.nlm.nih.gov", 503, "busy")
    handle = eutils.SearchHandle(count=10, webenv="W", query_key="1")
    with pytest.raises(client.FetchError) as excinfo:
        eutils.fetch_all(handle, max_articles=10, get=get, sleep=lambda s: None)
    assert excinfo.value.host == "eutils.ncbi.nlm.nih.gov"


def test_release_url_points_at_the_repo_release_asset():
    url = fetch_packs.release_url("sleep", "2026.09")
    assert url.startswith("https://github.com/")
    assert url.endswith(f"/releases/download/{fetch_packs.PACK_RELEASE_TAG}/sleep-2026.09.jsonl.gz")


def test_download_verifies_the_sidecar_digest_before_returning(tmp_path):
    pack_path = tmp_path / "src" / "sleep-1.jsonl.gz"
    pack_format.write_pack(pack_path, pack_format.CATALOG["sleep"], "1", [],
                           license="L")
    good = pack_format.sha256_file(pack_path)
    served = {fetch_packs.release_url("sleep", "1"): pack_path.read_bytes(),
              fetch_packs.release_url("sleep", "1") + ".sha256":
                  f"{good}  sleep-1.jsonl.gz\n".encode()}
    out = fetch_packs.download("sleep", "1", tmp_path / "dl", get=lambda u, **kw: served[u])
    assert out.exists() and pack_format.sha256_file(out) == good

    served[fetch_packs.release_url("sleep", "1") + ".sha256"] = b"deadbeef  x\n"
    with pytest.raises(pack_format.PackError):
        fetch_packs.download("sleep", "1", tmp_path / "dl2", get=lambda u, **kw: served[u])
    assert not (tmp_path / "dl2" / "sleep-1.jsonl.gz").exists()
```

- [ ] **Step 3: Update the two guard tests**

In `tests/test_no_network.py::test_the_query_path_cannot_reach_a_fetcher`, replace the text-scan loop with:

```python
    # Exactly one file under health_agent.literature may import a transport:
    # fetch/client.py. eutils.py and fetch/packs.py go through it, so the
    # allow-list of hosts and the user agent live in one place. Everything
    # else under literature/ is read-only over the local corpus.
    literature_dir = Path(offline_check.__file__).parent / "literature"
    allowed = literature_dir / "fetch" / "client.py"
    for path in literature_dir.rglob("*.py"):
        text = path.read_text()
        for forbidden in ("import urllib", "from urllib", "import http",
                          "import socket", "import requests", "import httpx"):
            if path == allowed:
                continue
            assert forbidden not in text, (
                f"{path} imports {forbidden!r}; only fetch/client.py may "
                f"reach the network")
    assert "import urllib" in allowed.read_text() or "from urllib" in allowed.read_text()
```

In `tests/test_embeddings_and_search.py`, rename the test to `test_the_network_surface_is_exactly_three_modules`, extend its docstring with a third line `literature/fetch/client.py   packs — NCBI and GitHub, only from two commands, after consent`, and change the expected set to include `"literature/fetch/client.py"` (check how the test builds relative paths; match its convention).

Run: `pytest tests/test_literature_fetch.py tests/test_no_network.py tests/test_embeddings_and_search.py -q` — Expected: fetch tests fail on import; the two guard tests fail because `client.py` does not exist yet.

- [ ] **Step 4: Write the package**

`health_agent/literature/fetch/__init__.py`:

```python
"""The corpus's network code, and nothing else's.

Two commands import this package: `literature build-pack` (NCBI E-utilities,
maintainer) and `literature install` (a GitHub Release asset, user). Nothing
on the ask path does, and `tests/test_no_network.py` proves it statically.
`client.py` is the only file here, or anywhere under `literature/`, that
imports a transport.
"""
```

`client.py`:

```python
"""One HTTP GET, two allowed hosts.

Every byte the corpus ever fetches goes through `get`. Keeping the transport
in one function is what lets THREAT_MODEL.md name the network surface as
exactly three modules and lets a test enforce it; the allow-list is what
makes "a pack request reveals a category, not a person" checkable, since a
URL to anywhere else is refused before a socket opens.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from urllib.parse import urlparse

from .. import __version__ as _pkg_version  # noqa: F401  (see below)

try:
    from ... import __version__
except ImportError:  # pragma: no cover
    __version__ = "0"

USER_AGENT = f"health-agent/{__version__} (+https://github.com/hydawo/local-health-agent)"

# NCBI for pack builds; GitHub for pack downloads. Release assets redirect to
# objects.githubusercontent.com, which is the only redirect followed.
ALLOWED_HOSTS: frozenset[str] = frozenset({
    "eutils.ncbi.nlm.nih.gov", "github.com", "objects.githubusercontent.com",
})


class FetchError(RuntimeError):
    def __init__(self, host: str, status: int | None, message: str) -> None:
        super().__init__(f"{host}: {message}" + (f" (HTTP {status})" if status else ""))
        self.host, self.status = host, status


class _AllowListRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).hostname not in ALLOWED_HOSTS:
            raise FetchError(urlparse(newurl).hostname or "?", code,
                             "redirect to a host outside the allow list")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: float) -> bytes:
    opener = urllib.request.build_opener(_AllowListRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener.open(request, timeout=timeout) as response:
        return response.read()


def get(url: str, *, timeout: float = 60.0) -> bytes:
    host = urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS or not url.startswith("https://"):
        raise FetchError(host or "?", None, "host is not on the allow list")
    try:
        return _open(url, timeout)
    except urllib.error.HTTPError as exc:
        raise FetchError(host, exc.code, exc.reason) from exc
    except urllib.error.URLError as exc:
        raise FetchError(host, None, str(exc.reason)) from exc
```

Fix the version import to whichever works (`from health_agent import __version__` is the safe form; drop the two speculative lines above it).

`eutils.py`:

```python
"""NCBI E-utilities for pack builds: esearch with the history server, then
efetch in pages. Called only by `literature build-pack`.

The polite delay between pages is NCBI's stated limit for callers without an
API key (three requests a second). With `NCBI_API_KEY` in the environment
the key is sent and the limit is higher, but the delay is kept: a pack build
is a one-off maintainer job, not a race.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from ..medline import ParsedArticle, parse_articles
from . import client

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
POLITE_DELAY_SECONDS = 0.4


@dataclass(frozen=True)
class SearchHandle:
    count: int
    webenv: str
    query_key: str


def _params(**kw) -> dict:
    params = {"db": "pubmed", "tool": "health-agent", **kw}
    key = os.environ.get("NCBI_API_KEY")
    if key:
        params["api_key"] = key
    return params


def search(term: str, *, get=client.get) -> SearchHandle:
    url = BASE + "esearch.fcgi?" + urlencode(_params(term=term, retmax=0, usehistory="y"))
    body = get(url).decode("utf-8", errors="replace")
    count = re.search(r"<Count>(\d+)</Count>", body)
    webenv = re.search(r"<WebEnv>([^<]+)</WebEnv>", body)
    key = re.search(r"<QueryKey>(\d+)</QueryKey>", body)
    if not (count and webenv and key):
        raise client.FetchError("eutils.ncbi.nlm.nih.gov", None,
                                "esearch response had no Count/WebEnv/QueryKey")
    return SearchHandle(int(count.group(1)), webenv.group(1), key.group(1))


def fetch_all(handle: SearchHandle, *, max_articles: int | None,
              get=client.get, sleep=time.sleep, batch: int = 500,
              progress=None) -> list[ParsedArticle]:
    """Page through the history handle. Unique PMIDs in first-seen order."""
    total = handle.count if max_articles is None else min(handle.count, max_articles)
    seen: set[str] = set()
    out: list[ParsedArticle] = []
    start = 0
    while start < total:
        size = min(batch, total - start)
        url = BASE + "efetch.fcgi?" + urlencode(_params(
            query_key=handle.query_key, WebEnv=handle.webenv,
            retstart=start, retmax=size, retmode="xml"))
        for article in parse_articles(get(url)):
            if article.pmid not in seen:
                seen.add(article.pmid)
                out.append(article)
        start += size
        if progress is not None:
            progress(min(start, total), total)
        if start < total:
            sleep(POLITE_DELAY_SECONDS)
    return out
```

`fetch/packs.py`:

```python
"""Download a pack from the project's GitHub Release and verify it.

The pack name and the tool's version are what the request reveals, plus the
caller's IP address to GitHub. Nothing from the data folder is involved.
"""

from __future__ import annotations

from pathlib import Path

from .. import packs as pack_format
from . import client

REPO = "hydawo/local-health-agent"
PACK_RELEASE_TAG = "packs-v1"


def release_url(slug: str, version: str) -> str:
    return (f"https://github.com/{REPO}/releases/download/{PACK_RELEASE_TAG}/"
            f"{pack_format.asset_name(slug, version)}")


def download(slug: str, version: str, dest_dir: Path, *, get=client.get) -> Path:
    """Fetch the asset and its .sha256; refuse to keep a file that does not
    match. The digest is checked before the file gets its final name so a
    bad download never looks like a good one on disk."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = release_url(slug, version)
    expected = get(url + ".sha256").decode("utf-8", errors="replace").split()[0]
    partial = dest_dir / (pack_format.asset_name(slug, version) + ".part")
    partial.write_bytes(get(url))
    actual = pack_format.sha256_file(partial)
    if actual != expected:
        partial.unlink()
        raise pack_format.PackError(
            f"{slug}-{version}: downloaded file digest {actual[:12]}... does not "
            f"match the published {expected[:12]}...; nothing was installed")
    final = dest_dir / pack_format.asset_name(slug, version)
    partial.replace(final)
    return final
```

- [ ] **Step 5: Run everything, commit**

Run: `pytest tests/test_literature_fetch.py tests/test_no_network.py tests/test_embeddings_and_search.py -q`, then `pytest -q`. Also run `health-agent offline-check` and paste PASS in the report.

```bash
git add health_agent/literature/fetch/ tests/test_literature_fetch.py tests/fixtures/literature/esearch.xml tests/fixtures/literature/efetch_batch.xml tests/test_no_network.py tests/test_embeddings_and_search.py
git commit -m "The corpus's network code: one client, two hosts, two callers

fetch/client.py is the only file under literature/ that imports a
transport, and the tests now say so: the network surface is exactly
three modules, and the ask path still cannot import fetch. Two hosts are
allowed and a redirect anywhere else is refused before a socket opens,
which is what makes 'a pack request reveals a category, not a person'
checkable. eutils.py pages a history handle politely; fetch/packs.py
downloads a release asset and refuses to keep it unless the published
digest matches.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Consent, and the three commands

**Files:**
- Modify: `health_agent/consent.py` (a `Notice` for the literature side; existing functions take a notice, default cloud)
- Modify: `health_agent/cli.py` (`literature packs|install|build-pack`, `literature-consent`; a `_confirm_literature` mirroring `_confirm_cloud`)
- Test: `tests/test_cli.py`, `tests/test_cloud_tier.py` (existing consent tests must still pass)

**Interfaces:**
- Consumes: `packs.CATALOG`, `packs.read_pack`, `fetch.packs.download`, `eutils.search/fetch_all`, `corpus.build`, `lit_embed.embed_corpus`.
- Produces: `consent.LITERATURE`, `consent.CLOUD` (`Notice` instances); `consent.needs_prompt(index_dir, notice=CLOUD)`, `record(index_dir, model, notice=CLOUD)`, `revoke(index_dir, notice=CLOUD)`, `load(...)`, `consent_path(...)` all gaining the keyword; CLI subcommands.

- [ ] **Step 1: Consent refactor with tests**

Append to `tests/test_cloud_tier.py` (or wherever `consent` is tested; `grep -n "consent" tests/*.py`):

```python
def test_literature_consent_is_a_separate_record_from_cloud(tmp_path):
    from health_agent import consent
    assert consent.needs_prompt(tmp_path, notice=consent.LITERATURE)
    consent.record(tmp_path, "", notice=consent.LITERATURE)
    assert not consent.needs_prompt(tmp_path, notice=consent.LITERATURE)
    assert consent.needs_prompt(tmp_path)              # cloud still unconsented
    assert consent.consent_path(tmp_path, notice=consent.LITERATURE).name == "literature_consent.json"
    assert consent.revoke(tmp_path, notice=consent.LITERATURE)
    assert consent.needs_prompt(tmp_path, notice=consent.LITERATURE)


def test_literature_notice_says_what_leaves_and_when():
    from health_agent import consent
    text = consent.LITERATURE.text.lower()
    for phrase in ("github.com", "eutils.ncbi.nlm.nih.gov", "pack", "ip address",
                   "never during", "revoke"):
        assert phrase in text
```

In `consent.py`, add a `Notice` dataclass (`name`, `version`, `filename`, `text`, `summary`) and two instances: `CLOUD` wrapping the existing `NOTICE`/`NOTICE_VERSION`/`CONSENT_FILENAME` (keep those module constants as aliases so nothing breaks), and `LITERATURE` with `filename="literature_consent.json"`, `version=1`, and this text (no em dashes):

```
────────────────────────────────────────────────────────────────────────
  LITERATURE PACKS: this command connects to the internet
────────────────────────────────────────────────────────────────────────

Asking questions never touches the network, and `offline-check` proves
it. Two literature commands do, and only when you run them:

  health-agent literature install <pack>
    Downloads one pack file from github.com (the project's releases).
    What the request reveals: which pack you chose, your IP address, and
    this tool's version. Nothing from your data folder. Nothing about
    your questions. Packs are deliberately broad (cardiovascular, sleep)
    so that the choice says as little about you as possible.

  health-agent literature build-pack <pack>
    A maintainer command. Sends the pack's search terms (a fixed list of
    medical subject headings, the same for everyone) to
    eutils.ncbi.nlm.nih.gov, with your IP address and the tool's version.

Neither command runs on its own, checks for updates, or reports usage.
Never during ask, ingest, or search.

Revoke with `health-agent literature-consent --revoke`.
────────────────────────────────────────────────────────────────────────
```

Every function gains `notice: Notice = CLOUD`; `ConsentRecord.is_current` compares against the notice's version (store the notice name in the record). Run the existing consent tests to confirm the cloud path is unchanged.

- [ ] **Step 2: CLI tests**

Append to `tests/test_cli.py`:

```python
def test_literature_packs_lists_the_catalog_offline(tmp_path, capsys, monkeypatch):
    from health_agent.cli import main
    from health_agent.literature.fetch import client

    monkeypatch.setattr(client, "_open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    code = main(["--index", str(tmp_path / ".index" / "health.db"), "literature", "packs"])
    out = capsys.readouterr().out
    assert code == 0
    for slug in ("sample", "cardiovascular", "metabolic", "sleep", "exercise"):
        assert slug in out
    assert "not installed" in out


def test_literature_install_stops_before_any_fetch_without_consent(tmp_path, capsys, monkeypatch):
    from health_agent.cli import main
    from health_agent.literature.fetch import client, packs as fetch_packs

    calls = []
    monkeypatch.setattr(fetch_packs, "download", lambda *a, **k: calls.append(a) or None)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))   # not a tty, no --yes
    code = main(["--index", str(tmp_path / ".index" / "health.db"),
                 "literature", "install", "sleep"])
    err = capsys.readouterr().err
    assert code != 0
    assert calls == []
    assert "LITERATURE PACKS" in err


def test_literature_install_from_a_local_pack_builds_the_corpus(tmp_path, capsys):
    from health_agent.cli import main
    from health_agent.literature import medline, packs, schema as lit_schema
    from health_agent import config as config_mod

    articles = medline.parse_articles(
        (Path(__file__).parent / "fixtures" / "literature" / "corpus.xml").read_bytes())
    pack_path = tmp_path / "sleep-1.jsonl.gz"
    packs.write_pack(pack_path, packs.CATALOG["sleep"], "1", articles, license=packs.PACK_LICENSE)
    index = tmp_path / ".index" / "health.db"

    code = main(["--index", str(index), "literature", "install", "sleep",
                 "--from", str(pack_path), "--yes", "--no-embed"])
    out = capsys.readouterr().out
    assert code == 0
    assert "sleep@1" in out or "sleep" in out

    cfg = config_mod.resolve(index_path=str(index))
    conn = lit_schema.connect(cfg.literature_path)
    row = conn.execute("SELECT slug, version, article_count FROM pack").fetchone()
    assert (row["slug"], row["version"]) == ("sleep", "1")
    assert row["article_count"] == len([a for a in articles if a.abstract.strip()])
    # Tiers come from publication_types at install, not from the pack.
    tiers = {r["pmid"]: r["evidence_tier"] for r in conn.execute("SELECT pmid, evidence_tier FROM article")}
    assert tiers["40000001"] == "meta_analysis" and tiers["40000010"] == "protocol"
    assert conn.execute("SELECT DISTINCT license FROM article").fetchone()["license"] == packs.PACK_LICENSE
    conn.close()

    # Same version again is a no-op that says so.
    code = main(["--index", str(index), "literature", "install", "sleep",
                 "--from", str(pack_path), "--yes", "--no-embed"])
    assert code == 0
    assert "already installed" in capsys.readouterr().out


def test_literature_install_refuses_an_unknown_pack(tmp_path, capsys):
    from health_agent.cli import main
    code = main(["--index", str(tmp_path / ".index" / "health.db"),
                 "literature", "install", "type-2-diabetes", "--yes"])
    assert code == 2
    assert "not a known pack" in capsys.readouterr().err


def test_build_pack_writes_a_pack_file_without_touching_the_index(tmp_path, capsys, monkeypatch):
    from health_agent.cli import main
    from health_agent.literature import packs
    from health_agent.literature.fetch import eutils

    fix = Path(__file__).parent / "fixtures" / "literature"
    monkeypatch.setattr(eutils, "search", lambda term, **k: eutils.SearchHandle(2, "W", "1"))
    monkeypatch.setattr(eutils, "fetch_all", lambda handle, **k: __import__(
        "health_agent.literature.medline", fromlist=["parse_articles"]
    ).parse_articles((fix / "efetch_batch.xml").read_bytes()))
    index = tmp_path / ".index" / "health.db"
    code = main(["--index", str(index), "literature", "build-pack", "sleep",
                 "--out", str(tmp_path / "out"), "--version", "2026.09", "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    written = tmp_path / "out" / "sleep-2026.09.jsonl.gz"
    assert written.exists() and (tmp_path / "out" / "sleep-2026.09.jsonl.gz.sha256").exists()
    manifest, articles = packs.read_pack(written)
    assert manifest.article_count == 2 and len(articles) == 2
    assert not (tmp_path / ".index" / "literature.db").exists()
    assert "2 articles" in out
```

Add `import io` to `test_cli.py` if absent.

- [ ] **Step 3: Implement the CLI**

In `cli.py`, add beside `_confirm_cloud`:

```python
def _confirm_literature(cfg: config.Config, *, assume_yes: bool) -> bool:
    """One-time consent for the two literature commands that use the network.

    Same shape as the cloud tier's: shown in full once, recorded per data
    folder, re-shown when the notice's substance changes.
    """
    if not consent.needs_prompt(cfg.index_dir, notice=consent.LITERATURE):
        return True
    print(consent.LITERATURE.text, file=sys.stderr)
    if assume_yes:
        consent.record(cfg.index_dir, "", notice=consent.LITERATURE)
        print("Consent recorded via --yes.\n", file=sys.stderr)
        return True
    if not sys.stdin.isatty():
        print("\nThis command needs a one-time confirmation and this is not an "
              "interactive terminal. Re-run in a terminal, or pass --yes if you "
              "have read the notice above.", file=sys.stderr)
        return False
    try:
        answer = input("\nConnect to the internet for literature packs? "
                       "Type 'yes' to agree: ").strip().lower()
    except EOFError:
        answer = ""
    if answer != "yes":
        print("Cancelled. Nothing was fetched.", file=sys.stderr)
        return False
    consent.record(cfg.index_dir, "", notice=consent.LITERATURE)
    print(f"Recorded in {consent.consent_path(cfg.index_dir, notice=consent.LITERATURE)}\n"
          f"Revoke with `health-agent literature-consent --revoke`.\n", file=sys.stderr)
    return True
```

Commands (place near `cmd_literature_build`):

```python
def cmd_literature_packs(args: argparse.Namespace, cfg: config.Config) -> int:
    from .literature import packs, schema as lit_schema

    installed: dict[str, tuple[str, int]] = {}
    if cfg.literature_path.exists():
        try:
            conn = lit_schema.connect(cfg.literature_path)
        except lit_schema.CorpusSchemaVersionMismatch as exc:
            print(str(exc), file=sys.stderr)
        else:
            installed = {r["slug"]: (r["version"], r["article_count"]) for r in
                         conn.execute("SELECT slug, version, article_count FROM pack")}
            conn.close()
    for spec in packs.CATALOG.values():
        state = (f"installed {installed[spec.slug][0]}, {installed[spec.slug][1]} articles"
                 if spec.slug in installed else "not installed")
        print(f"{spec.slug:<16}{state}")
        print(f"{'':<16}{spec.description}")
    for slug in installed:
        if slug not in packs.CATALOG:
            print(f"{slug:<16}installed {installed[slug][0]}, {installed[slug][1]} articles (built locally)")
    print("\nInstall with `health-agent literature install <pack>`; the first run "
          "shows what the download reveals and asks once.")
    return 0


def cmd_literature_install(args: argparse.Namespace, cfg: config.Config) -> int:
    from .literature import corpus as lit_corpus
    from .literature import embed as lit_embed
    from .literature import packs, schema as lit_schema
    from .literature import tiers as lit_tiers

    if args.slug not in packs.CATALOG and not args.source:
        print(f"{args.slug!r} is not a known pack. `health-agent literature packs` "
              f"lists them.", file=sys.stderr)
        return 2
    if not _confirm_literature(cfg, assume_yes=args.yes):
        return 1

    version = args.version or packs.LATEST_VERSION
    if args.source and not args.source.startswith("https://"):
        pack_path = Path(args.source).expanduser()
        if not pack_path.is_file():
            print(f"No such file: {pack_path}", file=sys.stderr)
            return 2
    else:
        from .literature.fetch import packs as fetch_packs
        try:
            if args.source:
                pack_path = fetch_packs.download_url(args.source, cfg.index_dir / "packs")
            else:
                print(f"Downloading {args.slug}-{version} from github.com ...", flush=True)
                pack_path = fetch_packs.download(args.slug, version, cfg.index_dir / "packs")
        except Exception as exc:  # noqa: BLE001 - reported, never a traceback
            print(f"Download failed: {exc}", file=sys.stderr)
            return 2

    try:
        manifest, articles = packs.read_pack(pack_path)
    except packs.PackError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    # The pack's stored tier is not trusted; the installed table is the
    # authority, and publication_types is verbatim for exactly this.
    for article in articles:
        article.evidence_tier, article.evidence_rank, article.tier_source = \
            lit_tiers.resolve(article.publication_types)

    try:
        conn = lit_schema.connect(cfg.literature_path, create=True)
    except lit_schema.CorpusSchemaVersionMismatch as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        lit_schema.initialize(conn)
        row = conn.execute("SELECT version FROM pack WHERE slug = ?",
                           (manifest.slug,)).fetchone()
        if row and row["version"] == manifest.version and not args.force:
            print(f"{manifest.slug}@{manifest.version} is already installed; "
                  f"pass --force to reinstall.")
            return 0
        stats = lit_corpus.build(conn, articles, slug=manifest.slug,
                                 version=manifest.version, license=manifest.license)
        print(f"{manifest.slug}@{manifest.version}: {stats.articles} articles, "
              f"{stats.chunks} chunks"
              f"{f', {stats.skipped} skipped (no abstract)' if stats.skipped else ''}")
        if not args.no_embed:
            embedder = embeddings.get_embedder(args.embed_backend)
            store = vector_store.VectorStore(cfg.literature_vector_path,
                                             table_name=lit_embed.TABLE_NAME)
            try:
                done = lit_embed.embed_corpus(conn, store, embedder)
                print(f"embedded {done} chunks with {embedder.name}")
            except (embeddings.EmbeddingUnavailable, embeddings.RemoteHostRefused):
                print("Ollama unavailable; corpus search will use keyword matching "
                      "until you run this again.", file=sys.stderr)
        return 0
    finally:
        conn.close()


def cmd_literature_build_pack(args: argparse.Namespace, cfg: config.Config) -> int:
    """Maintainer: fetch a pack's articles from NCBI and write the pack file.
    Never touches the local index; publishing to a release is a manual step."""
    from .literature import packs
    from .literature.fetch import eutils

    spec = packs.CATALOG.get(args.slug)
    if spec is None:
        print(f"{args.slug!r} is not a known pack.", file=sys.stderr)
        return 2
    if not _confirm_literature(cfg, assume_yes=args.yes):
        return 1
    cap = args.max if args.max is not None else spec.max_articles
    try:
        handle = eutils.search(spec.search_term())
        print(f"{handle.count} matching articles; fetching "
              f"{min(handle.count, cap) if cap else handle.count}", flush=True)
        articles = eutils.fetch_all(
            handle, max_articles=cap,
            progress=lambda done, total: print(f"  {done}/{total}", flush=True))
    except Exception as exc:  # noqa: BLE001
        print(f"Fetch failed: {exc}", file=sys.stderr)
        return 2
    out_dir = Path(args.out).expanduser()
    path = out_dir / packs.asset_name(spec.slug, args.version)
    manifest = packs.write_pack(path, spec, args.version, articles, license=packs.PACK_LICENSE)
    digest = packs.sha256_file(path)
    (out_dir / (path.name + ".sha256")).write_text(f"{digest}  {path.name}\n")
    print(f"wrote {path} ({manifest.article_count} articles, "
          f"{path.stat().st_size / 1e6:.1f} MB) and {path.name}.sha256")
    return 0


def cmd_literature_consent(args: argparse.Namespace, cfg: config.Config) -> int:
    if args.show_notice:
        print(consent.LITERATURE.text)
        return 0
    if args.revoke:
        if consent.revoke(cfg.index_dir, notice=consent.LITERATURE):
            print("Literature network consent withdrawn.")
            return 0
        print("No literature consent was recorded; nothing to revoke.")
        return 1
    rec = consent.load(cfg.index_dir, notice=consent.LITERATURE)
    print("Literature packs: " + ("consented" if rec and rec.is_current() else "not consented"))
    return 0
```

`packs.LATEST_VERSION` is a constant in `literature/packs.py` (add it, e.g. `"2026.09"`), the version the release tag carries. `fetch_packs.download_url(url, dest_dir)` downloads an explicit URL plus its `.sha256` sibling through `client.get` with the same verification as `download`; add it to `fetch/packs.py` with a test in `test_literature_fetch.py`.

Argparse: under the existing `literature` subparser add `packs`, `install <slug> [--version] [--from SOURCE] [--yes] [--force] [--no-embed] [--embed-backend]`, `build-pack <slug> --out DIR [--version, default packs.LATEST_VERSION] [--max N] [--yes]`; and a top-level `literature-consent [--revoke] [--show-notice]` beside `cloud-consent`. Also add `--yes` to `install` and `build-pack` only (not to `build`).

Also `cmd_doctor`: add a line `literature packs: <n installed>` reading the `pack` table when the corpus exists; skip if it does not.

- [ ] **Step 4: Run everything, commit**

Run: `pytest tests/test_cli.py tests/test_cloud_tier.py -q`, then `pytest -q`, then `health-agent offline-check` (PASS).

```bash
git add health_agent/consent.py health_agent/cli.py health_agent/literature/packs.py health_agent/literature/fetch/packs.py tests/test_cli.py tests/test_cloud_tier.py tests/test_literature_fetch.py
git commit -m "literature packs, install, build-pack, and a consent notice for the network

Install downloads one release asset after a one-time notice that says
exactly what the request reveals (the pack name, an IP address, the tool's
version) and when the tool connects (only these two commands, never during
ask). Tiers are re-derived from publication_types at install; the pack's
stored tier is not trusted. build-pack is the maintainer's E-utilities
caller and never touches the local index. Consent reuses the cloud tier's
mechanism with a second notice and a second file.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Documentation

**Files:** `README.md`, `ROADMAP.md`, `LICENSES.md`, `CONTRIBUTING.md`, `THREAT_MODEL.md`.

Run the humanizer skill (`/Users/hydawo/Downloads/Claude Code/brainiac/skills/humanizer/SKILL.md`) over every paragraph. No em dashes in new prose. `grep -c "—"` on each file must not rise.

- [ ] **THREAT_MODEL.md**: Claim 3's table gains a row `health_agent/literature/fetch/client.py | packs | NCBI E-utilities (build-pack, maintainer) and GitHub Releases (install), after consent`; its heading becomes "exactly three modules"; the test name in the prose updates. Add **Claim 6: literature packs** after Claim 5, with: what is sent by each command (mirror the notice), when (only those commands; never during ask; `offline-check` unchanged), the two hosts and the redirect rule, that no update checks exist, and Verify: `grep -rn "import urllib\|from urllib" health_agent/literature/` (one file), `pytest tests/test_no_network.py::test_the_query_path_cannot_reach_a_fetcher tests/test_embeddings_and_search.py::test_the_network_surface_is_exactly_three_modules`.
- [ ] **README.md**: "Medical literature corpus" section opens with `health-agent literature packs` / `health-agent literature install sample` and the five slugs, one sentence each; a sentence that the first install shows a notice and what a download reveals; `literature build --from` stays as "use your own export". In the known-hole section, replace the bullet "Slice 1 has no network fetch" with one sentence pointing at packs and Claim 6. In "What works today", add `health-agent literature install sample` to the setup block.
- [ ] **ROADMAP.md**: #1a: mark shipped in the packs shape, link the spec, keep the ClinicalTrials.gov line as still open. #2a: the remaining question is answered by packs; say so and link. #6: pack versions are the lineage unit now.
- [ ] **LICENSES.md**: a "Packs" section: abstracts and MEDLINE metadata only, NLM terms, `article.license` per row set to `PACK_LICENSE`, no full text, no CC claim; the `sample` pack is real records (unlike the fixture), which is why it is a release asset and not in the repo.
- [ ] **CONTRIBUTING.md**: "Building and publishing a pack": `health-agent literature build-pack <slug> --out dist/packs --version YYYY.MM`, attach both files to the `packs-v1` release with `gh release upload`, bump `packs.LATEST_VERSION` in the same PR; and the rule that packs stay at body-system level.
- [ ] Commit: `Docs: literature packs, Claim 6, and the three-module network surface`.

---

### Task 6: Whole-branch review, PR, and the first real pack

- [ ] Whole-branch review on the most capable model. Named risks: the v4 migration against `--rebuild`, `reset`, `offline-check`, `data_inventory`, and the eval runner; orphaned LanceDB rows when a re-linked article's chunks are rebuilt (is `store.search`'s tolerance enough, or should install drop and re-embed the table?); `client.get`'s allow-list against `http://` URLs and userinfo tricks (`https://github.com@evil.com/`); whether `--from <url>` can reach a non-allow-listed host (it must not); the consent record's `is_current` when both notices share `ConsentRecord`; every prose claim in Claim 6 against the code; no test opens a socket (`grep -rn "client._open\|monkeypatch" tests/test_literature_fetch.py`).
- [ ] Fix wave, scoped re-review, push, PR (humanized body, `🤖 Generated with [Claude Code](https://claude.com/claude-code)`), CI, merge on green.
- [ ] After merge, with the controller's go-ahead (it is a network call from this machine): `health-agent literature build-pack sample --out <scratch>/packs --version 2026.09 --yes`, then `gh release create packs-v1 --title "Literature packs v1" --notes "..." <files>`, then `health-agent --index <scratch index> literature install sample --yes` and eval run 5 over 27 questions, recorded in `eval_results.md` as the first reproducible run. The four topical packs are built the same way but are larger fetches (tens of thousands of records at 500 per request); run them one at a time and report sizes before uploading.
