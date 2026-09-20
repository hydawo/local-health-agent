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
from datetime import date, timedelta

from . import metrics
from .agent.guardrail import DISCLAIMER
from .literature import tiers
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


# Metric alias -> (kind, amount). "abs" is in the metric's unit; "pct" is a
# percentage of the prior window's mean. Each is well clear of day-to-day
# noise for that metric; none is a clinical figure.
SHIFT_THRESHOLDS: dict[str, tuple[str, float]] = {
    "resting-hr": ("abs", 5),     # bpm
    "weight": ("pct", 3),         # a few pounds on most adults
    "steps": ("pct", 25),         # a quarter of the daily count
    "hrv": ("pct", 20),           # HRV is noisy; smaller shifts are routine
    "sleep": ("abs", 45 * 60),    # seconds; three quarters of an hour a night
}
# Both windows need at least this fraction of their days present.
MIN_WINDOW_FRACTION = 0.5
# (display unit, divisor from the export's raw value to that unit), for metrics
# whose export unit isn't the one people actually say. An alias absent here
# keeps the export's own unit and value unconverted. `_threshold_text` and the
# `metric_shift` evidence both go through `_display`, so they always agree.
DISPLAY_UNIT: dict[str, tuple[str, float]] = {
    "resting-hr": ("bpm", 1),   # "count/min" in the export, "bpm" to people
    "sleep": ("min", 60),       # sleep_nights totals seconds
}


def _display(alias: str, value: float, unit: str | None) -> tuple[float, str]:
    """(value, unit) converted to display units, per DISPLAY_UNIT."""
    display_unit, divisor = DISPLAY_UNIT.get(alias, (unit or "", 1))
    return value / divisor, display_unit


def _threshold_text(alias: str, unit: str | None) -> str:
    kind, amount = SHIFT_THRESHOLDS[alias]
    if kind == "pct":
        return f"{amount:g}%"
    display_amount, display_unit = _display(alias, amount, unit)
    return f"{display_amount:g} {display_unit}".strip()


def metric_shift_for(recent: list[float], prior: list[float],
                     threshold: tuple[str, float]) -> bool:
    if not recent or not prior:
        return False
    recent_mean = sum(recent) / len(recent)
    prior_mean = sum(prior) / len(prior)
    kind, amount = threshold
    if kind == "abs":
        return abs(recent_mean - prior_mean) >= amount
    if prior_mean == 0:
        return False
    return abs(recent_mean - prior_mean) / abs(prior_mean) * 100 >= amount


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def _daily_values(conn, alias: str) -> tuple[str, str | None, dict[str, float]]:
    """(label, unit, {date: value}) for one metric, sleep via nights."""
    if alias == "sleep":
        nights = queries.sleep_nights(conn)
        return "Sleep", "s", {n["night"]: n["asleep_seconds"] for n in nights
                              if n["asleep_seconds"]}
    metric = metrics.resolve(alias)
    series = queries.metric_series(conn, metric, period="day")
    values = {p.period_start: p.value for p in series.points if p.value is not None}
    return metric.label, series.unit, values


