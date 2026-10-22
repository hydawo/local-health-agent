"""Agent tool-contract and orchestration tests.

The loop is tested against a scripted fake model rather than a live one: an LLM
is nondeterministic, slow, and unavailable in CI, and none of that is what these
tests are about. They check that the loop feeds results back, stops correctly,
records provenance, and survives a model behaving badly. Whether the model gives
*good* answers is what the eval set measures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from health_agent import ollama_client
from health_agent.agent import orchestrator, tools
from health_agent.ingest import healthkit, notes, records
from health_agent.store import sqlite_schema

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("agent")
    conn = sqlite_schema.connect(tmp / "health.db", create=True)
    sqlite_schema.initialize(conn)

    def register(path, kind):
        return healthkit.register_source_file(
            conn, path, kind, healthkit.sha256_file(path))

    healthkit.ingest_file(conn, FIXTURES / "export.xml")
    sqlite_schema.rebuild_daily_metrics(conn)
    # OCR enabled so the scanned report contributes; degrades to skipping that
    # one file when Tesseract is absent, which the OCR-specific test allows for.
    records.ingest_records(conn, FIXTURES / "records",
                           register=lambda p: register(p, "record"),
                           use_ocr=True)
    notes.ingest_notes(conn, FIXTURES / "notes",
                       register=lambda p: register(p, "note"))
    yield tools.ToolContext(conn=conn, vector_path=tmp / "vectors")
    conn.close()


# --------------------------------------------------------------------------- #
# Tool contract
# --------------------------------------------------------------------------- #

def test_schemas_are_well_formed():
    for schema in tools.schemas():
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["name"] and function["description"]
        parameters = function["parameters"]
        assert parameters["type"] == "object"
        assert parameters["properties"]
        # `required` is optional: get_lab_trend accepts either `analyte` or
        # `analytes`, which JSON Schema's `required` cannot express as an
        # either/or. Its handler validates instead.
        assert parameters.get("required", ["ok"])


def test_lab_trend_requires_one_of_analyte_or_analytes(ctx):
    assert "error" in tools.dispatch(ctx, "get_lab_trend", {})
    assert "error" not in tools.dispatch(ctx, "get_lab_trend", {"analyte": "ldl"})
    assert "error" not in tools.dispatch(ctx, "get_lab_trend",
                                         {"analytes": ["ldl"]})


def test_batched_lab_lookup_precomputes_out_of_range(ctx):
    """One call instead of one per analyte. Measured before this existed: a
    "what's abnormal?" question took 23 tool calls and 358 seconds."""
    result = tools.dispatch(ctx, "get_lab_trend", {
        "analytes": ["ldl", "hdl", "vitamin_d", "tsh", "glucose"]})

    assert set(result["analytes"]) == {"ldl", "hdl", "vitamin_d", "tsh", "glucose"}
    flagged = {f["analyte"] for f in result["out_of_range_on_latest_report"]}
    assert flagged == {"ldl", "vitamin_d"}
    assert all(f["citation"] for f in result["out_of_range_on_latest_report"])


def test_batched_lookup_reports_analytes_with_no_results(ctx):
    result = tools.dispatch(ctx, "get_lab_trend",
                            {"analytes": ["ldl", "psa"]})
    assert result["no_results_for"] == ["psa"]
    assert "ldl" in result["analytes"]


def test_batched_lookup_accepts_a_comma_separated_string(ctx):
    """Models sometimes send an array parameter as a string."""
    result = tools.dispatch(ctx, "get_lab_trend", {"analytes": "ldl, hdl"})
    assert set(result["analytes"]) == {"ldl", "hdl"}


def test_batched_lookup_caps_the_request(ctx):
    result = tools.dispatch(ctx, "get_lab_trend",
                            {"analytes": [f"made_up_{i}" for i in range(60)]})
    assert "truncated_request" in result


def test_query_healthkit_returns_a_precomputed_overall(ctx):
    """The model must never have to average anything itself. Before `overall`
    existed, qwen3.6 summed seven daily values by hand and returned 59.6 where
    the answer is exactly 60.0."""
    result = tools.dispatch(ctx, "query_healthkit",
                            {"metric": "resting-hr", "start_date": "2026-03-01",
                             "end_date": "2026-03-31"})
    assert result["overall"]["value"] == 60.0
    assert result["overall"]["aggregation"] == "avg"
    assert result["overall"]["total_samples"] == 7


def test_overall_is_sample_weighted_for_averages(ctx):
    """A day with many samples must not weigh the same as a day with few."""
    result = tools.dispatch(ctx, "query_healthkit",
                            {"metric": "resting-hr", "period": "week"})
    # Weeks hold 1 and 6 samples; the naive mean of the two weekly means differs
    # from the correct sample-weighted answer.
    assert result["overall"]["value"] == 60.0


def test_overall_sums_cumulative_metrics(ctx):
    result = tools.dispatch(ctx, "query_healthkit", {"metric": "steps"})
    # 6000 (best single source on 03-02) + 8500 on 03-03
    assert result["overall"]["value"] == 14500.0
    assert result["overall"]["aggregation"] == "sum"


def test_query_healthkit_warns_about_multiple_devices(ctx):
    result = tools.dispatch(ctx, "query_healthkit",
                            {"metric": "steps", "start_date": "2026-03-02",
                             "end_date": "2026-03-02"})
    assert result["points"][0]["value"] == 6000.0
    assert "multi_source_warning" in result
    assert "double-counts" in result["multi_source_warning"]


def test_missing_data_returns_coverage_not_emptiness(ctx):
    """Absence is data. Without the coverage fields a model will reach for the
    nearest numbers it can see and present them as an answer."""
    result = tools.dispatch(ctx, "query_healthkit",
                            {"metric": "resting-hr", "start_date": "2026-06-01",
                             "end_date": "2026-06-30"})
    assert result["points"] == []
    assert result["no_data_in_range"] is True
    assert result["available_to"] == "2026-03-07"
    assert "do not answer with data from a different period" in result["note"]


def test_metric_absent_entirely_says_so(ctx):
    result = tools.dispatch(ctx, "query_healthkit", {"metric": "vo2max"})
    assert result["total_records_for_metric"] == 0
    assert "not substitute a related metric" in result["note"]


def test_get_lab_trend_carries_citations_and_ranges(ctx):
    result = tools.dispatch(ctx, "get_lab_trend", {"analyte": "ldl"})
    points = result["points"]
    assert [p["value"] for p in points] == [128.0, 112.0]
    assert all(p["citation"] for p in points)
    assert points[-1]["out_of_range"] is True
    assert points[-1]["reference_range"] == "0-99"


@pytest.mark.skipif(not records.ocr_available(),
                    reason="Tesseract is not installed; no OCR values to flag")
def test_get_lab_trend_flags_ocr_values(ctx):
    result = tools.dispatch(ctx, "get_lab_trend", {"analyte": "hba1c"})
    assert result["points"][0].get("via_ocr") is True
    assert "ocr_warning" in result
    assert "checked against the original" in result["ocr_warning"]


def test_unknown_analyte_lists_what_exists(ctx):
    result = tools.dispatch(ctx, "get_lab_trend", {"analyte": "psa"})
    assert result["no_data"] is True
    assert "hba1c" in result["analytes_present_in_reports"]
    assert "Do not infer a value from a different analyte" in result["note"]


def test_sleep_is_attributed_to_nights_not_calendar_dates(ctx):
    """The eval set caught this: asked for deep sleep on the night of the 4th,
    the agent answered "none recorded" because the deep and REM segments start
    after midnight and land on the 5th."""
    result = tools.dispatch(ctx, "query_healthkit", {"metric": "sleep"})
    nights = {n["night_of"]: n for n in result["nights"]}

    assert "2026-03-04" in nights
    night = nights["2026-03-04"]
    assert night["stages_hours"]["AsleepDeep"] == 0.75      # 45 minutes
    assert night["stages_hours"]["AsleepREM"] == 0.75
    assert night["stages_hours"]["AsleepCore"] == 2.0
    assert night["in_bed_hours"] == 0.5
    assert night["asleep_hours"] == 3.5
    # The whole night is one entry; nothing spills onto the 5th.
    assert "2026-03-05" not in nights


def test_sleep_night_range_filter(ctx):
    result = tools.dispatch(ctx, "query_healthkit",
                            {"metric": "sleep", "start_date": "2026-03-04",
                             "end_date": "2026-03-04"})
    assert [n["night_of"] for n in result["nights"]] == ["2026-03-04"]
    assert "attribution" in result


def test_sleep_with_no_data_explains_night_labelling(ctx):
    result = tools.dispatch(ctx, "query_healthkit",
                            {"metric": "sleep", "start_date": "2026-05-01",
                             "end_date": "2026-05-31"})
    assert result["no_data_in_range"] is True
    assert result["available_from"]
    assert "after midnight" in result["note"]


def test_workouts_are_reachable(ctx):
    """Also from the eval set: the agent answered "no workouts logged" while
    the index held a run, because no tool could see the workout table."""
    result = tools.dispatch(ctx, "get_workouts", {})
    assert len(result["workouts"]) == 1
    session = result["workouts"][0]
    assert session["activity"] == "Running"
    assert session["date"] == "2026-03-06"
    assert session["distance"] == 5.0
    assert result["totals_by_activity"][0]["count"] == 1


def test_workouts_outside_the_range_report_coverage(ctx):
    result = tools.dispatch(ctx, "get_workouts",
                            {"start_date": "2025-01-01", "end_date": "2025-12-31"})
    assert result["no_data_in_range"] is True
    assert result["total_workouts"] == 1
    assert result["available_from"] == "2026-03-06"
    assert "rather than inferring activity from step counts" in result["note"]


def test_search_records_returns_citations(ctx):
    result = tools.dispatch(ctx, "search_records", {"query": "vitamin d"})
    assert result["results"]
    assert all(r["citation"] for r in result["results"])
    assert all(len(r["text"]) <= tools.MAX_SNIPPET_CHARS
               for r in result["results"])


def test_search_records_can_restrict_to_notes(ctx):
    result = tools.dispatch(ctx, "search_records",
                            {"query": "coffee", "kind": "note"})
    assert all(r["kind"] == "note" for r in result["results"])


def test_search_with_no_match_says_so(ctx):
    result = tools.dispatch(ctx, "search_records", {"query": "zzzqqqxyz"})
    assert result["no_matches"] is True
    assert "rather than answering from general knowledge" in result["note"]


def test_search_result_count_is_capped(ctx):
    result = tools.dispatch(ctx, "search_records",
                            {"query": "cholesterol", "limit": 999})
    assert len(result["results"]) <= tools.MAX_SEARCH_RESULTS


def test_unknown_tool_is_reported_not_raised(ctx):
    result = tools.dispatch(ctx, "no_such_tool", {})
    assert "error" in result
    assert "query_healthkit" in result["available_tools"]


def test_tool_errors_come_back_as_data(ctx):
    """A raising tool would end the conversation; a tool that returns an error
    lets the model correct its arguments."""
    assert "error" in tools.dispatch(ctx, "get_lab_trend", {})
    assert "error" in tools.dispatch(ctx, "query_healthkit", {"metric": ""})


def test_data_inventory_describes_the_index(ctx):
    inventory = tools.data_inventory(ctx.conn)
    assert inventory["healthkit"]["records"] == 25
    assert "resting-hr" in inventory["healthkit"]["metrics_available"]
    assert "hba1c" in inventory["labs"]["analytes_available"]
    assert "followup" in inventory["notes"]["tags"]


# --------------------------------------------------------------------------- #
# The loop, against a scripted model
# --------------------------------------------------------------------------- #

class FakeModel:
    """Replays a scripted list of assistant messages, recording what it saw."""

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.calls: list[list[dict]] = []

    def __call__(self, host, model, messages, **kwargs):
        self.calls.append(list(messages))
        message = self.script.pop(0) if self.script else {"content": "done"}
        return {"message": {"role": "assistant", **message}}


def make(ctx, script, **kwargs):
    return orchestrator.Orchestrator(ctx, **kwargs), FakeModel(script)


def test_single_tool_round_trip(ctx, monkeypatch):
    orch, fake = make(ctx, [
        {"content": "", "tool_calls": [{"function": {
            "name": "get_lab_trend", "arguments": {"analyte": "ldl"}}}]},
        {"content": "Your LDL was 112 mg/dL."},
    ])
    monkeypatch.setattr(ollama_client, "chat", fake)

    answer = orch.ask("What is my LDL?")
    assert answer.text.startswith("Your LDL was 112 mg/dL.")
    assert answer.tools_used == ["get_lab_trend"]
    assert answer.steps[0].result["points"][-1]["value"] == 112.0
    # A lab answer carries the standing disclaimer (plan §5).
    assert answer.guardrail.disclaimer_added
    assert not answer.guardrail.flags


def test_tool_results_are_fed_back_to_the_model(ctx, monkeypatch):
    """The loop is iterative for a measured reason: asked to compare two
    sources, qwen3.6 requests one tool, then the other only after seeing the
    first result. A single-shot design answers with half the data."""
    orch, fake = make(ctx, [
        {"content": "", "tool_calls": [{"function": {
            "name": "get_lab_trend", "arguments": {"analyte": "ldl"}}}]},
        {"content": "", "tool_calls": [{"function": {
            "name": "query_healthkit", "arguments": {"metric": "steps"}}}]},
        {"content": "Both retrieved."},
    ])
    monkeypatch.setattr(ollama_client, "chat", fake)

    answer = orch.ask("Compare my LDL with my steps.")
    assert answer.tools_used == ["get_lab_trend", "query_healthkit"]

    # The second model turn must have seen the first tool's output.
    second_turn = fake.calls[1]
    tool_messages = [m for m in second_turn if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "112" in tool_messages[0]["content"]

    # ...and the third turn must have seen both.
    assert len([m for m in fake.calls[2] if m.get("role") == "tool"]) == 2


def test_answer_without_any_tool_call(ctx, monkeypatch):
    orch, fake = make(ctx, [{"content": "Hello."}])
    monkeypatch.setattr(ollama_client, "chat", fake)
    answer = orch.ask("Say hello.")
    assert answer.text == "Hello."
    assert answer.steps == []


def test_step_limit_forces_a_final_answer(ctx, monkeypatch):
    """A model that keeps calling tools forever must still yield an answer."""
    keeps_calling = [{"content": "", "tool_calls": [{"function": {
        "name": "get_lab_trend", "arguments": {"analyte": "ldl"}}}]}] * 3
    # The 4th response is the forcing turn, made with no tools offered, so a
    # real model answers with content rather than another tool call.
    orch, fake = make(ctx, keeps_calling + [{"content": "Partial answer."}],
                      max_steps=3)
    monkeypatch.setattr(ollama_client, "chat", fake)

    answer = orch.ask("loop forever")
    assert answer.hit_step_limit is True
    assert len(answer.steps) == 3
    assert answer.text.startswith("Partial answer.")
    # The forcing turn must offer no tools, or the model would keep calling them.
    assert "Answer now using only" in fake.calls[-1][-1]["content"]


def test_string_encoded_arguments_are_parsed(ctx, monkeypatch):
    """Some models emit arguments as a JSON string rather than an object."""
    orch, fake = make(ctx, [
        {"content": "", "tool_calls": [{"function": {
            "name": "get_lab_trend",
            "arguments": json.dumps({"analyte": "ldl"})}}]},
        {"content": "ok"},
    ])
    monkeypatch.setattr(ollama_client, "chat", fake)
    answer = orch.ask("q")
    assert answer.steps[0].arguments == {"analyte": "ldl"}
    assert not answer.steps[0].failed


def test_a_failing_tool_does_not_end_the_conversation(ctx, monkeypatch):
    orch, fake = make(ctx, [
        {"content": "", "tool_calls": [{"function": {
            "name": "nonexistent", "arguments": {}}}]},
        {"content": "I could not look that up."},
    ])
    monkeypatch.setattr(ollama_client, "chat", fake)
    answer = orch.ask("q")
    assert answer.steps[0].failed
    assert answer.text == "I could not look that up."


def test_empty_model_response_gets_a_fallback(ctx, monkeypatch):
    orch, fake = make(ctx, [{"content": "   "}])
    monkeypatch.setattr(ollama_client, "chat", fake)
    answer = orch.ask("q")
    assert "could not produce an answer" in answer.text


def test_provenance_is_recorded_for_every_call(ctx, monkeypatch):
    """Per-answer provenance (plan §10 item 3) in its cheapest useful form: it
    costs nothing while the loop runs and cannot be reconstructed after."""
    orch, fake = make(ctx, [
        {"content": "", "tool_calls": [{"function": {
            "name": "get_lab_trend", "arguments": {"analyte": "hba1c"}}}]},
        {"content": "answer"},
    ])
    monkeypatch.setattr(ollama_client, "chat", fake)
    answer = orch.ask("q")

    step = answer.steps[0]
    assert step.step == 1
    assert step.arguments == {"analyte": "hba1c"}
    assert step.elapsed_sec >= 0
    assert any("labs_2026-03-10.pdf" in c for c in answer.citations)


def test_guardrail_rewrites_a_diagnostic_answer(ctx, monkeypatch):
    """End to end through the loop: the model diagnoses, the guard catches it,
    asks for a restatement, and the clean version is what the user sees."""
    orch, fake = make(ctx, [
        {"content": "", "tool_calls": [{"function": {
            "name": "get_lab_trend", "arguments": {"analyte": "hba1c"}}}]},
        {"content": "You have prediabetes and should take supplements."},
        {"content": "Your HbA1c was 5.4% on 2026-03-10, within the 4.8-5.6 range."},
    ])
    monkeypatch.setattr(ollama_client, "chat", fake)

    answer = orch.ask("What does my HbA1c mean?")
    assert answer.guardrail.rewritten
    assert not answer.guardrail.blocked
    assert "prediabetes" not in answer.text
    assert "5.4%" in answer.text


def test_guardrail_can_be_skipped_for_measurement(ctx, monkeypatch):
    """The eval runner needs to see raw model output to score it honestly."""
    orch, fake = make(ctx, [{"content": "You have prediabetes."}],
                      skip_guardrail=True)
    monkeypatch.setattr(ollama_client, "chat", fake)
    answer = orch.ask("q")
    assert answer.text == "You have prediabetes."
    assert answer.guardrail is None


def test_thinking_traces_are_captured(ctx, monkeypatch):
    orch, fake = make(ctx, [{"content": "answer", "thinking": "some reasoning"}],
                      think=True)
    monkeypatch.setattr(ollama_client, "chat", fake)
    answer = orch.ask("q")
    assert answer.thinking == ["some reasoning"]


def test_system_prompt_carries_the_data_inventory(ctx):
    orch = orchestrator.Orchestrator(ctx)
    prompt = orch.system_prompt()
    assert "resting-hr" in prompt
    assert "hba1c" in prompt
    assert "Anything not listed above is not available" in prompt


def test_system_prompt_states_the_hard_constraints(ctx):
    """These lines are the guardrail's first layer; the output check is M7."""
    prompt = orchestrator.SYSTEM_PROMPT
    assert "Do not diagnose" in prompt
    assert "Do not do arithmetic" in prompt
    assert "Never convert units" in prompt
    assert "cite" in prompt.lower()
    # Added after an adversarial probe: asked "do I have prediabetes?", the
    # model correctly declined to diagnose but volunteered the A1c 5.7-6.4%
    # threshold from its training data — an uncited clinical claim, which is
    # what the literature-grounding roadmap item exists to replace.
    assert "clinical thresholds" in prompt


