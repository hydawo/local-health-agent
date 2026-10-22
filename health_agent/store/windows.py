"""Windows either side of a dated event, and whether two of them can be
compared at all.

Composes over `queries.py` and holds no SQL of its own, the anchor lookup
included: that goes through `queries.clinical_facts` as well, so the one
read path holds and the command line and the model's tools reach these
numbers through the same functions.

Two windows either side of a date are two windows either side of a date.
This module reports both, reports whether they are comparable, and never
computes the gap between them. The moment a tool returns "32% worse" it has
made a claim its citation cannot carry.

Every refusal carries the numbers that produced it, and two absences are
never given one representation: a range with no rows in it reads differently
from an index that holds nothing of the figure at all, and a period that
carries no value for the figure asked for reads differently from both.
"""
from __future__ import annotations

import datetime as _dt
import statistics
from dataclasses import dataclass, field

from health_agent import metrics
from health_agent.store import queries

# "mixed" is `workout_series`'s sentinel for a figure whose period held more
# than one unit: the value is None and the unit is this word. It is not a
# unit, so it never takes part in the unit equality test.
MIXED = "mixed"

VERDICTS = ("ok", "no_overlap", "thin_on_one_side",
            "missing_measure_on_one_side", "unit_mismatch",
            "unit_mixed_on_one_side", "insufficient_data")

# Below this many periods on one side, two sides whose ranges do overlap are
# still not worth comparing. Named so the number is arguable rather than
# hidden. Checked after `no_overlap`, which is the more specific refusal.
THIN_ROWS = 3

# A derivation a SeriesSpec cannot express without guessing: `ratio` needs an
# ordered pair of measures and `percentile` needs which percentile. Refused
# by name rather than silently resolved to one of the choices.
_UNSPECIFIABLE_DERIVATIONS = ("ratio", "percentile")


@dataclass(frozen=True)
class SeriesSpec:
    """What figure a window is of: one metric, or one workout figure."""
    metric: str | None = None
    activity: str | None = None
    measure: str | None = None
    derive_name: str | None = None
    source_exclude: tuple = ()
    indoor: bool | None = None

    @property
    def label(self) -> str:
        if self.metric:
            return self.metric
        parts = [self.activity or "all workouts"]
        if self.measure:
            parts.append(self.measure)
        if self.derive_name:
            parts.append(self.derive_name)
        if not self.measure and not self.derive_name:
            parts.append("duration")
        return " ".join(parts)


@dataclass(frozen=True)
class Anchor:
    date: str
    label: str
    citation: str | None = None


@dataclass(frozen=True)
class AnchorAmbiguous:
    """Every candidate, with its date, and no choice made."""
    candidates: tuple


@dataclass(frozen=True)
class SideSummary:
    n: int
    empty_periods: int
    min: float | None
    max: float | None
    median: float | None
    mean: float | None
    unit: str | None
    first: str | None
    last: str | None
    # Periods that carry a value for the figure asked for. Fewer than `n`
    # when the periods exist and the figure does not.
    value_periods: int = 0
    # Every unit seen on this side, and whether the layer below refused the
    # figure because they differ. `unit` is None whenever this is True.
    units_seen: tuple = ()
    unit_mixed: bool = False
    # Underlying records behind the periods, reported only when asked for.
    samples: int = 0
    # What the index holds of this figure overall, so an empty side is
    # reported as coverage rather than as an empty record.
    coverage: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Window:
    label: str
    anchor: Anchor | None
    before: SideSummary
    after: SideSummary
    before_points: tuple = ()
    after_points: tuple = ()


@dataclass(frozen=True)
class Comparison:
    label: str
    left: SideSummary
    right: SideSummary
    left_points: tuple = ()
    right_points: tuple = ()
    comparable: dict = field(default_factory=dict)
    note: str = ""
    left_label: str = "earlier"
    right_label: str = "later"


NOTE = ("These are two spans of time, reported side by side. The tool makes "
        "no claim that anything in one span brought about anything in the "
        "other.")


# --------------------------------------------------------------------------- #
# The anchor
# --------------------------------------------------------------------------- #

