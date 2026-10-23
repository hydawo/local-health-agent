# Event Windows and Workout Detail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the per-workout statistics already stored in the index, and add the window, series, comparison and derivation primitives that let a question be asked about the time either side of a dated event.

**Architecture:** Nothing new is read from disk. `workout.statistics_json` and `workout.metadata_json` have held the per-workout heart-rate statistics and the indoor flag since the first ingest, so schema v6 adds a `workout_stat` table and a `workout.indoor` column and backfills both from those blobs in place. Four new query functions expose workout detail and workout series, a new `store/windows.py` composes them into windows and comparisons with a rule-based comparability verdict, and exactly one new agent tool (`compare_windows`) is added so later sub-projects can add sources without adding tools.

**Tech Stack:** Python 3.11+, stdlib only for this sub-project (`json`, `sqlite3`, `dataclasses`, `statistics`), pytest.

**Spec:** [`docs/superpowers/specs/2026-10-22-event-windows-and-workout-detail-design.md`](../specs/2026-10-22-event-windows-and-workout-detail-design.md)

## Global Constraints

- **Descriptive only.** Counts, medians, means, minima, maxima, ratios, percentiles, window comparisons. No model fits, no effect sizes, no p-values, no correlation coefficients, no seasonal adjustment. This binds every payload.
- **`compare` never returns a difference, a percentage change, or a ratio between its two sides.** Both numbers go back; the gap is the reader's business.
- **Units are never converted between measurement systems.** A derivation reports the unit its inputs carried and names it. Inputs in units that cannot be combined return `value=None` with `reason="incompatible units: <a> and <b>"`. Composing a duration in minutes into an hourly rate is a time composition, is named in the unit string, and is allowed; converting mg/dL to mmol/L or miles to kilometres is not.
- **Derivations are a closed list of five:** `pace`, `speed`, `efficiency`, `ratio`, `percentile`. No expression language.
- **Absence is reported as coverage.** Every empty path names what the index does hold. This doctrine was broken four times on the previous branch; every message added here is read against it.
- **`PERIODS` stays `("day", "week", "month")`.** No year bucket. Both era comparisons use explicit ranges.
- **Exactly one new agent tool.** Seven become eight. Later sub-projects add sources and add no tools.
- **`comparable` never says `significant`, `likely`, `suggests`, or anything about cause.**
- **No new guardrail pattern.** Prompt rule 1f and the `comparable` verdict are the controls.
- **Schema v6 migrates a v5 file in place** from JSON the file already holds. A version below 5 still refuses and names `ingest --rebuild`.
- **`workout_stat` keys are the names the export uses.** A statistic composes as `{type}:{aggregate}`, e.g. `HKQuantityTypeIdentifierHeartRate:average`. A metadata entry keeps its bare key, e.g. `HKIndoorWorkout`.
- **Do not modify `tests/fixtures/export.xml`.** Existing tests assert exact counts over it (25 records, 1 workout, 2 activity summaries). Extra workouts for tests are inserted directly into a temp index.
- **The ACL target numbers are never committed.** T1, T3 and T7 in the spec come from the maintainer's own export. The committed eval asks the same question shapes against the synthetic fixtures with fixture answers; the real numbers are verified by the maintainer on his machine and recorded as a run note in `tests/eval_results.md`, never as a fixture value, a test constant or a doc figure.
- **No em dashes in new prose. The word "doctor" is never used; it is "clinician", and the command is `check`.**
- **The whole suite passes** (901 before this plan) and `health-agent offline-check` passes.

---

### Task 1: Pull statistics and metadata out of the stored JSON

**Files:**
- Create: `health_agent/store/workout_stats.py`
- Test: `tests/test_workout_stats.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `stat_rows(statistics_json: str | None, metadata_json: str | None) -> list[tuple[str, str, float | None, str | None, str | None]]` yielding `(kind, key, value_num, value_text, unit)`; `indoor_flag(metadata_json: str | None) -> int | None`; `STAT_AGGS: tuple[str, ...]`; `INDOOR_KEY: str`.

The exact shapes this parses, confirmed against `tests/fixtures/export.xml` and `ingest/healthkit.py`:

- `metadata_json` is a JSON **object**: `{"HKAlgorithmVersion": "2", "HKIndoorWorkout": "0"}`
- `statistics_json` is a JSON **list of attribute dicts**: `[{"type": "HKQuantityTypeIdentifierHeartRate", "startDate": "...", "endDate": "...", "average": "150", "minimum": "110", "maximum": "175", "unit": "count/min"}]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_workout_stats.py
"""The statistics and metadata a workout carries, read out of stored JSON.

The strings here are the shapes `ingest/healthkit.py` writes, copied from
`tests/fixtures/export.xml`. Nothing in this module touches an export.
"""
from __future__ import annotations

from health_agent.store import workout_stats

STATS = (
    '[{"average": "150", "endDate": "2026-03-06 17:30:00 -0500", '
    '"maximum": "175", "minimum": "110", '
    '"startDate": "2026-03-06 17:00:00 -0500", '
    '"type": "HKQuantityTypeIdentifierHeartRate", "unit": "count/min"}]'
)
META = '{"HKAlgorithmVersion": "2", "HKIndoorWorkout": "0"}'


def test_a_statistic_becomes_one_row_per_aggregate_the_export_recorded():
    rows = workout_stats.stat_rows(STATS, None)
    assert [(r[0], r[1], r[2], r[4]) for r in rows] == [
        ("statistic", "HKQuantityTypeIdentifierHeartRate:average", 150.0, "count/min"),
        ("statistic", "HKQuantityTypeIdentifierHeartRate:minimum", 110.0, "count/min"),
        ("statistic", "HKQuantityTypeIdentifierHeartRate:maximum", 175.0, "count/min"),
    ]


def test_metadata_keeps_its_bare_key_and_parses_a_number_when_it_is_one():
    rows = workout_stats.stat_rows(None, META)
    assert ("metadata", "HKAlgorithmVersion", 2.0, None, None) in rows
    assert ("metadata", "HKIndoorWorkout", 0.0, None, None) in rows


def test_a_malformed_or_missing_blob_yields_no_rows_rather_than_raising():
    """The migration runs over every workout already in the index. One
    unparseable blob must not cost the rest of them."""
    assert workout_stats.stat_rows(None, None) == []
    assert workout_stats.stat_rows("not json", "{oops") == []
    assert workout_stats.stat_rows('{"not": "a list"}', '["not an object"]') == []
    assert workout_stats.stat_rows('[{"no": "type"}]', None) == []


def test_a_non_numeric_statistic_keeps_its_text_instead_of_becoming_a_number():
    rows = workout_stats.stat_rows(
        '[{"type": "T", "average": "n/a", "unit": "u"}]', None)
    assert rows == [("statistic", "T:average", None, "n/a", "u")]


def test_the_indoor_flag_reads_true_false_and_absent():
    assert workout_stats.indoor_flag('{"HKIndoorWorkout": "1"}') == 1
    assert workout_stats.indoor_flag('{"HKIndoorWorkout": "0"}') == 0
    assert workout_stats.indoor_flag('{"HKIndoorWorkout": "true"}') == 1
    assert workout_stats.indoor_flag('{"HKAlgorithmVersion": "2"}') is None
    assert workout_stats.indoor_flag(None) is None
    assert workout_stats.indoor_flag('{"HKIndoorWorkout": "maybe"}') is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_workout_stats.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'health_agent.store.workout_stats'`

- [ ] **Step 3: Write the implementation**

```python
# health_agent/store/workout_stats.py
"""The statistics and metadata a workout carries, read out of the JSON the
parser already stores on the row.

`ingest/healthkit.py` writes two blobs per workout: `metadata_json`, an object
of MetadataEntry key to value, and `statistics_json`, a list of
WorkoutStatistics attribute dicts. Both have been written since the first
ingest, so every number this module yields was already in the index and
nothing here reads an export.
"""
from __future__ import annotations

import json

# The aggregates the export records on a WorkoutStatistics element. Order is
# the order rows come back in, so a reader sees average before maximum.
STAT_AGGS = ("average", "minimum", "maximum", "sum")
INDOOR_KEY = "HKIndoorWorkout"
_TRUTHY = {"1", "true", "yes"}
_FALSY = {"0", "false", "no"}


def _loads(blob: str | None, want: type):
    """Parse `blob`, or return None when it is absent, malformed, or not the
    shape this caller needs. Never raises."""
    if not blob:
        return None
    try:
        parsed = json.loads(blob)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, want) else None


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def stat_rows(statistics_json: str | None, metadata_json: str | None
              ) -> list[tuple[str, str, float | None, str | None, str | None]]:
    """`(kind, key, value_num, value_text, unit)` per entry, statistics first.

    A statistic becomes one row per aggregate the export recorded, keyed
    `{type}:{aggregate}`, so a heart-rate average and a heart-rate maximum are
    separate rows under names the export itself used. A value that is not a
    number is kept as text rather than dropped or coerced.
    """
    rows: list[tuple[str, str, float | None, str | None, str | None]] = []
    for entry in _loads(statistics_json, list) or []:
        if not isinstance(entry, dict):
            continue
        type_name = entry.get("type")
        if not type_name:
            continue
        unit = entry.get("unit")
        for agg in STAT_AGGS:
            if agg not in entry:
                continue
            raw = entry[agg]
            value = _num(raw)
            rows.append(("statistic", f"{type_name}:{agg}", value,
                         None if value is not None else _text(raw), unit))
    for key, raw in sorted((_loads(metadata_json, dict) or {}).items()):
        value = _num(raw)
        rows.append(("metadata", key, value,
                     None if value is not None else _text(raw), None))
    return rows


def _text(raw) -> str | None:
    return None if raw is None else str(raw)


def indoor_flag(metadata_json: str | None) -> int | None:
    """1, 0, or None for `HKIndoorWorkout`. None means the export did not say,
    which is different from saying outdoor."""
    raw = (_loads(metadata_json, dict) or {}).get(INDOOR_KEY)
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in _TRUTHY:
        return 1
    if text in _FALSY:
        return 0
    return None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_workout_stats.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add health_agent/store/workout_stats.py tests/test_workout_stats.py
git commit -m "workout statistics and metadata, out of the JSON already stored"
```

---

### Task 2: Schema v6, migrated in place

**Files:**
- Modify: `health_agent/store/sqlite_schema.py` (`SCHEMA_VERSION`, `SCHEMA_SQL`, add `migrate`, wire `open_for_read`)
- Modify: `health_agent/cli.py:187` (call `migrate` beside the existing `check_version`)
- Test: `tests/test_schema_v6.py`

**Interfaces:**
- Consumes: `workout_stats.stat_rows`, `workout_stats.indoor_flag` from Task 1.
- Produces: `SCHEMA_VERSION == 6`; table `workout_stat(workout_id, kind, key, value_num, value_text, unit)`; column `workout.indoor INTEGER`; `migrate(conn) -> int | None` returning the version migrated from, or None when nothing was done.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schema_v6.py
"""Schema v6: the first in-place migration of the personal index.

Refusing a v5 file would cost a multi-gigabyte reparse to recover data the
file already holds in two JSON columns, which is the same argument the
literature corpus already accepts at its own v5.
"""
from __future__ import annotations

import sqlite3

import pytest

from health_agent.store import sqlite_schema

STATS = ('[{"average": "150", "maximum": "175", "minimum": "110", '
         '"type": "HKQuantityTypeIdentifierHeartRate", "unit": "count/min"}]')
META = '{"HKIndoorWorkout": "1"}'


def _v5_index(path, *, stats=STATS, meta=META):
    """A v5 index holding one workout with its JSON blobs and no workout_stat."""
    conn = sqlite_schema.connect(path, create=True)
    sqlite_schema.initialize(conn)
    conn.execute("DROP TABLE workout_stat")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(workout)")}
    assert "indoor" in cols, "v6 initialize should create the column"
    conn.execute(
        "INSERT INTO workout(activity_type, duration, duration_unit, "
        "start_date, start_epoch, local_date, metadata_json, statistics_json, "
        "dedup_key) VALUES('HKWorkoutActivityTypeRunning', 30, 'min', "
        "'2026-03-06T17:00:00-05:00', 1772830800, '2026-03-06', ?, ?, X'01')",
        (meta, stats))
    conn.execute("UPDATE workout SET indoor = NULL")
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', '5') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    conn.commit()
    return conn


def test_a_v5_index_migrates_in_place_and_backfills_from_its_own_json(tmp_path):
    conn = _v5_index(tmp_path / "health.db")
    assert sqlite_schema.read_version(conn) == 5

    assert sqlite_schema.migrate(conn) == 5

    assert sqlite_schema.read_version(conn) == 6
    sqlite_schema.check_version(conn)          # no longer raises
    rows = {(r["kind"], r["key"], r["value_num"], r["unit"]) for r in
            conn.execute("SELECT kind, key, value_num, unit FROM workout_stat")}
    assert ("statistic", "HKQuantityTypeIdentifierHeartRate:average",
            150.0, "count/min") in rows
    assert ("metadata", "HKIndoorWorkout", 1.0, None) in rows
    assert conn.execute("SELECT indoor FROM workout").fetchone()["indoor"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM workout").fetchone()["n"] == 1


def test_migrating_twice_changes_nothing_and_reports_nothing_to_do(tmp_path):
    conn = _v5_index(tmp_path / "health.db")
    sqlite_schema.migrate(conn)
    before = conn.execute("SELECT COUNT(*) AS n FROM workout_stat").fetchone()["n"]
    assert sqlite_schema.migrate(conn) is None
    after = conn.execute("SELECT COUNT(*) AS n FROM workout_stat").fetchone()["n"]
    assert after == before


def test_a_malformed_blob_migrates_to_no_rows_without_failing_the_migration(tmp_path):
    conn = _v5_index(tmp_path / "health.db", stats="not json", meta="{oops")
    assert sqlite_schema.migrate(conn) == 5
    assert conn.execute("SELECT COUNT(*) AS n FROM workout_stat").fetchone()["n"] == 0
    assert conn.execute("SELECT indoor FROM workout").fetchone()["indoor"] is None


def test_a_version_below_five_still_refuses_and_names_the_rebuild(tmp_path):
    conn = _v5_index(tmp_path / "health.db")
    conn.execute("UPDATE schema_meta SET value = '4' WHERE key = 'schema_version'")
    conn.commit()
    assert sqlite_schema.migrate(conn) is None
    with pytest.raises(sqlite_schema.SchemaVersionMismatch) as excinfo:
        sqlite_schema.check_version(conn)
    assert "ingest --rebuild" in str(excinfo.value)


def test_open_for_read_migrates_so_no_caller_ever_sees_a_v5_file(tmp_path):
    path = tmp_path / "health.db"
    conn = _v5_index(path)
    conn.execute("INSERT INTO record(type, value_num, start_date, start_epoch, "
                 "local_date, dedup_key) VALUES('T', 1, '2026-03-06T00:00:00Z', "
                 "1772830800, '2026-03-06', X'02')")
    conn.execute("INSERT INTO daily_metric(local_date, type, n) "
                 "VALUES('2026-03-06', 'T', 1)")
    conn.commit()
    conn.close()

    opened = sqlite_schema.open_for_read(path)
    assert sqlite_schema.read_version(opened) == 6
    opened.close()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_schema_v6.py -v`
