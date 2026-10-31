"""End-to-end CLI tests against the synthetic fixture."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest

from health_agent.cli import main
from health_agent.ingest import records


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

from health_agent.ingest import fhir  # noqa: E402

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


@pytest.fixture
def cli_no_clinical(tmp_path, capsys):
    """An index loaded with every fixture source except clinical records."""
    index = tmp_path / "health.db"

    def run(*args: str) -> tuple[int, str]:
        code = main(["--index", str(index), *args])
        captured = capsys.readouterr()
        run.err = captured.err
        return code, captured.out

    run.err = ""
    run("ingest", str(FIXTURES), "--no-ocr", "--no-embed", "--no-clinical")
    capsys.readouterr()
    return run


def _partial(tmp_path, capsys, *skip):
    index = tmp_path / "health.db"
    main(["--index", str(index), "ingest", str(FIXTURES), "--no-ocr",
          "--no-embed", "--skip-clinical", ",".join(skip)])
    capsys.readouterr()

    def run(*args):
        code = main(["--index", str(index), *args])
        return code, capsys.readouterr().out
    return run


_FACTS = ("conditions", "medications", "allergies", "immunizations",
          "procedures")


def test_clinical_with_vitals_but_no_facts_names_what_is_held(tmp_path, capsys):
    run = _partial(tmp_path, capsys, *_FACTS)
    code, out = run("clinical")
    assert code == 0
    assert "No clinical facts" in out
    assert "3 clinician vitals" in out and "2 note references" in out
    assert "No clinical records in the index" not in out


def test_vitals_with_facts_but_no_vitals_names_what_is_held(tmp_path, capsys):
    run = _partial(tmp_path, capsys, "vitals")
    code, out = run("vitals")
    assert code == 0
    assert "No clinician-measured vitals" in out
    assert "1 medications" in out and "1 conditions" in out
    assert "3 lab values" in out


def test_empty_messages_state_what_the_index_holds(cli_no_clinical):
    for cmd in ("clinical", "vitals"):
        code, out = cli_no_clinical(cmd)
        assert code == 0
        assert "HealthKit records" in out
        assert "clinical-records" in out


def test_vitals_miss_words_the_range_only_when_one_was_given(cli_records):
    _, out = cli_records("vitals", "--metric", "nonesuch")
    assert "date range" not in out and "The index holds" in out
    _, out = cli_records("vitals", "--metric", "pulse", "--start", "2030-01-01")
    assert "date range" in out


def test_vitals_says_when_limit_cut_the_result(cli_records):
    _, out = cli_records("vitals", "--metric", "pulse", "--limit", "1")
    assert "Stopped at --limit 1" in out
    _, out = cli_records("vitals", "--metric", "pulse", "--limit", "5")
    assert "Stopped at --limit" not in out


def test_clinical_lists_facts_as_a_table(cli_records):
    code, out = cli_records("clinical")
    assert code == 0
    assert "ibuprofen 600 MG tablet" in out
    assert "Left anterior cruciate ligament tear" in out
    assert "Take 1 tablet" in out


def test_clinical_filters_by_kind(cli_records):
    code, out = cli_records("clinical", "--kind", "conditions")
    assert code == 0
    assert "Left anterior cruciate ligament tear" in out
    assert "ibuprofen" not in out


def test_clinical_rejects_an_unknown_kind(cli_records):
    code, out = cli_records("clinical", "--kind", "diagnoses")
    assert code == 2
    assert "conditions" in cli_records.err


def test_clinical_says_so_when_there_are_none(cli_no_clinical):
    code, out = cli_no_clinical("clinical")
    assert code == 0
    assert "no clinical records" in out.lower()
    assert "clinical-records" in out


def test_vitals_lists_clinician_measurements(cli_records):
    code, out = cli_records("vitals", "--metric", "pulse")
    assert code == 0
    assert "72" in out and "bpm" in out
    assert "2023-09-15" in out


def test_vitals_without_a_metric_lists_what_exists(cli_records):
    code, out = cli_records("vitals")
    assert code == 0
    assert "pulse" in out.lower() and "spo2" in out.lower()


def test_vitals_says_so_when_there_are_none(cli_no_clinical):
    code, out = cli_no_clinical("vitals")
    assert code == 0
    assert "no clinician-measured vitals" in out.lower()
    assert "clinical-records" in out


def test_stats_counts_clinical_records(cli_records):
    code, out = cli_records("stats")
    assert code == 0
    assert "clinical records" in out.lower()
    import re
    for label, n in (("lab values", 3), ("clinician vitals", 3),
                     ("conditions", 1), ("medications", 1), ("allergies", 1),
                     ("immunizations", 1), ("procedures", 1),
                     ("note references", 2)):
        block = out.split("Clinical records (")[1]
        assert re.search(rf"^\s+{label}\s+{n}\b", block, re.M), label


def test_stats_clinical_block_gives_per_kind_counts_not_a_document_total(
        cli_records):
    """Childless document rows (dedupe collapses, note references) make a raw
    clinical document total misleading; the block reports per-kind counts."""
    code, out = cli_records("stats")
    block = out.split("Clinical records (")[1].split("\n\n")[0]
    assert "documents" not in block.lower()
    assert "lab values" in block and "clinician vitals" in block


def test_stats_omits_clinical_block_when_none(cli_no_clinical):
    code, out = cli_no_clinical("stats")
    assert code == 0
    assert "clinical records" not in out.lower()


def test_check_reports_clinical_records(cli_records, monkeypatch):
    from health_agent import embeddings
    monkeypatch.setattr(embeddings.OllamaEmbedder, "health_check",
                        lambda self: "stubbed")
    code, out = cli_records("check")
    assert ("clinical records: 3 lab values, 3 clinician vitals, 1 conditions, "
            "1 medications, 1 allergies, 1 immunizations, 1 procedures, "
            "2 note references") in out


def test_check_names_the_switch_that_skipped_the_folder(cli_no_clinical,
                                                        monkeypatch):
    """Spec §9: `check` reports whether the ingested export had clinical
    records, and names the switch when a folder was there and was skipped.
    Silence was the old behaviour and it is the one answer the reader cannot
    act on: --no-clinical and "your health system sent nothing" looked
    identical."""
    from health_agent import embeddings
    monkeypatch.setattr(embeddings.OllamaEmbedder, "health_check",
                        lambda self: "stubbed")
    code, out = cli_no_clinical("check")
    assert "clinical records: a clinical-records folder was in the export "\
           "and was skipped by --no-clinical" in out


def test_check_reports_an_export_with_no_clinical_folder(tmp_path, capsys,
                                                         monkeypatch):
    from health_agent import embeddings
    monkeypatch.setattr(embeddings.OllamaEmbedder, "health_check",
                        lambda self: "stubbed")
    index = tmp_path / "health.db"
    export = tmp_path / "export.xml"
    export.write_bytes((FIXTURES / "export.xml").read_bytes())
    main(["--index", str(index), "ingest", str(export), "--no-embed"])
    capsys.readouterr()
    main(["--index", str(index), "check"])
    out = capsys.readouterr().out
    assert ("clinical records: none; the ingested export had no "
            "clinical-records folder." in out)


def test_check_names_the_kinds_left_out_by_skip_clinical(tmp_path, capsys,
                                                         monkeypatch):
    from health_agent import embeddings
    monkeypatch.setattr(embeddings.OllamaEmbedder, "health_check",
                        lambda self: "stubbed")
    index = tmp_path / "health.db"
    main(["--index", str(index), "ingest", str(FIXTURES), "--no-ocr",
          "--no-embed", "--skip-clinical", "vitals,conditions"])
    capsys.readouterr()
    main(["--index", str(index), "check"])
    out = capsys.readouterr().out
    assert "clinical records: 3 lab values" in out
    assert "left out by --skip-clinical conditions,vitals" in out


def test_check_reports_a_folder_skipped_in_full(tmp_path, capsys, monkeypatch):
    from health_agent import embeddings
    monkeypatch.setattr(embeddings.OllamaEmbedder, "health_check",
                        lambda self: "stubbed")
    index = tmp_path / "health.db"
    every = ",".join(("labs", "vitals", "conditions", "medications",
                      "allergies", "immunizations", "procedures", "notes"))
    main(["--index", str(index), "ingest", str(FIXTURES), "--no-ocr",
          "--no-embed", "--skip-clinical", every])
    capsys.readouterr()
    main(["--index", str(index), "check"])
    out = capsys.readouterr().out
    assert "was skipped by --skip-clinical" in out


def test_clinical_table_stays_within_120_columns_for_long_rows(tmp_path,
                                                               capsys):
    from health_agent.cli import _CLINICAL_DETAIL_W, _CLINICAL_NAME_W
    from health_agent.store import sqlite_schema as ss
    index = tmp_path / "health.db"
    main(["--index", str(index), "ingest", str(FIXTURES), "--no-ocr",
          "--no-embed"])
    capsys.readouterr()
    conn = ss.connect(index)
    long_name = "Bucket-handle tear of lateral meniscus of left knee as currently documented"
    long_detail = "Take 1 tablet (10 mg total) by mouth 2 (two) times a day with food " * 2
    conn.execute("UPDATE clinical_fact SET name = ?, detail_text = ? "
                 "WHERE kind = 'medication'", (long_name, long_detail))
    long_status = "entered-in-error-and-then-some"
    conn.execute("UPDATE clinical_fact SET status = ? WHERE kind = 'condition'",
                 (long_status,))
    conn.commit()
    conn.close()
    code = main(["--index", str(index), "clinical"])
    out = capsys.readouterr().out
    table = out.split("\n\n")[0].splitlines()
    assert code == 0
    assert max(len(line) for line in table) <= 120
    row = next(line for line in table if line.startswith("medication"))
    assert "..." in row
    assert long_name not in row
    cond = next(line for line in table if line.startswith("condition"))
    assert long_status not in cond and "..." in cond
    assert _CLINICAL_NAME_W < len(long_name) and _CLINICAL_DETAIL_W < len(long_detail)


@pytest.mark.parametrize("stored", [
    "clinical-records/Observation-vital-pulse.json",
    "apple_health_export/clinical-records/Observation-vital-pulse.json",
    "clinical-records\\Observation-vital-pulse.json",
    "Observation-vital-pulse.json",
])
def test_vitals_citation_is_the_basename(tmp_path, capsys, stored):
    from health_agent.store import sqlite_schema as ss
    index = tmp_path / "health.db"
    main(["--index", str(index), "ingest", str(FIXTURES), "--no-ocr",
          "--no-embed"])
    capsys.readouterr()
    conn = ss.connect(index)
    conn.execute("UPDATE document SET path = ? WHERE id IN "
                 "(SELECT document_id FROM clinical_vital "
                 "WHERE metric_key = 'pulse')", (stored,))
    conn.commit()
    conn.close()
    code = main(["--index", str(index), "vitals", "--metric", "pulse"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Observation-vital-pulse.json" in out
    assert "clinical-records" not in out and "apple_health_export" not in out


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
    assert "lab values         42" in out


def test_documents_listing(cli_records):
    code, out = cli_records("documents")
    assert code == 0
    assert "labs_2026-03-10.pdf" in out
    assert "scan_2025-03-04.pdf" in out


def test_documents_listing_keeps_its_meaning_and_names_the_fhir_switch(
        cli_records):
    """I5: the records surface is the PDFs and notes a person dropped in. The
    export's clinical-records resources are documents too, and dumping them
    here turned a 3-row listing into thousands with no way to filter."""
    code, out = cli_records("documents")
    assert code == 0
    assert "Observation-lab-chol.json" not in out
    assert "documents --kind fhir" in out


def test_documents_kind_fhir_lists_the_clinical_resources(cli_records):
    code, out = cli_records("documents", "--kind", "fhir")
    assert code == 0
    assert "Observation-lab-chol.json" in out
    assert "labs_2026-03-10.pdf" not in out


def test_documents_rejects_an_unknown_kind(cli_records):
    code, _ = cli_records("documents", "--kind", "nope")
    assert code == 2


def test_documents_kind_fhir_reports_absence_as_coverage(cli_no_clinical):
    code, out = cli_no_clinical("documents", "--kind", "fhir")
    assert code == 1
    assert "No clinical-records resources in this index" in out
    assert "HealthKit records" in out


def test_labs_listing(cli_records):
    code, out = cli_records("labs")
    assert code == 0
    assert "hba1c" in out and "ldl" in out


def test_lab_trend_shows_values_with_citations(cli_records):
    code, out = cli_records("labs", "ldl")
    assert code == 0
    assert "128" in out and "112" in out
    assert "labs_2025-09-12.pdf, p.1" in out
    assert "down 6" in out


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
    # points[0] is now the clinical-records text-only result (2023-05-02);
    # the PDF-derived 5.7 % is the second point.
    assert payload["points"][0]["value_text"] == "see comment"
    assert payload["points"][1]["out_of_range"] is True
    assert payload["points"][1]["page_no"] == 1


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


def test_check_reports_environment(cli_records):
    code, out = cli_records("check")
    assert "OCR (for scanned records and photos)" in out
    assert "HEIC photos" in out
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
    assert "Notes: 5 note(s)" in out
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


def test_search_kind_image_reaches_photos_read_by_ocr(tmp_path, capsys):
    """The CLI's `search --kind image` is the same filter the agent tool
    accepts as kind='image'; a user should be able to ask for it too."""
    if not records.ocr_available():
        pytest.skip("Tesseract not installed; the image path is exercised in CI")

    docs = Path(__file__).parent / "fixtures" / "documents"
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    shutil.copy(docs / "medication-list.png", notes_dir / "IMG_0042.png")
    index = tmp_path / "health.db"

    code = main(["--index", str(index), "ingest", str(notes_dir), "--no-embed"])
    assert code == 0
    capsys.readouterr()

    code = main(["--index", str(index), "search", "metformin", "--kind", "image"])
    out = capsys.readouterr().out
    assert code == 0
    assert "(read by OCR)" in out


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


def test_ask_prints_and_serializes_the_literature_block(cli_records, tmp_path, monkeypatch):
    from health_agent import ollama_client

    pack_path = _write_pack(tmp_path, "cardiovascular")
    code, _ = cli_records("literature", "install", "cardiovascular",
                          "--from", str(pack_path), "--yes", "--no-embed")
    assert code == 0

    script = [
        {"content": "", "tool_calls": [{"function": {
            "name": "get_lab_trend", "arguments": {"analyte": "ldl"}}}]},
        {"content": "Your LDL was 112 mg/dL, flagged H."},
    ]
    monkeypatch.setattr(ollama_client, "chat", _fake_chat(script))
    code, out = cli_records("ask", "What is my LDL?")
    assert code == 0
    assert "Your LDL was 112 mg/dL" in out
    assert "Published research on the analytes flagged out of range" in out
    assert out.rstrip().endswith(").") or "no citable finding" in out

    monkeypatch.setattr(ollama_client, "chat", _fake_chat(script))
    code, out = cli_records("ask", "--json", "What is my LDL?")
    data = json.loads(out)
    assert data["literature_context"][0]["analyte"] == "ldl"
    assert data["literature_context_text"].startswith("---")

    monkeypatch.setattr(ollama_client, "chat", _fake_chat(script))
    code, out = cli_records("ask", "--no-literature-context", "What is my LDL?")
    assert "Published research" not in out


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


def test_literature_rebuild_replaces_the_corpus_and_spares_the_personal_index(
        cli, tmp_path):
    """The mirror of test_reset_does_not_destroy_the_literature_corpus:
    --rebuild reaches literature.db and the literature_chunks table, and
    nothing else."""
    from health_agent import config as config_mod
    from health_agent.literature import embed as lit_embed
    from health_agent.literature import schema as lit_schema
    from health_agent.store import sqlite_schema
    from health_agent.store import vector_store

    lit_fixture = Path(__file__).parent / "fixtures" / "literature" / "corpus.xml"
    code, _ = cli("literature", "build", "--from", str(lit_fixture),
                  "--slug", "test", "--version", "1",
                  "--embed-backend", "hashing")
    assert code == 0
    cfg = config_mod.resolve(index_path=str(tmp_path / "health.db"))

    personal = sqlite_schema.connect(cfg.index_path)
    records_before = personal.execute("SELECT COUNT(*) AS n FROM record").fetchone()["n"]
    personal.close()
    assert records_before > 0

    # Pretend the corpus is from an older schema.
    lit = lit_schema.connect(cfg.literature_path)
    lit.execute("UPDATE corpus_meta SET value = '1' WHERE key = 'schema_version'")
    lit.commit()
    lit.close()

    code, out = cli("literature", "build", "--from", str(lit_fixture),
                    "--slug", "test", "--version", "1",
                    "--embed-backend", "hashing")
    assert code != 0                      # refused: wrong version, no --rebuild

    code, out = cli("literature", "build", "--from", str(lit_fixture),
                    "--slug", "test", "--version", "1",
                    "--embed-backend", "hashing", "--rebuild")
    assert code == 0
    assert "articles" in out

    lit = lit_schema.connect(cfg.literature_path)
    assert lit_schema.read_version(lit) == lit_schema.LITERATURE_SCHEMA_VERSION
    assert lit.execute("SELECT COUNT(*) AS n FROM article").fetchone()["n"] > 0
    chunks_after = lit.execute(
        "SELECT COUNT(*) AS n FROM article_chunk").fetchone()["n"]
    lit.close()

    # A no-op drop would leave the old table's rows in place, whose chunk_ids
    # collide with the rebuilt corpus's new article_chunk ids — search would
    # then hydrate stale text under a fresh citation. Proving the vector
    # table's row count matches the rebuilt corpus rules that out.
    lit_vectors = vector_store.VectorStore(cfg.literature_vector_path,
                                            table_name=lit_embed.TABLE_NAME)
    assert lit_vectors.count() == chunks_after

    personal = sqlite_schema.connect(cfg.index_path)
    assert personal.execute("SELECT COUNT(*) AS n FROM record").fetchone()["n"] == records_before
    personal.close()
    # The `cli` fixture ingests a HealthKit export only, so the personal
    # vector store was never created; asserting it stays absent is this
    # test's way of showing --rebuild never reaches it.
    assert not cfg.vector_path.exists()


def test_literature_build_into_a_stale_corpus_names_the_rebuild_flag(
        tmp_path, capsys):
    from health_agent.cli import main
    from health_agent import config as config_mod
    from health_agent.literature import schema as lit_schema

    fixture = Path(__file__).parent / "fixtures" / "literature" / "corpus.xml"
    index = tmp_path / ".index" / "health.db"
    assert main(["--index", str(index), "literature", "build", "--from",
                 str(fixture), "--no-embed"]) == 0
    capsys.readouterr()

    cfg = config_mod.resolve(index_path=str(index))
    lit = lit_schema.connect(cfg.literature_path)
    lit.execute("UPDATE corpus_meta SET value = '1' WHERE key = 'schema_version'")
    lit.commit()
    lit.close()

    code = main(["--index", str(index), "literature", "build", "--from",
                 str(fixture), "--no-embed"])
    assert code == 2
    assert "--rebuild" in capsys.readouterr().err

    code = main(["--index", str(index), "literature", "status"])
    assert code != 0
    assert "--rebuild" in capsys.readouterr().err


def test_ask_path_survives_a_stale_corpus(tmp_path, capsys):
    """A wrong-version corpus must not crash `ask`; it is reported and the
    tool sees no corpus, which it already knows how to say."""
    from health_agent.cli import main
    from health_agent import config as config_mod
    from health_agent.literature import schema as lit_schema

    fixture = Path(__file__).parent / "fixtures" / "literature" / "corpus.xml"
    index = tmp_path / ".index" / "health.db"
    assert main(["--index", str(index), "literature", "build", "--from",
                 str(fixture), "--no-embed"]) == 0
    cfg = config_mod.resolve(index_path=str(index))
    lit = lit_schema.connect(cfg.literature_path)
    lit.execute("UPDATE corpus_meta SET value = '1' WHERE key = 'schema_version'")
    lit.commit()
    lit.close()
    capsys.readouterr()

    from health_agent import cli as cli_mod
    opened = cli_mod._open_literature_corpus(cfg)
    err = capsys.readouterr().err
    assert opened is None
    assert "--rebuild" in err


def test_ask_path_survives_a_corpus_that_cannot_be_migrated_in_place(tmp_path, capsys):
    """A v4 corpus on a read-only file cannot take the in-place migration
    `connect` normally does; that raises sqlite3.OperationalError, which
    must be reported and treated like any other corpus that is no help to
    `ask`, not let through as a crash."""
    import os

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores file permission bits")

    from tests.test_literature_schema import _v4_corpus
    from health_agent import cli as cli_mod
    from health_agent import config as config_mod

    index = tmp_path / ".index" / "health.db"
    cfg = config_mod.resolve(index_path=str(index))
    cfg.literature_path.parent.mkdir(parents=True, exist_ok=True)
    _v4_corpus(cfg.literature_path)
    cfg.literature_path.chmod(0o444)
    capsys.readouterr()

    try:
        opened = cli_mod._open_literature_corpus(cfg)
    finally:
        cfg.literature_path.chmod(0o644)

    err = capsys.readouterr().err
    assert opened is None
    assert "warning: literature corpus not used:" in err


def _document_paths(index: Path) -> list[str]:
    from health_agent.store import sqlite_schema

    conn = sqlite_schema.connect(index)
    try:
        return [r["path"] for r in conn.execute("SELECT path FROM document")]
    finally:
        conn.close()


def test_ingest_routes_an_image_to_the_notes_path(tmp_path, capsys, monkeypatch):
    """An explicit `ingest photo.png` is a note ingest of that one file: a
    sibling in the same folder is not swept in. With --no-ocr the summary
    says the photo was skipped because of the flag, not because Tesseract is
    missing."""
    from health_agent.cli import main

    docs = Path(__file__).parent / "fixtures" / "documents"
    photo = tmp_path / "photo.png"
    shutil.copy(docs / "medication-list.png", photo)
    (tmp_path / "todo.txt").write_text("buy milk\ncall the plumber\n")
    index = tmp_path / ".index" / "health.db"

    code = main(["--index", str(index), "ingest", str(photo), "--no-ocr", "--no-embed"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Ingesting 1 note(s)" in out
    assert "skipped: ocr disabled: 1" in out
    assert "--no-ocr was passed" in out
    assert "brew install tesseract" not in out
    assert not any(p.endswith("todo.txt") for p in _document_paths(index))


def test_ingest_of_one_pdf_does_not_sweep_its_siblings(tmp_path, capsys):
    """`ingest labs.pdf` ingests labs.pdf. Handing the parent folder to the
    records sweep would pull in every other PDF in Downloads."""
    from health_agent.cli import main

    fixtures = Path(__file__).parent / "fixtures" / "records"
    target = tmp_path / "labs.pdf"
    shutil.copy(fixtures / "labs_2026-03-10.pdf", target)
    shutil.copy(fixtures / "labs_2025-09-12.pdf", tmp_path / "older.pdf")
    index = tmp_path / ".index" / "health.db"

    code = main(["--index", str(index), "ingest", str(target), "--no-ocr", "--no-embed"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Ingesting 1 record file(s)" in out
    assert "Records: 1 document(s)" in out
    paths = _document_paths(index)
    assert len(paths) == 1
    assert paths[0].endswith("labs.pdf")


def test_ingest_install_hint_fires_only_when_tesseract_is_missing(
        tmp_path, capsys, monkeypatch):
    """Telling someone with Tesseract installed to install Tesseract sends
    them chasing the wrong problem; the hint is for the machine without it."""
    from health_agent.cli import main
    from health_agent.ingest import readers, records as records_mod

    monkeypatch.setattr(records_mod, "ocr_available", lambda: False)
    monkeypatch.setattr(readers, "read_image", lambda path: (_ for _ in ()).throw(
        records_mod.OcrUnavailable("no tesseract")))
    docs = Path(__file__).parent / "fixtures" / "documents"
    photo = tmp_path / "photo.png"
    shutil.copy(docs / "medication-list.png", photo)
    index = tmp_path / ".index" / "health.db"

    code = main(["--index", str(index), "ingest", str(photo), "--no-embed"])
    out = capsys.readouterr().out
    assert code == 0
    assert "skipped: ocr unavailable: 1" in out
    assert "brew install tesseract" in out
    assert "Then re-run `ingest`." in out
    assert "--force" not in out


def test_ingest_names_photos_left_in_the_records_folder(tmp_path, capsys):
    """A photo in records/ is read by nothing. Saying so, and where it does
    belong, is the difference between a gap and a silent one."""
    from health_agent.cli import main

    docs = Path(__file__).parent / "fixtures" / "documents"
    data = tmp_path / "data"
    (data / "records").mkdir(parents=True)
    (data / "notes").mkdir()
    shutil.copy(docs / "medication-list.png", data / "records" / "photo.png")
    (data / "notes" / "2026-01-01-note.md").write_text("# A note\n\nsome text\n")
    index = tmp_path / ".index" / "health.db"

    code = main(["--index", str(index), "ingest", str(data), "--no-ocr", "--no-embed"])
    captured = capsys.readouterr()
    assert code == 0
    assert ("Note: 1 photo(s) in records/ were not read; photos belong in notes/"
            in captured.err)
    assert not any(p.endswith("photo.png") for p in _document_paths(index))


@pytest.mark.skipif(not records.ocr_available(),
                    reason="Tesseract not installed; the image path is exercised in CI")
def test_notes_listing_includes_photos_marked_as_ocr(tmp_path, capsys):
    """A photo is listed with the notes it sits among, marked so a reader
    knows its text came from pixels."""
    from health_agent.cli import main

    docs = Path(__file__).parent / "fixtures" / "documents"
    photo = tmp_path / "photo.png"
    shutil.copy(docs / "medication-list.png", photo)
    index = tmp_path / ".index" / "health.db"
    assert main(["--index", str(index), "ingest", str(photo), "--no-embed"]) == 0
    capsys.readouterr()

    assert main(["--index", str(index), "notes"]) == 0
    out = capsys.readouterr().out
    assert "photo.png (OCR)" in out


def test_literature_packs_lists_the_catalog_offline(tmp_path, capsys, monkeypatch):
    from health_agent.cli import main
    from health_agent.literature.fetch import client

    monkeypatch.setattr(client, "_open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    code = main(["--index", str(tmp_path / ".index" / "health.db"), "literature", "packs"])
    out = capsys.readouterr().out
    assert code == 0
    for slug in ("sample", "cardiovascular", "metabolic", "sleep", "exercise"):
        assert slug in out
    assert "not installed" in out


def test_literature_install_stops_before_any_fetch_without_consent(tmp_path, capsys, monkeypatch):
    from health_agent.cli import main
    from health_agent.literature.fetch import client, packs as fetch_packs

    calls = []
    monkeypatch.setattr(fetch_packs, "download", lambda *a, **k: calls.append(a) or None)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))   # not a tty, no --yes
    code = main(["--index", str(tmp_path / ".index" / "health.db"),
                 "literature", "install", "sleep"])
    err = capsys.readouterr().err
    assert code != 0
    assert calls == []
    assert "LITERATURE PACKS" in err


def test_literature_install_from_a_local_pack_builds_the_corpus(tmp_path, capsys):
    from health_agent.cli import main
    from health_agent.literature import medline, packs, schema as lit_schema
    from health_agent import config as config_mod

    articles = medline.parse_articles(
        (Path(__file__).parent / "fixtures" / "literature" / "corpus.xml").read_bytes())
    pack_path = tmp_path / "sleep-1.jsonl.gz"
    packs.write_pack(pack_path, packs.CATALOG["sleep"], "1", articles, license=packs.PACK_LICENSE)
    index = tmp_path / ".index" / "health.db"

    code = main(["--index", str(index), "literature", "install", "sleep",
                 "--from", str(pack_path), "--yes", "--no-embed"])
    out = capsys.readouterr().out
    assert code == 0
    assert "sleep@1" in out

    cfg = config_mod.resolve(index_path=str(index))
    conn = lit_schema.connect(cfg.literature_path)
    row = conn.execute("SELECT slug, version, article_count FROM pack").fetchone()
    assert (row["slug"], row["version"]) == ("sleep", "1")
    assert row["article_count"] == len([a for a in articles if a.abstract.strip()])
    # Tiers come from publication_types at install, not from the pack.
    tiers = {r["pmid"]: r["evidence_tier"] for r in conn.execute("SELECT pmid, evidence_tier FROM article")}
    assert tiers["40000001"] == "meta_analysis" and tiers["40000010"] == "protocol"
    assert conn.execute("SELECT DISTINCT license FROM article").fetchone()["license"] == packs.PACK_LICENSE
    conn.close()

    # Same version again is a no-op that says so.
    code = main(["--index", str(index), "literature", "install", "sleep",
                 "--from", str(pack_path), "--yes", "--no-embed"])
    assert code == 0
    assert "already installed" in capsys.readouterr().out


def test_literature_install_refuses_an_unknown_pack(tmp_path, capsys):
    from health_agent.cli import main
    code = main(["--index", str(tmp_path / ".index" / "health.db"),
                 "literature", "install", "type-2-diabetes", "--yes"])
    assert code == 2
    assert "not a known pack" in capsys.readouterr().err


def test_build_pack_writes_a_pack_file_without_touching_the_index(tmp_path, capsys, monkeypatch):
    from health_agent.cli import main
    from health_agent.literature import packs
    from health_agent.literature.fetch import eutils

    fix = Path(__file__).parent / "fixtures" / "literature"
    monkeypatch.setattr(eutils, "search", lambda term, **k: eutils.SearchHandle(2, "W", "1"))
    monkeypatch.setattr(eutils, "fetch_all", lambda handle, **k: __import__(
        "health_agent.literature.medline", fromlist=["parse_articles"]
    ).parse_articles((fix / "efetch_batch.xml").read_bytes()))
    index = tmp_path / ".index" / "health.db"
    code = main(["--index", str(index), "literature", "build-pack", "sleep",
                 "--out", str(tmp_path / "out"), "--version", "2026.09", "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    written = tmp_path / "out" / "sleep-2026.09.jsonl.gz"
    assert written.exists() and (tmp_path / "out" / "sleep-2026.09.jsonl.gz.sha256").exists()
    manifest, articles = packs.read_pack(written)
    assert manifest.article_count == 2 and len(articles) == 2
    assert not (tmp_path / ".index" / "literature.db").exists()
    assert "2 articles" in out


def test_literature_consent_command_shows_revokes_and_reports(tmp_path, capsys):
    from health_agent.cli import main
    from health_agent import consent

    index = str(tmp_path / ".index" / "health.db")
    assert main(["--index", index, "literature-consent", "--show-notice"]) == 0
    assert "LITERATURE PACKS" in capsys.readouterr().out

    assert main(["--index", index, "literature-consent"]) == 0
    assert "not consented" in capsys.readouterr().out

    assert main(["--index", index, "literature-consent", "--revoke"]) == 1
    capsys.readouterr()

    consent.record(tmp_path / ".index", "", notice=consent.LITERATURE)
    assert main(["--index", index, "literature-consent"]) == 0
    assert "consented" in capsys.readouterr().out
    assert main(["--index", index, "literature-consent", "--revoke"]) == 0
    assert "withdrawn" in capsys.readouterr().out
    assert consent.needs_prompt(tmp_path / ".index", notice=consent.LITERATURE)


def test_check_reports_installed_packs(tmp_path, capsys, monkeypatch):
    from health_agent import embeddings
    from health_agent.cli import main
    from health_agent.literature import medline, packs

    # check probes Ollama on loopback; this test is about the pack line.
    monkeypatch.setattr(embeddings.OllamaEmbedder, "health_check",
                        lambda self: "stubbed")
    index = tmp_path / ".index" / "health.db"
    assert "literature packs: none installed" in _check_out(main, index, capsys)

    articles = medline.parse_articles(
        (Path(__file__).parent / "fixtures" / "literature" / "corpus.xml").read_bytes())
    pack_path = tmp_path / "sleep-1.jsonl.gz"
    packs.write_pack(pack_path, packs.CATALOG["sleep"], "1", articles, license=packs.PACK_LICENSE)
    assert main(["--index", str(index), "literature", "install", "sleep",
                 "--from", str(pack_path), "--yes", "--no-embed"]) == 0
    capsys.readouterr()
    out = _check_out(main, index, capsys)
    assert "literature packs: 1 installed (sleep@1" in out
    assert "never refreshed" in out


def test_check_joins_multiple_packs_with_semicolons(tmp_path, capsys, monkeypatch):
    """Each pack's own refresh note already starts with ', '; joining packs
    with ', ' too would read as one run-on list rather than two packs.
    `_installed_packs` orders by id, so this is install order every time,
    not whatever order sqlite happens to return."""
    from health_agent import embeddings
    from health_agent.cli import main

    monkeypatch.setattr(embeddings.OllamaEmbedder, "health_check",
                        lambda self: "stubbed")
    index = tmp_path / ".index" / "health.db"
    _install_pack(main, tmp_path, index, "sleep")
    _install_pack(main, tmp_path, index, "cardiovascular")
    capsys.readouterr()
    out = _check_out(main, index, capsys)
    assert "literature packs: 2 installed (" in out
    assert "sleep@1, never refreshed; cardiovascular@1, never refreshed" in out


def _check_out(main, index, capsys) -> str:
    main(["--index", str(index), "check"])
    return capsys.readouterr().out


def test_check_reports_no_packs_when_none_installed(tmp_path, capsys, monkeypatch):
    from health_agent import embeddings
    from health_agent.cli import main

    monkeypatch.setattr(embeddings.OllamaEmbedder, "health_check",
                        lambda self: "stubbed")
    index = tmp_path / ".index" / "health.db"
    assert "literature packs: none installed" in _check_out(main, index, capsys)


def test_check_checks_the_chat_model_separately(tmp_path, capsys, monkeypatch):
    """`ollama pull nomic-embed-text` alone passes the embedding check;
    `ask` still needs its own model, and check must say which."""
    from health_agent import cli, embeddings, ollama_client
    from health_agent.cli import main

    monkeypatch.setattr(embeddings.OllamaEmbedder, "health_check",
                        lambda self: "stubbed")
    monkeypatch.setattr(ollama_client, "list_models",
                        lambda host: ["nomic-embed-text:latest", "qwen3.5:9b"])
    monkeypatch.setattr(cli, "_physical_memory_gb", lambda: 16.0)
    monkeypatch.delenv(ollama_client.ENV_CHAT_MODEL, raising=False)
    index = tmp_path / ".index" / "health.db"

    out = _check_out(main, index, capsys)
    assert f"chat model     {ollama_client.DEFAULT_CHAT_MODEL} NOT pulled" in out
    assert f"ollama pull {ollama_client.DEFAULT_CHAT_MODEL}" in out
    assert "memory         16 GB" in out
    assert "--model qwen3.5:9b" in out

    monkeypatch.setenv(ollama_client.ENV_CHAT_MODEL, "qwen3.5:9b")
    out = _check_out(main, index, capsys)
    assert "chat model     qwen3.5:9b\n" in out
    assert "NOT pulled" not in out
    assert "--model qwen3.5:9b" not in out  # already on the lite model


def _write_sleep_pack(tmp_path, version="1"):
    return _write_pack(tmp_path, "sleep", version=version)


def _write_pack(tmp_path, slug, version="1"):
    from health_agent.literature import medline, packs
    articles = medline.parse_articles(
        (Path(__file__).parent / "fixtures" / "literature" / "corpus.xml").read_bytes())
    pack_path = tmp_path / f"{slug}-{version}.jsonl.gz"
    packs.write_pack(pack_path, packs.CATALOG[slug], version, articles,
                     license=packs.PACK_LICENSE)
    return pack_path


def _install_pack(main, tmp_path, index, slug, version="1"):
    pack_path = _write_pack(tmp_path, slug, version=version)
    assert main(["--index", str(index), "literature", "install", slug,
                 "--from", str(pack_path), "--yes", "--no-embed"]) == 0


def _refresh_stub(monkeypatch, *, added=3, retracted=1):
    """refresh_pack stand-in that writes a real refresh_log row through
    corpus.add so status and check have something to read."""
    from health_agent.literature import corpus, medline
    from health_agent.literature.fetch import refresh

    calls = []

    def fake(conn, spec, *, since=None, today=None, get=None, sleep=None, progress=None):
        calls.append((spec.slug, since))
        arts = [medline.ParsedArticle(pmid=f"9{i}", title=f"New {i}", abstract="Text.",
                                      publication_types=["Journal Article"])
                for i in range(added)]
        stats = corpus.add(conn, arts, slug=spec.slug, license="L",
                           window_from=since or "2026/09/19", window_to="2026/10/04",
                           matched=added, retracted=retracted)
        return refresh.RefreshStats("2026/09/19", "2026/10/04", added, stats.added,
                                    retracted, stats.chunks)
    monkeypatch.setattr(refresh, "refresh_pack", fake)
    return calls


def _install_sleep(main, tmp_path, index):
    pack_path = _write_sleep_pack(tmp_path)
    assert main(["--index", str(index), "literature", "install", "sleep",
                 "--from", str(pack_path), "--yes", "--no-embed"]) == 0


def test_literature_refresh_refreshes_installed_packs_and_reports(tmp_path, capsys, monkeypatch):
    from health_agent.cli import main
    index = tmp_path / ".index" / "health.db"
    _install_sleep(main, tmp_path, index)
    calls = _refresh_stub(monkeypatch)
    capsys.readouterr()

    code = main(["--index", str(index), "literature", "refresh", "--yes", "--no-embed"])
    out = capsys.readouterr().out
    assert code == 0
    assert calls == [("sleep", None)]
    assert "sleep" in out and "2026/09/19" in out and "3 added" in out and "1 retracted" in out

    code = main(["--index", str(index), "literature", "status"])
    out = capsys.readouterr().out
    assert "sleep@1" in out and "refreshed" in out and "+3" in out

    code = main(["--index", str(index), "check"])
    out = capsys.readouterr().out
    assert "sleep@1" in out and "refreshed" in out


def test_literature_refresh_sample_is_a_snapshot(tmp_path, capsys, monkeypatch):
    """Every named slug is a fixed snapshot: nothing to connect for, so the
    network notice is never shown and no consent record is written."""
    from health_agent import consent
    from health_agent.cli import main
    index = tmp_path / ".index" / "health.db"
    _install_sleep(main, tmp_path, index)
    calls = _refresh_stub(monkeypatch)
    capsys.readouterr()
    record_path = consent.consent_path(tmp_path / ".index", consent.LITERATURE)
    record_path.unlink(missing_ok=True)
    code = main(["--index", str(index), "literature", "refresh", "sample", "--yes"])
    assert code == 0
    assert "fixed snapshot" in capsys.readouterr().out
    assert calls == []
    assert not record_path.exists()


def test_literature_refresh_only_sample_installed_is_a_no_op(tmp_path, capsys, monkeypatch):
    """No topical pack installed at all: the default slug resolution is
    empty, so the command says so and exits 0 before any consent prompt."""
    from health_agent import consent
    from health_agent.cli import main
    index = tmp_path / ".index" / "health.db"
    _install_pack(main, tmp_path, index, "sample")
    calls = _refresh_stub(monkeypatch)
    capsys.readouterr()
    record_path = consent.consent_path(tmp_path / ".index", consent.LITERATURE)
    record_path.unlink(missing_ok=True)
    code = main(["--index", str(index), "literature", "refresh", "--yes"])
    out = capsys.readouterr().out
    assert code == 0
    assert "No topical packs installed" in out
    assert calls == []
    assert not record_path.exists()


def test_literature_refresh_bad_since_exits_before_consent(tmp_path, capsys, monkeypatch):
    from health_agent.cli import main
    index = tmp_path / ".index" / "health.db"
    _install_sleep(main, tmp_path, index)
    calls = _refresh_stub(monkeypatch)
    capsys.readouterr()
    code = main(["--index", str(index), "literature", "refresh", "--since", "not-a-date"])
    assert code == 2
    assert "--since must be YYYY-MM-DD" in capsys.readouterr().err
    assert calls == []


def test_literature_refresh_one_pack_failing_does_not_stop_the_others(tmp_path, capsys, monkeypatch):
    from health_agent.cli import main
    from health_agent.literature.fetch import refresh
    index = tmp_path / ".index" / "health.db"
    _install_sleep(main, tmp_path, index)
    _install_pack(main, tmp_path, index, "cardiovascular")
    calls = _refresh_stub(monkeypatch)
    real_fake = refresh.refresh_pack

    def flaky(conn, spec, **kwargs):
        if spec.slug == "cardiovascular":
            raise RuntimeError("NCBI timed out")
        return real_fake(conn, spec, **kwargs)
    monkeypatch.setattr(refresh, "refresh_pack", flaky)
    capsys.readouterr()

    code = main(["--index", str(index), "literature", "refresh", "--yes", "--no-embed"])
    out, err = capsys.readouterr()
    assert code == 2
    assert "cardiovascular" in err and "refresh failed" in err
    assert "sleep" in out and "3 added" in out
    assert calls == [("sleep", None)]  # cardiovascular raised before it could be recorded


def test_literature_refresh_rolls_back_a_failed_packs_partial_writes(
        tmp_path, capsys, monkeypatch):
    """`refresh_pack` writes the `pack` row, upserts, and a `refresh_log`
    row before its own commit. If it raises partway through, those writes
    must not survive to be swept up by the next pack's commit.

    cardiovascular is installed first so it is refreshed first and fails
    first; sleep's commit then follows. With the failing pack last, the
    pending writes would be discarded by conn.close() and the rollback
    would be untested."""
    from health_agent.cli import main
    from health_agent import config as config_mod
    from health_agent.literature import schema as lit_schema
    from health_agent.literature.fetch import refresh
    index = tmp_path / ".index" / "health.db"
    _install_pack(main, tmp_path, index, "cardiovascular")
    _install_sleep(main, tmp_path, index)
    from health_agent.literature import corpus, medline

    def flaky(conn, spec, *, since=None, today=None, get=None, sleep=None, progress=None):
        if spec.slug == "cardiovascular":
            conn.execute("UPDATE pack SET refreshed_at = 'x' WHERE slug = ?",
                        (spec.slug,))
            raise RuntimeError("NCBI timed out")
        arts = [medline.ParsedArticle(pmid="99", title="New", abstract="Text.",
                                      publication_types=["Journal Article"])]
        stats = corpus.add(conn, arts, slug=spec.slug, license="L",
                           window_from="2026/09/19", window_to="2026/10/04",
                           matched=1, retracted=0)
        conn.commit()
        return refresh.RefreshStats("2026/09/19", "2026/10/04", 1, stats.added,
                                    0, stats.chunks)
    monkeypatch.setattr(refresh, "refresh_pack", flaky)
    capsys.readouterr()

    code = main(["--index", str(index), "literature", "refresh", "--yes", "--no-embed"])
    capsys.readouterr()
    assert code == 2

    cfg = config_mod.resolve(index_path=str(index))
    conn = lit_schema.connect(cfg.literature_path)
    rows = {r["slug"]: r["refreshed_at"]
            for r in conn.execute("SELECT slug, refreshed_at FROM pack")}
    conn.close()
    assert rows["cardiovascular"] is None
    assert rows["sleep"] is not None


def test_literature_refresh_uninstalled_slug_exits_before_any_request(tmp_path, capsys, monkeypatch):
    from health_agent.cli import main
    index = tmp_path / ".index" / "health.db"
    _install_sleep(main, tmp_path, index)
    calls = _refresh_stub(monkeypatch)
    capsys.readouterr()
    code = main(["--index", str(index), "literature", "refresh", "exercise", "--yes"])
    assert code == 2
    assert "not installed" in capsys.readouterr().err
    assert calls == []


def test_literature_refresh_without_no_embed_runs_the_embed_phase(tmp_path, capsys, monkeypatch):
    """Without `--no-embed`, `refresh` embeds newly added chunks, same as
    `install`; the hashing backend needs no Ollama so this runs offline."""
    from health_agent.cli import main
    index = tmp_path / ".index" / "health.db"
    _install_sleep(main, tmp_path, index)
    _refresh_stub(monkeypatch)
    capsys.readouterr()

    code = main(["--index", str(index), "literature", "refresh", "--yes",
                 "--embed-backend", "hashing"])
    out = capsys.readouterr().out
    assert code == 0
    assert "embedded" in out


def test_literature_refresh_since_is_passed_through(tmp_path, capsys, monkeypatch):
    from health_agent.cli import main
    index = tmp_path / ".index" / "health.db"
    _install_sleep(main, tmp_path, index)
    calls = _refresh_stub(monkeypatch)
    capsys.readouterr()
    assert main(["--index", str(index), "literature", "refresh", "sleep",
                 "--since", "2026-01-01", "--yes", "--no-embed"]) == 0
    assert calls == [("sleep", "2026-01-01")]


def test_literature_refresh_asks_again_after_the_notice_changed(tmp_path, capsys, monkeypatch):
    """Consent recorded under notice version 1 does not cover refresh."""
    from health_agent import consent
    from health_agent.cli import main
    index = tmp_path / ".index" / "health.db"
    _install_sleep(main, tmp_path, index)
    _refresh_stub(monkeypatch)
    record_path = consent.consent_path(tmp_path / ".index", consent.LITERATURE)
    data = json.loads(record_path.read_text())
    data["notice_version"] = 1
    record_path.write_text(json.dumps(data))
    assert consent.needs_prompt(tmp_path / ".index", notice=consent.LITERATURE)
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    capsys.readouterr()
    assert main(["--index", str(index), "literature", "refresh", "--no-embed"]) == 1


def test_literature_install_refuses_a_file_that_names_a_different_pack(tmp_path, capsys):
    """`--from` may carry any slug (that is the local-test path), but the
    file and the argument must agree, or the user installs the wrong pack
    without being told."""
    from health_agent.cli import main
    pack_path = _write_sleep_pack(tmp_path)
    code = main(["--index", str(tmp_path / ".index" / "health.db"),
                 "literature", "install", "cardiovascular",
                 "--from", str(pack_path), "--yes", "--no-embed"])
    err = capsys.readouterr().err
    assert code == 2
    assert "sleep" in err and "cardiovascular" in err


def _stale_corpus(tmp_path, capsys):
    from health_agent.cli import main
    from health_agent import config as config_mod
    from health_agent.literature import schema as lit_schema
    index = tmp_path / ".index" / "health.db"
    pack_path = _write_sleep_pack(tmp_path)
    assert main(["--index", str(index), "literature", "install", "sleep",
                 "--from", str(pack_path), "--yes", "--no-embed"]) == 0
    capsys.readouterr()
    cfg = config_mod.resolve(index_path=str(index))
    lit = lit_schema.connect(cfg.literature_path)
    lit.execute("UPDATE corpus_meta SET value = '3' WHERE key = 'schema_version'")
    lit.commit()
    lit.close()
    return index


def test_literature_packs_names_a_stale_corpus_instead_of_saying_not_installed(
        tmp_path, capsys):
    from health_agent.cli import main
    index = _stale_corpus(tmp_path, capsys)
    code = main(["--index", str(index), "literature", "packs"])
    out, err = capsys.readouterr()
    assert code != 0
    assert "schema" in err.lower()
    assert "not installed" not in out


def test_check_reports_a_stale_corpus_as_a_problem(tmp_path, capsys, monkeypatch):
    from health_agent import embeddings
    from health_agent.cli import main
    monkeypatch.setattr(embeddings.OllamaEmbedder, "health_check",
                        lambda self: "stubbed")
    index = _stale_corpus(tmp_path, capsys)
    code = main(["--index", str(index), "check"])
    out = capsys.readouterr().out
    assert code == 1
    assert "PROBLEM" in out and "schema" in out.lower()
    assert "All good" not in out


def test_literature_install_does_not_download_what_it_already_has(
        tmp_path, capsys, monkeypatch):
    from health_agent.cli import main
    from health_agent.literature.fetch import packs as fetch_packs

    index = tmp_path / ".index" / "health.db"
    pack_path = _write_sleep_pack(tmp_path)
    assert main(["--index", str(index), "literature", "install", "sleep",
                 "--from", str(pack_path), "--yes", "--no-embed"]) == 0
    capsys.readouterr()

    calls = []
    monkeypatch.setattr(fetch_packs, "download", lambda *a, **k: calls.append(a) or None)
    code = main(["--index", str(index), "literature", "install", "sleep",
                 "--version", "1", "--yes", "--no-embed"])
    assert code == 0
    assert "already installed" in capsys.readouterr().out
    assert calls == []


def test_literature_install_removes_the_downloaded_file_afterwards(
        tmp_path, capsys, monkeypatch):
    """A pack file is 75 MB and never reused (a reinstall at the same
    version is a no-op before any fetch), so keeping it is pure cost."""
    from health_agent.cli import main
    from health_agent.literature.fetch import packs as fetch_packs

    index = tmp_path / ".index" / "health.db"
    pack_path = _write_sleep_pack(tmp_path)

    def fake_download(slug, version, dest_dir, **k):
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / pack_path.name
        shutil.copy(pack_path, dest)
        return dest

    monkeypatch.setattr(fetch_packs, "download", fake_download)
    code = main(["--index", str(index), "literature", "install", "sleep",
                 "--version", "1", "--yes", "--no-embed"])
    assert code == 0
    assert "sleep@1" in capsys.readouterr().out
    packs_dir = tmp_path / ".index" / "packs"
    assert list(packs_dir.iterdir()) == []
    # The user's own --from file is not the tool's to delete.
    assert pack_path.exists()


def _fake_download_url(pack_path):
    """A `download_url` that copies a local pack into the packs directory,
    the way the real one saves what it fetched."""
    def fake(url, dest_dir, **k):
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / pack_path.name
        shutil.copy(pack_path, dest)
        return dest
    return fake


def test_literature_install_removes_the_downloaded_file_on_a_refused_install(
        tmp_path, capsys, monkeypatch):
    """The file goes on every exit path, not only the successful one: a
    same-version `--from <url>` (refused as already installed) and a slug
    mismatch both leave the packs directory empty."""
    from health_agent.cli import main
    from health_agent.literature.fetch import packs as fetch_packs

    index = tmp_path / ".index" / "health.db"
    pack_path = _write_sleep_pack(tmp_path)
    assert main(["--index", str(index), "literature", "install", "sleep",
                 "--from", str(pack_path), "--yes", "--no-embed"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(fetch_packs, "download_url", _fake_download_url(pack_path))
    packs_dir = tmp_path / ".index" / "packs"

    code = main(["--index", str(index), "literature", "install", "sleep",
                 "--from", "https://github.com/x/sleep-1.jsonl.gz",
                 "--yes", "--no-embed"])
    assert code == 0
    assert "already installed" in capsys.readouterr().out
    assert list(packs_dir.iterdir()) == []

    code = main(["--index", str(index), "literature", "install", "cardiovascular",
                 "--from", "https://github.com/x/sleep-1.jsonl.gz",
                 "--yes", "--no-embed"])
    assert code == 2
    assert "sleep" in capsys.readouterr().err
    assert list(packs_dir.iterdir()) == []


def test_literature_install_checks_the_schema_before_any_fetch(
        tmp_path, capsys, monkeypatch):
    """`--force` skips the already-installed check, not the schema check:
    a stale corpus refuses the install either way, so the download must
    not happen first."""
    from health_agent.cli import main
    from health_agent.literature.fetch import packs as fetch_packs

    index = _stale_corpus(tmp_path, capsys)
    calls = []
    monkeypatch.setattr(fetch_packs, "download", lambda *a, **k: calls.append(a) or None)
    monkeypatch.setattr(fetch_packs, "download_url", lambda *a, **k: calls.append(a) or None)

    code = main(["--index", str(index), "literature", "install", "sleep",
                 "--yes", "--force", "--no-embed"])
    err = capsys.readouterr().err
    assert code == 2
    assert calls == []
    assert "--rebuild" in err

    code = main(["--index", str(index), "literature", "install", "sleep",
                 "--from", "https://github.com/x/sleep-1.jsonl.gz",
                 "--yes", "--no-embed"])
    assert code == 2
    assert calls == []
    assert "--rebuild" in capsys.readouterr().err


def test_literature_install_rebuild_replaces_a_stale_corpus_and_spares_the_index(
        cli, tmp_path, capsys):
    """A v3 corpus with no MEDLINE export of its own had no way forward
    before `install --rebuild`; `build --rebuild` needs an XML file."""
    from health_agent import config as config_mod
    from health_agent.literature import schema as lit_schema
    from health_agent.store import sqlite_schema

    cfg = config_mod.resolve(index_path=str(tmp_path / "health.db"))
    personal = sqlite_schema.connect(cfg.index_path)
    records_before = personal.execute("SELECT COUNT(*) AS n FROM record").fetchone()["n"]
    personal.close()
    assert records_before > 0

    pack_path = _write_sleep_pack(tmp_path)
    code, _ = cli("literature", "install", "sleep", "--from", str(pack_path),
                  "--yes", "--embed-backend", "hashing")
    assert code == 0
    lit = lit_schema.connect(cfg.literature_path)
    lit.execute("UPDATE corpus_meta SET value = '3' WHERE key = 'schema_version'")
    lit.commit()
    lit.close()

    code, _ = cli("literature", "install", "sleep", "--from", str(pack_path),
                  "--yes", "--force", "--embed-backend", "hashing")
    assert code != 0

    code, out = cli("literature", "install", "sleep", "--from", str(pack_path),
                    "--yes", "--rebuild", "--embed-backend", "hashing")
    assert code == 0
    assert "sleep@1:" in out and "already installed" not in out
    lit = lit_schema.connect(cfg.literature_path)
    assert lit_schema.read_version(lit) == lit_schema.LITERATURE_SCHEMA_VERSION
    assert lit.execute("SELECT COUNT(*) AS n FROM article").fetchone()["n"] > 0
    lit.close()

    personal = sqlite_schema.connect(cfg.index_path)
    assert personal.execute("SELECT COUNT(*) AS n FROM record").fetchone()["n"] == records_before
    personal.close()
    assert not cfg.vector_path.exists()


def test_literature_install_from_a_plain_http_url_hits_the_allow_list(tmp_path, capsys):
    from health_agent.cli import main
    code = main(["--index", str(tmp_path / ".index" / "health.db"),
                 "literature", "install", "sleep",
                 "--from", "http://github.com/x.jsonl.gz", "--yes"])
    err = capsys.readouterr().err
    assert code == 2
    assert "allow list" in err
    assert "No such file" not in err


def test_check_names_the_two_commands_that_connect(tmp_path, capsys, monkeypatch):
    from health_agent import embeddings
    from health_agent.cli import main
    monkeypatch.setattr(embeddings.OllamaEmbedder, "health_check",
                        lambda self: "stubbed")
    main(["--index", str(tmp_path / ".index" / "health.db"), "check"])
    out = capsys.readouterr().out
    assert "literature install" in out and "literature build-pack" in out
    assert "literature-consent" in out


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

    json_target = tmp_path / "sheet.json"
    code, out = cli_records("visit-prep", "--json", "--out", str(json_target))
    assert code == 0
    assert out == ""
    data = json.loads(json_target.read_text())
    assert data["prepared"]
    assert "wrote" in cli_records.err.lower()


def test_visit_prep_rejects_a_window_below_one(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--index", str(tmp_path / "health.db"), "visit-prep", "--window", "0"])
    assert exc.value.code == 2
    assert "window" in capsys.readouterr().err


def test_visit_prep_without_an_index(tmp_path, capsys):
    code = main(["--index", str(tmp_path / "none.db"), "visit-prep"])
    assert code == 2
    assert "ingest" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Clinical records (FHIR) in an Apple Health export
# --------------------------------------------------------------------------- #

def _clinical_zip(tmp_path: Path) -> Path:
    import zipfile
    target = tmp_path / "export.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("apple_health_export/export.xml",
                         '<?xml version="1.0"?><HealthData locale="en_US">'
                         '<ExportDate value="2026-10-21 09:00:00 -0400"/></HealthData>')
        for member in sorted((FIXTURES / "clinical-records").glob("*.json")):
            archive.writestr(f"apple_health_export/clinical-records/{member.name}",
                             member.read_bytes())
    return target


def test_ingest_reports_clinical_records(tmp_path, capsys):
    code = main(["--index", str(tmp_path / "h.db"), "ingest",
                 str(_clinical_zip(tmp_path)), "--no-embed"])
    out = capsys.readouterr().out
    assert code == 0
    assert "clinical records    " in out
    assert "3 labs" in out and "3 vitals" in out and "1 conditions" in out
    assert "1 unreadable" in out


def test_no_clinical_omits_the_clinical_line(tmp_path, capsys):
    code = main(["--index", str(tmp_path / "h.db"), "ingest",
                 str(_clinical_zip(tmp_path)), "--no-embed", "--no-clinical"])
    out = capsys.readouterr().out
    assert code == 0
    assert "clinical records" not in out


def test_skip_clinical_rejects_an_unknown_kind(tmp_path, capsys):
    code = main(["--index", str(tmp_path / "h.db"), "ingest",
                 str(_clinical_zip(tmp_path)), "--no-embed",
                 "--skip-clinical", "labs,bogus"])
    err = capsys.readouterr().err
    assert code == 2
    assert "bogus" in err
    for kind in fhir.CLINICAL_KINDS:
        assert kind in err


def test_skip_clinical_skips_the_named_kind(tmp_path, capsys):
    code = main(["--index", str(tmp_path / "h.db"), "ingest",
                 str(_clinical_zip(tmp_path)), "--no-embed",
                 "--skip-clinical", "labs"])
    out = capsys.readouterr().out
    assert code == 0
    assert "labs" not in out.split("clinical records", 1)[1].split("\n")[0]
    assert "skipped by --skip-clinical" in out


def test_clinical_records_ingest_when_the_export_is_unchanged(tmp_path, capsys):
    """The upgrade path: an export ingested before clinical support existed
    (or with --no-clinical) gets its clinical records on a plain re-run,
    without --force, even though HealthKit skips it as unchanged."""
    index = tmp_path / "h.db"
    zip_path = _clinical_zip(tmp_path)
    assert main(["--index", str(index), "ingest", str(zip_path),
                 "--no-embed", "--no-clinical"]) == 0
    capsys.readouterr()

    from health_agent.store import sqlite_schema
    conn = sqlite_schema.connect(index)
    try:
        assert conn.execute("SELECT COUNT(*) FROM clinical_fact").fetchone()[0] == 0
    finally:
        conn.close()

    assert main(["--index", str(index), "ingest", str(zip_path),
                 "--no-embed"]) == 0
    out = capsys.readouterr().out
    assert "already ingested and unchanged" in out
    assert "clinical records    " in out and "3 labs" in out
    conn = sqlite_schema.connect(index)
    try:
        assert conn.execute("SELECT COUNT(*) FROM clinical_fact").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM lab_result").fetchone()[0] == 3
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# compare, and workouts --detail / --period
# --------------------------------------------------------------------------- #

import datetime as _dt
import re as _re

from health_agent.store import sqlite_schema

_RHR = "HKQuantityTypeIdentifierRestingHeartRate"
_HR = "HKQuantityTypeIdentifierHeartRate"


def _make_index(path, *, facts=True, workouts=True, mixed_units=False):
    """An index built by direct inserts; export.xml is not touched."""
    conn = sqlite_schema.connect(path, create=True)
    sqlite_schema.initialize(conn)
    conn.execute("INSERT INTO source_file(id, path, kind, sha256, size_bytes, "
                 "ingested_at) VALUES(1, 'x', 'healthkit', 'h', 1, 'now')")
    n = 0
    for year, base in ((2023, 60), (2024, 55)):
        for day in range(1, 32):
            n += 1
            d = _dt.date(year, 1, day).isoformat()
            conn.execute(
                "INSERT INTO record(type, value_num, unit, source_name, "
                "start_date, start_epoch, local_date, dedup_key) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (_RHR, base + day % 4, "count/min", "Watch",
                 f"{d}T08:00:00+00:00",
                 _dt.datetime(year, 1, day, 8,
                              tzinfo=_dt.timezone.utc).timestamp(),
                 d, n.to_bytes(4, "big")))
    sqlite_schema.rebuild_daily_metrics(conn)
    if facts:
        for i, (name, onset) in enumerate((("Knee sprain", "2023-06-01"),
                                           ("Knee surgery", "2023-09-15"),
                                           ("Asthma", None))):
            cur = conn.execute(
                "INSERT INTO document(source_file_id, path, kind, extraction, "
                "ingested_at) VALUES(1, ?, 'fhir', 'text', 'now')",
                (f"clinical-records/f{i}.json",))
            conn.execute(
                "INSERT INTO clinical_fact(document_id, kind, name, onset_date,"
                " dedup_key) VALUES(?, 'condition', ?, ?, ?)",
                (cur.lastrowid, name, onset, bytes([i + 1])))
    if workouts:
        for i, d in enumerate(("2023-02-06", "2023-02-08", "2023-02-10")):
            cur = conn.execute(
                "INSERT INTO workout(activity_type, duration, duration_unit, "
                "total_distance, total_distance_unit, source_name, start_date, "
                "start_epoch, local_date, indoor, dedup_key) "
                "VALUES('HKWorkoutActivityTypeCycling', 60, 'min', 20, 'km', "
                "'Watch', ?, ?, ?, 0, ?)",
                (f"{d}T09:00:00+00:00",
                 _dt.datetime.fromisoformat(f"{d}T09:00:00+00:00").timestamp(),
                 d, bytes([10 + i])))
            unit = "bpm" if (mixed_units and i == 1) else "count/min"
            conn.execute(
                "INSERT INTO workout_stat(workout_id, kind, key, value_num, "
                "unit) VALUES(?, 'statistic', ?, ?, ?)",
                (cur.lastrowid, f"{_HR}:average", 140.0 + i, unit))
            conn.execute(
                "INSERT INTO record(type, value_num, unit, source_name, "
                "start_date, start_epoch, local_date, dedup_key) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (_HR, 150, "count/min", "Watch", f"{d}T09:10:00+00:00",
                 _dt.datetime.fromisoformat(
                     f"{d}T09:10:00+00:00").timestamp(), d,
                 bytes([50 + i])))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def workout_index(tmp_path):
    return _make_index(tmp_path / "health.db")


def _run(index, capsys, *args):
    code = main(["--index", str(index), *args])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


_LEFT = ("--left", "2023-01-01..2023-01-31")
_RIGHT = ("--right", "2024-01-01..2024-01-31")


def test_compare_prints_both_sides_and_the_verdict(workout_index, capsys):
    code, out, _ = _run(workout_index, capsys, "compare", "resting-hr",
                        *_LEFT, *_RIGHT)
    assert code == 0
    assert "Earlier" in out and "Later" in out
    assert "Not comparable" in out or "Comparable" in out
    assert "no claim" in out                      # the standing no-cause line


def test_compare_prints_no_gap_between_the_sides(workout_index, capsys):
    """Medians here are 61.5 and 56.5; their gap is the reader's business."""
    code, out, _ = _run(workout_index, capsys, "compare", "resting-hr",
                        *_LEFT, *_RIGHT)
    assert code == 0
    for banned in ("significant", "likely", "suggests", "difference", "%",
                   "ratio", "caused", "because of"):
        assert banned not in out.lower().replace("because:", "")
    assert not _re.search(r"[-+]\s?\d+(\.\d+)?\s?(bpm|count/min)", out)


