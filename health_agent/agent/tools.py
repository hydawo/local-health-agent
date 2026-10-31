"""The agent's tool contract.

Seven tools; the first three are from plan §4: `query_healthkit`, `search_records`,
`get_lab_trend`. Each is a thin adapter over `store/queries.py` — the same
functions the CLI calls — so the model and `health-agent metric` cannot report
different numbers for the same question. A tool that reimplemented its own
aggregation would be a second source of truth, and the two would drift.

**Context budget (plan §4).** A single question can touch several tools, and
raw results are unbounded: a daily step series over three years is 1,100 points,
and a vector search can return whole pages of a PDF. Every tool here caps what it
returns *to the model*, separately from what it queried, and says so in the
payload when it truncates. The caps are deliberately small — a model reasoning
about a trend needs its shape and endpoints, not every sample — and the
`truncated` flag exists so the model can say "I looked at the last 60 days"
rather than silently answering from a slice it didn't know was a slice.

**Tools return data, never prose.** Each returns JSON with values and citations
attached. Phrasing, hedging, and the decision about what is worth saying belong
to the model; deciding what is *true* belongs here.

**Absence is data.** When a query finds nothing, the tools return the coverage
that does exist (`available_from`, `available_to`, `total_records`) instead of an
empty result. Without that, a model asked about a period it has no data for will
reach for the nearest numbers it can see and present them as an answer. This is
the mechanism behind the missing-data behavior in plan §5a.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .. import labs, metrics
from ..literature import tiers as lit_tiers
from ..logging_setup import get_logger
from ..store import queries, vector_store, windows

log = get_logger("agent.tools")

# --- Context budget caps ---------------------------------------------------- #
# Points returned from a time series. 60 covers two months daily, a year weekly,
# or five years monthly — enough to describe a trend without flooding context.
MAX_SERIES_POINTS = 60
# Per side of a window comparison. Over this the side's summary still covers
# every period; only the listed points are cut, and the payload says so.
MAX_COMPARE_POINTS = 60
# Lab results per analyte. Someone with a decade of annual panels has ~10.
MAX_LAB_POINTS = 40
# Clinical facts and clinician-measured vitals returned per call.
MAX_CLINICAL_FACTS = 200
MAX_CLINICAL_VITALS = 200
# Search snippets, and characters per snippet. Five chunks of ~600 chars is
# roughly 800 tokens, which leaves room for the other tools in one turn.
MAX_SEARCH_RESULTS = 8
MAX_SNIPPET_CHARS = 600
# Literature findings per call. Deliberately smaller than MAX_SEARCH_RESULTS:
# each finding carries a citation, a tier, and a year alongside its text, so
# five is already a substantial share of one turn's context.
MAX_LITERATURE_FINDINGS = 5
MAX_FINDING_CHARS = 700


@dataclass
class ToolContext:
    """Everything a handler needs, passed in rather than imported.

    `embedder_factory` is a callable rather than an embedder so that semantic
    search degrades to keyword search when Ollama has no embedding model, without
    the tool layer needing to know anything about embedding backends.
    """

    conn: Any
    vector_path: Any
    embedder_factory: Callable[[], Any] | None = None
    literature_conn: Any | None = None
    literature_vector_path: Any | None = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[ToolContext, dict], dict]

    def schema(self) -> dict:
        """The tool definition in the shape Ollama's /api/chat expects."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# --------------------------------------------------------------------------- #
# query_healthkit
# --------------------------------------------------------------------------- #

def _query_healthkit(ctx: ToolContext, args: dict) -> dict:
    name = (args.get("metric") or "").strip()
    if not name:
        return {"error": "metric is required"}

    metric = metrics.resolve(name)
    if metric.identifier == "HKCategoryTypeIdentifierSleepAnalysis":
        return _query_sleep(ctx, args)
    period = (args.get("period") or "day").lower()
    if period not in queries.PERIODS:
        period = "day"
    aggregation = (args.get("aggregation") or "").lower() or None
    if aggregation and aggregation not in queries.AGGREGATIONS:
        aggregation = None

    try:
        result = queries.metric_series(
            ctx.conn, metric, period=period, agg=aggregation,
            start=args.get("start_date"), end=args.get("end_date"),
            source=args.get("source"),
        )
    except ValueError as exc:
        return {"error": str(exc)}

    if result.is_empty:
        # Absence reported as coverage, not as an empty list. See module docstring.
        payload = {
            "metric": metric.identifier,
            "label": metric.label,
            "points": [],
            "no_data_in_range": True,
            "total_records_for_metric": result.total_records,
            "available_from": result.available_from,
            "available_to": result.available_to,
        }
        if not result.total_records:
            payload["note"] = (
                f"There is no {metric.label} data in this index at all. Do not "
                f"substitute a related metric; say it is missing and ask whether "
                f"the user tracks it."
            )
        else:
            payload["note"] = (
                f"{metric.label} exists but not in the requested window. Say so "
                f"and give the range that is available; do not answer with data "
                f"from a different period as though it covered this one."
            )
        return payload

    points = result.points
    truncated = False
    if len(points) > MAX_SERIES_POINTS:
        points = points[-MAX_SERIES_POINTS:]
        truncated = True

    payload: dict = {
        "metric": metric.identifier,
        "label": metric.label,
        "aggregation": result.agg,
        "period": result.period,
        "unit": result.unit,
        # The aggregate over the whole requested range, computed here so the
        # model never has to. Measured before this existed: asked for an average
        # resting heart rate, the model summed seven daily values by hand and
        # returned 59.6 where the answer is exactly 60.0. Language models are
        # unreliable arithmetic engines, and a wrong number delivered fluently is
        # this project's worst failure mode — so any figure the model might
        # otherwise compute is precomputed and handed to it.
        "overall": _overall(result.points, result.agg),
        "points": [
            {"period": p.period, "value": p.value, "samples": p.n,
             **({"category": p.category} if p.category else {})}
            for p in points
        ],
        "available_from": result.available_from,
        "available_to": result.available_to,
    }
    if truncated:
        payload["truncated"] = True
        payload["truncation_note"] = (
            f"Showing the most recent {MAX_SERIES_POINTS} of "
            f"{len(result.points)} periods. Say that the answer covers this "
            f"window rather than the whole history."
        )
    if result.multi_source_periods and not args.get("source"):
        payload["multi_source_warning"] = (
            f"{result.multi_source_periods} period(s) had more than one recording "
            f"device ({', '.join(result.sources_seen)}). Values shown take the "
            f"largest single source rather than adding devices together, because "
            f"summing overlapping devices double-counts. Mention this if the "
            f"total matters."
        )
    if len(result.units_seen) > 1:
        # Said in words, because one of these can be the unlabelled sentinel
        # and "Mixed units: count/min, unlabelled" offers the model a unit to
        # report.
        named = ", ".join(queries.unit_in_words(u) for u in result.units_seen)
        payload["unit_warning"] = (
            f"Mixed units in this range: {named}. Values are not converted."
        )
    # The sentinel never leaves this function as a unit. `queries` reports an
    # absent unit as the string "unlabelled" rather than borrowing a
    # neighbour's, which is the honest choice; handing that string to the model
    # as `unit` is not, because it writes "60.5 unlabelled". `windows` had
    # already learned this for its rendered text. The note rather than the
    # tool's description does the work, which is where every instruction in
    # this project that had to hold every time ended up.
    if payload.get("unit") == queries.UNLABELLED:
        payload["unit"] = None
        payload["unit_note"] = UNIT_NOTE
    return payload


# Every lab and document result carries a `citation`, and the system prompt
# already says to reproduce it. Eval run 10 showed that is not enough: Q20
# printed a Source column naming the PDF and page while Q29 asked almost the
# same question of the same tool and left it out, so `source named` was
# measuring luck at 23/37. In this project an instruction inside the payload
# has held where a prompt rule alone has not: the `no_data` note, the unit
# warning and the OCR warning all ride with the data, and `gap named` is a
# full column. This puts the citation rule where those live.

# Where a citation sits, which the note above left open and the guardrail did
# not. Run 11's guardrail fired on all three draws of Q22, having fired 0/37 in
# run 10: this note asks for a citation "at least once for each report", while
# `guardrail._uncited_flags` wants the PMID in the same unit as the claim. An
# answer that followed the note carried its citation one sentence away and was
# flagged for an uncited claim. Three faithful shapes flagged and only a table
# row passed, because a row is one unit. Widening the guard was the
# alternative and was rejected: one real citation would then license every
# recalled claim beside it, which is the hazard the unit split exists for. So
# the instruction moved to meet the guard. See ROADMAP #16 and
# tests/test_guardrail.py.
SAME_SENTENCE_RULE = (
    "A sentence that states a general rule rather than this person's own "
    "value, such as a threshold, a diagnostic criterion, a \"normal\" range, "
    "or what the research found, carries its citation inside that same "
    "sentence. A citation in a neighbouring sentence does not vouch for it. "
    "In a table, the row is the sentence."
)