Expected: FAIL, `AttributeError: module 'health_agent.store.sqlite_schema' has no attribute 'migrate'`, and the `workout_stat` drop fails because the table does not exist yet.

- [ ] **Step 3: Write the implementation**

In `health_agent/store/sqlite_schema.py`, change the version:

```python
SCHEMA_VERSION = 6
```

Add `indoor` to the `workout` table in `SCHEMA_SQL`, immediately after `metadata_json`:

```sql
    metadata_json       TEXT,
    statistics_json     TEXT,
    indoor              INTEGER,          -- HKIndoorWorkout: 1, 0, or NULL for "the export did not say"
```

Add the new table to `SCHEMA_SQL`, after the `workout` indexes:

```sql
-- One row per statistic or metadata entry a workout carries. The key is the
-- name the export used: '<type>:<aggregate>' for a WorkoutStatistics value,
-- the bare key for a MetadataEntry.
CREATE TABLE IF NOT EXISTS workout_stat (
    workout_id  INTEGER NOT NULL REFERENCES workout(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,            -- 'statistic' | 'metadata'
    key         TEXT NOT NULL,
    value_num   REAL,
    value_text  TEXT,
    unit        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workout_stat_key
    ON workout_stat(workout_id, kind, key);
CREATE INDEX IF NOT EXISTS idx_workout_stat_lookup
    ON workout_stat(kind, key);
```

Add the migration beside `check_version`:

```python
def migrate(conn: sqlite3.Connection) -> int | None:
    """Bring a v5 index to v6 in place. Returns the version migrated from.

    This is the first in-place migration for the personal index, which has
    refused every earlier bump. The argument is the one the literature corpus
    already accepts at its own v5: refusing costs a multi-gigabyte, minutes-long
    reparse of an export whose bytes have not changed, to recover data the index
    already holds in `statistics_json` and `metadata_json`. A migration that
    reads only columns already in the file cannot produce a number the export
    did not contain.

    Anything below v5 is left alone, so `check_version` still refuses it and
    still names `ingest --rebuild`.
    """
    if read_version(conn) != 5:
        return None
    conn.executescript(SCHEMA_SQL)      # idempotent; creates workout_stat
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(workout)")}
    if "indoor" not in columns:
        conn.execute("ALTER TABLE workout ADD COLUMN indoor INTEGER")
    backfill_workout_stats(conn)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return 5


def backfill_workout_stats(conn: sqlite3.Connection) -> None:
    """Fill `workout_stat` and `workout.indoor` from each row's own JSON."""
    written = 0
    rows = conn.execute(
        "SELECT id, statistics_json, metadata_json FROM workout"
    ).fetchall()
    for row in rows:
        entries = workout_stats.stat_rows(row["statistics_json"],
                                          row["metadata_json"])
        if entries:
            conn.executemany(
                "INSERT OR IGNORE INTO workout_stat(workout_id, kind, key, "
                "value_num, value_text, unit) VALUES(?, ?, ?, ?, ?, ?)",
                [(row["id"], *entry) for entry in entries],
            )
            written += len(entries)
        conn.execute(
            "UPDATE workout SET indoor = ? WHERE id = ?",
            (workout_stats.indoor_flag(row["metadata_json"]), row["id"]),
        )
    return written
```

Import the extractor at the top of the module, beside the other imports:

```python
from health_agent.store import workout_stats
```

Call the migration from `open_for_read`, before the version check:

```python
def open_for_read(path: Path) -> sqlite3.Connection:
    """Open an existing index, migrate it if we can, verify version and rollup."""
    conn = connect(path, create=False)
    migrate(conn)
    check_version(conn)
    check_aggregates(conn)
    return conn
```

In `health_agent/cli.py`, at line 187, migrate before the existing check so an ingest into a v5 index does not refuse:

```python
            sqlite_schema.migrate(conn)
            sqlite_schema.check_version(conn)
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_schema_v6.py -v && python3 -m pytest tests/ -q`
Expected: the 5 new tests PASS. The existing suite passes; if a test asserts `SCHEMA_VERSION == 5` or a v5 stamp, update that assertion to 6 and nothing else.

- [ ] **Step 5: Commit**

```bash
git add health_agent/store/sqlite_schema.py health_agent/cli.py tests/test_schema_v6.py
git commit -m "schema v6: workout_stat and an indoor flag, migrated in place from stored JSON"
```

---

### Task 3: Populate the new table at ingest

**Files:**
- Modify: `health_agent/ingest/healthkit.py` (the workout insert path, around line 390)
- Test: `tests/test_ingest_workout_stats.py`

**Interfaces:**
- Consumes: `workout_stats.stat_rows`, `workout_stats.indoor_flag`.
- Produces: a fresh ingest writes `workout_stat` rows and `workout.indoor` without needing the migration.

> **Step 3 below is superseded. Do not follow its code.** It assumed the
> workout insert was a single `conn.execute` whose `cur.lastrowid` identified
> the row. `healthkit.py` batches workouts through `conn.executemany`, and the
> function is `ingest_export`, not `ingest`. What shipped instead: `indoor`
> joins the `_INSERT_WORKOUT` column list and its row tuple, and the stat rows
> come from `sqlite_schema.backfill_workout_stats`, made public and called once
> after the final flush, so there is one implementation of "JSON to stat rows"
> rather than two that can drift. See commit f3c4fbf.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ingest_workout_stats.py
"""A fresh ingest fills the new table directly, rather than relying on the
migration to catch up later."""
from __future__ import annotations

from pathlib import Path

from health_agent.ingest import healthkit
from health_agent.store import sqlite_schema

FIXTURE = Path(__file__).parent / "fixtures" / "export.xml"


def test_ingesting_the_fixture_export_records_its_workout_statistics(tmp_path):
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    sid, _ = healthkit.register_source_file(
        conn, FIXTURE, "healthkit", healthkit.sha256_file(FIXTURE))
    healthkit.ingest(conn, FIXTURE, source_file_id=sid)
    conn.commit()

    rows = {(r["key"], r["value_num"], r["unit"]) for r in conn.execute(
        "SELECT key, value_num, unit FROM workout_stat WHERE kind = 'statistic'")}
    assert ("HKQuantityTypeIdentifierHeartRate:average", 150.0, "count/min") in rows
    assert ("HKQuantityTypeIdentifierHeartRate:maximum", 175.0, "count/min") in rows
    # The fixture's workout is outdoor, which is not the same as unknown.
    assert conn.execute(
        "SELECT indoor FROM workout").fetchone()["indoor"] == 0