def test_compare_around_an_anchor_date_names_the_anchor(workout_index, capsys):
    code, out, _ = _run(workout_index, capsys, "compare", "resting-hr",
                        "--anchor", "2023-02-18", "--before", "7",
                        "--after", "6")
    assert code == 0
    assert "2023-02-18" in out
    assert "7 day(s) before" in out and "6 day(s) after" in out


def test_compare_on_an_ambiguous_fact_lists_the_candidates_and_exits_two(
        workout_index, capsys):
    code, out, _ = _run(workout_index, capsys, "compare", "resting-hr",
                        "--anchor-fact", "knee")
    assert code == 2
    assert "2023-06-01" in out and "2023-09-15" in out
    assert "Knee sprain" in out and "Knee surgery" in out
    assert "median" not in out                    # nothing was chosen or run


def test_compare_on_a_unique_fact_prints_its_citation(workout_index, capsys):
    code, out, _ = _run(workout_index, capsys, "compare", "resting-hr",
                        "--anchor-fact", "surgery", "--before", "7",
                        "--after", "7")
    assert code == 0
    assert "2023-09-15" in out and "clinical-records/f1.json" in out


def test_compare_names_three_different_anchor_absences(tmp_path, capsys):
    plain = _make_index(tmp_path / "a.db", facts=False)
    code, out, _ = _run(plain, capsys, "compare", "resting-hr",
                        "--anchor-fact", "knee")
    assert code == 2 and "no clinical facts at all" in out

    held = _make_index(tmp_path / "b.db")
    code, out2, _ = _run(held, capsys, "compare", "resting-hr",
                         "--anchor-fact", "migraine")
    assert code == 2 and "No clinical fact is named like" in out2
    assert "3 condition" in out2 and "2023-06-01" in out2

    code, out3, _ = _run(held, capsys, "compare", "resting-hr",
                         "--anchor-fact", "asthma")
    assert code == 2 and "none carries a date" in out3
    assert len({out, out2, out3}) == 3


