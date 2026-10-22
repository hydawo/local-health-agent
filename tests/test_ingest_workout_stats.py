"""A fresh ingest fills the new table directly, rather than relying on the
migration to catch up later."""
from __future__ import annotations

from pathlib import Path

from health_agent.ingest import healthkit
from health_agent.store import sqlite_schema

FIXTURE = Path(__file__).parent / "fixtures" / "export.xml"


def _ingest(conn):
    sid, _ = healthkit.register_source_file(
        conn, FIXTURE, "healthkit", healthkit.sha256_file(FIXTURE))
    return healthkit.ingest_export(conn, FIXTURE, source_file_id=sid)


def test_ingesting_the_fixture_export_records_its_workout_statistics(tmp_path):
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    _ingest(conn)
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
    _ingest(conn)
    first = conn.execute("SELECT COUNT(*) AS n FROM workout_stat").fetchone()["n"]
    stats = _ingest(conn)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) AS n FROM workout_stat").fetchone()["n"]
    assert n == first > 0
    assert n == conn.execute(
        "SELECT COUNT(DISTINCT workout_id || kind || key) AS n FROM workout_stat"
    ).fetchone()["n"]
    assert stats.workouts_inserted == 0


def test_backfill_changes_do_not_leak_into_the_insert_counts(tmp_path):
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    stats = _ingest(conn)
    assert stats.workouts_inserted == conn.execute(
        "SELECT COUNT(*) AS n FROM workout").fetchone()["n"] == 1
    assert stats.records_inserted == conn.execute(
        "SELECT COUNT(*) AS n FROM record").fetchone()["n"]