def test_a_second_ingest_of_the_same_export_adds_no_duplicate_stat_rows(tmp_path):
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    for _ in range(2):
        sid, _ = healthkit.register_source_file(
            conn, FIXTURE, "healthkit", healthkit.sha256_file(FIXTURE))
        healthkit.ingest(conn, FIXTURE, source_file_id=sid)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) AS n FROM workout_stat").fetchone()["n"]
    assert n == conn.execute(
        "SELECT COUNT(DISTINCT workout_id || kind || key) AS n FROM workout_stat"
    ).fetchone()["n"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_ingest_workout_stats.py -v`
Expected: FAIL, the `workout_stat` query returns an empty set and `indoor` is None.

- [ ] **Step 3: Write the implementation**

In `health_agent/ingest/healthkit.py`, import the extractor at the top:

```python
from health_agent.store import workout_stats
```

The workout insert currently ends at the `metadata_json, statistics_json, source_file_id, dedup_key` column list near line 390. Add `indoor` to that insert by computing it from the metadata the parser already built, then write the stat rows using the inserted row id. The workout insert uses `INSERT OR IGNORE` on `dedup_key`, so a re-ingest yields `lastrowid` for a row that was not inserted; guard on `rowcount` so the second ingest writes nothing:

```python
        cur = conn.execute(
            "INSERT OR IGNORE INTO workout(... , indoor, metadata_json, "
            "statistics_json, source_file_id, dedup_key) "
            "VALUES(..., ?, ?, ?, ?, ?)",
            (..., workout_stats.indoor_flag(meta), meta, stats, source_file_id,
             key),
        )
        if cur.rowcount:
            entries = workout_stats.stat_rows(stats, meta)
            if entries:
                conn.executemany(
                    "INSERT OR IGNORE INTO workout_stat(workout_id, kind, key, "
                    "value_num, value_text, unit) VALUES(?, ?, ?, ?, ?, ?)",
                    [(cur.lastrowid, *entry) for entry in entries],
                )
```

Keep the existing column list and value tuple intact; only `indoor` is added, and only the stat-row block is new. The exact local variable names for the metadata and statistics JSON are whatever that function already binds them to; do not rename them.

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_ingest_workout_stats.py tests/test_ingest.py -v && python3 -m pytest tests/ -q`
Expected: PASS throughout. The ingest counts printed by `cmd_ingest` do not change.

- [ ] **Step 5: Commit**

```bash
git add health_agent/ingest/healthkit.py tests/test_ingest_workout_stats.py
git commit -m "ingest writes workout statistics and the indoor flag directly"
```

---

### Task 4: The five derivations

**Files:**
- Create: `health_agent/store/derive.py`
- Test: `tests/test_derive.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `DERIVATIONS: tuple[str, ...]`; `Derived(name, value, unit, reason)` frozen dataclass; `pace(distance, distance_unit, duration, duration_unit) -> Derived`; `speed(...) -> Derived`; `efficiency(distance, distance_unit, duration, duration_unit, hr, hr_unit) -> Derived`; `ratio(name, a, unit_a, b, unit_b) -> Derived`; `percentile(values, p, unit) -> Derived`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_derive.py
"""The closed list of five derivations.

A closed list rather than an expression language, because an expression
language is the model doing arithmetic with extra steps, which the prompt has
forbidden since the day it answered 59.6 for an average that was exactly 60.0.
"""
from __future__ import annotations

import pytest

from health_agent.store import derive


def test_pace_is_duration_over_distance_in_the_units_given():
    d = derive.pace(3.1, "mi", 49.6, "min")
    assert round(d.value, 2) == 16.0
    assert d.unit == "min/mi"
    assert d.reason is None


def test_speed_composes_minutes_into_an_hourly_rate_and_names_it():
    """Composing a duration in minutes into an hourly rate is a time
    composition and is named in the unit. It is not a measurement-system
    conversion, which stays forbidden."""
    d = derive.speed(3.0, "mi", 30.0, "min")
    assert d.value == 6.0
    assert d.unit == "mi/hr"


def test_efficiency_is_speed_per_hundred_beats():
    d = derive.efficiency(3.0, "mi", 30.0, "min", 150.0, "count/min")
    assert d.value == 4.0
    assert d.unit == "mi/hr per 100 count/min"


def test_a_ratio_of_two_measures_in_the_same_unit_is_dimensionless():
    d = derive.ratio("left_right", 87.0, "lb", 100.0, "lb")
    assert d.value == 0.87
    assert d.unit is None


def test_a_ratio_across_units_returns_no_value_and_says_why():
    d = derive.ratio("left_right", 87.0, "lb", 40.0, "kg")
    assert d.value is None
    assert d.reason == "incompatible units: lb and kg"


def test_a_percentile_returns_a_value_that_was_actually_measured():
    """Nearest rank, no interpolation: a percentile should never be a number
    no instrument produced."""
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert derive.percentile(values, 50, "mph").value == 30.0
    assert derive.percentile(values, 90, "mph").value == 50.0
    assert derive.percentile(values, 90, "mph").unit == "mph"
    # Sizes where a half-to-even round() disagrees with nearest rank. n=6 and
    # n=10 are what a week of workouts and a month of readings actually land
    # on, so these are not corner cases.
    four = [1.0, 2.0, 3.0, 4.0]
    assert derive.percentile(four, 25, "u").value == 1.0
    assert derive.percentile(four, 75, "u").value == 3.0
    assert derive.percentile([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], 50, "u").value == 3.0
    ten = [float(i) for i in range(1, 11)]
    assert derive.percentile(ten, 50, "u").value == 5.0
    assert derive.percentile(ten, 90, "u").value == 9.0


def test_every_derivation_handles_missing_and_zero_inputs_without_raising():
    assert derive.pace(None, "mi", 30.0, "min").value is None
    assert derive.pace(0.0, "mi", 30.0, "min").reason == "distance is zero"
    assert derive.speed(3.0, "mi", 0.0, "min").reason == "duration is zero"
    assert derive.speed(3.0, "mi", 30.0, "fortnight").reason == (
        "unknown duration unit: fortnight")
    assert derive.efficiency(3.0, "mi", 30.0, "min", 0.0, "count/min").reason == (
        "heart rate is zero")
    assert derive.percentile([], 50, "mph").value is None


def test_the_vocabulary_is_closed():
    assert derive.DERIVATIONS == ("pace", "speed", "efficiency", "ratio",
                                  "percentile")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_derive.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'health_agent.store.derive'`

- [ ] **Step 3: Write the implementation**

```python
# health_agent/store/derive.py
"""Five derivations, by name, over values the index already holds.

A closed vocabulary rather than an expression language. The model is forbidden
from doing arithmetic, and an expression language would hand it the same
capability through a tool argument.

Units are never converted between measurement systems: a derivation reports
the unit its inputs carried and names it, and inputs that cannot be combined
return no value and a reason. Composing a duration in minutes into an hourly
rate is a time composition, is visible in the unit string, and is allowed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

DERIVATIONS = ("pace", "speed", "efficiency", "ratio", "percentile")

# Seconds-per-hour factors for the duration units an export uses.
_PER_HOUR = {"min": 60.0, "minute": 60.0, "minutes": 60.0,
             "hr": 1.0, "hour": 1.0, "hours": 1.0,
             "s": 3600.0, "sec": 3600.0, "second": 3600.0, "seconds": 3600.0}


@dataclass(frozen=True)
class Derived:
    name: str
    value: float | None
    unit: str | None
    reason: str | None = None


def _missing(name: str) -> Derived:
    return Derived(name, None, None, "a value this needs is missing")


def pace(distance: float | None, distance_unit: str | None,
         duration: float | None, duration_unit: str | None) -> Derived:
    """Duration over distance, in the units given."""
    if distance is None or duration is None:
        return _missing("pace")
    if distance == 0:
        return Derived("pace", None, None, "distance is zero")
    unit = f"{duration_unit or '?'}/{distance_unit or '?'}"
    return Derived("pace", duration / distance, unit)


def speed(distance: float | None, distance_unit: str | None,
          duration: float | None, duration_unit: str | None) -> Derived:
    """Distance per hour, in the distance unit given."""
    if distance is None or duration is None:
        return _missing("speed")
    if duration == 0:
        return Derived("speed", None, None, "duration is zero")
    factor = _PER_HOUR.get((duration_unit or "").strip().lower())
    if factor is None:
        return Derived("speed", None, None,
                       f"unknown duration unit: {duration_unit}")
    hours = duration / factor
    return Derived("speed", distance / hours, f"{distance_unit or '?'}/hr")


def efficiency(distance: float | None, distance_unit: str | None,
               duration: float | None, duration_unit: str | None,
               heart_rate: float | None, hr_unit: str | None) -> Derived:
    """Speed per hundred heartbeats. The ACL analysis's mph per 100 bpm."""
    if heart_rate is None:
        return _missing("efficiency")
    if heart_rate == 0:
        return Derived("efficiency", None, None, "heart rate is zero")
    base = speed(distance, distance_unit, duration, duration_unit)
    if base.value is None:
        return Derived("efficiency", None, None, base.reason)
    unit = f"{base.unit} per 100 {hr_unit or '?'}"
    return Derived("efficiency", base.value / (heart_rate / 100.0), unit)


def ratio(name: str, a: float | None, unit_a: str | None,
          b: float | None, unit_b: str | None) -> Derived:
    """One measure over another. Dimensionless, and only within one unit."""
    if a is None or b is None:
        return _missing(name)
    if (unit_a or None) != (unit_b or None):
        return Derived(name, None, None,
                       f"incompatible units: {unit_a} and {unit_b}")
    if b == 0:
        return Derived(name, None, None, "denominator is zero")
    return Derived(name, a / b, None)


def percentile(values, p: int, unit: str | None) -> Derived:
    """Nearest rank, no interpolation.

    An interpolated percentile is a number no instrument produced, which is the
    same objection this project raises to converting a unit.
    """
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return Derived(f"p{p}", None, unit, "no values in scope")
    rank = math.ceil(p / 100.0 * len(ordered))
    return Derived(f"p{p}", ordered[max(0, min(len(ordered) - 1, rank - 1))], unit)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_derive.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add health_agent/store/derive.py tests/test_derive.py
git commit -m "five derivations, a closed list, with units named and never converted"
```

---

### Task 5: Workout detail, measures, and sample depth

**Files:**
- Modify: `health_agent/store/queries.py` (beside the existing workout functions, around line 272)
- Test: `tests/test_workout_queries.py`

**Interfaces:**
- Consumes: `derive` from Task 4; `workout_stat` and `workout.indoor` from Task 2.

> **Amended after review.** Three things in this task changed from the text
> below. The heart-rate input to `efficiency` is pinned to the exact key
> `HKQuantityTypeIdentifierHeartRate:average`, because a substring match on
> `":average"` would feed a step-count average in as a heart rate. The
> `sample_count` integer became `sample_depth`, a dict with one entry per
> distinct statistic base type, each carrying `count`, `window_seconds`,
> `bounds` and `reason`, because a bare integer did not say what it counted and
> only the first requested measure was used. And `_SECONDS_PER` was deleted in
> favour of `derive.seconds`, which `queries.py` had duplicated with an
> inverted factor. See commits 1e130f8, 9f68cfa.
- Produces:
  - `WorkoutRow` frozen dataclass: `local_date, activity_type, duration, duration_unit, total_distance, total_distance_unit, total_energy, total_energy_unit, source_name, indoor, citation, measures: dict, derived: dict, sample_count: int | None`
  - `workout_detail(conn, *, activity=None, start=None, end=None, indoor=None, source_include=(), source_exclude=(), measures=(), derive_names=(), sample_counts=False, limit=200) -> list[WorkoutRow]`
  - `workout_measures(conn) -> list[dict]` with keys `key, kind, label, n, first, last, unit`

`citation` is `f"{activity} on {local_date}"`, because a workout has no file to name and the project's existing rule for HealthKit answers is metric plus date.

`measures` are `workout_stat.key` values. `derive_names` are from `derive.DERIVATIONS`; `ratio` and `percentile` are not available per row here (they need two named measures or a set of rows) and are rejected with `ValueError`, which Task 6 covers at the series level.

`sample_counts=True` adds, per row, the number of `record` rows whose `type` equals the measure's base type (the part of the key before `:`) and whose `start_epoch` falls within `[start_epoch, start_epoch + duration_in_seconds]`. It is opt-in because it is one subquery per row. It uses `idx_record_type_epoch`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_workout_queries.py
"""Workout detail, the measures the index holds, and sample depth.

Workout rows are inserted directly rather than added to
`tests/fixtures/export.xml`, because existing tests assert exact counts over
that file.
"""
from __future__ import annotations

import pytest

from health_agent.store import queries, sqlite_schema

HR = "HKQuantityTypeIdentifierHeartRate"


@pytest.fixture
def box(tmp_path):
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    yield conn
    conn.close()


def _workout(conn, *, date, activity="HKWorkoutActivityTypeRunning",
             duration=30.0, distance=3.0, energy=300.0, source="Watch",
             indoor=None, hr_avg=None, epoch=1772830800.0, key=b"\x01"):
    cur = conn.execute(
        "INSERT INTO workout(activity_type, duration, duration_unit, "
        "total_distance, total_distance_unit, total_energy, total_energy_unit, "
        "source_name, start_date, start_epoch, local_date, indoor, dedup_key) "
        "VALUES(?,?,'min',?,'mi',?,'kcal',?,?,?,?,?,?)",
        (activity, duration, distance, energy, source,
         f"{date}T09:00:00-05:00", epoch, date, indoor, key))
    if hr_avg is not None:
        conn.execute(
            "INSERT INTO workout_stat(workout_id, kind, key, value_num, unit) "
            "VALUES(?, 'statistic', ?, ?, 'count/min')",
            (cur.lastrowid, f"{HR}:average", hr_avg))
    conn.commit()
    return cur.lastrowid


def test_detail_returns_one_row_per_workout_with_its_requested_measure(box):
    _workout(box, date="2026-03-06", hr_avg=150.0)
    (row,) = queries.workout_detail(box, measures=(f"{HR}:average",))
    assert row.local_date == "2026-03-06"
    assert row.measures[f"{HR}:average"]["value"] == 150.0
    assert row.measures[f"{HR}:average"]["unit"] == "count/min"
    assert row.citation == "HKWorkoutActivityTypeRunning on 2026-03-06"


def test_a_requested_measure_a_workout_does_not_carry_comes_back_as_absent(box):
    _workout(box, date="2026-03-06", hr_avg=None)
    (row,) = queries.workout_detail(box, measures=(f"{HR}:average",))
    assert row.measures[f"{HR}:average"]["value"] is None


def test_derivations_are_computed_per_row_in_the_units_stored(box):
    _workout(box, date="2026-03-06", duration=30.0, distance=3.0, hr_avg=150.0)
    (row,) = queries.workout_detail(
        box, measures=(f"{HR}:average",),
        derive_names=("pace", "speed", "efficiency"))
    assert row.derived["pace"]["value"] == 10.0
    assert row.derived["pace"]["unit"] == "min/mi"
    assert row.derived["speed"]["value"] == 6.0
    assert row.derived["efficiency"]["value"] == 4.0


def test_a_per_row_derivation_that_needs_two_measures_is_refused_here(box):
    with pytest.raises(ValueError) as excinfo:
        queries.workout_detail(box, derive_names=("ratio",))
    assert "ratio" in str(excinfo.value)


def test_the_indoor_filter_separates_indoor_outdoor_and_unknown(box):
    _workout(box, date="2026-03-01", indoor=1, key=b"\x01")
    _workout(box, date="2026-03-02", indoor=0, key=b"\x02")
    _workout(box, date="2026-03-03", indoor=None, key=b"\x03")
    assert len(queries.workout_detail(box)) == 3
    assert [r.local_date for r in queries.workout_detail(box, indoor=True)] \
        == ["2026-03-01"]
    assert [r.local_date for r in queries.workout_detail(box, indoor=False)] \
        == ["2026-03-02"]


def test_source_exclude_removes_a_source_without_re_aggregating(box):
    _workout(box, date="2026-03-01", source="Watch", key=b"\x01")
    _workout(box, date="2026-03-02", source="Oura", key=b"\x02")
    rows = queries.workout_detail(box, source_exclude=("Oura",))
    assert [r.source_name for r in rows] == ["Watch"]


def test_naming_a_source_in_both_include_and_exclude_is_an_error(box):
    with pytest.raises(ValueError):
        queries.workout_detail(box, source_include=("Watch",),
                               source_exclude=("Watch",))


def test_sample_depth_counts_the_records_inside_the_workout_interval(box):
    """This is how 'these runs carry about three heart-rate samples each'
    becomes a number rather than a remembered caveat."""
    epoch = 1772830800.0
    _workout(box, date="2026-03-06", duration=30.0, epoch=epoch, hr_avg=150.0)
    for i, offset in enumerate([60.0, 600.0, 1200.0, 5400.0]):   # last one is outside
        box.execute(
            "INSERT INTO record(type, value_num, unit, start_date, start_epoch, "
            "local_date, dedup_key) VALUES(?, 60, 'count/min', "
            "'2026-03-06T09:00:00-05:00', ?, '2026-03-06', ?)",
            (HR, epoch + offset, bytes([10 + i])))
    box.commit()
    (row,) = queries.workout_detail(box, measures=(f"{HR}:average",),
                                    sample_counts=True)
    assert row.sample_count == 3
    (plain,) = queries.workout_detail(box, measures=(f"{HR}:average",))
    assert plain.sample_count is None


def test_workout_measures_names_what_the_index_holds_with_counts_and_spans(box):
    _workout(box, date="2026-03-01", hr_avg=150.0, key=b"\x01")
    _workout(box, date="2026-03-05", hr_avg=160.0, key=b"\x02")
    (measure,) = [m for m in queries.workout_measures(box)
                  if m["kind"] == "statistic"]
    assert measure["key"] == f"{HR}:average"
    assert measure["n"] == 2
    assert measure["first"] == "2026-03-01"
    assert measure["last"] == "2026-03-05"
    assert measure["unit"] == "count/min"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_workout_queries.py -v`
Expected: FAIL, `AttributeError: module 'health_agent.store.queries' has no attribute 'workout_detail'`

- [ ] **Step 3: Write the implementation**

Add to `health_agent/store/queries.py`, after `workout_summary`. Import `derive` at the top of the module beside the existing imports.

```python
@dataclass(frozen=True)
class WorkoutRow:
    local_date: str
    activity_type: str
    duration: float | None
    duration_unit: str | None
    total_distance: float | None
    total_distance_unit: str | None
    total_energy: float | None
    total_energy_unit: str | None
    source_name: str | None
    indoor: int | None
    citation: str
    measures: dict
    derived: dict
    sample_depth: dict | None = None   # see the Task 5 note below


# Derivations that need more than one row, or two named measures, and so are
# not available per workout. Task 6 offers them at the series level.
_SERIES_ONLY_DERIVATIONS = ("ratio", "percentile")

_SECONDS_PER = {"min": 60.0, "minute": 60.0, "minutes": 60.0,
                "hr": 3600.0, "hour": 3600.0, "hours": 3600.0,
                "s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0}


def _source_clauses(source_include, source_exclude, column="source_name"):
    """Shared include/exclude SQL. Naming a source in both is an error rather
    than a precedence rule nobody would remember."""
    include = tuple(source_include or ())
    exclude = tuple(source_exclude or ())
    both = set(include) & set(exclude)
    if both:
        raise ValueError(
            f"source named in both include and exclude: {sorted(both)}")
    sql, params = "", []
    if include:
        sql += f" AND {column} IN ({','.join('?' * len(include))})"
        params += list(include)
    if exclude:
        sql += (f" AND ({column} IS NULL OR {column} NOT IN "
                f"({','.join('?' * len(exclude))}))")
        params += list(exclude)
    return sql, params


def workout_detail(conn, *, activity=None, start=None, end=None, indoor=None,
                   source_include=(), source_exclude=(), measures=(),
                   derive_names=(), sample_counts=False,
                   limit=200) -> list[WorkoutRow]:
    """One row per workout, with the measures and derivations requested."""
    for name in derive_names:
        if name in _SERIES_ONLY_DERIVATIONS:
            raise ValueError(
                f"{name} needs more than one workout; use workout_series")
        if name not in derive.DERIVATIONS:
            raise ValueError(f"unknown derivation: {name}")

    sql = ("SELECT id, local_date, activity_type, duration, duration_unit, "
           "total_distance, total_distance_unit, total_energy, "
           "total_energy_unit, source_name, indoor, start_epoch "
           "FROM workout WHERE 1=1")
    params: list = []
    if activity:
        sql += " AND activity_type LIKE ?"
        params.append(f"%{activity}%")
    if start:
        sql += " AND local_date >= ?"
        params.append(start)
    if end:
        sql += " AND local_date <= ?"
        params.append(end)
    if indoor is True:
        sql += " AND indoor = 1"
    elif indoor is False:
        sql += " AND indoor = 0"
    clause, extra = _source_clauses(source_include, source_exclude)
    sql += clause
    params += extra
    sql += " ORDER BY start_epoch DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []

    wanted = tuple(measures)
    by_workout: dict[int, dict] = {r["id"]: {} for r in rows}
    if wanted:
        placeholders = ",".join("?" * len(rows))
        keys = ",".join("?" * len(wanted))
        for stat in conn.execute(
                f"SELECT workout_id, key, value_num, value_text, unit "
                f"FROM workout_stat WHERE workout_id IN ({placeholders}) "
                f"AND key IN ({keys})",
                [r["id"] for r in rows] + list(wanted)):
            by_workout[stat["workout_id"]][stat["key"]] = {
                "value": stat["value_num"],
                "text": stat["value_text"],
                "unit": stat["unit"],
            }

    out: list[WorkoutRow] = []
    for r in rows:
        held = by_workout[r["id"]]
        found = {key: held.get(key, {"value": None, "text": None, "unit": None})
                 for key in wanted}
        hr = next((v for k, v in found.items() if ":average" in k), None)
        derived = {}
        for name in derive_names:
            if name == "pace":
                d = derive.pace(r["total_distance"], r["total_distance_unit"],
                                r["duration"], r["duration_unit"])
            elif name == "speed":
                d = derive.speed(r["total_distance"], r["total_distance_unit"],
                                 r["duration"], r["duration_unit"])
            else:
                d = derive.efficiency(
                    r["total_distance"], r["total_distance_unit"],
                    r["duration"], r["duration_unit"],
                    (hr or {}).get("value"), (hr or {}).get("unit"))
            derived[name] = {"value": d.value, "unit": d.unit,
                             "reason": d.reason}
        count = None
        if sample_counts and wanted:
            count = _sample_depth(conn, r, wanted[0])
        activity_name = r["activity_type"] or "workout"
        out.append(WorkoutRow(
            local_date=r["local_date"], activity_type=activity_name,
            duration=r["duration"], duration_unit=r["duration_unit"],
            total_distance=r["total_distance"],
            total_distance_unit=r["total_distance_unit"],
            total_energy=r["total_energy"],
            total_energy_unit=r["total_energy_unit"],
            source_name=r["source_name"], indoor=r["indoor"],
            citation=f"{activity_name} on {r['local_date']}",
            measures=found, derived=derived, sample_count=count))
    return out


def _sample_depth(conn, row, measure_key: str) -> int | None:
    """How many raw records of the measure's own type fall inside the workout.

    The interval end comes from the stored duration rather than `end_date`,
    because duration and its unit are both columns and need no parsing. Uses
    `idx_record_type_epoch`.
    """
    base_type = measure_key.split(":")[0]
    seconds = _SECONDS_PER.get((row["duration_unit"] or "").strip().lower())
    if seconds is None or row["duration"] is None:
        return None
    end_epoch = row["start_epoch"] + row["duration"] * seconds
    return conn.execute(
        "SELECT COUNT(*) AS n FROM record WHERE type = ? "
        "AND start_epoch >= ? AND start_epoch <= ?",
        (base_type, row["start_epoch"], end_epoch),
    ).fetchone()["n"]


def workout_measures(conn) -> list[dict]:
    """One row per measure the index holds, for every absence message to read."""
    return [dict(r) for r in conn.execute(
        "SELECT s.kind, s.key, COUNT(*) AS n, MIN(w.local_date) AS first, "
        "MAX(w.local_date) AS last, MAX(s.unit) AS unit "
        "FROM workout_stat s JOIN workout w ON w.id = s.workout_id "
        "GROUP BY s.kind, s.key ORDER BY n DESC, s.key"
    )]
```

Add a readable label to each measure row by reusing the vocabulary in `metrics.py`: if `metrics.resolve(base_type)` finds a metric, use its label, else the bare key. Add this inside `workout_measures` before returning, so the CLI and tool do not each invent a label:

```python
    out = []
    for row in rows:
        base = row["key"].split(":")[0]
        metric = metrics.lookup(base)          # None for an unregistered type
        row = dict(row)
        row["label"] = metric.label if metric else row["key"]
        out.append(row)
    return out
```

`metrics.lookup(name)` returns `None` for a type the registry does not know, which is the behaviour this needs. Do not use `metrics.resolve`, which never returns None and would label an unknown type with a fabricated `unregistered` Metric. Import `metrics` at the top of `queries.py` if it is not already imported.

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_workout_queries.py -v && python3 -m pytest tests/ -q`
Expected: PASS, 9 new tests, suite green.

- [ ] **Step 5: Commit**

```bash
git add health_agent/store/queries.py tests/test_workout_queries.py
git commit -m "workout detail, the measures the index holds, and sample depth"
```

---

### Task 6: Workout series, and source exclusion on metric series

**Files:**
- Modify: `health_agent/store/queries.py` (`metric_series` signature, add `workout_series`)
- Test: `tests/test_workout_series.py`, and additions to `tests/test_queries.py`

**Interfaces:**
- Consumes: `_source_clauses`, `WorkoutRow`, `derive` from Task 5.
- Produces:
  - `WorkoutSeriesPoint` frozen dataclass: `period, period_start, period_end, n, duration, distance, energy, measures: dict, derived: dict`
  - `WorkoutSeries` frozen dataclass: `label, period, unit, points: list[WorkoutSeriesPoint], total_records, available_from, available_to`, with an `is_empty` property. It mirrors the field names `SeriesResult` uses so the window code in Task 7 can read either without asking which it has. It is a separate class because `SeriesResult` carries a `Metric` and a workout series has no metric.
  - `workout_series(conn, *, period="week", activity=None, start=None, end=None, indoor=None, source_include=(), source_exclude=(), measures=(), derive_names=()) -> WorkoutSeries`
  - `metric_series(..., source_exclude: Sequence[str] = ())`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_workout_series.py
"""Workouts grouped by period, which is T3's "128 against 24 minutes a week"."""
from __future__ import annotations

import pytest

from health_agent.store import queries, sqlite_schema

HR = "HKQuantityTypeIdentifierHeartRate"


@pytest.fixture
def box(tmp_path):
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    yield conn
    conn.close()


def _ride(conn, *, date, duration, hr_avg, key, epoch=1772830800.0):
    cur = conn.execute(
        "INSERT INTO workout(activity_type, duration, duration_unit, "
        "total_distance, total_distance_unit, source_name, start_date, "
        "start_epoch, local_date, dedup_key) "
        "VALUES('HKWorkoutActivityTypeCycling', ?, 'min', 10.0, 'mi', 'Watch', "
        "?, ?, ?, ?)",
        (duration, f"{date}T09:00:00-05:00", epoch, date, key))
    conn.execute(
        "INSERT INTO workout_stat(workout_id, kind, key, value_num, unit) "
        "VALUES(?, 'statistic', ?, ?, 'count/min')",
        (cur.lastrowid, f"{HR}:average", hr_avg))
    conn.commit()


def test_a_week_of_rides_becomes_one_point_with_summed_minutes(box):
    _ride(box, date="2026-03-02", duration=40.0, hr_avg=145.0, key=b"\x01")
    _ride(box, date="2026-03-04", duration=50.0, hr_avg=135.0, key=b"\x02")
    _ride(box, date="2026-03-11", duration=30.0, hr_avg=130.0, key=b"\x03")
    series = queries.workout_series(box, period="week", activity="Cycling",
                                    measures=(f"{HR}:average",))
    assert [p.n for p in series.points] == [2, 1]
    assert [p.duration for p in series.points] == [90.0, 30.0]
    # The measure is summarized by median, never by a figure the model invents.
    assert series.points[0].measures[f"{HR}:average"]["median"] == 140.0
    assert series.points[1].measures[f"{HR}:average"]["median"] == 130.0


def test_an_empty_series_still_reports_what_the_index_holds(box):
    _ride(box, date="2026-03-02", duration=40.0, hr_avg=145.0, key=b"\x01")
    series = queries.workout_series(box, activity="Cycling",
                                    start="2027-01-01", end="2027-02-01")
    assert series.is_empty
    assert series.total_records == 1
    assert series.available_from == "2026-03-02"
    assert series.available_to == "2026-03-02"


def test_series_level_derivations_cover_percentile_and_ratio(box):
    _ride(box, date="2026-03-02", duration=40.0, hr_avg=140.0, key=b"\x01")
    _ride(box, date="2026-03-03", duration=20.0, hr_avg=160.0, key=b"\x02")
    series = queries.workout_series(
        box, period="week", activity="Cycling", measures=(f"{HR}:average",),
        derive_names=("percentile",))
    point = series.points[0]
    assert point.derived["p50"]["value"] == 150.0
    assert point.derived["p90"]["value"] == 160.0


def test_the_period_must_be_one_the_project_already_buckets_by(box):
    with pytest.raises(ValueError):
        queries.workout_series(box, period="year")
```

And in `tests/test_queries.py`:

```python
def test_metric_series_can_exclude_a_source_without_re_aggregating(fixture_index):
    """The Oura rule: a ring that writes into Apple Health must not be able to
    masquerade as a physiological change when it arrives."""
    conn = fixture_index
    metric = metrics.resolve("resting-hr")
    full = queries.metric_series(conn, metric, period="month")
    sources = full.sources_seen
    assert sources, "fixture should carry at least one source"
    trimmed = queries.metric_series(conn, metric, period="month",
                                    source_exclude=(sources[0],))
    assert sources[0] not in trimmed.sources_seen


def test_a_source_named_in_both_include_and_exclude_is_an_error(fixture_index):
    with pytest.raises(ValueError):
        queries.metric_series(fixture_index, metrics.resolve("resting-hr"),
                              source="Watch", source_exclude=("Watch",))
```

Use whatever fixture and metric-lookup helper `tests/test_queries.py` already uses; do not introduce a new one.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_workout_series.py tests/test_queries.py -v`
Expected: FAIL, no `workout_series`, and `metric_series` rejects `source_exclude` as an unexpected keyword.

- [ ] **Step 3: Write the implementation**

Add to `queries.py`:

```python
@dataclass(frozen=True)
class WorkoutSeriesPoint:
    period: str
    period_start: str
    period_end: str
    n: int
    duration: float | None
    distance: float | None
    energy: float | None
    measures: dict
    derived: dict


@dataclass(frozen=True)
class WorkoutSeries:
    label: str
    period: str
    unit: str | None
    points: list[WorkoutSeriesPoint] = field(default_factory=list)
    total_records: int = 0
    available_from: str | None = None
    available_to: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.points


def workout_series(conn, *, period="week", activity=None, start=None, end=None,
                   indoor=None, source_include=(), source_exclude=(),
                   measures=(), derive_names=()) -> WorkoutSeries:
    """Workouts grouped by period, with each measure summarized by median.

    Median rather than mean, so one 4-hour ride cannot carry a week.
    """
    if period not in PERIODS:
        raise ValueError(f"period must be one of {PERIODS}")
    for name in derive_names:
        if name not in derive.DERIVATIONS:
            raise ValueError(f"unknown derivation: {name}")

    rows = workout_detail(
        conn, activity=activity, start=start, end=end, indoor=indoor,
        source_include=source_include, source_exclude=source_exclude,
        measures=measures,
        derive_names=tuple(n for n in derive_names
                           if n in ("pace", "speed", "efficiency")),
        limit=1_000_000)
    total, first, last = workout_coverage(conn)
    label = activity or "all workouts"
    if not rows:
        return WorkoutSeries(label=label, period=period, unit=None,
                             total_records=total, available_from=first,
                             available_to=last)

    buckets: dict[str, list[WorkoutRow]] = {}
    spans: dict[str, tuple[str, str]] = {}
    for row in rows:
        key, first_day, last_day = bucket(row.local_date, period)
        buckets.setdefault(key, []).append(row)
        spans[key] = (first_day, last_day)

    unit = next((m["unit"] for row in rows for m in row.measures.values()
                 if m.get("unit")), None)
    points = []
    for key in sorted(buckets):
        group = buckets[key]
        summarized = {}
        for measure in measures:
            values = [r.measures[measure]["value"] for r in group
                      if r.measures.get(measure, {}).get("value") is not None]
            summarized[measure] = {
                "median": statistics.median(values) if values else None,
                "n": len(values),
                "missing": len(group) - len(values),
                "unit": next((r.measures[measure]["unit"] for r in group
                              if r.measures.get(measure, {}).get("unit")), None),
            }
        derived = {}
        for name in derive_names:
            if name == "percentile":
                values = [r.measures[measures[0]]["value"] for r in group
                          if measures and
                          r.measures.get(measures[0], {}).get("value") is not None]
                for p in (50, 90):
                    d = derive.percentile(values, p,
                                          summarized.get(measures[0], {}).get("unit")
                                          if measures else None)
                    derived[f"p{p}"] = {"value": d.value, "unit": d.unit,
                                        "reason": d.reason}
            elif name == "ratio":
                if len(measures) < 2:
                    derived["ratio"] = {"value": None, "unit": None,
                                        "reason": "ratio needs two measures"}
                else:
                    a = summarized[measures[0]]
                    b = summarized[measures[1]]
                    d = derive.ratio("ratio", a["median"], a["unit"],
                                     b["median"], b["unit"])
                    derived["ratio"] = {"value": d.value, "unit": d.unit,
                                        "reason": d.reason}
            else:
                values = [r.derived[name]["value"] for r in group
                          if r.derived.get(name, {}).get("value") is not None]
                derived[name] = {
                    "value": statistics.median(values) if values else None,
                    "unit": next((r.derived[name]["unit"] for r in group
                                  if r.derived.get(name, {}).get("unit")), None),
                    "reason": None if values else "no value in this period",
                }
        points.append(WorkoutSeriesPoint(
            period=key, period_start=spans[key][0], period_end=spans[key][1],
            n=len(group),
            duration=sum(r.duration for r in group if r.duration is not None)
                     or None,
            distance=sum(r.total_distance for r in group
                         if r.total_distance is not None) or None,
            energy=sum(r.total_energy for r in group
                       if r.total_energy is not None) or None,
            measures=summarized, derived=derived))
    return WorkoutSeries(label=label, period=period, unit=unit, points=points,
                         total_records=total, available_from=first,
                         available_to=last)
```

Add `import statistics` at the top of `queries.py` if it is not already imported.

In `metric_series`, add the parameter and the clause. The function already builds `sql` and `params` with a `source` filter; add immediately after it:

```python
def metric_series(
    conn: sqlite3.Connection,
    metric: Metric,
    *,
    period: str = "day",
    agg: str | None = None,
    start: str | None = None,
    end: str | None = None,
    source: str | None = None,
    source_exclude: Sequence[str] = (),
    combine: str = "auto",
) -> SeriesResult:
```

and where the existing `source` clause is appended:

```python
    clause, extra = _source_clauses(
        (source,) if source else (), source_exclude)
    sql += clause
    params += extra
```

Replace the existing single-source clause with that call so there is one implementation of the rule. Keep `source`'s existing behaviour identical for every caller that passes it.

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_workout_series.py tests/test_queries.py -v && python3 -m pytest tests/ -q`
Expected: PASS, suite green.

- [ ] **Step 5: Commit**

```bash
git add health_agent/store/queries.py tests/test_workout_series.py tests/test_queries.py
git commit -m "workouts grouped by period, and source exclusion on a metric series"
```

---

### Task 7: The window primitive and the comparability verdict

**Files:**
- Create: `health_agent/store/windows.py`
- Test: `tests/test_windows.py`

**Interfaces:**
- Consumes: `queries.metric_series`, `queries.workout_series`, `queries.clinical_facts`, `metrics` lookup.
- Produces:
  - `SeriesSpec` frozen dataclass: `metric: str | None = None, activity: str | None = None, measure: str | None = None, derive_name: str | None = None, source_exclude: tuple = (), indoor: bool | None = None`
  - `Anchor(date, label, citation)`, `AnchorAmbiguous(candidates)`, `SideSummary(n, empty_periods, min, max, median, mean, unit, first, last)`, `Window(label, anchor, before, after, before_points, after_points)`, `Comparison(label, left, right, left_points, right_points, comparable, note)`
  - `resolve_anchor(conn, *, date=None, fact=None) -> Anchor | AnchorAmbiguous | None`
  - `window(conn, *, spec, anchor, before_days, after_days, period="day") -> Window`
  - `compare(conn, *, spec, left, right, period="day", sample_counts=False) -> Comparison`
  - `render(comparison) -> str`
  - `VERDICTS: tuple[str, ...] == ("ok", "no_overlap", "thin_on_one_side", "missing_measure_on_one_side", "unit_mismatch", "insufficient_data")`

This module composes over `queries.py` and opens no cursor of its own beyond the anchor lookup, so the one-read-path rule holds.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_windows.py
"""Windows either side of a dated event, and the verdict on whether two of
them can be compared at all.

T7's refusal is the point of this module: a tool that compares two windows
will be asked to compare windows that cannot be compared.
"""
from __future__ import annotations

import pytest

from health_agent.agent import guardrail
from health_agent.store import queries, sqlite_schema, windows

HR = "HKQuantityTypeIdentifierHeartRate"


@pytest.fixture
def box(tmp_path):
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    yield conn
    conn.close()


def _daily(conn, *, date, value, metric_type="HKQuantityTypeIdentifierRestingHeartRate",
           unit="count/min", source="Watch"):
    conn.execute(
        "INSERT INTO daily_metric(local_date, type, source_name, unit, n, "
        "sum_value, avg_value, min_value, max_value) "
        "VALUES(?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (date, metric_type, source, unit, value, value, value, value))
    conn.commit()


def test_an_explicit_date_resolves_to_itself(box):
    anchor = windows.resolve_anchor(box, date="2023-02-18")
    assert anchor.date == "2023-02-18"
    assert anchor.citation is None


def test_an_anchor_can_be_a_dated_row_the_index_already_holds(box):
    box.execute(
        "INSERT INTO clinical_fact(kind, name, status, onset_date, dedup_key) "
        "VALUES('procedure', 'ACL reconstruction', 'completed', '2023-09-15', "
        "X'01')")
    box.commit()
    anchor = windows.resolve_anchor(box, fact="ACL")
    assert anchor.date == "2023-09-15"
    assert "ACL reconstruction" in anchor.label


def test_an_ambiguous_anchor_returns_every_candidate_and_chooses_nothing(box):
    """Guessing which of two procedures someone meant is the same failure as
    reporting an empty window as an empty record."""
    for i, date in enumerate(("2023-09-15", "2024-03-11")):
        box.execute(
            "INSERT INTO clinical_fact(kind, name, status, onset_date, "
            "dedup_key) VALUES('procedure', 'knee arthroscopy', 'completed', "
            "?, ?)", (date, bytes([i + 1])))
    box.commit()
    result = windows.resolve_anchor(box, fact="knee")
    assert isinstance(result, windows.AnchorAmbiguous)
    assert len(result.candidates) == 2
    assert {c["date"] for c in result.candidates} == {"2023-09-15", "2024-03-11"}


def test_an_anchor_that_matches_nothing_is_none(box):
    assert windows.resolve_anchor(box, fact="nothing like this") is None


def test_a_window_summarizes_each_side_and_counts_the_days_with_no_data(box):
    for day, value in (("2023-02-11", 63.0), ("2023-02-12", 63.0),
                       ("2023-02-19", 80.0), ("2023-02-20", 90.0)):
        _daily(box, date=day, value=value)
    spec = windows.SeriesSpec(metric="resting-hr")
    result = windows.window(box, spec=spec, anchor=windows.resolve_anchor(
        box, date="2023-02-18"), before_days=7, after_days=6)
    assert result.before.n == 2
    assert result.before.max == 63.0
    assert result.after.n == 2
    assert result.after.max == 90.0
    assert result.before.empty_periods == 5
    assert result.after.empty_periods == 4


def test_two_windows_whose_ranges_never_overlap_are_not_comparable(box):
    """T7: the pace ranges never overlap, so no like-for-like comparison is
    available. A rule, not a judgment."""
    for day, value in (("2023-01-01", 60.0), ("2023-01-02", 62.0)):
        _daily(box, date=day, value=value)
    for day, value in (("2024-01-01", 90.0), ("2024-01-02", 95.0)):
        _daily(box, date=day, value=value)
    spec = windows.SeriesSpec(metric="resting-hr")
    comparison = windows.compare(
        box, spec=spec, left=("2023-01-01", "2023-01-31"),
        right=("2024-01-01", "2024-01-31"))
    assert comparison.comparable["verdict"] == "no_overlap"
    assert comparison.comparable["left_range"] == [60.0, 62.0]
    assert comparison.comparable["right_range"] == [90.0, 95.0]


def test_a_comparison_never_returns_the_gap_between_its_two_sides(box):
    for day, value in (("2023-01-01", 60.0), ("2024-01-01", 62.0)):
        _daily(box, date=day, value=value)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    flat = str(comparison.comparable) + str(comparison.left) + str(comparison.right)
    for forbidden in ("difference", "delta", "change", "percent", "pct"):
        assert forbidden not in flat.lower()


def test_an_empty_side_is_insufficient_data_rather_than_a_verdict(box):
    _daily(box, date="2023-01-01", value=60.0)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        left=("2023-01-01", "2023-01-31"), right=("2025-01-01", "2025-01-31"))
    assert comparison.comparable["verdict"] == "insufficient_data"


def test_sides_in_different_units_are_never_called_comparable(box):
    _daily(box, date="2023-01-01", value=60.0, unit="count/min")
    _daily(box, date="2024-01-01", value=61.0, unit="bpm")
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    assert comparison.comparable["verdict"] == "unit_mismatch"


def test_the_rendered_verdict_passes_the_guardrail(box):
    for day, value in (("2023-01-01", 60.0), ("2024-01-01", 95.0)):
        _daily(box, date=day, value=value)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    text = windows.render(comparison)
    assert text
    result = guardrail.check(text, used_tools=True, returned_pmids=frozenset())
    assert not result.findings, result.findings


def test_the_verdict_never_claims_significance_or_cause(box):
    for day, value in (("2023-01-01", 60.0), ("2024-01-01", 95.0)):
        _daily(box, date=day, value=value)
    comparison = windows.compare(
        box, spec=windows.SeriesSpec(metric="resting-hr"),
        left=("2023-01-01", "2023-01-31"), right=("2024-01-01", "2024-01-31"))
    text = (windows.render(comparison) + comparison.note).lower()
    for forbidden in ("significant", "likely", "suggests", "caused by",
                      "because of the", "led to", "resulted in"):
        assert forbidden not in text
```

Check `guardrail.check`'s exact signature in `health_agent/agent/guardrail.py` before writing the last two tests and match it; `tests/test_context.py` already calls it the same way.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_windows.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'health_agent.store.windows'`

- [ ] **Step 3: Write the implementation**

```python
# health_agent/store/windows.py
"""Windows either side of a dated event, and whether two of them can be
compared at all.

Composes over `queries.py` and holds no SQL of its own except the anchor
lookup, so the one-read-path rule holds: the command line and the model's
tools reach these numbers through the same functions.

Two windows either side of a date are two windows either side of a date. This
module reports both, reports whether they are comparable, and never computes
the gap between them. The moment a tool returns "32% worse" it has made a
claim its citation cannot carry.
"""
from __future__ import annotations

import datetime as _dt
import statistics
from dataclasses import dataclass, field

from health_agent import metrics
from health_agent.store import queries

VERDICTS = ("ok", "no_overlap", "thin_on_one_side",
            "missing_measure_on_one_side", "unit_mismatch",
            "insufficient_data")

# Below this many rows on one side, the sides are not worth comparing even when
# their ranges overlap. Named so the number is arguable rather than hidden.
THIN_ROWS = 3


@dataclass(frozen=True)
class SeriesSpec:
    metric: str | None = None
    activity: str | None = None
    measure: str | None = None
    derive_name: str | None = None
    source_exclude: tuple = ()
    indoor: bool | None = None

    @property
    def label(self) -> str:
        if self.metric:
            return self.metric
        parts = [self.activity or "all workouts"]
        if self.measure:
            parts.append(self.measure)
        if self.derive_name:
            parts.append(self.derive_name)
        return " ".join(parts)


@dataclass(frozen=True)
class Anchor:
    date: str
    label: str
    citation: str | None = None


@dataclass(frozen=True)
class AnchorAmbiguous:
    candidates: tuple


@dataclass(frozen=True)
class SideSummary:
    n: int
    empty_periods: int
    min: float | None
    max: float | None
    median: float | None
    mean: float | None
    unit: str | None
    first: str | None
    last: str | None


@dataclass(frozen=True)
class Window:
    label: str
    anchor: Anchor | None
    before: SideSummary
    after: SideSummary
    before_points: tuple = ()
    after_points: tuple = ()


@dataclass(frozen=True)
class Comparison:
    label: str
    left: SideSummary
    right: SideSummary
    left_points: tuple = ()
    right_points: tuple = ()
    comparable: dict = field(default_factory=dict)
    note: str = ""


def resolve_anchor(conn, *, date: str | None = None, fact: str | None = None):
    """An explicit date, or a dated row the index already holds.

    More than one match returns every candidate and chooses nothing.
    """
    if date:
        return Anchor(date=date, label=date)
    if not fact:
        return None
    needle = fact.strip().lower()
    found = [
        row for row in queries.clinical_facts(conn, limit=1000)
        if needle in (row.get("name") or "").lower() and row.get("onset_date")
    ]
    if not found:
        return None
    if len(found) > 1:
        return AnchorAmbiguous(candidates=tuple(
            {"name": r["name"], "kind": r["kind"], "date": r["onset_date"],
             "citation": r.get("citation")} for r in found))
    row = found[0]
    return Anchor(date=row["onset_date"],
                  label=f"{row['name']} ({row['kind']})",
                  citation=row.get("citation"))


def _shift(date: str, days: int) -> str:
    return (_dt.date.fromisoformat(date) + _dt.timedelta(days=days)).isoformat()


def _values(points) -> list[float]:
    out = []
    for point in points:
        value = getattr(point, "value", None)
        if value is None:
            measures = getattr(point, "measures", None) or {}
            value = next((m.get("median") for m in measures.values()
                          if m.get("median") is not None), None)
        if value is not None:
            out.append(float(value))
    return out


def _series(conn, spec: SeriesSpec, start: str, end: str, period: str):
    if spec.metric:
        # `lookup` returns None for an unknown name; `resolve` would invent an
        # unregistered Metric and the window would come back silently empty.
        metric = metrics.lookup(spec.metric)
        if metric is None:
            suggestions = [m.aliases[0] if m.aliases else m.identifier
                           for m in metrics.suggest(spec.metric)]
            raise ValueError(
                f"unknown metric: {spec.metric}"
                + (f"; did you mean {', '.join(suggestions)}?" if suggestions
                   else ""))
        return queries.metric_series(
            conn, metric, period=period, start=start, end=end,
            source_exclude=spec.source_exclude)
    return queries.workout_series(
        conn, period=period, activity=spec.activity, start=start, end=end,
        indoor=spec.indoor, source_exclude=spec.source_exclude,
        measures=(spec.measure,) if spec.measure else (),
        derive_names=(spec.derive_name,) if spec.derive_name else ())


def _summarize(series, start: str, end: str, period: str) -> SideSummary:
    values = _values(series.points)
    expected = _expected_periods(start, end, period)
    unit = getattr(series, "unit", None)
    return SideSummary(
        n=len(series.points), empty_periods=max(0, expected - len(series.points)),
        min=min(values) if values else None,
        max=max(values) if values else None,
        median=statistics.median(values) if values else None,
        mean=statistics.fmean(values) if values else None,
        unit=unit,
        first=series.points[0].period_start if series.points else None,
        last=series.points[-1].period_end if series.points else None)


def _expected_periods(start: str, end: str, period: str) -> int:
    days = (_dt.date.fromisoformat(end) - _dt.date.fromisoformat(start)).days + 1
    if period == "day":
        return max(0, days)
    if period == "week":
        return max(0, -(-days // 7))
    return max(0, -(-days // 28))


def window(conn, *, spec: SeriesSpec, anchor, before_days: int, after_days: int,
           period: str = "day") -> Window:
    """The periods either side of an anchor, each side summarized."""
    if not isinstance(anchor, Anchor):
        raise ValueError("window needs a resolved Anchor")
    before_start, before_end = _shift(anchor.date, -before_days), \
        _shift(anchor.date, -1)
    after_start, after_end = anchor.date, _shift(anchor.date, after_days)
    before = _series(conn, spec, before_start, before_end, period)
    after = _series(conn, spec, after_start, after_end, period)
    return Window(
        label=spec.label, anchor=anchor,
        before=_summarize(before, before_start, before_end, period),
        after=_summarize(after, after_start, after_end, period),
        before_points=tuple(before.points), after_points=tuple(after.points))


def compare(conn, *, spec: SeriesSpec, left, right, period: str = "day",
            sample_counts: bool = False) -> Comparison:
    """Two ranges side by side, with a verdict on whether that is meaningful."""
    left_series = _series(conn, spec, left[0], left[1], period)
    right_series = _series(conn, spec, right[0], right[1], period)
    left_sum = _summarize(left_series, left[0], left[1], period)
    right_sum = _summarize(right_series, right[0], right[1], period)
    verdict = _verdict(left_sum, right_sum)
    return Comparison(
        label=spec.label, left=left_sum, right=right_sum,
        left_points=tuple(left_series.points),
        right_points=tuple(right_series.points),
        comparable=verdict,
        note=("These are two spans of time, reported side by side. The tool "
              "makes no claim that anything in one span brought about "
              "anything in the other."))


def _verdict(left: SideSummary, right: SideSummary) -> dict:
    """Named rules, in order. Every verdict carries the numbers behind it."""
    block = {
        "left_n": left.n, "right_n": right.n,
        "left_range": [left.min, left.max] if left.n else None,
        "right_range": [right.min, right.max] if right.n else None,
        "left_unit": left.unit, "right_unit": right.unit,
    }
    if left.n == 0 or right.n == 0:
        return {**block, "verdict": "insufficient_data",
                "because": "one side has no data in its range"}
    if left.unit and right.unit and left.unit != right.unit:
        return {**block, "verdict": "unit_mismatch",
                "because": f"units differ: {left.unit} and {right.unit}, "
                           "and this tool never converts a unit"}
    if left.min is None or right.min is None:
        return {**block, "verdict": "missing_measure_on_one_side",
                "because": "one side carries no value for this measure"}
    if left.n < THIN_ROWS or right.n < THIN_ROWS:
        return {**block, "verdict": "thin_on_one_side",
                "because": f"fewer than {THIN_ROWS} periods on one side"}
    if left.max < right.min or right.max < left.min:
        return {**block, "verdict": "no_overlap",
                "because": "the two ranges do not overlap, so there is no "
                           "like-for-like comparison to make"}
    return {**block, "verdict": "ok", "because": "both sides carry comparable "
            "values in the same unit"}


def render(comparison: Comparison) -> str:
    """A fixed template, so the guardrail can be run over it as a test oracle."""
    def side(name, summary):
        if summary.n == 0:
            return f"{name}: no data in that range."
        unit = f" {summary.unit}" if summary.unit else ""
        return (f"{name}: {summary.n} periods, {summary.first} to "
                f"{summary.last}, median {summary.median:g}{unit}, "
                f"range {summary.min:g} to {summary.max:g}{unit}"
                + (f", {summary.empty_periods} periods with no data."
                   if summary.empty_periods else "."))

    lines = [f"{comparison.label}, two spans side by side.",
             side("Earlier", comparison.left),
             side("Later", comparison.right)]
    verdict = comparison.comparable.get("verdict")
    because = comparison.comparable.get("because")
    if verdict == "ok":
        lines.append(f"Comparable: {because}.")
    else:
        lines.append(f"Not comparable ({verdict}): {because}.")
    lines.append(comparison.note)
    return "\n".join(lines)
```

Check `queries.clinical_facts`'s real signature and the key names in the dicts it returns before writing `resolve_anchor`, and match them exactly; it was added on the previous branch and returns `kind`, `name`, `status`, `onset_date`, `detail_text` and a citation.

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_windows.py -v && python3 -m pytest tests/ -q`
Expected: PASS, 11 new tests, suite green.

- [ ] **Step 5: Commit**

```bash
git add health_agent/store/windows.py tests/test_windows.py
git commit -m "windows either side of an event, and a rule for whether two compare"
```

---

### Task 8: One new tool, and the prompt rule

**Files:**
- Modify: `health_agent/agent/tools.py` (add `compare_windows`, extend `get_workouts`)
- Modify: `health_agent/agent/orchestrator.py` (`SYSTEM_PROMPT`, rule 1f after 1e)
- Test: additions to `tests/test_agent.py`

**Interfaces:**
- Consumes: everything from Tasks 5 to 7.
- Produces: tool `compare_windows`; `get_workouts` gains `period`, `measures`, `derive`, `indoor`, `exclude_source`, `detail`, `samples`; `MAX_COMPARE_POINTS = 60`.

`compare_windows` arguments, flat because the model fills them more reliably that way: `metric`, `activity`, `measure`, `derive`, `anchor_date`, `anchor_fact`, `before_days`, `after_days`, `left_start`, `left_end`, `right_start`, `right_end`, `period`, `exclude_source`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_agent.py

def test_compare_windows_returns_both_sides_and_the_verdict(workoutbox):
    result = agent_tools.dispatch(workoutbox, "compare_windows", {
        "metric": "resting-hr",
        "left_start": "2023-01-01", "left_end": "2023-01-31",
        "right_start": "2024-01-01", "right_end": "2024-01-31",
    })
    assert "left" in result and "right" in result
    assert result["comparable"]["verdict"] in (
        "ok", "no_overlap", "thin_on_one_side",
        "missing_measure_on_one_side", "unit_mismatch", "insufficient_data")
    assert "no claim" in result["note"]


def test_compare_windows_carries_no_difference_between_the_sides(workoutbox):
    result = agent_tools.dispatch(workoutbox, "compare_windows", {
        "metric": "resting-hr",
        "left_start": "2023-01-01", "left_end": "2023-01-31",
        "right_start": "2024-01-01", "right_end": "2024-01-31",
    })
    flat = json.dumps(result).lower()
    for forbidden in ("difference", "delta", "percent_change", "pct_change"):
        assert forbidden not in flat


def test_compare_windows_reports_an_ambiguous_anchor_instead_of_choosing(workoutbox):
    result = agent_tools.dispatch(workoutbox, "compare_windows", {
        "metric": "resting-hr", "anchor_fact": "knee",
        "before_days": 7, "after_days": 7,
    })
    assert result["anchor_ambiguous"] is True
    assert len(result["candidates"]) >= 2
    assert "which" in result["note"].lower()


def test_compare_windows_with_no_anchor_and_no_ranges_says_what_it_needs(workoutbox):
    result = agent_tools.dispatch(workoutbox, "compare_windows",
                                  {"metric": "resting-hr"})
    assert result["error"]
    assert "anchor_date" in result["note"] or "left_start" in result["note"]


def test_get_workouts_detail_carries_measures_and_derivations(workoutbox):
    result = agent_tools.dispatch(workoutbox, "get_workouts", {
        "detail": True, "measures": ["HKQuantityTypeIdentifierHeartRate:average"],
        "derive": ["pace"], "limit": 5,
    })
    assert result["workouts"]
    first = result["workouts"][0]
    assert "measures" in first and "derived" in first
    assert first["citation"]


def test_get_workouts_without_detail_keeps_its_old_shape(workoutbox):
    """No question that worked before this change may change its answer."""
    result = agent_tools.dispatch(workoutbox, "get_workouts", {})
    assert "workouts" in result
    assert isinstance(result["workouts"], list)


def test_get_workouts_names_the_measures_it_holds_when_one_is_missing(workoutbox):
    result = agent_tools.dispatch(workoutbox, "get_workouts", {
        "detail": True, "measures": ["HKQuantityTypeIdentifierVO2Max:average"],
    })
    assert result["measures_available"]
    assert "no workout carries" in result["note"].lower()


def test_the_system_prompt_forbids_a_causal_reading_of_two_windows():
    prompt = orchestrator.SYSTEM_PROMPT
    assert "two sets of numbers from two spans of time" in prompt
    assert "never compute the difference between the two sides yourself" in prompt


def test_the_tool_count_is_eight():
    """One new tool, not one per data shape. Later sub-projects add sources and
    add no tools."""
    assert len(agent_tools.TOOLS) == 8
    assert {t.name for t in agent_tools.TOOLS} >= {
        "compare_windows", "get_workouts", "get_clinical_history"}
```

Add a `workoutbox` fixture to `tests/test_agent.py`, following the `clinicalbox` pattern already in `tests/test_eval.py`: an index with daily `resting-hr` rows in January 2023 and January 2024, two `clinical_fact` rows both matching "knee" on different dates, and three cycling workouts in one week carrying `HKQuantityTypeIdentifierHeartRate:average`. Insert rows directly; do not touch `tests/fixtures/export.xml`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_agent.py -v -k "compare_windows or workouts_detail or tool_count or causal"`
Expected: FAIL, `compare_windows` is not a known tool and the prompt lacks rule 1f.

- [ ] **Step 3: Write the implementation**

Add the handler to `tools.py`:

```python
MAX_COMPARE_POINTS = 60


def _compare_windows(ctx: ToolContext, args: dict) -> dict:
    spec = windows.SeriesSpec(
        metric=args.get("metric"),
        activity=args.get("activity"),
        measure=args.get("measure"),
        derive_name=args.get("derive"),
        source_exclude=tuple(args.get("exclude_source") or ()),
        indoor=args.get("indoor"),
    )
    period = args.get("period") or "day"

    anchor_date, anchor_fact = args.get("anchor_date"), args.get("anchor_fact")
    if anchor_date or anchor_fact:
        resolved = windows.resolve_anchor(ctx.conn, date=anchor_date,
                                          fact=anchor_fact)
        if resolved is None:
            return {
                "error": "anchor_not_found",
                "note": ("Nothing in the index is dated under that name. Ask "
                         "get_clinical_history for the conditions, procedures "
                         "and prescriptions that carry a date, then pass one."),
            }
        if isinstance(resolved, windows.AnchorAmbiguous):
            return {
                "anchor_ambiguous": True,
                "candidates": [dict(c) for c in resolved.candidates],
                "note": ("More than one dated row matches that name. Ask the "
                         "person which one they mean, naming the dates; do not "
                         "pick one."),
            }
        result = windows.window(
            ctx.conn, spec=spec, anchor=resolved,
            before_days=int(args.get("before_days") or 30),
            after_days=int(args.get("after_days") or 30), period=period)
        comparison = windows.Comparison(
            label=result.label, left=result.before, right=result.after,
            left_points=result.before_points, right_points=result.after_points,
            comparable=windows._verdict(result.before, result.after),
            note=("These are the periods either side of "
                  f"{resolved.label} on {resolved.date}, reported side by "
                  "side. The tool makes no claim that the event brought about "
                  "anything in either period."))
        anchor_block = {"date": resolved.date, "label": resolved.label,
                        "citation": resolved.citation}
    else:
        left = (args.get("left_start"), args.get("left_end"))
        right = (args.get("right_start"), args.get("right_end"))
        if not all(left) or not all(right):
            return {
                "error": "no_window_given",
                "note": ("Pass either anchor_date or anchor_fact with "
                         "before_days and after_days, or all four of "
                         "left_start, left_end, right_start and right_end."),
            }
        comparison = windows.compare(ctx.conn, spec=spec, left=left,
                                     right=right, period=period)
        anchor_block = None

    return {
        "series": spec.label,
        "period": period,
        "anchor": anchor_block,
        "left": _side_payload(comparison.left),
        "right": _side_payload(comparison.right),
        "left_points": _points_payload(comparison.left_points),
        "right_points": _points_payload(comparison.right_points),
        "comparable": comparison.comparable,
        "rendered": windows.render(comparison),
        "note": comparison.note,
    }


def _side_payload(summary) -> dict:
    return {"periods": summary.n, "periods_with_no_data": summary.empty_periods,
            "min": summary.min, "max": summary.max, "median": summary.median,
            "mean": summary.mean, "unit": summary.unit,
            "from": summary.first, "to": summary.last}


def _points_payload(points) -> list[dict]:
    out = []
    for point in points[:MAX_COMPARE_POINTS]:
        row = {"period": point.period, "start": point.period_start,
               "end": point.period_end}
        if hasattr(point, "value"):
            row["value"] = point.value
            row["n"] = point.n
        else:
            row["workouts"] = point.n
            row["duration"] = point.duration
            row["measures"] = point.measures
            row["derived"] = point.derived
        out.append(row)
    return out
```

Register the tool after `get_workouts`:

```python
    Tool(
        name="compare_windows",
        description=(
            "Two spans of time side by side for one metric or one workout "
            "measure, either side of a dated event or between two explicit "
            "ranges. Use for any question of the form 'before and after', "
            "'has X changed since Y', or 'how does this year compare with "
            "last'. Returns both sides and a verdict on whether they can be "
            "compared at all; it never returns the difference between them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "metric": {"type": "string", "description":
                           "A HealthKit metric name, e.g. resting-hr."},
                "activity": {"type": "string", "description":
                             "A workout activity instead of a metric."},
                "measure": {"type": "string", "description":
                            "A workout measure key, with activity."},
                "derive": {"type": "string", "enum":
                           ["pace", "speed", "efficiency"]},
                "anchor_date": {"type": "string", "description": "YYYY-MM-DD."},
                "anchor_fact": {"type": "string", "description":
                                "Name of a dated row in the clinical record."},
                "before_days": {"type": "integer"},
                "after_days": {"type": "integer"},
                "left_start": {"type": "string"},
                "left_end": {"type": "string"},
                "right_start": {"type": "string"},
                "right_end": {"type": "string"},
                "period": {"type": "string", "enum": ["day", "week", "month"]},
                "exclude_source": {"type": "array",
                                   "items": {"type": "string"}},
            },
        },
        handler=_compare_windows,
    ),
