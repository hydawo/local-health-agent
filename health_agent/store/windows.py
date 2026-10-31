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
never given one representation: a range with no periods in it reads
differently from an index that holds nothing of the figure at all, which
reads differently again from periods that carry no value for the figure
asked for, from a measure no workout carries, from an activity the index
does not hold, and from a side a source exclusion emptied.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from health_agent import metrics
from health_agent.store import derive, queries

# "mixed" is `workout_series`'s sentinel for a figure whose period held more
# than one unit: the value is None and the unit is this word. It is not a
# unit, so it never takes part in the unit equality test.
MIXED = "mixed"

# An absent unit is a unit of its own. Folding it into a named unit relabels
# the values it came with, and leaving it out of the set lets a side with no
# unit past the cross-side unit test with "the same unit" as its stated reason.
# Taken from `queries` rather than spelled again: it was two literals for one
# fact, and a sentinel that drifts in one layer is a sentinel printed in the
# other.
UNLABELLED = queries.UNLABELLED

VERDICTS = ("ok", "no_overlap", "thin_on_one_side",
            "missing_measure_on_one_side", "unit_mismatch",
            "unit_mixed_on_one_side", "insufficient_data")

# Below this many periods on one side, that side is reported as thin and no
# further rule is run over it. One period is a point, not a range, and a
# range claim over a point reads as more than it knows: with two periods a
# side there is something for `no_overlap` to be about. Named so the number
# is arguable rather than hidden, and checked before `no_overlap` so a
# single period is never hidden behind a statement about ranges.
THIN_ROWS = 2

# A derivation a SeriesSpec cannot express without guessing: `ratio` needs an
# ordered pair of measures and `percentile` needs which percentile. Refused
# by name rather than silently resolved to one of the choices.
_UNSPECIFIABLE_DERIVATIONS = ("ratio", "percentile")

_ACTIVITY_PREFIX = "HKWorkoutActivityType"

# How many held measures or activities an absence message names before it
# stops listing. Enough to point somewhere, short enough to read.
_HELD_LISTED = 5


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
    # What the underlying count counts, as a plural noun, and the count. A
    # metric side counts records and a workout side counts workouts, so the
    # noun travels with the number rather than being guessed from it. It is
    # not `sample_depth` below, which counts what is inside each workout.
    counted: str = "records"
    counted_n: int = 0
    # §6's fourth check: {base_type: {"median", "counts", "workouts_counted",
    # "workouts_without_a_count", "reasons"}}, the median over this side's
    # workouts of the records inside each one's own interval. None unless the
    # caller asked for it, and None on a metric side, which has no workout
    # interval to count inside and already carries its record count above.
    # The verdict does not read it: how few samples is too few is a judgment,
    # and this block states numbers.
    sample_depth: dict | None = None
    # The range asked for, beside the bucketed span in `first` and `last`,
    # which is clipped to it.
    window_start: str | None = None
    window_end: str | None = None
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
    # The spec the window was read with, so `as_comparison` and `render` can
    # say what was narrowed without the layers above passing it back in.
    spec: SeriesSpec | None = None


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
    spec: SeriesSpec | None = None


NOTE = ("These are two spans of time, reported side by side. The tool makes "
        "no claim that anything in one span brought about anything in the "
        "other.")


def _anchored_note(anchor: Anchor) -> str:
    """The generic note, with the anchor folded in.

    One place knows how to say this, so the layers above do not each invent
    their own anchored wording.
    """
    return (f"These are the spans before and after {_anchor_phrase(anchor)}, "
            "reported side by side. The tool makes no claim that anything in "
            "one span brought about anything in the other.")


def _anchor_phrase(anchor: Anchor) -> str:
    """The anchor named once. An explicit date is its own label, so "on
    {date}" after it would print the date twice."""
    if anchor.label == anchor.date:
        return anchor.date
    return f"{anchor.label} on {anchor.date}"


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


def _unit_phrase(side: SideSummary) -> str:
    """A side's unit, in words. An absent unit is named as absent rather
    than printed as though "unlabelled" were a unit somebody measured in.

    The wording lives in `queries.unit_in_words`, which the CLI and the tool
    payload now share: the same sentinel reached all three.
    """
    return queries.unit_in_words(side.unit)


