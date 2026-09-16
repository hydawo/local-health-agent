"""Proves the local tier never reaches the network (plan §5, §9).

Two layers, because they prove different things:

**In-process socket block** — fast, portable, runs on every CI machine. Any
attempt to create a socket raises, so the whole ingest-and-query path is
exercised with Python networking removed. This catches everything this codebase
could do, since all of it goes through Python.

**OS sandbox** — the kernel denies every socket syscall, so it also covers
native code (LanceDB's Rust, C extensions) that a Python patch cannot see. Skips
where no sandbox is available, rather than silently downgrading and reporting a
pass that means less than it appears to.

The second is the one that makes the claim falsifiable. The first is the one
that runs on every commit.
"""

from __future__ import annotations

import platform
import shutil
import socket
import sys
from pathlib import Path

import pytest

from health_agent import offline_check

FIXTURES = Path(__file__).parent / "fixtures"


class NetworkAttempted(AssertionError):
    """Raised if anything tries to open a socket during a local-tier run."""


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly on any *network* socket, in this process.

    AF_UNIX is deliberately allowed. LanceDB's async runtime calls
    `socket.socketpair()` (an AF_UNIX pair) to wake its event loop — that is
    in-process IPC that never touches a network interface, and blocking it
    would make this test fail for a reason that has nothing to do with the
    claim being proved. The OS sandbox draws exactly the same line: macOS
    `(deny network*)` permits an AF_UNIX socketpair and denies AF_INET.

    Getting this wrong in the lenient direction would be the dangerous error,
    so the guard blocks by default and allows only the local families.
    """
    real_socket = socket.socket
    local_families = {socket.AF_UNIX} if hasattr(socket, "AF_UNIX") else set()

    # A subclass rather than a function: `ssl.py` runs `class SSLSocket(socket)`
    # at import time, so a plain callable breaks any import that reaches ssl.
    class GuardedSocket(real_socket):
        def __init__(self, family=socket.AF_INET, *args, **kwargs):
            if family not in local_families:
                raise NetworkAttempted(
                    f"a local-tier code path opened a network socket "
                    f"(family={family!r})")
            super().__init__(family, *args, **kwargs)

    def blocked(*args, **kwargs):
        raise NetworkAttempted(
            "a local-tier code path attempted a network connection")

    monkeypatch.setattr(socket, "socket", GuardedSocket)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "gethostbyname", blocked)
    return blocked


# --------------------------------------------------------------------------- #
# The guard itself must work
# --------------------------------------------------------------------------- #

def test_the_block_actually_blocks(no_network):
    """A no-network test that doesn't verify its own guard proves nothing."""
    with pytest.raises(NetworkAttempted):
        socket.create_connection(("example.com", 80))
    with pytest.raises(NetworkAttempted):
        socket.socket()
    with pytest.raises(NetworkAttempted):
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    with pytest.raises(NetworkAttempted):
        socket.getaddrinfo("example.com", 80)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no AF_UNIX")
def test_local_socketpairs_are_permitted(no_network):
    """AF_UNIX is in-process IPC, not network. LanceDB's async runtime uses it,
    and the OS sandbox permits it too — the guard must draw the same line or it
    fails for reasons unrelated to the claim."""
    left, right = socket.socketpair()
    left.close()
    right.close()


def test_the_block_catches_our_own_client(no_network):
    """The Ollama client is the local tier's only network module — under the
    block it must fail, which confirms the guard sees real call paths."""
    from health_agent import ollama_client

    with pytest.raises(Exception) as exc:
        ollama_client.list_models("http://localhost:11434", timeout=1)
    assert exc.value is not None


# --------------------------------------------------------------------------- #
# The pipeline runs with networking removed
# --------------------------------------------------------------------------- #

def test_full_local_pipeline_runs_with_no_network(no_network, tmp_path):
    """Ingest, aggregate, search, and every agent tool — no sockets."""
    steps = offline_check.run_pipeline(FIXTURES, tmp_path)

    assert steps, "the pipeline reported no steps"
    failed = [s for s in steps if not s["ok"]]
    assert failed == [], f"steps failed under no-network: {failed}"

    names = {s["step"] for s in steps}
    # Guard against the proof quietly shrinking: these are the paths that touch
    # user data, and all of them must be covered.
    for required in ("ingest healthkit export", "ingest record PDFs (incl. OCR path)",
                     "ingest notes", "lab trend", "keyword search",
                     "vector search (LanceDB)", "agent tool: search_records"):
        assert required in names, f"{required!r} missing from the offline proof"


