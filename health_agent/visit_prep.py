"""Visit prep: questions worth asking, from the data alone (ROADMAP #3).

Two halves with one interface between them. `gather` reads the index and
returns a `Sheet` of `Signal`s; `render` turns a `Sheet` into markdown from
fixed templates. Nothing in `gather` composes prose; nothing in `render`
reads the database.

No model is involved, on purpose. The output is a list of the person's own
values with a fixed question around each, and a template does that exactly
where a model does it approximately and, on the evidence of eval run 6,
sometimes with a recalled threshold attached. It also means the sheet
works on a machine that cannot run the model, and that the guardrail can
be run over the rendered output as a test oracle: a template that trips it
is a failing test, not a runtime surprise.

Every threshold below is this tool's cutoff for pointing something out,
chosen to sit well clear of day-to-day noise. None is clinical, and the
sheet says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .store import queries
from .store.queries import LabPoint, LabTrend

# A result inside the printed range counts as "near" a limit when it sits
# within this fraction of the range's span from that limit.
NEAR_LIMIT_SPAN_FRACTION = 0.10
# When the lab printed only one limit there is no span; use this fraction
# of the limit's own value instead.
NEAR_LIMIT_SINGLE_FRACTION = 0.05

OCR_CAVEAT = ("The {date} value was read by OCR from a scanned page; "
              "check it against the original report.")


@dataclass
class Signal:
    kind: str
    subject: str
    label: str
    evidence: dict
    caveats: list[str] = field(default_factory=list)
    literature: dict | None = None


def _point_evidence(p: LabPoint) -> dict:
    return {
        "value": p.value_num,
        "unit": p.unit,
        "date": p.collected_date,
        "range": p.ref_text,
        "flag": p.flag,
        "citation": p.citation,
    }


def _ocr_caveats(*points: LabPoint) -> list[str]:
    return [OCR_CAVEAT.format(date=p.collected_date or "undated")
            for p in points if p.via_ocr]


def _near_limit(latest: LabPoint, previous: LabPoint) -> tuple[str, float] | None:
    """(side, limit) when `latest` is inside the range, close to one limit,
    and moved toward it since `previous`; else None."""
    if latest.value_num is None or previous.value_num is None:
        return None
    low, high = latest.ref_low, latest.ref_high
    if low is None and high is None:
        return None
    if low is not None and high is not None:
        band = (high - low) * NEAR_LIMIT_SPAN_FRACTION
    else:
        band = abs(high if high is not None else low) * NEAR_LIMIT_SINGLE_FRACTION
    moved = latest.value_num - previous.value_num
    if high is not None and high - latest.value_num <= band and moved > 0:
        return "high", high
    if low is not None and latest.value_num - low <= band and moved < 0:
        return "low", low
    return None


def lab_signals_for(trend: LabTrend) -> list[Signal]:
    """At most one signal per analyte, from its dated numeric results."""
    points = [p for p in trend.points
              if p.value_num is not None and p.collected_date]
    if not points:
        return []
    points.sort(key=lambda p: p.collected_date)
    latest = points[-1]
    previous = points[-2] if len(points) > 1 else None
    latest_out = bool(latest.flag) or latest.out_of_range is True

    if latest_out:
        return [Signal(
            kind="lab_out_of_range", subject=trend.key, label=trend.label,
            evidence={"latest": _point_evidence(latest),
                      "previous": _point_evidence(previous) if previous else None},
            caveats=_ocr_caveats(*([latest] + ([previous] if previous else []))),
        )]

    earlier_out = [p for p in points[:-1] if p.flag or p.out_of_range is True]
    if earlier_out:
        last_out = earlier_out[-1]
        return [Signal(
            kind="lab_returned_to_range", subject=trend.key, label=trend.label,
            evidence={"latest": _point_evidence(latest),
                      "previous": _point_evidence(last_out)},
            caveats=_ocr_caveats(latest, last_out),
        )]

    if previous is not None:
        near = _near_limit(latest, previous)
        if near:
            side, limit = near
            return [Signal(
                kind="lab_near_limit", subject=trend.key, label=trend.label,
                evidence={"latest": _point_evidence(latest),
                          "previous": _point_evidence(previous),
                          "limit_side": side, "limit": limit},
                caveats=_ocr_caveats(latest, previous),
            )]
    return []


def lab_signals(conn) -> list[Signal]:
    signals: list[Signal] = []
    seen: set[str] = set()
    for row in queries.list_analytes(conn):
        key = row["analyte_key"]
        if key in seen:
            continue  # list_analytes groups by panel too
        seen.add(key)
        signals.extend(lab_signals_for(queries.lab_trend(conn, key)))
    return signals