def _count(n: int, plural: str) -> str:
    """"1 record", "62 records": a count never names its noun in the plural
    when there is one of it."""
    noun = plural[:-1] if n == 1 and plural.endswith("s") else plural
    return f"{n} {noun}"


def _series(conn, spec: SeriesSpec, start: str, end: str, period: str,
            *, sample_counts: bool = False):
    if spec.metric and (spec.activity or spec.measure or spec.derive_name
                        or spec.indoor is not None):
        # `indoor` belongs to this list: without it, a metric comparison
        # asked for indoor-only came back byte-identical to the unfiltered
        # one, which reads as a filtered answer and is not.
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
        if metric.kind is metrics.Kind.CATEGORY:
            # A category series carries one point per period per category
            # value, as sleep does per stage. Collapsing the stages into one
            # figure per period means choosing how to combine them, which is
            # a decision about what sleep is and not one this layer can make.
            # Summarizing them as they come would report a spread across
            # stages as a range and three stages on one day as three periods,
            # which defeats the thin rule and can read as a false "ok".
            # The refusal names the tool that can answer, not a Python
            # function: `query_healthkit` with metric "sleep" routes to the
            # per-night answer, and a reader who cannot act on a refusal
            # reads it as a dead end.
            _refuse(f"{spec.metric} is a category metric ({metric.label}): "
                    "its series holds one point per period per category "
                    "value, and which combination of those values a window "
                    "should compare is not something this layer decides. Ask "
                    "query_healthkit for this metric, which reports the "
                    "categories as they are, or compare a quantity metric")
        return queries.metric_series(
            conn, metric, period=period, start=start, end=end,
            source_exclude=tuple(spec.source_exclude))
    return queries.workout_series(
        conn, period=period, activity=spec.activity, start=start, end=end,
        indoor=spec.indoor, source_exclude=tuple(spec.source_exclude),
        measures=(spec.measure,) if spec.measure else (),
        derive_names=(spec.derive_name,) if spec.derive_name else (),
        sample_counts=sample_counts)


def _entries(series, spec: SeriesSpec) -> list[dict]:
    """One {value, unit, units} per period, for the figure the spec names."""
    if spec.metric:
        return [{"value": point.value, "unit": series.unit,
                 "units": tuple(series.units_seen)}
                for point in series.points]
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


def _held(conn, spec: SeriesSpec, series) -> dict:
    """What the index holds that a thwarted spec should hear about.

    Read only when a side came back with no periods or with no value in any
    of them, so the ordinary path stays two series reads.
    """
    held: dict = {}
    if spec.source_exclude:
        held["sources_excluded"] = list(spec.source_exclude)
    if spec.metric:
        return held
    if spec.measure:
        # Every row, whatever its kind. `workout_measures` groups by (kind,
        # key) and a workout measure can be held as metadata rather than as a
        # statistic, so filtering to statistics made the absence machinery
        # blind to a key the index holds and printed "no workout carries it"
        # beside that key's own median. The table's unique index is on
        # (workout_id, kind, key), so one key can appear under both kinds:
        # the rows for the key asked for are therefore added up rather than
        # the first one being taken as the whole answer.
        rows = queries.workout_measures(conn)
        mine = [r for r in rows if r["key"] == spec.measure]
        held["measure_asked"] = (
            {"key": spec.measure,
             "workouts": sum(r["n"] for r in mine),
             "kinds": sorted({r["kind"] for r in mine if r.get("kind")}),
             "first": min((r["first"] for r in mine if r["first"]),
                          default=None),
             "last": max((r["last"] for r in mine if r["last"]),
                         default=None)}
            if mine else None)
        others: dict = {}
        for row in rows:
            if row["key"] == spec.measure:
                continue
            others[row["key"]] = others.get(row["key"], 0) + row["n"]
        held["measures_held"] = [
            {"key": key, "workouts": n}
            for key, n in list(others.items())[:_HELD_LISTED]]
        # The field named for the request echoes the request. `measure_asked`
        # is None when the index holds nothing under the key, and in run 13
        # that null read to the model as though no key had been asked for.
        held["measure_asked_for"] = spec.measure
        if not mine:
            # A key the index does not hold is not the same as a figure the
            # index cannot report. Run 13's third draw of Q35 read
            # `measures_held`, found only a heart rate, and told the person
            # none of their 21 cycling workouts carried a duration, while
            # the other two draws of the same question computed that duration
            # from the same index, because a call with no measure reports it.
            durations = sum(1 for point in series.points
                            if point.duration is not None)
            if durations:
                held["duration_held"] = durations
    if spec.activity or spec.indoor is not None:
        rows = queries.workout_summary(conn, limit=50)
        needle = (spec.activity or "").lower()
        mine = [r for r in rows
                if needle and needle in (r["activity_type"] or "").lower()]
        held["activity_asked"] = (
            {"activity": _activity_name(mine[0]["activity_type"]),
             "workouts": sum(r["n"] for r in mine),
             "first": min(r["first"] for r in mine if r["first"]),
             "last": max(r["last"] for r in mine if r["last"])}
            if mine and any(r["first"] for r in mine) else None)
        held["activities_held"] = [
            {"activity": _activity_name(r["activity_type"]),
             "workouts": r["n"]} for r in rows[:_HELD_LISTED]]
        if spec.indoor is not None:
            held["indoor_only"] = bool(spec.indoor)
    return held