def test_ingest_alone_is_offline(no_network, tmp_path):
    """Plan §5 singles out the ingest path: no network calls there in ANY mode,
    including when the cloud tier is in use."""
    from health_agent.ingest import healthkit, notes, records
    from health_agent.store import sqlite_schema

    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)

    def register(path, kind):
        return healthkit.register_source_file(
            conn, path, kind, healthkit.sha256_file(path))

    assert healthkit.ingest_file(conn, FIXTURES / "export.xml").records_inserted == 25
    sqlite_schema.rebuild_daily_metrics(conn)
    assert records.ingest_records(
        conn, FIXTURES / "records",
        register=lambda p: register(p, "record")).documents == 3
    assert notes.ingest_notes(
        conn, FIXTURES / "notes",
        register=lambda p: register(p, "note")).notes == 5
    conn.close()


def test_embedding_and_vector_store_are_offline(no_network, tmp_path):
    """The offline embedder and LanceDB must not need a network at any point —
    including index creation, which is where a vector DB might phone home."""
    from health_agent.embeddings import HashingEmbedder
    from health_agent.ingest import healthkit, records
    from health_agent.store import sqlite_schema, vector_store

    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    records.ingest_records(
        conn, FIXTURES / "records",
        register=lambda p: healthkit.register_source_file(
            conn, p, "record", healthkit.sha256_file(p)),
        use_ocr=False)

    store = vector_store.VectorStore(tmp_path / "vectors")
    embedder = HashingEmbedder()
    assert vector_store.embed_pending(conn, store, embedder) > 0
    assert vector_store.semantic_search(conn, store, embedder, "cholesterol")
    conn.close()


def test_no_telemetry_on_import(no_network):
    """Plan §5: no telemetry, no analytics, no phone-home, in either mode.

    Importing every module in the package must not open a socket — this is what
    would catch a dependency that reports usage at import time.
    """
    import importlib
    import pkgutil

    import health_agent

    package_dir = Path(health_agent.__file__).parent
    for info in pkgutil.walk_packages([str(package_dir)],
                                      prefix="health_agent."):
        # offline_check spawns a subprocess only when called, not on import.
        importlib.import_module(info.name)


# --------------------------------------------------------------------------- #
# OS-level sandbox: the strong form of the claim
# --------------------------------------------------------------------------- #

def _os_sandbox_available() -> bool:
    """Delegate to the real detector rather than re-deriving it here.

    This used to repeat `detect_mechanism`'s own "is the binary on PATH"
    test, which meant it inherited the same bug: on a host where `unshare`
    exists but cannot create a user namespace, both agreed a sandbox was
    available and the sandboxed tests then failed. Asking the detector is
    also the more honest skip condition — it skips exactly when the real
    command would decline to use the OS sandbox.
    """
    return offline_check.detect_mechanism()[0] == "os-sandbox"


needs_sandbox = pytest.mark.skipif(
    not _os_sandbox_available(),
    reason="no OS-level network sandbox available on this platform",
)


@needs_sandbox
def test_mechanism_detection_prefers_the_os_sandbox():
    mechanism, detail = offline_check.detect_mechanism()
    assert mechanism == "os-sandbox"
    assert detail


@needs_sandbox
@pytest.mark.slow
def test_pipeline_passes_under_a_real_os_sandbox():
    """The falsifiable version: every socket syscall denied by the kernel, so
    native code is covered too — not just Python."""
    result = offline_check.run(FIXTURES, timeout=900)

    assert result.mechanism == "os-sandbox", result.mechanism_detail
    assert result.passed, f"{result.error}\n{result.stderr_tail}"
    assert result.proves_offline
    assert any(s["step"] == "vector search (LanceDB)" for s in result.steps)


def test_a_failure_is_reported_rather_than_swallowed(tmp_path, monkeypatch):
    """If the sandboxed run cannot produce a result, the check must not pass."""
    monkeypatch.setattr(offline_check.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            FileNotFoundError("sandbox-exec missing")))
    result = offline_check.run(FIXTURES)
    assert result.passed is False
    assert result.proves_offline is False
    assert "could not start the sandbox" in result.error


# --------------------------------------------------------------------------- #
# The literature corpus must not open a hole in the network boundary
# --------------------------------------------------------------------------- #

