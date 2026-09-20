# Visit Prep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `health-agent visit-prep` writes a markdown sheet of questions to ask a clinician, each grounded in a value from the person's own index, with no model involved.

**Architecture:** A new module `health_agent/visit_prep.py` in two halves: `gather()` reads the index into a `Sheet` of `Signal`s (four kinds: lab out of range, lab returned to range, lab near a limit and moving toward it, HealthKit metric shifted between two windows); `render()` turns a `Sheet` into markdown from fixed templates. The literature lookup shared with the tool moves to `literature/store.py::hits`. The CLI command wires the three together.

**Tech Stack:** Python 3.11+, sqlite3 via `health_agent.store.queries`, existing `literature.store` search, pytest.

**Spec:** `docs/superpowers/specs/2026-09-20-visit-prep-design.md`

## Global Constraints

- No model call anywhere in `visit_prep.py` or `cmd_visit_prep`. No import of `agent.orchestrator` or `agent.backends`.
- No network: `visit_prep.py` imports nothing from `literature/fetch/`; `test_no_network.py`'s import scan must keep passing.
- Every question line starts with `**Ask`. No template contains a clinical threshold, a category word applied to the person ("high", "elevated", "low" as a judgment), or a verb of interpretation. "flagged H" quotes the lab's own flag.
- `guardrail.check(render(sheet), used_tools=True)` must return no flags for the fixture sheet, with and without literature.
- No em dashes in any prose (README, ROADMAP, module docstrings, templates). En dash in ranges ("0–99") is fine.
- Thresholds are module constants with a one-line comment each; the sheet says they are this tool's cutoffs, not clinical ones.
- Windows for `metric_shift` anchor at the metric's last date in the index (`SeriesResult.available_to`), not at today: an export is a snapshot, and "the last 30 days" of a six-month-old export would always be empty. The sheet prints the actual dates.

---

### Task 1: Lab signals

**Files:**
- Create: `health_agent/visit_prep.py`
- Test: `tests/test_visit_prep.py`

**Interfaces:**
- Consumes: `queries.lab_trend(conn, key) -> LabTrend` (`.points: list[LabPoint]`, `.label`), `queries.list_analytes(conn) -> list[dict]` (`analyte_key`), `LabPoint` fields `collected_date, value_num, unit, ref_low, ref_high, ref_text, flag, via_ocr`, properties `.citation`, `.out_of_range`.
- Produces:
  ```python
  @dataclass
  class Signal:
      kind: str            # "lab_out_of_range" | "lab_returned_to_range" | "lab_near_limit" | "metric_shift"
      subject: str         # analyte key or metric identifier
      label: str           # "LDL cholesterol", "Resting heart rate"
      evidence: dict       # kind-specific, see below
      caveats: list[str] = field(default_factory=list)
      literature: dict | None = None   # Task 4
  ```
  Evidence keys for the three lab kinds: `latest` and `previous` (each a dict `{"value": float, "unit": str|None, "date": str|None, "range": str|None, "flag": str|None, "citation": str}`; `previous` may be None), and for `lab_near_limit` also `limit_side: "high"|"low"` and `limit: float`.
  `lab_signals_for(trend: LabTrend) -> list[Signal]` (pure; the testable core) and `lab_signals(conn) -> list[Signal]` (iterates `list_analytes`).
  Constants: `NEAR_LIMIT_SPAN_FRACTION = 0.10`, `NEAR_LIMIT_SINGLE_FRACTION = 0.05`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_visit_prep.py
"""Visit prep: signals are found by rule, rendered by template, and never
interpreted. The guardrail is run over the rendered sheet as a test oracle."""
from __future__ import annotations

from pathlib import Path

import pytest

from health_agent import visit_prep
from health_agent.store.queries import LabPoint, LabTrend

FIXTURES = Path(__file__).parent / "fixtures"


def _point(date, value, low=None, high=None, flag=None, via_ocr=False, unit="mg/dL"):
    ref_text = None
    if low is not None and high is not None:
        ref_text = f"{low:g}-{high:g}"
    elif high is not None:
        ref_text = f"<{high:g}"
    elif low is not None:
        ref_text = f">{low:g}"
    return LabPoint(collected_date=date, value_num=value, value_text=str(value),
                    unit=unit, ref_low=low, ref_high=high, ref_text=ref_text,
                    flag=flag, analyte="X", path=f"/r/labs_{date}.pdf",
                    page_no=1, raw_line=None, via_ocr=via_ocr)


def _trend(*points, key="ldl", label="LDL cholesterol"):
    return LabTrend(key=key, label=label, points=list(points))


def _kinds(signals):
    return [s.kind for s in signals]


def test_latest_out_of_range_is_a_signal_with_the_previous_value():
    trend = _trend(_point("2025-09-12", 128, 0, 99, "H"),
                   _point("2026-03-10", 112, 0, 99, "H"))
    (signal,) = visit_prep.lab_signals_for(trend)
    assert signal.kind == "lab_out_of_range"
    assert signal.label == "LDL cholesterol"
    assert signal.evidence["latest"]["value"] == 112
    assert signal.evidence["latest"]["flag"] == "H"
    assert signal.evidence["latest"]["range"] == "0-99"
    assert signal.evidence["latest"]["citation"] == "labs_2026-03-10.pdf, p.1, 2026-03-10"
    assert signal.evidence["previous"]["value"] == 128


def test_out_of_range_by_value_without_a_lab_flag_still_counts():
    trend = _trend(_point("2026-03-10", 112, 0, 99, flag=None))
    assert _kinds(visit_prep.lab_signals_for(trend)) == ["lab_out_of_range"]


def test_one_in_range_result_is_no_signal():
    trend = _trend(_point("2026-03-10", 90, 0, 99))
    assert visit_prep.lab_signals_for(trend) == []