def test_system_prompt_says_clinical_history_is_not_the_whole_medication_list(ctx):
    """Eval run 8 regressed Q27. Given the question "given my medications, is
    my LDL trend something to worry about", the model called
    get_clinical_history, found only ibuprofen, never searched the note that
    records the statin, and told the person they take no lipid-lowering
    medication. The clinical tool had displaced the notes search, and the
    model read a health system's feed as the whole truth about what the
    person takes."""
    prompt = orchestrator.SYSTEM_PROMPT
    assert "not the whole of what this person takes" in prompt
    assert "search their notes in the same turn" in prompt
    # The first wording was satisfiable by saying less: the model stopped
    # making the false claim, scoped it to "recorded by your health systems",
    # and offered to check the notes instead of checking them.
    assert "check them, then answer" in prompt


def test_progress_events_are_emitted(ctx, monkeypatch):
    events = []
    orch, fake = make(ctx, [
        {"content": "", "tool_calls": [{"function": {
            "name": "get_lab_trend", "arguments": {"analyte": "ldl"}}}]},
        {"content": "ok"},
    ], on_event=lambda e, d: events.append((e, d)))
    monkeypatch.setattr(ollama_client, "chat", fake)
    orch.ask("q")

    kinds = [e for e, _ in events]
    assert "tool_call" in kinds and "tool_result" in kinds


