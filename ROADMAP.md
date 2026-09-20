# Roadmap

Directions for after the current release. **Nothing on this page is built, and
none of it is in scope for this version.** It is written down so the design
constraints are captured while they are fresh — several of these items have
guardrail implications that are much easier to state now than to reconstruct
later.

What *is* built is described in the [README](README.md); what the current
release claims and how to verify it is in [THREAT_MODEL.md](THREAT_MODEL.md).

Items are roughly in dependency order. The first one unlocks most of the rest.

---

## 1. Medical literature grounding (RAG, not fine-tuning)

**Slice 1 — shipped.** A `search_medical_literature` tool over a local,
curated corpus built from a MEDLINE XML export: keyword and semantic
retrieval, evidence tiers read from `PublicationType` (never inferred), dated
citations, retraction flagging, and a `min_tier`/`since_year` filter. Covered
by the eval set's Q21-Q26 and by the guardrail's `uncited_medical_claim`
check. See the README's [Medical literature corpus](README.md#medical-literature-corpus)
section and [LICENSES.md](LICENSES.md) for what a built corpus contains and
under what terms.

**Measured after slice 1, against 2,000 real abstracts** (recorded in
[docs/superpowers/specs/2026-09-15-literature-follow-ups-design.md](docs/superpowers/specs/2026-09-15-literature-follow-ups-design.md)):
the corpus is 15 KB per article all-in, so the 24k-article
meta-analyses-and-systematic-reviews scope is about 370 MB and the 61k scope
adding guidelines and RCTs about 920 MB; the unfiltered query would be
415k articles and 6.3 GB. 65.5% of articles have no recorded study design.
Those numbers produced three changes: the tool now reports what a `min_tier`
floor excludes, four more publication types are mapped (with trial protocols
overriding rather than inflating), and the write-only abstract column is
gone behind a schema bump and a `--rebuild` flag.

**One assumption from the original design turned out wrong:** Cochrane is not
a separate source. It has no open API of its own — Cochrane systematic reviews
are indexed in PubMed/MEDLINE like any other article, under the journal name
`Cochrane Database Syst Rev`. "Cochrane first" is therefore a filter on
PubMed results (by journal, or by the `systematic_review` tier plus that
journal name), not a second integration to build. The corpus module has one
ingestion path, not two.

**Network acquisition shipped separately, as packs; see #1a below.** Slice
1 read a MEDLINE XML file you already had and had no code path that reached
NCBI. RAG-vs-fine-tuning, bounded scope, and meta-analyses-first were slice 1
design calls, and the packs kept all three:

- **RAG, not fine-tuning.** Fine-tuning is expensive, needs real ML infra, risks
  degrading tool-calling through catastrophic forgetting, would have to be
  redone on every model swap — and cannot be cited or dated. RAG keeps facts
  external, sourced, and current.
- **Bounded scope**, not "all of PubMed": a defined MeSH-term list covering the
  domains this tool already holds data for — cardiometabolic health, sleep,
  exercise physiology, common lab panels, preventive medicine. Expanded from
  real questions, not preemptively.
- **Meta-analyses and systematic reviews first.** The closest thing biomedical
  literature has to synthesized ground truth, and far more tractable in volume
  than primary research.

There is already an argument for this in the current build: adversarial probing
found the model volunteering an A1c threshold from its training data — uncited,
undated, and invisible to the guardrail's pattern check. Replacing recalled
medical knowledge with dated, evidence-graded citations is the actual fix, and
slice 1 is the half of it that works without ever opening a socket.

### 1a. Network acquisition for the literature corpus

*Depends on: #1 (slice 1). PubMed and ClinicalTrials.gov, fetched on request
rather than lazily — see 2a for why lazy fetching is the wrong shape.*

**Shipped for PubMed, in the packs shape** ([design](docs/superpowers/specs/2026-09-20-literature-packs-design.md)).
The corpus is not fetched per user from E-utilities. A maintainer runs
`health-agent literature build-pack <slug>` against NCBI once, publishes the
file to a GitHub release, and a person installs it with `health-agent
literature install <slug>`. Five packs, each one broad body-system area
(`sample`, `cardiovascular`, `metabolic`, `sleep`, `exercise`), abstracts and
citation metadata only. The one file that can open a socket under
`literature/` is `fetch/client.py`, and its allow list is two services. Both
commands ask once before connecting. `THREAT_MODEL.md` Claim 6 has the full
disclosure. Refresh is a person running `install` again; there is no update
check.