def test_compare_json_carries_the_whole_structure(workout_index, capsys):
    code, out, _ = _run(workout_index, capsys, "compare", "resting-hr",
                        *_LEFT, *_RIGHT, "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["comparable"]["verdict"]
    assert payload["left"]["n"] == 31 and payload["right"]["n"] == 31
    assert len(payload["left_points"]) == 31
    assert payload["left"]["coverage"]["total_records"] == 62
    assert "no claim" in payload["note"]
    text = json.dumps(payload).lower()
    for banned in ("difference", "delta", "percent", "ratio", "change"):
        assert banned not in text


def test_compare_json_for_an_anchor_carries_the_anchor(workout_index, capsys):
    code, out, _ = _run(workout_index, capsys, "compare", "resting-hr",
                        "--anchor-fact", "surgery", "--json")
    payload = json.loads(out)
    assert payload["anchor"]["date"] == "2023-09-15"
    assert payload["anchor"]["citation"] == "clinical-records/f1.json"
    assert payload["left_label"] == "before"


def test_compare_an_empty_side_reports_what_the_index_holds(workout_index,
                                                            capsys):
    code, out, _ = _run(workout_index, capsys, "compare", "resting-hr",
                        "--left", "2020-01-01..2020-01-31", *_RIGHT)
    assert code == 0
    assert "insufficient_data" in out
    assert "62 records" in out and "2023-01-01" in out


def test_compare_a_workout_figure_names_the_workouts_held_when_a_side_is_empty(
        workout_index, capsys):
    code, out, _ = _run(workout_index, capsys, "compare", "--activity",
                        "swim", "--left", "2023-01-01..2023-03-01",
                        "--right", "2023-04-01..2023-05-01")
    assert code == 0
    assert "Cycling (3 workouts)" in " ".join(out.split())
    assert "whatever activity" not in out


def test_compare_a_measure_nothing_carries_lists_what_is_held(workout_index,
                                                              capsys):
    code, out, _ = _run(workout_index, capsys, "compare", "--measure",
                        f"{_HR}:maximum", *_LEFT, *_RIGHT)
    assert code == 1
    assert f"No workout carries {_HR}:maximum." in out
    assert f"{_HR}:average" in out


def test_compare_refuses_mixed_units_and_prints_the_refusal(tmp_path, capsys):
    index = _make_index(tmp_path / "m.db", mixed_units=True)
    code, out, _ = _run(index, capsys, "compare", "--measure", f"{_HR}:average",
                        "--period", "month",
                        "--left", "2023-02-01..2023-02-28",
                        "--right", "2023-02-01..2023-02-28")
    assert code == 0
    assert "more than one unit" in out
    assert "unit_mixed_on_one_side" in out


@pytest.mark.parametrize("argv, fragment", [
    (["compare", "resting-hr"], "--left and"),
    (["compare", *_LEFT, *_RIGHT], "name a metric"),
    (["compare", "resting-hr", "--anchor", "2023-02-18", *_LEFT, *_RIGHT],
     "not both"),
    (["compare", "resting-hr", *_LEFT, *_RIGHT, "--before", "7"],
     "around an anchor"),
    (["compare", "nonsense-metric", *_LEFT, *_RIGHT], "unknown metric"),
    (["compare", "resting-hr", "--anchor", "last tuesday"], "error"),
])
def test_compare_usage_errors_exit_two_on_stderr(workout_index, capsys, argv,
                                                 fragment):
    code, out, err = _run(workout_index, capsys, *argv)
    assert code == 2
    assert fragment in err and not out


def test_compare_rejects_a_backwards_range(workout_index, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--index", str(workout_index), "compare", "resting-hr",
              "--left", "2023-02-01..2023-01-01", *_RIGHT])
    assert exc.value.code == 2