def test_remote_host_is_refused_at_construction(ctx, monkeypatch):
    monkeypatch.delenv(ollama_client.ENV_ALLOW_REMOTE, raising=False)
    with pytest.raises(ollama_client.RemoteHostRefused):
        orchestrator.Orchestrator(ctx, host="http://192.168.1.9:11434")


def test_returned_pmids_collects_findings_across_steps_and_ignores_misses():
    # The brief names these `Answer`/`Step`; this codebase's dataclasses are
    # `Answer`/`ToolCallRecord`, so the calls below use the real names.
    from health_agent.agent.orchestrator import Answer, Orchestrator, ToolCallRecord

    answer = Answer(text="", steps=[
        ToolCallRecord(step=1, name="search_medical_literature", arguments={},
                       result={"findings": [{"pmid": "42613609"},
                                            {"pmid": "42609254"}]},
                       elapsed_sec=0.1),
        ToolCallRecord(step=2, name="get_lab_trend", arguments={},
                       result={"analytes": []}, elapsed_sec=0.1),
        ToolCallRecord(step=3, name="search_medical_literature", arguments={},
                       result={"findings": [], "no_matches": True},
                       elapsed_sec=0.1),
        ToolCallRecord(step=4, name="search_medical_literature", arguments={},
                       result={"findings": [{"pmid": "40000001"}]},
                       elapsed_sec=0.1),
    ])
    assert Orchestrator._returned_pmids(answer) == frozenset(
        {"42613609", "42609254", "40000001"})
    assert Orchestrator._returned_pmids(Answer(text="", steps=[])) == frozenset()


