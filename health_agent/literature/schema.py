"""`literature.db` — the corpus index.

Versioned independently of the personal index (`store/sqlite_schema.py`, v4),
because the two have unrelated lifecycles: a corpus refresh is not an ingest,
and a personal schema change must not invalidate a large download. Now at v5:
earlier bumps refused an older file outright, but v4 to v5 migrates in place
instead, since a corpus is no longer a small download that is cheap to redo.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

LITERATURE_SCHEMA_VERSION = 5

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS corpus_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per installed pack. Present from v1 even though slice 1 builds only
-- local packs: without pack identity, both distributable topic packs and corpus
-- versioning need a migration later.
-- One installed version per pack: UNIQUE(slug) alone, not (slug, version).
-- A newer version replaces the older one's row, so this table always says
-- what is installed rather than accumulating every version ever built.
CREATE TABLE IF NOT EXISTS pack (
    id                     INTEGER PRIMARY KEY,
    slug                   TEXT NOT NULL,
    title                  TEXT,
    version                TEXT NOT NULL,
    built_at               TEXT NOT NULL,
    article_count          INTEGER NOT NULL DEFAULT 0,
    source_manifest_sha256 TEXT,
    -- Set by `literature refresh`; null until the first one. built_at is
    -- the version's build, this is the last time NCBI was asked for more.
    refreshed_at           TEXT,
    UNIQUE(slug)
);

CREATE TABLE IF NOT EXISTS article (
    id                  INTEGER PRIMARY KEY,
    -- No pack_id. An article belongs to every pack that holds it, through
    -- article_pack below, and is stored once by PMID: an "exercise and
    -- hypertension" paper installed from two packs must be one finding,
    -- not two. (v4; v1-v3 keyed articles per pack.)
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_article_pmid ON article(pmid);
CREATE INDEX IF NOT EXISTS idx_article_tier ON article(evidence_rank, pub_year);

-- Which packs hold which articles. Many-to-many: an article stays once it is
-- stored, and a pack rebuild or removal changes only its rows here.
CREATE TABLE IF NOT EXISTS article_pack (
    article_id INTEGER NOT NULL REFERENCES article(id) ON DELETE CASCADE,
    pack_id    INTEGER NOT NULL REFERENCES pack(id) ON DELETE CASCADE,
    PRIMARY KEY (article_id, pack_id)
);

CREATE INDEX IF NOT EXISTS idx_article_pack_pack ON article_pack(pack_id);

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

CREATE TABLE IF NOT EXISTS mesh_term (
    article_id INTEGER NOT NULL REFERENCES article(id) ON DELETE CASCADE,
    term       TEXT NOT NULL,
    -- MEDLINE's own MajorTopicYN, as 1/0. coverage() counts only major
    -- terms: counting every heading alike named the population (Humans,
    -- Male, Female, Middle Aged) instead of the subject. (v3; v2 had no
    -- column, so every heading counted the same.)
    major      INTEGER NOT NULL DEFAULT 0
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
            migrate(conn)
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
            f"{LITERATURE_SCHEMA_VERSION}. Recreate it with "
            f"`health-agent literature install <pack> --rebuild`, or "
            f"`health-agent literature build --from <medline.xml> --rebuild` "
            f"from your own MEDLINE export."
        )


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


def rebuild_chunk_fts(conn: sqlite3.Connection) -> None:
    """Repopulate the external-content FTS index after a bulk load."""
    conn.execute("INSERT INTO article_chunk_fts(article_chunk_fts) "
                 "VALUES('rebuild')")
    conn.commit()
