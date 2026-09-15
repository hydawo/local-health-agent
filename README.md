# health-agent

Ask questions about your own health data (Apple Health exports, bloodwork PDFs,
medical records, personal notes) with **the data and the model both staying on
your machine**.

```bash
health-agent ask "How has my LDL changed since last year?"
```

```
Your LDL cholesterol has come down across your two most recent panels:

| Date       | LDL (mg/dL) | Reference | Flag | Source                    |
|------------|-------------|-----------|------|---------------------------|
| 2025-09-12 | 128         | 0–99      | H    | labs_2025-09-12.pdf, p.1  |
| 2026-03-10 | 112         | 0–99      | H    | labs_2026-03-10.pdf, p.1  |

Both are flagged high by the lab. The most recent is 16 mg/dL lower than the
previous one but still outside the printed range.

These are the values recorded in your own files, not medical advice.
```

No account, no upload, no server. A local model reads a local index and cites the
file and page behind every number.

![health-agent ingesting an export, answering questions, and proving it never touched the network](docs/demo.gif)

*Recorded from `demo/demo.sh` against the synthetic fixtures. You can run the
same thing, and regenerate it with `vhs demo/demo.tape`.*

**Why this exists.** Health assistants generally ask you to send your medical
records somewhere. This one is an argument that you don't have to. It is also an
argument that the claim should be *checkable* rather than asserted. `health-agent offline-check`
runs the entire pipeline inside a kernel-level sandbox with networking denied and
shows you the result. [`THREAT_MODEL.md`](THREAT_MODEL.md) states every claim,
how to verify it, and what this tool does **not** protect you from.

