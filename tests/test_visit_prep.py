"""Visit prep: signals are found by rule, rendered by template, and never
interpreted. The guardrail is run over the rendered sheet as a test oracle."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from health_agent import visit_prep
from health_agent.store.queries import LabPoint, LabTrend

FIXTURES = Path(__file__).parent / "fixtures"


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


def test_display_converts_sleep_seconds_to_minutes_and_resting_hr_to_bpm():
    # evidence and threshold_text must agree, so both go through _display.
    assert visit_prep._display("sleep", 2700, "s") == (45.0, "min")
    assert visit_prep._threshold_text("sleep", "s") == "45 min"
    assert visit_prep._display("resting-hr", 64, "count/min") == (64.0, "bpm")
    assert visit_prep._threshold_text("resting-hr", "count/min") == "5 bpm"
    # metrics with no DISPLAY_UNIT entry keep the export's own unit
    assert visit_prep._display("weight", 170, "lb") == (170.0, "lb")


def test_resting_heart_rate_shift_is_a_signal(tmp_path):
    prior = [("RestingHeartRate", "count/min", d, "58") for d in _days(date(2026, 5, 1), 30)]
    recent = [("RestingHeartRate", "count/min", d, "64") for d in _days(date(2026, 5, 31), 30)]
    conn = _synthetic_index(tmp_path, prior + recent)
    signals, gaps, window = visit_prep.metric_signals(conn, window_days=30)
    (signal,) = [s for s in signals if s.subject == "HKQuantityTypeIdentifierRestingHeartRate"]
    assert signal.kind == "metric_shift"
    assert signal.evidence["recent_mean"] == 64
    assert signal.evidence["prior_mean"] == 58
    assert signal.evidence["unit"] == "bpm"
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
    # prior ends well before recent begins so the last-30-days window (anchored
    # at the last recorded day) doesn't reach back into the prior data.
    prior = [("RestingHeartRate", "count/min", d, "58") for d in _days(date(2026, 4, 1), 30)]
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
        evidence={"recent_mean": 64.0, "prior_mean": 58.0, "unit": "bpm",
                  "recent_days": 30, "prior_days": 30,
                  "recent_start": "2026-08-22", "recent_end": "2026-09-20",
                  "prior_start": "2026-07-23", "prior_end": "2026-08-21",
                  "threshold_text": "5 bpm"})


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
    assert "averaged 64 bpm over 2026-08-22 to 2026-09-20 against 58 bpm over 2026-07-23 to 2026-08-21" in text
    assert "this tool's cutoff for pointing it out (5 bpm)" in text
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