**Still open: ClinicalTrials.gov.** No pack draws on it, and nothing in the
fetch client knows its host. Carried forward, unchanged from the original
design, and now mostly done:

- **Dated citations**, so "a 2019 meta-analysis found…" reads honestly rather
  than as evergreen fact, refreshed by an explicit `update-literature` command
  rather than aging invisibly.
- **Evidence tiers on every citation** (meta-analysis / systematic review > RCT
  > observational > case report) rather than presenting all sources as equally
  weighted — already built in slice 1, unaffected by where the XML comes from.
- **Licensing is a real compliance line**, not an assumption: full text only
  from the PMC Open Access subset, and only per the license recorded on that
  article — see [LICENSES.md](LICENSES.md), which slice 1 already needed
  because the fixture corpus and any hand-supplied export raise the same
  question.
- **Privacy**: this is the first place literature grounding actually reaches
  the network. `THREAT_MODEL.md` now has Claim 6 for it. Query terms never
  leave a user's machine at all. The only terms sent to PubMed are the
  catalog's fixed MeSH lists, sent by the maintainer's `build-pack`. A user's
  `install` reveals a pack name, and the versioned consent notice says so
  before the first connection.

## 2. Medical intake: retired, and why

*Decided 2026-09-15: this tool will not have an intake feature.* An earlier
version of this item described a user-editable file or table of current
medications, allergies, and known conditions, held by the tool as standing
context. It is not going to be built, in that form or any other.

**The drop folder is the mechanism.** Anyone who wants the model to know
about their medications, their conditions, or their medical history puts a
file saying so into the local data folder, exactly as they already do with
lab reports and notes. It is indexed like any other document and reached by
`search_records`. The tool never asks for that information, never holds it
in a structured form of its own, and never treats it differently from the
rest of the folder. What exists, and in what words, stays the user's
decision.

Two things from the retired item survive, relocated:

- **The guardrail re-test still has to happen.** "Given my medications, what
  does this mean" is exactly as diagnosis-adjacent when the medication list
  came from a dropped-in text file as it would have been from a form. That
  re-test belongs to `search_records` over personal documents, and it should
  use fixture documents shaped like what people will actually drop in.
  Q27 in `tests/eval_questions.md` is the vehicle, against a dropped-in
  medication and conditions note; the agent-scored run is pending
  (`eval_results.md` has no run past Q20). The first known finding from it
  is a DIAGNOSIS-check false positive: the reporting-context lookback in
  `guardrail.py` applies only to the uncited-claim patterns, so quoting the
  person's own conditions line ("your note says you have high cholesterol,
  diagnosed in 2024") trips the check. The fix is to apply the lookback to
  DIAGNOSIS.
- **The privacy question in 2a shrinks** but does not vanish; see below.

### 2a. What a literature fetch reveals

*Depends on: #1a. Decided 2026-09-20, with the packs
([design](docs/superpowers/specs/2026-09-20-literature-packs-design.md)).*

The earlier framing of this item, seeding the corpus from the intake list, no
longer applies: there is no intake list to turn into queries. What remains is
smaller and can be settled on its own.

**Fetch at an explicit, consented moment, never lazily during a question.**
This part is unchanged and is the reason #1a is shaped as a command rather
than a cache miss. Queries are not retrieval-bound (answer time is almost
entirely token generation; vector search is milliseconds), so prefetching
buys no speed. What it buys is that no health question ever opens a socket,
which is what `offline-check` proves today and would stop proving under lazy
fetching.

**The remaining question was what a fetch request discloses, and packs
answered it.** A user's request goes to GitHub, not NCBI, and names a pack,
which is a body-system area and never a condition. That is the rule the catalog in
`health_agent/literature/packs.py` enforces, and adding a pack below that line
is a design change rather than a catalog edit. The options as they stood
before that decision, kept for the record:

