"""SQLite schema for the structured store, plus connection helpers.

Design notes worth keeping in view:

* **Schema versioning (plan §5a).** `SCHEMA_VERSION` is stamped into `schema_meta`
  and into SQLite's own `PRAGMA user_version`. Opening an index written by a
  different version raises `SchemaVersionMismatch` with an actionable message
  instead of silently reading an incompatible layout.

* **Value columns are split.** `value_num` holds the numeric value when one
  parses; `value_text` holds the raw string. Quantity records populate the first,
  category records (sleep stages, stand hours) populate the second. Keeping them
  in one column would force every aggregate query to cast, and would let a
  non-numeric value quietly become 0.0.

* **`local_date` is precomputed.** HealthKit timestamps carry a UTC offset that
  changes with DST and travel ("2026-03-14 06:12:33 -0400"). Daily aggregation
  has to happen in the offset the record was *recorded* in, so the calendar date
  is computed once at ingest rather than re-derived in SQL.

* **`dedup_key` makes re-ingestion idempotent.** Every Apple Health export
  re-contains the full history, so ingesting a newer export over an older one
  would otherwise duplicate every prior record. Records carry no stable UUID, so
  the key is a hash of the fields that identify a sample.

* **`daily_metric` is a real table, not a view.** A real export is millions of
  rows; recomputing daily aggregates per query is the difference between a
  millisecond and several seconds (plan §3.1).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 4

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per ingested source file. Enables the incremental refresh in plan §3.4:
-- unchanged files (same hash) are skipped instead of re-parsed.
CREATE TABLE IF NOT EXISTS source_file (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    mtime        REAL,
    ingested_at  TEXT NOT NULL,
    record_count INTEGER NOT NULL DEFAULT 0,
    -- 'complete' | 'partial'. A file whose parse aborted part-way is marked
    -- 'partial' so the incremental skip in plan §3.4 retries it next run rather
    -- than treating a truncated ingest as done.
    status       TEXT NOT NULL DEFAULT 'complete'
);

-- Subject characteristics from the <Me> element (date of birth, biological sex,
-- blood type). Stored because age/sex are needed to interpret reference ranges
-- later; never leaves the machine.
CREATE TABLE IF NOT EXISTS characteristic (
    key            TEXT PRIMARY KEY,
    value          TEXT NOT NULL,
    source_file_id INTEGER REFERENCES source_file(id)
);

CREATE TABLE IF NOT EXISTS record (
    id             INTEGER PRIMARY KEY,
    type           TEXT NOT NULL,
    value_num      REAL,
    value_text     TEXT,
    unit           TEXT,
    source_name    TEXT,
    source_version TEXT,
    device         TEXT,
    start_date     TEXT NOT NULL,   -- ISO 8601 with offset, e.g. 2026-03-14T06:12:33-04:00
    end_date       TEXT,
    creation_date  TEXT,
    start_epoch    REAL NOT NULL,   -- UTC seconds; the offset-safe ordering key
    duration_sec   REAL,
    local_date     TEXT NOT NULL,   -- YYYY-MM-DD in the record's own local offset
    metadata_json  TEXT,
    source_file_id INTEGER REFERENCES source_file(id),
    dedup_key      BLOB NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_record_dedup   ON record(dedup_key);
CREATE INDEX IF NOT EXISTS idx_record_type_date      ON record(type, local_date);
CREATE INDEX IF NOT EXISTS idx_record_type_epoch     ON record(type, start_epoch);
CREATE INDEX IF NOT EXISTS idx_record_local_date     ON record(local_date);

CREATE TABLE IF NOT EXISTS workout (
    id                  INTEGER PRIMARY KEY,
    activity_type       TEXT NOT NULL,
    duration            REAL,
    duration_unit       TEXT,
    total_distance      REAL,
    total_distance_unit TEXT,
    total_energy        REAL,
    total_energy_unit   TEXT,
    source_name         TEXT,
    source_version      TEXT,
    device              TEXT,
    start_date          TEXT NOT NULL,
    end_date            TEXT,
    creation_date       TEXT,
    start_epoch         REAL NOT NULL,
    local_date          TEXT NOT NULL,
    metadata_json       TEXT,
    statistics_json     TEXT,
    source_file_id      INTEGER REFERENCES source_file(id),
    dedup_key           BLOB NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_workout_dedup  ON workout(dedup_key);
CREATE INDEX IF NOT EXISTS idx_workout_date          ON workout(local_date);
CREATE INDEX IF NOT EXISTS idx_workout_type          ON workout(activity_type, start_epoch);

-- Apple's own per-day ring totals. Kept separate from `daily_metric` because
-- these are Apple's numbers, not ours; useful as a cross-check on our own
-- aggregation of the underlying samples.
CREATE TABLE IF NOT EXISTS activity_summary (
    local_date               TEXT PRIMARY KEY,
    active_energy            REAL,
    active_energy_goal       REAL,
    active_energy_unit       TEXT,
    move_time                REAL,
    move_time_goal           REAL,
    exercise_time            REAL,
    exercise_time_goal       REAL,
    stand_hours              REAL,
    stand_hours_goal         REAL,
    source_file_id           INTEGER REFERENCES source_file(id)
);

-- Materialized daily rollup, rebuilt at the end of each ingest.
--
-- Grain is (date, type, source, unit, category). The `source_name` dimension is
-- load-bearing: an iPhone and an Apple Watch both record step counts for the
-- same day, so summing blindly across sources roughly doubles the total. Keeping
-- source in the grain lets the query layer choose how to combine (see
-- store/queries.py).
-- No declared primary key: `unit`, `category` and `source_name` are all
-- legitimately NULL, and a WITHOUT ROWID primary key would force them NOT NULL.
-- Uniqueness is guaranteed by construction (the table is DELETEd and rebuilt
-- from a single GROUP BY), so the indexes below are for lookup only.
CREATE TABLE IF NOT EXISTS daily_metric (
    local_date       TEXT NOT NULL,
    type             TEXT NOT NULL,
    source_name      TEXT,
    unit             TEXT,
    category         TEXT,            -- category value (e.g. sleep stage), else NULL
    n                INTEGER NOT NULL,
    sum_value        REAL,
    avg_value        REAL,
    min_value        REAL,
    max_value        REAL,
    duration_sec     REAL
);

CREATE INDEX IF NOT EXISTS idx_daily_type_date ON daily_metric(type, local_date);

-- --------------------------------------------------------------------------
-- Documents: bloodwork PDFs and medical records (schema v2, milestone 2)
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS document (
    id               INTEGER PRIMARY KEY,
    source_file_id   INTEGER NOT NULL REFERENCES source_file(id),
    path             TEXT NOT NULL,
    kind             TEXT NOT NULL,   -- 'pdf' (records) | 'note' (markdown, text, docx) | 'image' (photo or screenshot, OCR)
    title            TEXT,
    doc_date         TEXT,            -- collection date, or a note's own date
    page_count       INTEGER NOT NULL DEFAULT 0,
    extraction       TEXT NOT NULL,   -- 'text' | 'ocr' | 'mixed' | 'none'
    ingested_at      TEXT NOT NULL,
    -- Raw note frontmatter, kept whole even when only some keys are understood,
    -- so nothing the user wrote is silently dropped (schema v3).
    frontmatter_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_document_source ON document(source_file_id);
CREATE INDEX IF NOT EXISTS idx_document_kind   ON document(kind, doc_date);

-- Tags from note frontmatter and inline #hashtags. A separate table rather than
-- a delimited column so filtering is an index lookup instead of a LIKE scan.
CREATE TABLE IF NOT EXISTS document_tag (
    document_id INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    PRIMARY KEY (document_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_tag ON document_tag(tag);

CREATE TABLE IF NOT EXISTS document_page (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    page_no     INTEGER NOT NULL,     -- 1-indexed, as a human would cite it
    text        TEXT,
    char_count  INTEGER NOT NULL DEFAULT 0,
    extraction  TEXT NOT NULL         -- 'text' | 'ocr' | 'none'
);

CREATE INDEX IF NOT EXISTS idx_page_document ON document_page(document_id, page_no);

-- Structured lab values, kept separate from the free text they came from
-- (plan §3.2). Every row carries document + page + the exact source line, so
-- any number the agent quotes can be traced back to a spot in a file.
CREATE TABLE IF NOT EXISTS lab_result (
    id             INTEGER PRIMARY KEY,
    document_id    INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    page_no        INTEGER NOT NULL,
    analyte        TEXT NOT NULL,     -- name exactly as printed
    analyte_key    TEXT NOT NULL,     -- normalized, for cross-report trends
    panel          TEXT,
    value_num      REAL,
    value_text     TEXT,              -- non-numeric results ("Negative", "<5")
    unit           TEXT,
    ref_low        REAL,
    ref_high       REAL,
    ref_text       TEXT,              -- reference range exactly as printed
    flag           TEXT,              -- H / L / A as printed by the lab
    collected_date TEXT,
    raw_line       TEXT,              -- the source line, for citation and audit
    extracted_by   TEXT NOT NULL,     -- 'table' | 'line' — which pass found it
    -- 1 when the page had no text layer and this value came through OCR.
    -- OCR misreads digits (a reference range of 100-199 can arrive as 100-139),
    -- so these values carry real uncertainty that a text-layer value does not,
    -- and the CLI marks them rather than presenting both as equally solid
    -- (schema v4).
    via_ocr        INTEGER NOT NULL DEFAULT 0,
    dedup_key      BLOB NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_lab_dedup ON lab_result(dedup_key);
CREATE INDEX IF NOT EXISTS idx_lab_analyte     ON lab_result(analyte_key, collected_date);
CREATE INDEX IF NOT EXISTS idx_lab_document    ON lab_result(document_id);

-- Chunked free text. Chunks live here as the durable copy; their embeddings
-- live in the LanceDB store alongside a pointer back to `chunk.id`. Keeping the
-- text in SQLite means re-embedding (new model, changed chunk size) never
-- requires re-parsing the PDFs, and gives a keyword fallback when no embedding
-- model is available.
CREATE TABLE IF NOT EXISTS chunk (
    id            INTEGER PRIMARY KEY,
    document_id   INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    page_no       INTEGER,
    -- Heading trail for a markdown chunk ("Symptoms > Week of March 10").
    -- A note has no page numbers, so this is what a citation points at.
    section       TEXT,
    chunk_index   INTEGER NOT NULL,
    text          TEXT NOT NULL,
    char_count    INTEGER NOT NULL,
    embedded_with TEXT,               -- "provider:model" once embedded
    embedded_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_chunk_document ON chunk(document_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_chunk_pending  ON chunk(embedded_with);

-- External-content FTS index over `chunk.text`. Populated explicitly at ingest
-- rather than by triggers, so a bulk load doesn't pay per-row index costs.
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    text,
    content='chunk',
    content_rowid='id',
    tokenize='porter unicode61'
);
"""

