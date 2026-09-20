# Threat model

What this tool does with your health data, what it claims, and how to check the
claims yourself rather than trusting this document.

Every claim below is labelled with how you can verify it. Where a claim has
limits, the limits are stated here rather than left for you to discover.

---

## The one-sentence version

There are two tiers. **The local tier makes no network connections at all** and
you can prove that with one command. **The cloud tier sends your health data to
Anthropic** and requires you to type `yes` before it will do so once.

One more thing connects, and it is neither tier: installing a literature pack
downloads a public file, after a separate notice, and sends nothing of yours.
Claim 6 covers it.

---

## What is stored, and where

Nothing leaves the folder you choose.

| Thing | Location | Notes |
| --- | --- | --- |
| Your source files | wherever you put them (default `~/HealthData/`) | Never modified, never copied, never uploaded |
| SQLite index | `<data>/.index/health.db` | Record values, lab results, note text, chunk text |
| Vector index | `<data>/.index/vectors/` | Embeddings plus a copy of each chunk's text |
| Cloud consent record | `<data>/.index/cloud_consent.json` | Timestamp, model, notice version. No health data |
| Literature consent record | `<data>/.index/literature_consent.json` | Timestamp and notice version. No health data |
| Literature corpus | `<data>/.index/literature.db` and its vector table | Public abstracts from installed packs. Nothing of yours |
| Logs | stderr only | Filenames, counts, error types. Never values (see below) |

The index is derived data. `health-agent reset` deletes it; `ingest` rebuilds it.
Nothing is written outside the data folder — no `~/.config`, no cache directory,
no temp files that outlive a run.

**Verify:** `health-agent stats` prints every path in use. `find` your data
folder before and after a run.

---

## Claim 1 — the local tier makes no network connections

**The claim.** In default operation — `ingest`, `stats`, `metric`, `labs`,
`sleep`, `search`, `notes`, `documents`, `workouts`, and `ask` without
`--cloud` — this tool opens no network connections except to Ollama on
loopback, which is inference running on your own machine.

**How to check it yourself:**

```bash
health-agent offline-check
```

That runs the real pipeline — ingest, OCR, aggregation, lab trends, vector
search, and every one of the agent's tools — inside a sandbox where network
access is denied, and prints which enforcement mechanism applied:

| Mechanism | What it proves |
| --- | --- |
| `os-sandbox` | The **kernel** denied every socket syscall. Covers native code — LanceDB's Rust, any C extension — not just Python. This is the strong form. |
| `python` | Python's socket layer was disabled in the child process. Covers everything this codebase does, since all of it goes through Python, but a native library could in principle bypass it. |

The command names the mechanism it used and refuses to report a pass without
one. `sandbox-exec` (macOS) and `unshare` (Linux) give you the strong form.

**Also enforced in the test suite:** `tests/test_no_network.py` runs the whole
pipeline with sockets blocked, on every commit, and additionally runs the
OS-sandbox proof where a sandbox is available.

**Limits of this claim, stated plainly:**

- `offline-check` proves the pipeline *can* run offline. It is not a live
  wiretap on your actual sessions. If you want that, run the tool itself under
  `sandbox-exec -f` with the same profile, or watch it with `lsof`/`tcpdump`.
- Under the `python` mechanism, a dependency's native code could open a socket
  without Python seeing it. The OS sandbox is what closes that gap — use it.
- Ollama itself is a separate program. This tool only ever addresses it on
  loopback (see Claim 2), but what Ollama does is Ollama's threat model, not
  this one.

---

## Claim 2 — "local" means loopback, and that is enforced

A tool that let you point `--model` at a remote inference server while still
calling itself local would be exactly the ambiguity this project exists to
avoid. So the Ollama host is checked, not assumed: a non-loopback host is
**refused** unless you set `HEALTH_AGENT_ALLOW_REMOTE_OLLAMA=1`, and the error
says plainly that doing so means you no longer have a local-only setup.

**Verify:**

```bash
HEALTH_AGENT_OLLAMA_HOST=http://192.168.1.50:11434 health-agent ask "test"
```

It refuses. Tests: `test_remote_hosts_are_refused`,
`test_remote_host_allowed_only_with_explicit_opt_in`.

---

## Claim 3 — exactly three modules can reach the network

| Module | Tier | Destination |
| --- | --- | --- |
| `health_agent/ollama_client.py` | local | Ollama, loopback only |
| `health_agent/agent/backends.py` | cloud | Anthropic, after consent |
| `health_agent/literature/fetch/client.py` | packs | NCBI E-utilities (`build-pack`, maintainer) and GitHub Releases (`install`), after consent |