- **Pre-built topic packs** downloaded wholesale (a "cardiometabolic" pack, a
  "sleep" pack), so a request reveals a category, not a person. The
  measurements recorded in
  [docs/superpowers/specs/2026-09-15-literature-follow-ups-design.md](docs/superpowers/specs/2026-09-15-literature-follow-ups-design.md)
  make this a requirement rather than a preference: an unbounded fetch is
  415k articles and 6.3 GB. Packs are also what makes the corpus versionable
  (#6) and reproducible across users, and the schema already carries pack
  identity.
- **User-typed topics**, sent as broad MeSH bundles rather than precise
  strings, for people who want something a pack does not cover. Honest
  about what it sends; the consent notice has to show the exact terms.
- Routing through a user-supplied proxy or Tor, which moves the problem
  rather than solving it and adds a dependency the project has so far
  avoided.

The first option is what shipped. User-typed topics were not built, and no
proxy or Tor routing was added. `THREAT_MODEL.md` Claim 6 is the claim this
paragraph promised, and the fetch step has the versioned notice
(`health-agent literature-consent --show-notice`).

**The synthesis risk does not go away without intake.** A dropped-in file that
says "type 2 diabetes" next to a diabetes literature pack makes accidental
advice as easy to produce as an intake table would have. #4's review still
has to happen before literature is surfaced proactively.

## 3. Physician visit prep — suggested questions to ask

*Depends on: nothing strictly, but much better with #1.*

Given an upcoming appointment (or on request), surface a short list of questions
worth raising with a clinician, grounded in the user's own data: a lab value
trending toward an out-of-range threshold, a HealthKit metric that has shifted
notably, an anomaly the user might not have noticed. Once literature grounding
exists, the suggestions can also carry relevant evidence.

**This stays inside the lane the guardrail already establishes.** The output is
*questions to ask a clinician* — never answers, never conclusions, never an
implied diagnosis. That framing is not incidental; it is what keeps the feature
on the right side of the line. "Ask your doctor whether your LDL trend warrants
follow-up" is in scope. "Your LDL trend warrants follow-up" is not.

Pairs naturally with #1 and with whatever the person has dropped into the
data folder: their own documents make the questions specific to the person,
and literature grounding makes them specific to the evidence. Without either,
it still works — just from trends and anomalies alone.

Worth noting that the current build already produces something adjacent to this
by accident: asked "is there anything in my recent labs I should ask my doctor
about?", the agent lists flagged values and closes by deferring interpretation.
This item is that behavior made deliberate, proactive, and grounded — not a new
capability so much as a promoted one.

## 4. Literature-grounded context on out-of-range lab values

*Depends on: #1 (slice 1, done). Pairs with #3.*

When a lab value is trending toward or outside its reference range, surface what
the literature says about factors associated with that marker — for example,
"your HDL is trending below the typical range; here is what recent evidence says
about factors associated with HDL levels," followed by cited, evidence-tiered
findings.

**The distinction this feature lives or dies on:**

> It surfaces evidence. It does not issue a recommendation.

"Evidence links X to outcome Y" is a citable fact about published research.
"You should try X" is a personalized treatment recommendation, and stays out of
scope under the existing non-diagnosis, non-treatment guardrail. The line is not
subtle in principle and is very easy to cross in practice.

**This item requires explicit review before implementation.** The specific risk
is not a single sentence crossing the line — the guardrail's pattern check would
likely catch a bare "you should take X." The risk is *synthesis*: a model
summarizing across several findings can produce something that reads as advice
without containing any phrase a pattern check would flag. Three findings about
factors associated with HDL, assembled into a paragraph, can amount to a
recommendation that no individual clause states.

That failure mode is already documented in the current build — the guardrail's
own test suite includes a paraphrased diagnosis that slips through — and this
feature would make it substantially more likely. Before building it:

- decide whether findings are presented individually or synthesized at all, and
  if synthesized, under what constraints
- extend the guardrail's test corpus with synthesis-shaped cases, not just
  single-sentence ones — **done**, pulled forward into slice 1:
  `uncited_medical_claim` and its test cases in `agent/guardrail.py`
- re-run the eval set with literature-grounded questions added, including
  adversarial ones designed to elicit advice from evidence — **partly done**:
  Q21-Q26 in `tests/eval_questions.md` add corpus-sourced questions, and Q24
  and Q26 are exactly this — adversarial cases built to elicit a recommendation
  out of attributed findings and check that none appears. What's still open is
  the *lab-triggered* re-run this item specifically needs: eval cases where the
  trigger is an out-of-range value surfaced automatically, once auto-surfacing
  exists, rather than a question the user typed.
- consider whether the output format itself should enforce the distinction —
  a structured list of cited findings resists reading as advice in a way that
  free prose does not — **done**, pulled forward: `search_medical_literature`
  already returns a structured findings list (title, text, citation, tier,
  year, retraction status as separate fields) rather than free prose, for
  exactly this reason.

**What's left for #4 itself** is the feature slice 1 didn't build: automatically
*surfacing* literature context when a lab value is trending toward or outside
its reference range, rather than only responding when asked. Slice 1's
`search_medical_literature` is reachable by the model on any question; #4 is
about triggering it proactively from an out-of-range flag, plus the
lab-triggered eval re-run noted above.

## 5. Answer provenance / reproducibility log

A per-answer record of exactly which tool calls and sources produced a response,
so an answer is auditable after the fact. A research-infrastructure pattern
rather than a typical consumer-AI feature, and worth building for that reason.

Partly exists already: `Answer.steps` records every tool call with its arguments
and timing, and `--json` exposes it. What is missing is persistence, and tying
an answer to the literature snapshot that existed when it was given.

## 6. Corpus versioning / changelog

*Depends on: #1.*

Track what was added or changed on each literature refresh, so an answer given
at one point in time is traceable against the corpus that existed then, rather
than the corpus being a silent, unversioned blob.

Pack versions are now the lineage unit. Every installed pack carries the
version it was built at, and `health-agent literature packs` lists them. A
query change or a parser fix means a new pack version rather than an edit to
an old one. The maintainer's build is the one point where the corpus changes.
The changelog itself is still missing, meaning a record per pack version of
what came in and what went out.

## 7. Model-agnostic local inference

A `--model` flag or config entry for any Ollama-compatible model, instead of the
single pinned default. Needs: documentation of the tool-calling capability a
model must have, a short list of tested models (including a "lite" option for
lower-spec hardware), and graceful handling of models whose tool-calling is
weak.

The groundwork is done — `agent/backends.py` already abstracts the model behind
a backend contract, which is what made the cloud tier a translation layer rather
than a rewrite.

## 8. Expanded HealthKit coverage

Richer workout and GPS data, and third-party exports (Oura, Whoop) for users who
want them folded in.

## 9. Better lab value extraction

Move from regex and table heuristics to a more robust structured extraction
pass, with confidence scores and a review step for ambiguous values. The
measured OCR accuracy on the current fixtures — values and flags perfect, units
and reference ranges degrading — is the concrete argument for this.

## 10. Lightweight TUI or local web UI

Richer than the CLI, still fully local, nothing exposed beyond localhost.

## 11. Multi-model comparison mode

Run one question against two models side by side — useful for choosing a model,
and a good demonstration of the backend abstraction.

## 12. Plugin-style ingestion

Let others add parsers for new sources — other wearables, other EHR export
formats — without touching core.

## 13. Apple-native acceleration (Core ML / Vision / Neural Engine)

Mac-only optimizations: Apple's Vision framework instead of Tesseract for OCR,
and a Core ML embedding model as an alternative to `nomic-embed-text`. The
reasoning model stays on Ollama regardless — Core ML is impractical at that size
and would forfeit the GPU and unified-memory advantages already in use.

**Any such path ships as an optional accelerated alternative, never a
replacement.** The cross-platform, verify-it-yourself pitch depends on Linux and
Windows users getting a fully working experience without it.

## 14. iPhone-native port

Blocked on Apple exposing only the ~3B on-device model to third-party
developers, not the larger AFM tier (which is itself hardware-gated). Until that
changes, a phone-native version would either reason poorly or call out to a Mac
or cloud backend — which would undercut the entire local-only claim.
