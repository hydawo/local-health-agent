"""End-to-end CLI tests against the synthetic fixture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from health_agent.cli import main


@pytest.fixture
def cli(tmp_path, fixture_export, capsys):
    """Returns a runner bound to a throwaway index, pre-loaded with the fixture."""
    index = tmp_path / "health.db"

    def run(*args: str) -> tuple[int, str]:
        code = main(["--index", str(index), *args])
        return code, capsys.readouterr().out

    run("ingest", str(fixture_export))
    capsys.readouterr()
    return run


def test_ingest_reports_counts(tmp_path, fixture_export, capsys):
    index = tmp_path / "health.db"
    code = main(["--index", str(index), "ingest", str(fixture_export)])
    out = capsys.readouterr().out
    assert code == 0
    assert "records read        25" in out
    assert "correlation dupes   2" in out
    assert index.exists()


def test_stats(cli):
    code, out = cli("stats")
    assert code == 0
    assert "25 across 7 HealthKit types" in out
    assert "2026-03-01 to 2026-03-09" in out


def test_metric_daily(cli):
    code, out = cli("metric", "resting-hr")
    assert code == 0
    assert "avg per day" in out
    assert "2026-03-01" in out and "58.0" in out


def test_metric_multi_source_warning_is_shown(cli):
    code, out = cli("metric", "steps")
    assert code == 0
    assert "6,000" in out
    assert "more than one recording source" in out
    assert "11,500" not in out


def test_metric_combine_sum_changes_both_value_and_warning(cli):
    code, out = cli("metric", "steps", "--combine", "sum")
    assert code == 0
    assert "11,500" in out
    assert "ADD every source together" in out


def test_metric_json_is_machine_readable(cli):
    code, out = cli("metric", "resting-hr", "--by", "month", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["aggregation"] == "avg"
    assert payload["points"][0]["value"] == 60.0


def test_missing_metric_names_what_is_missing_and_asks(cli):
    """Plan §5a: don't just say 'no data'."""
    code, out = cli("metric", "vo2max")
    assert code == 1
    assert "don't have any VO2 max data" in out
    assert "Do you track this" in out


def test_empty_window_reports_the_available_range(cli):
    code, out = cli("metric", "resting-hr", "--from", "2026-06-01", "--to", "2026-06-30")
    assert code == 1
    assert "2026-03-01 to 2026-03-07" in out


def test_workouts(cli):
    code, out = cli("workouts")
    assert code == 0
    assert "Running" in out


def test_types_search(cli):
    code, out = cli("types", "--search", "sleep")
    assert code == 0
    assert "HKCategoryTypeIdentifierSleepAnalysis" in out


def test_reingest_is_a_no_op(cli, fixture_export):
    code, out = cli("ingest", str(fixture_export))
    assert code == 0
    assert "unchanged" in out


