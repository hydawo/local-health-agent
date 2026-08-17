"""Parser and schema tests, all against the synthetic fixtures.

Expected values are hand-computed and documented in tests/fixtures/README.md.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from health_agent.ingest import healthkit
from health_agent.store import sqlite_schema


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #

def test_parses_healthkit_timestamp_format():
    dt = healthkit.parse_hk_datetime("2026-03-01 08:00:00 -0500")
    assert dt.year == 2026 and dt.month == 3 and dt.day == 1
    assert dt.hour == 8
    assert dt.utcoffset().total_seconds() == -5 * 3600


def test_calendar_date_uses_the_records_own_offset():
    """A late-evening sample must not roll into the next UTC day.

    22:30 on 2026-03-04 at -0500 is 03:30 UTC on 2026-03-05; the sample belongs
    to the 4th.
    """
    dt = healthkit.parse_hk_datetime("2026-03-04 22:30:00 -0500")
    assert dt.date().isoformat() == "2026-03-04"
    assert dt.astimezone(tz=UTC).date().isoformat() == "2026-03-05"


def test_accepts_iso8601_as_a_fallback():
    dt = healthkit.parse_hk_datetime("2026-03-01T08:00:00+00:00")
    assert dt == datetime.fromisoformat("2026-03-01T08:00:00+00:00")


def test_rejects_garbage_timestamp():
    with pytest.raises(ValueError):
        healthkit.parse_hk_datetime("not-a-date")


# --------------------------------------------------------------------------- #
# Ingest counts
# --------------------------------------------------------------------------- #

def test_ingest_counts(ingested):
    _, stats = ingested
    assert stats.records_seen == 25
    assert stats.records_inserted == 25
    assert stats.workouts_inserted == 1
    assert stats.activity_summaries == 2
    assert stats.status == "complete"
    assert not stats.skipped


def test_correlation_children_are_not_double_counted(ingested):
    """Apple's DTD: records inside a Correlation also appear at top level."""
    conn, stats = ingested
    assert stats.correlation_children_skipped == 2
    systolic = conn.execute(
        "SELECT COUNT(*) AS n FROM record "
        "WHERE type = 'HKQuantityTypeIdentifierBloodPressureSystolic'"
    ).fetchone()["n"]
    assert systolic == 1


def test_characteristics_stored(ingested):
    conn, _ = ingested
    row = conn.execute(
        "SELECT value FROM characteristic "
        "WHERE key = 'HKCharacteristicTypeIdentifierDateOfBirth'"
    ).fetchone()
    assert row["value"] == "1990-01-01"


def test_export_date_captured(ingested):
    _, stats = ingested
    assert stats.export_date == "2026-03-16 09:00:00 -0400"


# --------------------------------------------------------------------------- #
# Value handling
# --------------------------------------------------------------------------- #

def test_numeric_values_land_in_value_num(ingested):
    conn, _ = ingested
    row = conn.execute(
        "SELECT value_num, value_text, unit FROM record "
        "WHERE type = 'HKQuantityTypeIdentifierRestingHeartRate' "
        "AND local_date = '2026-03-01'"
    ).fetchone()
    assert row["value_num"] == 58.0
    assert row["value_text"] is None
    assert row["unit"] == "count/min"


def test_category_values_stay_text(ingested):
    """A sleep stage must never be coerced into a number."""
    conn, _ = ingested
    rows = conn.execute(
        "SELECT value_num, value_text FROM record "
        "WHERE type = 'HKCategoryTypeIdentifierSleepAnalysis'"
    ).fetchall()
    assert len(rows) == 4
    assert all(r["value_num"] is None for r in rows)
    assert {r["value_text"] for r in rows} == {
        "HKCategoryValueSleepAnalysisInBed",
        "HKCategoryValueSleepAnalysisAsleepCore",
        "HKCategoryValueSleepAnalysisAsleepDeep",
        "HKCategoryValueSleepAnalysisAsleepREM",
    }


def test_duration_computed_from_start_and_end(ingested):
    conn, _ = ingested
    row = conn.execute(
        "SELECT duration_sec FROM record "
        "WHERE value_text = 'HKCategoryValueSleepAnalysisAsleepCore'"
    ).fetchone()
    assert row["duration_sec"] == 7200.0  # 00:00 -> 02:00


def test_metadata_stored_as_json(ingested):
    conn, _ = ingested
    row = conn.execute(
        "SELECT metadata_json FROM record "
        "WHERE type = 'HKQuantityTypeIdentifierHeartRateVariabilitySDNN' "
        "AND metadata_json IS NOT NULL"
    ).fetchone()
    assert '"HKAlgorithmVersion": "2"' in row["metadata_json"]


def test_dst_offset_change_is_respected(ingested):
    """Records on either side of the 2026-03-08 DST change carry different
    offsets; each one's calendar date must come from its own."""
    conn, _ = ingested
    rows = {
        r["local_date"]: r["start_date"]
        for r in conn.execute(
            "SELECT local_date, start_date FROM record "
            "WHERE type = 'HKQuantityTypeIdentifierBodyMass' ORDER BY start_epoch"
        )
    }
    assert rows["2026-03-01"].endswith("-05:00")
    assert rows["2026-03-09"].endswith("-04:00")


def test_workout_row(ingested):
    conn, _ = ingested
    row = conn.execute("SELECT * FROM workout").fetchone()
    assert row["activity_type"] == "HKWorkoutActivityTypeRunning"
    assert row["duration"] == 30.0
    assert row["total_distance"] == 5.0
    assert row["local_date"] == "2026-03-06"
    assert "HKQuantityTypeIdentifierHeartRate" in row["statistics_json"]


