# Literature Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `health-agent literature refresh` fetches what PubMed has added to each installed pack's query since the last refresh, folds it into the corpus, marks retractions, and records what changed.

**Architecture:** Schema v5 adds `pack.refreshed_at` and a `refresh_log` table, migrated in place from v4. `corpus.add()` shares `build()`'s upsert loop but never unlinks. A new `literature/fetch/refresh.py` orchestrates two E-utilities queries per pack (additions with an Entrez-date window; retractions without) through the existing sliced, retrying `fetch_term`. The CLI command sits beside `install`, under consent notice version 2.

**Tech Stack:** Python 3.11, sqlite3, existing `literature.fetch.eutils`/`client`, pytest with fake `get` callables.

**Spec:** `docs/superpowers/specs/2026-09-20-literature-refresh-design.md`

## Global Constraints

- Only `health_agent/literature/fetch/client.py` may import a transport; `tests/test_no_network.py` scans every other file under `literature/` for `import urllib`, `from urllib`, `import http`, `import socket`, `import requests`, `import httpx`. `refresh.py` goes under `fetch/` and calls `eutils`/`client` only.
- `cmd_literature_refresh` imports `.literature.fetch` inside the function body, never at module level (as `cmd_literature_install` does), so the ask/ingest import graph stays fetch-free.
- No em dashes in any new prose (notice text, docs, docstrings, comments). Never the word "doctor".
- `LITERATURE_SCHEMA_VERSION = 5`. A v4 corpus migrates in place; a version below 4 still raises `CorpusSchemaVersionMismatch`.
- `corpus.add()` never unlinks or deletes an article; it never changes `pack.version` or `pack.built_at`.
- `consent.LITERATURE.version == 2`; the notice text names `refresh` and says what it sends, to whom, and that it repeats on every refresh.
- Refresh window: start = `--since`, else date of `refreshed_at`, else date of `built_at`, minus one day; end = today. Additions use `datetype=edat`. Retractions query: `(<spec.query>) AND "Retracted Publication"[Publication Type]`, no window, no evidence filter.
- `sample` is never refreshed; naming it prints the snapshot line and exits 0.

---

### Task 1: Schema v5 with in-place migration; `corpus.add` and `mark_retracted`

**Files:**
- Modify: `health_agent/literature/schema.py`
- Modify: `health_agent/literature/corpus.py`
- Test: `tests/test_literature_schema.py`, `tests/test_literature_corpus.py`

**Interfaces:**
- Consumes: `schema.connect`, `schema.initialize`, `schema.read_version`, `corpus.build` (lines 49-160), `corpus._chunks`, `corpus._refresh_count`, `schema.rebuild_chunk_fts`.
- Produces:
  ```python
  # schema.py
  LITERATURE_SCHEMA_VERSION = 5
  def migrate(conn) -> None            # 4 -> 5 in place; no-op at 5; raises below 4
  # corpus.py
  @dataclass
  class AddStats:                      # returned by add()
      articles: int = 0                # upserted (new + existing)
      added: int = 0                   # PMIDs not in the corpus before
      chunks: int = 0
      skipped: int = 0
  def add(conn, articles, *, slug, license, window_from: str, window_to: str,
          matched: int, retracted: int = 0) -> AddStats
  def mark_retracted(conn, notes: dict[str, str | None]) -> int   # rows flipped
  ```
  `add()` writes one `refresh_log` row and sets `pack.refreshed_at`. `coverage()["pack_rows"]` becomes a list of dicts `{"slug","version","built_at","refreshed_at","article_count"}` alongside the existing `"packs"` strings.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_literature_schema.py`:

```python
def test_schema_version_is_5():
    assert schema.LITERATURE_SCHEMA_VERSION == 5


def _v4_corpus(path):
    """A corpus file as v4 wrote it: current tables minus the v5 additions,
    stamped 4, with one pack and one article."""
    import sqlite3
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    sql = schema.SCHEMA_SQL.replace("refreshed_at           TEXT,\n", "")
    start = sql.index("CREATE TABLE IF NOT EXISTS refresh_log")
    end = sql.index(");", start) + 2
    sql = sql[:start] + sql[end:]
    conn.executescript(sql)
    conn.execute("INSERT INTO corpus_meta(key, value) VALUES('schema_version', '4')")
    conn.execute("INSERT INTO pack(slug, title, version, built_at, article_count) "
                 "VALUES('sleep', 'sleep', '2026.09', '2026-09-20T20:00:00-04:00', 1)")
    conn.execute("INSERT INTO article(pmid, title, publication_types, evidence_tier, "
                 "tier_source, license) VALUES('1', 'T', '[]', 'unknown', 'unmapped', 'L')")
    conn.commit()
    conn.close()


def test_connect_migrates_a_v4_corpus_in_place(tmp_path):
    """Refusing a v4 file was fine when a corpus was a 1.6 MB download; it
    is not now that cardiovascular is 62 MB."""
    path = tmp_path / "literature.db"
    _v4_corpus(path)
    conn = schema.connect(path)
    assert schema.read_version(conn) == 5
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pack)")}
    assert "refreshed_at" in cols
    assert conn.execute("SELECT COUNT(*) FROM refresh_log").fetchone()[0] == 0
    row = conn.execute("SELECT slug, version, article_count, refreshed_at FROM pack").fetchone()
    assert (row["slug"], row["version"], row["article_count"], row["refreshed_at"]) == \
        ("sleep", "2026.09", 1, None)
    conn.close()
    # and it stays migrated
    conn = schema.connect(path)
    assert schema.read_version(conn) == 5
    conn.close()


