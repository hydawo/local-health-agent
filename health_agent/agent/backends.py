"""Model backends: local Ollama, and the opt-in cloud tier.

The orchestrator's loop is identical for both tiers — ask, run tools, feed
results back — but the two APIs disagree about almost every detail of how a
conversation is spelled. Ollama nests tool calls under `message.tool_calls[].
function` and takes results as `{"role": "tool"}` messages; Anthropic returns
`tool_use` content blocks and takes results as `tool_result` blocks inside a
*user* message. Rather than branch on provider throughout the loop, each backend
translates a neutral transcript to and from its own wire format.

That neutral form is also what makes the tiers comparable: the same question
produces the same tool calls against the same data, and only the reasoning
differs. It is the "clean contract separate from the model" §4 asks for, and it
is what a model-agnostic `--model` flag (roadmap item 5) will build on.

**Tier is a property of the backend, and the CLI prints it.** A user should never
have to infer from a flag whether their data left the machine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from .. import ollama_client
from ..logging_setup import get_logger

log = get_logger("agent.backends")


@dataclass
class ToolCall:
    name: str
    arguments: dict
    # Anthropic requires the id of the originating tool_use block on the result;
    # Ollama has no such id. Empty string when the provider doesn't use one.
    call_id: str = ""


@dataclass
class Turn:
    """One assistant turn, normalized across providers."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    thinking: str = ""
    stop_reason: str = ""
    refused: bool = False
    refusal_detail: str = ""


class Backend(Protocol):
    name: str
    tier: str          # "local" | "cloud"
    model: str

    def turn(self, system: str, transcript: list[dict],
             tools: list[dict], *, think: bool) -> Turn:
        ...

    def final_turn(self, system: str, transcript: list[dict],
                   instruction: str, *, think: bool) -> Turn:
        ...


# --------------------------------------------------------------------------- #
# Local: Ollama
# --------------------------------------------------------------------------- #

class LocalBackend:
    """Ollama on this machine. No data leaves the host."""

    tier = "local"

    def __init__(self, model: str | None = None, host: str | None = None) -> None:
        self.model = ollama_client.resolve_chat_model(model)
        self.host = ollama_client.resolve_host(host)
        self.name = f"ollama:{self.model}"

    def _messages(self, system: str, transcript: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for entry in transcript:
            role = entry["role"]
            if role == "tool":
                out.append({"role": "tool", "tool_name": entry["name"],
                            "content": entry["content"]})
            elif role == "assistant" and entry.get("tool_calls"):
                out.append({
                    "role": "assistant",
                    "content": entry.get("content", ""),
                    "tool_calls": [
                        {"function": {"name": call.name, "arguments": call.arguments}}
                        for call in entry["tool_calls"]
                    ],
                })
            else:
                out.append({"role": role, "content": entry.get("content", "")})
        return out

    def _chat(self, system, transcript, tools, think) -> Turn:
        response = ollama_client.chat(
            self.host, self.model, self._messages(system, transcript),
            tools=tools or None, think=think,
        )
        message = response.get("message", {}) or {}
        calls = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function", {})
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"_raw": arguments}
            calls.append(ToolCall(name=function.get("name", ""),
                                  arguments=arguments))
        return Turn(
            text=(message.get("content") or "").strip(),
            tool_calls=calls,
            thinking=message.get("thinking") or "",
            stop_reason="tool_use" if calls else "end_turn",
        )

    def turn(self, system, transcript, tools, *, think=False) -> Turn:
        return self._chat(system, transcript, tools, think)

    def final_turn(self, system, transcript, instruction, *, think=False) -> Turn:
        extended = transcript + [{"role": "user", "content": instruction}]
        return self._chat(system, extended, None, think)


# --------------------------------------------------------------------------- #
# Cloud: Anthropic
# --------------------------------------------------------------------------- #

# Sampling parameters (temperature/top_p/top_k) are rejected by this model
# family, and thinking is on by default — neither is set here.
DEFAULT_CLOUD_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 8192

# Server-side refusal fallback: if the model's safety classifiers decline a
# request, the API re-runs it on the fallback model rather than returning a
# refusal. Health questions are not the target of those classifiers, but a
# refused answer to "what did my lab report say" would be a confusing failure,
# and the recovery costs one parameter.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class CloudUnavailable(RuntimeError):
    """The cloud tier is not installed or not configured."""


