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
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..metrics import Kind, Metric

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
    if source:
        sql += "AND source_name = ? "
        params.append(source)
    sql += "ORDER BY local_date"

    rows = conn.execute(sql, params).fetchall()

    units = {r["unit"] for r in rows if r["unit"]}
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

    @property
    def is_empty(self) -> bool:
        return not self.points


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
        "l.via_ocr, d.path "
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
    sql += "ORDER BY COALESCE(l.collected_date, '9999'), l.page_no"

    rows = conn.execute(sql, params).fetchall()
    points = [
        LabPoint(
            collected_date=r["collected_date"], value_num=r["value_num"],
            value_text=r["value_text"], unit=r["unit"], ref_low=r["ref_low"],
            ref_high=r["ref_high"], ref_text=r["ref_text"], flag=r["flag"],
            analyte=r["analyte"], path=r["path"], page_no=r["page_no"],
            raw_line=r["raw_line"], via_ocr=bool(r["via_ocr"]),
        )
        for r in rows
    ]
    from .. import labs as _labs
    return LabTrend(
        key=key,
        label=_labs.label_for(key),
        points=points,
        units_seen=tuple(sorted({p.unit for p in points if p.unit})),
        total_results=total,
        available_from=available_from,
        available_to=available_to,
    )


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
        "SELECT COUNT(*) AS n, COUNT(DISTINCT analyte_key) AS analytes FROM lab_result"
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