**Verify:**

```bash
grep -rn "import urllib\|from urllib\|import socket\|import requests\|import httpx\|import aiohttp" health_agent/
```

The grep matches imports, as the test does; a bare word like "socket" in a
help string is not a network call. One extra file appears in the output:
`offline_check.py` imports `socket` in order to disable it, and the test
exempts it by name and then checks that it never opens one.
`test_the_network_surface_is_exactly_three_modules` asserts that exact set and
fails if it changes. **It counts vendor SDK imports too** — `import anthropic`
opens sockets as surely as `import urllib`, and a check that only looked for
stdlib transports would have passed while the cloud backend shipped data off
the machine. That is not hypothetical: it is the bug this test was written after
nearly shipping.

The cloud SDK is an optional install and is imported lazily. If you never run
`pip install -e ".[cloud]"`, the vendor SDK is not on your machine at all.

---

## Claim 4 — no telemetry, no analytics, no update checks

This tool reports nothing about you or your usage to anyone, in either tier.
There is no crash reporter, no version check, no usage ping.

This shaped a dependency choice. The plan named Chroma or LanceDB for the vector
store; **Chroma's `anonymized_telemetry` defaults to on** and its install pulls
an OpenTelemetry exporter and a Kubernetes client. LanceDB has no telemetry
enabled by default, runs as a library with no server, and installs 15 packages
instead of 76. That is why the vector store is LanceDB.

**Verify:** `test_no_telemetry_on_import` imports every module in the package
with sockets blocked — which is where an import-time phone-home would show up.

---

## Claim 5 — logs never contain your health data

Logs carry filenames, counts, timestamps, and error types. Never record values,
never lab results, never note or record text, never your questions, never the
model's answers.

This is why ingest errors read `2 record(s) skipped: missing or unparseable
startDate` rather than showing you the offending record.

**Verify:** read `health_agent/logging_setup.py`, then run any command with
`-v` and check what appears. The rule is stated in `CONTRIBUTING.md` as a hard
requirement for changes.

---

## Claim 6: literature packs, what two commands send, and nothing else does

The literature corpus is public PubMed abstracts, and since the packs feature
it can be fetched rather than built by hand. That is the first network code
the corpus has ever had, so it gets its own claim.

**What is sent, by which command.** This mirrors the notice the tool prints
before it connects (`health-agent literature-consent --show-notice`).

- `health-agent literature install <pack>` sends a request to `github.com` for
  one named pack file from the project's releases, plus its `.sha256` sidecar.
  The request reveals which pack you chose, your IP address, and this tool's
  version string in the `User-Agent` header. Nothing from your data folder or
  about your questions is involved. The pack you choose is visible to GitHub,
  which is why packs are broad (`cardiovascular`, `sleep`) and never a single
  condition.
- `health-agent literature build-pack <pack>` is a maintainer command. It sends
  the pack's search terms, a fixed list of medical subject headings from the
  catalog that is the same for everyone, to `eutils.ncbi.nlm.nih.gov`, with
  your IP address and the tool's version. If `NCBI_API_KEY` is set in your
  environment, the key is sent as well. That is the whole request.

**When.** Only when you run one of those two commands, and only after a
one-time notice you accept by typing `yes` (or passing `--yes` for scripts).
Never during `ask`, `ingest`, `search`, or anything else. `offline-check` is
unchanged and still proves that. Passing `--from` a local file to `install`
asks for the same consent. It is the same command, and a URL can be passed
there. Reinstalling a version that is already present stops before any
request is made, unless you pass `--force` or `--from`. The downloaded file
is deleted whether or not the install succeeds, so nothing about the download
stays on disk beyond the articles.

**Where.** Two services, three hostnames, listed in `ALLOWED_HOSTS` in
`client.py`. Builds go to `eutils.ncbi.nlm.nih.gov`. Downloads go to
`github.com`, which serves release assets through a redirect to
`objects.githubusercontent.com`, so that host is allowed as well. A URL to any
other host is refused before a socket opens. So is a plain-`http://` URL to an
allowed host, and so is a URL carrying a username or password. A redirect is a
second request and gets the same check, so an allowed host cannot bounce the
client anywhere else. The transport is Python's `urllib`, which honours `https_proxy`
from your environment the way any `urllib` client does. If you have a proxy
set, the proxy sees the request.