def test_returned_to_range_is_a_signal():
    trend = _trend(_point("2025-03-04", 5.9, 4.8, 5.6, "H", unit="%"),
                   _point("2025-09-12", 5.7, 4.8, 5.6, "H", unit="%"),
                   _point("2026-03-10", 5.4, 4.8, 5.6, unit="%"),
                   key="hba1c", label="HbA1c")
    (signal,) = visit_prep.lab_signals_for(trend)
    assert signal.kind == "lab_returned_to_range"
    assert signal.evidence["latest"]["value"] == 5.4
    # the previous value shown is the most recent out-of-range one
    assert signal.evidence["previous"]["value"] == 5.7


def test_near_limit_needs_proximity_and_movement_toward_it():
    # 0-99 range, span 99, 10% is 9.9: inside the band and rising -> signal
    rising = _trend(_point("2025-09-12", 80, 0, 99), _point("2026-03-10", 92, 0, 99))
    (signal,) = visit_prep.lab_signals_for(rising)
    assert signal.kind == "lab_near_limit"
    assert signal.evidence["limit_side"] == "high"
    assert signal.evidence["limit"] == 99
    # inside the band but falling away from the limit -> nothing
    falling = _trend(_point("2025-09-12", 95, 0, 99), _point("2026-03-10", 92, 0, 99))
    assert visit_prep.lab_signals_for(falling) == []
    # stable inside the band -> nothing
    stable = _trend(_point("2025-09-12", 92, 0, 99), _point("2026-03-10", 92, 0, 99))
    assert visit_prep.lab_signals_for(stable) == []
    # a jump that stays comfortably inside -> nothing
    inside = _trend(_point("2025-09-12", 40, 0, 99), _point("2026-03-10", 70, 0, 99))
    assert visit_prep.lab_signals_for(inside) == []


def test_near_limit_with_one_printed_limit_uses_five_percent_of_it():
    # only a lower limit of 40: 5% is 2, so 41 rising from... no, falling toward it
    trend = _trend(_point("2025-09-12", 46, 40, None), _point("2026-03-10", 41, 40, None),
                   key="hdl", label="HDL cholesterol")
    (signal,) = visit_prep.lab_signals_for(trend)
    assert signal.kind == "lab_near_limit"
    assert signal.evidence["limit_side"] == "low"


def test_near_limit_needs_two_numeric_points_and_a_numeric_range():
    one = _trend(_point("2026-03-10", 92, 0, 99))
    assert visit_prep.lab_signals_for(one) == []
    no_range = _trend(_point("2025-09-12", 80), _point("2026-03-10", 92))
    assert visit_prep.lab_signals_for(no_range) == []


def test_out_of_range_wins_over_near_limit_for_the_same_analyte():
    trend = _trend(_point("2025-09-12", 92, 0, 99), _point("2026-03-10", 112, 0, 99, "H"))
    assert _kinds(visit_prep.lab_signals_for(trend)) == ["lab_out_of_range"]


def test_ocr_values_carry_the_check_the_original_caveat():
    trend = _trend(_point("2025-03-04", 5.9, 4.8, 5.6, "H", via_ocr=True, unit="%"),
                   _point("2026-03-10", 5.4, 4.8, 5.6, unit="%"),
                   key="hba1c", label="HbA1c")
    (signal,) = visit_prep.lab_signals_for(trend)
    assert any("OCR" in c for c in signal.caveats)
    assert any("2025-03-04" in c for c in signal.caveats)


def test_lab_signals_over_the_fixtures(fixture_index):
    signals = visit_prep.lab_signals(fixture_index)
    by_subject = {s.subject: s.kind for s in signals}
    assert by_subject["ldl"] == "lab_out_of_range"
    assert by_subject["vitamin_d"] == "lab_out_of_range"
    assert by_subject["hba1c"] == "lab_returned_to_range"
    # one signal per analyte
    assert len(signals) == len(by_subject)
```

Add the `fixture_index` fixture at the top of the file (after `FIXTURES`):

```python
@pytest.fixture(scope="module")
def fixture_index(tmp_path_factory):
    """The committed fixtures, ingested once per module. OCR on so the
    scanned 2025-03-04 report contributes when Tesseract is present."""
    from health_agent.ingest import healthkit, records
    from health_agent.store import sqlite_schema

    tmp = tmp_path_factory.mktemp("visit_prep")
    conn = sqlite_schema.connect(tmp / "health.db", create=True)
    sqlite_schema.initialize(conn)

    def register(path, kind):
        return healthkit.register_source_file(
            conn, path, kind, healthkit.sha256_file(path))

    healthkit.ingest_file(conn, FIXTURES / "export.xml")
    sqlite_schema.rebuild_daily_metrics(conn)
    records.ingest_records(conn, FIXTURES / "records",
                           register=lambda p: register(p, "record"), use_ocr=True)
    conn.commit()
    yield conn
    conn.close()
```

Check `tests/test_agent.py::ctx` (lines 26-45) for the exact call shape of `records.ingest_records` and copy it if it differs from the above.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_visit_prep.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'health_agent.visit_prep'`

- [ ] **Step 3: Write the module**

```python
# health_agent/visit_prep.py
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
from datetime import date

from . import labs
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_visit_prep.py -q`
Expected: all PASS. If `test_lab_signals_over_the_fixtures` fails on `hba1c` because Tesseract is absent (the 2025-03-04 report is a scan), the test may be marked `pytest.importorskip`-style with `pytest.skip("Tesseract not installed")` when `records.ocr_available()` is False; do not weaken the assertion.

- [ ] **Step 5: Commit**

```bash
git add health_agent/visit_prep.py tests/test_visit_prep.py
git commit -m "visit prep: lab signals by rule, one per analyte"
```

---

### Task 2: Metric shift signals and the Sheet

**Files:**
- Modify: `health_agent/visit_prep.py`
- Test: `tests/test_visit_prep.py`