class CloudBackend:
    """Anthropic's API. **Sends health data off this machine** — see consent.py.

    Only constructed after consent has been recorded; the check lives in the CLI
    so that no code path can reach the network by instantiating this directly
    without going through the prompt.
    """

    tier = "cloud"

    def __init__(self, model: str | None = None,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 effort: str | None = None) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise CloudUnavailable(
                "The cloud tier needs the Anthropic SDK, which is not "
                "installed. It is an optional extra so that the local tier "
                "carries no vendor dependency:\n"
                "  pip install -e \".[cloud]\""
            ) from exc

        self._anthropic = anthropic
        try:
            self._client = anthropic.Anthropic()
        except Exception as exc:  # noqa: BLE001 - surfaces as a clean CLI error
            raise CloudUnavailable(
                f"Could not construct the Anthropic client ({type(exc).__name__}). "
                f"Set ANTHROPIC_API_KEY, or run `ant auth login`."
            ) from exc

        self.model = model or DEFAULT_CLOUD_MODEL
        self.max_tokens = max_tokens
        self.effort = effort
        self.name = f"anthropic:{self.model}"

    # -- wire translation --------------------------------------------------- #

    @staticmethod
    def tool_schemas(tools: list[dict]) -> list[dict]:
        """Ollama nests the schema under `function`; Anthropic does not."""
        out = []
        for tool in tools:
            function = tool.get("function", tool)
            out.append({
                "name": function["name"],
                "description": function["description"],
                "input_schema": function["parameters"],
            })
        return out

    def _messages(self, transcript: list[dict]) -> list[dict]:
        """Neutral transcript -> Anthropic messages.

        Tool results are `tool_result` blocks in a **user** message, and
        consecutive results must be batched into one message: splitting them
        across several trains the model to stop making parallel calls.
        """
        out: list[dict] = []
        pending_results: list[dict] = []

        def flush_results() -> None:
            nonlocal pending_results
            if pending_results:
                out.append({"role": "user", "content": pending_results})
                pending_results = []

        for entry in transcript:
            role = entry["role"]
            if role == "tool":
                pending_results.append({
                    "type": "tool_result",
                    "tool_use_id": entry.get("call_id") or entry["name"],
                    "content": entry["content"],
                })
                continue

            flush_results()
            if role == "assistant" and entry.get("tool_calls"):
                blocks: list[dict] = []
                if entry.get("content"):
                    blocks.append({"type": "text", "text": entry["content"]})
                for call in entry["tool_calls"]:
                    blocks.append({
                        "type": "tool_use",
                        "id": call.call_id,
                        "name": call.name,
                        "input": call.arguments,
                    })
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": role, "content": entry.get("content", "")})

        flush_results()
        return out

    def _create(self, system: str, transcript: list[dict],
                tools: list[dict] | None, think: bool) -> Any:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": self._messages(transcript),
        }
        if tools:
            payload["tools"] = self.tool_schemas(tools)
        if self.effort:
            payload["output_config"] = {"effort": self.effort}
        # Thinking is on by default on this model family; `--think` only asks
        # for the reasoning to be summarized back rather than omitted.
        if think:
            payload["thinking"] = {"type": "adaptive", "display": "summarized"}

        try:
            return self._client.beta.messages.create(
                betas=[FALLBACK_BETA], fallbacks="default", **payload)
        except self._anthropic.BadRequestError:
            # The fallback beta is not available to every account; the request
            # itself is valid without it.
            log.info("retrying without the refusal-fallback beta")
            return self._client.messages.create(**payload)

    def _turn(self, response: Any) -> Turn:
        # A refusal arrives as a successful response with an empty or partial
        # body — reading content[0] unconditionally would raise or, worse,
        # silently return a fragment as though it were the answer.
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            return Turn(
                stop_reason="refusal", refused=True,
                refusal_detail=getattr(details, "category", "") or "unspecified",
            )

        text_parts, calls, thinking = [], [], []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking":
                thinking.append(getattr(block, "thinking", "") or "")
            elif block.type == "tool_use":
                calls.append(ToolCall(name=block.name,
                                      arguments=dict(block.input or {}),
                                      call_id=block.id))
        return Turn(
            text="".join(text_parts).strip(),
            tool_calls=calls,
            thinking="\n".join(t for t in thinking if t),
            stop_reason=response.stop_reason or "",
        )

    def turn(self, system, transcript, tools, *, think=False) -> Turn:
        return self._turn(self._create(system, transcript, tools, think))

    def final_turn(self, system, transcript, instruction, *, think=False) -> Turn:
        extended = transcript + [{"role": "user", "content": instruction}]
        return self._turn(self._create(system, extended, None, think))

    def describe_error(self, exc: Exception) -> str:
        """Map SDK exceptions onto something a user can act on."""
        anthropic = self._anthropic
        if isinstance(exc, anthropic.AuthenticationError):
            return ("Anthropic rejected the credentials. Set ANTHROPIC_API_KEY, "
                    "or run `ant auth login`.")
        if isinstance(exc, anthropic.PermissionDeniedError):
            return "That API key does not have access to this model."
        if isinstance(exc, anthropic.NotFoundError):
            return f"Model {self.model!r} was not found for this account."
        if isinstance(exc, anthropic.RateLimitError):
            return "Rate limited by Anthropic. Wait a moment and try again."
        if isinstance(exc, anthropic.APIConnectionError):
            return "Could not reach Anthropic's API. Check your connection."
        if isinstance(exc, anthropic.APIStatusError):
            return f"Anthropic returned HTTP {exc.status_code}."
        return f"{type(exc).__name__} talking to Anthropic."

    @property
    def sdk_errors(self) -> tuple[type[BaseException], ...]:
        return (self._anthropic.APIError,)
