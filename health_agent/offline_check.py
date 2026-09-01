"""`health-agent offline-check` — the falsifiable proof for the local tier.

Plan §5 asks for a command that *proves* no outbound connections happen during a
local-mode run, rather than asserting it in a README. This runs the real
pipeline — ingest, aggregate, lab trends, search — inside a sandbox where
network access is denied, and reports which enforcement mechanism actually
applied.

Three mechanisms, strongest first. The command names the one it used, because
they are not equally convincing:

  os-sandbox   The kernel denies every socket syscall (macOS `sandbox-exec`,
               Linux `unshare -n`). This covers native code — the Rust inside
               LanceDB, anything in a C extension — not just Python.
  python       `socket` is patched to raise in the child process. Portable and
               enough to catch every call this codebase could make, since all
               of it goes through Python, but a native library could in
               principle bypass it.
  none         Neither was available. The check refuses to report a pass.

The distinction matters more than the result. A green check under `python` is
weaker evidence than a green check under `os-sandbox`, and a tool whose whole
claim is "verify this yourself" should not blur the two.

**It runs against the synthetic fixtures by default**, so anyone can reproduce
the proof without owning an Apple Health export — and so running the proof is
never itself a reason to touch real health data. `--data` points it at a real
folder when you want to prove it on your own files.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

MACOS_PROFILE = """\
(version 1)
(allow default)
(deny network*)
"""


@dataclass
class CheckResult:
    mechanism: str                       # os-sandbox | python | none
    mechanism_detail: str = ""
    passed: bool = False
    steps: list[dict] = field(default_factory=list)
    error: str = ""
    stderr_tail: str = ""

    @property
    def proves_offline(self) -> bool:
        return self.passed and self.mechanism in ("os-sandbox", "python")


# --------------------------------------------------------------------------- #
# The child: the actual pipeline, run under whatever sandbox wraps it
# --------------------------------------------------------------------------- #

def run_pipeline(data_dir: Path, workdir: Path) -> list[dict]:
    """Exercise every local code path that touches user data.

    Deliberately covers ingest *and* read: a check that only proved ingestion
    was offline would say nothing about the query path, and the query path is
    where the vector store and the tool layer live.
    """
    from health_agent import labs, metrics
    from health_agent.agent import tools as agent_tools
    from health_agent.ingest import healthkit, notes, records
    from health_agent.store import queries, sqlite_schema, vector_store

    steps: list[dict] = []

    def step(name: str, fn):
        try:
            detail = fn()
            steps.append({"step": name, "ok": True, "detail": detail})
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            steps.append({"step": name, "ok": False,
                          "detail": f"{type(exc).__name__}: {exc}"})
            raise

    index_path = workdir / "health.db"
    conn = sqlite_schema.connect(index_path, create=True)
    sqlite_schema.initialize(conn)

    def register(path, kind):
        return healthkit.register_source_file(
            conn, path, kind, healthkit.sha256_file(path))

    export = data_dir / "export.xml"
    if export.exists():
        step("ingest healthkit export", lambda: (
            f"{healthkit.ingest_file(conn, export).records_inserted} records"))
        step("rebuild daily aggregates",
             lambda: f"{sqlite_schema.rebuild_daily_metrics(conn)} rows")

    records_dir = data_dir / "records"
    if records_dir.is_dir():
        step("ingest record PDFs (incl. OCR path)", lambda: (
            f"{records.ingest_records(conn, records_dir, register=lambda p: register(p, 'record')).documents} documents"))

    notes_dir = data_dir / "notes"
    if notes_dir.is_dir():
        step("ingest notes", lambda: (
            f"{notes.ingest_notes(conn, notes_dir, register=lambda p: register(p, 'note')).notes} notes"))

    step("aggregate a metric", lambda: (
        f"{len(queries.metric_series(conn, metrics.resolve('resting-hr')).points)} points"))
    step("lab trend", lambda: (
        f"{len(queries.lab_trend(conn, 'ldl').points)} results"))
    step("sleep nights", lambda: f"{len(queries.sleep_nights(conn))} nights")
    step("keyword search", lambda: (
        f"{len(vector_store.keyword_search(conn, 'cholesterol'))} hits"))

    # Embed with the offline hashing backend and search the vector store: this
    # exercises LanceDB, whose native code an OS sandbox catches and a Python
    # socket patch would not.
    from health_agent.embeddings import HashingEmbedder

    store = vector_store.VectorStore(workdir / "vectors")
    embedder = HashingEmbedder()
    step("embed chunks (offline backend)", lambda: (
        f"{vector_store.embed_pending(conn, store, embedder)} chunks"))
    step("vector search (LanceDB)", lambda: (
        f"{len(vector_store.semantic_search(conn, store, embedder, 'cholesterol'))} hits"))

    ctx = agent_tools.ToolContext(conn=conn, vector_path=workdir / "vectors")
    step("agent tool: query_healthkit", lambda: (
        agent_tools.dispatch(ctx, "query_healthkit",
                             {"metric": "resting-hr"}).get("overall", {}).get("value")))
    step("agent tool: get_lab_trend", lambda: (
        f"{len(agent_tools.dispatch(ctx, 'get_lab_trend', {'analyte': 'ldl'}).get('points', []))} points"))
    step("agent tool: search_records", lambda: (
        f"{len(agent_tools.dispatch(ctx, 'search_records', {'query': 'vitamin d'}).get('results', []))} results"))

    # The literature corpus, built and searched entirely inside the sandbox.
    # Without this the falsifiable proof would have a hole exactly where the
    # newest code is.
    from health_agent.literature import corpus as lit_corpus
    from health_agent.literature import embed as lit_embed
    from health_agent.literature import medline
    from health_agent.literature import schema as lit_schema

    corpus_xml = data_dir / "literature" / "corpus.xml"
    if corpus_xml.exists():
        lit_conn = lit_schema.connect(workdir / "literature.db", create=True)
        lit_schema.initialize(lit_conn)
        step("build literature corpus", lambda: (
            f"{lit_corpus.build(lit_conn, medline.parse_articles(corpus_xml.read_bytes()), slug='fixture', version='1', license='synthetic').articles} articles"))

        lit_store_path = workdir / "literature_vectors"
        lit_vectors = vector_store.VectorStore(lit_store_path,
                                               table_name=lit_embed.TABLE_NAME)
        step("embed literature corpus", lambda: (
            f"{lit_embed.embed_corpus(lit_conn, lit_vectors, embedder)} chunks"))

        # Mirrors "vector search (LanceDB)" above: the agent-tool step below
        # runs with no `embedder_factory`, so it never exercises this path.
        # Without a direct call here, the newest native-code read — against
        # `literature_chunks` — would sit outside the one test whose entire
        # purpose is to be a falsifiable proof (spec §5).
        from health_agent.literature import store as lit_store
        step("vector search (literature LanceDB)", lambda: (
            f"{len(lit_store.search(lit_conn, lit_vectors, embedder, 'blood pressure'))} hits"))

        ctx.literature_conn = lit_conn
        ctx.literature_vector_path = lit_store_path
        step("agent tool: search_medical_literature", lambda: (
            f"{len(agent_tools.dispatch(ctx, 'search_medical_literature', {'query': 'blood pressure'}).get('findings', []))} findings"))
        lit_conn.close()

    conn.close()
    return steps


def _child_main(argv: list[str]) -> int:
    """Entry point for the sandboxed subprocess. Prints one JSON line."""
    data_dir = Path(argv[0])
    workdir = Path(argv[1])
    block_python_sockets = argv[2] == "python"

    if block_python_sockets:
        _install_socket_block()

    try:
        steps = run_pipeline(data_dir, workdir)
        print(json.dumps({"ok": True, "steps": steps}))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1


def _install_socket_block() -> None:
    """Make any socket creation raise, in this process.

    Covers everything this codebase does — all of it goes through Python — but
    not a native library that opens a socket without asking Python. The
    OS-sandbox mechanism is what covers that case, and the report says which
    one ran.
    """
    import socket

    class NetworkAttempted(RuntimeError):
        pass

    real_socket = socket.socket
    # AF_UNIX is local IPC, not network — LanceDB's async runtime opens a
    # socketpair to wake its event loop. The OS sandbox permits it too; this
    # keeps both mechanisms drawing the same line.
    local_families = {socket.AF_UNIX} if hasattr(socket, "AF_UNIX") else set()

    # A *subclass*, not a function: `ssl.py` runs `class SSLSocket(socket)` at
    # import time, so replacing socket.socket with a plain callable breaks any
    # import that pulls in ssl — including our own Ollama client. The OS
    # sandbox has no such problem, since it constrains the process rather than
    # rewriting the module.
    class GuardedSocket(real_socket):
        def __init__(self, family=socket.AF_INET, *args, **kwargs):
            if family not in local_families:
                raise NetworkAttempted(
                    f"offline-check: a network socket was opened "
                    f"(family={family!r})")
            super().__init__(family, *args, **kwargs)

    def blocked(*args, **kwargs):
        raise NetworkAttempted(
            "offline-check: a network call was attempted during a local run")

    socket.socket = GuardedSocket
    socket.create_connection = blocked
    socket.create_server = blocked
    for name in ("getaddrinfo", "gethostbyname"):
        setattr(socket, name, blocked)


# --------------------------------------------------------------------------- #
# The parent: pick a sandbox and run the child inside it
# --------------------------------------------------------------------------- #

def _sandbox_probe_passes(command: list[str]) -> bool:
    """Run the sandbox on a trivial command to see whether it actually works.

    Being on PATH is not the same as being usable. GitHub's Ubuntu runners
    ship `unshare` but forbid unprivileged user namespaces, so `unshare -rn`
    exists and fails every time; the same is true inside most containers,
    which is where a lot of people will try to verify this claim.

    Detecting by presence alone would report `os-sandbox` and then die. That
    is the worst of the three outcomes: this module's whole point is that a
    pass under `python` is weaker evidence than a pass under `os-sandbox`,
    which only means something if the name is true. So probe, and fall back
    to the weaker mechanism honestly when the stronger one cannot run.
    """
    try:
        completed = subprocess.run(command, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def detect_mechanism() -> tuple[str, str]:
    """Pick the strongest *working* enforcement. Returns (mechanism, detail)."""
    system = platform.system()
    if system == "Darwin" and shutil.which("sandbox-exec"):
        with tempfile.TemporaryDirectory(prefix="health-agent-probe-") as tmp:
            profile = Path(tmp) / "no-network.sb"
            profile.write_text(MACOS_PROFILE)
            if _sandbox_probe_passes(
                    ["sandbox-exec", "-f", str(profile), "true"]):
                return "os-sandbox", "macOS sandbox-exec, (deny network*)"
    elif system == "Linux" and shutil.which("unshare"):
        # An empty network namespace has no route to anywhere, including
        # loopback services on the host.
        if _sandbox_probe_passes(["unshare", "-rn", "true"]):
            return "os-sandbox", "Linux unshare -n (empty network namespace)"
    return "python", "socket module patched to raise in the child process"


def _wrap(mechanism: str, command: list[str], profile_path: Path) -> list[str]:
    if mechanism != "os-sandbox":
        return command
    if platform.system() == "Darwin":
        return ["sandbox-exec", "-f", str(profile_path), *command]
    return ["unshare", "-rn", *command]


def run(data_dir: Path, *, force_mechanism: str | None = None,
        timeout: float = 900.0) -> CheckResult:
    mechanism, detail = (
        (force_mechanism, f"forced: {force_mechanism}") if force_mechanism
        else detect_mechanism()
    )
    result = CheckResult(mechanism=mechanism, mechanism_detail=detail)

    with tempfile.TemporaryDirectory(prefix="health-agent-offline-") as tmp:
        tmpdir = Path(tmp)
        profile = tmpdir / "no-network.sb"
        profile.write_text(MACOS_PROFILE)
        workdir = tmpdir / "index"
        workdir.mkdir()

        command = _wrap(mechanism, [
            sys.executable, "-m", "health_agent.offline_check",
            "--child", str(data_dir), str(workdir), mechanism,
        ], profile)

        env = dict(os.environ)
        # Keep the child from finding credentials even by accident.
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(key, None)

        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout,
                env=env, cwd=str(Path(__file__).resolve().parent.parent))
        except subprocess.TimeoutExpired:
            result.error = f"the sandboxed run exceeded {timeout:.0f}s"
            return result
        except FileNotFoundError as exc:
            result.error = f"could not start the sandbox: {exc}"
            return result

        result.stderr_tail = "\n".join(completed.stderr.strip().splitlines()[-8:])
        payload = _last_json_line(completed.stdout)
        if payload is None:
            result.error = (f"the sandboxed run produced no result "
                            f"(exit {completed.returncode})")
            return result

        result.steps = payload.get("steps", [])
        if payload.get("ok"):
            result.passed = True
        else:
            result.error = payload.get("error", "unknown failure")
    return result


def _last_json_line(text: str) -> dict | None:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        raise SystemExit(_child_main(sys.argv[2:]))
    raise SystemExit("run via `health-agent offline-check`")