**No update checks.** Neither command runs on its own, checks for a newer
pack, or reports usage. A newer pack version reaches you only when you run
`install` again.

**Consent.** Recorded per data folder in `literature_consent.json`, separate
from the cloud consent record, so agreeing to one never implies the other. The
record carries the notice's version, and a change to what is sent bumps the
version and asks again.

```bash
health-agent literature-consent               # what you agreed to, and when
health-agent literature-consent --show-notice
health-agent literature-consent --revoke
```

**Verify:**

```bash
grep -rn "import urllib\|from urllib" health_agent/literature/
```

That finds one file, `fetch/client.py`. Then run the two tests that hold this
claim in place:

```bash
pytest tests/test_no_network.py::test_the_query_path_cannot_reach_a_fetcher \
       tests/test_embeddings_and_search.py::test_the_network_surface_is_exactly_three_modules
```

The first imports the whole ask path in a fresh interpreter and asserts that
no `literature.fetch` module was loaded, then scans every file under
`literature/` and fails if any file other than `fetch/client.py` imports a
transport. The second is Claim 3's test.

---

## The cloud tier: what it sends

`ask --cloud` is **hybrid, not local**, and this document will not call it
anything else.

**Sent to Anthropic on every `--cloud` question:**

- your question, verbatim
- an inventory of your index: which metrics you track, which lab analytes appear
  in your reports, the tags on your notes, and the date ranges covered
- the result of every tool call the model makes — HealthKit values, lab results
  with reference ranges, and **verbatim excerpts from your medical records and
  personal notes**

That last item is the one people underestimate. A question about a single lab
value can pull note text into the request, and you will not see what was sent
until after it was sent.

**Not sent:** your source files. The PDFs, notes, and export stay on disk.

**What happens to it there** is governed by Anthropic's terms, which this
document links rather than characterizes, because this project cannot make
promises on another company's behalf:

- https://www.anthropic.com/legal/commercial-terms
- https://privacy.anthropic.com/

**Consent.** The full disclosure is printed before the first cloud query and you
must type `yes` — not `y`, and not a bare flag. The record is stored per data
folder and carries the version of the notice you agreed to, so if what gets sent
ever changes, you are asked again rather than inheriting a "yes" you gave to a
different question. A corrupt or unreadable consent record is treated as *no
consent*.

```bash
health-agent cloud-consent               # what you agreed to, and when
health-agent cloud-consent --show-notice
health-agent cloud-consent --revoke
```

**The tier is always displayed.** Every `ask` prints `[local]` or
`[CLOUD — data is sent to Anthropic]`, and `--json` records the tier on the
answer. You should never have to remember which flag you typed to know where
your data went.

---

## What this tool does not protect you from

Stating these plainly is the point of the document.

- **Disk access.** The index is not encrypted. Anyone who can read your data
  folder can read your health data, exactly as they could read the source files.
  Use full-disk encryption; this tool does not add a second layer.
- **Backups and sync.** If your data folder is inside iCloud Drive, Dropbox, or
  a Time Machine target, your index goes wherever that goes. This tool cannot
  see that and does not warn you.
- **A compromised machine.** Nothing here defends against malware, a keylogger,
  or another user with your account.
- **Ollama's behavior.** A separate program with its own threat model.
- **Your own cloud use.** Once you opt into `--cloud`, the data is transmitted.
  Local-tier guarantees do not extend to it, and no setting in this tool can
  reach across that boundary.
- **Prompt injection through your own documents.** A malicious PDF could in
  principle contain text aimed at the model. The tools only ever read your data
  and the model has no ability to write anywhere or make network calls of its
  own, which bounds the damage — but the ingested text is not sanitized.
- **The medical guardrail being complete.** It is a pattern check behind a
  system prompt, documented as best-effort in `agent/guardrail.py` and
  demonstrated to be incomplete by a test that shows a paraphrased diagnosis
  slipping through.

---

## Accidental commits

The repo ships a `.gitignore` covering health-data paths and a pre-commit hook
that blocks a commit containing anything that looks like health data — an
`export.xml`, a `.index/`, a records folder, a consent record. Install it with:

```bash
./scripts/install-hooks.sh
```

The hook is a convenience, not a guarantee: it can be bypassed with
`--no-verify`, and it only sees what you stage.

---

## Reporting a problem with any of this

If a claim here is wrong, that is the most serious kind of bug this project can
have — more serious than a wrong lab value, because it is a claim people would
rely on. Open an issue describing what you observed and how to reproduce it.