def test_local_backend_honours_the_model_environment_variable(monkeypatch):
    """The README has promised `$HEALTH_AGENT_MODEL` since `--model` existed;
    until this test it was defined and never read."""
    from health_agent import ollama_client
    from health_agent.agent import backends

    monkeypatch.setenv(ollama_client.ENV_CHAT_MODEL, "qwen3.5:9b")
    assert backends.LocalBackend().model == "qwen3.5:9b"
    assert backends.LocalBackend(model="qwen3.6:27b").model == "qwen3.6:27b"
    monkeypatch.delenv(ollama_client.ENV_CHAT_MODEL)
    assert backends.LocalBackend().model == ollama_client.DEFAULT_CHAT_MODEL


# --------------------------------------------------------------------------- #
# Literature context block (task 2)
# --------------------------------------------------------------------------- #

def _lit_ctx(ctx, tmp_path, literature_fixture):
    from health_agent.literature import corpus, medline, schema
    lit = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(lit)
    corpus.build(lit, medline.parse_articles(literature_fixture.read_bytes()),
                 slug="sample", version="1", license="L")
    return tools.ToolContext(conn=ctx.conn, vector_path=ctx.vector_path,
                             literature_conn=lit,
                             literature_vector_path=tmp_path / "lv")


_LDL_CALL = {"content": "", "tool_calls": [{"function": {
    "name": "get_lab_trend", "arguments": {"analyte": "ldl"}}}]}


def test_flagged_lab_answer_gets_a_literature_block_after_the_model(ctx, tmp_path,
                                                                    literature_fixture, monkeypatch):
    orch, fake = make(_lit_ctx(ctx, tmp_path, literature_fixture),
                      [_LDL_CALL, {"content": "Your LDL was 112 mg/dL, flagged H."}])
    monkeypatch.setattr(ollama_client, "chat", fake)
    answer = orch.ask("What is my LDL?")
    assert [c.analyte for c in answer.literature_context] == ["ldl"]
    assert answer.literature_context[0].findings
    assert answer.literature_context_text.startswith("---\nPublished research")
    assert "PMID" in answer.literature_context_text
    # never in the transcript the model saw
    assert not any("Published research" in str(m) for call in fake.calls for m in call)
    # and not in the answer text the guard judged
    assert "Published research" not in answer.text


def test_no_block_when_the_model_searched_the_literature_itself(ctx, tmp_path,
                                                               literature_fixture, monkeypatch):
    orch, fake = make(_lit_ctx(ctx, tmp_path, literature_fixture), [
        _LDL_CALL,
        {"content": "", "tool_calls": [{"function": {
            "name": "search_medical_literature", "arguments": {"query": "LDL cholesterol"}}}]},
        {"content": "Your LDL was 112 mg/dL (PMID 40000001)."}])
    monkeypatch.setattr(ollama_client, "chat", fake)
    answer = orch.ask("What does research say about my LDL?")
    assert answer.literature_context == [] and answer.literature_context_text == ""