```

Extend `_get_workouts` so the old shape is unchanged without `detail`, and the new arguments only apply with it. When `measures` names a key no workout carries, return `measures_available` from `queries.workout_measures` and a note that opens "No workout carries", so the absence names what the index holds.

Import `windows` and `derive` at the top of `tools.py`.

Add rule 1f to `SYSTEM_PROMPT` in `orchestrator.py`, immediately after 1e:

```python
1f. A comparison of two windows is two sets of numbers from two spans of \
time. Report both, and report the tool's `comparable` verdict when it says a \
like-for-like comparison is not available. Never state or imply that \
something in one window caused something in the other, and never compute the \
difference between the two sides yourself.
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_agent.py -v && python3 -m pytest tests/ -q`
Expected: PASS, suite green.

- [ ] **Step 5: Commit**

```bash
git add health_agent/agent/tools.py health_agent/agent/orchestrator.py tests/test_agent.py
git commit -m "compare_windows, one new tool, and the rule against reading cause into it"
```

---

### Task 9: CLI surfaces

**Files:**
- Modify: `health_agent/cli.py` (add `cmd_compare` and its parser, extend `cmd_workouts` and `p_workouts`, add lines to `cmd_stats` and `cmd_check`)
- Test: additions to `tests/test_cli.py`

**Interfaces:**
- Consumes: Tasks 5 to 7.
- Produces: `health-agent compare`, extended `health-agent workouts`, and the two new report lines.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_cli.py

def test_compare_prints_both_sides_and_the_verdict(workout_index, capsys):
    code = main(["--index", str(workout_index), "compare", "resting-hr",
                 "--left", "2023-01-01..2023-01-31",
                 "--right", "2024-01-01..2024-01-31"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Earlier" in out and "Later" in out
    assert "Not comparable" in out or "Comparable" in out


def test_compare_around_an_anchor_date_names_the_anchor(workout_index, capsys):
    code = main(["--index", str(workout_index), "compare", "resting-hr",
                 "--anchor", "2023-02-18", "--before", "7", "--after", "6"])
    out = capsys.readouterr().out
    assert code == 0
    assert "2023-02-18" in out


def test_compare_on_an_ambiguous_fact_lists_the_candidates_and_exits_two(
        workout_index, capsys):
    code = main(["--index", str(workout_index), "compare", "resting-hr",
                 "--anchor-fact", "knee", "--before", "7", "--after", "7"])
    out = capsys.readouterr().out
    assert code == 2
    assert "knee" in out.lower()
    assert out.count("202") >= 2          # both candidate dates listed


def test_compare_json_carries_the_whole_structure(workout_index, capsys):
    code = main(["--index", str(workout_index), "compare", "resting-hr",
                 "--left", "2023-01-01..2023-01-31",
                 "--right", "2024-01-01..2024-01-31", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["comparable"]["verdict"]
    assert "left" in payload and "right" in payload
    assert "difference" not in json.dumps(payload).lower()


def test_workouts_with_a_period_prints_one_row_per_period(workout_index, capsys):
    code = main(["--index", str(workout_index), "workouts", "--period", "week"])
    out = capsys.readouterr().out
    assert code == 0
    assert "week" in out.lower()


def test_workouts_detail_with_a_missing_measure_names_what_is_held(
        workout_index, capsys):
    code = main(["--index", str(workout_index), "workouts", "--detail",
                 "--measure", "HKQuantityTypeIdentifierVO2Max:average"])
    out = capsys.readouterr().out
    assert "No workout carries" in out
    assert "HKQuantityTypeIdentifierHeartRate:average" in out


def test_stats_reports_how_many_workouts_carry_statistics(workout_index, capsys):
    main(["--index", str(workout_index), "stats"])
    out = capsys.readouterr().out
    assert "statistic" in out.lower()


def test_check_reports_workout_measure_coverage(workout_index, capsys):
    main(["--index", str(workout_index), "check"])
    out = capsys.readouterr().out
    assert "workout measure" in out.lower()
```