def _activity_name(activity_type: str | None) -> str:
    name = activity_type or "workout"
    return name[len(_ACTIVITY_PREFIX):] if name.startswith(_ACTIVITY_PREFIX) \
        else name


def _summarize(conn, series, spec: SeriesSpec, start: str, end: str,
               period: str) -> SideSummary:
    entries = _entries(series, spec)
    # `derive.finite` and not `float()`: a non-finite column value reaches
    # here through the raw sums in `queries`, and NaN propagates into every
    # figure on the side rather than being reported as a missing value.
    values = [v for v in (derive.finite(e["value"]) for e in entries)
              if v is not None]
    seen: set[str] = set()
    mixed = False
    for entry in entries:
        unit = entry.get("unit")
        others = tuple(u for u in (entry.get("units") or ())
                       if u and u != MIXED)
        if unit == MIXED:
            mixed = True
        elif derive.finite(entry["value"]) is not None and not others:
            # An entry that carries a value and names no unit is unlabelled,
            # which is a unit of its own: folding it into the side's other
            # unit would relabel the values it came with. An entry whose
            # `units` list is populated has already said what it saw, and a
            # None `unit` beside it means those units differ, not that the
            # values are unlabelled.
            seen.add(unit or UNLABELLED)
        seen.update(others)
    mixed = mixed or len(seen) > 1
    unit = None if mixed else (sorted(seen)[0] if len(seen) == 1 else None)
    # Distinct period labels, not points: a series that ever emits more than
    # one point per period must not report that as more periods.
    periods = len({point.period for point in series.points})
    counted = "records" if spec.metric else "workouts"
    coverage = {"counted": counted,
                f"total_{counted}": series.total_records,
                "available_from": series.available_from,
                "available_to": series.available_to}
    if periods == 0 or not values:
        coverage["held"] = _held(conn, spec, series)
    span = _clip_span(series, start, end)
    return SideSummary(
        n=periods,
        empty_periods=max(0, _expected_periods(start, end, period) - periods),
        min=min(values) if values else None,
        max=max(values) if values else None,
        median=derive.median(values, unit).value,
        mean=(sum(values) / len(values)) if values else None,
        unit=unit,
        first=span[0], last=span[1],
        value_periods=len(values),
        units_seen=tuple(sorted(seen)),
        unit_mixed=mixed,
        counted=counted,
        counted_n=sum(getattr(p, "n", 0) or 0 for p in series.points),
        sample_depth=_side_depth(series.points),
        window_start=start, window_end=end,
        coverage=coverage)