def test_no_block_without_a_corpus_or_when_disabled(ctx, tmp_path, literature_fixture, monkeypatch):
    orch, fake = make(ctx, [_LDL_CALL, {"content": "Your LDL was 112 mg/dL."}])
    monkeypatch.setattr(ollama_client, "chat", fake)
    assert orch.ask("LDL?").literature_context == []
    orch, fake = make(_lit_ctx(ctx, tmp_path, literature_fixture),
                      [_LDL_CALL, {"content": "Your LDL was 112 mg/dL."}],
                      literature_context=False)
    monkeypatch.setattr(ollama_client, "chat", fake)
    assert orch.ask("LDL?").literature_context == []


def test_literature_context_failure_does_not_break_the_answer(ctx, tmp_path,
                                                              literature_fixture, monkeypatch):
    """A broken corpus (a stale index, a locked file, anything else `lab_context`
    or `render` can raise on) must not take the whole answer down with it. The
    answer text and tool steps the model already produced are worth more than
    a block the person never asked for."""
    orch, fake = make(_lit_ctx(ctx, tmp_path, literature_fixture),
                      [_LDL_CALL, {"content": "Your LDL was 112 mg/dL, flagged H."}])
    monkeypatch.setattr(ollama_client, "chat", fake)

    def _raise(*args, **kwargs):
        raise RuntimeError("corpus index is corrupt")

    monkeypatch.setattr(orchestrator.context_module, "lab_context", _raise)
    answer = orch.ask("What is my LDL?")
    assert answer.text.startswith("Your LDL was 112 mg/dL")
    assert answer.literature_context == []
    assert answer.literature_context_text == ""


def test_system_prompt_tells_the_model_the_tool_appends_findings(ctx):
    text = orchestrator.Orchestrator(ctx).system_prompt()
    assert "the tool itself appends published findings" in text


# --------------------------------------------------------------------------- #
# Clinical records (FHIR)
# --------------------------------------------------------------------------- #

@pytest.fixture
def clinical_ctx(tmp_path):
    """A fresh index holding the committed clinical fixtures.

    Separate from the module `ctx`, which must stay free of clinical records so
    the absence tests mean something."""
    from health_agent.ingest import fhir
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    import shutil
    root = tmp_path / "apple_health_export"
    shutil.copytree(FIXTURES / "clinical-records", root / "clinical-records")
    marker = next((root / "clinical-records").glob("*.json"))
    sid, _ = healthkit.register_source_file(conn, marker, "healthkit",
                                            healthkit.sha256_file(marker))
    fhir.ingest_clinical(conn, root, source_file_id=sid)
    conn.commit()
    yield tools.ToolContext(conn=conn, vector_path=tmp_path / "vectors")
    conn.close()


def test_clinical_history_returns_facts_with_citations(clinical_ctx):
    result = tools.dispatch(clinical_ctx, "get_clinical_history",
                            {"kind": "medications"})
    assert result["kinds_available"]
    (row,) = [r for r in result["facts"] if r["name"].startswith("ibuprofen")]
    assert row["detail_text"].startswith("Take 1 tablet")
    assert row["citation"].startswith("clinical-records/")
    assert "not medical advice" not in str(result)   # tools return data, not prose


def test_clinical_history_without_a_kind_returns_everything(clinical_ctx):
    result = tools.dispatch(clinical_ctx, "get_clinical_history", {})
    assert {r["kind"] for r in result["facts"]} >= {"condition", "medication"}


def test_clinical_history_rejects_an_unknown_kind(clinical_ctx):
    assert "error" in tools.dispatch(clinical_ctx, "get_clinical_history",
                                     {"kind": "moods"})


def test_clinical_history_reports_absence_as_coverage(ctx):
    """`ctx` has no clinical records at all."""
    result = tools.dispatch(ctx, "get_clinical_history", {"kind": "conditions"})
    assert result["facts"] == []
    assert result["no_clinical_records"] is True
    assert "clinical-records" in result["note"]


def test_clinical_vitals_are_separate_from_healthkit(clinical_ctx):
    result = tools.dispatch(clinical_ctx, "get_clinical_vitals",
                            {"metric": "pulse"})
    assert result["measurements"][0]["value"] == 72
    assert result["measurements"][0]["unit"] == "bpm"
    assert result["measurements"][0]["citation"].startswith("clinical-records/")
    assert "query_healthkit" in result["note"]      # names the other tool


def test_clinical_vitals_without_a_metric_lists_what_exists(clinical_ctx):
    result = tools.dispatch(clinical_ctx, "get_clinical_vitals", {})
    assert {m["metric_key"] for m in result["metrics_available"]} >= {"pulse", "spo2"}


def test_clinical_vitals_report_absence_as_coverage(ctx):
    result = tools.dispatch(ctx, "get_clinical_vitals", {})
    assert result["measurements"] == [] and result["no_clinical_records"] is True


def test_both_tools_are_registered_and_schemas_are_well_formed():
    assert "get_clinical_history" in tools.BY_NAME
    assert "get_clinical_vitals" in tools.BY_NAME
    assert len(tools.TOOLS) == 8
    for schema in tools.schemas():
        assert schema["function"]["description"]
        assert schema["function"]["parameters"]["type"] == "object"


def test_data_inventory_names_the_clinical_surface(clinical_ctx):
    inventory = tools.data_inventory(clinical_ctx.conn)
    assert inventory["clinical"]["conditions"] == 1
    assert inventory["clinical"]["medications"] == 1
    assert "pulse" in inventory["clinical"]["vital_metrics_available"]


def test_the_prompt_separates_clinic_measurements_from_wearable_data(ctx):
    text = orchestrator.Orchestrator(ctx).system_prompt()
    assert "clinician-measured" in text.lower()
    assert "get_clinical_vitals" in text


@pytest.fixture
def two_source_ctx(tmp_path):
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    marker = tmp_path / "marker.txt"
    marker.write_text("x")
    sid, _ = healthkit.register_source_file(conn, marker, "healthkit",
                                            healthkit.sha256_file(marker))
    ids = []
    for kind, path in (("pdf", "labs.pdf"), ("fhir", "clinical-records/a.json")):
        ids.append(conn.execute(
            "INSERT INTO document(source_file_id, path, kind, page_count, "
            "extraction, ingested_at) VALUES(?,?,?,1,'text','2026-10-21')",
            (sid, path, kind)).lastrowid)

    def add(doc, value, unit="mg/dL", extracted_by="line"):
        conn.execute(
            "INSERT INTO lab_result(document_id, page_no, analyte, analyte_key, "
            "value_num, unit, collected_date, raw_line, extracted_by, via_ocr, "
            "dedup_key) VALUES(?,1,'LDL','ldl',?,?,'2026-03-10','x',?,0,?)",
            (doc, value, unit, extracted_by, f"{doc}{value}{unit}".encode()))

    yield tools.ToolContext(conn=conn, vector_path=tmp_path / "v"), ids, add
    conn.close()


