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
    from health_agent.literature import medline, packs
    articles = medline.parse_articles(
        (Path(__file__).parent / "fixtures" / "literature" / "corpus.xml").read_bytes())
    pack_path = tmp_path / f"sleep-{version}.jsonl.gz"
    packs.write_pack(pack_path, packs.CATALOG["sleep"], version, articles,
                     license=packs.PACK_LICENSE)
    return pack_path


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
    from health_agent.cli import main
    index = tmp_path / ".index" / "health.db"
    _install_sleep(main, tmp_path, index)
    calls = _refresh_stub(monkeypatch)
    capsys.readouterr()
    code = main(["--index", str(index), "literature", "refresh", "sample", "--yes"])
    assert code == 0
    assert "fixed snapshot" in capsys.readouterr().out
    assert calls == []


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
