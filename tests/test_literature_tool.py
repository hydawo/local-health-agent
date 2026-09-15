"""The agent-facing contract. Findings stay listed; nothing is synthesized."""
from __future__ import annotations

import pytest

from health_agent.agent import tools as agent_tools
from health_agent.literature import corpus, medline, schema


@pytest.fixture
def ctx(tmp_path, literature_fixture, ingested):
    conn, _ = ingested
    lit = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(lit)
    corpus.build(lit, medline.parse_articles(literature_fixture.read_bytes()),
                 slug="cardiometabolic", version="2026.02", license="CC-BY")
    return agent_tools.ToolContext(
        conn=conn, vector_path=tmp_path / "vectors", literature_conn=lit,
        literature_vector_path=tmp_path / "literature_vectors")


def test_tool_is_registered():
    assert "search_medical_literature" in agent_tools.BY_NAME


def test_returns_a_list_of_individually_cited_findings(ctx):
    payload = agent_tools.dispatch(ctx, "search_medical_literature",
                                   {"query": "exercise and blood pressure"})
    assert payload["findings"]
    for finding in payload["findings"]:
        assert finding["citation"]
        assert finding["pmid"]
        assert finding["evidence_tier"]
        assert finding["tier_source"] in ("publication_type", "unmapped")
        assert "year" in finding


def test_payload_instructs_against_combining_findings(ctx):
    payload = agent_tools.dispatch(ctx, "search_medical_literature",
                                   {"query": "blood pressure"})
    assert "recommendation" in payload["note"].lower()


def test_a_miss_returns_corpus_coverage_not_an_empty_list(ctx):
    payload = agent_tools.dispatch(
        ctx, "search_medical_literature",
        {"query": "zzzz nonexistent orthopedic arthroplasty topic"})
    assert payload["no_matches"] is True
    assert payload["corpus"]["article_count"] > 0
    assert payload["corpus"]["topics"]
    assert "do not answer from general knowledge" in payload["note"].lower()


def test_absent_corpus_says_so_rather_than_erroring(ingested, tmp_path):
    conn, _ = ingested
    bare = agent_tools.ToolContext(conn=conn, vector_path=tmp_path / "v")
    payload = agent_tools.dispatch(bare, "search_medical_literature",
                                   {"query": "anything"})
    assert payload["corpus_installed"] is False
    assert "not installed" in payload["note"].lower()


def test_missing_query_is_an_error_returned_as_data(ctx):
    payload = agent_tools.dispatch(ctx, "search_medical_literature", {})
    assert "error" in payload


def test_retracted_findings_carry_a_warning_for_the_model(ctx):
    payload = agent_tools.dispatch(
        ctx, "search_medical_literature",
        {"query": "study findings withdrawn", "limit": 8})
    if any(f["retracted"] for f in payload["findings"]):
        assert "retract" in payload["retraction_warning"].lower()


def test_findings_are_capped(ctx):
    payload = agent_tools.dispatch(ctx, "search_medical_literature",
                                   {"query": "blood pressure", "limit": 99})
    assert len(payload["findings"]) <= agent_tools.MAX_LITERATURE_FINDINGS


def test_payload_never_carries_personal_record_text(ctx):
    """Corpus findings and personal snippets answer different questions and are
    never merged into one payload."""
    payload = agent_tools.dispatch(ctx, "search_medical_literature",
                                   {"query": "cholesterol"})
    assert "results" not in payload      # search_records' key
    assert "analytes" not in payload     # get_lab_trend's key


def _search(ctx, **args):
    return agent_tools.dispatch(ctx, "search_medical_literature", args)


def test_a_filtered_miss_is_not_reported_as_a_coverage_gap(ctx):
    """With a floor set, 'the corpus holds nothing on this' is false: PMID
    40000009 matches these terms and is simply untagged. The payload must say
    the filter did the excluding, and how much of the corpus it excludes."""
    payload = _search(ctx, query="cuff confidence", min_tier="rct")
    assert payload["no_matches"] is True
    assert payload["filtered"] is True
    assert payload["filter"]["min_tier"] == "rct"
    assert payload["filter"]["unranked_articles_excluded"] >= 1
    assert 0 < payload["filter"]["unranked_share"] < 1
    note = payload["note"].lower()
    assert "holds nothing" not in note
    assert "no recorded study design" in note
    assert "without min_tier" in note
    assert "do not answer from general knowledge" in note


def test_an_unfiltered_miss_keeps_the_coverage_wording(ctx):
    payload = _search(ctx, query="zzzz nonexistent orthopedic arthroplasty topic")
    assert payload["no_matches"] is True
    assert "filtered" not in payload
    assert "filter" not in payload
    assert "holds nothing" in payload["note"].lower()


def test_a_since_year_only_miss_is_filtered_without_the_unranked_numbers(ctx):
    """The unranked share only explains a tier floor. Quoting it on a
    year-only miss would blame the wrong filter."""
    payload = _search(ctx, query="cuff confidence", since_year=2090)
    assert payload["no_matches"] is True
    assert payload["filtered"] is True
    assert payload["filter"]["since_year"] == 2090
    assert "unranked_articles_excluded" not in payload["filter"]
    assert "no recorded study design" not in payload["note"].lower()


def test_a_filtered_hit_reports_what_the_floor_excluded(ctx):
    payload = _search(ctx, query="blood pressure", min_tier="rct")
    assert payload["findings"]
    assert payload["filter"]["min_tier"] == "rct"
    assert payload["filter"]["unranked_articles_excluded"] >= 1
    assert "no recorded study design" in payload["filter_note"].lower()


def test_an_unfiltered_hit_carries_no_filter_block(ctx):
    payload = _search(ctx, query="blood pressure")
    assert payload["findings"]
    assert "filter" not in payload
    assert "filter_note" not in payload


def test_unknown_findings_carry_the_tier_warning(ctx):
    """Runs against a real unknown row (PMID 40000009) for the first time."""
    payload = _search(ctx, query="cuff confidence")
    assert any(f["pmid"] == "40000009" and f["evidence_tier"] == "unknown"
               for f in payload["findings"])
    assert "study design is unknown" in payload["tier_warning"].lower()


def test_protocol_findings_carry_their_own_warning(ctx):
    payload = _search(ctx, query="reminders statin")
    protocol = next(f for f in payload["findings"] if f["pmid"] == "40000010")
    assert protocol["evidence_tier"] == "protocol"
    assert protocol["tier_source"] == "publication_type"
    assert "no results" in payload["protocol_warning"].lower()


def test_min_tier_description_warns_that_most_of_the_corpus_is_untagged():
    schema = agent_tools.BY_NAME["search_medical_literature"].schema()
    params = schema["function"]["parameters"]
    description = params["properties"]["min_tier"]["description"].lower()
    assert "no study-design tag" in description or "no recorded study design" in description
    assert "strongest-first" in description or "strongest first" in description
    assert "protocol" not in params["properties"]["min_tier"]["enum"]
    assert "unknown" not in params["properties"]["min_tier"]["enum"]