def test_a_draw_reported_by_both_sources_is_one_point_with_its_source(two_source_ctx):
    ctx_, (pdf_doc, fhir_doc), add = two_source_ctx
    add(pdf_doc, 112.0)
    add(fhir_doc, 112.0, extracted_by="fhir")
    result = tools.dispatch(ctx_, "get_lab_trend", {"analyte": "ldl"})
    (point,) = result["points"]
    assert point["source"] == "pdf"   # the row carrying the range wins; see also_from
    assert "source_disagreement" not in result


def test_conflicting_sources_produce_a_note_naming_both_values(two_source_ctx):
    ctx_, (pdf_doc, fhir_doc), add = two_source_ctx
    add(pdf_doc, 112.0)
    add(fhir_doc, 5.4, unit="mmol/L", extracted_by="fhir")
    result = tools.dispatch(ctx_, "get_lab_trend", {"analyte": "ldl"})
    assert {p["source"] for p in result["points"]} == {"pdf", "fhir"}
    note = result["source_disagreement"]
    assert "2026-03-10" in note
    assert "112" in note and "mg/dL" in note and "pdf" in note
    assert "5.4" in note and "mmol/L" in note and "fhir" in note


def test_a_since_that_filters_history_to_empty_names_the_plural_kinds(clinical_ctx):
    result = tools.dispatch(clinical_ctx, "get_clinical_history",
                            {"since": "2099-01-01"})
    assert result["facts"] == []
    assert "no_clinical_records" not in result
    assert "conditions" in result["kinds_available"]
    assert "condition," not in result["note"] and "conditions" in result["note"]


def test_a_date_only_vitals_miss_names_what_exists(clinical_ctx):
    result = tools.dispatch(clinical_ctx, "get_clinical_vitals",
                            {"start_date": "2099-01-01"})
    assert result["measurements"] == []
    assert "No clinician-measured vitals" in result["note"]
    assert "pulse" in result["note"]


@pytest.fixture
def labs_only_ctx(tmp_path):
    """An index holding FHIR labs and note references and no facts or vitals.

    The shape C1 was reproduced on: a health system that shared labs and
    notes but no problem list. Nothing on this path is a switch the person
    has to have set."""
    from health_agent.ingest import fhir
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    import shutil
    root = tmp_path / "apple_health_export"
    shutil.copytree(FIXTURES / "clinical-records", root / "clinical-records")
    marker = next((root / "clinical-records").glob("*.json"))
    sid, _ = healthkit.register_source_file(conn, marker, "healthkit",
                                            healthkit.sha256_file(marker))
    fhir.ingest_clinical(
        conn, root, source_file_id=sid,
        skip=frozenset({"vitals", "conditions", "medications", "allergies",
                        "immunizations", "procedures"}))
    conn.commit()
    yield tools.ToolContext(conn=conn, vector_path=tmp_path / "vectors")
    conn.close()


def test_clinical_history_with_labs_but_no_facts_does_not_claim_emptiness(
        labs_only_ctx):
    result = tools.dispatch(labs_only_ctx, "get_clinical_history", {})
    assert result["facts"] == []
    assert "no_clinical_records" not in result
    assert result["clinical_records_held"]["lab values"] >= 1
    assert "lab values" in result["note"]
    assert "holds no clinical records" not in result["note"]


def test_clinical_vitals_with_labs_but_no_vitals_does_not_claim_emptiness(
        labs_only_ctx):
    result = tools.dispatch(labs_only_ctx, "get_clinical_vitals", {})
    assert result["measurements"] == []
    assert "no_clinical_records" not in result
    assert result["clinical_records_held"]["lab values"] >= 1
    assert "lab values" in result["note"]


def test_data_inventory_lists_labs_and_notes_when_no_facts_exist(labs_only_ctx):
    clinical = tools.data_inventory(labs_only_ctx.conn)["clinical"]
    assert clinical["lab_values"] >= 1
    assert clinical["note_references"] >= 1
    assert clinical["date_range"][0] and clinical["date_range"][1]


def test_vitals_truncation_is_reported_at_the_limit_that_actually_ran(tmp_path):
    """More readings than the default limit: the flag has to fire, or the
    model describes a slice of the record as the record."""
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    marker = tmp_path / "marker.txt"
    marker.write_text("x")
    sid, _ = healthkit.register_source_file(conn, marker, "healthkit",
                                            healthkit.sha256_file(marker))
    doc = conn.execute(
        "INSERT INTO document(source_file_id, path, kind, page_count, "
        "extraction, ingested_at) VALUES(?,'clinical-records/v.json','fhir',"
        "0,'none','2026-10-21')", (sid,)).lastrowid
    for i in range(150):
        day = f"2026-01-{i % 28 + 1:02d}"
        conn.execute(
            "INSERT INTO clinical_vital(document_id, metric, metric_key, "
            "value_num, unit, measured_at, local_date, dedup_key) "
            "VALUES(?,'Pulse','pulse',?,'bpm',?,?,?)",
            (doc, 60 + i % 20, f"{day}T09:0{i % 10}:00", day, f"v{i}".encode()))
    conn.commit()
    ctx_ = tools.ToolContext(conn=conn, vector_path=tmp_path / "v")

    result = tools.dispatch(ctx_, "get_clinical_vitals", {})
    assert len(result["measurements"]) == 100
    assert result["truncated"] is True
    assert "150" in result["truncation_note"]

    narrow = tools.dispatch(ctx_, "get_clinical_vitals", {"limit": 10})
    assert narrow["truncated"] is True
    whole = tools.dispatch(ctx_, "get_clinical_vitals", {"limit": 200})
    assert len(whole["measurements"]) == 150
    assert "truncated" not in whole
    conn.close()


def test_a_metric_miss_names_the_metric(clinical_ctx):
    result = tools.dispatch(clinical_ctx, "get_clinical_vitals",
                            {"metric": "pulse", "start_date": "2099-01-01"})
    assert "clinician-measured pulse" in result["note"]