def test_reset_requires_confirmation(cli, tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    code, out = cli("reset")
    assert code == 1
    assert "Cancelled" in out
    assert (tmp_path / "health.db").exists()


def test_reset_yes_skips_the_prompt(cli, tmp_path):
    code, out = cli("reset", "--yes")
    assert code == 0
    assert not (tmp_path / "health.db").exists()


def test_reset_does_not_destroy_the_literature_corpus(cli, tmp_path):
    """Design spec §1: "delete my data" and "delete my corpus" are different
    requests. `reset` must not reach the corpus's LanceDB table — regression
    for the bug where `literature_vector_path` and `vector_path` resolved to
    the same on-disk directory."""
    from health_agent import config as config_mod

    lit_fixture = Path(__file__).parent / "fixtures" / "literature" / "corpus.xml"
    code, _ = cli("literature", "build", "--from", str(lit_fixture),
                  "--slug", "test", "--version", "1",
                  "--embed-backend", "hashing")
    assert code == 0

    cfg = config_mod.resolve(index_path=str(tmp_path / "health.db"))
    assert cfg.literature_path.exists()
    assert cfg.literature_vector_path.exists()
    assert cfg.literature_vector_path != cfg.vector_path

    code, _ = cli("reset", "--yes")
    assert code == 0
    assert not cfg.index_path.exists()
    assert not cfg.vector_path.exists()
    # The corpus and its vectors survive.
    assert cfg.literature_path.exists()
    assert cfg.literature_vector_path.exists()


def test_query_before_ingest_is_actionable(tmp_path, capsys):
    code = main(["--index", str(tmp_path / "missing.db"), "stats"])
    err = capsys.readouterr().err
    assert code == 2
    assert "Run `health-agent ingest` first" in err


# --------------------------------------------------------------------------- #
# Records, labs and search (milestone 2)
# --------------------------------------------------------------------------- #

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cli_records(tmp_path, capsys):
    """A runner over an index loaded with every fixture source.

    Returns (exit code, stdout); the most recent stderr is left on `run.err`,
    since several behaviors (progress, warnings) are deliberately written there
    to keep stdout pipeable.
    """
    index = tmp_path / "health.db"

    def run(*args: str) -> tuple[int, str]:
        code = main(["--index", str(index), *args])
        captured = capsys.readouterr()
        run.err = captured.err
        return code, captured.out

    run.err = ""
    run("ingest", str(FIXTURES), "--no-ocr", "--no-embed")
    capsys.readouterr()
    return run


def test_ingest_reports_document_and_lab_counts(tmp_path, capsys):
    index = tmp_path / "health.db"
    code = main(["--index", str(index), "ingest", str(FIXTURES),
                 "--no-ocr", "--no-embed"])
    out = capsys.readouterr().out
    assert code == 0
    assert "3 document(s)" in out
    assert "lab values          39" in out


def test_stats_includes_documents(cli_records):
    code, out = cli_records("stats")
    assert code == 0
    assert "lab values         39" in out


def test_documents_listing(cli_records):
    code, out = cli_records("documents")
    assert code == 0
    assert "labs_2026-03-10.pdf" in out
    assert "scan_2025-03-04.pdf" in out


def test_labs_listing(cli_records):
    code, out = cli_records("labs")
    assert code == 0
    assert "hba1c" in out and "ldl" in out


def test_lab_trend_shows_values_with_citations(cli_records):
    code, out = cli_records("labs", "ldl")
    assert code == 0
    assert "128" in out and "112" in out
    assert "labs_2025-09-12.pdf, p.1" in out
    assert "down 16" in out


def test_lab_trend_marks_out_of_range(cli_records):
    code, out = cli_records("labs", "hba1c")
    assert code == 0
    assert "5.7 *" in out
    assert "outside the reference range" in out


def test_lab_trend_disclaims_interpretation(cli_records):
    """Non-diagnosis guardrail starts here, before any model is involved."""
    code, out = cli_records("labs", "ldl")
    assert "Interpretation is for you and your clinician" in out


def test_missing_analyte_asks_rather_than_shrugging(cli_records):
    code, out = cli_records("labs", "psa")
    assert code == 1
    assert "don't have any" in out


def test_lab_trend_json(cli_records):
    code, out = cli_records("labs", "hba1c", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["analyte_key"] == "hba1c"
    assert payload["points"][0]["out_of_range"] is True
    assert payload["points"][0]["page_no"] == 1


def test_search_falls_back_to_keywords_and_says_so(cli_records):
    code, out = cli_records("search", "cholesterol")
    assert code == 0
    assert "keyword" in out
    assert "labs_" in out


def test_search_with_no_match_suggests_alternatives(cli_records):
    code, out = cli_records("search", "zzzzqqq")
    assert code == 1
    assert "matched" in out


def test_embed_and_semantic_search_offline(cli_records):
    code, out = cli_records("embed", "--embedder", "hashing")
    assert code == 0
    assert "embedded with hashing" in out

    code, out = cli_records("search", "cholesterol", "--embedder", "hashing")
    assert code == 0
    assert "semantic" in out


def test_doctor_reports_environment(cli_records):
    code, out = cli_records("doctor")
    assert "OCR (for scanned records)" in out
    assert "Local model (Ollama)" in out
    assert "Network posture" in out
    assert code in (0, 1)


# --------------------------------------------------------------------------- #
# Notes (milestone 3)
# --------------------------------------------------------------------------- #

def test_ingest_reports_note_counts(tmp_path, capsys):
    index = tmp_path / "health.db"
    code = main(["--index", str(index), "ingest", str(FIXTURES),
                 "--no-ocr", "--no-embed"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Notes: 4 note(s)" in out
    assert "undated             1" in out


def test_a_data_shaped_folder_reads_its_subfolders_only(tmp_path, capsys):
    """Pointing at a folder with records/ and notes/ must not sweep up stray
    markdown elsewhere in the tree — a README beside your records is
    documentation, not a health note."""
    index = tmp_path / "health.db"
    main(["--index", str(index), "ingest", str(FIXTURES), "--no-ocr", "--no-embed"])
    capsys.readouterr()
    code, out = main(["--index", str(index), "notes"]), capsys.readouterr().out
    assert code == 0
    assert "README.md" not in out


def test_notes_listing(cli_records):
    code, out = cli_records("notes")
    assert code == 0
    assert "2026-03-11-sleep-log.md" in out
    assert "caffeine" in out


def test_notes_tags_listing(cli_records):
    code, out = cli_records("notes", "--tags")
    assert code == 0
    assert "followup" in out


def test_notes_by_tag(cli_records):
    code, out = cli_records("notes", "--tag", "followup")
    assert code == 0
    assert "2026-03-11-sleep-log.md" in out
    assert "2026-03-12-symptoms.md" in out


def test_unknown_tag_suggests_listing_tags(cli_records):
    code, out = cli_records("notes", "--tag", "nonexistent")
    assert code == 1
    assert "--tags" in out


def test_stats_includes_notes(cli_records):
    code, out = cli_records("stats")
    assert code == 0
    assert "Notes:" in out
    assert "tags" in out


def test_search_spans_notes_and_records(cli_records):
    """The point of the multi-source design: one query, both sources."""
    code, out = cli_records("search", "vitamin d", "--limit", "5")
    assert code == 0
    assert ".md" in out and ".pdf" in out


def test_note_citations_use_headings_not_page_numbers(cli_records):
    code, out = cli_records("search", "coffee", "--kind", "note")
    assert code == 0
    assert ">" in out          # a heading trail
    assert "p.1" not in out    # page numbers are meaningless for a note


def test_searching_for_a_tag_points_at_the_tag_lookup(cli_records):
    """Frontmatter is stripped before chunking, so a tag is not body text.
    Saying so beats letting the user conclude the note isn't indexed."""
    code, out = cli_records("search", "caffeine")
    assert code == 1
    assert "is a tag" in out
    assert "notes --tag caffeine" in out


def test_search_kind_filter_excludes_the_other_source(cli_records):
    code, out = cli_records("search", "vitamin d", "--kind", "pdf", "--limit", "5")
    assert code == 0
    assert ".pdf" in out
    assert ".md" not in out


# --------------------------------------------------------------------------- #
# ask (milestone 5) — against a stubbed model, so no Ollama is needed
# --------------------------------------------------------------------------- #

def _fake_chat(script):
    remaining = list(script)

    def chat(host, model, messages, **kwargs):
        message = remaining.pop(0) if remaining else {"content": "done"}
        return {"message": {"role": "assistant", **message}}

    return chat


def test_ask_answers_and_reports_tools(cli_records, monkeypatch):
    from health_agent import ollama_client

    monkeypatch.setattr(ollama_client, "chat", _fake_chat([
        {"content": "", "tool_calls": [{"function": {
            "name": "get_lab_trend", "arguments": {"analyte": "ldl"}}}]},
        {"content": "Your latest LDL is 112 mg/dL (labs_2026-03-10.pdf, p.1)."},
    ]))
    code, out = cli_records("ask", "What is my LDL?", "--show-tools")
    assert code == 0
    assert "112 mg/dL" in out
    assert "get_lab_trend" in out


def test_ask_json_includes_provenance(cli_records, monkeypatch):
    from health_agent import ollama_client

    monkeypatch.setattr(ollama_client, "chat", _fake_chat([
        {"content": "", "tool_calls": [{"function": {
            "name": "get_lab_trend", "arguments": {"analyte": "hba1c"}}}]},
        {"content": "5.4%"},
    ]))
    code, out = cli_records("ask", "HbA1c?", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["tools_used"] == ["get_lab_trend"]
    assert payload["steps"][0]["arguments"] == {"analyte": "hba1c"}
    assert any("labs_" in c for c in payload["citations_available"])


def test_ask_flags_an_answer_that_used_no_data(cli_records, monkeypatch):
    """An answer with no tool call is not grounded in the user's data, and
    saying so beats letting it read as though it were."""
    from health_agent import ollama_client

    monkeypatch.setattr(ollama_client, "chat",
                        _fake_chat([{"content": "Probably fine."}]))
    code, _ = cli_records("ask", "How am I doing?")
    assert code == 0
    assert "used none of your data" in cli_records.err


def test_ask_reports_an_unreachable_model(cli_records, monkeypatch):
    from health_agent import ollama_client

    def boom(*a, **k):
        raise ollama_client.OllamaUnavailable("Could not reach Ollama")

    monkeypatch.setattr(ollama_client, "chat", boom)
    code, _ = cli_records("ask", "anything")
    assert code == 2


def test_search_json_carries_kind_and_section(cli_records):
    code, out = cli_records("search", "coffee", "--kind", "note", "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload[0]["kind"] == "note"
    assert payload[0]["section"]


def test_literature_build_and_status(tmp_path, capsys):
    from health_agent.cli import main

    fixture = Path(__file__).parent / "fixtures" / "literature" / "corpus.xml"
    index = tmp_path / ".index" / "health.db"

    code = main(["--index", str(index), "literature", "build",
                 "--from", str(fixture), "--slug", "test", "--version", "1",
                 "--no-embed"])
    assert code == 0
    assert "articles" in capsys.readouterr().out

    code = main(["--index", str(index), "literature", "status"])
    assert code == 0
    out = capsys.readouterr().out
    assert "test@1" in out


def test_literature_status_without_a_corpus_explains_rather_than_crashes(
        tmp_path, capsys):
    from health_agent.cli import main

    code = main(["--index", str(tmp_path / ".index" / "health.db"),
                 "literature", "status"])
    assert code != 0
    assert "literature build" in capsys.readouterr().err