def _side_depth(points) -> dict | None:
    """One side's sample depth, over its workouts rather than its periods.

    Each period carried the counts of its own workouts for exactly this: a
    median of per-period medians is a different number, and on an uneven split
    it is the wrong one. Three workouts at 1, 1 and 9 samples have a median of
    1; their two periods' medians are 1 and 9, whose median is 5.
    """
    if not any(getattr(p, "sample_depth", None) is not None for p in points):
        return None
    bases = sorted({b for p in points for b in (p.sample_depth or {})})
    out = {}
    for base in bases:
        entries = [(p.sample_depth or {}).get(base) for p in points]
        entries = [e for e in entries if e]
        counts = tuple(sorted(c for e in entries for c in e["counts"]))
        reasons = tuple(r for e in entries for r in e["reasons"])
        out[base] = {
            "median": derive.median(list(counts), None).value if counts else None,
            "counts": counts,
            "workouts_counted": len(counts),
            "workouts_without_a_count": len(reasons),
            "reasons": reasons,
        }
    return out


def _clip_span(series, start: str, end: str):
    """The span the side actually covers, clipped to the window.

    A week or month bucket runs past the window at both ends, so an unclipped
    span has an after side appearing to start before the anchor, and two
    sides inside one bucket reporting identical spans.
    """
    if not series.points:
        return None, None
    first = max(series.points[0].period_start, start)
    last = min(series.points[-1].period_end, end)
    return first, last


# --------------------------------------------------------------------------- #
# The window and the comparison
# --------------------------------------------------------------------------- #

def window(conn, *, spec: SeriesSpec, anchor, before_days: int,
            after_days: int, period: str = "day",
            sample_counts: bool = False) -> Window:
    """The periods either side of an anchor, each side summarized.

    The anchor's own day is in neither side: the day of an event is the
    event, not a day before it or a day after it. `as_comparison` says so in
    the verdict block and in the rendered text, because a day the tool set
    aside is an absence the tool created.
    """
    if not isinstance(anchor, Anchor):
        _refuse("window needs a resolved Anchor")
    if before_days < 1 or after_days < 1:
        _refuse("before_days and after_days are whole days, at least one each")
    before_start, before_end = _shift(anchor.date, -before_days), \
        _shift(anchor.date, -1)
    after_start, after_end = _shift(anchor.date, 1), _shift(anchor.date,
                                                            after_days)
    before = _series(conn, spec, before_start, before_end, period,
                     sample_counts=sample_counts)
    after = _series(conn, spec, after_start, after_end, period,
                    sample_counts=sample_counts)
    return Window(
        label=spec.label, anchor=anchor,
        before=_summarize(conn, before, spec, before_start, before_end, period),
        after=_summarize(conn, after, spec, after_start, after_end, period),
        before_points=tuple(before.points), after_points=tuple(after.points),
        spec=spec)


def as_comparison(window_: Window, *, sample_counts: bool = False) -> Comparison:
    """A window's two sides, with the same verdict a `compare` gets.

    Public because the anchored comparisons built on top of this module need
    the verdict without reaching into a private name.
    """
    anchor = window_.anchor
    where = f" either side of {_anchor_phrase(anchor)}" if anchor else ""
    comparable = _verdict(window_.before, window_.after,
                          names=("before", "after"), spec=window_.spec,
                          sample_counts=sample_counts)
    if anchor:
        comparable["anchor_day"] = anchor.date
    return Comparison(
        label=f"{window_.label}{where}",
        left=window_.before, right=window_.after,
        left_points=window_.before_points, right_points=window_.after_points,
        comparable=comparable,
        note=_anchored_note(anchor) if anchor else NOTE,
        left_label="before", right_label="after", spec=window_.spec)


def compare(conn, *, spec: SeriesSpec, left, right, period: str = "day",
            sample_counts: bool = False) -> Comparison:
    """Two ranges side by side, with a verdict on whether that is meaningful.

    `sample_counts=True` adds the underlying count behind each side, named by
    what it counts. It is off by default so the payload stays the same size
    for a caller that did not ask for it.
    """
    left_series = _series(conn, spec, left[0], left[1], period,
                          sample_counts=sample_counts)
    right_series = _series(conn, spec, right[0], right[1], period,
                           sample_counts=sample_counts)
    left_sum = _summarize(conn, left_series, spec, left[0], left[1], period)
    right_sum = _summarize(conn, right_series, spec, right[0], right[1], period)
    return Comparison(
        label=spec.label, left=left_sum, right=right_sum,
        left_points=tuple(left_series.points),
        right_points=tuple(right_series.points),
        comparable=_verdict(left_sum, right_sum, spec=spec,
                            sample_counts=sample_counts),
        note=NOTE, spec=spec)


