"""Cloud tier: consent gate, wire translation, and tier provenance.

No test here makes a network call. The Anthropic SDK is stubbed, which is the
right level for what needs proving: that consent is taken before anything could
be sent, that the neutral transcript translates to Anthropic's message shape
correctly, and that a refusal is handled rather than read as an answer.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from health_agent import consent
from health_agent.agent import backends, orchestrator, tools
from health_agent.ingest import healthkit, notes, records
from health_agent.store import sqlite_schema

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- #
# Consent
# --------------------------------------------------------------------------- #

def test_no_consent_recorded_initially(tmp_path):
    assert consent.load(tmp_path) is None
    assert consent.needs_prompt(tmp_path) is True


def test_recording_and_reading_consent(tmp_path):
    entry = consent.record(tmp_path, "claude-opus-5")
    assert entry.model == "claude-opus-5"
    assert entry.is_current()
    assert consent.needs_prompt(tmp_path) is False

    reloaded = consent.load(tmp_path)
    assert reloaded.granted_at == entry.granted_at
    assert reloaded.model == "claude-opus-5"


def test_consent_is_scoped_to_a_data_folder(tmp_path):
    """Someone keeping separate folders for different people consents per
    folder — the right default when the thing sent is somebody's health data."""
    first, second = tmp_path / "a", tmp_path / "b"
    consent.record(first, "claude-opus-5")
    assert consent.needs_prompt(first) is False
    assert consent.needs_prompt(second) is True


def test_a_changed_notice_invalidates_old_consent(tmp_path, monkeypatch):
    """Consent is to a specific disclosure. If what gets sent changes, the old
    'yes' answered a different question."""
    consent.record(tmp_path, "claude-opus-5")
    assert consent.needs_prompt(tmp_path) is False

    monkeypatch.setattr(consent, "NOTICE_VERSION", consent.NOTICE_VERSION + 1)
    assert consent.needs_prompt(tmp_path) is True


def test_corrupt_consent_file_fails_closed(tmp_path):
    """An unreadable record must mean 'ask again', never 'assume yes'."""
    consent.consent_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    consent.consent_path(tmp_path).write_text("{ not json")
    assert consent.load(tmp_path) is None
    assert consent.needs_prompt(tmp_path) is True


def test_revoking_consent(tmp_path):
    consent.record(tmp_path, "claude-opus-5")
    assert consent.revoke(tmp_path) is True
    assert consent.needs_prompt(tmp_path) is True
    assert consent.revoke(tmp_path) is False


def test_the_notice_names_what_leaves_and_what_stays():
    notice = consent.NOTICE
    assert "excerpts from your medical records and personal notes" in notice
    assert "your files" in notice and "stay on this disk" in notice
    # Plan §4: documented as hybrid, never as local, and linking to Anthropic's
    # terms rather than characterizing them.
    assert "HYBRID" in notice
    assert "anthropic.com/legal" in notice


def test_the_notice_does_not_characterize_anthropics_terms():
    """§4: link to the terms rather than paraphrasing what they say."""
    lowered = consent.NOTICE.lower()
    for overclaim in ("we do not store", "will be deleted", "never used for",
                      "is not retained", "anonymized"):
        assert overclaim not in lowered


def test_literature_consent_is_a_separate_record_from_cloud(tmp_path):
    assert consent.needs_prompt(tmp_path, notice=consent.LITERATURE)
    consent.record(tmp_path, "", notice=consent.LITERATURE)
    assert not consent.needs_prompt(tmp_path, notice=consent.LITERATURE)
    assert consent.needs_prompt(tmp_path)              # cloud still unconsented
    assert consent.consent_path(tmp_path, notice=consent.LITERATURE).name == "literature_consent.json"
    assert consent.revoke(tmp_path, notice=consent.LITERATURE)
    assert consent.needs_prompt(tmp_path, notice=consent.LITERATURE)


def test_literature_notice_says_what_leaves_and_when():
    text = consent.LITERATURE.text.lower()
    for phrase in ("github.com", "eutils.ncbi.nlm.nih.gov", "pack", "ip address",
                   "never during", "revoke"):
        assert phrase in text


def test_a_record_for_one_notice_does_not_satisfy_the_other(tmp_path):
    """The two files are separate, but a file copied or renamed across them
    must not count either: the record names the notice it answered."""
    consent.record(tmp_path, "", notice=consent.LITERATURE)
    lit = consent.consent_path(tmp_path, notice=consent.LITERATURE)
    lit.rename(consent.consent_path(tmp_path))
    assert consent.needs_prompt(tmp_path) is True


