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
    """A v5 index: one workout with its JSON blobs, no workout_stat, no indoor."""
    conn = sqlite_schema.connect(path, create=True)
    sqlite_schema.initialize(conn)
    conn.execute("DROP TABLE workout_stat")
    conn.execute("ALTER TABLE workout DROP COLUMN indoor")
    conn.execute(
        "INSERT INTO workout(activity_type, duration, duration_unit, "
        "start_date, start_epoch, local_date, metadata_json, statistics_json, "
        "dedup_key) VALUES('HKWorkoutActivityTypeRunning', 30, 'min', "
        "'2026-03-06T17:00:00-05:00', 1772830800, '2026-03-06', ?, ?, X'01')",
        (meta, stats))
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', '5') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    conn.commit()
    return conn


def test_a_v5_index_migrates_in_place_and_backfills_from_its_own_json(tmp_path):
    conn = _v5_index(tmp_path / "health.db")
    conn.execute(
        "INSERT INTO workout(activity_type, duration, duration_unit, "
        "start_date, start_epoch, local_date, metadata_json, statistics_json, "
        "dedup_key) VALUES('HKWorkoutActivityTypeCycling', 45, 'min', "
        "'2026-03-07T09:00:00-05:00', 1772895600, '2026-03-07', "
        "'{\"HKIndoorWorkout\": \"0\"}', ?, X'03')",
        ('[{"average": "120", "type": "HKQuantityTypeIdentifierHeartRate", '
         '"unit": "count/min"}]',))
    conn.execute(
        "INSERT INTO workout(activity_type, duration, duration_unit, "
        "start_date, start_epoch, local_date, dedup_key) "
        "VALUES('HKWorkoutActivityTypeWalking', 20, 'min', "
        "'2026-03-08T09:00:00-05:00', 1772982000, '2026-03-08', X'04')")
    conn.commit()
    assert sqlite_schema.read_version(conn) == 5
    before = {r["name"] for r in conn.execute("PRAGMA table_info(workout)")}
    assert "indoor" not in before

    assert sqlite_schema.migrate(conn) == 5

    after = {r["name"] for r in conn.execute("PRAGMA table_info(workout)")}
    assert "indoor" in after

    assert sqlite_schema.read_version(conn) == 6
    sqlite_schema.check_version(conn)          # no longer raises
    rows = {(r["kind"], r["key"], r["value_num"], r["unit"]) for r in
            conn.execute("SELECT kind, key, value_num, unit FROM workout_stat")}
    assert ("statistic", "HKQuantityTypeIdentifierHeartRate:average",
            150.0, "count/min") in rows
    assert ("metadata", "HKIndoorWorkout", 1.0, None) in rows
    indoor = [r["indoor"] for r in conn.execute(
        "SELECT indoor FROM workout ORDER BY start_epoch")]
    assert indoor == [1, 0, None]
    assert conn.execute("SELECT COUNT(*) AS n FROM workout").fetchone()["n"] == 3
    assert conn.execute("SELECT COUNT(*) AS n FROM workout_stat").fetchone()["n"] == 6


def test_a_fresh_index_has_the_indoor_column(tmp_path):
    conn = sqlite_schema.connect(tmp_path / "fresh.db", create=True)
    sqlite_schema.initialize(conn)
    assert "indoor" in {r["name"] for r in conn.execute("PRAGMA table_info(workout)")}


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