# Said once, because both tools that can hand back a unitless figure have to
# say the same thing about it. The honest report of an absent unit is words,
# never the sentinel `queries` carries it as internally.
UNIT_NOTE = (
    "These records name no unit: the export did not carry one for them. The "
    "figures above are therefore unitless. Say that the records name no unit, "
    "and never supply one."
)


CITATION_NOTE = (
    "Every result above carries a `citation`. Reproduce it verbatim at least "
    "once for each report you draw on; in a table, give it a column. A date "
    "on its own is not a citation. " + SAME_SENTENCE_RULE
)

# The literature tool's own note. A module-level constant rather than a literal
# inside the payload so that the one rule about where a citation sits can be
# asserted in every place that teaches it; the guardrail reads PMIDs, so this
# is the note that governs the sentences it flags.
LITERATURE_NOTE = (
    "These are findings about populations from published research, not "
    "facts about this person. State each one separately with its "
    "evidence tier and its year \u2014 'a 2019 meta-analysis found...' \u2014 and "
    "cite it with its PMID, so the reader can check it at the source. "
    "Do not merge several findings into a single conclusion, "
    "and do not turn any of them into a recommendation about what this "
    "person should do. " + SAME_SENTENCE_RULE
)


# --------------------------------------------------------------------------- #
# get_lab_trend
# --------------------------------------------------------------------------- #

def _query_sleep(ctx: ToolContext, args: dict) -> dict:
    """Sleep, attributed to nights rather than calendar dates.

    A night's sleep spans midnight: the in-bed segment lands on one date and the
    deep and REM stages on the next. Answering "how much deep sleep on the night
    of the 4th" from calendar dates returns nothing for the 4th — which the
    model then reports as an absence of deep-sleep data. Grouping by night
    (noon boundary) is what the question actually means.
    """
    start, end = args.get("start_date"), args.get("end_date")
    nights = queries.sleep_nights(ctx.conn, start=start, end=end)

    if not nights:
        total, first, last = queries.sleep_coverage(ctx.conn)
        return {
            "metric": "sleep",
            "nights": [],
            "no_data_in_range": True,
            "total_sleep_records": total,
            "available_from": first,
            "available_to": last,
            "note": ("No sleep in that range. Nights are labelled by the "
                     "evening they began, so the night of the 4th includes "
                     "segments recorded after midnight on the 5th."),
        }

    trimmed = nights[-MAX_SERIES_POINTS:]
    return {
        "metric": "sleep",
        "attribution": ("Each night is labelled by the evening it began; "
                        "segments after midnight count toward the night "
                        "before."),
        "nights": [
            {
                "night_of": n["night"],
                "asleep_hours": round(n["asleep_seconds"] / 3600, 2),
                "in_bed_hours": round(n["in_bed_seconds"] / 3600, 2),
                "stages_hours": {stage: round(seconds / 3600, 2)
                                 for stage, seconds in sorted(n["stages"].items())},
                "sources": n["sources"],
            }
            for n in trimmed
        ],
        "truncated": len(nights) > len(trimmed) or None,
    }


_DETAIL_ONLY_ARGS = ("activity", "period", "measures", "derive", "indoor",
                     "exclude_source", "samples", "ratio_of", "percentile_of")


def _get_workouts(ctx: ToolContext, args: dict) -> dict:
    if args.get("detail") in (True, "true", "True"):
        try:
            return _get_workouts_detail(ctx, args)
        except ValueError as exc:
            log.warning("tool get_workouts refused (ValueError)")
            return {"error": "invalid_request", "note": str(exc)}
        except (TypeError, OverflowError) as exc:
            log.warning("tool get_workouts refused (%s)", type(exc).__name__)
            return {"error": "invalid_request",
                    "note": "an argument was not the type expected: limit is "
                            "a whole number, measures and derive are lists, "
                            "dates are YYYY-MM-DD"}
    result = _get_workouts_plain(ctx, args)
    # An argument that only applies with detail, passed without it, must not
    # come back looking like a filtered answer. Old callers never passed them.
    # `indoor` is tested with `is not None`: False means outdoor only, which
    # is a filter, and reading it as unset returned every workout, indoor
    # ones included, with nothing saying the filter had been dropped.
    ignored = [k for k in _DETAIL_ONLY_ARGS
               if (args.get(k) is not None if k == "indoor"
                   else args.get(k) not in (None, "", [], False))]
    if ignored:
        result["ignored_arguments"] = ignored
        result["ignored_note"] = (
            f"{', '.join(ignored)} had no effect because detail was not true. "
            "This list is every workout in the range, unfiltered. Call again "
            "with detail true to apply them.")
    return result


def _get_workouts_plain(ctx: ToolContext, args: dict) -> dict:
    start, end = args.get("start_date"), args.get("end_date")
    limit = min(int(args.get("limit") or 25), 50)
    sessions = queries.list_workouts(ctx.conn, start=start, end=end, limit=limit)

    if not sessions:
        total, first, last = queries.workout_coverage(ctx.conn)
        return {
            "workouts": [],
            "no_data_in_range": True,
            "total_workouts": total,
            "available_from": first,
            "available_to": last,
            "note": ("No workouts in that range. Say so rather than inferring "
                     "activity from step counts or energy burned."),
        }

    totals = queries.workout_summary(ctx.conn, start=start, end=end)
    return {
        "workouts": [
            {
                "date": w["local_date"],
                "activity": (w["activity_type"] or "")
                .replace("HKWorkoutActivityType", ""),
                "duration": w["duration"], "duration_unit": w["duration_unit"],
                "distance": w["total_distance"],
                "distance_unit": w["total_distance_unit"],
                "energy": w["total_energy"], "energy_unit": w["total_energy_unit"],
                "source": w["source_name"],
            }
            for w in sessions
        ],
        "totals_by_activity": [
            {
                "activity": (t["activity_type"] or "")
                .replace("HKWorkoutActivityType", ""),
                "count": t["n"], "total_duration": t["total_duration"],
                "duration_unit": t["duration_unit"],
                "total_distance": t["total_distance"],
                "distance_unit": t["total_distance_unit"],
                "first": t["first"], "last": t["last"],
            }
            for t in totals
        ],
    }


def _workout_name(activity_type) -> str:
    return (activity_type or "").replace("HKWorkoutActivityType", "")


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _measure_absence(ctx: ToolContext, requested: list, rows_or_points) -> dict:
    """What the index holds of the measures asked for, when they are not all
    there. Three absences stay apart: a key no workout carries (names what the
    index does hold), a key the index holds that none of the workouts in the
    answer carry (names where it does), and no measures held at all."""
    held = queries.workout_measures(ctx.conn)
    by_key = {m["key"]: m for m in held}
    unheld = [k for k in requested if k not in by_key]
    out: dict = {}
    notes = []
    if unheld:
        out["measures_available"] = [
            {"key": m["key"], "label": m["label"], "kind": m["kind"],
             "workouts": m["n"], "from": m["first"], "to": m["last"],
             "unit": m["unit"]} for m in held]
        if held:
            notes.append(
                f"No workout carries {', '.join(unheld)}. The measures the "
                "index does hold are listed in measures_available; say which "
                "one was asked for and which are held.")
        else:
            notes.append(
                f"No workout carries {', '.join(unheld)}, and the index holds "
                "no workout measures of any kind. Say that.")
    carried = set()
    for item in rows_or_points:
        for key, entry in item.items():
            if entry is not None:
                carried.add(key)
    quiet = [k for k in requested if k in by_key and k not in carried]
    if quiet:
        out["measures_not_in_this_answer"] = [
            {"key": k, "held_on_workouts": by_key[k]["n"],
             "from": by_key[k]["first"], "to": by_key[k]["last"]}
            for k in quiet]
        notes.append(
            f"The index holds {', '.join(quiet)} on other workouts, but none "
            "of the workouts in this answer carry it. Say that, with the span "
            "it is held for.")
    if notes:
        out["note"] = " ".join(notes)
    return out