def test_vitals_absence_distinguishes_no_vitals_from_no_records(tmp_path, clinical_ctx):
    conn = clinical_ctx.conn
    conn.execute("DELETE FROM clinical_vital")
    result = tools.dispatch(clinical_ctx, "get_clinical_vitals", {})
    assert "no_clinical_records" not in result
    assert "but no clinician-measured vitals" in result["note"]


def test_disagreement_note_skips_dates_not_in_the_payload():
    d = ({"collected_date": "2020-01-01",
          "reported": [{"value": 1.0, "unit": "x", "sources": ["pdf"]},
                       {"value": 2.0, "unit": "x", "sources": ["fhir"]}]},)
    assert tools._source_disagreement_note(d, {"2021-01-01"}) is None
    assert "2020-01-01" in tools._source_disagreement_note(d, {"2020-01-01"})


# --------------------------------------------------------------------------- #
# compare_windows and get_workouts detail
# --------------------------------------------------------------------------- #

_RHR = "HKQuantityTypeIdentifierRestingHeartRate"
_HR = "HKQuantityTypeIdentifierHeartRate"


@pytest.fixture
def workoutbox(tmp_path):
    """An index built by direct inserts; tests/fixtures/export.xml is untouched."""
    import datetime as dt

    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    conn.execute("INSERT INTO source_file(id, path, kind, sha256, size_bytes, "
                 "ingested_at) VALUES(1, 'x', 'healthkit', 'h', 1, 'now')")
    n = 0
    for year, base in ((2023, 60), (2024, 55)):
        for day in range(1, 32):
            n += 1
            date = dt.date(year, 1, day).isoformat()
            epoch = dt.datetime(year, 1, day, 8,
                                tzinfo=dt.timezone.utc).timestamp()
            conn.execute(
                "INSERT INTO record(type, value_num, unit, source_name, "
                "start_date, start_epoch, local_date, dedup_key) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (_RHR, base + day % 4, "count/min", "Watch",
                 f"{date}T08:00:00+00:00", epoch, date, n.to_bytes(4, "big")))
    sqlite_schema.rebuild_daily_metrics(conn)

    for i, (name, onset) in enumerate((("Knee sprain", "2023-06-01"),
                                       ("Knee surgery", "2023-09-15"))):
        cur = conn.execute(
            "INSERT INTO document(source_file_id, path, kind, extraction, "
            "ingested_at) VALUES(1, ?, 'fhir', 'text', 'now')",
            (f"clinical-records/knee{i}.json",))
        conn.execute(
            "INSERT INTO clinical_fact(document_id, kind, name, onset_date, "
            "dedup_key) VALUES(?, 'condition', ?, ?, ?)",
            (cur.lastrowid, name, onset, bytes([i + 1])))

    for i, date in enumerate(("2023-02-06", "2023-02-08", "2023-02-10")):
        cur = conn.execute(
            "INSERT INTO workout(activity_type, duration, duration_unit, "
            "total_distance, total_distance_unit, source_name, start_date, "
            "start_epoch, local_date, dedup_key) "
            "VALUES('HKWorkoutActivityTypeCycling', 60, 'min', 20, 'km', "
            "'Watch', ?, ?, ?, ?)",
            (f"{date}T09:00:00+00:00",
             dt.datetime.fromisoformat(f"{date}T09:00:00+00:00").timestamp(),
             date, bytes([10 + i])))
        conn.execute(
            "INSERT INTO workout_stat(workout_id, kind, key, value_num, unit) "
            "VALUES(?, 'statistic', ?, ?, 'count/min')",
            (cur.lastrowid, f"{_HR}:average", 140.0 + i))
    conn.commit()
    yield tools.ToolContext(conn=conn, vector_path=tmp_path / "v")
    conn.close()


_RANGES = {"metric": "resting-hr",
           "left_start": "2023-01-01", "left_end": "2023-01-31",
           "right_start": "2024-01-01", "right_end": "2024-01-31"}


def test_compare_windows_returns_both_sides_and_the_verdict(workoutbox):
    result = tools.dispatch(workoutbox, "compare_windows", _RANGES)
    assert result["left"]["periods"] == 31 and result["right"]["periods"] == 31
    assert result["comparable"]["verdict"] in windows_verdicts()
    assert "no claim" in result["note"]
    # An empty-handed side reports coverage, not an empty record.
    assert result["left"]["coverage"]["total_records"] == 62


def windows_verdicts():
    from health_agent.store import windows
    return windows.VERDICTS


def test_compare_windows_carries_no_difference_between_the_sides(workoutbox):
    flat = json.dumps(tools.dispatch(workoutbox, "compare_windows", _RANGES)).lower()
    for forbidden in ("difference", "delta", "percent", "pct_change", "ratio",
                      "significant", "likely", "suggests", "caused"):
        assert forbidden not in flat


def test_compare_windows_anchored_uses_the_windows_own_note_and_labels(workoutbox):
    result = tools.dispatch(workoutbox, "compare_windows", {
        "metric": "resting-hr", "anchor_date": "2023-01-16",
        "before_days": 7, "after_days": 7})
    assert result["anchor"]["date"] == "2023-01-16"
    assert result["left_label"] == "before"
    assert result["left"]["periods"] == 7 and result["right"]["periods"] == 7
    assert "no claim" in result["note"]
    assert "samples" not in result["left"]


def test_compare_windows_sample_counts_only_when_asked(workoutbox):
    result = tools.dispatch(workoutbox, "compare_windows",
                            {**_RANGES, "sample_counts": True})
    assert result["left"]["samples"] == 31
    assert result["comparable"]["left_samples"] == 31


def test_an_empty_side_names_what_the_index_holds(workoutbox):
    result = tools.dispatch(workoutbox, "compare_windows", {
        **_RANGES, "right_start": "2025-01-01", "right_end": "2025-01-31"})
    assert result["comparable"]["verdict"] == "insufficient_data"
    assert result["right"]["periods"] == 0
    assert result["right"]["coverage"]["available_to"] == "2024-01-31"
    assert "62 records" in result["comparable"]["because"]


def test_compare_windows_reports_an_ambiguous_anchor_instead_of_choosing(workoutbox):
    result = tools.dispatch(workoutbox, "compare_windows", {
        "metric": "resting-hr", "anchor_fact": "knee",
        "before_days": 7, "after_days": 7})
    assert result["anchor_ambiguous"] is True
    assert {c["date"] for c in result["candidates"]} == {"2023-06-01",
                                                         "2023-09-15"}
    assert "which" in result["note"].lower()
    assert "left" not in result