def test_workouts_default_output_is_unchanged(workout_index, capsys):
    code, out, _ = _run(workout_index, capsys, "workouts")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["activity", "n", "duration", "distance",
                                "energy", "first", "last"]
    assert "Cycling" in lines[2] and "180" in lines[2]
    assert len(lines) == 3


def test_workouts_with_a_period_prints_one_row_per_period(workout_index,
                                                          capsys):
    code, out, _ = _run(workout_index, capsys, "workouts", "--period", "week",
                        "--measure", f"{_HR}:average")
    assert code == 0
    assert out.splitlines()[0].startswith("week")
    assert "2023-02-06" in out and "HeartRate:average" in out
    assert "median over the workouts" in out


def test_workouts_detail_prints_one_row_per_workout_with_citations(
        workout_index, capsys):
    code, out, _ = _run(workout_index, capsys, "workouts", "--detail",
                        "--measure", f"{_HR}:average", "--derive", "pace",
                        "--samples")
    assert code == 0
    assert out.count("Cycling on 2023-02-0") + out.count(
        "Cycling on 2023-02-10") >= 6
    assert "142.0 count/min" in out and "outdoor" in out
    assert "min/km" in out
    assert "both ends included" in out


def test_workouts_detail_with_a_missing_measure_names_what_is_held(
        workout_index, capsys):
    code, out, _ = _run(workout_index, capsys, "workouts", "--detail",
                        "--measure", "HKQuantityTypeIdentifierVO2Max:average")
    assert code == 1
    assert "No workout carries" in out
    assert "HKQuantityTypeIdentifierHeartRate:average" in out