def resolve_anchor(conn, *, date: str | None = None, fact: str | None = None):
    """An explicit date, or a dated row the index already holds.

    More than one date among the matches returns every candidate and chooses
    nothing. Matches that all name one date are one anchor: the window is the
    same either way, so there is nothing to choose.

    Returns None when a search found nothing. Asking for nothing at all is a
    caller error, not an absence in the data, so it raises instead.
    """
    if date and fact:
        return _refuse("give a date or a fact to look up, not both")
    if date:
        _dt.date.fromisoformat(date)  # a bad date fails here, not downstream
        return Anchor(date=date, label=date)
    if fact is None:
        return _refuse("resolve_anchor needs a date or a fact to look up")
    needle = fact.strip().lower()
    if not needle:
        return _refuse("resolve_anchor needs a date or a fact to look up")
    found = [row for row in queries.clinical_facts(conn)
             if needle in (row.get("name") or "").lower()
             and row.get("onset_date")]
    if not found:
        return None
    dates = sorted({row["onset_date"] for row in found})
    if len(dates) > 1:
        return AnchorAmbiguous(candidates=tuple(
            {"name": row["name"], "kind": row["kind"],
             "date": row["onset_date"], "citation": row.get("citation")}
            for row in sorted(found, key=lambda r: (r["onset_date"],
                                                    r["name"]))))
    rows = sorted(found, key=lambda r: r["name"])
    names = sorted({f"{r['name']} ({r['kind']})" for r in rows})
    citations = sorted({r["citation"] for r in rows if r.get("citation")})
    return Anchor(date=dates[0], label="; ".join(names),
                  citation="; ".join(citations) or None)


def _refuse(message: str):
    raise ValueError(message)


# --------------------------------------------------------------------------- #
# One side of a window
# --------------------------------------------------------------------------- #

def _shift(date: str, days: int) -> str:
    return (_dt.date.fromisoformat(date) + _dt.timedelta(days=days)).isoformat()


def _series(conn, spec: SeriesSpec, start: str, end: str, period: str):
    if spec.metric and (spec.activity or spec.measure or spec.derive_name):
        _refuse("a spec names a metric or a workout figure, not both")
    if spec.derive_name in _UNSPECIFIABLE_DERIVATIONS:
        _refuse(f"{spec.derive_name} needs more than a SeriesSpec can carry: "
                "ask for it through queries.workout_series, which takes the "
                "pair or the percentile by name")
    if spec.metric:
        # `lookup` returns None for an unknown name; `resolve` would invent an
        # unregistered Metric and the window would come back silently empty.
        metric = metrics.lookup(spec.metric)
        if metric is None:
            suggestions = [m.aliases[0] if m.aliases else m.identifier
                           for m in metrics.suggest(spec.metric)]
            _refuse(f"unknown metric: {spec.metric}"
                    + (f"; did you mean {', '.join(suggestions)}?"
                       if suggestions else ""))
        return queries.metric_series(
            conn, metric, period=period, start=start, end=end,
            source_exclude=tuple(spec.source_exclude))
    return queries.workout_series(
        conn, period=period, activity=spec.activity, start=start, end=end,
        indoor=spec.indoor, source_exclude=tuple(spec.source_exclude),
        measures=(spec.measure,) if spec.measure else (),
        derive_names=(spec.derive_name,) if spec.derive_name else ())


def _entries(series, spec: SeriesSpec) -> list[dict]:
    """One {value, unit, units} per period, for the figure the spec names."""
    if spec.metric:
        return [{"value": point.value, "unit": series.unit,
                 "units": series.units_seen} for point in series.points]
    out = []
    for point in series.points:
        if spec.derive_name:
            entry = point.derived.get(spec.derive_name) or {}
            out.append({"value": entry.get("value"), "unit": entry.get("unit"),
                        "units": tuple(entry.get("units") or ())})
        elif spec.measure:
            entry = point.measures.get(spec.measure) or {}
            out.append({"value": entry.get("median"), "unit": entry.get("unit"),
                        "units": tuple(entry.get("units") or ())})
        else:
            out.append({"value": point.duration, "unit": point.duration_unit,
                        "units": ()})
    return out


def _expected_periods(start: str, end: str, period: str) -> int:
    """The periods the range covers, by the same bucketing the series uses."""
    first = _dt.date.fromisoformat(start)
    last = _dt.date.fromisoformat(end)
    if last < first:
        return 0
    labels = set()
    day = first
    while day <= last:
        labels.add(queries.bucket(day.isoformat(), period)[0])
        day += _dt.timedelta(days=1)
    return len(labels)


