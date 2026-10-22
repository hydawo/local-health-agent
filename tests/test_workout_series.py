"""Workouts grouped by period, which is T3's "128 against 24 minutes a week"."""
from __future__ import annotations

import pytest

from health_agent.store import queries, sqlite_schema

HR = "HKQuantityTypeIdentifierHeartRate"
CAD = "HKQuantityTypeIdentifierCyclingCadence"


@pytest.fixture
def box(tmp_path):
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    yield conn
    conn.close()


def _ride(conn, *, date, duration, hr_avg, key, epoch=1772830800.0,
          source="Watch", unit="min", cadence=None, dist_unit="mi",
          energy=None, energy_unit="kcal", hr_unit="count/min"):
    cur = conn.execute(
        "INSERT INTO workout(activity_type, duration, duration_unit, "
        "total_distance, total_distance_unit, total_energy, "
        "total_energy_unit, source_name, start_date, "
        "start_epoch, local_date, dedup_key) "
        "VALUES('HKWorkoutActivityTypeCycling', ?, ?, 10.0, ?, ?, ?, ?, "
        "?, ?, ?, ?)",
        (duration, unit, dist_unit, energy, energy_unit if energy else None,
         source, f"{date}T09:00:00-05:00", epoch, date, key))
    conn.execute(
        "INSERT INTO workout_stat(workout_id, kind, key, value_num, unit) "
        "VALUES(?, 'statistic', ?, ?, ?)",
        (cur.lastrowid, f"{HR}:average", hr_avg, hr_unit))
    if cadence is not None:
        conn.execute(
            "INSERT INTO workout_stat(workout_id, kind, key, value_num, unit) "
            "VALUES(?, 'statistic', ?, ?, 'count/min')",
            (cur.lastrowid, f"{CAD}:average", cadence))
    conn.commit()


def test_a_week_of_rides_becomes_one_point_with_summed_minutes(box):
    _ride(box, date="2026-03-02", duration=40.0, hr_avg=145.0, key=b"\x01")
    _ride(box, date="2026-03-04", duration=50.0, hr_avg=135.0, key=b"\x02")
    _ride(box, date="2026-03-11", duration=30.0, hr_avg=130.0, key=b"\x03")
    series = queries.workout_series(box, period="week", activity="Cycling",
                                    measures=(f"{HR}:average",))
    assert [p.n for p in series.points] == [2, 1]
    assert [p.duration for p in series.points] == [90.0, 30.0]
    assert series.points[0].duration_unit == "min"
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


def test_series_level_percentile_is_nearest_rank(box):
    _ride(box, date="2026-03-02", duration=40.0, hr_avg=140.0, key=b"\x01")
    _ride(box, date="2026-03-03", duration=20.0, hr_avg=160.0, key=b"\x02")
    series = queries.workout_series(
        box, period="week", activity="Cycling", measures=(f"{HR}:average",),
        derive_names=("percentile",))
    point = series.points[0]
    # Nearest rank takes an observed value; it never interpolates to 150.
    assert point.derived["p50"]["value"] == 140.0
    assert point.derived["p90"]["value"] == 160.0


def test_ratio_names_its_numerator_and_denominator(box):
    _ride(box, date="2026-03-02", duration=40.0, hr_avg=150.0, cadence=75.0,
          key=b"\x01")
    hr, cad = f"{HR}:average", f"{CAD}:average"
    one = queries.workout_series(box, measures=(hr, cad),
                                 derive_names=("ratio",), ratio_of=(hr, cad))
    other = queries.workout_series(box, measures=(cad, hr),
                                   derive_names=("ratio",), ratio_of=(hr, cad))
    assert one.points[0].derived["ratio"]["value"] == 2.0
    # Reordering measures changes nothing, because order was never the meaning.
    assert other.points[0].derived["ratio"]["value"] == 2.0


def test_ratio_without_a_named_pair_refuses_rather_than_guess_by_position(box):
    _ride(box, date="2026-03-02", duration=40.0, hr_avg=150.0, cadence=75.0,
          key=b"\x01")
    series = queries.workout_series(
        box, measures=(f"{HR}:average", f"{CAD}:average"),
        derive_names=("ratio",))
    entry = series.points[0].derived["ratio"]
    assert entry["value"] is None
    assert "ratio_of" in entry["reason"]


def test_ratio_of_must_name_requested_measures(box):
    with pytest.raises(ValueError):
        queries.workout_series(box, measures=(f"{HR}:average",),
                               derive_names=("ratio",),
                               ratio_of=(f"{HR}:average", f"{CAD}:average"))


def test_percentile_with_several_measures_needs_to_be_told_which(box):
    _ride(box, date="2026-03-02", duration=40.0, hr_avg=150.0, cadence=75.0,
          key=b"\x01")
    hr, cad = f"{HR}:average", f"{CAD}:average"
    ambiguous = queries.workout_series(box, measures=(hr, cad),
                                       derive_names=("percentile",))
    assert ambiguous.points[0].derived["p50"]["value"] is None
    named = queries.workout_series(box, measures=(hr, cad),
                                   derive_names=("percentile",),
                                   percentile_of=cad)
    assert named.points[0].derived["p50"]["value"] == 75.0


def test_mixed_duration_units_in_one_period_are_not_added(box):
    _ride(box, date="2026-03-02", duration=40.0, hr_avg=140.0, key=b"\x01")
    _ride(box, date="2026-03-03", duration=1.0, hr_avg=140.0, key=b"\x02",
          unit="hr")
    point = queries.workout_series(box, period="week").points[0]
    assert point.n == 2
    assert point.duration is None