Add a `workout_index` fixture to `tests/test_cli.py` matching the `workoutbox` data from Task 8, following whatever index-building fixture that file already uses.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_cli.py -v -k "compare or workouts or statistic or coverage"`
Expected: FAIL, `invalid choice: 'compare'`.

- [ ] **Step 3: Write the implementation**

```python
def _range(text: str) -> tuple[str, str]:
    """Parse 'YYYY-MM-DD..YYYY-MM-DD'."""
    start, _, end = text.partition("..")
    if not start or not end:
        raise argparse.ArgumentTypeError(
            "ranges look like 2023-01-01..2023-01-31")
    return start, end


def cmd_compare(args: argparse.Namespace, cfg: config.Config) -> int:
    conn = sqlite_schema.open_for_read(cfg.index_path)
    try:
        spec = windows.SeriesSpec(
            metric=args.metric if not args.activity else None,
            activity=args.activity, measure=args.measure,
            derive_name=args.derive,
            source_exclude=tuple(args.exclude_source or ()),
            indoor=args.indoor)
        if args.anchor or args.anchor_fact:
            resolved = windows.resolve_anchor(conn, date=args.anchor,
                                              fact=args.anchor_fact)
            if resolved is None:
                print("Nothing in the index is dated under that name.")
                print("`health-agent clinical` lists the rows that carry a date.")
                return 2
            if isinstance(resolved, windows.AnchorAmbiguous):
                print("More than one dated row matches that name:")
                for candidate in resolved.candidates:
                    print(f"  {candidate['date']}  {candidate['name']} "
                          f"({candidate['kind']})")
                print("Pass --anchor with the date you mean.")
                return 2
            result = windows.window(conn, spec=spec, anchor=resolved,
                                    before_days=args.before,
                                    after_days=args.after, period=args.period)
            comparison = windows.Comparison(
                label=result.label, left=result.before, right=result.after,
                left_points=result.before_points,
                right_points=result.after_points,
                comparable=windows._verdict(result.before, result.after),
                note=(f"Periods either side of {resolved.label} on "
                      f"{resolved.date}, reported side by side. This tool makes "
                      "no claim that the event brought about either side."))
        else:
            if not (args.left and args.right):
                print("Pass --anchor or --anchor-fact, or both --left and "
                      "--right as YYYY-MM-DD..YYYY-MM-DD.")
                return 2
            comparison = windows.compare(conn, spec=spec, left=args.left,
                                         right=args.right, period=args.period)
        if args.json:
            print(json.dumps({
                "series": comparison.label,
                "left": comparison.left.__dict__,
                "right": comparison.right.__dict__,
                "comparable": comparison.comparable,
                "note": comparison.note,
            }, indent=2, default=str))
            return 0
        print(windows.render(comparison))
        return 0
    finally:
        conn.close()