def _summarize(series, spec: SeriesSpec, start: str, end: str,
               period: str) -> SideSummary:
    entries = _entries(series, spec)
    values = [float(e["value"]) for e in entries if e["value"] is not None]
    seen: set[str] = set()
    mixed = False
    for entry in entries:
        unit = entry.get("unit")
        if unit == MIXED:
            mixed = True
        elif unit:
            seen.add(unit)
        for other in entry.get("units") or ():
            if other and other != MIXED:
                seen.add(other)
    mixed = mixed or len(seen) > 1
    unit = None if mixed else (sorted(seen)[0] if len(seen) == 1 else None)
    return SideSummary(
        n=len(series.points),
        empty_periods=max(0, _expected_periods(start, end, period)
                          - len(series.points)),
        min=min(values) if values else None,
        max=max(values) if values else None,
        median=statistics.median(values) if values else None,
        mean=statistics.fmean(values) if values else None,
        unit=unit,
        first=series.points[0].period_start if series.points else None,
        last=series.points[-1].period_end if series.points else None,
        value_periods=len(values),
        units_seen=tuple(sorted(seen)),
        unit_mixed=mixed,
        samples=sum(getattr(p, "n", 0) or 0 for p in series.points),
        coverage={"total_records": series.total_records,
                  "available_from": series.available_from,
                  "available_to": series.available_to})


# --------------------------------------------------------------------------- #
# The window and the comparison
# --------------------------------------------------------------------------- #

def window(conn, *, spec: SeriesSpec, anchor, before_days: int,
           after_days: int, period: str = "day") -> Window:
    """The periods either side of an anchor, each side summarized.

    The anchor's own day is in neither side: the day of an event is the
    event, not a day before it or a day after it.
    """
    if not isinstance(anchor, Anchor):
        _refuse("window needs a resolved Anchor")
    if before_days < 1 or after_days < 1:
        _refuse("before_days and after_days are whole days, at least one each")
    before_start, before_end = _shift(anchor.date, -before_days), \
        _shift(anchor.date, -1)
    after_start, after_end = _shift(anchor.date, 1), _shift(anchor.date,
                                                            after_days)
    before = _series(conn, spec, before_start, before_end, period)
    after = _series(conn, spec, after_start, after_end, period)
    return Window(
        label=spec.label, anchor=anchor,
        before=_summarize(before, spec, before_start, before_end, period),
        after=_summarize(after, spec, after_start, after_end, period),
        before_points=tuple(before.points), after_points=tuple(after.points))


def as_comparison(window_: Window, *, sample_counts: bool = False) -> Comparison:
    """A window's two sides, with the same verdict a `compare` gets.

    Public because the anchored comparisons built on top of this module need
    the verdict without reaching into a private name.
    """
    anchor = window_.anchor
    where = f" either side of {anchor.label} on {anchor.date}" if anchor else ""
    return Comparison(
        label=f"{window_.label}{where}",
        left=window_.before, right=window_.after,
        left_points=window_.before_points, right_points=window_.after_points,
        comparable=_verdict(window_.before, window_.after,
                            names=("before", "after"),
                            sample_counts=sample_counts),
        note=NOTE, left_label="before", right_label="after")


def compare(conn, *, spec: SeriesSpec, left, right, period: str = "day",
            sample_counts: bool = False) -> Comparison:
    """Two ranges side by side, with a verdict on whether that is meaningful.

    `sample_counts=True` adds the underlying record count behind each side.
    It is off by default so the payload stays the same size for a caller that
    did not ask for it.
    """
    left_series = _series(conn, spec, left[0], left[1], period)
    right_series = _series(conn, spec, right[0], right[1], period)
    left_sum = _summarize(left_series, spec, left[0], left[1], period)
    right_sum = _summarize(right_series, spec, right[0], right[1], period)
    return Comparison(
        label=spec.label, left=left_sum, right=right_sum,
        left_points=tuple(left_series.points),
        right_points=tuple(right_series.points),
        comparable=_verdict(left_sum, right_sum, sample_counts=sample_counts),
        note=NOTE)


def _absence(name: str, side: SideSummary) -> str:
    total = side.coverage.get("total_records") or 0
    if not total:
        return (f"the {name} side has no data in its range, and the index "
                "holds nothing of this figure at all")
    return (f"the {name} side has no data in its range, while the index holds "
            f"{total} records of this figure from "
            f"{side.coverage.get('available_from')} to "
            f"{side.coverage.get('available_to')}")


