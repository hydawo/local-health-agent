"""Read layer over the structured store.

This is deliberately a separate module from the CLI. At milestone 5 the agent's
`query_healthkit` tool calls these same functions, so the CLI and the LLM see
identical numbers by construction rather than by two implementations agreeing
(plan §4: "the agent's tool interface is built as a clean contract separate from
the model").

**The multi-source problem.** A real export has several devices writing the same
metric: an iPhone and an Apple Watch both record step counts, a Watch and an Oura
ring both record sleep. Naively summing across sources roughly doubles every
cumulative total. Because there is no principled way to merge overlapping samples
without per-sample interval reconciliation, the default here is to compute each
source's total independently and report the largest — the number Apple's own
Health app approximates — and to *tell the caller* how many periods had more than
one contributing source, so a suspicious total is visible rather than silent.
`--combine sum` and `--source NAME` are available when the caller knows better.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Sequence

from ..labs import slug as _labs_slug
from .. import metrics
from ..metrics import Kind, Metric
from . import derive

PERIODS = ("day", "week", "month")
AGGREGATIONS = ("sum", "avg", "min", "max", "count", "duration")

# Aggregations whose across-source combination is a real choice (they add up).
ADDITIVE = {"sum", "count", "duration"}


@dataclass
class SeriesPoint:
    period: str
    period_start: str
    period_end: str
    value: float | None
    n: int
    sources: tuple[str, ...]
    per_source: dict[str, float]
    category: str | None = None


@dataclass
class SeriesResult:
    metric: Metric
    agg: str
    period: str
    combine: str
    unit: str | None
    points: list[SeriesPoint] = field(default_factory=list)
    units_seen: tuple[str, ...] = ()
    sources_seen: tuple[str, ...] = ()
    multi_source_periods: int = 0
    # Whole-index context for this metric, used to answer honestly when a query
    # window comes back empty (plan §5a missing-data behavior).
    total_records: int = 0
    available_from: str | None = None
    available_to: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.points


# --------------------------------------------------------------------------- #
# Period bucketing
# --------------------------------------------------------------------------- #

def bucket(local_date: str, period: str) -> tuple[str, str, str]:
    """Map a YYYY-MM-DD to (label, period_start, period_end)."""
    day = date.fromisoformat(local_date)
    if period == "day":
        iso = day.isoformat()
        return iso, iso, iso
    if period == "week":
        # Labelled by the Monday that starts the week; matches the weekly_metric
        # view's `date(local_date, 'weekday 0', '-6 days')`.
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=6)
        return start.isoformat(), start.isoformat(), end.isoformat()
    if period == "month":
        start = day.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return start.strftime("%Y-%m"), start.isoformat(), end.isoformat()
    raise ValueError(f"unknown period: {period!r}")


# --------------------------------------------------------------------------- #
# Index-level summaries
# --------------------------------------------------------------------------- #

def index_summary(conn: sqlite3.Connection) -> dict:
    # Everything here reads the 100k-row daily rollup rather than the raw record
    # table. On a real 8.3M-record index that is the difference between 19
    # seconds and 20 milliseconds: `COUNT(*) ... GROUP BY source_name` over the
    # raw rows is a full scan, and `stats` is the first command anyone runs.
    row = conn.execute(
        "SELECT COALESCE(SUM(n), 0) AS n, MIN(local_date) AS first, "
        "MAX(local_date) AS last FROM daily_metric"
    ).fetchone()
    workouts = conn.execute(
        "SELECT COUNT(*) AS n, MIN(local_date) AS first, MAX(local_date) AS last "
        "FROM workout"
    ).fetchone()
    types = conn.execute(
        "SELECT COUNT(DISTINCT type) AS n FROM daily_metric"
    ).fetchone()["n"]
    sources = [
        r["source_name"]
        for r in conn.execute(
            "SELECT source_name, SUM(n) AS n FROM daily_metric "
            "WHERE source_name IS NOT NULL GROUP BY source_name ORDER BY n DESC"
        )
    ]
    files = conn.execute(
        "SELECT path, kind, size_bytes, record_count, ingested_at, status "
        "FROM source_file ORDER BY ingested_at"
    ).fetchall()
    summaries = conn.execute(
        "SELECT COUNT(*) AS n FROM activity_summary"
    ).fetchone()["n"]
    return {
        "records": row["n"],
        "first_date": row["first"],
        "last_date": row["last"],
        "distinct_types": types,
        "sources": sources,
        "workouts": workouts["n"],
        "workouts_first": workouts["first"],
        "workouts_last": workouts["last"],
        "activity_summaries": summaries,
        "files": [dict(f) for f in files],
    }


def list_types(conn: sqlite3.Connection, search: str | None = None,
               limit: int | None = None) -> list[dict]:
    sql = (
        "SELECT type, SUM(n) AS n, MIN(local_date) AS first, "
        "MAX(local_date) AS last, COUNT(DISTINCT source_name) AS sources "
        "FROM daily_metric "
    )
    params: list = []
    if search:
        sql += "WHERE lower(type) LIKE ? "
        params.append(f"%{search.lower()}%")
    sql += "GROUP BY type ORDER BY n DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def type_coverage(conn: sqlite3.Connection, identifier: str) -> tuple[int, str | None, str | None]:
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(local_date) AS first, MAX(local_date) AS last "
        "FROM daily_metric WHERE type = ?",
        (identifier,),
    ).fetchone()
    total = conn.execute(
        "SELECT COALESCE(SUM(n), 0) AS n FROM daily_metric WHERE type = ?",
        (identifier,),
    ).fetchone()["n"]
    if not row or row["n"] == 0:
        return 0, None, None
    return int(total), row["first"], row["last"]


# Sleep segments are attributed to a *night* using a noon boundary: anything
# starting after 12:00 belongs to that date's night, anything before belongs to
# the previous one. Without this, a night's sleep splits across two calendar
# dates — the segments before midnight land on one, the deep and REM stages
# after midnight on the next — and "how much deep sleep did I get on the night
# of the 4th" answers "none", which is both wrong and confidently stated.
SLEEP_NIGHT_SQL = """
SELECT CASE WHEN CAST(substr(start_date, 12, 2) AS INTEGER) >= 12
            THEN local_date
            ELSE date(local_date, '-1 day') END AS night,
       value_text AS stage,
       SUM(duration_sec)  AS seconds,
       COUNT(*)           AS n,
       MIN(start_date)    AS first_start,
       MAX(end_date)      AS last_end,
       source_name