def test_an_unmatched_anchor_fact_is_not_found_and_names_what_is_held(workoutbox):
    result = tools.dispatch(workoutbox, "compare_windows", {
        "metric": "resting-hr", "anchor_fact": "asthma"})
    assert result["error"] == "anchor_not_found"
    assert "condition" in result["clinical_kinds_held"]


def test_compare_windows_with_no_anchor_and_no_ranges_says_what_it_needs(workoutbox):
    result = tools.dispatch(workoutbox, "compare_windows",
                            {"metric": "resting-hr"})
    assert result["error"]
    assert "anchor_date" in result["note"] and "left_start" in result["note"]


def test_compare_windows_refuses_an_anchor_and_ranges_together(workoutbox):
    result = tools.dispatch(workoutbox, "compare_windows",
                            {**_RANGES, "anchor_date": "2023-01-16"})
    assert result["error"] == "two_ways_to_name_windows"


def test_compare_windows_layer_refusals_come_back_as_the_reason(workoutbox):
    result = tools.dispatch(workoutbox, "compare_windows", {
        **_RANGES, "metric": "resting-hrr"})
    assert result["error"] == "invalid_request"
    assert "unknown metric" in result["note"]


def test_compare_windows_over_a_workout_measure(workoutbox):
    result = tools.dispatch(workoutbox, "compare_windows", {
        "activity": "Cycling", "measure": f"{_HR}:average", "period": "week",
        "left_start": "2023-02-01", "left_end": "2023-02-14",
        "right_start": "2023-03-01", "right_end": "2023-03-14"})
    assert result["left"]["median"] == 141.0
    assert result["right"]["periods"] == 0
    assert result["right"]["coverage"]["total_records"] == 3


def test_compare_windows_lists_every_point_under_the_cap(workoutbox):
    result = tools.dispatch(workoutbox, "compare_windows", {
        "metric": "resting-hr",
        "left_start": "2023-01-01", "left_end": "2023-01-31",
        "right_start": "2024-01-01", "right_end": "2024-01-31"})
    assert "points_truncated" not in result      # 31 <= 60
    assert len(result["left_points"]) == 31


def test_get_workouts_detail_carries_measures_and_derivations(workoutbox):
    result = tools.dispatch(workoutbox, "get_workouts", {
        "detail": True, "measures": [f"{_HR}:average"],
        "derive": ["pace"], "limit": 5})
    first = result["workouts"][0]
    assert first["measures"][f"{_HR}:average"]["value"] == 142.0
    assert first["derived"]["pace"]["value"] is not None
    assert first["citation"]
    assert "sample_depth" not in first
    assert "note" not in result


def test_get_workouts_detail_samples_carry_sample_depth(workoutbox):
    result = tools.dispatch(workoutbox, "get_workouts", {
        "detail": True, "measures": [f"{_HR}:average"], "samples": True})
    depth = result["workouts"][0]["sample_depth"][_HR]
    assert depth["count"] == 0 and depth["bounds"] == "inclusive"


def test_get_workouts_detail_by_period_groups_workouts(workoutbox):
    result = tools.dispatch(workoutbox, "get_workouts", {
        "detail": True, "period": "week", "measures": [f"{_HR}:average"]})
    (point,) = result["points"]
    assert point["workouts"] == 3
    assert point["measures"][f"{_HR}:average"]["median"] == 141.0


def test_get_workouts_detail_ratio_without_its_pair_is_refused_in_words(workoutbox):
    result = tools.dispatch(workoutbox, "get_workouts", {
        "detail": True, "period": "week", "measures": [f"{_HR}:average"],
        "ratio_of": [f"{_HR}:average", "x"]})
    assert result["error"] == "invalid_request"
    assert "ratio" in result["note"]


def test_get_workouts_without_detail_keeps_its_old_shape(workoutbox):
    """No question that worked before this change may change its answer."""
    result = tools.dispatch(workoutbox, "get_workouts", {})
    assert set(result) == {"workouts", "totals_by_activity"}
    assert set(result["workouts"][0]) == {
        "date", "activity", "duration", "duration_unit", "distance",
        "distance_unit", "energy", "energy_unit", "source"}
    ignored = tools.dispatch(workoutbox, "get_workouts",
                             {"measures": [f"{_HR}:average"], "period": "week"})
    assert ignored == result


def test_get_workouts_names_the_measures_it_holds_when_one_is_missing(workoutbox):
    result = tools.dispatch(workoutbox, "get_workouts", {
        "detail": True,
        "measures": ["HKQuantityTypeIdentifierVO2Max:average"]})
    assert [m["key"] for m in result["measures_available"]] == [
        f"{_HR}:average"]
    assert "no workout carries" in result["note"].lower()
    assert result["workouts"]               # the workouts still come back


def test_detail_with_no_workouts_in_range_keeps_the_coverage_shape(workoutbox):
    result = tools.dispatch(workoutbox, "get_workouts", {
        "detail": True, "measures": [f"{_HR}:average"],
        "start_date": "2020-01-01", "end_date": "2020-12-31"})
    assert result["no_data_in_range"] is True       # the old empty shape
    result = tools.dispatch(workoutbox, "get_workouts", {
        "detail": True, "period": "week", "measures": [f"{_HR}:average"],
        "indoor": True})
    assert result["no_data_in_range"] is True


def test_the_index_with_no_workout_measures_says_so(workoutbox):
    workoutbox.conn.execute("DELETE FROM workout_stat")
    workoutbox.conn.commit()
    result = tools.dispatch(workoutbox, "get_workouts", {
        "detail": True, "measures": [f"{_HR}:average"]})
    assert result["measures_available"] == []
    assert "no workout measures of any kind" in result["note"]


def test_the_system_prompt_forbids_a_causal_reading_of_two_windows():
    prompt = orchestrator.SYSTEM_PROMPT
    assert "two sets of numbers from two spans of time" in prompt
    assert "never compute the difference between the two sides yourself" in prompt
    rule = prompt[prompt.index("1f."):prompt.index("1a.")]
    assert "\u2014" not in rule


def test_the_tool_count_is_eight():
    """One new tool, not one per data shape. Later sub-projects add sources and
    add no tools."""
    assert len(tools.TOOLS) == 8
    assert {t.name for t in tools.TOOLS} >= {
        "compare_windows", "get_workouts", "get_clinical_history"}
