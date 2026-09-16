# Contributing

## Setup

Python 3.11+.

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

## Work against the fixtures, never against real health data

`tests/fixtures/` holds synthetic Apple Health data — invented values in
HealthKit's real format. Every test, every manual check, and every AI-assisted
iteration on this codebase runs against those files.

Do not add a test, script, or default that reads a real `~/HealthData/` folder.
Do not commit anything derived from real health data, including "just one
anonymized record" — the fixtures exist so that is never necessary.

If you need a new shape the fixtures don't cover (a record type, a malformed
case, a device combination), add it to `tests/fixtures/export.xml` with invented
values and document the expected aggregate in `tests/fixtures/README.md`.

For record PDFs, edit `tests/fixtures/generate_pdfs.py` and re-run it — the
generated PDFs are committed so the test suite doesn't need reportlab. Keep the
values invented; the point of these fixtures is that a realistic *format* is all
that's needed to test a parser.

## Expectations for a PR

1. **Tests pass**, and new behavior comes with a test.
2. **Hand-verifiable expectations.** Assertions on aggregates state the arithmetic
   they expect (see `tests/test_queries.py`); a test that just re-asserts whatever
   the code currently returns catches nothing.
3. **The eval set is re-run** (`pytest tests/test_eval.py`, and
   `tests/run_agent_eval.py` for anything touching prompts, tools, or the
   guardrail). Accuracy regressions are otherwise silent.
4. **Network calls stay in `ollama_client.py` (local) and `agent/backends.py`
   (cloud).** `test_the_network_surface_is_exactly_two_modules` asserts that
   exact set, counting vendor SDK imports as well as stdlib transports. A third
   network module is a design discussion, not a patch.
5. **No health data in logs.** Log filenames, counts, error types. Never values,
   never query text, never response text. See `health_agent/logging_setup.py`.

## Dependencies

The bar for adding one is high, and higher for anything that opens a socket or
ships telemetry. Current runtime dependencies are `pdfplumber` and `lancedb`;
the Ollama client is stdlib `urllib` specifically to avoid adding an HTTP
library. The agent layer deliberately does not use an orchestration framework —
the tool-calling loop is hand-rolled against Ollama's API (see plan §4 for why),
so please don't add LangChain, LlamaIndex, or similar.

## Project layout

```
health_agent/
├── ingest/            healthkit.py, records.py, notes.py
├── store/             sqlite_schema.py, queries.py, vector_store.py
├── agent/             tools.py, orchestrator.py, guardrail.py, backends.py
├── metrics.py         HealthKit metric registry: aliases + aggregation semantics
├── labs.py            analyte registry: aliases + expected units
├── embeddings.py      embedder contract and backends
├── consent.py         cloud-tier disclosure and consent record
├── ollama_client.py   network: local tier (loopback only)
└── cli.py
```

The network surface is `ollama_client.py` and `agent/backends.py`, nothing else.

## Working on the agent

Two rules that are easy to violate and expensive to debug:

**Tools return data, never prose.** Deciding what is *true* belongs to the tool
layer; phrasing and hedging belong to the model. A tool that returns a sentence
has taken a decision away from the model and hidden it from the tests.

**Never make the model do arithmetic.** If a question implies a number, the tool
must compute it and return it. The model summing values by hand is a real
observed failure — it produced 59.6 for an average that is exactly 60.0. Any new
tool that returns a series must also return the aggregate.

The loop itself is tested against a scripted fake model in `tests/test_agent.py`
(deterministic, fast, no Ollama needed). Whether answers are *good* is what the
eval set measures, not those tests.

`store/queries.py` is the read contract. The CLI uses it today and the agent's
tools will use the same functions, so both report identical numbers by
construction rather than by two implementations happening to agree.

## Schema changes

The SQLite index carries a version stamp (`SCHEMA_VERSION` in
`store/sqlite_schema.py`). Bump it whenever you change the schema — a mismatch
produces a clear "run `ingest --rebuild`" message, whereas an unbumped change
produces confusing failures against an existing index.

## The eval set

`tests/eval_questions.md` holds 27 questions with hand-verified answers, and
`tests/test_eval.py` executes them against the tool layer. This is the accuracy
signal, as distinct from the unit tests' correctness signal: a prompt change, a
retrieval change, or a model swap can leave every unit test green while making
answers worse.

Run it at the end of every milestone, not only at the end of the project:

```bash
pytest tests/test_eval.py -v
```

If you change a fixture, update the expected values in **both** files — a test
enforces that every documented question has a test and vice versa.

## The medical guardrail

`agent/guardrail.py` is the second layer behind the system prompt (plan §5). Two
rules when changing it:

**Add a must-not-trip case for every must-trip case.** Roughly half of
`tests/test_guardrail.py` is phrases that are correct answers and must pass.
False positives are the expensive failure: a guard that fires on "your LDL is
high" gets disabled, and then it protects nothing.

**Do not claim more than it does.** It is a pattern check. It cannot understand a
sentence, and there is a test asserting the docstring still says so, plus one
demonstrating a paraphrased diagnosis that gets through. Both are there to keep
the limitation visible; do not delete them to make the suite look better.

## The cloud tier

`agent/backends.py` holds both tiers. Three rules:

**Nothing reaches `CloudBackend` without consent.** The gate is in the CLI
(`_confirm_cloud`), and `consent.py` fails closed — an unreadable record means
ask again, never assume yes. If you add another entry point to the cloud tier,
it takes the same gate.

**The network-surface test counts vendor SDKs.** `import anthropic` opens
sockets as surely as `import urllib`. `test_the_network_surface_is_exactly_two_modules`
asserts the exact set; if you add a third network module, that is a design
discussion.

**`anthropic` is imported lazily, inside `CloudBackend.__init__`.** The local
tier must run with the `[cloud]` extra uninstalled, and a test enforces the lazy
import.

Note for anyone updating the Anthropic call: this model family **rejects**
`temperature`, `top_p`, and `top_k` with a 400, and a refusal arrives as a
successful response with `stop_reason: "refusal"` and empty content — check it
before reading `content[0]`. Both are covered by tests.

## Privacy claims are load-bearing — verify, don't assert

Install the hook before your first commit:

```bash
./scripts/install-hooks.sh
```

Three checks enforce the claims in `THREAT_MODEL.md`, and all three are written
to be *precise*, because a check with false positives gets relaxed until it
means nothing:

| Check | Enforces |
| --- | --- |
| `test_the_network_surface_is_exactly_two_modules` | Only `ollama_client.py` and `agent/backends.py` import networking, vendor SDKs included |
| `tests/test_no_network.py` | The whole pipeline runs with sockets blocked, plus an OS-sandbox proof where available |
| `health-agent offline-check` | End-to-end proof, reporting which enforcement mechanism applied |

Two lessons already learned the hard way, so you don't repeat them:

**Match imports, not words.** An earlier version of the surface test scanned for
the bare string `socket` and flagged `cli.py` for the phrase "socket syscall" in
a help message.

**AF_UNIX is not network.** LanceDB's async runtime opens a `socketpair()` to
wake its event loop. The socket guards allow local families and block AF_INET —
the same line the OS sandbox draws. Blocking everything makes the test fail for
reasons unrelated to the claim.

If you change what the cloud tier sends, bump `NOTICE_VERSION` in `consent.py`.
That invalidates recorded consent and re-asks, because the old yes answered a
different question.
