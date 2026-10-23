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


def test_a_deeply_nested_blob_yields_no_rows_rather_than_raising():
    """The migration runs over every blob already in the index, so a blob
    nested past the parser's recursion limit must cost only itself."""
    deep = "[" * 100_000 + "]" * 100_000
    assert workout_stats.stat_rows(deep, None) == []
    assert workout_stats.indoor_flag('{"HKIndoorWorkout": ' + "[" * 100_000) is None


def test_booleans_and_non_finite_values_take_the_text_branch():
    rows = workout_stats.stat_rows(
        '[{"type": "T", "average": true, "minimum": "nan", '
        '"maximum": "1e999", "unit": "u"}]', None)
    assert rows == [
        ("statistic", "T:average", None, "True", "u"),
        ("statistic", "T:minimum", None, "nan", "u"),
        ("statistic", "T:maximum", None, "1e999", "u"),
    ]


def test_a_four_hundred_digit_integer_takes_the_text_branch():
    # A 400-digit JSON integer takes the text branch instead of aborting a
    # migration that runs over every blob in the index.
    rows = workout_stats.stat_rows(
        '[{"type": "T", "average": ' + "9" * 400 + '}]', None)
    assert rows[0][2] is None and rows[0][3] is not None


def test_the_sum_aggregate_is_recorded_like_the_others():
    rows = workout_stats.stat_rows(
        '[{"type": "T", "sum": "42", "unit": "u"}]', None)
    assert rows == [("statistic", "T:sum", 42.0, None, "u")]


def test_a_non_dict_entry_is_skipped_among_valid_ones():
    rows = workout_stats.stat_rows(
        '[{"type": "T", "average": "1", "unit": "u"}, 7, '
        '{"type": "U", "average": "2", "unit": "u"}]', None)
    assert [(r[1], r[2]) for r in rows] == [("T:average", 1.0),
                                            ("U:average", 2.0)]


def test_a_non_string_type_yields_no_rows():
    assert workout_stats.stat_rows(
        '[{"type": 5, "average": "1", "unit": "u"}]', None) == []
    assert workout_stats.stat_rows(
        '[{"type": ["a"], "average": "1", "unit": "u"}]', None) == []