**Interfaces:**
- Consumes: `queries.metric_series(conn, metric, period="day", start=, end=) -> SeriesResult` (`.points[i].period_start`, `.value`, `.available_to`, `.total_records`), `queries.sleep_nights(conn, start, end) -> list[dict]` (`night`, `asleep_seconds`), `metrics.resolve(name) -> Metric` (`.identifier`, `.label`).
- Produces:
  ```python
  @dataclass
  class Sheet:
      prepared: str                      # ISO date
      window_days: int
      labs: dict                         # {"reports": int, "first": str|None, "last": str|None, "analytes": int}
      healthkit: dict | None             # {"recent_start","recent_end","prior_start","prior_end"} or None when no metric had data
      signals: list[Signal]
      gaps: list[str]                    # "HRV (SDNN): 4 days in the last 30", "Body mass: none in the export"
      literature: dict | None = None     # Task 4: {"packs": [...], "articles": int} or None
  ```
  `SHIFT_THRESHOLDS: dict[str, tuple[str, float]]` mapping metric alias to `("abs"|"pct", amount)`; `metric_signals(conn, *, window_days) -> tuple[list[Signal], list[str], dict | None]`; `metric_shift_for(recent: list[float], prior: list[float], threshold, window_days) -> bool`; `gather(conn, *, window_days=30, today: date | None = None) -> Sheet`.
  `metric_shift` evidence: `{"recent_mean": float, "prior_mean": float, "unit": str, "recent_days": int, "prior_days": int, "recent_start": str, "recent_end": str, "prior_start": str, "prior_end": str, "threshold_text": "5 bpm" | "3%"}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_visit_prep.py`:

```python
def _export_xml(records: list[tuple[str, str, str, str]]) -> str:
    """Minimal HealthKit export. records: (type, unit, date YYYY-MM-DD, value)."""
    body = "".join(
        f' <Record type="HKQuantityTypeIdentifier{t}" sourceName="Synthetic" '
        f'unit="{u}" creationDate="{d} 08:05:00 -0500" startDate="{d} 08:00:00 -0500" '
        f'endDate="{d} 08:00:00 -0500" value="{v}"/>\n'
        for t, u, d, v in records)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n'
            ' <ExportDate value="2026-06-30 09:00:00 -0400"/>\n'
            ' <Me HKCharacteristicTypeIdentifierDateOfBirth="1990-01-01"/>\n'
            f'{body}</HealthData>\n')


def _synthetic_index(tmp_path, records):
    from health_agent.ingest import healthkit
    from health_agent.store import sqlite_schema
    export = tmp_path / "export.xml"
    export.write_text(_export_xml(records))
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    healthkit.ingest_file(conn, export)
    sqlite_schema.rebuild_daily_metrics(conn)
    conn.commit()
    return conn


def _days(start: date, n: int) -> list[str]:
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def test_metric_shift_rule():
    assert visit_prep.metric_shift_for([64.0] * 20, [58.0] * 20, ("abs", 5)) is True
    assert visit_prep.metric_shift_for([62.0] * 20, [58.0] * 20, ("abs", 5)) is False
    assert visit_prep.metric_shift_for([77.0] * 20, [80.0] * 20, ("pct", 3)) is True
    assert visit_prep.metric_shift_for([79.0] * 20, [80.0] * 20, ("pct", 3)) is False


def test_resting_heart_rate_shift_is_a_signal(tmp_path):
    prior = [("RestingHeartRate", "count/min", d, "58") for d in _days(date(2026, 5, 1), 30)]
    recent = [("RestingHeartRate", "count/min", d, "64") for d in _days(date(2026, 5, 31), 30)]
    conn = _synthetic_index(tmp_path, prior + recent)
    signals, gaps, window = visit_prep.metric_signals(conn, window_days=30)
    (signal,) = [s for s in signals if s.subject == "HKQuantityTypeIdentifierRestingHeartRate"]
    assert signal.kind == "metric_shift"
    assert signal.evidence["recent_mean"] == 64
    assert signal.evidence["prior_mean"] == 58
    assert signal.evidence["threshold_text"] == "5 bpm"
    assert signal.evidence["recent_start"] == "2026-05-31"
    assert signal.evidence["recent_end"] == "2026-06-29"
    assert signal.evidence["prior_start"] == "2026-05-01"
    assert window["recent_end"] == "2026-06-29"
    # the four other metrics were looked for and are reported as gaps
    assert any(g.startswith("Body mass") for g in gaps)
    assert any(g.startswith("Steps") for g in gaps)


def test_metric_windows_anchor_at_the_last_day_present(tmp_path):
    """An export is a snapshot; 'the last 30 days' means its last 30."""
    prior = [("BodyMass", "lb", d, "180") for d in _days(date(2025, 1, 1), 30)]
    recent = [("BodyMass", "lb", d, "170") for d in _days(date(2025, 1, 31), 30)]
    conn = _synthetic_index(tmp_path, prior + recent)
    signals, _, window = visit_prep.metric_signals(conn, window_days=30)
    assert window["recent_end"] == "2025-03-01"
    assert [s.subject for s in signals] == ["HKQuantityTypeIdentifierBodyMass"]


def test_too_few_days_is_a_gap_not_a_signal(tmp_path):
    prior = [("RestingHeartRate", "count/min", d, "58") for d in _days(date(2026, 5, 1), 30)]
    recent = [("RestingHeartRate", "count/min", d, "70") for d in _days(date(2026, 5, 31), 10)]
    conn = _synthetic_index(tmp_path, prior + recent)
    signals, gaps, _ = visit_prep.metric_signals(conn, window_days=30)
    assert signals == []
    assert any(g.startswith("Resting heart rate: 10 days in the last 30") for g in gaps)


def test_gather_assembles_the_sheet(fixture_index):
    sheet = visit_prep.gather(fixture_index, window_days=30, today=date(2026, 9, 20))
    assert sheet.prepared == "2026-09-20"
    assert sheet.labs["reports"] == 3
    assert sheet.labs["analytes"] >= 20
    assert {s.subject for s in sheet.signals} >= {"ldl", "vitamin_d"}
    assert sheet.literature is None
```

Add `from datetime import date, timedelta` to the imports at the top of the test file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_visit_prep.py -q -k "metric or gather"`
Expected: FAIL with `AttributeError: module 'health_agent.visit_prep' has no attribute 'metric_shift_for'`

- [ ] **Step 3: Implement**

Add to `health_agent/visit_prep.py` (after the lab section):

```python
from datetime import timedelta