def metric_signals(conn, *, window_days: int) -> tuple[list[Signal], list[str], dict | None]:
    signals: list[Signal] = []
    gaps: list[str] = []
    window: dict | None = None
    need = max(1, int(window_days * MIN_WINDOW_FRACTION))
    for alias, threshold in SHIFT_THRESHOLDS.items():
        label, unit, values = _daily_values(conn, alias)
        if not values:
            gaps.append(f"{label}: none in the export")
            continue
        last = date.fromisoformat(max(values))
        recent_start = last - timedelta(days=window_days - 1)
        prior_start = recent_start - timedelta(days=window_days)
        prior_end = recent_start - timedelta(days=1)
        recent = [v for d, v in values.items() if recent_start.isoformat() <= d <= last.isoformat()]
        prior = [v for d, v in values.items() if prior_start.isoformat() <= d <= prior_end.isoformat()]
        if window is None or last.isoformat() > window["recent_end"]:
            window = {"recent_start": recent_start.isoformat(), "recent_end": last.isoformat(),
                      "prior_start": prior_start.isoformat(), "prior_end": prior_end.isoformat()}
        if len(recent) < need:
            gaps.append(f"{label}: {_days(len(recent))} in the {window_days} days "
                       f"to {last.isoformat()}")
            continue
        if len(prior) < need:
            gaps.append(f"{label}: {_days(len(prior))} in the {window_days} days "
                       f"before {recent_start.isoformat()}")
            continue
        if not metric_shift_for(recent, prior, threshold):
            continue
        identifier = "HKCategoryTypeIdentifierSleepAnalysis" if alias == "sleep" \
            else metrics.resolve(alias).identifier
        recent_mean, display_unit = _display(alias, sum(recent) / len(recent), unit)
        prior_mean, _ = _display(alias, sum(prior) / len(prior), unit)
        signals.append(Signal(
            kind="metric_shift", subject=identifier, label=label,
            evidence={
                "recent_mean": round(recent_mean, 1),
                "prior_mean": round(prior_mean, 1),
                "unit": display_unit, "recent_days": len(recent), "prior_days": len(prior),
                "recent_start": recent_start.isoformat(), "recent_end": last.isoformat(),
                "prior_start": prior_start.isoformat(), "prior_end": prior_end.isoformat(),
                "threshold_text": _threshold_text(alias, unit),
            },
        ))
    return signals, gaps, window


# Reading order for the rendered sheet: the values furthest from routine
# come first, near-limit and returned-to-range follow, metric shifts last.
# Stable within a kind, so `sorted` alone gives a deterministic order.
_KIND_ORDER = {"lab_out_of_range": 0, "lab_near_limit": 1,
              "lab_returned_to_range": 2, "metric_shift": 3}


@dataclass
class Sheet:
    prepared: str
    window_days: int
    labs: dict
    healthkit: dict | None
    signals: list[Signal]
    gaps: list[str]
    literature: dict | None = None


def gather(conn, *, window_days: int = 30, today: date | None = None) -> Sheet:
    docs = queries.document_summary(conn)
    lab = lab_signals(conn)
    shifts, gaps, window = metric_signals(conn, window_days=window_days)
    return Sheet(
        prepared=(today or date.today()).isoformat(),
        window_days=window_days,
        labs={"reports": docs["documents"], "first": docs["doc_first"],
              "last": docs["doc_last"], "analytes": docs["analytes"]},
        healthkit=window,
        signals=sorted(lab + shifts, key=lambda s: _KIND_ORDER[s.kind]),
        gaps=gaps,
    )

# Every question opens with "Ask". The sentence after it holds only the
# person's values, the printed range, dates and citations: the register
# the guardrail's personal and reporting exemptions are written for, and
# `tests/test_visit_prep.py` runs the guard over a rendered sheet to hold
# these templates to it.
QUESTION_TEMPLATES = {
    "lab_out_of_range": "**Ask whether your {label} needs follow-up.**",
    "lab_returned_to_range": "**Ask whether your {label} should keep being checked.**",
    "lab_near_limit": "**Ask whether your {label} is worth watching.**",
    "metric_shift": "**Ask about your {label}.**",
}
NO_CORPUS = ("No literature corpus is installed; `health-agent literature "
             "packs` lists what is available")
INTRO = ("Prepared {date} from your own files. Nothing here is a conclusion; "
         "each item is a value that stood out and a question it might be "
         "worth asking. Bring the reports named.")
NOTHING = ("Nothing stood out in what was looked at. That describes this "
           "tool's cutoffs; it says nothing about your health.")

# Evidence tier key -> how the sheet says it. A key outside the map is
# printed with its underscores replaced by spaces.
TIER_LABELS = {
    "meta_analysis": "meta-analysis",
    "systematic_review": "systematic review",
    "rct": "randomized trial",
    "clinical_trial": "clinical trial",
    "guideline": "guideline",
    "narrative_review": "review",
    "scoping_review": "scoping review",
    "observational": "observational study",
    "case_report": "case report",
}


def _tier_label(tier: str) -> str:
    return TIER_LABELS.get(tier, tier.replace("_", " "))


