"""Aggregation tests. Expected values are hand-computed; see fixtures/README.md."""

from __future__ import annotations

from health_agent import metrics
from health_agent.store import queries


def series(conn, name, **kwargs):
    return queries.metric_series(conn, metrics.resolve(name), **kwargs)


# --------------------------------------------------------------------------- #
# Aggregation semantics
# --------------------------------------------------------------------------- #

def test_discrete_metric_defaults_to_mean(ingested):
    conn, _ = ingested
    result = series(conn, "resting-hr", period="month")
    assert result.agg == "avg"
    assert len(result.points) == 1
    # 58 + 60 + 62 + 59 + 61 + 57 + 63 = 420 over 7 samples
    assert result.points[0].value == 60.0
    assert result.points[0].n == 7
    assert result.unit == "count/min"


def test_cumulative_metric_defaults_to_sum(ingested):
    conn, _ = ingested
    result = series(conn, "steps", period="day", start="2026-03-03", end="2026-03-03")
    assert result.agg == "sum"
    assert result.points[0].value == 8500.0


def test_daily_series_has_one_point_per_day_with_data(ingested):
    conn, _ = ingested
    result = series(conn, "resting-hr")
    assert [p.period for p in result.points] == [
        f"2026-03-0{d}" for d in range(1, 8)
    ]
    assert [p.value for p in result.points] == [58, 60, 62, 59, 61, 57, 63]


def test_explicit_aggregation_overrides_the_default(ingested):
    conn, _ = ingested
    result = series(conn, "resting-hr", period="month", agg="min")
    assert result.points[0].value == 57.0
    result = series(conn, "resting-hr", period="month", agg="max")
    assert result.points[0].value == 63.0
    result = series(conn, "resting-hr", period="month", agg="count")
    assert result.points[0].value == 7.0


def test_weekly_average_is_sample_weighted_not_average_of_averages(ingested):
    conn, _ = ingested
    # 2026-03-01 is a Sunday, so it belongs to the week starting 2026-02-23;
    # 03-02..03-07 fall in the week starting 2026-03-02.
    result = series(conn, "resting-hr", period="week")
    by_period = {p.period: p for p in result.points}
    assert by_period["2026-02-23"].value == 58.0
    assert by_period["2026-02-23"].n == 1
    # (60 + 62 + 59 + 61 + 57 + 63) / 6 = 60.333...
    assert round(by_period["2026-03-02"].value, 4) == 60.3333
    assert by_period["2026-03-02"].n == 6


# --------------------------------------------------------------------------- #
# The multi-source problem
# --------------------------------------------------------------------------- #

def test_overlapping_sources_are_not_summed_by_default(ingested):
    """Watch 6000 + Phone 5500 on the same day is not 11500 steps walked."""
    conn, _ = ingested
    result = series(conn, "steps", start="2026-03-02", end="2026-03-02")
    assert result.combine == "max"
    assert result.points[0].value == 6000.0
    assert result.multi_source_periods == 1
    assert result.points[0].per_source == {
        "Fixture Watch": 6000.0,
        "Fixture Phone": 5500.0,
    }


def test_combine_sum_is_available_when_the_caller_knows_better(ingested):
    conn, _ = ingested
    result = series(conn, "steps", start="2026-03-02", end="2026-03-02",
                    combine="sum")
    assert result.points[0].value == 11500.0


def test_source_filter(ingested):
    conn, _ = ingested
    result = series(conn, "steps", start="2026-03-02", end="2026-03-02",
                    source="Fixture Phone")
    assert result.points[0].value == 5500.0
    assert result.multi_source_periods == 0


def test_single_source_days_are_unaffected(ingested):
    conn, _ = ingested
    result = series(conn, "steps", start="2026-03-03", end="2026-03-03")
    assert result.multi_source_periods == 0
    assert result.points[0].value == 8500.0


# --------------------------------------------------------------------------- #
# Category metrics
# --------------------------------------------------------------------------- #

def test_sleep_aggregates_by_duration_per_stage(ingested):
    conn, _ = ingested
    result = series(conn, "sleep")
    assert result.agg == "duration"
    by_key = {(p.period, p.category): p.value for p in result.points}
    assert by_key[("2026-03-04", "HKCategoryValueSleepAnalysisInBed")] == 1800.0
    assert by_key[("2026-03-05", "HKCategoryValueSleepAnalysisAsleepCore")] == 7200.0
    assert by_key[("2026-03-05", "HKCategoryValueSleepAnalysisAsleepDeep")] == 2700.0
    assert by_key[("2026-03-05", "HKCategoryValueSleepAnalysisAsleepREM")] == 2700.0


# --------------------------------------------------------------------------- #
# Windows and missing data
# --------------------------------------------------------------------------- #

def test_date_window_is_inclusive(ingested):
    conn, _ = ingested
    result = series(conn, "resting-hr", start="2026-03-02", end="2026-03-04")
    assert [p.period for p in result.points] == ["2026-03-02", "2026-03-03", "2026-03-04"]


def test_empty_window_still_reports_what_exists(ingested):
    """Feeds the missing-data behavior in plan §5a: name what's missing and where
    the data actually is, rather than an unqualified 'no data'."""
    conn, _ = ingested
    result = series(conn, "resting-hr", start="2026-05-01", end="2026-05-31")
    assert result.is_empty
    assert result.total_records == 7
    assert result.available_from == "2026-03-01"
    assert result.available_to == "2026-03-07"


def test_metric_absent_from_the_index(ingested):
    conn, _ = ingested
    result = series(conn, "vo2max")
    assert result.is_empty
    assert result.total_records == 0
    assert result.available_from is None


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

def test_aliases_and_identifiers_both_resolve():
    by_alias = metrics.resolve("resting-hr")
    by_identifier = metrics.resolve("HKQuantityTypeIdentifierRestingHeartRate")
    by_bare_name = metrics.resolve("RestingHeartRate")
    assert by_alias == by_identifier == by_bare_name


def test_unknown_identifier_falls_back_to_discrete():
    """Averaging a cumulative type fails visibly; summing a discrete one produces
    a plausible-looking wrong number. Prefer the visible failure."""
    metric = metrics.resolve("HKQuantityTypeIdentifierMadeUpThing")
    assert metric.kind is metrics.Kind.DISCRETE
    assert metric.default_agg == "avg"


def test_category_identifier_fallback_is_category():
    metric = metrics.resolve("HKCategoryTypeIdentifierMadeUpThing")
    assert metric.kind is metrics.Kind.CATEGORY


# --------------------------------------------------------------------------- #
# Index-level summaries
# --------------------------------------------------------------------------- #

def test_index_summary(ingested):
    conn, _ = ingested
    summary = queries.index_summary(conn)
    assert summary["records"] == 25
    assert summary["first_date"] == "2026-03-01"
    assert summary["last_date"] == "2026-03-09"
    assert summary["workouts"] == 1
    assert summary["activity_summaries"] == 2
    assert "Fixture Watch" in summary["sources"]


def test_list_types_search(ingested):
    conn, _ = ingested
    found = queries.list_types(conn, search="sleep")
    assert len(found) == 1
    assert found[0]["type"] == "HKCategoryTypeIdentifierSleepAnalysis"
    assert found[0]["n"] == 4


def test_workout_summary(ingested):
    conn, _ = ingested
    found = queries.workout_summary(conn)
    assert found[0]["activity_type"] == "HKWorkoutActivityTypeRunning"
    assert found[0]["total_distance"] == 5.0
