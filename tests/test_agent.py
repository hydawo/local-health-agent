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