def _get_workouts_detail(ctx: ToolContext, args: dict) -> dict:
    for name in ("start_date", "end_date"):
        _check_date(name, args.get(name))
    try:
        int(args.get("limit") or 25)
    except (TypeError, ValueError):
        raise ValueError("limit is a whole number, up to 50") from None
    start, end = args.get("start_date"), args.get("end_date")
    activity = args.get("activity")
    measures = tuple(_as_list(args.get("measures")))
    derive_names = tuple(_as_list(args.get("derive")))
    exclude = tuple(_as_list(args.get("exclude_source")))
    indoor = args.get("indoor")
    period = args.get("period")
    sample_counts = args.get("samples") in (True, "true", "True")
    ratio_of = tuple(args["ratio_of"]) if args.get("ratio_of") else None
    percentile_of = args.get("percentile_of") or None

    if sample_counts and period:
        # The depth is per workout and a period is a group of workouts, so
        # there is nothing to attach it to. Read and then dropped, the answer
        # came back with no depth and no reason, against a schema that
        # promises the count.
        raise ValueError(
            "samples counts the raw records inside each workout, so it needs "
            "detail with measures and no period: a period groups workouts "
            "and the count has nothing to attach to. Ask again without "
            "period, or without samples")

    if period:
        series = queries.workout_series(
            ctx.conn, period=period, activity=activity, start=start, end=end,
            indoor=indoor, source_exclude=exclude, measures=measures,
            derive_names=derive_names, ratio_of=ratio_of,
            percentile_of=percentile_of)
        if series.is_empty:
            return _no_workouts_payload(ctx, args)
        shown = series.points[-MAX_SERIES_POINTS:]
        payload = {
            "period": period, "series": series.label, "unit": series.unit,
            "points": [_workout_point(p) for p in shown],
        }
        if len(series.points) > len(shown):
            payload["truncated"] = True
            payload["total_periods"] = len(series.points)
            payload["note_truncated"] = (
                f"Only the latest {len(shown)} of {len(series.points)} "
                "periods are listed.")
        absence = _measure_absence(
            ctx, list(measures),
            [{k: (v if v.get("n") else None)
              for k, v in p.measures.items()} for p in shown])
        payload.update(absence)
        return payload

    if ratio_of or percentile_of:
        raise ValueError("ratio_of and percentile_of need period, because "
                         "they are taken over many workouts")
    limit = min(int(args.get("limit") or 25), 50)
    rows = queries.workout_detail(
        ctx.conn, activity=activity, start=start, end=end, indoor=indoor,
        source_exclude=exclude, measures=measures, derive_names=derive_names,
        sample_counts=sample_counts, limit=limit)
    if not rows:
        return _no_workouts_payload(ctx, args)
    payload = {"workouts": [_workout_row(w) for w in rows]}
    payload.update(_measure_absence(
        ctx, list(measures),
        [{k: (v if v.get("value") is not None else None)
          for k, v in w.measures.items()} for w in rows]))
    return payload


def _no_workouts_payload(ctx: ToolContext, args: dict | None = None) -> dict:
    """The empty answer. When filters were given, it echoes them and says what
    the unfiltered range holds, so a filter that matched nothing is not read as
    a range that held nothing."""
    total, first, last = queries.workout_coverage(ctx.conn)
    payload = {
        "workouts": [],
        "no_data_in_range": True,
        "total_workouts": total,
        "available_from": first,
        "available_to": last,
        "note": ("No workouts in that range. Say so rather than inferring "
                 "activity from step counts or energy burned."),
    }
    args = args or {}
    start, end = args.get("start_date"), args.get("end_date")
    activity = args.get("activity")
    exclude = tuple(_as_list(args.get("exclude_source")))
    indoor = args.get("indoor")
    asked = {k: v for k, v in (
        ("start_date", start), ("end_date", end), ("activity", activity),
        ("exclude_source", list(exclude) or None), ("indoor", indoor))
        if v is not None}
    if not (activity or exclude or indoor is not None):
        return payload
    payload["asked"] = asked
    by_activity: dict = {}
    for row in queries.workout_summary(ctx.conn, start=start, end=end,
                                       limit=1000):
        name = _workout_name(row["activity_type"])
        by_activity[name] = by_activity.get(name, 0) + row["n"]
    payload["activities_in_range"] = [
        {"activity": k, "workouts": v}
        for k, v in sorted(by_activity.items(), key=lambda kv: (-kv[1], kv[0]))]
    parts = []
    if by_activity:
        held = ", ".join(f"{v} {k}" for k, v in by_activity.items())
        parts.append(f"The range itself holds workouts: {held}. The filters "
                     "asked for matched none of them.")
    else:
        parts.append("The range holds no workouts of any activity.")
    if exclude:
        excluded = queries.workout_detail(
            ctx.conn, activity=activity, start=start, end=end, indoor=indoor,
            source_include=exclude, limit=1_000_000)
        payload["excluded_by_source"] = {"sources": list(exclude),
                                         "workouts": len(excluded)}
        parts.append(f"{len(excluded)} matching workouts came from the "
                     f"excluded source ({', '.join(exclude)}).")
    payload["note"] += " " + " ".join(parts)
    return payload


def _workout_row(w) -> dict:
    row = {
        "date": w.local_date,
        "activity": _workout_name(w.activity_type),
        "duration": w.duration, "duration_unit": w.duration_unit,
        "distance": w.total_distance,
        "distance_unit": w.total_distance_unit,
        "energy": w.total_energy, "energy_unit": w.total_energy_unit,
        "source": w.source_name,
        "indoor": None if w.indoor is None else bool(w.indoor),
        "citation": w.citation,
        "measures": w.measures,
        "derived": w.derived,
    }
    if w.sample_depth is not None:
        row["sample_depth"] = w.sample_depth
    return row


def _workout_point(p) -> dict:
    return {"period": p.period, "start": p.period_start, "end": p.period_end,
            "workouts": p.n,
            "duration": p.duration, "duration_unit": p.duration_unit,
            "distance": p.distance, "distance_unit": p.distance_unit,
            "energy": p.energy, "energy_unit": p.energy_unit,
            "measures": p.measures, "derived": p.derived}


# --------------------------------------------------------------------------- #
# compare_windows
# --------------------------------------------------------------------------- #

def _unit_for_the_model(unit: str | None) -> str | None:
    """A unit field with `queries.UNLABELLED` taken out of it.

    The sentinel is the absence of a unit. `windows` says that in words in its
    rendered text and in `because`; the raw `unit` fields beside them handed
    the word itself to the model, which then writes "median 60 unlabelled".
    None is what no unit looks like in a payload, and `unit_note` says why.
    """
    return None if unit == queries.UNLABELLED else unit


def _comparable_payload(comparable: dict) -> dict:
    """The verdict block, with the sentinel out of its unit fields.

    The verdict itself was decided before this, inside `windows`, and `because`
    already reads "units differ: no unit named and count/min". Only the raw
    fields beside it needed the same treatment.
    """
    out = dict(comparable)
    for key in ("left_unit", "right_unit"):
        if key in out:
            out[key] = _unit_for_the_model(out[key])
    for key in ("left_units_seen", "right_units_seen"):
        if key in out:
            out[key] = [queries.unit_in_words(u) for u in out[key]]
    return out


def _side_payload(summary, *, sample_counts: bool = False) -> dict:
    out = {
        "periods": summary.n,
        "periods_with_no_data": summary.empty_periods,
        "periods_with_a_value": summary.value_periods,
        "min": summary.min, "max": summary.max, "median": summary.median,
        "mean": summary.mean, "unit": _unit_for_the_model(summary.unit),
        # A list of the units seen may name the absence, since a list is not a
        # figure's unit, but it names it in words like everything else.
        "units_seen": [queries.unit_in_words(u) for u in summary.units_seen],
        "unit_mixed": summary.unit_mixed,
        "from": summary.first, "to": summary.last,
        "window": [summary.window_start, summary.window_end],
        # The noun travels with the count: a metric side counts records and a
        # workout side counts workouts, and "samples" named neither.
        "counted": summary.counted,
        "coverage": summary.coverage,
    }
    if sample_counts:
        out[summary.counted] = summary.counted_n
        if summary.sample_depth is not None:
            # §6's sample_depth check: the median records inside each workout
            # on this side. The raw per-workout counts stay behind, because
            # the median and the two workout counts are the figures and a list
            # of every count is something a model would be free to recompute
            # from. Absent rather than null on a metric side, which has no
            # workout interval to count inside.
            out["sample_depth"] = {
                base: {k: v for k, v in depth.items() if k != "counts"}
                for base, depth in summary.sample_depth.items()}
    return out


def _points_payload(points) -> tuple[list[dict], bool]:
    out = []
    for point in points[:MAX_COMPARE_POINTS]:
        row = {"period": point.period, "start": point.period_start,
               "end": point.period_end}
        if hasattr(point, "value"):
            row["value"] = point.value
            row["n"] = point.n
        else:
            row["workouts"] = point.n
            row["duration"] = point.duration
            row["duration_unit"] = point.duration_unit
            row["measures"] = point.measures
            row["derived"] = point.derived
        out.append(row)
    return out, len(points) > MAX_COMPARE_POINTS


def _whole_days(args: dict, name: str, default: int) -> int:
    """An absent count takes the default; a present one is used as given, so an
    explicit 0 reaches the layer, which refuses it, instead of becoming a
    different question."""
    value = args.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} is a whole number of days, e.g. 30") from None


def _check_date(name: str, value) -> None:
    if value is None:
        return
    try:
        _dt.date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"{name} is a date as YYYY-MM-DD, e.g. 2024-01-31; "
                         f"got {str(value)[:40]!r}") from None