# --------------------------------------------------------------------------- #
# Wire translation
# --------------------------------------------------------------------------- #

def test_tool_schemas_are_unwrapped_for_anthropic():
    """Ollama nests the schema under `function`; Anthropic takes it flat with
    `input_schema`. Getting this wrong produces a 400, not a wrong answer."""
    converted = backends.CloudBackend.tool_schemas(tools.schemas())
    assert converted
    for schema in converted:
        assert set(schema) == {"name", "description", "input_schema"}
        assert schema["input_schema"]["type"] == "object"
    assert {s["name"] for s in converted} == {t.name for t in tools.TOOLS}


class FakeBlock:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeResponse:
    def __init__(self, content, stop_reason="end_turn", stop_details=None):
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = stop_details


def make_cloud(monkeypatch, responses):
    """A CloudBackend whose SDK is a stub. No sockets, no key needed."""
    calls = []

    class FakeMessages:
        def create(self, **payload):
            calls.append(payload)
            return responses.pop(0)

    class FakeBeta:
        def __init__(self):
            self.messages = FakeMessages()

    class FakeClient:
        def __init__(self):
            self.messages = FakeMessages()
            self.beta = FakeBeta()

    fake_sdk = types.ModuleType("anthropic")
    fake_sdk.Anthropic = FakeClient
    for name in ("APIError", "APIStatusError", "APIConnectionError",
                 "AuthenticationError", "PermissionDeniedError",
                 "NotFoundError", "RateLimitError", "BadRequestError"):
        setattr(fake_sdk, name, type(name, (Exception,), {}))
    monkeypatch.setitem(sys.modules, "anthropic", fake_sdk)
    return backends.CloudBackend(), calls


def test_transcript_translates_to_anthropic_messages(monkeypatch):
    backend, calls = make_cloud(monkeypatch, [
        FakeResponse([FakeBlock(type="text", text="done")])])

    transcript = [
        {"role": "user", "content": "What is my LDL?"},
        {"role": "assistant", "content": "",
         "tool_calls": [backends.ToolCall(name="get_lab_trend",
                                          arguments={"analyte": "ldl"},
                                          call_id="toolu_1")]},
        {"role": "tool", "name": "get_lab_trend", "call_id": "toolu_1",
         "content": '{"points": []}'},
    ]
    backend.turn("SYSTEM", transcript, tools.schemas(), think=False)

    messages = calls[0]["messages"]
    assert calls[0]["system"] == "SYSTEM"
    assert messages[0] == {"role": "user", "content": "What is my LDL?"}

    # The assistant turn carries a tool_use block with its id.
    use = messages[1]["content"][0]
    assert use["type"] == "tool_use"
    assert use["id"] == "toolu_1"
    assert use["input"] == {"analyte": "ldl"}

    # The result comes back as a tool_result block in a USER message.
    assert messages[2]["role"] == "user"
    assert messages[2]["content"][0]["type"] == "tool_result"
    assert messages[2]["content"][0]["tool_use_id"] == "toolu_1"


def test_parallel_tool_results_batch_into_one_user_message(monkeypatch):
    """Splitting results across several messages trains the model to stop
    making parallel calls."""
    backend, calls = make_cloud(monkeypatch, [
        FakeResponse([FakeBlock(type="text", text="ok")])])

    transcript = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [
            backends.ToolCall(name="a", arguments={}, call_id="t1"),
            backends.ToolCall(name="b", arguments={}, call_id="t2"),
        ]},
        {"role": "tool", "name": "a", "call_id": "t1", "content": "{}"},
        {"role": "tool", "name": "b", "call_id": "t2", "content": "{}"},
    ]
    backend.turn("S", transcript, tools.schemas(), think=False)

    results = [m for m in calls[0]["messages"] if m["role"] == "user"
               and isinstance(m["content"], list)]
    assert len(results) == 1
    assert len(results[0]["content"]) == 2


def test_tool_use_blocks_are_parsed_into_calls(monkeypatch):
    backend, _ = make_cloud(monkeypatch, [FakeResponse(
        [FakeBlock(type="text", text="Looking that up."),
         FakeBlock(type="tool_use", id="toolu_9", name="get_lab_trend",
                   input={"analyte": "hba1c"})],
        stop_reason="tool_use")])

    turn = backend.turn("S", [{"role": "user", "content": "q"}],
                        tools.schemas(), think=False)
    assert turn.text == "Looking that up."
    assert turn.tool_calls[0].name == "get_lab_trend"
    assert turn.tool_calls[0].arguments == {"analyte": "hba1c"}
    assert turn.tool_calls[0].call_id == "toolu_9"