from . import metrics

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


def _threshold_text(alias: str, unit: str | None) -> str:
    kind, amount = SHIFT_THRESHOLDS[alias]
    if kind == "pct":
        return f"{amount:g}%"
    if alias == "sleep":
        return f"{amount / 60:g} min"
    return f"{amount:g} {unit or ''}".strip()


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
            gaps.append(f"{label}: {len(recent)} days in the last {window_days}")
            continue
        if len(prior) < need:
            gaps.append(f"{label}: {len(prior)} days in the {window_days} before")
            continue
        if not metric_shift_for(recent, prior, threshold):
            continue
        identifier = "HKCategoryTypeIdentifierSleepAnalysis" if alias == "sleep" \
            else metrics.resolve(alias).identifier
        signals.append(Signal(
            kind="metric_shift", subject=identifier, label=label,
            evidence={
                "recent_mean": round(sum(recent) / len(recent), 1),
                "prior_mean": round(sum(prior) / len(prior), 1),
                "unit": unit, "recent_days": len(recent), "prior_days": len(prior),
                "recent_start": recent_start.isoformat(), "recent_end": last.isoformat(),
                "prior_start": prior_start.isoformat(), "prior_end": prior_end.isoformat(),
                "threshold_text": _threshold_text(alias, unit),
            },
        ))
    return signals, gaps, window


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
        signals=lab + shifts,
        gaps=gaps,
    )
```

`metric_series` returns `SeriesPoint.value` in the metric's default aggregation (mean for discrete, sum for cumulative), which is what the windows want. Check `metrics.resolve("sleep")` is not needed: sleep goes through `sleep_nights`. If `metrics.resolve("hrv").label` differs from "HRV (SDNN)", use whatever it returns; the tests key on the identifier.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_visit_prep.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add health_agent/visit_prep.py tests/test_visit_prep.py
git commit -m "visit prep: metric shifts between two windows, and the Sheet"
```

---

### Task 3: Render, with the guardrail as oracle

**Files:**
- Modify: `health_agent/visit_prep.py`
- Test: `tests/test_visit_prep.py`

**Interfaces:**
- Consumes: `Sheet`, `Signal`; `health_agent.agent.guardrail.DISCLAIMER` (str) and `guardrail.check(text, used_tools=True) -> list[Flag]`.
- Produces: `render(sheet: Sheet) -> str`. Templates as module constants `QUESTION_TEMPLATES: dict[str, str]` keyed by kind. Literature line rendered when `signal.literature` is set (Task 4 fills it): `{"title": str, "year": int|None, "tier": str, "pmid": str}`.

- [ ] **Step 1: Write the failing tests**

Append:

```python
from health_agent.agent import guardrail


def _sheet_with(*signals, literature=None, gaps=()):
    return visit_prep.Sheet(
        prepared="2026-09-20", window_days=30,
        labs={"reports": 3, "first": "2025-03-04", "last": "2026-03-10", "analytes": 23},
        healthkit={"recent_start": "2026-08-22", "recent_end": "2026-09-20",
                   "prior_start": "2026-07-23", "prior_end": "2026-08-21"},
        signals=list(signals), gaps=list(gaps), literature=literature)


def _ldl():
    return visit_prep.lab_signals_for(_trend(
        _point("2025-09-12", 128, 0, 99, "H"), _point("2026-03-10", 112, 0, 99, "H")))[0]


def _a1c():
    return visit_prep.lab_signals_for(_trend(
        _point("2025-03-04", 5.9, 4.8, 5.6, "H", via_ocr=True, unit="%"),
        _point("2026-03-10", 5.4, 4.8, 5.6, unit="%"), key="hba1c", label="HbA1c"))[0]


def _near():
    return visit_prep.lab_signals_for(_trend(
        _point("2025-09-12", 80, 0, 99), _point("2026-03-10", 92, 0, 99)))[0]


def _rhr():
    return visit_prep.Signal(
        kind="metric_shift", subject="HKQuantityTypeIdentifierRestingHeartRate",
        label="Resting heart rate",
        evidence={"recent_mean": 64.0, "prior_mean": 58.0, "unit": "count/min",
                  "recent_days": 30, "prior_days": 30,
                  "recent_start": "2026-08-22", "recent_end": "2026-09-20",
                  "prior_start": "2026-07-23", "prior_end": "2026-08-21",
                  "threshold_text": "5 count/min"})


def test_rendered_sheet_has_the_sections_and_the_disclaimer():
    text = visit_prep.render(_sheet_with(_ldl(), _a1c(), _near(), _rhr(),
                                         gaps=["Body mass: none in the export"]))
    assert text.startswith("# Questions for your next visit")
    assert "Prepared 2026-09-20" in text
    assert "## Looked at" in text
    assert "- 3 lab reports, 2025-03-04 to 2026-03-10, 23 analytes" in text
    assert "- Apple Health, 2026-08-22 to 2026-09-20 against 2026-07-23 to 2026-08-21" in text
    assert "No literature corpus is installed" in text
    assert "## Questions" in text
    assert "## Not enough data to check" in text
    assert "- Body mass: none in the export" in text
    assert text.rstrip().endswith(f"_{guardrail.DISCLAIMER}_")


def test_every_question_opens_with_ask_and_cites_its_evidence():
    text = visit_prep.render(_sheet_with(_ldl(), _a1c(), _near(), _rhr()))
    questions = [l for l in text.splitlines() if l[:2].rstrip(".").isdigit() or l[:3].rstrip(".").isdigit()]
    assert len(questions) == 4
    for q in questions:
        assert "**Ask" in q, q
    assert "112 mg/dL on 2026-03-10, flagged H against the printed range of 0-99 mg/dL" in text
    assert "down from 128 mg/dL on 2025-09-12" in text
    assert "(labs_2026-03-10.pdf, p.1; labs_2025-09-12.pdf, p.1)" in text
    assert "5.4% on 2026-03-10, inside the printed range of 4.8-5.6%" in text
    assert "was 5.9% on 2025-03-04, flagged H" in text
    assert "read by OCR" in text
    assert "92 mg/dL on 2026-03-10, inside the printed range of 0-99 mg/dL and closer to its upper limit than the 80 mg/dL on 2025-09-12" in text
    assert "averaged 64 count/min over 2026-08-22 to 2026-09-20 against 58 count/min over 2026-07-23 to 2026-08-21" in text
    assert "this tool's cutoff for pointing it out (5 count/min)" in text
    assert "not something this tool can say" in text


def test_literature_line_when_present():
    ldl = _ldl()
    ldl.literature = {"title": "Lipid lowering in adults", "year": 2026,
                      "tier": "meta_analysis", "pmid": "42613609"}
    text = visit_prep.render(_sheet_with(ldl, literature={"packs": ["sample"], "articles": 2000}))
    assert "- Literature: sample (2,000 articles)" in text
    assert "Evidence you could bring up: *Lipid lowering in adults* (2026, meta_analysis, PMID 42613609)." in text
    assert "No literature corpus" not in text


def test_empty_sheet_says_nothing_stood_out():
    text = visit_prep.render(_sheet_with())
    assert "Nothing stood out" in text
    assert "## Questions" not in text


@pytest.mark.parametrize("with_literature", [False, True])
def test_the_guardrail_finds_nothing_to_flag_in_a_rendered_sheet(with_literature):
    """The argument for having no model in this feature, pinned: the
    templates never read as interpretation or as a recalled threshold."""
    ldl = _ldl()
    if with_literature:
        ldl.literature = {"title": "Lipid lowering in adults", "year": 2026,
                          "tier": "meta_analysis", "pmid": "42613609"}
    text = visit_prep.render(_sheet_with(ldl, _a1c(), _near(), _rhr(),
                                         gaps=["Body mass: none in the export"]))
    flags = guardrail.check(text, used_tools=True, returned_pmids=frozenset({"42613609"}))
    assert flags == [], [str(f) for f in flags]


def test_the_fixture_sheet_passes_the_guardrail(fixture_index):
    sheet = visit_prep.gather(fixture_index, today=date(2026, 9, 20))
    flags = guardrail.check(visit_prep.render(sheet), used_tools=True)
    assert flags == [], [str(f) for f in flags]
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_visit_prep.py -q -k "render or question or literature_line or empty or guardrail or fixture_sheet"`
Expected: FAIL with `AttributeError: ... has no attribute 'render'`