def test_a_real_zero_total_is_not_reported_as_missing(box):
    _ride(box, date="2026-03-02", duration=0.0, hr_avg=140.0, key=b"\x01")
    assert queries.workout_series(box).points[0].duration == 0.0


def test_source_exclusion_reaches_the_series(box):
    _ride(box, date="2026-03-02", duration=40.0, hr_avg=140.0, key=b"\x01")
    _ride(box, date="2026-03-03", duration=20.0, hr_avg=140.0, key=b"\x02",
          source="Ring")
    series = queries.workout_series(box, source_exclude=("Ring",))
    assert series.points[0].n == 1
    with pytest.raises(ValueError):
        queries.workout_series(box, source_include=("Ring",),
                               source_exclude=("Ring",))


def test_the_period_must_be_one_the_project_already_buckets_by(box):
    with pytest.raises(ValueError):
        queries.workout_series(box, period="year")


def test_per_workout_derivations_are_summarized_by_median(box):
    _ride(box, date="2026-03-02", duration=40.0, hr_avg=140.0, key=b"\x01")
    _ride(box, date="2026-03-03", duration=20.0, hr_avg=140.0, key=b"\x02")
    series = queries.workout_series(box, derive_names=("speed",))
    # 15 and 30 mi/hr; the median of two is their midpoint.
    assert series.points[0].derived["speed"]["value"] == 22.5
    assert series.points[0].derived["speed"]["unit"] == "mi/hr"


def test_a_derived_median_over_two_units_is_refused_and_names_them(box):
    _ride(box, date="2026-03-02", duration=40.0, hr_avg=140.0, key=b"\x01")
    _ride(box, date="2026-03-03", duration=20.0, hr_avg=140.0, key=b"\x02",
          dist_unit="km")
    entry = queries.workout_series(
        box, derive_names=("speed",)).points[0].derived["speed"]
    assert entry["value"] is None and entry["unit"] == "mixed"
    assert "km" in entry["reason"] and "mi" in entry["reason"]


def test_a_measure_median_over_two_units_is_refused_and_reports_them(box):
    _ride(box, date="2026-03-02", duration=40.0, hr_avg=140.0, key=b"\x01")
    _ride(box, date="2026-03-03", duration=40.0, hr_avg=2.3, key=b"\x02",
          hr_unit="count/s")
    hr = f"{HR}:average"
    point = queries.workout_series(
        box, measures=(hr,), derive_names=("percentile",)).points[0]
    m = point.measures[hr]
    assert m["median"] is None and m["unit"] == "mixed"
    assert m["units"] == ["count/min", "count/s"]
    assert point.derived["p50"]["value"] is None
    assert point.derived["p50"]["unit"] == "mixed"


def test_measure_counts_separate_present_from_missing(box):
    _ride(box, date="2026-03-02", duration=40.0, hr_avg=140.0, key=b"\x01")
    _ride(box, date="2026-03-03", duration=40.0, hr_avg=150.0, key=b"\x02")
    _ride(box, date="2026-03-04", duration=40.0, hr_avg=150.0, key=b"\x03")
    box.execute("DELETE FROM workout_stat WHERE workout_id = "
                "(SELECT MAX(id) FROM workout)")
    box.commit()
    m = queries.workout_series(
        box, measures=(f"{HR}:average",)).points[0].measures[f"{HR}:average"]
    assert (m["n"], m["missing"], m["median"]) == (2, 1, 145.0)


def test_nothing_reported_and_unit_mismatch_are_different_absences(box):
    _ride(box, date="2026-03-02", duration=40.0, hr_avg=140.0, key=b"\x01",
          dist_unit="km")
    _ride(box, date="2026-03-03", duration=40.0, hr_avg=140.0, key=b"\x02")
    point = queries.workout_series(box).points[0]
    assert point.distance is None and point.distance_unit == "mixed"
    assert point.energy is None and point.energy_unit is None


def test_mixed_energy_units_are_not_added_but_matching_ones_are(box):
    _ride(box, date="2026-03-02", duration=40.0, hr_avg=140.0, key=b"\x01",
          energy=300.0)
    _ride(box, date="2026-03-03", duration=40.0, hr_avg=140.0, key=b"\x02",
          energy=200.0)
    point = queries.workout_series(box).points[0]
    assert (point.energy, point.energy_unit) == (500.0, "kcal")
    _ride(box, date="2026-03-04", duration=40.0, hr_avg=140.0, key=b"\x03",
          energy=100.0, energy_unit="kJ")
    point = queries.workout_series(box).points[0]
    assert point.energy is None and point.energy_unit == "mixed"


def test_ratio_of_must_be_two_different_measures(box):
    hr = f"{HR}:average"
    with pytest.raises(ValueError):
        queries.workout_series(box, measures=(hr,), derive_names=("ratio",),
                               ratio_of=(hr, hr))


def test_ratio_of_and_percentile_of_need_their_derivation_requested(box):
    hr, cad = f"{HR}:average", f"{CAD}:average"
    with pytest.raises(ValueError):
        queries.workout_series(box, measures=(hr, cad), ratio_of=(hr, cad))
    with pytest.raises(ValueError):
        queries.workout_series(box, measures=(hr, cad), percentile_of=hr)
