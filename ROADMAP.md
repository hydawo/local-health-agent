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

**One assumption from the original design turned out wrong:** Cochrane is not
a separate source. It has no open API of its own — Cochrane systematic reviews
are indexed in PubMed/MEDLINE like any other article, under the journal name
`Cochrane Database Syst Rev`. "Cochrane first" is therefore a filter on
PubMed results (by journal, or by the `systematic_review` tier plus that
journal name), not a second integration to build. The corpus module has one
ingestion path, not two.

**Not shipped — network acquisition, moved to its own item, #1a below.** Slice
1 reads a MEDLINE XML file you already have; there is no `update-literature`
command and no code path that reaches NCBI. RAG-vs-fine-tuning, bounded scope,
and meta-analyses-first were slice 1 design calls and remain the plan for
slice 2's fetch step:

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

The half of #1 that slice 1 deliberately left out: a command that reaches
PubMed's E-utilities (and, later, ClinicalTrials.gov) to build or refresh a
corpus, instead of requiring a MEDLINE XML export supplied by hand. Carries
forward, unchanged from the original design:

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
  the network. `THREAT_MODEL.md` gains a claim here — it correctly has none
  yet, because slice 1 adds no network call. Query terms sent to PubMed are a
  new disclosure surface and need the same explicit, versioned consent
  treatment the cloud tier already has, not a silent default-on fetch.

## 2. Basic medical intake (medications, allergies, conditions)

A user-editable local file or table capturing current medications, allergies,
and known conditions, treated as **standing context injected into relevant
queries** rather than a separate retrievable tool — the value is in
personalizing what other sources return, not in being queried on its own.

**This pushes closer to clinical territory than anything else here.** "Given my
medications, what does this mean" is materially more diagnosis-adjacent than the
trend questions the guardrail was built and tested against, so the non-diagnosis
constraint needs re-testing specifically against intake-informed queries before
this ships.

Intake is also the natural seeding signal for the literature corpus — see 2a,
which has a privacy question that must be settled before either item is built.

### 2a. Seeding the literature corpus from intake

*Depends on: #1a and #2. Decide this before building either.* (Slice 1's
retrieval half is already built and needs nothing decided here; what this item
gates is the network fetch in #1a.)

Rather than fetching literature lazily when a question needs it, seed the local
corpus at intake: someone who records type 2 diabetes, metformin, and an ACL
reconstruction gets those topic areas pulled once, up front, forming a starting
library that grows as real questions require more.

**The reason to do this is offline integrity, not speed.** Worth stating
plainly, because the intuition is that prefetching makes queries faster and it
does not:

- Queries are not retrieval-bound. In the release eval, answers took 20-171s and
  essentially all of it was token generation on a 27B model. Vector search over
  a local corpus is milliseconds. Pre-warming saves nothing measurable.
- What it actually buys is that **no health question ever triggers a network
  call.** A lazily-fetched corpus phones out mid-query, which is precisely what
  `offline-check` currently proves cannot happen — it would turn the headline
  claim into "local, except when it isn't." Seeding at an explicit, consented
  moment (intake, or an `update-literature` command) keeps every actual question
  fully local.
- Secondary benefit: a corpus bounded to the person's real conditions has less
  competing material in the embedding space than a general one. Intake is a
  better seeding signal than #1's current "expanded from real questions."

**The blocking design question is a privacy inversion.** The intake list is the
most sensitive data in the system — more revealing than any single lab value.
Turning it into literature queries means transmitting a reconstructable medical
profile to NCBI, keyed to the user's IP, in a feature that presents itself as
local-first. That is a worse leak than anything the current build does, and it
arrives wearing a privacy badge.

Options, to be chosen deliberately rather than defaulted into:

- **Pre-built topic packs** downloaded wholesale (a "cardiometabolic" pack, a
  "post-surgical rehab" pack), so a request reveals a category rather than a
  profile. Currently the most promising: it also makes the corpus versionable
  (#6) and reproducible across users.
- Broad topic bundles instead of precise query strings, accepting a larger
  corpus for a vaguer request.
- Batching real topics with decoys, which is weaker than it sounds and worth
  treating as a fallback rather than a plan.
- Routing through a user-supplied proxy or Tor, which moves the problem rather
  than solving it and adds a dependency the project has so far avoided.

Whichever is chosen, `THREAT_MODEL.md` gains a claim, and the fetch step needs
the same consent treatment the cloud tier already has — a versioned notice that
says exactly what leaves the machine.

**It also compounds the guardrail problem.** Holding both "this person has type
2 diabetes" and a diabetes literature corpus makes synthesized advice materially
easier to produce by accident. That is exactly the synthesis risk #4 gates on,
so #4's review must happen before this ships, not after.

## 3. Physician visit prep — suggested questions to ask

*Depends on: nothing strictly, but much better with #1 and #2.*

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

Pairs naturally with #1 and #2: intake data makes the questions specific to the
person, and literature grounding makes them specific to the evidence. Without
either, it still works — just from trends and anomalies alone.

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