def _compare_windows(ctx: ToolContext, args: dict) -> dict:
    try:
        for name in ("anchor_date", "left_start", "left_end", "right_start",
                     "right_end"):
            _check_date(name, args.get(name))
        return _compare_windows_inner(ctx, args)
    except ValueError as exc:
        log.warning("tool compare_windows refused (ValueError)")
        return {"error": "invalid_request", "note": str(exc)}


def _compare_windows_inner(ctx: ToolContext, args: dict) -> dict:
    spec = windows.SeriesSpec(
        metric=args.get("metric"),
        activity=args.get("activity"),
        measure=args.get("measure"),
        derive_name=args.get("derive"),
        source_exclude=tuple(_as_list(args.get("exclude_source"))),
        indoor=args.get("indoor"),
    )
    period = args.get("period") or "day"
    sample_counts = args.get("sample_counts") in (True, "true", "True")
    needs = ("Pass either anchor_date or anchor_fact with before_days and "
             "after_days, or all four of left_start, left_end, right_start "
             "and right_end.")

    anchor_date, anchor_fact = args.get("anchor_date"), args.get("anchor_fact")
    ranges = [args.get(k) for k in
              ("left_start", "left_end", "right_start", "right_end")]
    if (anchor_date or anchor_fact) and any(ranges):
        return {"error": "two_ways_to_name_windows",
                "note": "Give an anchor or four range dates, not both. " + needs}

    if anchor_date or anchor_fact:
        resolved = windows.resolve_anchor(ctx.conn, date=anchor_date,
                                          fact=anchor_fact)
        if resolved is None:
            held = queries.clinical_fact_coverage(ctx.conn)
            payload = {
                "error": "anchor_not_found",
                "note": ("No dated row in the clinical record matches that "
                         "name. Ask get_clinical_history for the conditions, "
                         "procedures and prescriptions that carry a date, "
                         "then pass one; or pass anchor_date."),
            }
            if held:
                payload["clinical_kinds_held"] = held
            else:
                payload["note"] = (
                    "The index holds no clinical facts at all, so there is "
                    "nothing to anchor on by name. Say that, or pass "
                    "anchor_date if the person gives a date.")
            return payload
        if isinstance(resolved, windows.AnchorAmbiguous):
            return {
                "anchor_ambiguous": True,
                "candidates": [dict(c) for c in resolved.candidates],
                "note": ("More than one dated row matches that name, on "
                         "different dates. Ask the person which one they "
                         "mean, naming the dates, and choose none yourself."),
            }
        result = windows.window(
            ctx.conn, spec=spec, anchor=resolved,
            before_days=_whole_days(args, "before_days", 30),
            after_days=_whole_days(args, "after_days", 30), period=period,
            # The sides are summarized while the window is built, before
            # `as_comparison` sees the flag, so the depth has to be asked for
            # here or it is never counted at all.
            sample_counts=sample_counts)
        comparison = windows.as_comparison(result, sample_counts=sample_counts)
        anchor_block = {"date": resolved.date, "label": resolved.label,
                        "citation": resolved.citation}
    else:
        left = (args.get("left_start"), args.get("left_end"))
        right = (args.get("right_start"), args.get("right_end"))
        if not all(left) or not all(right):
            return {"error": "no_window_given", "note": needs}
        comparison = windows.compare(ctx.conn, spec=spec, left=left,
                                     right=right, period=period,
                                     sample_counts=sample_counts)
        anchor_block = None

    left_points, left_cut = _points_payload(comparison.left_points)
    right_points, right_cut = _points_payload(comparison.right_points)
    payload = {
        "series": spec.label,
        "period": period,
        "anchor": anchor_block,
        "left_label": comparison.left_label,
        "right_label": comparison.right_label,
        "left": _side_payload(comparison.left, sample_counts=sample_counts),
        "right": _side_payload(comparison.right, sample_counts=sample_counts),
        "left_points": left_points,
        "right_points": right_points,
        "comparable": _comparable_payload(comparison.comparable),
        "rendered": windows.render(comparison),
        "note": comparison.note,
    }
    if queries.UNLABELLED in (comparison.left.unit, comparison.right.unit):
        payload["unit_note"] = UNIT_NOTE
    if left_cut or right_cut:
        payload["points_truncated"] = {
            "left": left_cut, "right": right_cut,
            "listed_per_side": MAX_COMPARE_POINTS}
    return payload


def _overall(points: list, aggregation: str) -> dict | None:
    """Collapse a series to one figure covering the whole requested range.

    Each point's value is already combined across sources for its period, so the
    remaining question is only how to combine periods. Averages are weighted by
    sample count: a day with 300 heart-rate samples should not count the same as
    a day with 3.
    """
    values = [(p.value, p.n) for p in points if p.value is not None]
    if not values:
        return None

    total_samples = sum(n for _, n in values)
    if aggregation in ("sum", "duration", "count"):
        figure = sum(v for v, _ in values)
    elif aggregation == "min":
        figure = min(v for v, _ in values)
    elif aggregation == "max":
        figure = max(v for v, _ in values)
    elif aggregation == "avg":
        figure = (sum(v * n for v, n in values) / total_samples
                  if total_samples else None)
    else:
        return None

    return {
        "value": figure,
        "aggregation": aggregation,
        "periods_covered": len(values),
        "total_samples": total_samples,
        "from": points[0].period_start,
        "to": points[-1].period_end,
        "note": ("Use this value directly. Do not recompute it from the points "
                 "below."),
    }


# Analytes per batched call, and points kept for each. Batching exists because a
# broad question ("anything out of range?") otherwise becomes one call per
# analyte: measured at 23 calls and 358 seconds before this parameter existed.
# Points are trimmed harder in batch mode since the question is usually about
# the latest value and its direction, not the full history.
MAX_BATCH_ANALYTES = 30
MAX_BATCH_POINTS = 6


def _source_disagreement_note(disagreements, dates=None) -> str | None:
    """What each source reported on each date they did not agree, as a note.

    Reported, never resolved: the model is told to say what each source said
    and not to pick one."""
    if dates is not None:
        disagreements = [d for d in disagreements if d["collected_date"] in dates]
    if not disagreements:
        return None
    parts = []
    for d in disagreements:
        said = "; ".join(
            f"{r['value']}{' ' + r['unit'] if r.get('unit') else ''} "
            f"from {' and '.join(r['sources'])}"
            for r in d["reported"])
        parts.append(f"{d['collected_date']}: {said}")
    return (
        "Sources reported different results for the same draw ("
        + " | ".join(parts) + "). Say what each source reported, with its "
        "unit. Do not choose between them or call either one correct."
    )


def _get_lab_trend(ctx: ToolContext, args: dict) -> dict:
    batch = args.get("analytes")
    if isinstance(batch, str):
        batch = [a.strip() for a in batch.split(",") if a.strip()]
    if batch:
        return _get_lab_trends_batch(ctx, batch, args)

    name = (args.get("analyte") or "").strip()
    if not name:
        return {"error": "analyte or analytes is required"}

    key, _ = labs.resolve_key(name)
    trend = queries.lab_trend(ctx.conn, key, start=args.get("start_date"),
                              end=args.get("end_date"))

    if trend.is_empty:
        available = queries.list_analytes(ctx.conn, limit=60)
        payload = {
            "analyte": key,
            "label": trend.label,
            "points": [],
            "no_data": True,
            "total_results_for_analyte": trend.total_results,
            "available_from": trend.available_from,
            "available_to": trend.available_to,
            "analytes_present_in_reports": [a["analyte_key"] for a in available],
            "note": (
                f"No {trend.label} results in the requested range. Say so "
                f"explicitly. Do not infer a value from a different analyte."
            ),
        }
        return payload

    points = trend.points
    truncated = False
    if len(points) > MAX_LAB_POINTS:
        points = points[-MAX_LAB_POINTS:]
        truncated = True

    payload: dict = {
        "analyte": trend.key,
        "label": trend.label,
        "points": [
            {
                "collected_date": p.collected_date,
                "value": p.value_num if p.value_num is not None else p.value_text,
                "unit": p.reported_unit,
                "reference_range": p.ref_text,
                "flag": p.flag,
                "out_of_range": p.out_of_range,
                "citation": p.citation,
                "source": p.source,
                **({"via_ocr": True} if p.via_ocr else {}),
            }
            for p in points
        ],
    }
    if truncated:
        payload["truncated"] = True
    payload["citation_note"] = CITATION_NOTE
    siblings = queries.panel_siblings(ctx.conn, trend.key)
    payload["other_analytes_in_this_panel"] = [s["analyte_key"] for s in siblings]
    # Coverage, stated as a fact and nothing more. The first version of this
    # note ended with "call get_lab_trend again with `analytes` set to those
    # keys", and that broke Q27 four times out of four: an imperative inside a
    # tool result arrives exactly when the model is choosing its next call, and
    # it beat prompt rule 1e, so the notes search that finds atorvastatin never
    # happened. A payload instruction outranking a prompt rule is why
    # `citation_note` works; it is also why this one must not direct a call.
    # Only the prompt sees the question, so whether to widen is rule 4b's.
    payload["panel_note"] = (
        "other_analytes_in_this_panel is every other analyte from this panel "
        "that these reports hold, so an empty list means they hold none. This "
        "states what the reports cover. It is not a request to fetch them."
    )
    if len(trend.units_seen) > 1:
        payload["unit_warning"] = (
            f"Results use different units across reports "
            f"({', '.join(trend.units_seen)}) and are NOT converted. Do not "
            f"compare them numerically without saying this."
        )
    if any(p.via_ocr for p in points):
        payload["ocr_warning"] = (
            "Values marked via_ocr were read from a scanned page. OCR misreads "
            "digits, so reference ranges especially may be wrong. Say that these "
            "should be checked against the original report."
        )
    note = _source_disagreement_note(
        trend.disagreements, {p.collected_date for p in points})
    if note:
        payload["source_disagreement"] = note
    return payload