# Weekly/monthly rollups are cheap views over the daily table: the daily table
# has one row per (day, type, source), which is small enough that grouping it on
# demand costs nothing, and a second materialized table would need its own
# rebuild path. These views exist for ad-hoc SQL against the index; the CLI and
# (later) the agent's tools go through store/queries.py, which applies the same
# period bucketing — weeks are labelled by their Monday, because SQLite's
# strftime has no portable ISO-week specifier and a date label can't drift
# between the two paths.
VIEWS_SQL = """
DROP VIEW IF EXISTS weekly_metric;
CREATE VIEW weekly_metric AS
SELECT
    date(local_date, 'weekday 0', '-6 days') AS period,
    type,
    source_name,
    unit,
    category,
    SUM(n)                         AS n,
    SUM(sum_value)                 AS sum_value,
    -- Sample-weighted mean: averaging the daily averages would weight a day with
    -- 3 samples the same as a day with 300.
    CASE WHEN SUM(n) > 0 THEN SUM(avg_value * n) / SUM(n) END AS avg_value,
    MIN(min_value)                 AS min_value,
    MAX(max_value)                 AS max_value,
    SUM(duration_sec)              AS duration_sec,
    MIN(local_date)                AS period_start,
    MAX(local_date)                AS period_end
FROM daily_metric
GROUP BY period, type, source_name, unit, category;

DROP VIEW IF EXISTS monthly_metric;
CREATE VIEW monthly_metric AS
SELECT
    substr(local_date, 1, 7) AS period,
    type,
    source_name,
    unit,
    category,
    SUM(n)                   AS n,
    SUM(sum_value)           AS sum_value,
    CASE WHEN SUM(n) > 0 THEN SUM(avg_value * n) / SUM(n) END AS avg_value,
    MIN(min_value)           AS min_value,
    MAX(max_value)           AS max_value,
    SUM(duration_sec)        AS duration_sec,
    MIN(local_date)          AS period_start,
    MAX(local_date)          AS period_end
FROM daily_metric
GROUP BY period, type, source_name, unit, category;
"""