FROM record
WHERE type = 'HKCategoryTypeIdentifierSleepAnalysis'
GROUP BY night, stage, source_name
"""

ASLEEP_PREFIX = "HKCategoryValueSleepAnalysisAsleep"


def sleep_nights(conn: sqlite3.Connection, start: str | None = None,
                 end: str | None = None) -> list[dict]:
    """Sleep totals per night, with stages broken out.

    Returns one entry per night, each with `stages` (seconds per stage),
    `asleep_seconds` (every Asleep* stage summed), and `in_bed_seconds`.
    Nights are ordered oldest first.
    """
    rows = conn.execute(SLEEP_NIGHT_SQL).fetchall()

    by_night: dict[str, dict] = {}
    for row in rows:
        night = row["night"]
        if not night or (start and night < start) or (end and night > end):
            continue
        entry = by_night.setdefault(night, {
            "night": night, "stages": {}, "asleep_seconds": 0.0,
            "in_bed_seconds": 0.0, "sources": set(),
            "first_start": row["first_start"], "last_end": row["last_end"],
        })
        stage = (row["stage"] or "Unknown").replace("HKCategoryValueSleepAnalysis", "")
        seconds = row["seconds"] or 0.0
        entry["stages"][stage] = entry["stages"].get(stage, 0.0) + seconds
        if (row["stage"] or "").startswith(ASLEEP_PREFIX):
            entry["asleep_seconds"] += seconds
        elif row["stage"] == "HKCategoryValueSleepAnalysisInBed":
            entry["in_bed_seconds"] += seconds
        if row["source_name"]:
            entry["sources"].add(row["source_name"])
        entry["first_start"] = min(entry["first_start"], row["first_start"])
        entry["last_end"] = max(entry["last_end"] or "", row["last_end"] or "")

    out = []
    for night in sorted(by_night):
        entry = by_night[night]
        entry["sources"] = sorted(entry["sources"])
        out.append(entry)
    return out


def sleep_coverage(conn: sqlite3.Connection) -> tuple[int, str | None, str | None]:
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(local_date) AS first, MAX(local_date) AS last "
        "FROM record WHERE type = 'HKCategoryTypeIdentifierSleepAnalysis'"
    ).fetchone()
    return (row["n"] or 0), row["first"], row["last"]


def list_workouts(conn: sqlite3.Connection, start: str | None = None,
                  end: str | None = None, limit: int = 50) -> list[dict]:
    sql = ("SELECT activity_type, local_date, duration, duration_unit, "
           "total_distance, total_distance_unit, total_energy, "
           "total_energy_unit, source_name FROM workout WHERE 1=1 ")
    params: list = []
    if start:
        sql += "AND local_date >= ? "
        params.append(start)
    if end:
        sql += "AND local_date <= ? "
        params.append(end)
    sql += "ORDER BY start_epoch DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def workout_coverage(conn: sqlite3.Connection) -> tuple[int, str | None, str | None]:
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(local_date) AS first, MAX(local_date) AS last "
        "FROM workout"
    ).fetchone()
    return (row["n"] or 0), row["first"], row["last"]


def workout_summary(conn: sqlite3.Connection, start: str | None = None,
                    end: str | None = None, limit: int = 20) -> list[dict]:
    sql = (
        "SELECT activity_type, COUNT(*) AS n, "
        "SUM(duration) AS total_duration, duration_unit, "
        "SUM(total_distance) AS total_distance, total_distance_unit, "
        "SUM(total_energy) AS total_energy, total_energy_unit, "
        "MIN(local_date) AS first, MAX(local_date) AS last "
        "FROM workout WHERE 1=1 "
    )
    params: list = []
    if start:
        sql += "AND local_date >= ? "
        params.append(start)
    if end:
        sql += "AND local_date <= ? "
        params.append(end)
    sql += ("GROUP BY activity_type, duration_unit, total_distance_unit, "
            "total_energy_unit ORDER BY n DESC LIMIT ?")
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


@dataclass(frozen=True)
class WorkoutRow:
    local_date: str
    activity_type: str
    duration: float | None
    duration_unit: str | None
    total_distance: float | None
    total_distance_unit: str | None
    total_energy: float | None
    total_energy_unit: str | None
    source_name: str | None
    indoor: int | None
    citation: str
    measures: dict
    derived: dict
    # {base_type: {"count": int, "window_seconds": float, "bounds": "inclusive"}}
    # None only when sample_counts was not requested. When requested it is a
    # dict: {} if no statistic measure was among those asked for, and an entry
    # with count None and a reason where the window cannot be computed.
    # One entry per distinct statistic base type requested. The window is
    # start_epoch <= sample <= start_epoch + duration, so a sample on the
    # boundary of two back-to-back workouts counts for both, and one that
    # starts before the workout and overlaps it is excluded.
    sample_depth: dict | None = None


# Derivations that need more than one row, or two named measures, and so are
# not available per workout. Task 6 offers them at the series level.
_SERIES_ONLY_DERIVATIONS = ("ratio", "percentile")

_HEART_RATE_AVERAGE = "HKQuantityTypeIdentifierHeartRate:average"

def _source_clauses(source_include, source_exclude, column="source_name"):
    """Shared include/exclude SQL. Naming a source in both is an error rather
    than a precedence rule nobody would remember."""
    include = tuple(source_include or ())
    exclude = tuple(source_exclude or ())
    both = set(include) & set(exclude)
    if both:
        raise ValueError(
            f"source named in both include and exclude: {sorted(both)}")
    sql, params = "", []
    if include:
        sql += f" AND {column} IN ({','.join('?' * len(include))})"
        params += list(include)
    if exclude:
        sql += (f" AND ({column} IS NULL OR {column} NOT IN "
                f"({','.join('?' * len(exclude))}))")
        params += list(exclude)
    return sql, params


def workout_detail(conn, *, activity=None, start=None, end=None, indoor=None,
                   source_include=(), source_exclude=(), measures=(),
                   derive_names=(), sample_counts=False,
                   limit=200) -> list[WorkoutRow]:
    """One row per workout, with the measures and derivations requested."""
    if sample_counts and not measures:
        raise ValueError(
            "sample_counts needs at least one statistic in measures, "
            "to say which record type to count")
    for name in derive_names:
        if name in _SERIES_ONLY_DERIVATIONS:
            raise ValueError(
                f"{name} needs more than one workout; use workout_series")
        if name not in derive.DERIVATIONS:
            raise ValueError(f"unknown derivation: {name}")

    sql = ("SELECT id, local_date, activity_type, duration, duration_unit, "
           "total_distance, total_distance_unit, total_energy, "
           "total_energy_unit, source_name, indoor, start_epoch "
           "FROM workout WHERE 1=1")
    params: list = []
    if activity:
        sql += " AND activity_type LIKE ?"
        params.append(f"%{activity}%")
    if start:
        sql += " AND local_date >= ?"
        params.append(start)
    if end:
        sql += " AND local_date <= ?"
        params.append(end)
    if indoor is True:
        sql += " AND indoor = 1"
    elif indoor is False:
        sql += " AND indoor = 0"
    clause, extra = _source_clauses(source_include, source_exclude)
    sql += clause
    params += extra
    sql += " ORDER BY start_epoch DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []

    wanted = tuple(measures)
    by_workout: dict[int, dict] = {r["id"]: {} for r in rows}
    if wanted:
        placeholders = ",".join("?" * len(rows))
        keys = ",".join("?" * len(wanted))
        for stat in conn.execute(
                f"SELECT workout_id, kind, key, value_num, value_text, unit "
                f"FROM workout_stat WHERE workout_id IN ({placeholders}) "
                f"AND key IN ({keys})",
                [r["id"] for r in rows] + list(wanted)):
            by_workout[stat["workout_id"]][stat["key"]] = {
                "kind": stat["kind"],
                "value": stat["value_num"],
                "text": stat["value_text"],
                "unit": stat["unit"],
            }

    out: list[WorkoutRow] = []
    for r in rows:
        held = by_workout[r["id"]]
        found = {key: held.get(key, {"kind": None, "value": None, "text": None,
                                      "unit": None})
                 for key in wanted}
        hr = found.get(_HEART_RATE_AVERAGE)
        derived = {}
        for name in derive_names:
            if name == "pace":
                d = derive.pace(r["total_distance"], r["total_distance_unit"],
                                r["duration"], r["duration_unit"])
            elif name == "speed":
                d = derive.speed(r["total_distance"], r["total_distance_unit"],
                                 r["duration"], r["duration_unit"])
            else:
                d = derive.efficiency(
                    r["total_distance"], r["total_distance_unit"],
                    r["duration"], r["duration_unit"],
                    (hr or {}).get("value"), (hr or {}).get("unit"))
            derived[name] = {"value": d.value, "unit": d.unit,
                             "reason": d.reason}
        depth = None
        if sample_counts:
            # Keyed off the requested keys, not the held ones: the records
            # are counted in the time window whether or not the workout
            # carries a statistic for the type. A statistic key has a colon;
            # Apple's metadata keys carry none.
            bases = sorted({k.split(":")[0] for k in wanted if ":" in k})
            depth = {b: _sample_depth(conn, r, b) for b in bases}
        activity_name = r["activity_type"] or "workout"
        out.append(WorkoutRow(
            local_date=r["local_date"], activity_type=activity_name,
            duration=r["duration"], duration_unit=r["duration_unit"],
            total_distance=r["total_distance"],
            total_distance_unit=r["total_distance_unit"],
            total_energy=r["total_energy"],
            total_energy_unit=r["total_energy_unit"],
            source_name=r["source_name"], indoor=r["indoor"],
            citation=f"{activity_name} on {r['local_date']}",
            measures=found, derived=derived, sample_depth=depth))
    return out


def _sample_depth(conn, row, base_type: str) -> dict:
    """How many raw records of one type fall inside the workout, with the
    window that was counted. When the duration or its unit is unusable the
    count and window are None and `reason` says why.

    The interval end comes from the stored duration rather than `end_date`,
    because duration and its unit are both columns and need no parsing. The
    bounds are inclusive at both ends. Uses `idx_record_type_epoch`.
    """
    window = derive.seconds(row["duration"], row["duration_unit"])
    if window is None:
        if row["duration"] is None:
            reason = "workout has no duration"
        elif derive._finite(row["duration"]) is None:
            reason = "workout duration is not a finite number"
        else:
            reason = (f"duration unit not recognized: "
                      f"{derive._label(row['duration_unit'])}")
        return {"count": None, "window_seconds": None,
                "bounds": "inclusive", "reason": reason}
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM record WHERE type = ? "
        "AND start_epoch >= ? AND start_epoch <= ?",
        (base_type, row["start_epoch"], row["start_epoch"] + window),
    ).fetchone()["n"]
    return {"count": count, "window_seconds": window,
            "bounds": "inclusive", "reason": None}


def workout_measures(conn) -> list[dict]:
    """One row per measure the index holds, for every absence message to read.

    Each row carries the key, its kind, a readable label, a count, the date
    span and a unit. The label comes from the metric registry when the base
    type is registered, and is the bare key otherwise.
    """
    rows = conn.execute(
        "SELECT s.kind, s.key, COUNT(*) AS n, MIN(w.local_date) AS first, "
        "MAX(w.local_date) AS last, MAX(s.unit) AS unit "
        "FROM workout_stat s JOIN workout w ON w.id = s.workout_id "
        "GROUP BY s.kind, s.key ORDER BY n DESC, s.key"
    ).fetchall()
    out = []
    for row in rows:
        base = row["key"].split(":")[0]
        metric = metrics.lookup(base)          # None for an unregistered type
        item = dict(row)
        suffix = row["key"].partition(":")[2]
        if metric:
            item["label"] = (f"{metric.label} ({suffix})" if suffix
                             else metric.label)
        else:
            item["label"] = row["key"]
        out.append(item)
    return out


def workout_stat_coverage(conn) -> dict:
    """How many workouts carry any statistic, and any metadata entry.

    `workout_measures` counts rows per key, so it cannot say how many
    workouts there are behind them: one workout carries many keys.
    """
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM workout) AS workouts, "
        "(SELECT COUNT(DISTINCT workout_id) FROM workout_stat "
        " WHERE kind = 'statistic') AS with_statistics, "
        "(SELECT COUNT(DISTINCT workout_id) FROM workout_stat "
        " WHERE kind = 'metadata') AS with_metadata").fetchone()
    return {"workouts": row["workouts"] or 0,
            "with_statistics": row["with_statistics"] or 0,
            "with_metadata": row["with_metadata"] or 0}


@dataclass(frozen=True)
class WorkoutSeriesPoint:
    period: str
    period_start: str
    period_end: str
    n: int
    # Summed over the period. value None with unit None: no workout reported
    # the figure. value None with unit "mixed": workouts reported it in
    # different units, and units are never converted, so the tool refused to
    # add them.
    duration: float | None
    distance: float | None
    energy: float | None
    measures: dict
    derived: dict
    duration_unit: str | None = None
    distance_unit: str | None = None
    energy_unit: str | None = None


@dataclass(frozen=True)
class WorkoutSeries:
    """Mirrors SeriesResult's field names (points, unit, is_empty,
    total_records, available_from, available_to) so a caller can read either
    without asking which it has. Separate because SeriesResult carries a
    Metric and a workout series has none."""
    label: str
    period: str
    unit: str | None
    points: list[WorkoutSeriesPoint] = field(default_factory=list)
    total_records: int = 0
    available_from: str | None = None
    available_to: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.points


def _summed(rows, value_attr: str, unit_attr: str):
    """(sum, unit) over the rows that carry the figure; (None, None) when none
    do, (None, "mixed") when their units differ."""
    pairs = [(getattr(r, value_attr), getattr(r, unit_attr)) for r in rows
             if getattr(r, value_attr) is not None]
    if not pairs:
        return None, None
    units = {u for _, u in pairs}
    if len(units) != 1:
        return None, _MIXED
    return sum(v for v, _ in pairs), units.pop()


_MIXED = "mixed"
_UNLABELLED = "unlabelled"


def _median_in_one_unit(pairs, name: str) -> dict:
    """Median of (value, unit) pairs, refused when the units differ.

    The reason names the units seen, and `units` lists them, rather than
    picking one to label a figure that mixes them."""
    # An absent unit is a unit of its own, as in _summed: assuming an
    # unlabelled value shares its neighbours' unit would be assuming a unit.
    seen = {u or None for _, u in pairs}
    units = sorted(u or _UNLABELLED for u in seen)
    if len(seen) > 1:
        return {"value": None, "unit": _MIXED, "units": units,
                "reason": f"{name} reported in more than one unit: "
                          f"{', '.join(units)}"}
    unit = next(iter(seen)) if seen else None
    d = derive.median([v for v, _ in pairs], unit)
    return {"value": d.value, "unit": d.unit, "units": units,
            "reason": None if d.value is not None else "no value in this period"}


def _entry(d) -> dict:
    # `units` is carried here as well as on the measure entries. A caller
    # explaining a refused derived figure has to name the units that clashed,
    # and an entry that leaves the key off sends it looking elsewhere for
    # them, which is how an explanation ends up quoting another side's unit.
    return {"value": d.value, "unit": d.unit,
            "units": [d.unit] if d.unit and d.unit != _MIXED else [],
            "reason": d.reason}


def workout_series(conn, *, period="week", activity=None, start=None, end=None,
                   indoor=None, source_include=(), source_exclude=(),
                   measures=(), derive_names=(), ratio_of=None,
                   percentile_of=None) -> WorkoutSeries:
    """Workouts grouped by period, with each measure summarized by median.

    Median rather than mean, so one 4-hour ride cannot carry a week.

    `ratio` and `percentile` are named, not positional. `ratio_of` is a
    (numerator, denominator) pair and `percentile_of` one measure, each of
    which must be among `measures`. Without them, `percentile` is taken only
    when exactly one measure was asked for, and `ratio` is refused with a
    reason, so reordering `measures` can never change what is divided.
    """
    if period not in PERIODS:
        raise ValueError(f"period must be one of {PERIODS}")
    for name in derive_names:
        if name not in derive.DERIVATIONS:
            raise ValueError(f"unknown derivation: {name}")
    if ratio_of is not None:
        if "ratio" not in derive_names:
            raise ValueError("ratio_of was given but ratio is not in "
                             "derive_names")
        if len(ratio_of) != 2:
            raise ValueError("ratio_of is a (numerator, denominator) pair")
        if ratio_of[0] == ratio_of[1]:
            raise ValueError("ratio_of names the same measure twice")
        for m in ratio_of:
            if m not in measures:
                raise ValueError(f"ratio_of names a measure not requested: {m}")
    if percentile_of is not None:
        if "percentile" not in derive_names:
            raise ValueError("percentile_of was given but percentile is not "
                             "in derive_names")
        if percentile_of not in measures:
            raise ValueError(
                f"percentile_of names a measure not requested: {percentile_of}")

    rows = workout_detail(
        conn, activity=activity, start=start, end=end, indoor=indoor,
        source_include=source_include, source_exclude=source_exclude,
        measures=measures,
        derive_names=tuple(n for n in derive_names
                           if n not in _SERIES_ONLY_DERIVATIONS),
        limit=1_000_000)
    total, first, last = workout_coverage(conn)
    label = activity or "all workouts"
    if not rows:
        return WorkoutSeries(label=label, period=period, unit=None,
                             total_records=total, available_from=first,
                             available_to=last)

    buckets: dict[str, list[WorkoutRow]] = {}
    spans: dict[str, tuple[str, str]] = {}
    for row in rows:
        key, first_day, last_day = bucket(row.local_date, period)
        buckets.setdefault(key, []).append(row)
        spans[key] = (first_day, last_day)

    measure_units = {m.get("unit") or None for row in rows
                     for m in row.measures.values()
                     if m.get("value") is not None}
    if len(measure_units) > 1:
        unit = _MIXED
    else:
        unit = next(iter(measure_units), None)

    pct_measure = percentile_of or (measures[0] if len(measures) == 1 else None)
    points = []
    for key in sorted(buckets):
        group = buckets[key]
        summarized = {}
        for measure in measures:
            pairs = [(r.measures[measure]["value"], r.measures[measure].get("unit"))
                     for r in group
                     if r.measures.get(measure, {}).get("value") is not None]
            m = _median_in_one_unit(pairs, measure)
            summarized[measure] = {
                "median": m["value"], "n": len(pairs),
                "missing": len(group) - len(pairs),
                "unit": m["unit"], "units": m["units"], "reason": m["reason"],
            }
        derived = {}
        for name in derive_names:
            if name == "percentile":
                if pct_measure is None:
                    for p in (50, 90):
                        derived[f"p{p}"] = {
                            "value": None, "unit": None, "units": [],
                            "reason": "percentile needs percentile_of when "
                                      "more than one measure is requested"}
                    continue
                values = [r.measures[pct_measure]["value"] for r in group
                          if r.measures.get(pct_measure, {}).get("value")
                          is not None]
                pct_unit = summarized[pct_measure]["unit"]
                for p in (50, 90):
                    if pct_unit == _MIXED:
                        derived[f"p{p}"] = {
                            "value": None, "unit": _MIXED,
                            "units": summarized[pct_measure]["units"],
                            "reason": summarized[pct_measure]["reason"]}
                    else:
                        derived[f"p{p}"] = _entry(
                            derive.percentile(values, p, pct_unit))
            elif name == "ratio":
                if ratio_of is None:
                    derived["ratio"] = {
                        "value": None, "unit": None, "units": [],
                        "reason": "ratio needs ratio_of, a (numerator, "
                                  "denominator) pair of requested measures"}
                else:
                    a, b = summarized[ratio_of[0]], summarized[ratio_of[1]]
                    refused = next((x for x in (a, b) if x["unit"] == _MIXED),
                                   None)
                    if refused:
                        derived["ratio"] = {"value": None, "unit": _MIXED,
                                            "units": refused["units"],
                                            "reason": refused["reason"]}
                        continue
                    derived["ratio"] = _entry(derive.ratio(
                        "ratio", a["median"], a["unit"],
                        b["median"], b["unit"]))
            else:
                pairs = [(r.derived[name]["value"], r.derived[name].get("unit"))
                         for r in group
                         if r.derived.get(name, {}).get("value") is not None]
                m = _median_in_one_unit(pairs, name)
                derived[name] = {"value": m["value"], "unit": m["unit"],
                                 "units": m["units"], "reason": m["reason"]}
        dur, dur_u = _summed(group, "duration", "duration_unit")
        dist, dist_u = _summed(group, "total_distance", "total_distance_unit")
        en, en_u = _summed(group, "total_energy", "total_energy_unit")
        points.append(WorkoutSeriesPoint(
            period=key, period_start=spans[key][0], period_end=spans[key][1],
            n=len(group), duration=dur, distance=dist, energy=en,
            measures=summarized, derived=derived, duration_unit=dur_u,
            distance_unit=dist_u, energy_unit=en_u))
    return WorkoutSeries(label=label, period=period, unit=unit, points=points,
                         total_records=total, available_from=first,
                         available_to=last)


# --------------------------------------------------------------------------- #
# Metric series
# --------------------------------------------------------------------------- #

def metric_series(
    conn: sqlite3.Connection,
    metric: Metric,
    *,
    period: str = "day",
    agg: str | None = None,
    start: str | None = None,
    end: str | None = None,
    source: str | None = None,
    source_exclude: Sequence[str] = (),
    combine: str = "auto",
) -> SeriesResult:
    """Aggregate one metric over time, from the materialized daily rollup.

    `agg=None` uses the metric's semantically correct default (sum for cumulative
    types, mean for discrete ones, duration for category types) — see metrics.py.
    """
    if period not in PERIODS:
        raise ValueError(f"period must be one of {PERIODS}")
    agg = agg or metric.default_agg
    if agg not in AGGREGATIONS:
        raise ValueError(f"aggregation must be one of {AGGREGATIONS}")

    total_records, available_from, available_to = type_coverage(conn, metric.identifier)

    sql = (
        "SELECT local_date, source_name, unit, category, n, sum_value, avg_value, "
        "min_value, max_value, duration_sec FROM daily_metric WHERE type = ? "
    )
    params: list = [metric.identifier]
    if start:
        sql += "AND local_date >= ? "
        params.append(start)
    if end:
        sql += "AND local_date <= ? "
        params.append(end)
    clause, extra = _source_clauses((source,) if source else (), source_exclude)
    sql += clause + " "
    params += extra
    sql += "ORDER BY local_date"

    rows = conn.execute(sql, params).fetchall()

    # An absent unit is a unit of its own, as in `_median_in_one_unit`:
    # dropping the NULLs made a type whose rows are partly unitless look
    # like one unit, and the layers above then printed the unlabelled values
    # under the name of the labelled ones. A duration aggregate is in
    # seconds by construction and says so below, whatever the rows carry.
    units = {r["unit"] or _UNLABELLED for r in rows}
    sources = sorted({r["source_name"] for r in rows if r["source_name"]})

    if combine == "auto":
        combine = "max" if agg in ADDITIVE else "weighted"

    # (period, category) -> source -> accumulator
    buckets: dict[tuple[str, str | None], dict[str | None, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"n": 0, "sum": 0.0, "dur": 0.0,
                                     "min": None, "max": None})
    )
    spans: dict[str, tuple[str, str]] = {}

    is_category = metric.kind is Kind.CATEGORY
    for row in rows:
        label, p_start, p_end = bucket(row["local_date"], period)
        spans[label] = (p_start, p_end)
        key = (label, row["category"] if is_category else None)
        acc = buckets[key][row["source_name"]]
        acc["n"] += row["n"] or 0
        if row["sum_value"] is not None:
            acc["sum"] += row["sum_value"]
        if row["duration_sec"] is not None:
            acc["dur"] += row["duration_sec"]
        if row["min_value"] is not None:
            acc["min"] = row["min_value"] if acc["min"] is None else min(acc["min"], row["min_value"])
        if row["max_value"] is not None:
            acc["max"] = row["max_value"] if acc["max"] is None else max(acc["max"], row["max_value"])

    points: list[SeriesPoint] = []
    multi_source = 0
    # Sort key coerces a missing category to "": a category type with a record
    # that has no value attribute would otherwise mix None and str and raise.
    for (label, category), by_source in sorted(
        buckets.items(), key=lambda item: (item[0][0], item[0][1] or "")
    ):
        per_source: dict[str, float] = {}
        for src, acc in by_source.items():
            per_source[src or "(unknown)"] = _source_value(acc, agg)
        if len(by_source) > 1 and agg in ADDITIVE:
            multi_source += 1
        value = _combine(by_source, agg, combine)
        total_n = sum(acc["n"] for acc in by_source.values())
        p_start, p_end = spans[label]
        points.append(
            SeriesPoint(
                period=label,
                period_start=p_start,
                period_end=p_end,
                value=value,
                n=total_n,
                sources=tuple(sorted(s or "(unknown)" for s in by_source)),
                per_source=per_source,
                category=category,
            )
        )

    unit = "s" if agg == "duration" else (sorted(units)[0] if len(units) == 1 else None)

    return SeriesResult(
        metric=metric,
        agg=agg,
        period=period,
        combine=combine,
        unit=unit,
        points=points,
        units_seen=tuple(sorted(units)),
        sources_seen=tuple(sources),
        multi_source_periods=multi_source,
        total_records=total_records,
        available_from=available_from,
        available_to=available_to,
    )


# --------------------------------------------------------------------------- #
# Documents and lab results (milestone 2)
# --------------------------------------------------------------------------- #

@dataclass
class LabPoint:
    collected_date: str | None
    value_num: float | None
    value_text: str | None
    unit: str | None
    ref_low: float | None
    ref_high: float | None
    ref_text: str | None
    flag: str | None
    analyte: str
    path: str
    page_no: int
    raw_line: str | None
    via_ocr: bool = False
    source: str = "pdf"            # 'pdf' | 'fhir'; OCR stays in via_ocr
    also_from: tuple[str, ...] = ()  # other sources that reported this same value

    @property
    def reported_unit(self) -> str | None:
        """The unit, but only when there is a value for it to qualify.

        An invalid `valueQuantity` can carry a unit and no number. Storing the
        unit is right, since it is the one thing the resource does say, but
        showing "mg/dL" beside a null value reads as a measurement the lab
        never made.
        """
        if self.value_num is None and self.value_text is None:
            return None
        return self.unit

    @property
    def citation(self) -> str:
        from pathlib import Path as _Path
        parts = [_Path(self.path).name, f"p.{self.page_no}"]
        if self.collected_date:
            parts.append(self.collected_date)
        return ", ".join(parts)

    @property
    def out_of_range(self) -> bool | None:
        """True/False when a range is known, None when the lab printed none."""
        if self.value_num is None:
            return None
        if self.ref_low is None and self.ref_high is None:
            return None
        if self.ref_low is not None and self.value_num < self.ref_low:
            return True
        if self.ref_high is not None and self.value_num > self.ref_high:
            return True
        return False


@dataclass
class LabTrend:
    key: str
    label: str
    points: list[LabPoint] = field(default_factory=list)
    units_seen: tuple[str, ...] = ()
    total_results: int = 0
    available_from: str | None = None
    available_to: str | None = None
    # One entry per date where two sources each reported something the other
    # did not: {"collected_date", "reported": [{"value", "unit", "sources"}]}.
    # Reported, never resolved.
    disagreements: tuple[dict, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.points


def panel_siblings(conn: sqlite3.Connection, analyte_key: str) -> list[dict]:
    """Every other analyte the reports hold from the same panel as `analyte_key`.

    Read off the index rather than the registry, so an analyte that arrived
    with a slugified key still counts: the question this answers is "what else
    do these reports hold", and the registry only knows what it was taught.

    An empty list means the reports hold no sibling, which is a different fact
    from nobody having asked. Callers must pass the distinction on rather than
    omitting the field.
    """
    panel = conn.execute(
        "SELECT panel FROM lab_result WHERE analyte_key = ? AND panel IS NOT NULL "
        "LIMIT 1", (analyte_key,)).fetchone()
    if panel is None or not panel["panel"]:
        return []
    rows = conn.execute(
        "SELECT analyte_key, COUNT(*) AS n, MAX(analyte) AS printed_as, "
        "MIN(collected_date) AS first, MAX(collected_date) AS last "
        "FROM lab_result WHERE panel = ? AND analyte_key <> ? "
        "GROUP BY analyte_key ORDER BY n DESC, analyte_key",
        (panel["panel"], analyte_key))
    return [dict(r) for r in rows]


def list_documents(conn: sqlite3.Connection,
                   kind: str | None = None) -> list[dict]:
    sql = (
        "SELECT d.id, d.path, d.title, d.kind, d.doc_date, d.page_count, "
        "d.extraction, d.ingested_at, "
        "(SELECT COUNT(*) FROM lab_result l WHERE l.document_id = d.id) AS labs, "
        "(SELECT COUNT(*) FROM chunk c WHERE c.document_id = d.id) AS chunks, "
        "(SELECT GROUP_CONCAT(t.tag, ', ') FROM document_tag t "
        " WHERE t.document_id = d.id) AS tags "
        "FROM document d "
    )
    params: list = []
    if kind == "note":
        # The notes surface includes photos; see NOTE_KINDS.
        sql += "WHERE d.kind IN ('note', 'image') "
    elif kind:
        sql += "WHERE d.kind = ? "
        params.append(kind)
    else:
        # No kind means the records surface, which is what this listing meant
        # before the export's clinical-records folder was ingested. A FHIR
        # resource is a document row with no pages and no chunks, and
        # thousands of them would bury the records a person actually dropped
        # in; `kind='fhir'` asks for those on purpose.
        sql += "WHERE d.kind != 'fhir' "
    sql += "ORDER BY COALESCE(d.doc_date, d.ingested_at)"
    return [dict(r) for r in conn.execute(sql, params)]


def list_tags(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    sql = (
        "SELECT t.tag, COUNT(*) AS n, MIN(d.doc_date) AS first, "
        "MAX(d.doc_date) AS last FROM document_tag t "
        "JOIN document d ON d.id = t.document_id "
        "GROUP BY t.tag ORDER BY n DESC, t.tag"
    )
    params: list = []
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def notes_by_tag(conn: sqlite3.Connection, tag: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT d.id, d.path, d.title, d.doc_date FROM document d "
        "JOIN document_tag t ON t.document_id = d.id "
        "WHERE t.tag = ? ORDER BY COALESCE(d.doc_date, d.ingested_at)",
        (tag.strip().lstrip("#").lower(),),
    )]


# A photo or screenshot in notes/ is a note that happened to arrive as
# pixels: it is searched, cited as read by OCR, and never parsed for lab
# values. So it belongs to the notes surface everywhere a count is reported,
# and the PDF/records surface must never claim it. Counting it under "reports"
# would tell the model there is a lab report to trend where there is none.
NOTE_KINDS = ("note", "image")


def note_summary(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(doc_date) AS first, MAX(doc_date) AS last, "
        "SUM(CASE WHEN doc_date IS NULL THEN 1 ELSE 0 END) AS undated, "
        "SUM(CASE WHEN kind = 'image' THEN 1 ELSE 0 END) AS images "
        "FROM document WHERE kind IN ('note', 'image')"
    ).fetchone()
    tags = conn.execute(
        "SELECT COUNT(DISTINCT tag) AS n FROM document_tag"
    ).fetchone()["n"]
    chunks = conn.execute(
        "SELECT COUNT(*) AS n FROM chunk c JOIN document d ON d.id = c.document_id "
        "WHERE d.kind IN ('note', 'image')"
    ).fetchone()["n"]
    return {
        "notes": row["n"] or 0,
        "images": row["images"] or 0,
        "first": row["first"],
        "last": row["last"],
        "undated": row["undated"] or 0,
        "tags": tags,
        "chunks": chunks,
    }


def list_analytes(conn: sqlite3.Connection, search: str | None = None,
                  limit: int | None = None) -> list[dict]:
    sql = (
        "SELECT analyte_key, panel, COUNT(*) AS n, "
        "MIN(collected_date) AS first, MAX(collected_date) AS last, "
        "MAX(analyte) AS printed_as "
        "FROM lab_result "
    )
    params: list = []
    if search:
        sql += "WHERE lower(analyte) LIKE ? OR analyte_key LIKE ? "
        needle = f"%{search.lower()}%"
        params.extend([needle, needle])
    sql += "GROUP BY analyte_key, panel ORDER BY n DESC, analyte_key"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def lab_coverage(conn: sqlite3.Connection, key: str) -> tuple[int, str | None, str | None]:
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(collected_date) AS first, "
        "MAX(collected_date) AS last FROM lab_result WHERE analyte_key = ?",
        (key,),
    ).fetchone()
    if not row or not row["n"]:
        return 0, None, None
    return int(row["n"]), row["first"], row["last"]


def _collapse(points: list[LabPoint]) -> tuple[list[LabPoint], tuple[dict, ...]]:
    """One measurement reported by two sources becomes one point; two different
    measurements on a date stay two points and are reported.

    The same draw can arrive from a scanned report and from the health
    system's own FHIR record. `lab_result.dedup_key` includes the document,
    so it makes re-ingesting one file idempotent and does nothing across
    files: without this, a trend would plot the same value twice.

    Only a cross-source repeat collapses. Rows from a single source are never
    merged (two identical results from one report are two results), so a
    single-source trend comes back exactly as it went in. Where both sources
    report the same value some number of times, the larger per-source count
    is kept, so a genuine same-day repeat in one source survives.
    """
    order = {id(p): i for i, p in enumerate(points)}
    by_date: dict[str | None, list[LabPoint]] = {}
    for point in points:
        by_date.setdefault(point.collected_date, []).append(point)

    kept: list[tuple[int, LabPoint]] = []
    clashes: list[dict] = []
    for day, group in by_date.items():
        by_value: dict[tuple, list[LabPoint]] = {}
        for point in group:
            by_value.setdefault((point.value_num, point.value_text, point.unit),
                                []).append(point)
        for same in by_value.values():
            sources = {p.source for p in same}
            if len(sources) == 1:
                kept.extend((order[id(p)], p) for p in same)
                continue
            counts = {s: sum(1 for p in same if p.source == s) for s in sources}
            # Survivor preference: a row carrying a range, then a non-OCR
            # row, then the pdf row. The last is deliberate: a person can open
            # a PDF page and read the number, whereas a clinical-records
            # Observation-<id>.json path means nothing to them, so the more
            # checkable citation wins when nothing else separates the rows.
            ranked = sorted(same, key=lambda p: (p.ref_text is None, p.via_ocr,
                                                 p.source != "pdf", order[id(p)]))
            survivors = ranked[:max(counts.values())]
            # also_from only where the other source really holds a matching
            # row: pair survivors off against each other source's own count.
            claims: dict[int, list[str]] = {id(p): [] for p in survivors}
            for src, n in counts.items():
                for p in [p for p in survivors if p.source != src][:n]:
                    claims[id(p)].append(src)
            for p in survivors:
                others = tuple(sorted(claims[id(p)]))
                kept.append((order[id(p)],
                             replace(p, also_from=others) if others else p))
        # Report a conflict only when one measurement is held by exactly one
        # source and a different measurement by a different single source.
        # One source simply having fewer records is not a conflict.
        singles = {next(iter(srcs)) for srcs in
                   ({p.source for p in v} for v in by_value.values())
                   if len(srcs) == 1}
        if len(singles) > 1:
            clashes.append({
                "collected_date": day,
                "reported": [
                    {"value": v[0].value_num if v[0].value_num is not None
                     else v[0].value_text,
                     "unit": v[0].reported_unit,
                     "sources": sorted({p.source for p in v})}
                    for v in by_value.values()],
            })
    kept.sort(key=lambda t: t[0])
    return [p for _, p in kept], tuple(clashes)


def lab_trend(conn: sqlite3.Connection, key: str, *, start: str | None = None,
              end: str | None = None) -> LabTrend:
    """Time series for one analyte, with a citation on every point.

    This is the shape the agent's `get_lab_trend` tool returns at milestone 5,
    which is why each point carries its source file and page rather than just a
    number — an answer that cites "LDL 110" without saying which report it came
    from can't be checked.
    """
    total, available_from, available_to = lab_coverage(conn, key)

    sql = (
        "SELECT l.collected_date, l.value_num, l.value_text, l.unit, l.ref_low, "
        "l.ref_high, l.ref_text, l.flag, l.analyte, l.page_no, l.raw_line, "
        "l.via_ocr, l.extracted_by, d.path "
        "FROM lab_result l JOIN document d ON d.id = l.document_id "
        "WHERE l.analyte_key = ? "
    )
    params: list = [key]
    if start:
        sql += "AND l.collected_date >= ? "
        params.append(start)
    if end:
        sql += "AND l.collected_date <= ? "
        params.append(end)
    sql += "ORDER BY COALESCE(l.collected_date, '9999'), l.page_no, l.id"

    rows = conn.execute(sql, params).fetchall()
    points = [
        LabPoint(
            collected_date=r["collected_date"], value_num=r["value_num"],
            value_text=r["value_text"], unit=r["unit"], ref_low=r["ref_low"],
            ref_high=r["ref_high"], ref_text=r["ref_text"], flag=r["flag"],
            analyte=r["analyte"], path=r["path"], page_no=r["page_no"],
            raw_line=r["raw_line"], via_ocr=bool(r["via_ocr"]),
            source="fhir" if r["extracted_by"] == "fhir" else "pdf",
        )
        for r in rows
    ]
    points, disagreements = _collapse(points)
    from .. import labs as _labs
    return LabTrend(
        key=key,
        label=_labs.label_for(key),
        points=points,
        units_seen=tuple(sorted({p.unit for p in points if p.unit})),
        total_results=total,
        available_from=available_from,
        available_to=available_to,
        disagreements=disagreements,
    )


def clinical_facts(conn: sqlite3.Connection, *, kind: str | None = None,
                   since: str | None = None, limit: int | None = None) -> list[dict]:
    """Conditions, medications, allergies, immunizations and procedures that
    a health system sent back, newest first."""
    sql = ("SELECT f.kind, f.name, f.code, f.code_system, f.status, "
           "f.onset_date, f.detail_text, f.category, d.path "
           "FROM clinical_fact f JOIN document d ON d.id = f.document_id ")
    where, params = [], []
    if kind:
        where.append("f.kind = ?")
        params.append(kind)
    if since:
        where.append("(f.onset_date IS NULL OR f.onset_date >= ?)")
        params.append(since)
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY COALESCE(f.onset_date, '0000') DESC, f.name"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    out = []
    for row in conn.execute(sql, params):
        item = dict(row)
        item["citation"] = item.pop("path")
        out.append(item)
    return out


def clinical_fact_coverage(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n, MIN(onset_date) AS first, "
        "MAX(onset_date) AS last FROM clinical_fact GROUP BY kind").fetchall()
    return {r["kind"]: {"count": r["n"], "first": r["first"], "last": r["last"]}
            for r in rows}


def clinical_vitals(conn: sqlite3.Connection, *, metric: str | None = None,
                    start: str | None = None, end: str | None = None,
                    limit: int = 200) -> list[dict]:
    sql = ("SELECT v.metric, v.metric_key, v.value_num, v.value_text, v.unit, "
           "v.measured_at, v.local_date, v.category, d.path "
           "FROM clinical_vital v JOIN document d ON d.id = v.document_id ")
    where, params = [], []
    if metric:
        where.append("v.metric_key = ?")
        params.append(_labs_slug(metric))
    if start:
        where.append("v.local_date >= ?")
        params.append(start)
    if end:
        where.append("v.local_date <= ?")
        params.append(end)
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY COALESCE(v.measured_at, '') DESC LIMIT ?"
    params.append(limit)
    out = []
    for row in conn.execute(sql, params):
        item = dict(row)
        item["citation"] = item.pop("path")
        out.append(item)
    return out


def clinical_vital_metrics(conn: sqlite3.Connection) -> list[dict]:
    """One row per vital a clinician measured, with its reading count and span.

    `unit` is null when the same metric was recorded in more than one unit:
    picking one of them arbitrarily would attach a wrong unit to half the
    readings, and a null says "ask the readings" instead. `units` carries
    every unit seen, so a caller that wants to show the conflict can.
    """
    rows = conn.execute(
        "SELECT metric_key, MAX(metric) AS metric, "
        "COUNT(*) AS n, COUNT(DISTINCT unit) AS n_units, "
        "MIN(local_date) AS first, MAX(local_date) AS last "
        "FROM clinical_vital GROUP BY metric_key ORDER BY n DESC").fetchall()
    out = []
    for row in rows:
        item = dict(row)
        n_units = item.pop("n_units")
        units = [r[0] for r in conn.execute(
            "SELECT DISTINCT unit FROM clinical_vital WHERE metric_key = ? "
            "AND unit IS NOT NULL ORDER BY unit", (item["metric_key"],))]
        item["unit"] = units[0] if n_units <= 1 and units else None
        item["units"] = units
        out.append(item)
    return out


def clinical_content_counts(conn: sqlite3.Connection) -> dict:
    """The clinical figures that are not facts or vitals: lab values that came
    from the export, and note references (a DocumentReference or
    DiagnosticReport the export lists but whose text it does not carry).

    Deliberately no count of clinical `document` rows: each dedupe collapse
    leaves one with no child, and note references are contentless, so that
    total would read as records it does not hold."""
    labs = conn.execute(
        "SELECT COUNT(*) AS n, MIN(collected_date) AS first, "
        "MAX(collected_date) AS last FROM lab_result "
        "WHERE extracted_by = 'fhir'").fetchone()
    notes = conn.execute(
        "SELECT COUNT(*) AS n, MIN(doc_date) AS first, MAX(doc_date) AS last "
        "FROM document WHERE kind = 'fhir' AND "
        "fhir_resource_type IN ('DocumentReference', 'DiagnosticReport')"
    ).fetchone()
    return {"labs": labs["n"], "labs_first": labs["first"],
            "labs_last": labs["last"], "note_references": notes["n"],
            "note_references_first": notes["first"],
            "note_references_last": notes["last"]}


# The plural vocabulary the `kind` argument and every absence message use,
# paired with the singular `clinical_fact.kind` the rows store.
CLINICAL_FACT_PLURALS = (
    ("conditions", "condition"), ("medications", "medication"),
    ("allergies", "allergy"), ("immunizations", "immunization"),
    ("procedures", "procedure"),
)


def _span(first: str | None, last: str | None) -> str:
    if not first and not last:
        return ""
    if not first or not last or first == last:
        return first or last or ""
    return f"{first} to {last}"


def clinical_holdings(conn: sqlite3.Connection) -> dict:
    """Everything this index holds from an export's clinical-records folder.

    The one answer to "what does this index hold clinically". Clinical content
    lives in three tables (`clinical_fact`, `clinical_vital`, and, for FHIR
    labs and note references, `lab_result` and `document`), and a caller that
    reads one of them and reports emptiness tells the person their own records
    are absent. So no caller assembles that picture itself: the CLI, the two
    clinical tools and `data_inventory` all read this.

    `labels` is (label, count, date span) per kind that holds anything, in the
    plural vocabulary the `kind` argument takes, so every surface names what is
    held with the same words. `any` is the whole question C1 turned on: true
    whenever a clinical row of any kind exists.
    """
    facts = clinical_fact_coverage(conn)
    vital_metrics = clinical_vital_metrics(conn)
    extra = clinical_content_counts(conn)

    vitals_n = sum(m["n"] for m in vital_metrics)
    vitals_first = min((m["first"] for m in vital_metrics if m["first"]),
                       default=None)
    vitals_last = max((m["last"] for m in vital_metrics if m["last"]),
                      default=None)

    labels: list[tuple[str, int, str]] = []
    if extra["labs"]:
        labels.append(("lab values", extra["labs"],
                       _span(extra["labs_first"], extra["labs_last"])))
    if vitals_n:
        labels.append(("clinician vitals", vitals_n,
                       _span(vitals_first, vitals_last)))
    for plural, singular in CLINICAL_FACT_PLURALS:
        info = facts.get(singular)
        if info:
            labels.append((plural, info["count"],
                           _span(info["first"], info["last"])))
    if extra["note_references"]:
        labels.append(("note references", extra["note_references"],
                       _span(extra["note_references_first"],
                             extra["note_references_last"])))

    firsts = [d for d in [extra["labs_first"], vitals_first,
                          *(i["first"] for i in facts.values())] if d]
    lasts = [d for d in [extra["labs_last"], vitals_last,
                         *(i["last"] for i in facts.values())] if d]

    return {
        "facts": facts,
        "fact_total": sum(i["count"] for i in facts.values()),
        "labs": extra["labs"],
        "labs_first": extra["labs_first"],
        "labs_last": extra["labs_last"],
        "note_references": extra["note_references"],
        "vitals": vitals_n,
        "vital_metrics": vital_metrics,
        "vitals_first": vitals_first,
        "vitals_last": vitals_last,
        "first": min(firsts, default=None),
        "last": max(lasts, default=None),
        "labels": labels,
        "any": bool(labels),
    }


def clinical_holdings_phrase(conn: sqlite3.Connection, *,
                             leave_out: frozenset[str] | set[str] = frozenset(),
                             holdings: dict | None = None) -> str:
    """What the index holds, as a sentence fragment for an absence message.

    Lives beside `clinical_holdings` rather than in a surface, because the CLI
    and both clinical tools have to say the same thing: a count list, not
    prose. `leave_out` names the labels the caller has just reported missing.
    When no clinical figure is left, falls back to the HealthKit record and
    report counts, so the reader still learns what is there.
    """
    held = clinical_holdings(conn) if holdings is None else holdings
    parts = [f"{n:,} {label}" for label, n, _ in held["labels"]
             if label not in leave_out]
    if parts:
        return (parts[0] if len(parts) == 1
                else ", ".join(parts[:-1]) + " and " + parts[-1])
    summary = index_summary(conn)
    docs = document_summary(conn)
    return (f"{summary['records']:,} HealthKit records and "
            f"{docs['documents']:,} report PDF(s)")


def document_summary(conn: sqlite3.Connection) -> dict:
    """Counts for the records surface: PDFs and their pages only.

    `kind = 'pdf'` rather than `!= 'note'`, because an OCR'd photo is a note
    (see NOTE_KINDS) and must not be reported as a lab report or have its one
    OCR page counted among scanned pages.
    """
    docs = conn.execute(
        "SELECT COUNT(*) AS n, MIN(doc_date) AS first, MAX(doc_date) AS last "
        "FROM document WHERE kind = 'pdf'"
    ).fetchone()
    pages = conn.execute(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN p.extraction = 'ocr' THEN 1 ELSE 0 END) "
        "AS ocr FROM document_page p JOIN document d ON d.id = p.document_id "
        "WHERE d.kind = 'pdf'"
    ).fetchone()
    labs_row = conn.execute(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT analyte_key) AS analytes, "
        "MIN(collected_date) AS first, MAX(collected_date) AS last "
        "FROM lab_result"
    ).fetchone()
    chunks = conn.execute(
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN embedded_with IS NOT NULL THEN 1 ELSE 0 END) AS embedded "
        "FROM chunk"
    ).fetchone()
    return {
        "documents": docs["n"],
        "doc_first": docs["first"],
        "doc_last": docs["last"],
        "pages": pages["n"] or 0,
        "pages_ocr": pages["ocr"] or 0,
        "lab_results": labs_row["n"],
        "analytes": labs_row["analytes"],
        # The span of the lab values themselves, over every source. `doc_first`
        # and `doc_last` above are PDF document dates, which say nothing about
        # a lab value that came from the clinical-records folder.
        "lab_first": labs_row["first"],
        "lab_last": labs_row["last"],
        "chunks": chunks["n"] or 0,
        "chunks_embedded": chunks["embedded"] or 0,
    }


def _source_value(acc: dict, agg: str) -> float | None:
    if agg == "sum":
        return acc["sum"]
    if agg == "duration":
        return acc["dur"]
    if agg == "count":
        return float(acc["n"])
    if agg == "min":
        return acc["min"]
    if agg == "max":
        return acc["max"]
    if agg == "avg":
        return acc["sum"] / acc["n"] if acc["n"] else None
    raise ValueError(agg)


def _combine(by_source: dict, agg: str, combine: str) -> float | None:
    values = [v for v in (_source_value(a, agg) for a in by_source.values())
              if v is not None]
    if not values:
        return None
    if agg == "min":
        return min(values)
    if agg == "max":
        return max(values)
    if agg == "avg":
        # Sample-weighted across sources: a source contributing 400 samples
        # should not be averaged 1:1 with one contributing 3.
        total_n = sum(a["n"] for a in by_source.values())
        if not total_n:
            return None
        return sum(a["sum"] for a in by_source.values()) / total_n
    # Additive aggregates: sum or max across sources (see module docstring).
    if combine == "sum":
        return sum(values)
    return max(values)