def test_workouts_with_no_workouts_in_the_index_says_so_and_nothing_more(
        tmp_path, capsys):
    index = _make_index(tmp_path / "n.db", workouts=False)
    code, out, _ = _run(index, capsys, "workouts", "--detail")
    assert code == 1 and "holds no workouts at all" in out


def test_workouts_filtered_to_nothing_says_which_filter_did_it(workout_index,
                                                               capsys):
    code, out, _ = _run(workout_index, capsys, "workouts", "--detail",
                        "--activity", "swim")
    assert code == 1
    assert "Taking off --activity alone would leave some" in out
    assert "Cycling 3" in out

    code, out2, _ = _run(workout_index, capsys, "workouts", "--period", "week",
                         "--indoor")
    assert code == 1 and "--indoor/--outdoor" in out2
    assert out != out2


def test_workouts_flags_without_a_listing_are_refused(workout_index, capsys):
    code, out, err = _run(workout_index, capsys, "workouts", "--measure",
                          f"{_HR}:average")
    assert code == 2 and "need --detail or --period" in err
    code, out, err = _run(workout_index, capsys, "workouts", "--detail",
                          "--samples")
    assert code == 2 and "statistic" in err
    code, out, err = _run(workout_index, capsys, "workouts", "--detail",
                          "--period", "week")
    assert code == 2