# --------------------------------------------------------------------------- #
# search_records
# --------------------------------------------------------------------------- #

def _get_lab_trends_batch(ctx: ToolContext, names: list, args: dict) -> dict:
    """Several analytes in one call, trimmed to the most recent results each."""
    requested = [str(n).strip() for n in names if str(n).strip()]
    truncated_list = len(requested) > MAX_BATCH_ANALYTES
    requested = requested[:MAX_BATCH_ANALYTES]

    found: dict[str, dict] = {}
    missing: list[str] = []
    flagged: list[dict] = []

    for name in requested:
        key, _ = labs.resolve_key(name)
        trend = queries.lab_trend(ctx.conn, key, start=args.get("start_date"),
                                  end=args.get("end_date"))
        if trend.is_empty:
            missing.append(key)
            continue
        points = trend.points[-MAX_BATCH_POINTS:]
        found[key] = {
            "label": trend.label,
            "results": [
                {
                    "collected_date": p.collected_date,
                    "value": p.value_num if p.value_num is not None else p.value_text,
                    "unit": p.reported_unit,
                    "reference_range": p.ref_text,
                    "flag": p.flag,
                    "out_of_range": p.out_of_range,
                    "citation": p.citation,
                    "source": p.source,
                    **({"via_ocr": True} if p.via_ocr else {}),
                }
                for p in points
            ],
            **({"source_disagreement": n} if (n := _source_disagreement_note(
                trend.disagreements, {p.collected_date for p in points})) else {}),
        }
        latest = trend.points[-1]
        if latest.out_of_range:
            flagged.append({
                "analyte": key, "label": trend.label,
                "value": latest.value_num, "unit": latest.unit,
                "reference_range": latest.ref_text, "flag": latest.flag,
                "collected_date": latest.collected_date,
                "citation": latest.citation,
            })

    payload: dict = {
        "analytes": found,
        "citation_note": CITATION_NOTE,
        # Precomputed so the model doesn't have to scan every result itself and
        # risk missing one — the same principle as `overall` for time series.
        "out_of_range_on_latest_report": flagged,
    }
    if missing:
        payload["no_results_for"] = missing
    if truncated_list:
        payload["truncated_request"] = (
            f"Only the first {MAX_BATCH_ANALYTES} analytes were looked up.")
    if any(r.get("via_ocr") for a in found.values() for r in a["results"]):
        payload["ocr_warning"] = (
            "Values marked via_ocr were read from a scanned page. OCR misreads "
            "digits, so reference ranges especially may be wrong. Say that these "
            "should be checked against the original report.")
    return payload


def _search_records(ctx: ToolContext, args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}

    limit = min(int(args.get("limit") or 5), MAX_SEARCH_RESULTS)
    kind = args.get("kind")
    if kind not in ("note", "pdf", "image", None):
        kind = None

    hits: list = []
    method = "keyword"
    if ctx.embedder_factory is not None:
        try:
            embedder = ctx.embedder_factory()
            embedded = ctx.conn.execute(
                "SELECT COUNT(*) AS n FROM chunk WHERE embedded_with = ?",
                (embedder.name,),
            ).fetchone()["n"]
            if embedded:
                store = vector_store.VectorStore(ctx.vector_path)
                hits = vector_store.semantic_search(
                    ctx.conn, store, embedder, query, limit=limit, kind=kind)
                method = "semantic"
        except Exception as exc:  # noqa: BLE001 - retrieval must not kill a turn
            log.warning("semantic search unavailable (%s)", type(exc).__name__)

    if not hits:
        hits = vector_store.keyword_search(ctx.conn, query, limit=limit, kind=kind)
        method = "keyword"

    if not hits:
        total = ctx.conn.execute(
            "SELECT COUNT(*) AS n FROM chunk").fetchone()["n"]
        tags = [r["tag"] for r in ctx.conn.execute(
            "SELECT DISTINCT tag FROM document_tag ORDER BY tag")]
        return {
            "results": [],
            "no_matches": True,
            "chunks_indexed": total,
            "tags_available": tags,
            "note": ("Nothing matched. Say that the documents and notes do not "
                     "cover this, rather than answering from general knowledge."),
        }

    return {
        "method": method,
        "results": [
            {
                "citation": h.citation,
                "kind": h.kind,
                "text": " ".join(h.text.split())[:MAX_SNIPPET_CHARS],
            }
            for h in hits
        ],
        "note": ("Quote or paraphrase only what these snippets say, and cite "
                 "the `citation` string verbatim with any claim drawn from "
                 "them. The note's own date or heading is not a citation: "
                 "reproduce the string."),
    }


# --------------------------------------------------------------------------- #
# search_medical_literature
# --------------------------------------------------------------------------- #

def _search_medical_literature(ctx: ToolContext, args: dict) -> dict:
    from ..literature import corpus as lit_corpus

    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}

    if ctx.literature_conn is None:
        # Not an error: a corpus is optional. But the model must be told that
        # the absence is why there is nothing here, so it does not fall back on
        # recalled medical knowledge — the failure this tool exists to fix.
        return {
            "corpus_installed": False,
            "findings": [],
            "note": ("A literature corpus is not installed on this machine. Say "
                     "that you have no literature to cite and that you cannot "
                     "state clinical thresholds or general medical facts "
                     "without one. Do not answer from general knowledge."),
        }

    limit = min(int(args.get("limit") or MAX_LITERATURE_FINDINGS),
                MAX_LITERATURE_FINDINGS)
    min_tier = args.get("min_tier") or None
    since_year = args.get("since_year") or None

    try:
        findings = _literature_hits(ctx, query, limit, min_tier, since_year)
    except ValueError as exc:
        return {"error": str(exc)}

    coverage = lit_corpus.coverage(ctx.literature_conn)

    # A floor excludes every unranked article, and on a real corpus that is
    # most of it: 65.5% of 2,000 PubMed abstracts resolved to `unknown`
    # because PubMed records no study design for most primary research. The
    # numbers here are corpus-level and exact, from coverage(); a per-query
    # count over an FTS OR-match would be true and useless. Protocols are
    # counted separately (payload["protocol_warning"], below): a protocol has
    # a known design, it simply reports no results, so it is not part of
    # "no recorded study design".
    filter_block: dict | None = None
    if min_tier or since_year:
        filter_block = {"min_tier": min_tier, "since_year": since_year}
        if min_tier:
            unranked = coverage["tiers"].get(lit_tiers.UNKNOWN, 0)
            total = coverage["article_count"] or 1
            filter_block["unranked_articles_excluded"] = unranked
            filter_block["unranked_share"] = round(unranked / total, 3)

    if not findings:
        miss: dict = {
            "corpus_installed": True,
            "findings": [],
            "no_matches": True,
            "corpus": coverage,
        }
        if filter_block is None:
            miss["note"] = (
                "The corpus holds nothing on this. Say that your literature "
                "does not cover it, name what it does cover, and do not answer "
                "from general knowledge.")
        else:
            # Not a coverage gap: the corpus may hold plenty on this, untagged.
            # Saying 'holds nothing' here would send the model to recall.
            miss["filtered"] = True
            miss["filter"] = filter_block
            reason = "Nothing matched under these filters. "
            if min_tier:
                reason += (
                    f"{filter_block['unranked_articles_excluded']} of "
                    f"{coverage['article_count']} articles in this corpus "
                    f"({filter_block['unranked_share']:.0%}) have no recorded "
                    f"study design and are excluded by any min_tier floor; "
                    f"that is normal for PubMed, not a sign of thin coverage. ")
            miss["note"] = (
                reason + "Retry without min_tier and since_year before "
                "concluding that the corpus does not cover this, and do not "
                "answer from general knowledge.")
        return miss

    payload: dict = {
        "corpus_installed": True,
        "corpus": {"packs": coverage["packs"], "built": coverage["built"],
                   "article_count": coverage["article_count"]},
        "method": findings[0].method,
        "findings": [
            {
                "title": f.title,
                "text": " ".join(f.text.split())[:MAX_FINDING_CHARS],
                "citation": f.citation,
                "pmid": f.pmid,
                "doi": f.doi,
                "year": f.year,
                "evidence_tier": f.evidence_tier,
                "tier_source": f.tier_source,
                "license": f.license,
                "retracted": f.retracted,
                **({"retraction_note": f.retraction_note} if f.retracted else {}),
            }
            for f in findings
        ],
        "note": LITERATURE_NOTE,
    }
    if any(f.retracted for f in findings):
        payload["retraction_warning"] = (
            "One or more findings come from a RETRACTED publication. Say so "
            "beside the citation, and do not present it as current evidence.")
    if any(f.evidence_tier == lit_tiers.UNKNOWN for f in findings):
        payload["tier_warning"] = (
            "Findings with evidence_tier 'unknown' had no publication type "
            "recorded. Say that their study design is unknown rather than "
            "implying one.")
    if filter_block is not None:
        payload["filter"] = filter_block
        if min_tier:
            payload["filter_note"] = (
                f"min_tier={min_tier} excluded the "
                f"{filter_block['unranked_articles_excluded']} articles "
                f"({filter_block['unranked_share']:.0%} of this corpus) that "
                f"have no recorded study design. Without a floor, results "
                f"are ordered strongest-first among the matches, but the "
                f"matches themselves are chosen by relevance, so a floor "
                f"can surface strong evidence a broad query would not.")
    if any(f.evidence_tier == lit_tiers.PROTOCOL for f in findings):
        payload["protocol_warning"] = (
            "One or more findings are trial PROTOCOLS: they describe a "
            "planned study and report no results. Say so beside the citation, "
            "and do not present a protocol as evidence of anything.")
    return payload