class SchemaVersionMismatch(RuntimeError):
    """Raised when an existing index was written by a different schema version."""


class IndexNotFound(RuntimeError):
    """Raised when a command needs an index that hasn't been created yet."""


def connect(path: Path, *, create: bool = False) -> sqlite3.Connection:
    """Open the index at `path`.

    With `create=False` a missing file is an error rather than an empty database,
    so a typo'd `--index` doesn't silently produce "no data found".
    """
    path = Path(path)
    if not path.exists():
        if not create:
            raise IndexNotFound(
                f"No index at {path}. Run `health-agent ingest` first."
            )
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    """Create tables/views and stamp the schema version. Idempotent."""
    conn.executescript(SCHEMA_SQL)
    conn.executescript(VIEWS_SQL)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def read_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return int(row["value"]) if row else None


def check_version(conn: sqlite3.Connection) -> None:
    """Fail loudly on a version mismatch, with the command that fixes it."""
    found = read_version(conn)
    if found is None:
        raise SchemaVersionMismatch(
            "This index has no schema version stamp and predates the current "
            "format. Run `health-agent ingest --rebuild` to recreate it."
        )
    if found != SCHEMA_VERSION:
        raise SchemaVersionMismatch(
            f"Index schema version is {found}, this build expects "
            f"{SCHEMA_VERSION}. Run `health-agent ingest --rebuild` to recreate it."
        )