def test_workouts_series_with_mixed_units_prints_the_refusal(tmp_path, capsys):
    index = _make_index(tmp_path / "m.db", mixed_units=True)
    code, out, _ = _run(index, capsys, "workouts", "--period", "month",
                        "--measure", f"{_HR}:average")
    assert code == 0
    assert "mixed units" in out and "bpm" in out


def test_workouts_json_carries_the_citation_and_sample_depth(workout_index,
                                                             capsys):
    code, out, _ = _run(workout_index, capsys, "workouts", "--detail",
                        "--measure", f"{_HR}:average", "--samples", "--json")
    payload = json.loads(out)
    row = payload["workouts"][0]
    assert row["citation"] == "HKWorkoutActivityTypeCycling on 2023-02-10"
    assert row["sample_depth"][_HR]["count"] == 1


def test_workouts_tables_stay_within_118_columns(tmp_path, capsys):
    """A long activity, source and measure key must clip, not widen the row."""
    index = _make_index(tmp_path / "w.db")
    conn = sqlite_schema.connect(index)
    long_key = "HKQuantityTypeIdentifierAppleSleepingWristTemperature:average"
    conn.execute("UPDATE workout SET activity_type = "
                 "'HKWorkoutActivityTypeFunctionalStrengthTrainingExtra', "
                 "source_name = ?", ("A Very Long Source Name " * 3,))
    conn.execute("UPDATE workout_stat SET key = ?", (long_key,))
    conn.commit()
    conn.close()
    widest = 0
    for argv in (["workouts", "--detail", "--measure", long_key, "--derive",
                  "pace", "--samples"],
                 ["workouts", "--period", "week", "--measure", long_key],
                 ["workouts", "--detail", "--measure", "Missing:average"]):
        _, out, _ = _run(index, capsys, *argv)
        widest = max([widest, *(len(l) for l in out.splitlines())])
    assert widest <= 118, widest