def _literature_hits(ctx: ToolContext, query: str, limit: int,
                     min_tier: str | None, since_year: int | None) -> list:
    """Semantic where possible, keyword otherwise. Never raises for retrieval."""
    from ..literature import store as lit_store
    return lit_store.hits(ctx.literature_conn, query, limit=limit,
                          min_tier=min_tier, since_year=since_year,
                          vector_path=ctx.literature_vector_path,
                          embedder_factory=ctx.embedder_factory)


# --------------------------------------------------------------------------- #
# Clinical records (FHIR)
# --------------------------------------------------------------------------- #

# The `kind` argument is plural; rows store the singular. The model sees the
# plural everywhere (argument, payload, inventory) so it sends back what it read.
_FACT_KIND_ARG = {
    "conditions": "condition", "medications": "medication",
    "allergies": "allergy", "immunizations": "immunization",
    "procedures": "procedure",
}
# Public: the CLI takes the same plural vocabulary.
FACT_KIND_ARG = _FACT_KIND_ARG
_FACT_KIND_PLURAL = {v: k for k, v in _FACT_KIND_ARG.items()}

# What the person wrote down themselves, fetched beside a health system's feed.
#
# Rule 1e asked the model to search the notes in the same turn as it called
# get_clinical_history, because a feed holds what a health system sent and not
# what someone takes. Five draws on one commit had it comply three times out of
# five, and the two failures said this person takes no lipid-lowering drug while
# their own note records atorvastatin. A prompt rule is a request, and a request
# that must hold every time is the wrong mechanism for a false clinical claim.
#
# So the second lookup is not requested any more, it is performed. FTS5 over the
# chunk table is pure SQLite: no embeddings, no model, no network, and it runs
# whether or not anything has been embedded. The vocabulary is ORed by
# `_fts_query`, so these are terms a person's own medication or condition note
# tends to contain, not a question.
# Keyed on the singular row kind (`row_kind`), not the plural tool argument:
# the tool takes "medications" and the fact table stores "medication", and
# keying this on the argument made every call fall through to the whole
# vocabulary instead of the relevant half.
_NOTE_VOCABULARY = {
    "medication": "medication medications prescription prescribed taking dose mg",
    "condition": "diagnosed diagnosis condition conditions history",
}
MAX_NOTES_BESIDE = 5


def _notes_beside_clinical(ctx: ToolContext, kind: str) -> list[dict]:
    """Passages from this person's own notes and photographed lists that bear on
    medications or conditions. Empty is a fact about the index, not a failure,
    and the caller reports it as coverage either way."""
    from ..store import vector_store

    terms = (_NOTE_VOCABULARY.get(kind) or
             " ".join(_NOTE_VOCABULARY.values()))
    try:
        hits = vector_store.keyword_search(ctx.conn, terms,
                                           limit=MAX_NOTES_BESIDE)
    except Exception:  # pragma: no cover - a missing FTS table must not
        return []      # take down a clinical answer
    return [
        {
            "citation": h.citation,
            "kind": h.kind,
            "text": " ".join(h.text.split())[:MAX_SNIPPET_CHARS],
        }
        for h in hits
    ]


def _attach_personal_notes(ctx: ToolContext, payload: dict, kind: str) -> dict:
    """Every get_clinical_history answer carries both sources or says it has one.

    Always present, so an empty list means the notes hold nothing on this and
    not that nobody looked -- the same contract `other_analytes_in_this_panel`
    keeps. Descriptive only: this names what is already in the payload and must
    never ask for another tool call, because an imperative inside a tool result
    outranks a prompt rule and will displace whatever the model would have done
    next.
    """
    notes = _notes_beside_clinical(ctx, kind)
    payload["also_in_personal_notes"] = notes
    payload["two_sources_note"] = (
        "`facts` is what this person's health systems sent. "
        "`also_in_personal_notes` is what they wrote down or photographed "
        "themselves, already retrieved for you. Neither is the whole of what "
        "they take or have been told, and an empty `also_in_personal_notes` "
        "means their notes hold nothing on this. Report what each source "
        "holds, and never say they take no medication of some kind, or have "
        "no condition of some kind, on the strength of `facts` alone."
    )
    return payload


def _get_clinical_history(ctx: ToolContext, args: dict) -> dict:
    kind = (args.get("kind") or "").strip().lower()
    row_kind = _FACT_KIND_ARG.get(kind)
    if kind and row_kind is None:
        return {"error": f"kind must be one of {sorted(_FACT_KIND_ARG)}"}
    # One source for "what does this index hold clinically": facts are one of
    # three tables, and deciding emptiness from `clinical_fact` alone told the
    # person their own labs and note references did not exist.
    holdings = queries.clinical_holdings(ctx.conn)
    coverage = holdings["facts"]
    facts = queries.clinical_facts(ctx.conn, kind=row_kind,
                                   since=args.get("since"),
                                   limit=MAX_CLINICAL_FACTS)
    payload: dict = {
        "facts": facts,
        "kinds_available": {_FACT_KIND_PLURAL.get(k, k): v["count"]
                            for k, v in coverage.items()},
    }
    if not holdings["any"]:
        payload["no_clinical_records"] = True
        payload["note"] = (
            "This index holds no clinical records. They come from the "
            "clinical-records folder of an Apple Health export, which this "
            "person has either not ingested or skipped. Say that rather than "
            "answering from anything else."
        )
        return _attach_personal_notes(ctx, payload, row_kind or "")
    if not coverage:
        payload["clinical_records_held"] = {
            label: n for label, n, _ in holdings["labels"]}
        payload["note"] = (
            "No clinical facts (conditions, medications, allergies, "
            "immunizations or procedures) in this index. It does hold "
            + queries.clinical_holdings_phrase(
                ctx.conn, leave_out=frozenset(_FACT_KIND_ARG),
                holdings=holdings)
            + ". Say what is absent and what is held, and do not say this "
              "person has no clinical records."
        )
        return _attach_personal_notes(ctx, payload, row_kind or "")
    if not facts:
        # Naming the other kinds and leaving the model to ask for them is not
        # enough: eval run 12 drew a model that asked for procedures, was
        # told conditions are held, reported "no procedure records" and then
        # asked the person for the date of an injury that a condition record
        # carried. Rule 1e already settled this shape by handing over the
        # personal notes rather than telling the model to go looking, so the
        # other kinds are handed over the same way. `facts` stays empty,
        # because no record of the kind asked for exists.
        # Only when a kind was asked for. Without one, this query is the one
        # that already came back empty, so the guard saves a repeat rather
        # than changing what the model sees. The window is the person's own
        # `since`: records from outside it would answer a question nobody
        # asked.
        others = queries.clinical_facts(ctx.conn, since=args.get("since"),
                                        limit=MAX_CLINICAL_FACTS) \
            if row_kind else []
        payload["note"] = (
            f"No {kind or 'clinical'} records in the requested range. The "
            f"index does hold {', '.join(payload['kinds_available'])}. Say "
            f"what is absent."
        )
        if others:
            payload["facts_of_other_kinds"] = others
            payload["note"] += (
                " Those records are here under `facts_of_other_kinds`, "
                "already retrieved. Read them before telling the person "
                "something is unrecorded, and before asking them for a date "
                "or a name one of them carries."
            )
        return _attach_personal_notes(ctx, payload, row_kind or "")
    if len(facts) >= MAX_CLINICAL_FACTS:
        payload["truncated"] = True
    payload["note"] = (
        # "never state" rather than "do not say": the latter matches the
        # eval's decline pattern, so a model relaying this instruction would
        # satisfy a check for having declined something. See
        # tests/test_scorer_contract.py.
        "These are what the person's health systems recorded, not what they "
        "told you. State them as records with their dates, cite the file, and "
        "never state whether a regimen or a diagnosis is correct."
    )
    return _attach_personal_notes(ctx, payload, row_kind or "")