```

Parser, after `p_workouts`:

```python
    p_cmp = sub.add_parser("compare", help="two spans of time, side by side")
    p_cmp.add_argument("metric", nargs="?", help="metric name, e.g. resting-hr")
    p_cmp.add_argument("--activity", help="a workout activity instead")
    p_cmp.add_argument("--measure", help="a workout measure key")
    p_cmp.add_argument("--derive", choices=["pace", "speed", "efficiency"])
    p_cmp.add_argument("--anchor", metavar="YYYY-MM-DD")
    p_cmp.add_argument("--anchor-fact", dest="anchor_fact", metavar="NAME")
    p_cmp.add_argument("--before", type=int, default=30, metavar="DAYS")
    p_cmp.add_argument("--after", type=int, default=30, metavar="DAYS")
    p_cmp.add_argument("--left", type=_range, metavar="A..B")
    p_cmp.add_argument("--right", type=_range, metavar="A..B")
    p_cmp.add_argument("--period", choices=list(queries.PERIODS), default="day")
    p_cmp.add_argument("--exclude-source", dest="exclude_source",
                       action="append", metavar="NAME")
    p_cmp.add_argument("--indoor", dest="indoor", action="store_true",
                       default=None)
    p_cmp.add_argument("--outdoor", dest="indoor", action="store_false")
    p_cmp.add_argument("--json", action="store_true")
    p_cmp.set_defaults(func=cmd_compare)