class AggregatesMissing(RuntimeError):
    """Raised when raw records exist but the daily rollup was never built."""


def check_aggregates(conn: sqlite3.Connection) -> None:
    """Guard against reading an index whose rollup was never built.

    Every read path goes through `daily_metric` for speed, so an index with rows
    in `record` but nothing in `daily_metric` would report "no data" for
    everything. That failure is indistinguishable from an empty export unless we
    check for it explicitly.
    """
    has_records = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM record) AS e"
    ).fetchone()["e"]
    if not has_records:
        return
    has_daily = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM daily_metric) AS e"
    ).fetchone()["e"]
    if not has_daily:
        raise AggregatesMissing(
            "This index has records but no daily aggregates. Run "
            "`health-agent ingest --rebuild` to recompute them."
        )


def open_for_read(path: Path) -> sqlite3.Connection:
    """Open an existing index, verify its schema version and its rollup."""
    conn = connect(path, create=False)
    check_version(conn)
    check_aggregates(conn)
    return conn


def mark_source_file(conn: sqlite3.Connection, source_file_id: int, *,
                     record_count: int, status: str = "complete") -> None:
    """Stamp a source file as fully (or partially) ingested.

    Every ingest path must call this on success. A file left at the default
    'partial' status is re-parsed on every subsequent run, which silently
    defeats the incremental refresh in plan §3.4 — the ingest still produces
    correct results, so the only symptom is that it never gets faster.
    """
    from datetime import datetime as _dt

    conn.execute(
        "UPDATE source_file SET record_count = ?, status = ?, ingested_at = ? "
        "WHERE id = ?",
        (record_count, status,
         _dt.now().astimezone().isoformat(timespec="seconds"), source_file_id),
    )
    conn.commit()


def rebuild_chunk_fts(conn: sqlite3.Connection) -> None:
    """Resync the external-content FTS index with `chunk`.

    Called after documents are added or removed. `rebuild` rather than
    incremental delete/insert commands: an external-content FTS table needs the
    *old* text to delete a row cleanly, which is gone once the chunk is deleted,
    and at this corpus size a full rebuild is milliseconds.
    """
    conn.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('rebuild')")
    conn.commit()


def clear_document_data(conn: sqlite3.Connection, source_file_id: int) -> int:
    """Remove every document (and its pages, labs, chunks) from one source file.

    Documents are replaced wholesale rather than merged, unlike HealthKit
    records: a re-exported PDF is a new rendering of the same report, and
    accumulating both copies would double every lab value in a trend.
    """
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM document WHERE source_file_id = ?",
        (source_file_id,),
    ).fetchone()["n"]
    conn.execute("DELETE FROM document WHERE source_file_id = ?", (source_file_id,))
    conn.commit()
    return int(rows)


def rebuild_daily_metrics(conn: sqlite3.Connection) -> int:
    """Recompute the materialized daily rollup from `record`. Returns row count.

    Full recompute rather than incremental: a new export can backfill older days
    (a device syncing late), so touching only the newly-seen dates would leave
    stale rows behind. On a multi-million-row index this is a few seconds.
    """
    conn.execute("DELETE FROM daily_metric")
    conn.execute(
        """
        INSERT INTO daily_metric (
            local_date, type, source_name, unit, category,
            n, sum_value, avg_value, min_value, max_value, duration_sec
        )
        SELECT
            local_date,
            type,
            source_name,
            unit,
            value_text,
            COUNT(*),
            SUM(value_num),
            AVG(value_num),
            MIN(value_num),
            MAX(value_num),
            SUM(duration_sec)
        FROM record
        GROUP BY local_date, type, source_name, unit, value_text
        """
    )
    conn.commit()
    return conn.execute("SELECT COUNT(*) AS c FROM daily_metric").fetchone()["c"]