def test_connect_still_refuses_a_corpus_below_v4(tmp_path):
    path = tmp_path / "literature.db"
    _v4_corpus(path)
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute("UPDATE corpus_meta SET value = '3' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(schema.CorpusSchemaVersionMismatch):
        schema.connect(path)
```

Replace the existing `test_schema_version_is_4` with the `_5` version above (delete the old one). Check the file's imports include `pytest`.

Append to `tests/test_literature_corpus.py`:

```python
def _fresh(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    return conn


def _art(pmid, title="T", abstract="Some abstract text.", **kw):
    return medline.ParsedArticle(pmid=pmid, title=title, abstract=abstract,
                                 publication_types=["Journal Article"], **kw)


def test_add_links_new_and_existing_articles_and_unlinks_nothing(tmp_path):
    conn = _fresh(tmp_path)
    corpus.build(conn, [_art("1"), _art("2")], slug="sleep", version="2026.09",
                 license="L")
    before = conn.execute("SELECT version, built_at FROM pack").fetchone()

    stats = corpus.add(conn, [_art("2", title="T2 revised"), _art("3")],
                       slug="sleep", license="L", window_from="2026-09-19",
                       window_to="2026-10-04", matched=2)

    assert (stats.articles, stats.added) == (2, 1)
    linked = {r["pmid"] for r in conn.execute(
        "SELECT a.pmid FROM article a JOIN article_pack ap ON ap.article_id = a.id")}
    assert linked == {"1", "2", "3"}          # 1 stayed linked
    assert conn.execute("SELECT title FROM article WHERE pmid = '2'").fetchone()[0] == "T2 revised"
    pack = conn.execute("SELECT version, built_at, refreshed_at, article_count FROM pack").fetchone()
    assert (pack["version"], pack["built_at"]) == (before["version"], before["built_at"])
    assert pack["refreshed_at"] is not None
    assert pack["article_count"] == 3
    log = conn.execute("SELECT * FROM refresh_log").fetchone()
    assert (log["window_from"], log["window_to"], log["matched"], log["added"], log["retracted"]) == \
        ("2026-09-19", "2026-10-04", 2, 1, 0)


def test_add_requires_an_installed_pack(tmp_path):
    conn = _fresh(tmp_path)
    with pytest.raises(corpus.PackNotInstalled):
        corpus.add(conn, [_art("1")], slug="sleep", license="L",
                   window_from="a", window_to="b", matched=1)


def test_mark_retracted_flips_only_rows_that_exist(tmp_path):
    conn = _fresh(tmp_path)
    corpus.build(conn, [_art("1"), _art("2")], slug="sleep", version="1", license="L")
    n = corpus.mark_retracted(conn, {"2": "Retraction in: J 2026", "9": None})
    assert n == 1
    rows = {r["pmid"]: (r["retracted"], r["retraction_note"]) for r in
            conn.execute("SELECT pmid, retracted, retraction_note FROM article")}
    assert rows["2"] == (1, "Retraction in: J 2026")
    assert rows["1"] == (0, None)
    # idempotent: already-retracted rows are not counted again
    assert corpus.mark_retracted(conn, {"2": "Retraction in: J 2026"}) == 0


def test_coverage_reports_refresh_per_pack(tmp_path):
    conn = _fresh(tmp_path)
    corpus.build(conn, [_art("1")], slug="sleep", version="1", license="L")
    report = corpus.coverage(conn)
    (row,) = report["pack_rows"]
    assert row["slug"] == "sleep" and row["refreshed_at"] is None
    corpus.add(conn, [_art("2")], slug="sleep", license="L",
               window_from="a", window_to="b", matched=1)
    assert corpus.coverage(conn)["pack_rows"][0]["refreshed_at"] is not None
```

Add `import pytest` to that file's imports if missing.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_literature_schema.py tests/test_literature_corpus.py -q`
Expected: failures on `LITERATURE_SCHEMA_VERSION == 5`, `refreshed_at`, `corpus.add`, `PackNotInstalled`, `mark_retracted`, `pack_rows`.

- [ ] **Step 3: Implement**

`schema.py`: set `LITERATURE_SCHEMA_VERSION = 5`. In `SCHEMA_SQL`, add to the `pack` table after `source_manifest_sha256 TEXT,`:

```sql
    -- Set by `literature refresh`; null until the first one. built_at is
    -- the version's build, this is the last time NCBI was asked for more.
    refreshed_at           TEXT,
```

(keep the exact spelling `refreshed_at           TEXT,` on its own line with a trailing newline; the migration test strips that line to fake a v4 file) and add after the `article_pack` index:

```sql
-- One row per pack per `literature refresh`: the window asked for, how
-- many PubMed matched, how many were new here, how many existing rows were
-- marked retracted. The smallest useful form of "what changed on each
-- refresh" (ROADMAP #5). (v5.)
CREATE TABLE IF NOT EXISTS refresh_log (
    id          INTEGER PRIMARY KEY,
    pack_id     INTEGER NOT NULL REFERENCES pack(id) ON DELETE CASCADE,
    ran_at      TEXT NOT NULL,
    window_from TEXT NOT NULL,
    window_to   TEXT NOT NULL,
    matched     INTEGER NOT NULL,
    added       INTEGER NOT NULL,
    retracted   INTEGER NOT NULL DEFAULT 0
);
```

Add `migrate` and call it from `connect`:

```python
# Versions this build can bring forward in place. Every earlier bump
# refused the file and asked for --rebuild; that was fine at 1.6 MB and is
# not at 62 MB. Below the floor the file is still refused.
MIGRATABLE_FROM = 4


def migrate(conn: sqlite3.Connection) -> None:
    """Bring a corpus at MIGRATABLE_FROM up to the current version in place.
    No-op when already current; raises for anything older."""
    found = read_version(conn)
    if found == LITERATURE_SCHEMA_VERSION:
        return
    if found != MIGRATABLE_FROM:
        check_version(conn)  # raises with the --rebuild message
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pack)")}
    if "refreshed_at" not in cols:
        conn.execute("ALTER TABLE pack ADD COLUMN refreshed_at TEXT")
    conn.executescript(SCHEMA_SQL)  # CREATE IF NOT EXISTS: adds refresh_log, touches nothing else
    conn.execute("UPDATE corpus_meta SET value = ? WHERE key = 'schema_version'",
                 (str(LITERATURE_SCHEMA_VERSION),))
    conn.commit()
```

In `connect`, replace the `check_version(conn)` call under `if existed:` with `migrate(conn)`, keeping the close-on-raise. Update the module docstring's "v4" mention if it names the version.

`corpus.py`: add `class PackNotInstalled(RuntimeError)`, `AddStats`, and refactor. Extract the per-article body of `build()`'s loop (from `body = article.abstract.strip()` through `stats.articles += 1`) into

```python
def _upsert(conn, article, *, pack_id: int, license: str, full_text: bool,
            stamp: str, stats) -> int | None:
    """Write one article and link it to the pack. Returns the article id,
    or None when it was skipped for having no abstract."""
```

with `stats.chunks`/`stats.skipped`/`stats.articles` updated inside, and have `build()` call it. Then:

```python
def add(conn, articles, *, slug: str, license: str, window_from: str,
        window_to: str, matched: int, retracted: int = 0,
        full_text: bool = False) -> AddStats:
    """`literature refresh`: upsert and link, never unlink. The pack's
    version and built_at are untouched; refreshed_at and one refresh_log
    row record what happened."""
    row = conn.execute("SELECT id FROM pack WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise PackNotInstalled(f"{slug} is not installed")
    pack_id = row["id"]
    stats = AddStats()
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    for article in articles:
        existed = conn.execute("SELECT 1 FROM article WHERE pmid = ?",
                               (article.pmid,)).fetchone() is not None
        article_id = _upsert(conn, article, pack_id=pack_id, license=license,
                             full_text=full_text, stamp=stamp, stats=stats)
        if article_id is not None and not existed:
            stats.added += 1
    conn.execute("UPDATE pack SET refreshed_at = ? WHERE id = ?", (stamp, pack_id))
    conn.execute(
        "INSERT INTO refresh_log(pack_id, ran_at, window_from, window_to, "
        "matched, added, retracted) VALUES(?,?,?,?,?,?,?)",
        (pack_id, stamp, window_from, window_to, matched, stats.added, retracted))
    _refresh_count(conn, pack_id)
    schema.rebuild_chunk_fts(conn)
    log.info("refreshed pack %s: %d matched, %d added, %d retracted",
             slug, matched, stats.added, retracted)
    return stats


def mark_retracted(conn, notes: dict[str, str | None]) -> int:
    """Flip `retracted` on the rows that exist and are not already marked.
    Returns how many changed."""
    flipped = 0
    for pmid, note in notes.items():
        cur = conn.execute(
            "UPDATE article SET retracted = 1, retraction_note = ? "
            "WHERE pmid = ? AND retracted = 0", (note, pmid))
        flipped += cur.rowcount
    conn.commit()
    return flipped
```

`AddStats` mirrors `BuildStats` (`articles, chunks, skipped`) plus `added`. In `coverage()`, add `"pack_rows": [dict(r) for r in conn.execute("SELECT slug, version, built_at, refreshed_at, article_count FROM pack ORDER BY slug")]` to both return dicts (the empty-corpus early return too).

- [ ] **Step 4: Run the suite**

Run: `python -m pytest tests/ -q`
Expected: all pass, including the existing `test_connect_refuses_a_corpus_at_another_version` (check what version it writes; if it writes 4 expecting a refusal, change it to write 3, since 4 now migrates, and say so in the test's docstring).

- [ ] **Step 5: Commit**

```bash
git add health_agent/literature/schema.py health_agent/literature/corpus.py tests/test_literature_schema.py tests/test_literature_corpus.py
git commit -m "literature schema v5: refreshed_at and refresh_log, migrated in place; corpus.add and mark_retracted"
```

---

### Task 2: `eutils` window parameters and `fetch/refresh.py`

**Files:**
- Modify: `health_agent/literature/fetch/eutils.py`
- Create: `health_agent/literature/fetch/refresh.py`
- Test: `tests/test_literature_fetch.py`

**Interfaces:**
- Consumes: `eutils.search(term, *, sort, mindate, maxdate, get, sleep)`, `eutils.search_slices`, `eutils.fetch_term(term, *, sort, max_articles, first_year, get, sleep, progress) -> (count, articles)`, `packs.PackSpec.query`, `.search_term()`, `corpus.add`, `corpus.mark_retracted`.
- Produces:
  ```python
  # eutils.py
  def search(term, *, sort=None, mindate=None, maxdate=None, datetype="pdat", get=..., sleep=...)
  def fetch_term(term, *, sort, max_articles, first_year, get=..., sleep=..., progress=None,
                 mindate: str | None = None, maxdate: str | None = None, datetype: str = "pdat")
      # with a window: the whole-count search and every slice carry the window and datetype;
      # year/month slices are clipped to the window
  # refresh.py
  RETRACTED_PT = '"Retracted Publication"[Publication Type]'
  @dataclass
  class RefreshStats: window_from: str; window_to: str; matched: int; added: int; retracted: int; chunks: int
  def window_for(built_at: str, refreshed_at: str | None, *, since: str | None, today: date) -> tuple[str, str]
      # ("YYYY/MM/DD", "YYYY/MM/DD") for E-utilities; start is since, else the date part of refreshed_at
      # else built_at, minus one day
  def retraction_term(spec) -> str      # f"({spec.query}) AND {RETRACTED_PT}"
  def refresh_pack(conn, spec, *, since=None, today=None, get=client.get, sleep=time.sleep, progress=None) -> RefreshStats
  ```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_literature_fetch.py`:

```python
# --------------------------------------------------------------------------- #
# refresh
# --------------------------------------------------------------------------- #

from datetime import date

from health_agent.literature import corpus, medline, schema
from health_agent.literature.fetch import refresh


def test_esearch_sends_the_window_with_the_named_datetype():
    urls = []
    eutils.search("x", mindate="2026/09/19", maxdate="2026/10/04", datetype="edat",
                  get=lambda url, **kw: (urls.append(url), (FIX / "esearch.xml").read_bytes())[1],
                  sleep=lambda s: None)
    assert "datetype=edat" in urls[0] and "mindate=2026%2F09%2F19" in urls[0]


def test_window_for_starts_one_day_before_the_last_refresh_or_build():
    assert refresh.window_for("2026-09-20T20:00:00-04:00", None, since=None,
                              today=date(2026, 10, 4)) == ("2026/09/19", "2026/10/04")
    assert refresh.window_for("2026-09-20T20:00:00-04:00", "2026-10-01T09:00:00-04:00",
                              since=None, today=date(2026, 10, 4)) == ("2026/09/30", "2026/10/04")
    assert refresh.window_for("2026-09-20T20:00:00-04:00", "2026-10-01T09:00:00-04:00",
                              since="2026-01-01", today=date(2026, 10, 4)) == ("2026/01/01", "2026/10/04")


def test_retraction_term_has_no_window_and_no_evidence_filter():
    spec = pack_format.CATALOG["sleep"]
    term = refresh.retraction_term(spec)
    assert term.startswith(f"({spec.query}) AND ")
    assert "Retracted Publication" in term
    assert "Meta-Analysis" not in term and "[dp]" not in term


def _fake_ncbi(additions_xml: bytes, retractions_xml: bytes, *, add_count: int,
               ret_count: int):
    """esearch answers the additions query (has mindate) with add_count and
    the retractions query (has 'Retracted') with ret_count; efetch serves
    the matching fixture."""
    from urllib.parse import parse_qs, unquote, urlparse
    seen = {"esearch": [], "efetch": []}

    def get(url, **kw):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        if "esearch" in url:
            seen["esearch"].append(q)
            count = ret_count if "Retracted" in unquote(q["term"]) else add_count
            web = "RET" if "Retracted" in unquote(q["term"]) else "ADD"
            return (f"<eSearchResult><Count>{count}</Count><WebEnv>{web}</WebEnv>"
                    f"<QueryKey>1</QueryKey></eSearchResult>").encode()
        seen["efetch"].append(q)
        return retractions_xml if q["WebEnv"] == "RET" else additions_xml
    return get, seen


def test_refresh_pack_adds_marks_retractions_and_logs(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    fixture = medline.parse_articles((FIX / "corpus.xml").read_bytes())
    # the corpus holds the fixture's first two articles; one of them will be retracted
    corpus.build(conn, fixture[:2], slug="sleep", version="2026.09", license="L")
    existing = [a.pmid for a in fixture[:2]]

    additions = (FIX / "efetch_batch.xml").read_bytes()      # PMIDs 40000001, 40000002
    ret = fixture[0]
    ret.retracted, ret.retraction_note = True, "Retraction in: J 2026"
    retractions_xml = _medline_xml([ret])                     # see helper below
    get, seen = _fake_ncbi(additions, retractions_xml, add_count=2, ret_count=1)

    stats = refresh.refresh_pack(conn, pack_format.CATALOG["sleep"], today=date(2026, 10, 4),
                                 get=get, sleep=lambda s: None)

    add_q = next(q for q in seen["esearch"] if "mindate" in q)
    assert add_q["datetype"] == "edat" and add_q["maxdate"] == "2026/10/04"
    assert "hasabstract" in add_q["term"]
    ret_q = next(q for q in seen["esearch"] if "mindate" not in q)
    assert "Retracted" in ret_q["term"]
    assert stats.matched == 2 and stats.retracted == 1
    assert stats.added == len({"40000001", "40000002"} - set(existing))
    row = conn.execute("SELECT retracted FROM article WHERE pmid = ?", (existing[0],)).fetchone()
    assert row["retracted"] == 1
    assert conn.execute("SELECT added, retracted FROM refresh_log").fetchone()[:] == (stats.added, 1)


def test_refresh_pack_with_nothing_new_still_logs_and_checks_retractions(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    corpus.build(conn, medline.parse_articles((FIX / "corpus.xml").read_bytes())[:1],
                 slug="sleep", version="2026.09", license="L")
    get, seen = _fake_ncbi(b"", b"", add_count=0, ret_count=0)
    stats = refresh.refresh_pack(conn, pack_format.CATALOG["sleep"], today=date(2026, 10, 4),
                                 get=get, sleep=lambda s: None)
    assert (stats.matched, stats.added, stats.retracted) == (0, 0, 0)
    assert seen["efetch"] == []                      # nothing to fetch, nothing fetched
    assert conn.execute("SELECT COUNT(*) FROM refresh_log").fetchone()[0] == 1


def test_refresh_pack_leaves_the_corpus_alone_when_the_fetch_fails(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    corpus.build(conn, medline.parse_articles((FIX / "corpus.xml").read_bytes())[:1],
                 slug="sleep", version="2026.09", license="L")

    def broken(url, **kw):
        raise client.FetchError("eutils.ncbi.nlm.nih.gov", 400, "Bad Request")

    with pytest.raises(client.FetchError):
        refresh.refresh_pack(conn, pack_format.CATALOG["sleep"], today=date(2026, 10, 4),
                             get=broken, sleep=lambda s: None)
    assert conn.execute("SELECT COUNT(*) FROM refresh_log").fetchone()[0] == 0
    assert conn.execute("SELECT refreshed_at FROM pack").fetchone()[0] is None


def test_refresh_pack_refuses_sample():
    with pytest.raises(refresh.NotRefreshable):
        refresh.refresh_pack(None, pack_format.CATALOG["sample"], today=date(2026, 10, 4),
                             get=lambda *a, **k: b"", sleep=lambda s: None)
```

The `_medline_xml(articles)` helper: build a minimal PubmedArticleSet string from `ParsedArticle`s that `medline.parse_articles` reads back, including a `<CommentsCorrections RefType="RetractionIn">` element when `retracted` is set. Read `tests/fixtures/literature/corpus.xml` and `health_agent/literature/medline.py::_retraction` (line ~145) for the exact element the parser looks for, and keep the helper under 25 lines. If a fixture already contains a retracted article (check `corpus.xml` for `RetractionIn`), reuse it instead of the helper.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_literature_fetch.py -q -k "window or retraction or refresh or datetype"`
Expected: `ModuleNotFoundError` for `refresh`, and `search()` rejecting `datetype`.

- [ ] **Step 3: Implement**

`eutils.py`:
- `search(...)` gains `datetype: str = "pdat"`; the `if mindate or maxdate:` block uses `datetype=datetype`.
- `search_slices(term, *, first_year, last_year, get, sleep, mindate=None, maxdate=None, datetype="pdat")`: the whole-count search passes the window; each year slice is clipped to the window (skip years wholly outside it; clip the first and last year's `mindate`/`maxdate` to the window's edges); month slices likewise. Represent the window as `date` objects internally and format with `strftime("%Y/%m/%d")`.
- `fetch_term(..., mindate=None, maxdate=None, datetype="pdat")` passes all three to `search` (capped path) and `search_slices`. When a window is given and `first_year` is None, derive `first_year`/`last_year` from the window.

`refresh.py`:

```python
"""`literature refresh`: what PubMed has added to an installed pack's query
since the last refresh, plus retractions, folded into the corpus.

Two queries per pack. Additions use the Entrez date (the day PubMed added
the record), because "new since my last refresh" means indexed since then,
and a 2025 paper indexed in 2026 would be missed by publication date.
Retractions are asked for without a window or an evidence filter: a
retracted trial is retracted whatever its tier, and a snapshot would cite
it forever.

Orchestration only: the network is `eutils` and `client`, the writes are
`corpus.add` and `corpus.mark_retracted`. Nothing here prints.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .. import corpus
from ..packs import PackSpec
from . import client, eutils

RETRACTED_PT = '"Retracted Publication"[Publication Type]'
# One day of overlap at the start of every window: a record indexed on the
# boundary day is fetched twice rather than never, and the upsert makes the
# second fetch a no-op.
OVERLAP_DAYS = 1


class NotRefreshable(ValueError):
    """`sample` is a fixed snapshot; there is nothing to refresh it toward."""


@dataclass
class RefreshStats:
    window_from: str
    window_to: str
    matched: int
    added: int
    retracted: int
    chunks: int


def _date_part(stamp: str) -> date:
    return datetime.fromisoformat(stamp).date()


def window_for(built_at: str, refreshed_at: str | None, *, since: str | None,
               today: date) -> tuple[str, str]:
    if since:
        start = date.fromisoformat(since)
    else:
        start = _date_part(refreshed_at or built_at) - timedelta(days=OVERLAP_DAYS)
    return start.strftime("%Y/%m/%d"), today.strftime("%Y/%m/%d")


def retraction_term(spec: PackSpec) -> str:
    return f"({spec.query}) AND {RETRACTED_PT}"


def refresh_pack(conn, spec: PackSpec, *, since: str | None = None,
                 today: date | None = None, get=client.get, sleep=time.sleep,
                 progress=None) -> RefreshStats:
    if spec.max_articles is not None:
        raise NotRefreshable(f"{spec.slug} is a fixed snapshot of "
                             f"{spec.max_articles} articles; install a topical "
                             f"pack to have something to refresh")
    row = conn.execute("SELECT built_at, refreshed_at FROM pack WHERE slug = ?",
                       (spec.slug,)).fetchone()
    if row is None:
        raise corpus.PackNotInstalled(f"{spec.slug} is not installed")
    window_from, window_to = window_for(row["built_at"], row["refreshed_at"],
                                        since=since, today=today or date.today())

    # Both fetches complete before anything is written, so a failure in
    # either leaves the corpus exactly as it was.
    matched, additions = eutils.fetch_term(
        spec.search_term(), sort=None, max_articles=None, first_year=None,
        mindate=window_from, maxdate=window_to, datetype="edat",
        get=get, sleep=sleep, progress=progress)
    _, retracted_articles = eutils.fetch_term(
        retraction_term(spec), sort=None, max_articles=None,
        first_year=spec.since_year, get=get, sleep=sleep)

    flipped = corpus.mark_retracted(
        conn, {a.pmid: a.retraction_note for a in retracted_articles})
    stats = corpus.add(conn, additions, slug=spec.slug, license=_license(),
                       window_from=window_from, window_to=window_to,
                       matched=matched, retracted=flipped)
    return RefreshStats(window_from, window_to, matched, stats.added, flipped,
                        stats.chunks)


def _license() -> str:
    from ..packs import PACK_LICENSE
    return PACK_LICENSE
```

`fetch_term` with `max_articles=None` and `count == 0` must return `(0, [])` without an efetch (check `fetch_all`'s loop: `while start < total` already does this; the sliced path must skip empty handles, which `search_slices` does).

One thing the tests pin: `refresh_pack` runs the retraction fetch through `fetch_term` too, so the tests' fake must answer the retraction esearch without `mindate`. Also the retracted articles carry `retracted=True` from the parser only when the MEDLINE record says so; `mark_retracted` uses `a.retraction_note`, which may be None. That is fine: the query itself asserted the publication type.

- [ ] **Step 4: Run the suite**

Run: `python -m pytest tests/ -q`
Expected: all pass, including `tests/test_no_network.py`'s import scan over `literature/` (refresh.py imports nothing forbidden).

- [ ] **Step 5: Commit**

```bash
git add health_agent/literature/fetch/eutils.py health_agent/literature/fetch/refresh.py tests/test_literature_fetch.py
git commit -m "literature refresh: additions by Entrez date and retractions, through the sliced fetcher"
```

---

### Task 3: The command, consent version 2, status and check lines

**Files:**
- Modify: `health_agent/cli.py` (`cmd_literature_refresh` next to `cmd_literature_install`; parser after `p_lit_install`; `cmd_literature_status`; `cmd_literature_packs`; `cmd_check`'s packs line; the network-posture text at ~1902 and the `literature-consent` description at ~2213)
- Modify: `health_agent/consent.py` (`LITERATURE` notice)
- Test: `tests/test_cli.py`, `tests/test_cloud_tier.py` (or wherever `consent.LITERATURE` is tested), `tests/test_no_network.py`

**Interfaces:**
- Consumes: `refresh.refresh_pack`, `refresh.NotRefreshable`, `corpus.PackNotInstalled`, `_confirm_literature(cfg, assume_yes)`, `_installed_packs(cfg)`, `_open_literature_corpus`, `embeddings.get_embedder`, `lit_embed.embed_corpus`, `_reclaim_literature_vectors`, `corpus.coverage()["pack_rows"]`.
- Produces: `health-agent literature refresh [slug ...] [--since YYYY-MM-DD] [--yes] [--no-embed] [--embed-backend {ollama,hashing}]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def _refresh_stub(monkeypatch, *, added=3, retracted=1):
    """refresh_pack stand-in that writes a real refresh_log row through
    corpus.add so status and check have something to read."""
    from health_agent.literature import corpus, medline
    from health_agent.literature.fetch import refresh

    calls = []

    def fake(conn, spec, *, since=None, today=None, get=None, sleep=None, progress=None):
        calls.append((spec.slug, since))
        arts = [medline.ParsedArticle(pmid=f"9{i}", title=f"New {i}", abstract="Text.",
                                      publication_types=["Journal Article"])
                for i in range(added)]
        stats = corpus.add(conn, arts, slug=spec.slug, license="L",
                           window_from=since or "2026/09/19", window_to="2026/10/04",
                           matched=added, retracted=retracted)
        return refresh.RefreshStats("2026/09/19", "2026/10/04", added, stats.added,
                                    retracted, stats.chunks)
    monkeypatch.setattr(refresh, "refresh_pack", fake)
    return calls


def _install_sleep(main, tmp_path, index):
    pack_path = _write_sleep_pack(tmp_path)
    assert main(["--index", str(index), "literature", "install", "sleep",
                 "--from", str(pack_path), "--yes", "--no-embed"]) == 0


def test_literature_refresh_refreshes_installed_packs_and_reports(tmp_path, capsys, monkeypatch):
    from health_agent.cli import main
    index = tmp_path / ".index" / "health.db"
    _install_sleep(main, tmp_path, index)
    calls = _refresh_stub(monkeypatch)
    capsys.readouterr()

    code = main(["--index", str(index), "literature", "refresh", "--yes", "--no-embed"])
    out = capsys.readouterr().out
    assert code == 0
    assert calls == [("sleep", None)]
    assert "sleep" in out and "2026/09/19" in out and "3 added" in out and "1 retracted" in out

    code = main(["--index", str(index), "literature", "status"])
    out = capsys.readouterr().out
    assert "sleep@1" in out and "refreshed" in out and "+3" in out

    code = main(["--index", str(index), "check"])
    out = capsys.readouterr().out
    assert "sleep@1" in out and "refreshed" in out


def test_literature_refresh_sample_is_a_snapshot(tmp_path, capsys, monkeypatch):
    from health_agent.cli import main
    index = tmp_path / ".index" / "health.db"
    _install_sleep(main, tmp_path, index)
    calls = _refresh_stub(monkeypatch)
    capsys.readouterr()
    code = main(["--index", str(index), "literature", "refresh", "sample", "--yes"])
    assert code == 0
    assert "fixed snapshot" in capsys.readouterr().out
    assert calls == []


def test_literature_refresh_uninstalled_slug_exits_before_any_request(tmp_path, capsys, monkeypatch):
    from health_agent.cli import main
    index = tmp_path / ".index" / "health.db"
    _install_sleep(main, tmp_path, index)
    calls = _refresh_stub(monkeypatch)
    capsys.readouterr()
    code = main(["--index", str(index), "literature", "refresh", "exercise", "--yes"])
    assert code == 2
    assert "not installed" in capsys.readouterr().err
    assert calls == []


def test_literature_refresh_since_is_passed_through(tmp_path, capsys, monkeypatch):
    from health_agent.cli import main
    index = tmp_path / ".index" / "health.db"
    _install_sleep(main, tmp_path, index)
    calls = _refresh_stub(monkeypatch)
    capsys.readouterr()
    assert main(["--index", str(index), "literature", "refresh", "sleep",
                 "--since", "2026-01-01", "--yes", "--no-embed"]) == 0
    assert calls == [("sleep", "2026-01-01")]


def test_literature_refresh_asks_again_after_the_notice_changed(tmp_path, capsys, monkeypatch):
    """Consent recorded under notice version 1 does not cover refresh."""
    from health_agent import consent
    from health_agent.cli import main
    index = tmp_path / ".index" / "health.db"
    _install_sleep(main, tmp_path, index)
    _refresh_stub(monkeypatch)
    record_path = consent.consent_path(tmp_path / ".index", consent.LITERATURE)
    data = json.loads(record_path.read_text())
    data["notice_version"] = 1
    record_path.write_text(json.dumps(data))
    assert consent.needs_prompt(tmp_path / ".index", notice=consent.LITERATURE)
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    capsys.readouterr()
    assert main(["--index", str(index), "literature", "refresh", "--no-embed"]) == 1
```

Check the exact JSON field name for the version in a consent record (`consent.ConsentRecord`) and the exact behaviour of `_confirm_literature` on "no" (exit 1 is what `install` returns; match it). Check that `_write_sleep_pack` exists near line 875 and what version string it stamps (the tests above assume "1").

In `tests/test_cloud_tier.py` (or `tests/test_consent.py` if that exists), add:

```python
def test_literature_notice_is_version_2_and_names_refresh():
    assert consent.LITERATURE.version == 2
    assert "literature refresh" in consent.LITERATURE.text
    assert "eutils.ncbi.nlm.nih.gov" in consent.LITERATURE.text
    assert "refresh" in consent.LITERATURE.summary
```

In `tests/test_no_network.py`, extend the test that asserts `cmd_literature_install` imports fetch lazily (find it; if there is none, add one that reads `cli.py` and asserts no module-level `from .literature.fetch` / `from .literature import fetch` line exists outside a function body, by checking every line starting with `from .literature.fetch` is indented).

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_cli.py -q -k refresh`
Expected: argparse rejects `refresh`.

- [ ] **Step 3: Implement**

`consent.py`: `LITERATURE.version = 2`. In the notice text, change "Two literature commands do" to "Three literature commands do", and insert before the `build-pack` paragraph:

```
  health-agent literature refresh [pack ...]
    Sends each installed pack's search terms (the same fixed list as
    build-pack) plus a date range to eutils.ncbi.nlm.nih.gov, with your
    IP address and the tool's version, and NCBI_API_KEY if it is set.
    Because you only refresh packs you have installed, NCBI can tell
    which broad areas you chose. That is the same kind of fact GitHub
    sees at install, from a second party, and it repeats on every
    refresh. Nothing from your data folder. Nothing about your
    questions.
```

Change "Neither command runs on its own" to "None of them runs on its own". Update `summary` to mention refresh: "...refresh sends the same terms with a date range to NCBI, on demand...".

`cli.py`, after `cmd_literature_install`:

```python
def cmd_literature_refresh(args: argparse.Namespace, cfg: config.Config) -> int:
    """Ask NCBI for what each installed pack's query has gained since the
    last refresh, and for retractions, and fold both in.

    Slugs are checked against the catalog and the installed set before
    consent and before any request, so a typo costs nothing. Each pack is
    committed as it completes; a failure on one leaves the earlier ones
    refreshed and that one untouched.
    """
    from .literature import corpus as lit_corpus
    from .literature import embed as lit_embed
    from .literature import packs, schema as lit_schema

    try:
        installed = _installed_packs(cfg)
    except lit_schema.CorpusSchemaVersionMismatch as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not installed:
        print("No packs installed; `health-agent literature install <pack>` first.",
              file=sys.stderr)
        return 2

    slugs = args.slugs or [s for s in installed if s in packs.CATALOG
                           and packs.CATALOG[s].max_articles is None]
    for slug in slugs:
        if slug not in packs.CATALOG:
            print(f"{slug!r} is not a known pack.", file=sys.stderr)
            return 2
        if slug not in installed:
            print(f"{slug} is not installed; nothing to refresh.", file=sys.stderr)
            return 2
    if not _confirm_literature(cfg, assume_yes=args.yes):
        return 1

    from .literature.fetch import refresh as lit_refresh

    conn = lit_schema.connect(cfg.literature_path)
    failed = False
    try:
        for slug in slugs:
            spec = packs.CATALOG[slug]
            if spec.max_articles is not None:
                print(f"{slug} is a fixed snapshot of {spec.max_articles} articles; "
                      f"install a topical pack to have something to refresh.")
                continue
            try:
                stats = lit_refresh.refresh_pack(
                    conn, spec, since=args.since,
                    progress=lambda done, total: print(f"  {done}/{total}", flush=True)
                    if done % 5000 == 0 or done == total else None)
            except Exception as exc:  # noqa: BLE001 - reported, never a traceback
                print(f"{slug}: refresh failed: {exc}", file=sys.stderr)
                failed = True
                continue
            print(f"{slug}: {stats.window_from} to {stats.window_to}, "
                  f"{stats.matched} matched, {stats.added} added, "
                  f"{stats.retracted} retracted, {stats.chunks} chunks")
        if not args.no_embed:
            embedder = embeddings.get_embedder(args.embed_backend)
            store = vector_store.VectorStore(cfg.literature_vector_path,
                                             table_name=lit_embed.TABLE_NAME)
            try:
                done = lit_embed.embed_corpus(conn, store, embedder)
                print(f"embedded {done} chunks with {embedder.name}")
            except (embeddings.EmbeddingUnavailable, embeddings.RemoteHostRefused):
                print("Ollama unavailable; new articles are keyword-searchable "
                      "until you run this again.", file=sys.stderr)
            _reclaim_literature_vectors(conn, store)
    finally:
        conn.close()
    return 2 if failed else 0
```

The progress lambda above returns `None` either way; write it as a small nested `def` instead if the conditional expression reads badly. `refresh_pack` raising `NotRefreshable` cannot happen here (the snapshot check runs first) but is caught by the broad except regardless.

Parser, after `p_lit_install`'s `set_defaults`:

```python
    p_lit_refresh = lit_sub.add_parser(
        "refresh",
        help="fetch what PubMed has added to your installed packs since the "
             "last refresh, and mark retractions (asks once before connecting)",
        description="For each installed pack, asks NCBI for records added to "
                    "PubMed since the pack was built or last refreshed, adds "
                    "them to the corpus, and marks any of the pack's articles "
                    "that PubMed now lists as retracted. `sample` is a fixed "
                    "snapshot and is skipped. Sends the pack's fixed search "
                    "terms and a date range to eutils.ncbi.nlm.nih.gov.")
    p_lit_refresh.add_argument("slugs", nargs="*", metavar="slug",
                               help="packs to refresh (default: every installed topical pack)")
    p_lit_refresh.add_argument("--since", metavar="YYYY-MM-DD",
                               help="start of the window, instead of the last refresh or build")
    p_lit_refresh.add_argument("--yes", action="store_true",
                               help="accept the network notice without prompting")
    p_lit_refresh.add_argument("--no-embed", action="store_true")
    p_lit_refresh.add_argument("--embed-backend", default="ollama",
                               choices=("ollama", "hashing"))
    p_lit_refresh.set_defaults(func=cmd_literature_refresh)
```

Validate `--since` with `date.fromisoformat` at the top of the command and print a clear error (exit 2) on a bad value.

`cmd_literature_status`: after the `packs:` line, one line per `report["pack_rows"]`:
`f"  {slug}@{version}  built {built_at[:10]}  " + (f"refreshed {refreshed_at[:10]} (+{added}, {retracted} retracted)" if refreshed_at else "never refreshed")`, where `added`/`retracted` come from the latest `refresh_log` row for that pack (add a small query in `coverage()` or in the command; keep it in `coverage()` as `"last_refresh": {"added","retracted","ran_at"} | None` per pack row).

`cmd_literature_packs`: the installed state gains `, refreshed <date>` or `, never refreshed` for catalog packs with `max_articles is None`.

`cmd_check`: `_installed_packs` returns `(version, count)`; extend it to `(version, count, refreshed_at)` and update its two callers (`packs`, `check`) and its tests. The check line becomes e.g. `literature packs: 2 installed (cardiovascular@2026.09, refreshed 14 days ago; sleep@2026.09, never refreshed)`; `sample` shows no refresh note.

Network posture text (`cmd_check`, ~line 1902): "Two commands connect to the internet, `literature install` and `literature build-pack`" becomes "Three commands connect to the internet, `literature install`, `literature refresh` and `literature build-pack`". Same change in the `literature-consent` parser description (~2213) and in `offline-check`'s help/description if it names the two commands (grep `offline` in `build_parser`).

- [ ] **Step 4: Run the suite**

Run: `python -m pytest tests/ -q`
Expected: all pass. `tests/test_check_reports_installed_packs` and the `_installed_packs` tuple change need their assertions updated for the new line format.

- [ ] **Step 5: Commit**

```bash
git add health_agent/cli.py health_agent/consent.py tests/test_cli.py tests/test_cloud_tier.py tests/test_no_network.py
git commit -m "literature refresh command; consent notice version 2; status and check show staleness"
```

---

### Task 4: Docs

**Files:**
- Modify: `THREAT_MODEL.md` (Claim 6, ~lines 168-198)
- Modify: `README.md` (literature section ~lines 355-390; the "two commands" sentences at ~112 and ~561; the "What is not built" style list if any names refresh)
- Modify: `ROADMAP.md` (#1's refresh bullet ~line 91; #5 change tracking ~line 283-290)
- Test: none new; `python -m pytest tests/ -q` still passes (some tests read README text: grep `README` in `tests/` before editing sentences they pin).

- [ ] **Step 1: THREAT_MODEL Claim 6**

Retitle "## Claim 6: literature packs, what two commands send, and nothing else does" to "...what three commands send...". Add a bullet between `install` and `build-pack`:

```
- `health-agent literature refresh [pack ...]` sends each installed pack's
  search terms (the same fixed catalog list `build-pack` sends) with a date
  range, `datetype=edat`, to `eutils.ncbi.nlm.nih.gov`, then a second query
  for the pack's topic with `"Retracted Publication"[Publication Type]`.
  Both carry your IP address and the tool's version, and `NCBI_API_KEY` if
  set. Because only installed packs are refreshed, NCBI can infer which
  broad areas you chose: the same class of fact GitHub sees at install,
  from a second party, repeated on every refresh. The response is parsed
  and written to the corpus; the log row records the window and counts,
  not the request.
```

In "**When.**", "one of those two commands" becomes "one of those three commands", and add: "Nothing schedules a refresh and nothing checks whether one is due; `check` reports how long since each pack was refreshed and stops there."

- [ ] **Step 2: README**

Literature section: after the paragraph ending "See THREAT_MODEL.md Claim 6.", add:

```markdown
Packs go stale at the rate PubMed grows. `health-agent literature refresh`
asks NCBI, from your machine, for what has been added to each installed
pack's query since the pack was built or last refreshed, adds those
abstracts, and marks any of the pack's articles PubMed now lists as
retracted. It sends the pack's fixed search terms and a date range, so
NCBI learns which broad areas you have installed; that is the same kind
of fact GitHub sees at install, and it repeats each time you run the
command. It never runs on its own. `sample` is a fixed snapshot and is
skipped. `literature status` shows when each pack was last refreshed and
what that added; `check` says how long ago.
```

Change the two "two commands" sentences (~112, ~561) to name three. In the quick-start block, no new line (refresh is not a first-run step).

- [ ] **Step 3: ROADMAP**

#1's bullet about "refreshed by an explicit `update-literature` command" (~line 91): mark shipped as `literature refresh`, one sentence, link to the spec. #5 (~283-290): note that `refresh_log` records the window and counts per refresh, which is the smallest form of this; the per-article "what changed" view is still open.

- [ ] **Step 4: Humanizer pass and suite**

Read `../brainiac/skills/humanizer/SKILL.md` "Hard Rules" and patterns 7-11; fix any match in the new prose. No em dashes. Then:

Run: `python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add THREAT_MODEL.md README.md ROADMAP.md
git commit -m "docs: literature refresh in the threat model, README, and roadmap"
```

---

## Self-review notes

- Spec §1 command and flags: Task 3. `sample` handling: Task 2 raises `NotRefreshable`, Task 3 prints and skips before calling. Uninstalled slug exit 2 before consent: Task 3 checks before `_confirm_literature`.
- Spec §2 two queries, `edat`, upsert without unlink, retractions marked: Tasks 1 and 2.
- Spec §3 window: Task 2 `window_for`, `OVERLAP_DAYS = 1`, `--since`.
- Spec §4 schema v5, migration, `refresh_log`, `add`, `mark_retracted`, embedding after: Tasks 1 and 3.
- Spec §5 notice v2, THREAT_MODEL, posture text: Tasks 3 and 4.
- Spec §6 status, packs, check lines, README, ROADMAP: Tasks 3 and 4.
- Spec §8 tests: each named case appears in Tasks 1-3; the no-network scan is existing plus Task 3's lazy-import check.
- Types: `corpus.add` signature identical in Tasks 1, 2 (caller) and 3 (test stub); `corpus.AddStats` (the add() result, read as `.added`/`.chunks` in Task 2) and `refresh.RefreshStats` (what `refresh_pack` returns) are distinct on purpose.