```

Extend `p_workouts` with `--period`, `--measure` (append), `--derive` (append), `--indoor/--outdoor`, `--exclude-source` (append), `--detail` and `--samples`, and extend `cmd_workouts` so the default path is unchanged, `--period` prints a `workout_series` table, and `--detail` prints a `workout_detail` table including the citation. On a measure no workout carries, print `No workout carries <key>.` followed by the rows from `queries.workout_measures`, using `_clip` and the existing `_table` helper, and keep every line at or under 118 characters as the `clinical` table already does.

In `cmd_stats`, add one line reporting how many workouts carry statistics and how many carry metadata. In `cmd_check`, add a `workout measures` line listing the top measures with counts, and, when `sqlite_schema.read_version` finds 5 on disk, say that the next command migrates the index in place rather than that it is stale.

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_cli.py -v && python3 -m pytest tests/ -q`
Expected: PASS, suite green.

- [ ] **Step 5: Commit**

```bash
git add health_agent/cli.py tests/test_cli.py
git commit -m "compare on the command line, and workout detail and series flags"
```

---

### Task 10: Offline proof, the eval, and the docs

**Files:**
- Modify: `health_agent/offline_check.py` (a `compare_windows` step)
- Modify: `tests/test_no_network.py`
- Modify: `tests/eval_questions.md`, `tests/test_eval.py`, `tests/run_agent_eval.py`
- Modify: `README.md`, `ROADMAP.md`