def _verdict(left: SideSummary, right: SideSummary, *,
             names: tuple = ("earlier", "later"),
             sample_counts: bool = False) -> dict:
    """Named rules, in order. Every verdict carries the numbers behind it."""
    block = {
        "left_n": left.n, "right_n": right.n,
        "left_value_periods": left.value_periods,
        "right_value_periods": right.value_periods,
        "left_empty_periods": left.empty_periods,
        "right_empty_periods": right.empty_periods,
        "left_range": [left.min, left.max] if left.value_periods else None,
        "right_range": [right.min, right.max] if right.value_periods else None,
        "left_span": [left.first, left.last] if left.n else None,
        "right_span": [right.first, right.last] if right.n else None,
        "left_unit": left.unit, "right_unit": right.unit,
        "left_units_seen": left.units_seen,
        "right_units_seen": right.units_seen,
        "left_coverage": left.coverage, "right_coverage": right.coverage,
    }
    if sample_counts:
        block["left_samples"] = left.samples
        block["right_samples"] = right.samples

    empty = [(name, side) for name, side in zip(names, (left, right))
             if side.n == 0]
    if empty:
        if (len(empty) == 2
                and not (left.coverage.get("total_records") or 0)
                and not (right.coverage.get("total_records") or 0)):
            because = ("neither side has data in its range, and the index "
                       "holds nothing of this figure at all")
        else:
            because = "; ".join(_absence(name, side) for name, side in empty)
        return {**block, "verdict": "insufficient_data", "because": because}

    if left.unit_mixed or right.unit_mixed:
        which = [name for name, side in zip(names, (left, right))
                 if side.unit_mixed]
        units = sorted(set(left.units_seen) | set(right.units_seen))
        return {**block, "verdict": "unit_mixed_on_one_side",
                "because": (f"the {' and '.join(which)} side reports this "
                            f"figure in more than one unit "
                            f"({', '.join(units) or 'none named'}), and this "
                            "tool never converts a unit, so there is no one "
                            "figure to compare")}

    if left.unit and right.unit and left.unit != right.unit:
        return {**block, "verdict": "unit_mismatch",
                "because": f"units differ: {left.unit} and {right.unit}, and "
                           "this tool never converts a unit"}

    if left.value_periods == 0 or right.value_periods == 0:
        which = [name for name, side in zip(names, (left, right))
                 if side.value_periods == 0]
        return {**block, "verdict": "missing_measure_on_one_side",
                "because": (f"the {' and '.join(which)} side has periods but "
                            "no value for this figure in any of them")}

    if left.max < right.min or right.max < left.min:
        return {**block, "verdict": "no_overlap",
                "because": (f"the two ranges do not overlap ({left.min:g} to "
                            f"{left.max:g} against {right.min:g} to "
                            f"{right.max:g}), so there is no like-for-like "
                            "comparison to make")}

    if left.n < THIN_ROWS or right.n < THIN_ROWS:
        which = [name for name, side in zip(names, (left, right))
                 if side.n < THIN_ROWS]
        return {**block, "verdict": "thin_on_one_side",
                "because": (f"fewer than {THIN_ROWS} periods on the "
                            f"{' and '.join(which)} side")}

    return {**block, "verdict": "ok",
            "because": "both sides carry values in the same unit over enough "
                       "periods, and their ranges overlap"}


# --------------------------------------------------------------------------- #
# The rendered text
# --------------------------------------------------------------------------- #

def _side_line(name: str, side: SideSummary) -> str:
    if side.n == 0:
        total = side.coverage.get("total_records") or 0
        if not total:
            return (f"{name}: no periods in that range, and nothing of this "
                    "figure anywhere in the index.")
        return (f"{name}: no periods in that range. The index holds {total} "
                f"records of this figure, from "
                f"{side.coverage.get('available_from')} to "
                f"{side.coverage.get('available_to')}.")
    span = f"{side.n} periods, {side.first} to {side.last}"
    if side.unit_mixed:
        units = ", ".join(side.units_seen) or "more than one"
        return (f"{name}: {span}, reported in more than one unit ({units}), "
                "so no one figure is given for them.")
    if side.value_periods == 0:
        return f"{name}: {span}, none of which carries this figure."
    unit = f" {side.unit}" if side.unit else ""
    tail = (f", {side.empty_periods} periods with no data."
            if side.empty_periods else ".")
    return (f"{name}: {span}, median {side.median:g}{unit}, range "
            f"{side.min:g} to {side.max:g}{unit}" + tail)


def render(comparison: Comparison) -> str:
    """A fixed template, so the guardrail can be run over it as a test oracle."""
    verdict = comparison.comparable.get("verdict")
    because = comparison.comparable.get("because")
    lines = [f"{comparison.label}, two spans side by side.",
             _side_line(comparison.left_label.capitalize(), comparison.left),
             _side_line(comparison.right_label.capitalize(), comparison.right)]
    if verdict == "ok":
        lines.append(f"Comparable (ok): {because}.")
    else:
        lines.append(f"Not comparable ({verdict}): {because}.")
    lines.append(comparison.note)
    return "\n".join(lines)