def test_stats_reports_how_many_workouts_carry_statistics(workout_index,
                                                          capsys):
    _, out, _ = _run(workout_index, capsys, "stats")
    assert "3 of 3 carry statistics, 0 carry metadata" in out
    _, out, _ = _run(workout_index, capsys, "stats", "--json")
    assert json.loads(out)["workout_detail"]["with_statistics"] == 3


def test_check_reports_workout_measure_coverage(workout_index, capsys,
                                                monkeypatch):
    monkeypatch.setattr("health_agent.embeddings.OllamaEmbedder.health_check",
                        lambda self: "ok", raising=False)
    _, out, _ = _run(workout_index, capsys, "check")
    line = " ".join(out.split("workout measures", 1)[1].split("\n\n")[0]
                    .split("literature packs")[0].split())
    assert "1 distinct" in line and "HeartRate:average (3)" in line


def test_check_says_a_schema_5_index_was_migrated_by_the_check(workout_index,
                                                               capsys,
                                                               monkeypatch):
    monkeypatch.setattr("health_agent.embeddings.OllamaEmbedder.health_check",
                        lambda self: "ok", raising=False)
    conn = sqlite_schema.connect(workout_index)
    conn.execute("UPDATE schema_meta SET value = '5' WHERE key = "
                 "'schema_version'")
    conn.commit()
    conn.close()
    _, out, _ = _run(workout_index, capsys, "check")
    assert "written at schema 5" in out and "migrated" in out