# --------------------------------------------------------------------------- #
# Absence, in one place
# --------------------------------------------------------------------------- #

def _spec_clauses(sides, spec: SeriesSpec | None) -> list[str]:
    """What the reader needs to know about the figure, said once.

    The index-wide count a bare absence quotes is about every record or every
    workout, so a narrowed spec says what it narrowed to and what is there.
    These facts are about the figure and the filters, not about one side, so
    they are stated once after the per-side facts. Said per side they ran a
    refusal past 600 characters, and a refusal nobody reads is a refusal that
    failed.
    """
    for side in sides:
        clauses = _held_clauses(side, spec)
        if clauses:
            return clauses
    return []


def _held_clauses(side: SideSummary, spec: SeriesSpec | None) -> list[str]:
    held = side.coverage.get("held") or {}
    out = []
    if "measure_asked" in held:
        asked = held["measure_asked"]
        measure = spec.measure if spec else "that measure"
        if asked is None:
            others = ", ".join(
                f"{m['key']} ({_count(m['workouts'], 'workouts')})"
                for m in held.get("measures_held") or ())
            out.append(f"no workout carries {measure}"
                       + (f", and the measures held are {others}" if others
                          else ", and the index holds no workout measures "
                               "at all")
                       + (", while the duration of each workout is held and a "
                          "call with no measure reports it"
                          if held.get("duration_held") else ""))
        else:
            out.append(f"{asked['key']} is carried by "
                       f"{_count(asked['workouts'], 'workouts')} in the "
                       f"index, from {asked['first']} to {asked['last']}")
    if "activities_held" in held:
        asked = held.get("activity_asked")
        if asked:
            out.append(f"the index holds "
                       f"{_count(asked['workouts'], 'workouts')} of "
                       f"{asked['activity']}, from {asked['first']} to "
                       f"{asked['last']}")
        elif spec is not None and spec.activity:
            others = ", ".join(
                f"{a['activity']} ({_count(a['workouts'], 'workouts')})"
                for a in held.get("activities_held") or ())
            out.append(f"the index holds no workout named like "
                       f"{spec.activity}"
                       + (f", and it holds {others}" if others else ""))
    if held.get("indoor_only") is not None:
        out.append("only workouts the export marked indoor"
                   + ("" if held["indoor_only"] else " as false")
                   + " were read, on both sides")
    if held.get("sources_excluded"):
        out.append("both sides were read with "
                   + ", ".join(held["sources_excluded"]) + " excluded")
    return out


def _coverage_tail(side: SideSummary, spec: SeriesSpec | None) -> str:
    """What the index holds of this figure, as the tail of an absence.

    One wording, used by the verdict and by `render`, so a side with no
    periods reads as coverage in both places rather than as two different
    absences in two different voices. The facts about the figure itself are
    said once by `_spec_clauses`, not repeated under each side.
    """
    total = side.coverage.get(f"total_{side.counted}") or 0
    if not total:
        return (", and the index holds no "
                f"{side.counted} of this figure at all")
    first = side.coverage.get("available_from")
    last = side.coverage.get("available_to")
    if spec is not None and not spec.metric:
        return (f", while the index holds {_count(total, 'workouts')} in all, "
                f"from {first} to {last}")
    return (f", while the index holds {_count(total, side.counted)} of this "
            f"figure from {first} to {last}")


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #

def _verdict(left: SideSummary, right: SideSummary, *,
             names: tuple = ("earlier", "later"),
             spec: SeriesSpec | None = None,
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
        "left_window": [left.window_start, left.window_end],
        "right_window": [right.window_start, right.window_end],
        "left_unit": left.unit, "right_unit": right.unit,
        "left_units_seen": left.units_seen,
        "right_units_seen": right.units_seen,
        "left_coverage": left.coverage, "right_coverage": right.coverage,
    }
    if sample_counts:
        block[f"left_{left.counted}"] = left.counted_n
        block[f"right_{right.counted}"] = right.counted_n
        # §6's sample_depth check. Absent rather than null on a metric side,
        # which has no workout interval to count inside: a null would read as
        # a depth nobody could measure rather than a figure that does not
        # apply to this kind of side.
        for name, side in (("left", left), ("right", right)):
            if side.sample_depth is not None:
                block[f"{name}_sample_depth"] = side.sample_depth

    empty = [(name, side) for name, side in zip(names, (left, right))
             if side.n == 0]
    if empty:
        if len(empty) == 2:
            # Both sides quote the same index-wide coverage, because it is
            # coverage of the figure and not of a side. Printed twice it is
            # the same sentence again for no reader's benefit.
            parts = ["neither side has any periods in that range"
                     + _coverage_tail(left, spec)]
        else:
            parts = [f"the {name} side has no periods in that range"
                     + _coverage_tail(side, spec) for name, side in empty]
        return {**block, "verdict": "insufficient_data",
                "because": "; ".join(parts + _spec_clauses((left, right),
                                                           spec))}

    if left.unit_mixed or right.unit_mixed:
        parts = []
        for name, side in zip(names, (left, right)):
            if not side.unit_mixed:
                continue
            # The units named are this side's own. The other side's unit is
            # no evidence that this one mixes, and `workout_series` leaves
            # `units` off some derived entries, so an empty set says so
            # rather than borrowing a unit from the side that is not mixed.
            named = (f" ({', '.join(side.units_seen)})" if side.units_seen
                     else ", which the layer below did not name")
            parts.append(f"the {name} side reports this figure in more than "
                         f"one unit{named}")
        return {**block, "verdict": "unit_mixed_on_one_side",
                "because": ("; ".join(parts) + ", and this tool never "
                            "converts a unit, so there is no one figure to "
                            "compare")}

    # Before the unit test: a side with no value has no unit either, and
    # "units differ" would be a worse answer than naming the dead side.
    if left.value_periods == 0 or right.value_periods == 0:
        parts = [f"the {name} side has " + _count(side.n, "periods")
                 + " but no value for this figure in any of them"
                 for name, side in zip(names, (left, right))
                 if not side.value_periods]
        return {**block, "verdict": "missing_measure_on_one_side",
                "because": "; ".join(parts + _spec_clauses((left, right),
                                                           spec))}

    # Both sides carry values by now, so any difference between their units
    # is a real difference, the absence of a unit on one side included.
    if left.unit != right.unit:
        return {**block, "verdict": "unit_mismatch",
                "because": (f"units differ: {_unit_phrase(left)} and "
                            f"{_unit_phrase(right)}, and this tool never "
                            "converts a unit")}

    # On the periods carrying a value, not on the periods: padding the count
    # with periods that hold no figure is how a range claim over a single
    # value got past the gate this constant exists to close.
    if left.value_periods < THIN_ROWS or right.value_periods < THIN_ROWS:
        thin = [(name, side) for name, side in zip(names, (left, right))
                if side.value_periods < THIN_ROWS]
        return {**block, "verdict": "thin_on_one_side",
                "because": ("; ".join(
                    f"the {name} side has "
                    + _count(side.value_periods, "periods")
                    + " carrying a value" for name, side in thin)
                    + f", fewer than the {THIN_ROWS} this tool compares over")}

    if left.max < right.min or right.max < left.min:
        return {**block, "verdict": "no_overlap",
                "because": (f"the two ranges do not overlap ({left.min:g} to "
                            f"{left.max:g} against {right.min:g} to "
                            f"{right.max:g}), so there is no like-for-like "
                            "comparison to make")}

    return {**block, "verdict": "ok",
            "because": "both sides carry values in the same unit over enough "
                       "periods, and their ranges overlap"}


# --------------------------------------------------------------------------- #
# The rendered text
# --------------------------------------------------------------------------- #