> **Status: v0.1, feature-complete for its scope.** All three source types
> ingest, natural-language questions work end to end, accuracy is measured
> against a 20-question eval set, and the privacy claims are falsifiable.
> Post-release directions are in [`ROADMAP.md`](ROADMAP.md). The largest is
> literature grounding with evidence-graded citations.
>
> Not a medical device, and not a substitute for your clinician. See
> [Not a medical device](#not-a-medical-device).

## What works today

Ask in plain language, or query directly. The structured commands are instant
where `ask` takes 25 to 60 seconds. Both read through the same code path, so
they cannot disagree.

```bash
health-agent ask "My notes mention a vitamin D result. What did the lab say?"
health-agent ask "Anything in my recent labs I should ask my doctor about?" --show-tools
```

Setup and inspection:

```bash
health-agent ingest ~/Downloads/export.zip     # export.xml, export.zip, PDFs, or a folder
health-agent doctor                            # what's installed, what's missing
health-agent stats
```

HealthKit metrics:

```bash
health-agent metric resting-hr --by week
health-agent metric steps --from 2026-03-01 --to 2026-03-31
health-agent metric sleep --by day
health-agent workouts
```

Bloodwork and records:

```bash
health-agent documents
health-agent labs                     # every analyte found across your reports
health-agent labs ldl                 # one analyte over time, with citations
```

Notes, and search across everything:

```bash
health-agent notes                    # what's ingested, with dates and tags
health-agent notes --tags             # every tag
health-agent notes --tag followup
health-agent search "vitamin d"       # notes and records in one ranked list
health-agent search "coffee" --kind note
```

Everything above is local computation over a SQLite index and a file-based
vector store. The only process that ever opens a socket is embedding, and only
to Ollama on localhost (see [Privacy](#privacy)).

## Try it without your own data

The repo ships synthetic fixtures, invented values in the real formats, so you
can exercise the whole pipeline before pointing it at anything personal. The
demo runs entirely against those:

```bash
./demo/demo.sh          # add --fast to skip the steps that need a local model
```

It ingests an export, lab PDFs and notes, then walks through the queries below
and finishes by proving nothing touched the network. Real output from it:

```text
$ health-agent labs ldl
LDL cholesterol (ldl)

collected   value  unit   reference  flag  source
----------  -----  -----  ---------  ----  ------------------------------------
2025-09-12  128 *  mg/dL  0-99       H     labs_2025-09-12.pdf, p.1, 2025-09-12
2026-03-10  112 *  mg/dL  0-99       H     labs_2026-03-10.pdf, p.1, 2026-03-10

* outside the reference range printed on that report

first 128 -> latest 112 (down 16, 12.5%)

These are the numbers as printed on your reports. Interpretation is for you
and your clinician.
```

```text
$ health-agent labs hba1c
collected   value  unit  reference  flag  source
----------  -----  ----  ---------  ----  ------------------------------------------
2025-03-04  5.9 *  %     4.8-5.6    H     scan_2025-03-04.pdf, p.1, 2025-03-04 [OCR]
2025-09-12  5.7 *  %     4.8-5.6    H     labs_2025-09-12.pdf, p.1, 2025-09-12
2026-03-10    5.4  %     4.8-5.6    -     labs_2026-03-10.pdf, p.1, 2026-03-10

[OCR] read from a scanned page with no text layer. OCR misreads digits — a
reference range of 100-199 can arrive as 100-139 — so check these against
the original before relying on them.
```

Three reports from three different labs, naming the analyte three different
ways, merged into one trend. The scanned one is marked as less trustworthy
rather than silently mixed in.

To regenerate the demo GIF (`demo/demo.tape` is checked in, so the recording
is reproducible):

```bash
brew install vhs && vhs demo/demo.tape
```

## Install

Requires Python 3.11+.

```bash
pip install -e ".[dev]"
```

Two optional pieces, both of which the tool works without and tells you about:

- **Tesseract**, only needed for scanned records with no text layer.
  `brew install tesseract` (macOS) or `apt install tesseract-ocr` (Debian).
- **Ollama**, needed for `ask` and for semantic search. Without it, `search`
  falls back to keyword matching and says so, and everything else still works.
  ```bash
  ollama pull qwen3.6:27b && ollama pull nomic-embed-text
  ```
  The 27B model needs roughly 18 GB of VRAM. Override with `--model` or
  `$HEALTH_AGENT_MODEL`.

Run `health-agent doctor` to see what's present. Packaging is pip-from-source
only for now; PyPI and Homebrew are deferred.

## Where your data lives

```
~/HealthData/                 <- you control this; nothing is written outside it
├── healthkit/export.xml
├── records/*.pdf             <- bloodwork, medical records
├── notes/*.md                <- personal notes, markdown or plain text
└── .index/
    ├── health.db             <- SQLite: records, labs, aggregates, chunk text
    └── vectors/              <- LanceDB: embeddings
```

Both halves of the index are generated and disposable. `health-agent reset`
deletes them and `ingest` rebuilds from your source files. Override locations
with `--data-dir`, `--index`, or `$HEALTH_AGENT_DATA_DIR`.

## Measured performance

Numbers from an actual Apple Health export. Query latency for
natural-language questions will be added once the model is in the loop.

| | |
| --- | --- |
| Export size | 3.7 GB, 8.3M records, 75 HealthKit types, 41 recording sources |
| Ingest | 6.5 min to parse, 6.8 min wall including hashing and rollup |
| Peak memory | ~1.4 GB, almost all of it SQLite's page cache. The XML parse itself holds flat at ~13 MB regardless of file size |
| Index size | 4.5 GB |
| `stats` | 0.13 s |
| `metric steps --by month` | 0.07 s |

Aggregate queries stay in the tens of milliseconds because they read a
materialized daily rollup (100k rows) rather than the raw record table.

## Architecture, and where the data stops

Everything inside the dashed box happens on your machine. The only two arrows
that cross it are the local model (loopback, enforced) and the opt-in cloud tier
(consent-gated).

```mermaid
flowchart TB
    subgraph disk["Your data folder (you control this)"]
        HK["healthkit/export.xml"]
        REC["records/*.pdf"]
        NOTE["notes/*.md"]
    end

    subgraph machine["YOUR MACHINE"]
        subgraph ingest["Ingest (no network, in any tier)"]
            P1["healthkit.py<br/>stream-parse 8M+ records"]
            P2["records.py<br/>pdfplumber → OCR fallback"]
            P3["notes.py<br/>frontmatter + headings"]
        end

        subgraph store["Local index (derived, disposable)"]
            SQL[("SQLite<br/>records · labs · chunks<br/>daily aggregates")]
            VEC[("LanceDB<br/>embeddings")]
        end

        Q["store/queries.py<br/>the single read contract"]
        CLI["CLI<br/>metric · labs · sleep · search"]
        TOOLS["agent/tools.py<br/>4 tools, capped results"]
        LOOP["orchestrator.py<br/>iterative tool loop"]
        GUARD["guardrail.py<br/>output check + disclaimer"]
    end

    OLLAMA["Ollama<br/>loopback only"]
    CLOUD["Anthropic API"]

    HK --> P1
    REC --> P2
    NOTE --> P3
    P1 & P2 & P3 --> SQL
    SQL --> VEC
    SQL --> Q
    VEC --> Q
    Q --> CLI
    Q --> TOOLS
    TOOLS <--> LOOP
    LOOP --> GUARD
    LOOP <-.->|"default tier"| OLLAMA
    LOOP <-.->|"--cloud, after consent"| CLOUD

    style machine fill:#f6f8fa,stroke:#444,stroke-dasharray: 6 4
    style disk fill:#eef6ee,stroke:#3a3
    style CLOUD fill:#fdecea,stroke:#c33
    style OLLAMA fill:#eef3fb,stroke:#36c
    style GUARD fill:#fff8e1,stroke:#b8860b
```

Three things the diagram is making explicit:

**Source files are read, never written.** The index is derived and disposable.
`reset` deletes it, `ingest` rebuilds it.

**`store/queries.py` is the only read path.** The CLI and the agent's tools call
the same functions, so `health-agent labs ldl` and asking the model about LDL
cannot disagree. There are not two implementations that happen to match; there
is one.

**Ingest never touches the network in either tier.** That is the claim
`offline-check` proves at the kernel level.

## How the agent works

There is no orchestration framework. The tool-calling loop is about sixty lines in
[`agent/orchestrator.py`](health_agent/agent/orchestrator.py), talking directly
to Ollama's chat API. Three tools (`query_healthkit`, `get_lab_trend`,
`search_records`) are thin adapters over the same `store/queries.py` functions
the CLI uses, so the model and `health-agent labs` cannot report different
numbers for the same question. A fourth, `search_medical_literature`, returns
dated, evidence-tiered findings from the optional literature corpus (below)
instead of letting the model answer clinical questions from its training data.

Three decisions in that loop came out of measuring the model:

**The loop is iterative, not single-shot.** Asked to compare two sources,
`qwen3.6:27b` requests one tool, and only asks for the second after seeing the
first result. A single-shot design would answer such questions with half the
data and no sign anything was missing.

**Thinking mode is off by default.** Enabling it tripled latency (17 s to 51 s on
a tool-call decision) and produced a byte-identical tool call. `--think` turns it
on when a question is worth the wait.

**The model never does arithmetic.** Every tool result includes a precomputed
`overall` figure for the requested range. Before that existed, asked for an
average resting heart rate, the model summed seven daily values by hand and
answered 59.6 where the answer is exactly 60.0. A wrong number delivered
fluently is this project's worst failure mode, so any figure the model might
otherwise compute is handed to it already computed.

**Tools batch, because the model won't.** Asked which results were abnormal, the
model looked up all 23 analytes one at a time. That was 23 tool calls and six minutes. The
lab tool now takes a list and precomputes the out-of-range set, which turned that
into a single call.

Tools also return *absence* as data, coverage ranges rather than an empty list.
A model told only "no results" will reach for the nearest numbers it can
see and present them as an answer.

Measured on an M5 with `qwen3.6:27b`: 25-60 s for a focused question, ~165 s for
one that sweeps every lab analyte. An answer that used no tool at all is flagged
as ungrounded rather than presented as though it came from your data.

## Medical literature corpus

`search_medical_literature` answers questions like "what does the research say
about X" from a **local, curated corpus of MEDLINE citations**, dated,
evidence-tiered, and cited by PMID, instead of the model's training data.

```bash
health-agent literature build --from medline_export.xml --slug cardiometabolic
health-agent literature status
```

A few things worth stating plainly:

- **The corpus is optional.** Nothing else in this tool depends on it. Without
  one, `search_medical_literature` reports that no corpus is installed and
  tells the model not to answer from recalled medical knowledge instead of
  quietly falling back on it. That is the exact failure this tool exists to
  close (see [Not a medical device](#not-a-medical-device)).
- **`health-agent literature build`** is what creates one, from a MEDLINE XML
  export you supply. See [LICENSES.md](LICENSES.md) for what's retained and
  under what terms.
- **Evidence tiers come from publication metadata, not judgement.** A tier is
  read off MEDLINE's own `PublicationType` field — meta-analysis and
  systematic review rank above RCT, which ranks above observational and case
  report — or it is `unknown`. Nothing inspects a title, an abstract, or asks a
  model to guess; an inferred tier would be a fabricated credential, and the
  feature's whole value is that its citations can be trusted.
- **Slice 1 ships no way to fetch a corpus over the network.** `literature
  build` reads a MEDLINE XML file you already have; there is no `update` or
  `fetch` command, and no code path in this release opens a socket to NCBI or
  anywhere else. Network acquisition, with its own licensing and consent
  requirements, is the next release. See [ROADMAP.md](ROADMAP.md) #1.

## Design notes worth knowing

**Aggregation follows HealthKit semantics.** Cumulative types (steps, energy) are
summed over a period; discrete types (heart rate, weight) are averaged; category
types (sleep) are totalled by duration. Getting this backwards produces
confident, wrong numbers. "Average steps per day: 41" is an averaged sample
count, not a daily total. Override with `--agg`.

**Multiple devices recording the same metric are not summed.** If an iPhone and
an Apple Watch both log step counts, adding their totals roughly doubles your
day. The default reports the largest single source's total and tells you when
more than one source contributed. Use `--source NAME` to pin one, or
`--combine sum` if you know the sources don't overlap.

**Re-ingesting is safe.** Every Apple export contains your full history, so
records carry a content hash and a second ingest inserts nothing new.

**Timestamps keep their own UTC offset.** A sample recorded at 22:30 on a
`-0500` evening belongs to that calendar date, not to the following UTC day, and
the offset changes across DST boundaries within a single export.

**Lab values are extracted twice and stored once.** Reports are laid out either
as ruled tables or as whitespace-aligned columns, and real reports mix both on
one page. Both extraction passes run; a content hash means a value found by both
is stored once. Every stored value keeps the source line it came from.

**Nothing is interpreted.** `labs` marks a value as outside the reference range
*the lab printed on that report*, and mixed units across labs are shown as
printed and never converted. Converting without knowing the assay is how you get
confidently wrong numbers.

**OCR-derived values are marked.** Scanned reports with no
text layer go through OCR, which reads result values and H/L flags reliably but
degrades on units and reference ranges. A range of `100-199` can arrive as
`100-139`. Those values are stored and shown with an `[OCR]` marker and a note
to check them against the original.

**Notes are never mined for lab values.** "My LDL was 112" in a journal entry is
recollection, not a lab result. Letting prose write into the lab table would put
uncited numbers into trends that are supposed to be traceable to a report.

**Notes cite headings; PDFs cite pages.** A note has no page numbers, so its
chunks carry the heading trail they came from and cite as
`sleep-log.md, Sleep log > Night by night, 2026-03-11`.

**Undated notes stay undated.** A note with no date in its frontmatter or
filename is reported as undated rather than dated from file mtime. Copying a
folder rewrites mtimes and would silently re-date years of notes.

## Two tiers, and the difference is not cosmetic

**Local (default).** The model runs on your machine through Ollama. Nothing
leaves the host. This is the product; it needs no flags.

**Cloud (`--cloud`, opt-in).** Answers come from Anthropic's API. Your question,
a description of what your index contains, and **the results of every tool call,
including verbatim excerpts from your records and notes**, are transmitted
per request. Your source files stay on disk.

That second tier is **hybrid, not local**, and this README will not call it
anything else. Before the first `--cloud` query the tool prints a full
disclosure and requires you to type `yes`. Not `y`, and never a silent flag.
Consent is recorded per data folder, and re-requested if what gets sent ever
changes.

```bash
health-agent cloud-consent              # what you agreed to, and when
health-agent cloud-consent --show-notice
health-agent cloud-consent --revoke
```

The cloud tier is an optional install (`pip install -e ".[cloud]"`). Someone who
never opts in has no vendor SDK and no HTTP client on their machine at all.

## Privacy

The design commitment (plan §5): the default path makes no outbound network
calls, and the cloud tier is an explicit, clearly flagged opt-in rather than a
silent fallback.

**Exactly two modules can reach the network**, and the split is the point:

| Module | Tier | Talks to |
| --- | --- | --- |
| `ollama_client.py` | local | Ollama on loopback, on-device inference |
| `agent/backends.py` | cloud | Anthropic, only after recorded consent |

The local one is enforced, not assumed: a non-loopback Ollama host is refused
unless you explicitly set `HEALTH_AGENT_ALLOW_REMOTE_OLLAMA=1`, because pointing
this at someone else's inference server would quietly turn the local tier into a
hybrid one.

A test (`test_the_network_surface_is_exactly_two_modules`) fails if that set
ever changes, **and it counts the vendor SDK**, because `import anthropic`
opens sockets just as surely as `import urllib`. A check that only looked for
stdlib transports would have passed while the cloud backend shipped data off the
machine. Ingestion, storage, aggregation, and search remain pure local
computation in both tiers.

### Prove it yourself

```bash
health-agent offline-check
```

This runs the real pipeline (ingest, OCR, aggregation, lab trends, vector
search, and every agent tool) inside a sandbox with network access denied, and
tells you **which enforcement actually applied**:

```
  enforced  OS sandbox — the kernel denies every socket syscall, including
            from native code

  [ok  ] ingest healthkit export   25 records
  [ok  ] ingest record PDFs (incl. OCR path)   3 documents
  [ok  ] vector search (LanceDB)   5 hits
  [ok  ] agent tool: search_records   5 results

PASS — the whole local pipeline ran to completion with every network syscall
denied at the kernel level.
```

The two mechanisms are reported separately because they cover different
things. A pass under the kernel sandbox (`sandbox-exec`, `unshare`) covers
native code like LanceDB's Rust. A pass under the Python-level fallback covers
only what goes through Python. A tool whose claim is "verify this yourself"
shouldn't blur the two.

The same proof runs in CI on Linux and macOS, plus a job that fails if any
health-data-shaped file is ever committed.

**Keep your own data out of git:**

```bash
./scripts/install-hooks.sh
```

[`THREAT_MODEL.md`](THREAT_MODEL.md) states every claim, how to check it, and,
just as importantly, what this tool does *not* protect you from.

The vector store is LanceDB rather than Chroma partly for this reason: Chroma
installs 76 packages including an OpenTelemetry OTLP exporter and a Kubernetes
client, and its `anonymized_telemetry` setting defaults to on. LanceDB installs
15, runs as a library against a local directory with no server, and has no
telemetry on by default.

The fuller, falsifiable version of all this (`THREAT_MODEL.md`, a sandboxed
no-network test in CI, and a pre-commit hook keeping health data out of git)
lands at milestone 9.

## Accuracy

`tests/eval_questions.md` holds 20 questions with hand-verified answers spanning
all three sources. There are two ways to run them:

```bash
pytest tests/test_eval.py -v          # against the tool layer: fast, deterministic
```

```bash
python tests/run_agent_eval.py --index ~/HealthData/.index/health.db
```

The second runs them through the model and scores each answer on whether the
number is right, a source is cited, a data gap is named when one exists, and
nothing diagnostic was said. Release run (`qwen3.6:27b`, thinking off, 18.9 min):

| Correct number | Cited | Source file named | Gap named | No diagnosis | Right tools |
| --- | --- | --- | --- | --- | --- |
| 20/20 | 18/20 | 16/20 | 20/20 | 20/20 | 20/20 |

The medical guardrail fired on 0 of the 20. The no-diagnosis score is the
prompt's doing, not the guard's, which is the point of measuring them separately.

The four that miss `source file named` cite the note title or the report date
instead of the filename, which the tool did supply. That is a locator the reader
can still follow, and it is left unfixed on purpose: prompting until this exact
phrasing passes would be fitting the model to the scorer. Full history, and what
each run caught, in [`tests/eval_results.md`](tests/eval_results.md).

The unit suite (325 tests, `pytest`) and the eval set measure different things,
which is why both exist. The first eval run scored 17/20 on numbers and exposed
two defects that the 239 unit tests passing at the time had missed. Both were
failures of the *tool contract* rather than of any function in it. Five of the
twenty questions are multi-source, and two have
"I don't have that data" as part of the correct answer, which is the hardest
thing to get a model to say, which is why it is measured.

Logs contain metadata only: filenames, counts, error types. Never values, never
query text.

## Not a medical device

This tool describes your own recorded data. It does not diagnose, interpret, or
recommend treatment.

That is enforced in two layers (plan §5). The system prompt instructs the model
to report values, the flags and reference ranges the lab printed, and what your
own notes say, and to send interpretation to your clinician. Behind it,
[`agent/guardrail.py`](health_agent/agent/guardrail.py) pattern-checks the
finished answer; if it reads as diagnostic or prescriptive, the model is asked
once to restate it without interpretation. If that fails the answer is
shown with a visible note. Answers that
touch lab values carry a standing disclaimer.

**The second layer is best-effort, not a guarantee, and the code says so.** A
regex does not understand a sentence. The test suite includes a case
demonstrating a paraphrased diagnosis that slips through, kept deliberately so
the limitation stays visible.

The patterns are narrow on purpose, because the expensive failure is the false
positive: "your LDL is high" restates the lab's own flag and must pass, while
"you have high cholesterol" must not. Roughly half the guardrail tests are
phrases that must **not** trip it. A guard that fires on ordinary reporting
gets switched off, and then it protects nothing.

The failure mode actually observed in 40 eval answers was the opposite of
diagnosis: asked whether anything in the labs was worth raising with a doctor,
the model once declined to look anything up at all. Over-caution withholds
information that is already yours, so the guard flags that too.

### A known hole, and what closed part of it

Adversarial probing found a leak the diagnosis/treatment pattern check could
not close. Asked "do I have prediabetes?", the model correctly refused to
diagnose, and then volunteered that clinical definitions "often cite specific
thresholds (e.g., an A1c of 5.7% to 6.4%)". That figure came from its training
data, not from your reports: uncited, undated, and impossible for a regex
looking for diagnostic *phrasing* to distinguish from sourced text, because
nothing about the sentence reads as a diagnosis. The system prompt forbade
quoting clinical thresholds from memory, but a prompt rule is not enforcement.

That gap is why the medical literature corpus above exists, and it is now
partly closed. `search_medical_literature` gives the model dated,
evidence-tiered citations to reach for instead of recalled thresholds, and the
guardrail gained a second check for exactly this shape of failure:
`uncited_medical_claim` fires when the finished answer states a general
clinical threshold or normal range (not a diagnosis, just a bare fact like "an
A1c of 5.7% to 6.4% is considered prediabetic") **and the turn's tool calls
returned no literature finding to back it.** The same sentence, backed by a
cited corpus finding, is a report of published evidence and passes. Unbacked,
it is recalled knowledge and gets flagged the same way a diagnosis does: one
rewrite pass, then a visible note if that fails.

**What is still genuinely open:**

- **The corpus is only as good as what has been built into it.** A threshold
  the installed corpus simply doesn't cover cannot be cited, and the tool says
  so. That is a coverage gap, not a fixed leak.
- **Slice 1 has no network fetch.** A corpus has to be built from a MEDLINE
  export you already have (`health-agent literature build`); there is no
  `update-literature` command yet, so keeping coverage current is manual. See
  [ROADMAP.md](ROADMAP.md) #1a.
- **The check is still a regex, with the same limits the rest of the guardrail
  states plainly** (see [`agent/guardrail.py`](health_agent/agent/guardrail.py)'s
  own docstring): it cannot understand a sentence, and it will miss a claim
  phrased in a way these patterns don't anticipate. It is a best-effort
  backstop behind the system prompt, not a guarantee, same as every other
  category this guardrail checks.

## What's next

[`ROADMAP.md`](ROADMAP.md) covers post-release directions. The one that unlocks
most of the others is **medical literature grounding**: a curated local corpus
with evidence-graded, dated citations, so the tool can say what published
research reports about a marker instead of the model recalling it. Retrieval,
tiering, and the `search_medical_literature` tool are built and described
above; what is not built is a way to acquire a corpus over the network without
you supplying the MEDLINE export yourself. That has a concrete motivation from
this build. Adversarial probing caught the model volunteering a clinical
threshold from its training data, uncited, undated, and invisible to the
guardrail's pattern check.

Two features build directly on it: **physician visit prep** (questions worth
raising with a clinician, grounded in your own trends) and **literature-grounded
context on out-of-range values** (what evidence says about a marker, surfaced as
citations, never as a recommendation). Both stay inside the
contextualize-don't-diagnose line, and the second carries an explicit design
review before implementation, because synthesis across findings can read as
advice without any single sentence crossing the line.

## Contributing

[`CONTRIBUTING.md`](CONTRIBUTING.md). The short version: work against the
synthetic fixtures, never real health data; install the pre-commit hook; and if
you touch anything the privacy claims rest on, the tests that enforce them are
not optional furniture.

## License

MIT. See [LICENSE](LICENSE).