def _get_clinical_vitals(ctx: ToolContext, args: dict) -> dict:
    holdings = queries.clinical_holdings(ctx.conn)
    metrics_available = holdings["vital_metrics"]
    if not metrics_available:
        # "Nothing at all" is decided from every clinical table, not from
        # facts alone: labs and note references are clinical records too.
        if holdings["any"]:
            return {"measurements": [],
                    "clinical_records_held": {
                        label: n for label, n, _ in holdings["labels"]},
                    "note": ("This index holds clinical records but no "
                             "clinician-measured vitals among them. It does "
                             "hold "
                             + queries.clinical_holdings_phrase(
                                 ctx.conn,
                                 leave_out=frozenset({"clinician vitals"}),
                                 holdings=holdings)
                             + ". Say so, and do not substitute watch data.")}
        return {"measurements": [], "no_clinical_records": True,
                "note": ("This index holds no clinical records at all. They "
                         "come from the clinical-records folder of an Apple "
                         "Health export, which this person has either not "
                         "ingested or skipped. Say so.")}
    metric = args.get("metric")
    try:
        limit = int(args.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    # The flag has to be compared against the limit that actually ran. Held
    # against MAX_CLINICAL_VITALS it was unreachable for every default call,
    # so a 100-row slice of 2,984 readings looked like the whole set.
    effective_limit = max(1, min(limit, MAX_CLINICAL_VITALS))
    rows = queries.clinical_vitals(
        ctx.conn, metric=metric, start=args.get("start_date"),
        end=args.get("end_date"), limit=effective_limit)
    payload: dict = {
        "measurements": [
            {"metric": r["metric"],
             "value": r["value_num"] if r["value_num"] is not None else r["value_text"],
             "unit": r["unit"], "measured_at": r["measured_at"],
             "date": r["local_date"], "citation": r["citation"]}
            for r in rows],
        "metrics_available": metrics_available,
        "note": ("These were measured by a clinician at an encounter, not by "
                 "this person's watch. Wearable series come from "
                 "query_healthkit and the two must not be mixed or compared "
                 "as if they were one series."),
    }
    if len(rows) >= effective_limit:
        payload["truncated"] = True
        window = (f"{rows[-1]['local_date']} to {rows[0]['local_date']}"
                  if rows[0]["local_date"] and rows[-1]["local_date"]
                  else "the most recent readings")
        held = sum(m["n"] for m in metrics_available
                   if metric is None or m["metric_key"] == labs.slug(metric))
        payload["truncation_note"] = (
            f"Only the {len(rows)} most recent readings were read, covering "
            f"{window}, out of {held} this index holds for that metric "
            f"selection. Say which window you looked at rather than "
            f"describing it as the whole record.")
    if not rows:
        first = min((m["first"] for m in metrics_available if m["first"]),
                    default=None)
        last = max((m["last"] for m in metrics_available if m["last"]),
                   default=None)
        what = f"clinician-measured {metric}" if metric else "clinician-measured vitals"
        payload["note"] = (
            f"No {what} in the requested range. The index does hold "
            f"{', '.join(m['metric_key'] for m in metrics_available)}, "
            f"from {first} to {last}. Say what is absent.")
    return payload


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="query_healthkit",
        description=(
            "Aggregate a metric from the user's Apple Health export over time "
            "(heart rate, steps, sleep, HRV, weight, and similar). Use for "
            "anything a watch or phone recorded. Returns one value per day, "
            "week, or month with the units. A null `unit` beside a "
            "`unit_note` means those records name no unit at all; report "
            "that, never a unit of your own."
        ),
        parameters={
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "description": (
                        "Metric name, e.g. resting-hr, steps, sleep, hrv, "
                        "weight, active-energy, respiratory-rate. Accepts full "
                        "HealthKit identifiers too. Asking for 'sleep' returns "
                        "per-night totals with stages broken out, where a night "
                        "includes the hours after midnight."
                    ),
                },
                "start_date": {"type": "string",
                               "description": "Inclusive, YYYY-MM-DD."},
                "end_date": {"type": "string",
                             "description": "Inclusive, YYYY-MM-DD."},
                "period": {
                    "type": "string",
                    "enum": ["day", "week", "month"],
                    "description": "Bucket size. Defaults to day.",
                },
                "aggregation": {
                    "type": "string",
                    "enum": ["sum", "avg", "min", "max", "count", "duration"],
                    "description": (
                        "Omit this unless the user asked for something specific. "
                        "The correct default is chosen per metric: cumulative "
                        "metrics like steps are summed, point-in-time metrics "
                        "like heart rate are averaged."
                    ),
                },
            },
            "required": ["metric"],
        },
        handler=_query_healthkit,
    ),
    Tool(
        name="get_lab_trend",
        description=(
            "Blood-test results from the user's lab reports, oldest first, with "
            "the reference range and flag each report printed and a citation to "
            "the source file and page. Use for anything from bloodwork. "
            "For a broad question such as which results are abnormal, pass "
            "several names in `analytes` in ONE call rather than calling this "
            "repeatedly — the reply then includes a ready-made list of every "
            "value flagged out of range on the latest report."
        ),
        parameters={
            "type": "object",
            "properties": {
                "analyte": {
                    "type": "string",
                    "description": (
                        "A single analyte, e.g. ldl, hdl, hba1c, tsh, glucose, "
                        "vitamin_d, ferritin, creatinine. Returns its full "
                        "history."
                    ),
                },
                "analytes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Several analytes at once. Use this for broad questions. "
                        "Returns the most recent few results for each plus "
                        "`out_of_range_on_latest_report`."
                    ),
                },
                "start_date": {"type": "string", "description": "YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "YYYY-MM-DD."},
            },
        },
        handler=_get_lab_trend,
    ),
    # A fourth tool, beyond the three named in plan §4, added because the eval
    # set caught its absence: asked "what workouts have I logged?", the agent
    # answered "none" while the index held a logged run. Workouts live in their
    # own table with their own shape (activity, duration, distance, energy), and
    # nothing in the other three tools could see them. §4 allows for 4-5 tools.
    Tool(
        name="get_workouts",
        description=(
            "Workout sessions the user recorded — activity type, date, "
            "duration, distance, and energy. Use for any question about "
            "workouts, exercise sessions, runs, rides, or training. Step counts "
            "and active energy are NOT workouts; use query_healthkit for those. "
            "A unit of \"mixed\" beside a null figure means that period's "
            "records were in more than one unit and nothing was added up: "
            "report it as a refusal, never as a unit."
        ),
        parameters={
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "YYYY-MM-DD."},
                "limit": {"type": "integer",
                          "description": "Max sessions to return, up to 50."},
                "detail": {"type": "boolean", "description": (
                    "Set true to read per-workout measures, derivations or "
                    "periods. Without it nothing below applies.")},
                "activity": {"type": "string", "description":
                             "Only with detail. e.g. Cycling, Running."},
                "measures": {"type": "array", "items": {"type": "string"},
                             "description": (
                    "Only with detail. Workout measure keys such as "
                    "HKQuantityTypeIdentifierHeartRate:average.")},
                "derive": {"type": "array", "items": {"type": "string", "enum":
                           ["pace", "speed", "efficiency", "ratio",
                            "percentile"]},
                           "description": (
                    "Only with detail. ratio and percentile need period and "
                    "ratio_of or percentile_of.")},
                "ratio_of": {"type": "array", "items": {"type": "string"},
                             "description": "[numerator, denominator] measures."},
                "percentile_of": {"type": "string", "description":
                                  "The one measure to take percentiles of."},
                "period": {"type": "string", "enum": ["day", "week", "month"],
                           "description": (
                    "Only with detail. Group workouts by period, each measure "
                    "summarized by median.")},
                "indoor": {"type": "boolean", "description": (
                    "Only with detail. True for indoor workouts only, false "
                    "for outdoor only; leave it out for both. Workouts the "
                    "export did not flag match neither.")},
                "exclude_source": {"type": "array",
                                   "items": {"type": "string"}},
                "samples": {"type": "boolean", "description": (
                    "Only with detail and measures, and not with period. "
                    "Count the raw records inside each workout.")},
            },
        },
        handler=_get_workouts,
    ),
    Tool(
        name="compare_windows",
        description=(
            "Two spans of time side by side for one metric or one workout "
            "measure, either side of a dated event or between two explicit "
            "ranges. Use for any question of the form 'before and after', "
            "'has X changed since Y', or 'how does this year compare with "
            "last'. Returns both sides and a verdict on whether they can be "
            "compared at all; it never returns a gap between them. A unit of "
            "\"mixed\" beside a null figure means the records for that period "
            "were in more than one unit and nothing was added up; report it "
            "as a refusal, never as a unit. A null `unit` beside a "
            "`unit_note` means those records name no unit at all; report "
            "that, never a unit of your own."
        ),
        parameters={
            "type": "object",
            "properties": {
                "metric": {"type": "string", "description":
                           "A HealthKit metric name, e.g. resting-hr."},
                "activity": {"type": "string", "description":
                             "A workout activity instead of a metric."},
                "measure": {"type": "string", "description":
                            "A workout measure key, with activity."},
                "derive": {"type": "string", "enum":
                           ["pace", "speed", "efficiency"]},
                "anchor_date": {"type": "string", "description": "YYYY-MM-DD."},
                "anchor_fact": {"type": "string", "description":
                                "Name of a dated row in the clinical record."},
                "before_days": {"type": "integer"},
                "after_days": {"type": "integer"},
                "left_start": {"type": "string"},
                "left_end": {"type": "string"},
                "right_start": {"type": "string"},
                "right_end": {"type": "string"},
                "period": {"type": "string", "enum": ["day", "week", "month"]},
                "indoor": {"type": "boolean"},
                "exclude_source": {"type": "array",
                                   "items": {"type": "string"}},
                "sample_counts": {"type": "boolean", "description":
                                  "Also report the records behind each side, "
                                  "and for a workout measure the median "
                                  "records inside each workout."},
            },
        },
        handler=_compare_windows,
    ),
    Tool(
        name="search_records",
        description=(
            "Search the text of the user's medical records and personal notes. "
            "Use for context, symptoms, what a report said in prose, or anything "
            "the user wrote down. Returns snippets with citations. Not for "
            "numeric lab values — use get_lab_trend for those."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "What to look for, in natural language."},
                "kind": {
                    "type": "string",
                    "enum": ["note", "pdf", "image"],
                    "description": (
                        "Restrict to personal notes, to medical records (PDFs), "
                        "or to photos and screenshots read by OCR. Omit to "
                        "search all three."
                    ),
                },
                "limit": {"type": "integer",
                          "description": f"Max snippets, up to {MAX_SEARCH_RESULTS}."},
            },
            "required": ["query"],
        },
        handler=_search_records,
    ),
    Tool(
        name="search_medical_literature",
        description=(
            "Search a local corpus of published medical research for what the "
            "evidence says about a topic. Use this for ANY general medical "
            "claim — what a marker is associated with, what a threshold is, "
            "what research has found — because you must not state such things "
            "from your own knowledge. Returns individual findings, each with a "
            "dated citation and an evidence tier. These describe populations, "
            "NOT this person; use the other tools for their own data."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "The topic, in natural language."},
                "min_tier": {
                    "type": "string",
                    "enum": list(lit_tiers.TIERS),
                    "description": (
                        "Floor on study design, strongest to weakest. Most "
                        "primary research in PubMed carries no study-design "
                        "tag, so ANY floor excludes the majority of the "
                        "corpus, not just weak studies. Without it, results "
                        "are ordered strongest-first among the relevance "
                        "matches. Set it only when "
                        "the user asks for evidence at a stated strength, and "
                        "if it returns nothing, retry without it."
                    ),
                },
                "since_year": {"type": "integer",
                               "description": "Only findings published since."},
                "limit": {"type": "integer",
                          "description": f"Max findings, up to {MAX_LITERATURE_FINDINGS}."},
            },
            "required": ["query"],
        },
        handler=_search_medical_literature,
    ),
    Tool(
        name="get_clinical_history",
        description=(
            "What this person's health systems recorded about them: "
            "diagnoses, prescriptions, allergies, immunizations, and "
            "procedures. The data comes from the clinical-records folder of "
            "their Apple Health export, so it is the health system's record, "
            "not what the person typed and not what a watch measured. Use for "
            "questions about conditions, medications, allergies, vaccines, or "
            "past procedures. Returns records with dates and the file each "
            "came from. Lab values are in get_lab_trend and vitals in "
            "get_clinical_vitals."
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": sorted(_FACT_KIND_ARG),
                    "description": (
                        "Restrict to one kind of record. Omit to return all "
                        "kinds."
                    ),
                },
                "since": {"type": "string",
                          "description": "YYYY-MM-DD. Only records from then on."},
            },
        },
        handler=_get_clinical_history,
    ),
    Tool(
        name="get_clinical_vitals",
        description=(
            "Vital signs a clinician measured at an encounter (blood "
            "pressure, pulse, oxygen saturation, temperature, weight, and "
            "similar), as recorded by this person's health systems and "
            "delivered through their Apple Health export. These are not "
            "wearable readings: use query_healthkit for anything a watch or "
            "phone recorded, and never merge the two into one series. "
            "Without a metric it returns the most recent measurements of every "
            "vital together with a list of which vitals exist."
        ),
        parameters={
            "type": "object",
            "properties": {
                "metric": {"type": "string",
                           "description": ("A vital name or key, e.g. pulse, "
                                           "spo2. Omit to see what exists.")},
                "start_date": {"type": "string", "description": "YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "YYYY-MM-DD."},
                "limit": {"type": "integer",
                          "description": (f"Max measurements, up to "
                                          f"{MAX_CLINICAL_VITALS}.")},
            },
        },
        handler=_get_clinical_vitals,
    ),
)

BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}


def schemas() -> list[dict]:
    return [t.schema() for t in TOOLS]


def dispatch(ctx: ToolContext, name: str, arguments: dict) -> dict:
    """Run one tool call. Errors come back as data, never as exceptions.

    A tool that raises would end the conversation; a tool that returns an error
    lets the model correct itself — usually by fixing an argument — which is the
    difference between a failed answer and a slower one.
    """
    tool = BY_NAME.get(name)
    if tool is None:
        return {"error": f"unknown tool {name!r}",
                "available_tools": list(BY_NAME)}
    if not isinstance(arguments, dict):
        return {"error": "arguments must be an object"}
    try:
        return tool.handler(ctx, arguments)
    except Exception as exc:  # noqa: BLE001
        log.warning("tool %s failed (%s)", name, type(exc).__name__)
        return {"error": f"{type(exc).__name__} while running {name}"}


def serialize(payload: dict) -> str:
    return json.dumps(payload, default=str, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Data inventory for the system prompt
# --------------------------------------------------------------------------- #

def data_inventory(conn, *, max_metrics: int = 25,
                   max_analytes: int = 30) -> dict:
    """A compact description of what this index actually contains.

    Injected into the system prompt rather than exposed as a fourth tool. A model
    that doesn't know which metrics exist will guess names, burn a turn on a
    failed lookup, and sometimes answer about a metric the user doesn't track.
    Telling it up front costs a few hundred tokens once and removes that whole
    failure class.
    """
    summary = queries.index_summary(conn)
    notes = queries.note_summary(conn)
    docs = queries.document_summary(conn)

    metric_names = []
    for row in queries.list_types(conn, limit=max_metrics):
        metric = metrics.lookup(row["type"])
        metric_names.append(metric.aliases[0] if metric and metric.aliases
                            else row["type"])

    analytes = [a["analyte_key"] for a in
                queries.list_analytes(conn, limit=max_analytes)]
    tags = [t["tag"] for t in queries.list_tags(conn, limit=25)]
    holdings = queries.clinical_holdings(conn)
    clinical = holdings["facts"]
    vital_metrics = holdings["vital_metrics"]

    return {
        "healthkit": {
            "records": summary["records"],
            "date_range": [summary["first_date"], summary["last_date"]],
            "metrics_available": metric_names,
        },
        "labs": {
            "reports": docs["documents"],
            "date_range": [docs["doc_first"], docs["doc_last"]],
            "analytes_available": analytes,
        },
        "notes": {
            "count": notes["notes"],
            # Photos are notes, not reports: the model learns here that
            # kind="image" has something to return, and labs.reports never
            # counts them.
            "images": notes["images"],
            "date_range": [notes["first"], notes["last"]],
            "tags": tags,
        },
        # Records a health system sent back through the export, as distinct
        # from anything this person typed and from anything their watch
        # recorded. Named here so the model does not guess whether they exist.
        # Every clinical kind the index holds, not just the facts: the system
        # prompt appends "Anything not listed above is not available", so a
        # block that omitted the labs and note references would turn this into
        # a false statement about the person's own records.
        "clinical": {
            # Plural, matching the `kind` argument get_clinical_history takes.
            **{_FACT_KIND_PLURAL.get(kind, kind): info["count"]
               for kind, info in clinical.items()},
            "lab_values": holdings["labs"],
            "clinician_vitals": holdings["vitals"],
            "note_references": holdings["note_references"],
            "date_range": [holdings["first"], holdings["last"]],
            "vital_metrics_available": [m["metric_key"] for m in vital_metrics[:25]],
        },
    }