def _num(v: float) -> str:
    """`v` as people write it: no exponent, no trailing `.0`. `:g` switches
    to exponent notation above six significant figures, which turns a
    platelet count into `1.25e+06`."""
    text = f"{v:.10f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _sentence_label(label: str) -> str:
    """`label`, lowercased at the start only when it reads as a normal word
    ("Total cholesterol" -> "total cholesterol") and left alone when the
    second character says it's an acronym or an initialism ("LDL
    cholesterol", "HRV (SDNN)"). Used mid-sentence, in the question line
    only; gaps and headings keep the label as given."""
    if len(label) >= 2 and label[1].islower():
        return label[0].lower() + label[1:]
    return label


def _val(e: dict) -> str:
    unit = f" {e['unit']}" if e.get("unit") and e["unit"] != "%" else (e.get("unit") or "")
    return f"{_num(e['value'])}{unit}"


def _range(e: dict) -> str:
    unit = f" {e['unit']}" if e.get("unit") and e["unit"] != "%" else (e.get("unit") or "")
    return f"{e['range']}{unit}" if e.get("range") else "no printed range"


def _flagged(e: dict) -> str:
    return f", flagged {e['flag']}" if e.get("flag") else ""


def _cite(*evidence: dict | None) -> str:
    parts = []
    for e in evidence:
        if e:
            file_and_page = ", ".join(e["citation"].split(", ")[:2])
            parts.append(file_and_page)
    return f"({'; '.join(parts)})"


def _evidence_sentence(s: Signal) -> str:
    e = s.evidence
    if s.kind == "lab_out_of_range":
        latest, prev = e["latest"], e["previous"]
        if latest.get("range"):
            text = (f"It was {_val(latest)} on {latest['date']}{_flagged(latest)} "
                    f"against the printed range of {_range(latest)}")
        else:
            text = (f"It was {_val(latest)} on {latest['date']}{_flagged(latest)}, "
                    f"with no printed range")
        if prev:
            direction = "down" if latest["value"] < prev["value"] else "up"
            text += f", {direction} from {_val(prev)} on {prev['date']}"
        return f"{text} {_cite(latest, prev)}."
    if s.kind == "lab_returned_to_range":
        latest, prev = e["latest"], e["previous"]
        range_clause = (f"inside the printed range of {_range(latest)}"
                        if latest.get("range") else "with no printed range")
        return (f"It was {_val(prev)} on {prev['date']}{_flagged(prev)}, and "
                f"{_val(latest)} on {latest['date']}, {range_clause} "
                f"{_cite(prev, latest)}.")
    if s.kind == "lab_near_limit":
        latest, prev = e["latest"], e["previous"]
        side = "upper" if e["limit_side"] == "high" else "lower"
        range_clause = (f"inside the printed range of {_range(latest)}"
                        if latest.get("range") else "with no printed range")
        return (f"It was {_val(latest)} on {latest['date']}, {range_clause} "
                f"and closer to its {side} limit than the "
                f"{_val(prev)} on {prev['date']} {_cite(latest, prev)}.")
    unit = e.get("unit") or ""
    return (f"It averaged {_num(e['recent_mean'])} {unit} over {e['recent_start']} to "
            f"{e['recent_end']} against {_num(e['prior_mean'])} {unit} over "
            f"{e['prior_start']} to {e['prior_end']}, a shift larger than this "
            f"tool's cutoff for pointing it out ({e['threshold_text']}). Whether "
            f"that matters is not something this tool can say.").replace("  ", " ")