- [ ] **Step 3: Implement render**

Append to `health_agent/visit_prep.py`:

```python
from .agent.guardrail import DISCLAIMER

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
NOTHING = ("Nothing stood out in what was looked at. That is a statement about "
           "this tool's cutoffs, not about your health.")


def _val(e: dict) -> str:
    unit = f" {e['unit']}" if e.get("unit") and e["unit"] != "%" else (e.get("unit") or "")
    return f"{e['value']:g}{unit}"


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
        text = (f"It was {_val(latest)} on {latest['date']}{_flagged(latest)} "
                f"against the printed range of {_range(latest)}")
        if prev:
            direction = "down" if latest["value"] < prev["value"] else "up"
            text += f", {direction} from {_val(prev)} on {prev['date']}"
        return f"{text} {_cite(latest, prev)}."
    if s.kind == "lab_returned_to_range":
        latest, prev = e["latest"], e["previous"]
        return (f"It was {_val(prev)} on {prev['date']}{_flagged(prev)}, and "
                f"{_val(latest)} on {latest['date']}, inside the printed range of "
                f"{_range(latest)} {_cite(prev, latest)}.")
    if s.kind == "lab_near_limit":
        latest, prev = e["latest"], e["previous"]
        side = "upper" if e["limit_side"] == "high" else "lower"
        return (f"It was {_val(latest)} on {latest['date']}, inside the printed "
                f"range of {_range(latest)} and closer to its {side} limit than the "
                f"{_val(prev)} on {prev['date']} {_cite(latest, prev)}.")
    unit = e.get("unit") or ""
    return (f"It averaged {e['recent_mean']:g} {unit} over {e['recent_start']} to "
            f"{e['recent_end']} against {e['prior_mean']:g} {unit} over "
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
        lines.append(f"- Apple Health, {hk['recent_start']} to {hk['recent_end']} "
                     f"against {hk['prior_start']} to {hk['prior_end']}")
    else:
        lines.append("- No Apple Health export in the index")
    if sheet.literature:
        packs = ", ".join(sheet.literature["packs"]) or "corpus"
        lines.append(f"- Literature: {packs} ({sheet.literature['articles']:,} articles)")
    else:
        lines.append(f"- {NO_CORPUS}")
    lines.append("")

    if sheet.signals:
        lines.append("## Questions")
        for i, s in enumerate(sheet.signals, 1):
            lines.append(f"{i}. {QUESTION_TEMPLATES[s.kind].format(label=s.label)} "
                         f"{_evidence_sentence(s)}")
            for caveat in s.caveats:
                lines.append(f"   {caveat}")
            if s.literature:
                lit = s.literature
                year = f"{lit['year']}, " if lit.get("year") else ""
                lines.append(f"   Evidence you could bring up: *{lit['title']}* "
                             f"({year}{lit['tier']}, PMID {lit['pmid']}).")
        lines.append("")
    else:
        lines.extend([NOTHING, ""])

    if sheet.gaps:
        lines.append("## Not enough data to check")
        lines.extend(f"- {g}" for g in sheet.gaps)
        lines.append("")

    lines.append(f"_{DISCLAIMER}_")
    return "\n".join(lines) + "\n"
```

`health_agent/agent/__init__.py` may import the orchestrator; if importing `health_agent.agent.guardrail` pulls in `backends`/`ollama_client` that is acceptable (they make no network call on import; `test_no_network.py::test_no_telemetry_on_import` proves it), but do not import `orchestrator` directly.

