"""The only module in this package that opens a socket.

Everything else — parsing, storage, aggregation, retrieval, orchestration — is
pure local computation. Keeping the entire network surface in one small file is
what makes the privacy claim in plan §5 checkable by reading rather than by
trusting: `grep -rn "urllib\\|socket\\|http" health_agent/` returns this file and
nothing else, and a test in the suite fails if that stops being true.

The connection is to Ollama on **localhost**, which is on-device inference, not a
remote service. That distinction is the product, so it is enforced rather than
assumed: a non-loopback host is refused unless the user explicitly sets
`HEALTH_AGENT_ALLOW_REMOTE_OLLAMA=1`. Pointing this at someone else's Ollama box
would quietly turn the local tier into a hybrid one, which is exactly the
blurring the project exists to avoid.

Transport only. This module knows how to speak to Ollama; it holds no opinion
about prompts, tools, or health data.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_CHAT_MODEL = "qwen3.6:27b"

ENV_HOST = "HEALTH_AGENT_OLLAMA_HOST"
ENV_EMBED_MODEL = "HEALTH_AGENT_EMBED_MODEL"
ENV_CHAT_MODEL = "HEALTH_AGENT_MODEL"
ENV_ALLOW_REMOTE = "HEALTH_AGENT_ALLOW_REMOTE_OLLAMA"

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}

# Generation on a 27B model is slow enough that a short timeout would abort
# healthy requests; a multi-tool answer can legitimately take minutes.
DEFAULT_TIMEOUT = 600.0


class OllamaUnavailable(RuntimeError):
    """The server could not be reached, or the model is not usable."""


class RemoteHostRefused(RuntimeError):
    """A non-loopback Ollama host was configured without explicit opt-in."""


def resolve_chat_model(model: str | None = None) -> str:
    """`--model`, then `$HEALTH_AGENT_MODEL`, then the default. The README
    has promised the environment variable since the embed one existed; this
    is where the promise is kept."""
    return model or os.environ.get(ENV_CHAT_MODEL) or DEFAULT_CHAT_MODEL


def resolve_host(host: str | None = None) -> str:
    resolved = (host or os.environ.get(ENV_HOST) or DEFAULT_HOST).rstrip("/")
    assert_local(resolved)
    return resolved


def assert_local(host: str) -> None:
    parsed = urllib.parse.urlparse(host)
    hostname = (parsed.hostname or "").lower()
    if hostname in LOOPBACK_HOSTS:
        return
    if os.environ.get(ENV_ALLOW_REMOTE) == "1":
        return
    raise RemoteHostRefused(
        f"Refusing to send health data to a non-local Ollama host ({hostname!r}). "
        f"The default tier is local-only by design. If you really intend to use a "
        f"remote inference server, set {ENV_ALLOW_REMOTE}=1 — but understand that "
        f"this is no longer a local-only setup."
    )


def post(host: str, endpoint: str, payload: dict,
         timeout: float = DEFAULT_TIMEOUT) -> dict:
    """POST JSON to Ollama and return the decoded response."""
    request = urllib.request.Request(
        f"{host}{endpoint}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:  # noqa: BLE001 - the error body is best-effort
            pass
        model = payload.get("model", "")
        if exc.code == 404 and "not found" in detail.lower():
            raise OllamaUnavailable(
                f"Ollama has no model {model!r}. Pull it with:\n"
                f"  ollama pull {model}"
            ) from exc
        raise OllamaUnavailable(
            f"Ollama returned HTTP {exc.code}" + (f": {detail}" if detail else "")
        ) from exc
    except urllib.error.URLError as exc:
        raise OllamaUnavailable(
            f"Could not reach Ollama at {host} ({exc.reason}). Is it running? "
            f"Start it with `ollama serve`, or install it from "
            f"https://ollama.com/download"
        ) from exc
    except json.JSONDecodeError as exc:
        raise OllamaUnavailable("Ollama returned a malformed response") from exc


def embed(host: str, model: str, texts: list[str],
          timeout: float = DEFAULT_TIMEOUT) -> list[list[float]]:
    data = post(host, "/api/embed", {"model": model, "input": texts}, timeout)
    vectors = data.get("embeddings")
    if not vectors or len(vectors) != len(texts):
        raise OllamaUnavailable(
            f"Ollama returned {len(vectors or [])} embeddings for {len(texts)} inputs"
        )
    return [[float(v) for v in vector] for vector in vectors]


def chat(host: str, model: str, messages: list[dict], *,
         tools: list[dict] | None = None, think: bool | None = None,
         keep_alive: str = "15m", options: dict | None = None,
         timeout: float = DEFAULT_TIMEOUT) -> dict:
    """One non-streaming turn of /api/chat. Returns the raw response.

    Non-streaming on purpose: a streamed response has to be reassembled before
    tool calls can be read, and the loop needs the whole message anyway. Progress
    is surfaced to the user by reporting each tool call as it runs, which is more
    informative than a token stream during the phase where the model is deciding
    what to look up.
    """
    payload: dict = {"model": model, "messages": messages, "stream": False,
                     "keep_alive": keep_alive}
    if tools:
        payload["tools"] = tools
    if think is not None:
        payload["think"] = think
    if options:
        payload["options"] = options
    return post(host, "/api/chat", payload, timeout)


def list_models(host: str, timeout: float = 30.0) -> list[str]:
    request = urllib.request.Request(f"{host}/api/tags", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise OllamaUnavailable(f"Could not reach Ollama at {host}") from exc
    return [m.get("name", "") for m in data.get("models", [])]