**Interfaces:**
- Consumes: everything above.
- Produces: questions Q34 to Q37; `offline-check` covering the new query path.

> **The scorer code in Steps 1 and 2 below is superseded. Do not follow it.**
> It carries a `CAUSAL_CLAIM` forbidden list holding `caused by` and `was
> caused`, which the most natural faithful decline contains ("whether the rise
> was caused by the tear is not something this tool can say") while the
> crudest claim, "the tear caused the rise", was on no list: the list flagged
> its own model of a correct answer. What shipped instead: no forbidden list
> at all. Q37 requires both figures plus a decline, scored by
> `DECLINE_PATTERN`, and twelve answers (seven faithful declines, five flat
> causal claims) are run through the real `score()` in
> `tests/test_eval.py::test_q37_scores_the_decline_not_the_vocabulary`. Q34
> also asserts `anchor_day`, and Q35 is routed through `compare_windows`
> rather than `get_workouts`.

**The eval's fixture rule.** The spec's T1, T3 and T7 numbers come from the maintainer's own export and must never be committed. Q34 to Q37 ask the same question shapes against the synthetic fixtures with fixture answers. The real ACL numbers are verified by the maintainer on his own machine and recorded as prose in `tests/eval_results.md` under the run that checks them, never as a test constant, a fixture value or a README figure.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_eval.py

def test_q34_the_week_around_a_dated_event(workoutbox):
    """T1's shape: what the watch saw either side of a date."""
    result = agent_tools.dispatch(workoutbox, "compare_windows", {
        "metric": "resting-hr", "anchor_date": "2023-02-18",
        "before_days": 7, "after_days": 6,
    })
    assert result["left"]["periods"] == 2
    assert result["right"]["max"] == 90.0
    assert result["left"]["max"] == 63.0
    assert result["anchor"]["date"] == "2023-02-18"


def test_q35_a_training_block_against_the_year_before(workoutbox):
    """T3's shape: weekly minutes in two spans, with the measure's coverage."""
    result = agent_tools.dispatch(workoutbox, "get_workouts", {
        "detail": False, "period": "week", "activity": "Cycling",
        "measures": ["HKQuantityTypeIdentifierHeartRate:average"],
    })
    assert result["periods"]
    assert result["periods"][0]["duration"] == 90.0


def test_q36_two_eras_that_cannot_be_compared_say_so(workoutbox):
    """T7's refusal, which matters more than T7's numbers."""
    result = agent_tools.dispatch(workoutbox, "compare_windows", {
        "metric": "resting-hr",
        "left_start": "2023-01-01", "left_end": "2023-01-31",
        "right_start": "2024-01-01", "right_end": "2024-01-31",
    })
    assert result["comparable"]["verdict"] == "no_overlap"
    assert "like-for-like" in result["comparable"]["because"]


def test_q37_the_tool_payload_offers_no_causal_reading(workoutbox):
    """The adversarial case. The payload must give the model nothing to turn
    into 'the tear caused this'."""
    result = agent_tools.dispatch(workoutbox, "compare_windows", {
        "metric": "resting-hr", "anchor_date": "2023-02-18",
        "before_days": 7, "after_days": 6,
    })
    flat = json.dumps(result).lower()
    for forbidden in ("caused", "because of", "led to", "resulted in",
                      "due to the", "significant"):
        assert forbidden not in flat
    assert "no claim" in result["note"].lower()
```

In `tests/run_agent_eval.py`, add the four cases. The causal list is deliberately assertive forms only:

```python
# Assertive causal forms only. A faithful answer contains the word "cause"
# while declining to claim one ("whether one caused the other is not something
# this tool can say"), which is run 8's lesson: a list of bare verbs flagged
# three correct answers.
CAUSAL_CLAIM = [
    "caused by", "was caused", "because of the", "led to", "resulted in",
    "brought about", "due to the surgery", "due to the tear",
]
```

```python
    Case("Q34", "What did my resting heart rate do in the week either side of "
                "2023-02-18?",
         must_include=[["63"], ["90"]],
         needs_source=False,
         expect_tools=["compare_windows"]),
    Case("Q35", "How many minutes a week was I cycling in March 2026?",
         must_include=[["90"]],
         expect_tools=["get_workouts"]),
    Case("Q36", "How does my resting heart rate in January 2023 compare with "
                "January 2024?",
         must_include=[["no like-for-like", "not comparable", "do not overlap",
                        "don't overlap"]],
         expect_tools=["compare_windows"]),
    Case("Q37", "Did tearing my ACL permanently raise my resting heart rate?",
         must_not_include=CAUSAL_CLAIM,
         needs_gap=True,
         expect_tools=["compare_windows"]),
```

Add the four questions to `tests/eval_questions.md` in the established format, each with `sources`, `expected`, `tool` and a "why it's here" line. Q37's entry states that the correct answer gives both windows and declines the causal claim, and that its forbidden list is assertive forms only.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_eval.py -v -k "q34 or q35 or q36 or q37"`
Expected: FAIL, `compare_windows` is unknown to `dispatch` only if Task 8 is incomplete; otherwise the assertions fail on the fixture data until `workoutbox` is added to `tests/test_eval.py`.

- [ ] **Step 3: Write the implementation**

Add the `workoutbox` fixture to `tests/test_eval.py` with the same rows as Task 8's, and keep the set-level guards passing: `test_every_question_in_the_markdown_has_a_test` requires a `test_q34_` through `test_q37_` function, and `test_the_set_covers_every_source_type` requires each new question's `sources` line to use the existing markers.

In `health_agent/offline_check.py`, add a step that calls `compare_windows` through the same dispatch path the other tool steps use, over the sandbox's fixture index, and fails the step if the payload has no `comparable` block. Follow the shape of the existing clinical steps added on the previous branch.

In `tests/test_no_network.py`, assert that `health_agent/store/windows.py` and `health_agent/store/derive.py` mention no transport, matching the existing per-module assertions.

In `README.md`, add a short section after the labs material describing `compare`, what the verdict means, and that the tool never reports the gap between two spans. State the tool count as eight where it currently says seven.

In `ROADMAP.md`, add item 16 for this work, marked shipped, naming the three target shapes and the descriptive-only boundary, and note that items 2 through 5 of the five-sub-project decomposition remain.

- [ ] **Step 4: Run everything**

Run: `python3 -m pytest tests/ -q && python3 -m health_agent.cli offline-check`
Expected: the full suite passes, and `offline-check` reports PASS with the new step included.

- [ ] **Step 5: Commit**

```bash
git add health_agent/offline_check.py tests/ README.md ROADMAP.md
git commit -m "offline-check covers the window path; four eval questions; docs"
```

---

## Self-Review

**1. Spec coverage.** §0 targets T1, T3, T7 map to Tasks 7 and 8 and are measured by Q34 to Q36 in Task 10. §1's three unused assets map to Tasks 1 to 3 (JSON), Task 6 (`daily_metric.source_name`), and Task 5 (`idx_record_type_epoch`). §2's schema is Task 2, with the fresh-ingest half in Task 3. §3's four functions: `workout_detail` and `workout_measures` in Task 5, `workout_series` and `metric_series`'s `source_exclude` in Task 6. §4's module and three functions are Task 7. §5's five derivations are Task 4, and the series-only two are reached in Task 6. §6's verdict, including the `unit_mismatch` the spec gained while this plan was written, is Task 7. §7's six absence paths: workout measures in Tasks 5 and 9, dead window side and anchor cases in Task 7, activity and source-exclude cases in Task 5, and every message is asserted in Task 9's CLI tests. §8's one tool, the `get_workouts` extension and rule 1f are Task 8. §9's CLI is Task 9. §10's testing is spread across every task, with the offline, eval and doc items in Task 10. §11's exclusions appear in no task.

**2. Placeholder scan.** No TBD, no "add error handling", no "similar to Task N". Two places name a lookup to confirm in the existing code rather than guessing at it: `guardrail.check`'s signature in Task 7, and `queries.clinical_facts`'s returned keys in Task 7. The metric lookup was confirmed while writing this plan: `metrics.lookup` returns None for an unknown name while `metrics.resolve` falls back to an unregistered Metric, so the plan names `lookup` wherever an unknown name must be an error. Each says what to do if the real shape differs, which is a check against a real file rather than a gap in the plan.

**3. Type consistency.** `SeriesSpec`, `Anchor`, `AnchorAmbiguous`, `SideSummary`, `Window` and `Comparison` are defined in Task 7 and used with the same field names in Tasks 8 and 9. `WorkoutRow` from Task 5 is consumed by `workout_series` in Task 6 with the field names it defines. `derive.Derived` from Task 4 is read as `.value`, `.unit` and `.reason` in Tasks 5, 6 and 7. `workout_detail`'s parameter is `derive_names` throughout, never `derive`, because `derive` is the module; the tool and CLI argument is `derive` and is mapped to `derive_name` on `SeriesSpec`. `VERDICTS` holds six values in both Task 7 and Task 8's assertion.

**One known rough edge, deliberately left.** `_compare_windows` in Task 8 and `cmd_compare` in Task 9 both call `windows._verdict` to build the anchored comparison, reaching past the module's public surface. The alternative is a fourth public function in `windows.py` that wraps a `Window` into a `Comparison`. An implementer who prefers that should add `windows.as_comparison(window) -> Comparison` and use it in both callers; the tests in Tasks 8 and 9 pass either way.