def test_activity_summary_row(ingested):
    conn, _ = ingested
    row = conn.execute(
        "SELECT * FROM activity_summary WHERE local_date = '2026-03-02'"
    ).fetchone()
    assert row["active_energy"] == 530.0
    assert row["stand_hours"] == 12.0


# --------------------------------------------------------------------------- #
# Idempotence and incremental refresh (plan §3.4)
# --------------------------------------------------------------------------- #

def test_reingesting_the_same_file_is_skipped(ingested, fixture_export):
    conn, _ = ingested
    assert healthkit.ingest_file(conn, fixture_export) is None


def test_forced_reingest_inserts_no_duplicates(ingested, fixture_export):
    """Every Apple export re-contains full history, so a second parse of the same
    data must not double the store."""
    conn, _ = ingested
    before = conn.execute("SELECT COUNT(*) AS n FROM record").fetchone()["n"]
    stats = healthkit.ingest_file(conn, fixture_export, force=True)
    after = conn.execute("SELECT COUNT(*) AS n FROM record").fetchone()["n"]

    assert after == before
    assert stats.records_seen == 25
    assert stats.records_inserted == 0
    assert stats.records_duplicate == 25


# --------------------------------------------------------------------------- #
# Malformed input policy (plan §5a)
# --------------------------------------------------------------------------- #

def test_bad_records_are_skipped_and_counted(index_path, malformed_export):
    conn = sqlite_schema.connect(index_path, create=True)
    sqlite_schema.initialize(conn)
    stats = healthkit.ingest_file(conn, malformed_export)

    # 6 records seen: 2 skipped (bad date, missing date), 4 stored.
    assert stats.records_seen == 6
    assert stats.records_inserted == 4
    assert sum(stats.skipped.values()) == 2
    assert stats.status == "complete"

    # The non-numeric value on a quantity type is kept as text rather than 0.
    row = conn.execute(
        "SELECT value_num, value_text FROM record WHERE local_date = '2026-03-04'"
    ).fetchone()
    assert row["value_num"] is None
    assert row["value_text"] == "unknown"

    # A missing unit is not fatal.
    row = conn.execute(
        "SELECT unit, value_num FROM record "
        "WHERE type = 'HKQuantityTypeIdentifierBodyMass'"
    ).fetchone()
    assert row["unit"] is None and row["value_num"] == 180.0
    conn.close()


def test_truncated_export_keeps_what_parsed(tmp_path, fixture_export, index_path):
    """A file that ends mid-element is ingested up to the break and marked
    partial, rather than losing the whole run."""
    truncated = tmp_path / "export.xml"
    text = fixture_export.read_text()
    truncated.write_text(text[: text.index("HKQuantityTypeIdentifierBodyMass")])

    conn = sqlite_schema.connect(index_path, create=True)
    sqlite_schema.initialize(conn)
    stats = healthkit.ingest_file(conn, truncated)

    assert stats.status == "partial"
    assert stats.records_inserted > 0
    status = conn.execute("SELECT status FROM source_file").fetchone()["status"]
    assert status == "partial"
    conn.close()


def test_wrong_root_element_is_rejected(tmp_path, index_path):
    bogus = tmp_path / "export.xml"
    bogus.write_text('<?xml version="1.0"?><NotHealthData/>')
    conn = sqlite_schema.connect(index_path, create=True)
    sqlite_schema.initialize(conn)
    with pytest.raises(healthkit.MalformedExport):
        healthkit.ingest_file(conn, bogus)
    conn.close()


# --------------------------------------------------------------------------- #
# Zip handling
# --------------------------------------------------------------------------- #

def test_ingests_straight_from_a_zip(tmp_path, fixture_export, index_path):
    import zipfile

    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(fixture_export, "apple_health_export/export.xml")
        zf.writestr("apple_health_export/export_cda.xml", "<ClinicalDocument/>")

    conn = sqlite_schema.connect(index_path, create=True)
    sqlite_schema.initialize(conn)
    stats = healthkit.ingest_file(conn, archive)
    assert stats.records_inserted == 25
    conn.close()


# --------------------------------------------------------------------------- #
# Schema versioning (plan §5a)
# --------------------------------------------------------------------------- #

def test_version_mismatch_is_actionable(ingested, index_path):
    conn, _ = ingested
    conn.execute("UPDATE schema_meta SET value = '999' WHERE key = 'schema_version'")
    conn.commit()
    with pytest.raises(sqlite_schema.SchemaVersionMismatch) as exc:
        sqlite_schema.check_version(conn)
    assert "--rebuild" in str(exc.value)


def test_missing_index_is_reported_not_created(tmp_path):
    with pytest.raises(sqlite_schema.IndexNotFound):
        sqlite_schema.connect(tmp_path / "nope.db")


def test_unbuilt_aggregates_are_reported_not_silently_empty(index_path,
                                                            fixture_export):
    """Reads go through daily_metric, so an unbuilt rollup would look exactly
    like an empty export. It must say so instead."""
    conn = sqlite_schema.connect(index_path, create=True)
    sqlite_schema.initialize(conn)
    healthkit.ingest_file(conn, fixture_export)  # deliberately no rebuild
    with pytest.raises(sqlite_schema.AggregatesMissing) as exc:
        sqlite_schema.check_aggregates(conn)
    assert "--rebuild" in str(exc.value)
    conn.close()


def test_empty_index_is_not_flagged_as_stale(index_path):
    conn = sqlite_schema.connect(index_path, create=True)
    sqlite_schema.initialize(conn)
    sqlite_schema.check_aggregates(conn)  # must not raise
    conn.close()