def render(sheet: Sheet) -> str:
    lines = ["# Questions for your next visit", "",
             INTRO.format(date=sheet.prepared), "", "## Looked at"]
    labs = sheet.labs
    if labs["reports"]:
        lines.append(f"- {labs['reports']} lab reports, {labs['first']} to "
                     f"{labs['last']}, {labs['analytes']} analytes")
    else:
        lines.append("- No lab reports in the index")
    hk = sheet.healthkit
    if hk:
        # Each metric anchors its own pair of windows at its own last day,
        # so a single date range would be untrue; say the shape and the
        # latest anchor instead.
        lines.append(f"- Apple Health, {sheet.window_days}-day windows ending at "
                     f"each metric's last day, latest {hk['recent_end']}")
    else:
        lines.append("- No data for the watched metrics in the export")
    if sheet.literature:
        packs = ", ".join(sheet.literature["packs"]) or "corpus"
        lines.append(f"- Literature: {packs} ({sheet.literature['articles']:,} articles)")
    else:
        lines.append(f"- {NO_CORPUS}")
    lines.append("")

    if sheet.signals:
        lines.append("## Questions")
        for i, s in enumerate(sheet.signals, 1):
            lines.append(f"{i}. {QUESTION_TEMPLATES[s.kind].format(label=_sentence_label(s.label))} "
                         f"{_evidence_sentence(s)}")
            for caveat in s.caveats:
                lines.append(f"   {caveat}")
            if s.literature:
                lit = s.literature
                year = f"{lit['year']}, " if lit.get("year") else ""
                lines.append(f"   Evidence you could bring up: *{lit['title']}* "
                             f"({year}{_tier_label(lit['tier'])}, PMID {lit['pmid']}).")
        lines.append("")
    else:
        lines.extend([NOTHING, ""])

    if sheet.gaps:
        lines.append("## Not enough data to check")
        lines.extend(f"- {g}" for g in sheet.gaps)
        lines.append("")

    lines.append(f"_{DISCLAIMER}_")
    return "\n".join(lines) + "\n"


# Tiers that never make it onto the sheet. A protocol is a plan, an
# `unknown` is an article PubMed recorded no design for; neither is a
# finding a person should carry into a visit as evidence.
_UNCITABLE_TIERS = frozenset({tiers.PROTOCOL, tiers.UNKNOWN})
# `hits` is asked for this many so the filter above has something left to
# choose from; the best-tiered of what survives is the one printed.
_LITERATURE_HITS = 5


def _citable(finding) -> bool:
    return not finding.retracted and finding.evidence_tier not in _UNCITABLE_TIERS


class _OnceEmbedder:
    """`hits` decides semantic-versus-keyword on every call, so a corpus
    whose embedder is down would log the same warning once per question.
    This proxy sits between `hits` and the real embedder: the first failed
    `embed` trips a flag, and `factory` then hands `hits` nothing at all,
    so the rest of the sheet goes straight to keyword search. When the
    embedder works, `hits` sees exactly what it would have seen."""

    def __init__(self, embedder_factory) -> None:
        self._make = embedder_factory
        self._embedder = None
        self.failed = False

    @property
    def factory(self):
        return None if self.failed or self._make is None else self

    def __call__(self):
        if self._embedder is None:
            try:
                self._embedder = self._make()
            except Exception:
                self.failed = True
                raise
        return self

    @property
    def name(self) -> str:
        return self._embedder.name

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            return self._embedder.embed(texts)
        except Exception:
            self.failed = True
            raise


def attach_literature(sheet: Sheet, literature_conn, *, vector_path=None,
                      embedder_factory=None) -> None:
    """One finding per lab signal: the best-tiered citable hit among the
    top few for the analyte's label. Retracted articles, protocols and
    untiered articles are never chosen; when nothing citable comes back the
    signal is left bare. None for metric shifts: a paper about resting heart
    rate says nothing about this person's watch. In place."""
    from .literature import corpus as lit_corpus
    from .literature import store as lit_store

    report = lit_corpus.coverage(literature_conn)
    sheet.literature = {"packs": [p.split("@")[0] for p in report["packs"]],
                        "articles": report["article_count"]}
    once = _OnceEmbedder(embedder_factory)
    for signal in sheet.signals:
        if not signal.kind.startswith("lab_"):
            continue
        found = lit_store.hits(literature_conn, signal.label, limit=_LITERATURE_HITS,
                               vector_path=vector_path,
                               embedder_factory=once.factory)
        citable = [f for f in found if _citable(f)]
        if not citable:
            continue
        best = min(citable, key=lambda f: (f.evidence_rank is None,
                                           f.evidence_rank or 0))
        signal.literature = {"title": best.title, "year": best.year,
                             "tier": best.evidence_tier, "pmid": best.pmid}