def test_the_query_path_cannot_reach_a_fetcher():
    """The network boundary, enforced statically rather than by discipline.

    Slice 2 adds `literature/fetch/`. This asserts that nothing reachable from
    the ask path imports it — so the headline claim (no health question touches
    the network) is checked by CI rather than by remembering.

    Run in a subprocess rather than by deleting health_agent* out of
    sys.modules and re-importing in-process: churning module identity mid-test-
    session can corrupt state for tests that already hold references to those
    modules, which is a worse failure mode than the one this test is guarding
    against. offline_check.py already runs its own checks via a subprocess
    child process for the same reason — this follows that pattern.
    """
    import subprocess

    code = (
        "import sys\n"
        "import importlib\n"
        "importlib.import_module('health_agent.agent.orchestrator')\n"
        "importlib.import_module('health_agent.agent.tools')\n"
        "importlib.import_module('health_agent.literature.store')\n"
        "leaked = [n for n in sys.modules "
        "if n.startswith('health_agent.literature.fetch')]\n"
        "print('LEAKED:' + ','.join(leaked))\n"
    )
    completed = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    leaked_line = next(
        line for line in completed.stdout.splitlines()
        if line.startswith("LEAKED:"))
    leaked = [n for n in leaked_line[len("LEAKED:"):].split(",") if n]
    assert not leaked, f"the query path imported a fetcher: {leaked}"

    # Nothing under health_agent.literature may reach the network at all. The
    # corpus is read locally; only slice 2's fetch package will be allowed a
    # client, and this asserts it has not arrived early or by accident.
    literature_dir = Path(offline_check.__file__).parent / "literature"
    for path in literature_dir.rglob("*.py"):
        text = path.read_text()
        for forbidden in ("import urllib", "import http", "import socket",
                          "import requests"):
            assert forbidden not in text, (
                f"{path} imports {forbidden!r}; the corpus must not reach the "
                f"network")


def test_offline_check_pipeline_covers_the_literature_tool(tmp_path):
    """The falsifiable proof must not develop a hole where the new tool is."""
    steps = offline_check.run_pipeline(FIXTURES, tmp_path)
    names = [s["step"] for s in steps]
    assert any("search_medical_literature" in n for n in names)
    assert all(s["ok"] for s in steps)


def test_offline_check_exercises_the_corpus_vector_store_directly(tmp_path):
    """`run_pipeline`'s ToolContext has no `embedder_factory`, so the
    `search_medical_literature` step above runs keyword-only and never
    touches LanceDB. The personal store has a dedicated "vector search
    (LanceDB)" step for exactly this reason (spec §5); the corpus needs its
    mirror, or the newest native-code path — reads against
    `literature_chunks` — sits outside the one test whose entire purpose is
    to be a falsifiable proof."""
    steps = offline_check.run_pipeline(FIXTURES, tmp_path)
    names = [s["step"] for s in steps]
    assert "vector search (literature LanceDB)" in names
    assert all(s["ok"] for s in steps)


def test_detect_mechanism_falls_back_when_the_sandbox_cannot_actually_run(
        monkeypatch):
    """Being on PATH is not the same as working.

    GitHub's Ubuntu runners ship `unshare` but forbid unprivileged user
    namespaces, so the binary exists and every run of it fails. Claiming
    `os-sandbox` and then erroring is worse than reporting the weaker
    mechanism honestly — naming the mechanism only means something if the
    name is true.
    """
    monkeypatch.setattr(offline_check, "_sandbox_probe_passes",
                        lambda command: False)
    mechanism, detail = offline_check.detect_mechanism()

    assert mechanism == "python"
    assert detail


def test_detect_mechanism_reports_the_os_sandbox_when_the_probe_passes(
        monkeypatch):
    if not (shutil.which("sandbox-exec") or shutil.which("unshare")):
        pytest.skip("no sandbox binary on this platform")
    monkeypatch.setattr(offline_check, "_sandbox_probe_passes",
                        lambda command: True)

    assert offline_check.detect_mechanism()[0] == "os-sandbox"


def test_missing_sandbox_is_explained_as_absent_or_as_blocked(monkeypatch):
    """"Not installed" and "installed but not permitted" need different advice.

    The second is the common case — containers and CI runners ship `unshare`
    and forbid unprivileged user namespaces — and telling that reader to
    install a binary they already have sends them the wrong way.
    """
    monkeypatch.setattr(offline_check.platform, "system", lambda: "Linux")

    monkeypatch.setattr(offline_check.shutil, "which", lambda name: None)
    assert "install" in offline_check.explain_missing_sandbox().lower()

    monkeypatch.setattr(offline_check.shutil, "which", lambda name: "/usr/bin/unshare")
    blocked = offline_check.explain_missing_sandbox()
    assert "install" not in blocked.lower()
    assert "namespace" in blocked.lower()
