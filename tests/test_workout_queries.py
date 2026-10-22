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
