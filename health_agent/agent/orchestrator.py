"""The tool-calling loop, hand-rolled against Ollama's chat API.

No orchestration framework, by design (plan §4): the tool surface is three
functions, the loop below is about sixty lines, and owning it outright keeps the
tool-calling contract free to change when model-agnostic support lands. A
framework here would add a dependency layer between the model and three SQL
queries.

**The loop is iterative, not single-shot, and that is load-bearing.** Measured
against `qwen3.6:27b` before this was written: asked to "compare my LDL trend
with my step counts", the model emitted one tool call (`get_lab_trend`) and
stopped, rather than requesting both tools at once. A single-shot design would
have answered that question with half the data and no indication anything was
missing. So each tool result is fed back and the model gets another turn to ask
for more, until it stops calling tools or hits `max_steps`.

**Thinking is off by default.** Same measurement: enabling it tripled latency
(17s to 51s on a tool-call decision) and produced a byte-identical tool call.
`--think` turns it on for questions where the reasoning is worth the wait.

**Every turn is recorded.** `Answer.steps` holds each tool call and its result,
which is what makes an answer auditable after the fact — the reproducibility-log
idea from §10 item 3, in its cheapest useful form. It costs nothing to record
while the loop is running and cannot be reconstructed afterwards.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger
from . import backends
from . import guardrail as guardrail_module
from . import tools as tools_module

log = get_logger("agent.orchestrator")

DEFAULT_MAX_STEPS = 6

SYSTEM_PROMPT = """\
You answer questions about one person's own health data. You have tools that \
read their Apple Health export, their lab reports, and their personal notes.

Rules, in order of importance:

1. Every number you state must come from a tool result in this conversation. \
You have no other knowledge of this person. Never estimate, never fill a gap \
with a typical value, never carry a number over from your training data.

1b. That applies to medicine in general, not only to this person. Do not quote \
clinical thresholds, diagnostic criteria, guideline cut-offs, or "normal" ranges \
from your own knowledge — only the reference ranges the tools return, which are \
the ones the lab printed, and findings returned by search_medical_literature, \
which you must cite with their year and evidence tier. If a question turns on a \
threshold you were not given, search the literature for it; if that returns \
nothing, say the reports do not state it and their clinician can.

1a. Do not do arithmetic. If a tool result has an `overall` field, that is the \
figure for the whole range — quote it as given. Never average, total, or \
otherwise recompute values from the individual points; if the number you want \
is not in a tool result, call a tool again with different arguments instead of \
working it out yourself.

2. Cite the source of every fact drawn from a tool. Lab and document results \
carry a `citation` string such as "labs_2026-03-10.pdf, p.1, 2026-03-10" — \
reproduce it verbatim, at least once per source you draw on, rather than giving \
only the date. When you show a table of lab values, include the citation as a \
column or name the report beneath it. For Apple Health data there is no file, so \
name the metric and the date or period instead.

3. When a tool reports that data is missing, say so plainly. Give the range that \
IS available, and ask whether the person tracks the missing thing. Do not \
answer with a nearby period's data as though it covered the period asked about, \
and do not substitute a different but related metric.

4. Do not diagnose, do not interpret results as indicating a condition, and do \
not recommend treatment, medication, dosage, or supplements. You may say what a \
value is, whether the lab flagged it as outside its printed reference range, how \
it has changed, and what the person's own notes say. Anything beyond that \
belongs to them and their clinician.

4a. Declining to interpret never means declining to look. When a question is \
about this person's health data, call the tools and report what the data shows \
FIRST, then say plainly that interpreting it is a conversation for them and \
their doctor. Answering a question about their data without calling any tool is \
always wrong — it is unhelpful and it withholds information that is already \
theirs. "Is there anything I should ask my doctor about?" is answered by \
retrieving the values their reports flagged as out of range and listing them, \
not by refusing.

4b. Never ask which data to fetch when you could just fetch it. If a question \
is broad, gather the obvious things and answer; offer to go deeper afterwards.

