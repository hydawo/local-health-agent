# tests/test_derive.py
"""The closed list of five derivations.

A closed list rather than an expression language, because an expression
language is the model doing arithmetic with extra steps, which the prompt has
forbidden since the day it answered 59.6 for an average that was exactly 60.0.
"""
from __future__ import annotations

from fractions import Fraction

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
    # n=4: p25 is the 1st of 4 by nearest rank, p75 the 3rd. The first
    # version of this used Python's round(), which is half to even, and
    # returned the 2nd and the 4th.
    four = [1.0, 2.0, 3.0, 4.0]
    assert derive.percentile(four, 25, "u").value == 1.0
    assert derive.percentile(four, 50, "u").value == 2.0
    assert derive.percentile(four, 75, "u").value == 3.0
    # n=6 at p50 and n=10 at p50 and p90 are the sizes this project will
    # actually hit, since a week of workouts or a month of readings lands there.
    assert derive.percentile([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], 50, "u").value == 3.0
    ten = [float(i) for i in range(1, 11)]
    assert derive.percentile(ten, 50, "u").value == 5.0
    assert derive.percentile(ten, 90, "u").value == 9.0
    # n=25 p=28: 0.28 * 25 is 7.000000000000001 in float, so a ceil on the
    # float product returned the 8th value. Integer arithmetic is exact.
    twenty_five = [float(i) for i in range(1, 26)]
    assert derive.percentile(twenty_five, 28, "u").value == 7.0


def test_percentile_is_nearest_rank_at_every_size_and_percentile():
    """Checked against exact rational arithmetic, because the first two
    attempts at this were a half-to-even round and then a float
    multiply, and both passed a handful of hand-picked sizes."""
    for n in range(1, 60):
        values = [float(i) for i in range(1, n + 1)]
        for p in range(0, 101):
            rank = -(-Fraction(p) * n // 100)
            expected = values[max(0, min(n - 1, int(rank) - 1))]
            assert derive.percentile(values, p, "u").value == expected, (n, p)


def test_every_derivation_handles_missing_and_zero_inputs_without_raising():
    assert derive.pace(None, "mi", 30.0, "min").value is None
    assert derive.pace(0.0, "mi", 30.0, "min").reason == "distance is zero"
    assert derive.speed(3.0, "mi", 0.0, "min").reason == "duration is zero"
    assert derive.speed(3.0, "mi", 30.0, "fortnight").reason == (
        "unknown duration unit: fortnight")
    assert derive.efficiency(3.0, "mi", 30.0, "min", 0.0, "count/min").reason == (
        "heart rate is zero")
    assert derive.percentile([], 50, "mph").value is None
    # float() and math.isfinite() both raise OverflowError on an int this
    # large, so the guard has to catch it rather than assume a number is
    # convertible.
    huge = 10 ** 400
    assert derive.pace(huge, "mi", 5.0, "min").value is None
    assert derive.percentile([huge], 50, "u").value is None


def test_every_derivation_returns_a_reason_for_non_numbers_and_never_raises():
    assert derive.pace("3", "mi", 30.0, "min").reason == "not a number: '3'"
    assert derive.speed(3.0, "mi", 30.0, 5).reason == "unknown duration unit: 5"
    assert derive.speed(float("nan"), "mi", 30.0, "min").reason == (
        "not a number: nan")
    assert derive.ratio("x", "1", "lb", 2.0, "lb").reason == "not a number: '1'"
    assert derive.percentile(None, 50, "u").reason == "no values in scope"
    # repr() on an integer past CPython's 4300-digit limit raises, so the
    # reason has to be built without calling repr on the raw value.
    assert derive.pace(10 ** 5000, "mi", 5.0, "min").value is None
    assert derive.percentile([1.0], 10 ** 5000, "u").value is None
    # A reason is read by a local model, so it stays short enough to scan.
    assert len(derive.pace(10 ** 400, "mi", 5.0, "min").reason) <= 80
    assert derive.percentile([1.0], float("nan"), "u").reason == (
        "not a number: nan")


def test_the_vocabulary_is_closed():
    assert derive.DERIVATIONS == ("pace", "speed", "efficiency", "ratio",
                                  "percentile")


def test_seconds_converts_known_units_and_refuses_unknown_ones():
    assert derive.seconds(30.0, "min") == 1800.0
    assert derive.seconds(1.0, "hr") == 3600.0
    assert derive.seconds(5.0, "sec") == 5.0
    assert derive.seconds(5.0, "fortnight") is None
    assert derive.seconds(None, "min") is None


def test_median_is_the_middle_value_and_averages_the_two_middle_of_an_even_count():
    assert derive.median([3, 1, 2], "u").value == 2
    assert derive.median([1, 2, 3, 4], "u").value == 2.5
    assert derive.median([], "u").value is None
    assert derive.median([None, float("nan")], "u").reason
