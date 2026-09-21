"""Literature context on out-of-range labs: found by a rule after the model
is done, rendered by template, never shown to the model. The guardrail is
run over the rendered block as a test oracle."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from health_agent.agent import context, guardrail
from health_agent.literature import corpus, medline, schema, store

FIX = Path(__file__).parent / "fixtures" / "literature"


@dataclass
class Step:
    name: str
    result: dict


def _batch(*rows):
    return Step("get_lab_trend", {"out_of_range_on_latest_report": list(rows),
                                  "trends": {}})


def _row(analyte, label, value, unit, flag, rng="0-99"):
    return {"analyte": analyte, "label": label, "value": value, "unit": unit,
            "reference_range": rng, "flag": flag, "collected_date": "2026-03-10",
            "citation": f"labs_2026-03-10.pdf, p.1, 2026-03-10"}


def _single(analyte, label, value, unit, flag, out):
    return Step("get_lab_trend", {"analyte": analyte, "label": label, "points": [
        {"collected_date": "2025-09-12", "value": value + 10, "unit": unit,
         "reference_range": "0-99", "flag": "H", "out_of_range": True,
         "citation": "labs_2025-09-12.pdf, p.1, 2025-09-12"},
        {"collected_date": "2026-03-10", "value": value, "unit": unit,
         "reference_range": "0-99", "flag": flag, "out_of_range": out,
         "citation": "labs_2026-03-10.pdf, p.1, 2026-03-10"}]})


def test_flagged_analytes_reads_the_batch_form():
    steps = [_batch(_row("ldl", "LDL cholesterol", 112.0, "mg/dL", "H"),
                    _row("vitamin_d", "Vitamin D, 25-OH", 28.4, "ng/mL", "L", "30.0-100.0"))]
    got = context.flagged_analytes(steps)
    assert [g["analyte"] for g in got] == ["ldl", "vitamin_d"]
    assert got[0] == {"analyte": "ldl", "label": "LDL cholesterol", "value": 112.0,
                      "unit": "mg/dL", "date": "2026-03-10", "flag": "H",
                      "citation": "labs_2026-03-10.pdf, p.1, 2026-03-10"}


def test_flagged_analytes_reads_the_single_form_from_the_latest_point():
    flagged = _single("ldl", "LDL cholesterol", 112.0, "mg/dL", "H", True)
    in_range = _single("hdl", "HDL cholesterol", 52.0, "mg/dL", None, False)
    got = context.flagged_analytes([flagged, in_range])
    assert [g["analyte"] for g in got] == ["ldl"]
    assert got[0]["value"] == 112.0 and got[0]["date"] == "2026-03-10"


def test_flagged_analytes_dedupes_caps_and_keeps_tool_order():
    steps = [_single("ldl", "LDL cholesterol", 112.0, "mg/dL", "H", True),
             _batch(_row("ldl", "LDL cholesterol", 112.0, "mg/dL", "H"),
                    _row("a1c", "Hemoglobin A1c", 6.1, "%", "H"),
                    _row("tsh", "TSH", 5.2, "mIU/L", "H"),
                    _row("glucose", "Glucose", 104.0, "mg/dL", "H"))]
    got = context.flagged_analytes(steps)
    assert [g["analyte"] for g in got] == ["ldl", "a1c", "tsh"]


def test_flagged_analytes_is_empty_when_the_model_searched_the_literature():
    steps = [_batch(_row("ldl", "LDL cholesterol", 112.0, "mg/dL", "H")),
             Step("search_medical_literature", {"findings": [{"pmid": "1"}]})]
    assert context.flagged_analytes(steps) == []


def test_flagged_analytes_ignores_other_tools_and_errors():
    steps = [Step("query_healthkit", {"points": [{"value": 1}]}),
             Step("get_lab_trend", {"error": "analyte is required"}),
             Step("get_lab_trend", {"analyte": "ldl", "label": "LDL", "points": [],
                                    "no_data": True})]
    assert context.flagged_analytes(steps) == []


def test_flagged_analytes_ignores_a_printed_flag_with_no_parsed_range():
    """A text-valued lab result (`>300`) has no numeric range to parse, so
    `out_of_range` is None even though the report printed a flag. The single
    form's gate must match the batch form's (`tools.py` only appends to
    `out_of_range_on_latest_report` when the range says so), or the block
    would later try to format a non-numeric value and crash after the
    answer is already final."""
    steps = [Step("get_lab_trend", {"analyte": "ferritin", "label": "Ferritin", "points": [
        {"collected_date": "2026-03-10", "value": ">300", "unit": "ng/mL",
         "reference_range": None, "flag": "H", "out_of_range": None,
         "citation": "labs_2026-03-10.pdf, p.1, 2026-03-10"}]})]
    assert context.flagged_analytes(steps) == []


@pytest.fixture
def corpus_conn(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    corpus.build(conn, medline.parse_articles((FIX / "corpus.xml").read_bytes()),
                 slug="sample", version="1", license="L")
    return conn


def test_lab_context_attaches_citable_findings_per_analyte(corpus_conn):
    steps = [_batch(_row("ldl", "LDL cholesterol", 112.0, "mg/dL", "H"),
                    _row("zzz", "Zeta protein", 1.0, "u", "H"))]
    got = context.lab_context(steps, corpus_conn)
    assert [g.analyte for g in got] == ["ldl", "zzz"]
    ldl, zzz = got
    assert 1 <= len(ldl.findings) <= context.PER_ANALYTE
    assert set(ldl.findings[0]) == {"title", "year", "tier", "pmid"}
    retracted = {a.pmid for a in medline.parse_articles((FIX / "corpus.xml").read_bytes())
                 if a.retracted}
    assert not {f["pmid"] for f in ldl.findings} & retracted
    assert all(f["tier"] not in ("protocol", "unknown") for f in ldl.findings)
    assert zzz.findings == []


def test_lab_context_prefers_the_best_tier(corpus_conn, monkeypatch):
    from health_agent.literature.store import Finding
    found = [Finding(1, "1", "obs", "", "", evidence_tier="observational", evidence_rank=6),
             Finding(2, "2", "meta", "", "", evidence_tier="meta_analysis", evidence_rank=1),
             Finding(3, "3", "proto", "", "", evidence_tier="protocol", evidence_rank=None),
             Finding(4, "4", "rct", "", "", evidence_tier="rct", evidence_rank=3)]
    monkeypatch.setattr(store, "hits", lambda *a, **k: found)
    (ldl,) = context.lab_context([_batch(_row("ldl", "LDL cholesterol", 112.0, "mg/dL", "H"))],
                                 corpus_conn)
    assert [f["pmid"] for f in ldl.findings] == ["2", "4"]


def test_lab_context_without_a_corpus_is_empty():
    steps = [_batch(_row("ldl", "LDL cholesterol", 112.0, "mg/dL", "H"))]
    assert context.lab_context(steps, None) == []


def _ctx(findings):
    return context.AnalyteContext(analyte="ldl", label="LDL cholesterol", value=112.0,
                                  unit="mg/dL", date="2026-03-10", flag="H",
                                  citation="labs_2026-03-10.pdf, p.1, 2026-03-10",
                                  findings=findings)


def test_render_matches_the_block_shape():
    text = context.render([
        _ctx([{"title": "Title one", "year": 2026, "tier": "meta_analysis", "pmid": "42613609"},
              {"title": "Title two", "year": 2025, "tier": "rct", "pmid": "42609254"}]),
        context.AnalyteContext(analyte="vitamin_d", label="Vitamin D, 25-OH", value=28.4,
                               unit="ng/mL", date="2026-03-10", flag="L",
                               citation="labs_2026-03-10.pdf, p.1, 2026-03-10", findings=[]),
    ])
    assert text.startswith("---\nPublished research on the analytes flagged out of range")
    assert "not chosen by the\nmodel and not about your result." in text
    assert ("- **LDL cholesterol**, 112 mg/dL on 2026-03-10, flagged H:\n"
            "  *Title one* (2026, meta-analysis, PMID 42613609);\n"
            "  *Title two* (2025, randomized trial, PMID 42609254).") in text
    assert ("- **Vitamin D, 25-OH**, 28.4 ng/mL on 2026-03-10, flagged L:\n"
            "  no citable finding in the installed packs.") in text


def test_render_matches_the_block_shape_for_one_finding():
    text = context.render([
        _ctx([{"title": "Title one", "year": 2026, "tier": "meta_analysis",
              "pmid": "42613609"}]),
    ])
    assert ("- **LDL cholesterol**, 112 mg/dL on 2026-03-10, flagged H:\n"
            "  *Title one* (2026, meta-analysis, PMID 42613609).") in text


def test_render_strips_a_trailing_period_from_the_title():
    text = context.render([
        _ctx([{"title": "A trial.", "year": 2026, "tier": "rct", "pmid": "42613609"}]),
    ])
    assert "*A trial* (2026, randomized trial, PMID 42613609)" in text
    assert "*A trial.*" not in text


def test_render_of_nothing_is_empty():
    assert context.render([]) == ""


def test_render_does_not_raise_on_a_text_valued_result():
    text = context.render([context.AnalyteContext(
        analyte="ferritin", label="Ferritin", value=">300", unit="ng/mL",
        date="2026-03-10", flag="H", citation="labs_2026-03-10.pdf, p.1, 2026-03-10",
        findings=[])])
    assert ">300 ng/mL" in text


@pytest.mark.parametrize("with_findings", [False, True])
def test_the_guardrail_finds_nothing_to_flag_in_the_block(with_findings):
    findings = ([{"title": "An A1c above 6.5% is considered diabetic: a meta-analysis",
                  "year": 2026, "tier": "meta_analysis", "pmid": "42613609"}]
                if with_findings else [])
    contexts = [_ctx(findings)]
    text = context.render(contexts)
    flags = guardrail.check(text, used_tools=True,
                            returned_pmids=context.pmids(contexts))
    assert flags == [], [str(f) for f in flags]


def test_the_guardrail_finds_nothing_to_flag_with_two_threshold_titles():
    findings = [{"title": "An A1c above 6.5% is considered diabetic: a meta-analysis",
                "year": 2026, "tier": "meta_analysis", "pmid": "42613609"},
                {"title": "LDL over 130 mg/dL is classified as elevated: a cohort study",
                "year": 2025, "tier": "observational", "pmid": "42609254"}]
    contexts = [_ctx(findings)]
    text = context.render(contexts)
    flags = guardrail.check(text, used_tools=True,
                            returned_pmids=context.pmids(contexts))
    assert flags == [], [str(f) for f in flags]


def test_without_the_tools_pmids_the_guard_would_flag_a_threshold_title():
    """Proves the guard is doing real work here: without the block's own
    PMIDs passed as `returned_pmids`, the exact same threshold-shaped title
    IS flagged. `context.pmids` is what clears it."""
    findings = [{"title": "An A1c above 6.5% is considered diabetic: a meta-analysis",
                "year": 2026, "tier": "meta_analysis", "pmid": "42613609"},
                {"title": "LDL over 130 mg/dL is classified as elevated: a cohort study",
                "year": 2025, "tier": "observational", "pmid": "42609254"}]
    text = context.render([_ctx(findings)])
    flags = guardrail.check(text, used_tools=True, returned_pmids=frozenset())
    assert flags != []


def test_the_fixture_corpus_block_passes_the_guardrail(corpus_conn):
    steps = [_batch(_row("ldl", "LDL cholesterol", 112.0, "mg/dL", "H"),
                    _row("hba1c", "Hemoglobin A1c", 6.1, "%", "H"))]
    contexts = context.lab_context(steps, corpus_conn)
    text = context.render(contexts)
    flags = guardrail.check(text, used_tools=True,
                            returned_pmids=context.pmids(contexts))
    assert flags == [], [str(f) for f in flags]


def test_store_helpers_moved_and_shared():
    from health_agent import visit_prep
    assert visit_prep.tier_label is store.tier_label
    assert visit_prep.citable is store.citable
    assert store.tier_label("rct") == "randomized trial"
    assert store.tier_label("made_up_key") == "made up key"