5. Report units and reference ranges exactly as the tools give them. Never \
convert units. If a tool warns that units differ across reports, say so rather \
than comparing the numbers as if they matched.

6. Relay tool warnings that affect how much a number can be trusted — values \
read by OCR from a scan, days where two devices both recorded, truncated \
ranges.

Be brief and concrete. Lead with the answer. Prefer a short table or a few \
lines over paragraphs. Do not repeat the question back."""


@dataclass
class ToolCallRecord:
    step: int
    name: str
    arguments: dict
    result: dict
    elapsed_sec: float

    @property
    def failed(self) -> bool:
        return "error" in self.result


@dataclass
class Answer:
    text: str
    steps: list[ToolCallRecord] = field(default_factory=list)
    thinking: list[str] = field(default_factory=list)
    model: str = ""
    # 'local' or 'cloud' — recorded on the answer so provenance survives into
    # --json output and any later reproducibility log (§10 item 3). Which tier
    # answered is part of what an answer means.
    tier: str = "local"
    elapsed_sec: float = 0.0
    hit_step_limit: bool = False
    refused: bool = False
    guardrail: guardrail_module.GuardrailResult | None = None

    @property
    def tools_used(self) -> list[str]:
        return [s.name for s in self.steps]

    @property
    def citations(self) -> list[str]:
        """Every citation string the tools handed back, deduped in order.

        This is what the answer *could* have cited. Comparing it against what the
        answer actually cites is how milestone 6 measures citation quality.
        """
        seen: dict[str, None] = {}
        for step in self.steps:
            for value in _walk_citations(step.result):
                seen.setdefault(value, None)
        return list(seen)


def _walk_citations(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "citation" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_walk_citations(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_citations(item))
    return found


class Orchestrator:
    """Runs one question to an answer, calling tools as the model asks for them."""

    def __init__(self, ctx: tools_module.ToolContext, *, model: str | None = None,
                 host: str | None = None, think: bool = False,
                 max_steps: int = DEFAULT_MAX_STEPS,
                 on_event: Callable[[str, dict], None] | None = None,
                 skip_guardrail: bool = False,
                 backend: "backends.Backend | None" = None) -> None:
        self.ctx = ctx
        self.skip_guardrail = skip_guardrail
        # Default to the local tier. The cloud backend is only ever passed in
        # explicitly, by a CLI path that has already taken consent.
        self.backend = backend or backends.LocalBackend(model=model, host=host)
        self.model = self.backend.model
        self.tier = self.backend.tier
        self.think = think
        self.max_steps = max_steps
        # Progress callback: the loop can run for a minute or more, and reporting
        # each tool call as it happens is more informative than a spinner.
        self.on_event = on_event or (lambda event, data: None)

    def system_prompt(self) -> str:
        """The base prompt plus a description of what this index contains.

        The inventory matters: without it the model guesses metric names, wastes
        a turn on a lookup that returns nothing, and sometimes answers about a
        metric this person doesn't track.
        """
        inventory = tools_module.data_inventory(self.ctx.conn)
        return (
            f"{SYSTEM_PROMPT}\n\n"
            f"This person's data, as currently indexed:\n"
            f"{json.dumps(inventory, indent=2, default=str)}\n\n"
            f"Anything not listed above is not available. Use the tools to read "
            f"actual values; the inventory only tells you what exists."
        )

    def ask(self, question: str) -> Answer:
        started = time.monotonic()
        system = self.system_prompt()
        # Neutral transcript; each backend translates it to its own wire format.
        transcript: list[dict] = [{"role": "user", "content": question}]
        answer = Answer(text="", model=self.model, tier=self.tier)

        for step in range(1, self.max_steps + 1):
            self.on_event("thinking", {"step": step})
            turn = self.backend.turn(system, transcript,
                                     tools_module.schemas(), think=self.think)
            if turn.thinking:
                answer.thinking.append(turn.thinking)

            if turn.refused:
                answer.refused = True
                answer.text = (
                    f"The model declined to answer this "
                    f"({turn.refusal_detail}). Try rephrasing, or ask the "
                    f"local tier instead — `health-agent ask` without --cloud."
                )
                break

            # Append the assistant turn so the model sees its own tool calls on
            # the next pass; dropping them breaks multi-step reasoning.
            transcript.append({"role": "assistant", "content": turn.text,
                               "tool_calls": turn.tool_calls})

            if not turn.tool_calls:
                answer.text = turn.text
                break

            for call in turn.tool_calls:
                record = self._run_call(step, call)
                answer.steps.append(record)
                transcript.append({
                    "role": "tool",
                    "name": record.name,
                    "call_id": call.call_id,
                    "content": tools_module.serialize(record.result),
                })
        else:
            # Out of steps with tools still being requested. Ask once more with
            # no tools available, so the user gets an answer from what was
            # gathered instead of an empty response.
            answer.hit_step_limit = True
            self.on_event("step_limit", {"max_steps": self.max_steps})
            final = self.backend.final_turn(
                system, transcript,
                "Answer now using only what the tool results above already "
                "show. Say plainly what you could not determine.",
                think=self.think,
            )
            answer.text = final.text

        if not answer.text:
            answer.text = ("I could not produce an answer for that. Try "
                           "rephrasing, or use `health-agent labs` / "
                           "`health-agent metric` to read the data directly.")

        answer.text, answer.guardrail = self._guard(answer, system, transcript)
        answer.elapsed_sec = time.monotonic() - started
        return answer

    def _guard(self, answer: Answer, system: str, transcript: list[dict]):
        """Second guardrail layer (plan §5), applied to the finished answer.

        The first layer is the system prompt. This one catches what the prompt
        did not, and can ask the model once to restate itself. The rewrite reuses
        the existing conversation so the numbers and citations already gathered
        stay available to it.
        """
        if self.skip_guardrail or answer.refused:
            return answer.text, None

        def rewrite(instruction: str) -> str:
            self.on_event("guardrail_rewrite", {"instruction": instruction})
            return self.backend.final_turn(system, transcript, instruction,
                                           think=self.think).text

        text, result = guardrail_module.apply(
            answer.text, tools_used=answer.tools_used,
            literature_cited=self._cited_literature(answer), rewrite=rewrite)
        if result.flags:
            # Categories only — never the flagged text, which is health content
            # (§5a logging policy).
            log.info("guardrail flagged %s (rewritten=%s)",
                     ",".join(result.categories), result.rewritten)
            self.on_event("guardrail", {"categories": result.categories,
                                        "rewritten": result.rewritten})
        return text, result

    @staticmethod
    def _cited_literature(answer: "Answer") -> bool:
        """True when a literature search actually returned findings this turn.

        Calling the tool is not enough — a call that returned `no_matches` gives
        the model nothing to cite, and an answer that states a threshold anyway
        is reciting, which is exactly what the check is for.
        """
        for step in answer.steps:
            if step.name != "search_medical_literature":
                continue
            result = step.result if isinstance(step.result, dict) else {}
            if result.get("findings"):
                return True
        return False

    def _run_call(self, step: int, call: backends.ToolCall) -> ToolCallRecord:
        name = call.name
        arguments = call.arguments
        # Some models emit arguments as a JSON string rather than an object.
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"_raw": arguments}

        self.on_event("tool_call", {"step": step, "name": name,
                                    "arguments": arguments})
        started = time.monotonic()
        result = tools_module.dispatch(self.ctx, name, arguments)
        elapsed = time.monotonic() - started

        # Metadata only: counts and names, never the values themselves (§5a).
        log.debug("step %d: %s -> %d key(s) in %.2fs", step, name,
                  len(result) if isinstance(result, dict) else 0, elapsed)
        self.on_event("tool_result", {"step": step, "name": name,
                                      "elapsed_sec": elapsed,
                                      "error": result.get("error")})
        return ToolCallRecord(step=step, name=name, arguments=arguments,
                              result=result, elapsed_sec=elapsed)
