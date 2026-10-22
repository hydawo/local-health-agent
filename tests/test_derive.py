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