def test_a_refusal_is_handled_not_read_as_an_answer(monkeypatch):
    """A refusal arrives as a successful response with empty content. Reading
    content[0] unconditionally would raise, or return a fragment as an answer."""
    backend, _ = make_cloud(monkeypatch, [FakeResponse(
        [], stop_reason="refusal",
        stop_details=FakeBlock(type="refusal", category="cyber"))])

    turn = backend.turn("S", [{"role": "user", "content": "q"}],
                        tools.schemas(), think=False)
    assert turn.refused is True
    assert turn.refusal_detail == "cyber"
    assert turn.tool_calls == []


def test_sampling_parameters_are_never_sent(monkeypatch):
    """temperature/top_p/top_k are rejected by this model family with a 400."""
    backend, calls = make_cloud(monkeypatch, [
        FakeResponse([FakeBlock(type="text", text="ok")])])
    backend.turn("S", [{"role": "user", "content": "q"}], tools.schemas(),
                 think=False)
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in calls[0]


def test_effort_is_passed_through_output_config(monkeypatch):
    backend, calls = make_cloud(monkeypatch, [
        FakeResponse([FakeBlock(type="text", text="ok")])])
    backend.effort = "high"
    backend.turn("S", [{"role": "user", "content": "q"}], tools.schemas(),
                 think=False)
    assert calls[0]["output_config"] == {"effort": "high"}


def test_missing_sdk_is_an_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(backends.CloudUnavailable) as exc:
        backends.CloudBackend()
    assert '[cloud]' in str(exc.value)


# --------------------------------------------------------------------------- #
# Tier provenance through the orchestrator
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cloud")
    conn = sqlite_schema.connect(tmp / "health.db", create=True)
    sqlite_schema.initialize(conn)

    def register(path, kind):
        return healthkit.register_source_file(
            conn, path, kind, healthkit.sha256_file(path))

    healthkit.ingest_file(conn, FIXTURES / "export.xml")
    sqlite_schema.rebuild_daily_metrics(conn)
    records.ingest_records(conn, FIXTURES / "records",
                           register=lambda p: register(p, "record"),
                           use_ocr=False)
    notes.ingest_notes(conn, FIXTURES / "notes",
                       register=lambda p: register(p, "note"))
    yield tools.ToolContext(conn=conn, vector_path=tmp / "vectors")
    conn.close()


def test_the_default_tier_is_local(ctx):
    orch = orchestrator.Orchestrator(ctx)
    assert orch.tier == "local"
    assert isinstance(orch.backend, backends.LocalBackend)


def test_the_answer_records_which_tier_produced_it(ctx, monkeypatch):
    backend, _ = make_cloud(monkeypatch, [
        FakeResponse([FakeBlock(type="text", text="Your LDL is 112 mg/dL.")])])
    orch = orchestrator.Orchestrator(ctx, backend=backend)

    answer = orch.ask("What is my LDL?")
    assert answer.tier == "cloud"
    assert answer.model == backends.DEFAULT_CLOUD_MODEL
    assert "112" in answer.text


def test_the_loop_runs_tools_identically_on_the_cloud_tier(ctx, monkeypatch):
    """Same tools, same data, same numbers — only the reasoning differs."""
    backend, _ = make_cloud(monkeypatch, [
        FakeResponse([FakeBlock(type="tool_use", id="t1", name="get_lab_trend",
                                input={"analyte": "ldl"})],
                     stop_reason="tool_use"),
        FakeResponse([FakeBlock(type="text", text="LDL went 128 -> 112.")]),
    ])
    orch = orchestrator.Orchestrator(ctx, backend=backend)

    answer = orch.ask("How has my LDL changed?")
    assert answer.tools_used == ["get_lab_trend"]
    assert answer.steps[0].result["points"][-1]["value"] == 112.0
    assert answer.tier == "cloud"


def test_a_refused_answer_skips_the_guardrail_and_says_so(ctx, monkeypatch):
    backend, _ = make_cloud(monkeypatch, [FakeResponse(
        [], stop_reason="refusal",
        stop_details=FakeBlock(type="refusal", category="cyber"))])
    orch = orchestrator.Orchestrator(ctx, backend=backend)

    answer = orch.ask("something the classifier dislikes")
    assert answer.refused is True
    assert "declined" in answer.text
    assert "without --cloud" in answer.text
    assert answer.guardrail is None
