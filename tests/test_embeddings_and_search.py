"""Embedding backend and retrieval tests.

These run without Ollama: the offline `HashingEmbedder` exercises the storage and
retrieval paths, and the Ollama client is tested against a stub HTTP layer rather
than a live server. No test in this file opens a socket.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from health_agent import embeddings, ollama_client
from health_agent.ingest import healthkit, records
from health_agent.store import sqlite_schema, vector_store

RECORDS = Path(__file__).parent / "fixtures" / "records"


# --------------------------------------------------------------------------- #
# The local-only guard
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("host", [
    "http://localhost:11434",
    "http://127.0.0.1:11434",
    "http://[::1]:11434",
])
def test_loopback_hosts_are_allowed(host):
    assert embeddings.OllamaEmbedder(host=host).host == host


@pytest.mark.parametrize("host", [
    "http://192.168.1.50:11434",
    "https://ollama.example.com",
])
def test_remote_hosts_are_refused(host, monkeypatch):
    """A remote Ollama would quietly turn the local tier into a hybrid one."""
    monkeypatch.delenv(embeddings.ENV_ALLOW_REMOTE, raising=False)
    with pytest.raises(embeddings.RemoteHostRefused) as exc:
        embeddings.OllamaEmbedder(host=host)
    assert "no longer a local-only setup" in str(exc.value)


def test_remote_host_allowed_only_with_explicit_opt_in(monkeypatch):
    monkeypatch.setenv(embeddings.ENV_ALLOW_REMOTE, "1")
    assert embeddings.OllamaEmbedder(host="http://10.0.0.5:11434")


def test_the_network_surface_is_exactly_two_modules():
    """The privacy claim in plan §5 rests on this staying true.

    Two modules may reach the network, and the split is the product:

      ollama_client.py    local tier — loopback only, enforced at runtime
      agent/backends.py   cloud tier — Anthropic, gated behind explicit consent

    The vendor SDK counts. `import anthropic` opens sockets just as surely as
    `import urllib`, and a check that only looked for stdlib transports would
    have passed while the cloud backend quietly shipped data off the machine —
    which is precisely the blurring this project exists to avoid.
    """
    import re

    package = Path(__file__).resolve().parent.parent / "health_agent"
    # Matches *imports*, not free text. Earlier this scanned for the bare words
    # and flagged `cli.py` for the phrase "socket syscall" in a help string,
    # and `offline_check.py` for `sqlite_schema.connect(`. A check with false
    # positives gets relaxed until it means nothing; opening a network
    # connection requires importing a networking module, so that is the signal.
    pattern = re.compile(
        r"^\s*(?:import|from)\s+"
        r"(urllib|socket|http\.client|http|requests|httpx|aiohttp"
        r"|anthropic|openai|google\.genai)\b",
        re.MULTILINE,
    )

    matched = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if pattern.search(path.read_text())
    }
    # offline_check.py touches `socket` for the opposite reason: it disables it
    # to prove the pipeline runs without one. Exempted by name, and the
    # exemption is then checked rather than trusted, below.
    assert matched == {"ollama_client.py", "agent/backends.py",
                       "offline_check.py"}, (
        f"the network surface changed: {matched}"
    )


def test_the_offline_checker_disables_networking_rather_than_using_it():
    """`offline_check.py` is exempt from the surface test above only because it
    patches `socket` to raise. If it ever actually connected, the exemption
    would be hiding a network call inside the very thing that proves there
    isn't one."""
    import re

    package = Path(__file__).resolve().parent.parent / "health_agent"
    source = (package / "offline_check.py").read_text()

    # Two precision requirements, both learned from false positives:
    #   * a trailing "(" — the file *assigns* `socket.create_connection = blocked`
    #     to disable it, which is the opposite of calling it
    #   * no bare `.connect(` — sqlite3 and `sqlite_schema.connect()` use that
    #     name, and a check whose whole value is trustworthiness cannot cry wolf
    outbound = re.compile(
        r"\b(urlopen|socket\.create_connection|socket\.connect|"
        r"requests\.(get|post)|httpx\.(get|post)|session\.(get|post))\s*\(")
    assert outbound.search(source) is None, (
        "offline_check.py appears to make an outbound call"
    )
    # It must actually install the block, or the exemption is unearned.
    assert "_install_socket_block" in source
    assert "raise NetworkAttempted" in source


def test_the_local_tier_does_not_import_the_cloud_sdk():
    """Someone who never opts in should not have the vendor SDK on the import
    path at all — it is an optional extra, and the local tier must run without
    it installed."""
    import re

    package = Path(__file__).resolve().parent.parent / "health_agent"
    backends = (package / "agent" / "backends.py").read_text()
    # The import sits inside CloudBackend.__init__, not at module scope, so
    # importing the agent package never pulls in the SDK. Anchored at column
    # zero on purpose: an indented import is exactly the lazy form we want.
    module_level = re.search(r"^(?:import|from)\s+anthropic\b", backends,
                             re.MULTILINE)
    assert module_level is None, (
        "anthropic must be imported lazily inside CloudBackend, so the local "
        "tier works without the [cloud] extra installed"
    )


# --------------------------------------------------------------------------- #
# Ollama client, against a stubbed transport
# --------------------------------------------------------------------------- #

def test_ollama_embed_request_and_response(monkeypatch):
    captured = {}

    def fake_post(host, endpoint, payload, timeout=None):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        return {"embeddings": [[0.1, 0.2], [0.3, 0.4]]}

    monkeypatch.setattr(ollama_client, "post", fake_post)
    vectors = embeddings.OllamaEmbedder().embed(["one", "two"])

    assert captured["endpoint"] == "/api/embed"
    assert captured["payload"] == {"model": "nomic-embed-text",
                                   "input": ["one", "two"]}
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