def test_compare_prose_stays_within_118_columns(workout_index, capsys):
    _, out, _ = _run(workout_index, capsys, "compare", "resting-hr",
                     "--anchor-fact", "surgery", "--before", "7",
                     "--after", "7")
    assert max(len(l) for l in out.splitlines()) <= 118


def test_compare_refuses_a_workout_only_flag_on_a_metric(workout_index, capsys):
    for flag in ("--indoor", "--outdoor", "--workouts"):
        code, out, err = _run(workout_index, capsys, "compare", "resting-hr",
                              flag, *_LEFT, *_RIGHT)
        assert code == 2 and not out
        assert flag in err and "applies only to a workout figure" in err


def test_an_indoor_miss_says_how_the_flag_splits(workout_index, capsys):
    """All three workouts are flagged outdoor, so --indoor finds none."""
    code, out, _ = _run(workout_index, capsys, "workouts", "--detail",
                        "--indoor")
    assert code == 1
    assert ("0 are flagged indoor, 3 outdoor, and 0 carry no indoor flag"
            in " ".join(out.split()))
    code, out, _ = _run(workout_index, capsys, "compare", "--workouts",
                        "--indoor", "--left", "2023-02-01..2023-02-28",
                        "--right", "2023-03-01..2023-03-31")
    assert "3 outdoor" in " ".join(out.split())


def test_compare_prints_the_per_period_table_capped(workout_index, capsys):
    _, out, _ = _run(workout_index, capsys, "compare", "resting-hr",
                     *_LEFT, *_RIGHT)
    assert "Earlier, by period:" in out and "Later, by period:" in out
    assert "2023-01-01" in out and "2023-01-31" in out
    assert "2023-01-15" not in out                      # elided
    assert "31 periods in all; the middle 21 are not shown" in out
    assert "--json carries them whole" in out
    assert max(len(l) for l in out.splitlines()) <= 118
    _, out, _ = _run(workout_index, capsys, "compare", "resting-hr",
                     "--left", "2023-01-01..2023-01-10",
                     "--right", "2024-01-01..2024-01-10")
    assert "2023-01-05" in out and "not shown" not in out


def test_compare_anchor_day_is_said_once(workout_index, capsys):
    _, out, _ = _run(workout_index, capsys, "compare", "resting-hr",
                     "--anchor", "2023-01-15", "--before", "7", "--after", "7")
    assert out.count("is in neither side") == 1 and "own day" not in out


# --------------------------------------------------------------------------- #
# A metric whose rows name no unit
# --------------------------------------------------------------------------- #

def _no_unit_index(path):
    """An index holding one metric whose rows carry no unit at all."""
    from health_agent.store import sqlite_schema

    conn = sqlite_schema.connect(path, create=True)
    sqlite_schema.initialize(conn)
    for date in ("2026-03-01", "2026-03-02"):
        conn.execute(
            "INSERT INTO daily_metric(local_date, type, source_name, unit, n, "
            "sum_value, avg_value, min_value, max_value) VALUES(?, "
            "'HKQuantityTypeIdentifierRestingHeartRate', 'Watch', NULL, 1, "
            "60, 60, 60, 60)", (date,))
    conn.commit()
    conn.close()
    return path


def test_the_title_does_not_print_the_unlabelled_sentinel_as_a_unit(tmp_path, capsys):
    """`queries` reports an absent unit as "unlabelled" rather than borrowing a
    neighbour's, which is honest. Printing it in the title is not: "avg per day
    (unlabelled)" reads as a unit somebody measured in."""
    code, out, _ = _run(_no_unit_index(tmp_path / "health.db"), capsys,
                        "metric", "resting-hr")
    assert code == 0
    assert "unlabelled" not in out
    assert "(none)" not in out
    assert "avg per day" in out and "60" in out
    assert "name no unit" in out


def _rides_for_depth(path):
    """Two thin rides in January, two well-sampled ones in March."""
    from health_agent.store import sqlite_schema

    hr = "HKQuantityTypeIdentifierHeartRate"
    conn = sqlite_schema.connect(path, create=True)
    sqlite_schema.initialize(conn)
    plan = [("2023-01-05", 1672585200.0, 2, 10, b"\x01"),
            ("2023-01-12", 1673190000.0, 4, 20, b"\x02"),
            ("2023-03-05", 1677672000.0, 20, 30, b"\x03"),
            ("2023-03-12", 1678276800.0, 24, 60, b"\x04")]
    for date, epoch, samples, key_base, key in plan:
        cur = conn.execute(
            "INSERT INTO workout(activity_type, duration, duration_unit, "
            "total_distance, total_distance_unit, source_name, start_date, "
            "start_epoch, local_date, dedup_key) VALUES("
            "'HKWorkoutActivityTypeCycling', 30.0, 'min', 10.0, 'mi', "
            "'Watch', ?, ?, ?, ?)",
            (f"{date}T09:00:00-05:00", epoch, date, key))
        conn.execute(
            "INSERT INTO workout_stat(workout_id, kind, key, value_num, unit) "
            "VALUES(?, 'statistic', ?, 140.0, 'count/min')",
            (cur.lastrowid, hr + ":average"))
        for i in range(samples):
            conn.execute(
                "INSERT INTO record(type, value_num, unit, start_date, "
                "start_epoch, local_date, dedup_key) VALUES(?, 60, "
                "'count/min', ?, ?, ?, ?)",
                (hr, f"{date}T09:00:00-05:00", epoch + 60.0 * (i + 1), date,
                 bytes([key_base, i])))
    conn.commit()
    # The CLI refuses an index that holds records with no daily rollup, which
    # is a real guard and not something a fixture should route around.
    sqlite_schema.rebuild_daily_metrics(conn)
    conn.commit()
    conn.close()
    return path


_DEPTH_COMPARE = ("compare", "--workouts", "--activity", "Cycling",
                  "--measure", "HKQuantityTypeIdentifierHeartRate:average",
                  "--left", "2023-01-01..2023-01-31",
                  "--right", "2023-03-01..2023-03-31", "--period", "week")


def test_compare_samples_prints_the_depth_behind_each_side(tmp_path, capsys):
    """The §6 comparability check, from the command line as well as the tool."""
    index = _rides_for_depth(tmp_path / "health.db")
    code, out, _ = _run(index, capsys, *_DEPTH_COMPARE, "--samples")
    assert code == 0
    # Joined, because the printed line wraps at the terminal width.
    flat = " ".join(out.split())
    assert "median of 3 heart rate records inside each workout" in flat
    assert "median of 22 heart rate records inside each workout" in flat


def test_compare_without_samples_says_nothing_about_depth(tmp_path, capsys):
    index = _rides_for_depth(tmp_path / "health.db")
    code, out, _ = _run(index, capsys, *_DEPTH_COMPARE)
    assert code == 0
    assert "inside each workout" not in out


def test_compare_samples_json_carries_the_depth(tmp_path, capsys):
    index = _rides_for_depth(tmp_path / "health.db")
    code, out, _ = _run(index, capsys, *_DEPTH_COMPARE, "--samples", "--json")
    assert code == 0
    payload = json.loads(out)
    hr = "HKQuantityTypeIdentifierHeartRate"
    assert payload["left"]["sample_depth"][hr]["median"] == 3.0
    assert payload["comparable"]["right_sample_depth"][hr]["median"] == 22.0