The `test_every_question_opens_with_ask_and_cites_its_evidence` line-selection is deliberately crude; if it miscounts, replace with `re.match(r"^\d+\. ", l)`.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_visit_prep.py -q`
Expected: all PASS. If `test_the_guardrail_finds_nothing_to_flag_in_a_rendered_sheet` fails, read the flag's label and excerpt: fix the **template**, never the guard. The one known trap: "flagged H against the printed range" must keep "printed range" inside 60 characters of the match so `_REPORTING_CONTEXT`'s "printed" is seen; the templates above are written that way.

- [ ] **Step 5: Commit**

```bash
git add health_agent/visit_prep.py tests/test_visit_prep.py
git commit -m "visit prep: render the sheet; the guardrail is its test oracle"
```

---

### Task 4: Shared literature hits, and one finding per lab signal

**Files:**
- Modify: `health_agent/literature/store.py` (add `hits`)
- Modify: `health_agent/agent/tools.py:686-706` (`_literature_hits` delegates)
- Modify: `health_agent/visit_prep.py` (`attach_literature`)
- Test: `tests/test_visit_prep.py`, `tests/test_agent.py` (existing literature tool tests must still pass)

**Interfaces:**
- Consumes: `lit_store.search(conn, store, embedder, query, *, limit, min_tier, since_year) -> list[Finding]`, `lit_store.keyword_search(conn, query, *, limit, min_tier, since_year)`, `vector_store.VectorStore(path, table_name=lit_embed.TABLE_NAME)`, `Finding` fields `pmid, title, year, evidence_tier, evidence_rank`, `corpus.coverage(conn) -> {"packs": [...], "article_count": int, ...}`.
- Produces:
  ```python
  # literature/store.py
  def hits(conn, query: str, *, limit: int = 5, min_tier: str | None = None,
           since_year: int | None = None, vector_path: Path | None = None,
           embedder_factory=None) -> list[Finding]:
      """Semantic where possible, keyword otherwise. Never raises for retrieval;
      a ValueError (bad min_tier) still propagates."""
  # visit_prep.py
  def attach_literature(sheet: Sheet, literature_conn, *, vector_path=None,
                        embedder_factory=None) -> None:
      """Fills sheet.literature and, for each lab_* signal, signal.literature
      with the best-tiered of the top three hits for its label. In place."""
  ```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_visit_prep.py`:

```python
@pytest.fixture
def corpus(tmp_path):
    """The eight-article literature fixture, built without embeddings."""
    from health_agent.literature import corpus as lit_corpus, medline, schema
    conn = schema.connect(tmp_path / "literature.db", create=True)
    articles = medline.parse_articles((FIXTURES / "literature" / "corpus.xml").read_bytes())
    lit_corpus.build(conn, articles, pack_slug="sample", pack_version="1")
    conn.commit()
    return conn


def test_attach_literature_fills_lab_signals_only(corpus):
    ldl, rhr = _ldl(), _rhr()
    sheet = _sheet_with(ldl, rhr)
    visit_prep.attach_literature(sheet, corpus)
    assert sheet.literature["packs"] == ["sample"]
    assert sheet.literature["articles"] == 8
    assert ldl.literature is not None
    assert set(ldl.literature) == {"title", "year", "tier", "pmid"}
    assert ldl.literature["pmid"].isdigit()
    assert rhr.literature is None


def test_attach_literature_prefers_the_best_tier_of_the_top_three(corpus, monkeypatch):
    from health_agent.literature import store as lit_store
    from health_agent.literature.store import Finding
    found = [Finding(1, "1", "obs", "", "", evidence_tier="observational", evidence_rank=6),
             Finding(2, "2", "meta", "", "", evidence_tier="meta_analysis", evidence_rank=1),
             Finding(3, "3", "rct", "", "", evidence_tier="rct", evidence_rank=3)]
    monkeypatch.setattr(lit_store, "hits", lambda *a, **k: found)
    ldl = _ldl()
    visit_prep.attach_literature(_sheet_with(ldl), corpus)
    assert ldl.literature["pmid"] == "2"


def test_attach_literature_with_no_hits_leaves_the_signal_bare(corpus, monkeypatch):
    from health_agent.literature import store as lit_store
    monkeypatch.setattr(lit_store, "hits", lambda *a, **k: [])
    ldl = _ldl()
    visit_prep.attach_literature(_sheet_with(ldl), corpus)
    assert ldl.literature is None
```

Check `lit_corpus.build`'s keyword names against `health_agent/literature/corpus.py:49` and the way `tests/test_cli.py::test_check_reports_installed_packs` builds a pack; use whichever the signature actually takes. Check `Finding`'s positional order at `literature/store.py:31-43` (article_id, pmid, title, text, method) and adjust the `Finding(...)` calls if it differs. Add a `hits` test to `tests/test_literature.py` (or wherever `keyword_search` is tested) only if none of the existing tool tests exercise the fallback; the tool tests in `tests/test_agent.py` that pass `literature_conn` without a vector path already cover the keyword path.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_visit_prep.py -q -k attach`
Expected: FAIL with `AttributeError: ... has no attribute 'attach_literature'`

- [ ] **Step 3: Move `_literature_hits` and add `attach_literature`**

In `health_agent/literature/store.py`, add at the end:

```python
def hits(conn: sqlite3.Connection, query: str, *, limit: int = 5,
         min_tier: str | None = None, since_year: int | None = None,
         vector_path=None, embedder_factory=None) -> list[Finding]:
    """Semantic where possible, keyword otherwise. Never raises for
    retrieval; a bad `min_tier` (ValueError) still propagates.

    Shared by the agent's `search_medical_literature` tool and by visit
    prep, so both see the same corpus the same way."""
    if embedder_factory is not None and vector_path:
        from ..store import vector_store
        from . import embed as lit_embed
        try:
            embedder = embedder_factory()
            store = vector_store.VectorStore(vector_path, table_name=lit_embed.TABLE_NAME)
            return search(conn, store, embedder, query, limit=limit,
                          min_tier=min_tier, since_year=since_year)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - retrieval must not kill a turn
            log.warning("corpus semantic search unavailable (%s)", type(exc).__name__)
    return keyword_search(conn, query, limit=limit, min_tier=min_tier,
                          since_year=since_year)