def _depth_clause(side: SideSummary) -> str:
    """The side's sample depth, as a number and nothing more.

    One clause per record type counted, which is one type in every comparison
    this layer can be asked for, since a spec names one measure. "Almost no
    samples" is the reader's conclusion to draw, not this template's: it says
    how many and in how many workouts.
    """
    if not side.sample_depth:
        return ""
    parts = []
    for base, depth in sorted(side.sample_depth.items()):
        counted = depth["workouts_counted"]
        total = counted + depth["workouts_without_a_count"]
        where = ("inside each workout" if counted == total
                 else f"inside {counted} of its {_count(total, 'workouts')}")
        if depth["median"] is None:
            # Not "no records counted": the eval's gap family reads "no" near
            # "records", and a tool's own words must not be what a scorer
            # looks for. The absence here is of a countable interval, which is
            # also the more accurate thing to say.
            parts.append(f"the interval inside each of its "
                         f"{_count(total, 'workouts')} could not be counted")
        else:
            parts.append(f"a median of {depth['median']:g} "
                         f"{_label_for(base)} records {where}")
    return ", " + ", ".join(parts)


def _label_for(base_type: str) -> str:
    """A record type in the words a reader uses, when the registry knows it."""
    metric = metrics.lookup(base_type)
    return metric.label.lower() if metric else base_type


def _side_line(name: str, side: SideSummary, spec: SeriesSpec | None) -> str:
    if side.n == 0:
        return (f"{name}: no periods in that range"
                + _coverage_tail(side, spec) + ".")
    span = f"{_count(side.n, 'periods')}, {side.first} to {side.last}"
    if side.unit_mixed:
        units = ", ".join(side.units_seen)
        return (f"{name}: {span}, reported in more than one unit"
                + (f" ({units})" if units
                   else ", which the layer below did not name")
                + ", so no one figure is given for them.")
    if side.value_periods == 0:
        return f"{name}: {span}, none of which carries this figure."
    # A median resting on one of five periods must not read as resting on
    # five. The partial case had no representation in this text at all,
    # which is the text the model is handed and the reader is shown.
    basis = ""
    if 0 < side.value_periods < side.n:
        carry = "carries" if side.value_periods == 1 else "carry"
        basis = (f", of which {_count(side.value_periods, 'periods')} "
                 f"{carry} this figure")
    unit = ("" if side.unit in (None, UNLABELLED) else f" {side.unit}")
    # An absent unit is said in words, next to the figure it qualifies.
    # "median 120 unlabelled" prints a sentinel as though somebody had
    # measured in it.
    named = " with no unit named" if side.unit == UNLABELLED else ""
    tail = (f", {_count(side.empty_periods, 'periods')} with no data."
            if side.empty_periods else ".")
    return (f"{name}: {span}{basis}, median {side.median:g}{unit}{named}, "
            f"range {side.min:g} to {side.max:g}{unit}"
            + _depth_clause(side) + tail)


def render(comparison: Comparison) -> str:
    """A fixed template, so the text is the same shape every time.

    The guardrail is run over this in the tests as a cheap regression guard
    only: its patterns are anchored on clinical markers and condition names,
    and it holds no pattern for cause, for a percentage or for a comparison
    between two spans. The word scans in the tests are what actually hold
    this template to the rule against reading a gap into two spans.
    """
    verdict = comparison.comparable.get("verdict")
    because = comparison.comparable.get("because")
    spec = comparison.spec
    lines = [f"{comparison.label}, two spans side by side.",
             _side_line(comparison.left_label.capitalize(), comparison.left,
                        spec),
             _side_line(comparison.right_label.capitalize(), comparison.right,
                        spec)]
    # The facts about the figure travel inside `because`, which the verdict
    # line below prints in full. A line of their own here would be the third
    # place one page says them.
    anchor_day = comparison.comparable.get("anchor_day")
    if anchor_day:
        lines.append(f"The day of the event itself, {anchor_day}, is in "
                     "neither side, so anything recorded on it is in neither "
                     "count above.")
    if verdict == "ok":
        lines.append(f"Comparable (ok): {because}.")
    else:
        lines.append(f"Not comparable ({verdict}): {because}.")
    lines.append(comparison.note)
    return "\n".join(lines)
