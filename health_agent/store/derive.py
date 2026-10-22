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

from dataclasses import dataclass

DERIVATIONS = ("pace", "speed", "efficiency", "ratio", "percentile")

# Seconds-per-hour factors for the duration units an export uses.
_PER_HOUR = {"min": 60.0, "minute": 60.0, "minutes": 60.0,
             "hr": 1.0, "hour": 1.0, "hours": 1.0,
             "s": 3600.0, "sec": 3600.0, "second": 3600.0, "seconds": 3600.0}


@dataclass(frozen=True)
class Derived:
    name: str
    value: float | None
    unit: str | None
    reason: str | None = None


def _missing(name: str) -> Derived:
    return Derived(name, None, None, "a value this needs is missing")


def pace(distance: float | None, distance_unit: str | None,
         duration: float | None, duration_unit: str | None) -> Derived:
    """Duration over distance, in the units given."""
    if distance is None or duration is None:
        return _missing("pace")
    if distance == 0:
        return Derived("pace", None, None, "distance is zero")
    unit = f"{duration_unit or '?'}/{distance_unit or '?'}"
    return Derived("pace", duration / distance, unit)


def speed(distance: float | None, distance_unit: str | None,
          duration: float | None, duration_unit: str | None) -> Derived:
    """Distance per hour, in the distance unit given."""
    if distance is None or duration is None:
        return _missing("speed")
    if duration == 0:
        return Derived("speed", None, None, "duration is zero")
    factor = _PER_HOUR.get((duration_unit or "").strip().lower())
    if factor is None:
        return Derived("speed", None, None,
                       f"unknown duration unit: {duration_unit}")
    hours = duration / factor
    return Derived("speed", distance / hours, f"{distance_unit or '?'}/hr")


def efficiency(distance: float | None, distance_unit: str | None,
               duration: float | None, duration_unit: str | None,
               heart_rate: float | None, hr_unit: str | None) -> Derived:
    """Speed per hundred heartbeats. The ACL analysis's mph per 100 bpm."""
    if heart_rate is None:
        return _missing("efficiency")
    if heart_rate == 0:
        return Derived("efficiency", None, None, "heart rate is zero")
    base = speed(distance, distance_unit, duration, duration_unit)
    if base.value is None:
        return Derived("efficiency", None, None, base.reason)
    unit = f"{base.unit} per 100 {hr_unit or '?'}"
    return Derived("efficiency", base.value / (heart_rate / 100.0), unit)


def ratio(name: str, a: float | None, unit_a: str | None,
          b: float | None, unit_b: str | None) -> Derived:
    """One measure over another. Dimensionless, and only within one unit."""
    if a is None or b is None:
        return _missing(name)
    if (unit_a or None) != (unit_b or None):
        return Derived(name, None, None,
                       f"incompatible units: {unit_a} and {unit_b}")
    if b == 0:
        return Derived(name, None, None, "denominator is zero")
    return Derived(name, a / b, None)


def percentile(values, p: int, unit: str | None) -> Derived:
    """Nearest rank, no interpolation.

    An interpolated percentile is a number no instrument produced, which is the
    same objection this project raises to converting a unit.
    """
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return Derived(f"p{p}", None, unit, "no values in scope")
    index = max(0, min(len(ordered) - 1,
                       int(round((p / 100.0) * len(ordered) + 0.5)) - 1))
    return Derived(f"p{p}", ordered[index], unit)