```

If `store.py` has no `log`, add `from ..logging_setup import get_logger` and `log = get_logger("literature.store")` near its imports (see how `agent/tools.py` does it).

Replace the body of `_literature_hits` in `health_agent/agent/tools.py` with:

```python
def _literature_hits(ctx: ToolContext, query: str, limit: int,
                     min_tier: str | None, since_year: int | None) -> list:
    from ..literature import store as lit_store
    return lit_store.hits(ctx.literature_conn, query, limit=limit,
                          min_tier=min_tier, since_year=since_year,
                          vector_path=ctx.literature_vector_path,
                          embedder_factory=ctx.embedder_factory)
```

Append to `health_agent/visit_prep.py`:

```python
def attach_literature(sheet: Sheet, literature_conn, *, vector_path=None,
                      embedder_factory=None) -> None:
    """One finding per lab signal: the best-tiered of the top three hits for
    the analyte's label. None for metric shifts: a paper about resting heart
    rate says nothing about this person's watch. In place."""
    from .literature import corpus as lit_corpus
    from .literature import store as lit_store

    report = lit_corpus.coverage(literature_conn)
    sheet.literature = {"packs": list(report["packs"]),
                        "articles": report["article_count"]}
    for signal in sheet.signals:
        if not signal.kind.startswith("lab_"):
            continue
        found = lit_store.hits(literature_conn, signal.label, limit=3,
                               vector_path=vector_path,
                               embedder_factory=embedder_factory)
        if not found:
            continue
        best = min(found, key=lambda f: (f.evidence_rank is None,
                                         f.evidence_rank or 0))
        signal.literature = {"title": best.title, "year": best.year,
                             "tier": best.evidence_tier, "pmid": best.pmid}
```

- [ ] **Step 4: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: all PASS, including every existing `search_medical_literature` test in `tests/test_agent.py` and `tests/test_no_network.py` (the import scan: `hits` lives in `store.py`, which imports no transport).

- [ ] **Step 5: Commit**

```bash
git add health_agent/literature/store.py health_agent/agent/tools.py health_agent/visit_prep.py tests/test_visit_prep.py
git commit -m "visit prep: one literature finding per lab signal, through the tool's own search path"
```

---

### Task 5: The command, README, ROADMAP

**Files:**
- Modify: `health_agent/cli.py` (new `cmd_visit_prep` next to `cmd_stats`; parser entry after `p_check`)
- Modify: `README.md` (new section after "Medical literature corpus"; the quick-start block near line 63; "What's next" pointer near line 719)
- Modify: `ROADMAP.md` (#3, lines ~196-216)
- Test: `tests/test_cli.py`, `tests/test_no_network.py`

**Interfaces:**
- Consumes: `visit_prep.gather`, `visit_prep.attach_literature`, `visit_prep.render`; `_open_literature_corpus(cfg)`; `embeddings.get_embedder(args.embedder)` (see `cmd_ask` for the `--embedder` argument; `visit-prep` takes the same flag with the same default); `sqlite_schema.open_for_read`.
- Produces: `health-agent visit-prep [--window DAYS] [--json] [--out FILE] [--no-literature] [--embedder NAME]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_visit_prep_writes_a_sheet_from_the_fixtures(cli_records):
    code, out = cli_records("visit-prep")
    assert code == 0
    assert out.startswith("# Questions for your next visit")
    assert "**Ask whether your LDL cholesterol needs follow-up.**" in out
    assert "labs_2026-03-10.pdf, p.1" in out
    assert "No literature corpus is installed" in out
    assert "not medical advice" in out


