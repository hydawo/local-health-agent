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
