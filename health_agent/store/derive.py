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

# How many of each duration unit make one hour: 60 minutes, 3600 seconds, 1 hour.
_PER_HOUR = {"min": 60.0, "minute": 60.0, "minutes": 60.0,
             "hr": 1.0, "hour": 1.0, "hours": 1.0,
             "s": 3600.0, "sec": 3600.0, "second": 3600.0, "seconds": 3600.0}


@dataclass(frozen=True)
class Derived:
    name: str
    value: float | None
    unit: str | None
    reason: str | None = None


def _finite(value) -> float | None:
    """A real number, or None for anything else, NaN and infinity included.
    Callers turn None into a reason rather than raising."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _missing(name: str) -> Derived:
    return Derived(name, None, None, "a value this needs is missing")


def _reject(name: str, *values) -> Derived | None:
    """A Derived saying why the values cannot be used, or None if they can."""
    for value in values:
        if value is None:
            return _missing(name)
        if _finite(value) is None:
            return Derived(name, None, None, f"not a number: {value!r}")
    return None


def pace(distance: float | None, distance_unit: str | None,
         duration: float | None, duration_unit: str | None) -> Derived:
    """Duration over distance, in the units given."""
    bad = _reject("pace", distance, duration)
    if bad:
        return bad
    d, t = _finite(distance), _finite(duration)
    if d == 0:
        return Derived("pace", None, None, "distance is zero")
    unit = f"{duration_unit or '?'}/{distance_unit or '?'}"
    return Derived("pace", t / d, unit)


def speed(distance: float | None, distance_unit: str | None,
          duration: float | None, duration_unit: str | None) -> Derived:
    """Distance per hour, in the distance unit given."""
    bad = _reject("speed", distance, duration)
    if bad:
        return bad
    d, t = _finite(distance), _finite(duration)
    if t == 0:
        return Derived("speed", None, None, "duration is zero")
    factor = (_PER_HOUR.get(duration_unit.strip().lower())
              if isinstance(duration_unit, str) else None)
    if factor is None:
        return Derived("speed", None, None,
                       f"unknown duration unit: {duration_unit}")
    hours = t / factor
    return Derived("speed", d / hours, f"{distance_unit or '?'}/hr")


def efficiency(distance: float | None, distance_unit: str | None,
               duration: float | None, duration_unit: str | None,
               heart_rate: float | None, hr_unit: str | None) -> Derived:
    """Speed per hundred heartbeats. The ACL analysis's mph per 100 bpm."""
    bad = _reject("efficiency", heart_rate)
    if bad:
        return bad
    hr = _finite(heart_rate)
    if hr == 0:
        return Derived("efficiency", None, None, "heart rate is zero")
    base = speed(distance, distance_unit, duration, duration_unit)
    if base.value is None:
        return Derived("efficiency", None, None, base.reason)
    unit = f"{base.unit} per 100 {hr_unit or '?'}"
    return Derived("efficiency", base.value / (hr / 100.0), unit)


def ratio(name: str, a: float | None, unit_a: str | None,
          b: float | None, unit_b: str | None) -> Derived:
    """One measure over another. Dimensionless, and only within one unit."""
    bad = _reject(name, a, b)
    if bad:
        return bad
    x, y = _finite(a), _finite(b)
    if (unit_a or None) != (unit_b or None):
        return Derived(name, None, None,
                       f"incompatible units: {unit_a} and {unit_b}")
    if y == 0:
        return Derived(name, None, None, "denominator is zero")
    return Derived(name, x / y, None)


def percentile(values, p: int, unit: str | None) -> Derived:
    """Nearest rank, no interpolation.

    An interpolated percentile is a number no instrument produced, which is the
    same objection this project raises to converting a unit.
    """
    pf = _finite(p)
    if pf is None:
        return Derived("percentile", None, None, f"not a number: {p!r}")
    try:
        items = list(values)
    except TypeError:
        items = []
    ordered = sorted(x for x in map(_finite, items) if x is not None)
    if not ordered:
        return Derived(f"p{p}", None, unit, "no values in scope")
    # Integer arithmetic: a float product such as 0.28 * 25 lands a hair above
    # 7 and ceil would take the 8th value. Integers are exact.
    rank = -(-int(pf) * len(ordered) // 100)
    index = max(0, min(len(ordered) - 1, rank - 1))
    return Derived(f"p{p}", ordered[index], unit)