def test_ollama_mismatched_response_is_an_error(monkeypatch):
    monkeypatch.setattr(ollama_client, "post",
                        lambda h, e, p, t=None: {"embeddings": [[0.1]]})
    with pytest.raises(embeddings.EmbeddingUnavailable):
        embeddings.OllamaEmbedder().embed(["one", "two"])


def test_unreachable_ollama_gives_actionable_message(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(ollama_client.urllib.request, "urlopen", boom)
    with pytest.raises(embeddings.EmbeddingUnavailable) as exc:
        embeddings.OllamaEmbedder().embed(["x"])
    assert "ollama serve" in str(exc.value)


def test_missing_model_names_the_pull_command(monkeypatch):
    import io
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.HTTPError(
            "http://localhost:11434/api/embed", 404, "Not Found", {},
            io.BytesIO(json.dumps({"error": 'model "x" not found'}).encode()))

    monkeypatch.setattr(ollama_client.urllib.request, "urlopen", boom)
    with pytest.raises(embeddings.EmbeddingUnavailable) as exc:
        embeddings.OllamaEmbedder(model="nomic-embed-text").embed(["x"])
    assert "ollama pull nomic-embed-text" in str(exc.value)


def test_chat_payload_shape(monkeypatch):
    """The orchestrator depends on this exact request shape."""
    captured = {}

    def fake_post(host, endpoint, payload, timeout=None):
        captured.update({"endpoint": endpoint, "payload": payload})
        return {"message": {"role": "assistant", "content": "ok"}}

    monkeypatch.setattr(ollama_client, "post", fake_post)
    ollama_client.chat("http://localhost:11434", "m",
                       [{"role": "user", "content": "hi"}],
                       tools=[{"type": "function"}], think=False)

    assert captured["endpoint"] == "/api/chat"
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["think"] is False
    assert captured["payload"]["tools"] == [{"type": "function"}]


# --------------------------------------------------------------------------- #
# Hashing embedder
# --------------------------------------------------------------------------- #

def test_hashing_embedder_is_deterministic_and_normalized():
    embedder = embeddings.HashingEmbedder(dim=64)
    first, second = embedder.embed(["glucose is elevated"])[0], \
        embedder.embed(["glucose is elevated"])[0]
    assert first == second
    assert len(first) == 64
    assert abs(sum(v * v for v in first) - 1.0) < 1e-9


def test_hashing_embedder_is_not_semantic():
    """Documented limitation, asserted so nobody mistakes it for a real model."""
    embedder = embeddings.HashingEmbedder(dim=256)
    a, b = embedder.embed(["elevated glucose", "high blood sugar"])
    similarity = sum(x * y for x, y in zip(a, b))
    assert abs(similarity) < 0.01  # unrelated, despite meaning the same thing


# --------------------------------------------------------------------------- #
# End-to-end retrieval
# --------------------------------------------------------------------------- #

@pytest.fixture
def indexed(tmp_path):
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)

    def register(path):
        return healthkit.register_source_file(
            conn, path, "record", healthkit.sha256_file(path))

    records.ingest_records(conn, RECORDS, register=register, use_ocr=False)
    store = vector_store.VectorStore(tmp_path / "vectors")
    yield conn, store
    conn.close()


def test_keyword_search_works_without_any_embeddings(indexed):
    conn, _ = indexed
    hits = vector_store.keyword_search(conn, "cholesterol", limit=3)
    assert hits
    assert all(h.method == "keyword" for h in hits)
    assert hits[0].citation.endswith(("2025-09-12", "2026-03-10"))


def test_keyword_search_tolerates_punctuation():
    """A bare '-' or '*' in a question must not be parsed as FTS operator syntax."""
    assert vector_store._fts_query("what about my LDL - is it high?*")
    assert '"LDL"' in vector_store._fts_query("LDL - high?")


def test_embed_then_semantic_search(indexed):
    conn, store = indexed
    embedder = embeddings.HashingEmbedder()
    count = vector_store.embed_pending(conn, store, embedder)
    assert count > 0
    assert store.count() == count

    hits = vector_store.semantic_search(conn, store, embedder,
                                        "cholesterol lipids", limit=2)
    assert hits
    assert all(h.method == "semantic" for h in hits)
    assert all(h.page_no == 1 for h in hits)


def test_embedding_is_resumable_and_not_repeated(indexed):
    conn, store = indexed
    embedder = embeddings.HashingEmbedder()
    first = vector_store.embed_pending(conn, store, embedder)
    second = vector_store.embed_pending(conn, store, embedder)
    assert first > 0
    assert second == 0
    assert store.count() == first


def test_vectors_from_different_models_are_not_mixed(indexed):
    """Cosine distance across two models' vector spaces is noise, so a query
    embedded with one model must only see rows embedded with that model."""
    conn, store = indexed
    small = embeddings.HashingEmbedder(dim=256)
    vector_store.embed_pending(conn, store, small)

    other = embeddings.HashingEmbedder(dim=256)
    other.name = "hashing:other"
    hits = vector_store.semantic_search(conn, store, other, "cholesterol")
    assert hits == []


def test_search_hit_citation_format(indexed):
    conn, _ = indexed
    hit = vector_store.keyword_search(conn, "cholesterol", limit=1)[0]
    assert "labs_" in hit.citation and "p.1" in hit.citation