def test_visit_prep_json_and_out(cli_records, tmp_path):
    code, out = cli_records("visit-prep", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["prepared"]
    assert any(s["kind"] == "lab_out_of_range" and s["subject"] == "ldl"
               for s in data["signals"])

    target = tmp_path / "sheet.md"
    code, out = cli_records("visit-prep", "--out", str(target))
    assert code == 0
    assert out == ""
    assert target.read_text().startswith("# Questions for your next visit")
    assert "wrote" in cli_records.err.lower()


def test_visit_prep_without_an_index(tmp_path, capsys):
    code = main(["--index", str(tmp_path / "none.db"), "visit-prep"])
    assert code == 1
    assert "ingest" in capsys.readouterr().err
```

Check how other commands report a missing index (grep `cmd_stats`/`cmd_search` for the `FileNotFoundError` or `IndexMissing` branch) and match its message; the assertion is only that stderr points at `ingest`.

Add to `tests/test_no_network.py`, next to the literature import scan:

```python
def test_visit_prep_imports_no_fetcher_and_no_model():
    text = (Path(offline_check.__file__).parent / "visit_prep.py").read_text()
    for forbidden in ("literature.fetch", "fetch import", "orchestrator", "backends",
                      "import urllib", "from urllib", "import http"):
        assert forbidden not in text, f"visit_prep.py mentions {forbidden!r}"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_cli.py -q -k visit_prep`
Expected: FAIL (argparse: `invalid choice: 'visit-prep'`), exit code 2 rather than 0.

- [ ] **Step 3: Implement the command**

In `health_agent/cli.py`, after `cmd_stats`:

```python
def cmd_visit_prep(args: argparse.Namespace, cfg: config.Config) -> int:
    """Questions worth asking a clinician, from the index alone (ROADMAP #3).

    No model: see `visit_prep.py`'s docstring for why. Offline: the corpus,
    when present, is read from disk like everything else here."""
    from . import visit_prep
    from dataclasses import asdict

    conn = sqlite_schema.open_for_read(cfg.index_path)
    literature_conn = None
    try:
        sheet = visit_prep.gather(conn, window_days=args.window)
        if not args.no_literature:
            literature_conn = _open_literature_corpus(cfg)
        if literature_conn is not None:
            def embedder_factory():
                return embeddings.get_embedder(args.embedder)
            visit_prep.attach_literature(
                sheet, literature_conn,
                vector_path=cfg.literature_vector_path,
                embedder_factory=embedder_factory)
        if args.json:
            print(json.dumps(asdict(sheet), indent=2))
            return 0
        text = visit_prep.render(sheet)
        if args.out:
            Path(args.out).write_text(text)
            print(f"wrote {args.out}", file=sys.stderr)
        else:
            print(text, end="")
        return 0
    finally:
        conn.close()
        if literature_conn is not None:
            literature_conn.close()
```

Match how the sibling commands handle a missing index: if `sqlite_schema.open_for_read` raises on a missing file and `main()` already turns that into the "run `health-agent ingest`" message with exit 1, nothing more is needed; otherwise wrap the open the way `cmd_stats` does.

Parser entry, after `p_check`:

```python
    p_visit = sub.add_parser(
        "visit-prep",
        help="questions worth asking at your next appointment, from your own data",
        description="Writes a short list of questions to raise with a clinician, "
                    "each tied to a value in your index: a lab result outside its "
                    "printed range, one that came back inside it, one drifting "
                    "toward a limit, or a watch metric that shifted between two "
                    "windows. No model is involved; every line is a template around "
                    "your own numbers. Reads the literature corpus when one is "
                    "installed.")
    p_visit.add_argument("--window", type=int, default=30,
                         help="days per HealthKit comparison window (default 30)")
    p_visit.add_argument("--json", action="store_true", help="the signals as JSON")
    p_visit.add_argument("--out", help="write the sheet here instead of stdout")
    p_visit.add_argument("--no-literature", action="store_true",
                         help="skip the corpus even when one is installed")
    p_visit.add_argument("--embedder", default="ollama",
                         help=argparse.SUPPRESS)
    p_visit.set_defaults(func=cmd_visit_prep)
```

Copy the `--embedder` definition from `p_ask` exactly (default and choices) rather than the sketch above.

- [ ] **Step 4: Run the suite**

Run: `python -m pytest tests/ -q`
Expected: all PASS.

- [ ] **Step 5: README and ROADMAP**

README, quick-start block (around line 63), add after the `check` line:

```
health-agent visit-prep                        # questions worth asking at your next appointment
```

README, new section after "Medical literature corpus" (before "Design notes worth knowing"):

```markdown
## Visit prep

`health-agent visit-prep` writes a short sheet of questions to bring to a
clinician, each one tied to a value in your own files:

- a lab result outside the range printed on the report, with the previous
  result so the direction is visible;
- one that was outside and has come back inside, with the question of
  whether to keep checking it;
- one inside the range but within a tenth of its span of a limit and moving
  toward it since the previous result;
- a watch metric (resting heart rate, weight, steps, HRV, sleep) whose
  average over the last 30 days moved past this tool's cutoff against the
  30 days before.

Every question opens with "Ask". The sentence after it is the value, the
printed range, the date, and the report it came from. When a literature
pack is installed, each lab question also names one finding by title, year,
tier and PMID, without quoting it.

There is no model in this command, on purpose. The output is a list of
your numbers with a fixed sentence around each, and a template does that
exactly where a model does it approximately and, on the evidence of
[eval run 6](tests/eval_results.md), sometimes with a recalled threshold
attached. It also means the sheet works on a machine that cannot run the
model, and that the guardrail can be run over the finished sheet as a test:
a template that ever reads as interpretation fails the suite.

What it does not read: your notes. Medications and conditions you wrote
down are free text, and a rule cannot tell "stopped metformin" from
"started metformin". `ask` handles those. The cutoffs for pointing out a
watch metric are this tool's, chosen to sit clear of day-to-day noise; the
sheet says so next to each one.

```bash
health-agent visit-prep                # to the terminal
health-agent visit-prep --out prep.md  # to a file you can print
```
```

README "What's next" (around line 719): change the sentence that says visit prep "builds on" the corpus to say it shipped and point at the section.

ROADMAP #3: replace the heading's body with a "**Shipped**" paragraph in the style of #1's slice 1 (see lines 18-24): what it does, a link to the spec, and what it does not do (notes; no appointment date; no tool exposing the sheet to the model).

- [ ] **Step 6: Run the prose through the humanizer**

Read `../brainiac/skills/humanizer/SKILL.md` and check the new README and ROADMAP text against its patterns: no em dashes, no "not X, it's Y" closers, no rule-of-three padding, no "-ing" tails. Fix inline.

- [ ] **Step 7: Full suite and commit**

Run: `python -m pytest tests/ -q`
Expected: all PASS.

```bash
git add health_agent/cli.py README.md ROADMAP.md tests/test_cli.py tests/test_no_network.py
git commit -m "visit-prep command; README and ROADMAP #3 shipped"
```

---

## Self-review notes

- Spec §2 `--window`, `--json`, `--out`, `--no-literature`: Task 5. Exit 0 on an empty sheet: `render` handles empty `signals` (Task 3) and the command never returns non-zero for content.
- Spec §3 four kinds: Tasks 1-2. OCR caveat: Task 1. Notes excluded: nothing reads them. One-result analytes not listed as gaps: `lab_signals_for` returns `[]` and adds no gap. Metrics absent listed by name: Task 2 `gaps`.
- Spec §3 windows "the last DAYS days": this plan anchors at the metric's last recorded day (Global Constraints); the spec's sheet prints the actual dates either way. Task 5's README text says "the last 30 days", which is true of the export.
- Spec §4 literature: Task 4; metric shifts skipped; `hits` shared with the tool.
- Spec §5 sheet: Task 3 templates match the example except that the metric sentence prints both windows' dates rather than "the last 30 days", which is more honest for an old export.
- Spec §7 tests: each named test exists in Tasks 1-5. `_literature_hits` moved with existing tests passing: Task 4 Step 4.
- Types: `Signal.evidence` keys are used identically in Tasks 1, 3 and 4; `Sheet.literature` shape `{"packs", "articles"}` in Tasks 2, 3, 4; `signal.literature` shape `{"title","year","tier","pmid"}` in Tasks 3 and 4.
