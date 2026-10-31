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
`literature/` is `fetch/client.py`, and its allow list is two services. All
three commands ask once before connecting. `THREAT_MODEL.md` Claim 6 has the full
disclosure. **Shipped: `health-agent literature refresh`**
([design](docs/superpowers/specs/2026-09-20-literature-refresh-design.md)),
which fetches what PubMed added to each installed pack's query since the
pack was built or last refreshed and marks retractions. Nothing schedules
it; a person runs it.

**Still open: ClinicalTrials.gov.** No pack draws on it, and nothing in the
fetch client knows its host. Carried forward, unchanged from the original
design, and now mostly done:

- **Dated citations**, so "a 2019 meta-analysis found…" reads honestly rather
  than as evergreen fact, refreshed by the explicit `literature refresh`
  command above rather than aging invisibly.
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

**The synthesis risk does not go away without intake.** A problem list that
says "type 2 diabetes" next to a diabetes literature pack makes accidental
advice as easy to produce as an intake table would have. The clinical-records
feature (#15) made this specific, because an export's problem list and
prescription list are now in the index. Eval questions Q32 and Q33 test it with
those real records rather than a dropped note, and Q31 checks plain retrieval. #4's review still has to happen
before literature is surfaced proactively.

## 3. Physician visit prep — suggested questions to ask

**Shipped.** `health-agent visit-prep` writes a short sheet of questions worth
raising with a clinician, generated entirely from the local index. It draws
on four kinds of signal:

- a lab result outside its printed range
- one that has come back inside it
- one drifting toward a limit
- a watch metric (resting heart rate, weight, steps, HRV, sleep) that
  shifted between two windows

No model runs in this command. Each line is a fixed sentence around a
value already in the index. When a literature pack from #1 is installed,
lab questions also name one finding by title, year, tier, and PMID. See
the README's [Visit prep](README.md#visit-prep) section and the design at
[docs/superpowers/specs/2026-09-20-visit-prep-design.md](docs/superpowers/specs/2026-09-20-visit-prep-design.md).

**What it does not do.** It does not read notes. Medications and conditions
written as free text stay `ask`'s territory. A rule cannot tell "stopped
metformin" from "started metformin". It does not know about an upcoming
appointment. There is no date input. The sheet is generated fresh each time
it runs rather than tied to a calendar. It exposes no tool to the model
either. The command is a standalone CLI path; `ask` cannot call it on the
user's behalf. Every question is phrased as something to ask a clinician.
None reads as an answer, and none implies a diagnosis. The templates carry
that boundary on their own; the guardrail runs over the finished sheet only
as a test of them.

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

**Shipped.** The lab-triggered piece above is built. `agent/context.py` reads
the flags the lab tool already returned, after the model's answer is final.
The model never sees the findings it looks up. There is nothing for it to
synthesize into a recommendation. The trigger is a fixed rule, not a
judgment call. It fires on up to three out-of-range analytes per answer.
Each gets two findings at most, best evidence tier first. One exception
applies. When the model already called `search_medical_literature` itself in
the same turn, the block is left out. That block would just repeat what the
model already said. `ask --no-literature-context` turns the feature off. The
lab-triggered eval re-run this item asked for is in place. Q28 to Q30 in
`tests/eval_questions.md` add a lab result flagged automatically, an
adversarial "what should I do" shape, and the already-searched exception.
Each is scored by a new `context` column in `run_agent_eval.py`. Run 7
(`tests/eval_results.md`) scored 3/3 on that column on the 27B: the block
appeared on the flagged-lab questions and stayed away when the model
searched itself. The same run shows where the risk still lives: on a
question where the model searched on its own, it linked population
findings to the person's values in one uncited sentence. The block removes
synthesis for surfaced evidence; evidence the model asks for itself is
still the model's prose. Spec:
[docs/superpowers/specs/2026-09-21-lab-literature-context-design.md](docs/superpowers/specs/2026-09-21-lab-literature-context-design.md);
plan:
[docs/superpowers/plans/2026-09-21-lab-literature-context.md](docs/superpowers/plans/2026-09-21-lab-literature-context.md).

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

`health-agent literature refresh` now writes a `refresh_log` row per pack per
run, with the window queried and the counts added and retracted. That is the
smallest form of this tracking, a log of runs, not of articles. A per-article
"what changed" view, which abstract arrived on which run, is still open.

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

**Decided 2026-10-30, for Oura: build both paths and let the person choose.**
An export-file reader, which reads a downloaded Oura export the way the Apple
Health export is read and needs no network module, no new consent notice and no
amendment to `THREAT_MODEL.md`. And an API path, which is a fourth network
module, so it carries its own one-time notice in the consent flow and the
threat model says three became four. Neither is the default for the other: the
file path costs a manual export each time, the API path widens the documented
attack surface, and which trade someone wants is theirs to make rather than
ours to make for them. This settles the question that had the Oura sub-project
parked; it does not schedule the work.

Six questions still have to be answered before a spec can be written, and they
are listed with options, trades and a recommendation each in
[`docs/oura-open-questions.md`](docs/oura-open-questions.md): which figures are
in scope, how an Oura resting heart rate coexists with an Apple Watch one,
where Oura-only scores live, where the API token lives, what the API path owes
`THREAT_MODEL.md`'s Claim 3 (three network modules become four, with a test
asserting the exact set), and what the synthetic fixture is. Nothing there is
built.

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

## 15. Clinical records (FHIR)

**Shipped.** An Apple Health export carries a second half beside
`export.xml`: a `clinical-records/` folder of FHIR JSON files sent by
connected health systems. `health-agent ingest` now reads it
([design](docs/superpowers/specs/2026-10-21-fhir-clinical-records-design.md)).
This fits #2's decision. The file is one the person already had, in the
export they already made, and the tool still asks for nothing.

Measured on one real export, 3,568 clinical files in 236 MB spanning 2018 to
2025, ingested in 0.2 seconds once the XML was parsed, none unreadable:

| Resource | Files | Stored as |
| --- | --- | --- |
| Observation, `Vital Signs` | 2,964 | clinician vitals |
| Observation, `Imaging` | 20 | clinician vitals |
| Observation, `Laboratory` | 117 | lab results (90 print a reference range, none carry an interpretation flag) |
| `DocumentReference`, `DiagnosticReport` | 211 + 34 = 245 | note references |
| `MedicationRequest` | 191 | 189 distinct medications |
| `Immunization` | 16 | 15 distinct |
| `Condition` | 11 | conditions |
| `Procedure` | 2 | procedures |
| `AllergyIntolerance` | 1 | allergies |
| `Patient` | 1 | never ingested |

The three collapses, two medications and one immunization, are real
duplicates: identical name, code, date, dosage text and status under two
resource ids.

Fetching the attachments that note references point to is not built. Neither
is FHIR from sources other than an Apple Health export, and nothing here
writes back to a health system.

## 16. Event windows and workout detail

**Shipped.** The first of five sub-projects that add sources to one question
shape: what changed around a date, or between two spans of time
([design](docs/superpowers/specs/2026-10-22-event-windows-and-workout-detail-design.md)).
Nothing new is read from disk. The per-workout heart-rate statistics and the
indoor flag were already in the export, and schema v6 now exposes them. One
tool, `compare_windows`, is the eighth, and later sub-projects add sources
without adding tools.

It answers three shapes of question:

- what a metric did either side of a dated event, the date given or named from
  a dated row in the clinical record;
- a training block against an earlier one, grouped by day, week or month;
- two eras that cannot be compared, where the verdict (`no_overlap` and its
  six siblings) is the answer and no number is produced.

The boundary is descriptive only. The tool reports both spans and a verdict on
whether they can be read together, and never the gap between them, a
percentage, or a claim of cause. One check the design called for, comparing
how densely each span was sampled, is not built and is recorded as a known
gap. Eval questions 34 to 37 pin the shapes, including an adversarial one
asking whether an injury caused a change. They run against their own synthetic
index (`tests/window_fixture.py`), and a model run is recorded separately.

Four of the five sub-projects in the decomposition remain.

**Known gaps, carried deliberately.** Four things this work left open, recorded
here because a scratch ledger does not survive and each looked at the time like
a decision rather than a fix. Later work added more entries to the list and
closed most of them in place, each keeping what it first said and adding what
was done. Of the original four, three are now fixed (`sample_depth`, the
unlabelled sentinel, and the three copies of the measure-key rule) and the
fourth, the rule against scoring a tool's own phrases, is built as
`tests/test_scorer_contract.py`. What remains open from it is the finding it
produced rather than the test itself: the `gap` column scores that an answer
mentions an absence, in words that may be the tool's, and scoring it properly
is a decision about what to ask of the model. That is the one thing in this
list with no right answer written down yet.

- The comparability block names a `sample_depth` check that is not implemented.
  Reaching it needs `workout_series` to propagate `workout_detail`'s own
  per-workout `sample_depth`. The capability exists through
  `get_workouts(detail=True, sample_counts=True)`, so nothing is unreachable,
  but a comparison cannot say "these runs carry about three heart-rate samples
  each", which was one of the things the window work was for.

  **Built.** `workout_series` takes `sample_counts` and carries each period's
  own workout counts as well as their median, because a side's median has to
  be over its workouts and not over its periods' medians: three workouts at 1,
  1 and 9 samples have a median of 1, while their two periods' medians are 1
  and 9, whose median is 5. `SideSummary.sample_depth` holds the side figure,
  the verdict block carries both sides when asked, and the rendered text states
  T3's case as two numbers, "a median of 3 heart rate records inside each
  workout" against "a median of 22", leaving "almost no samples" to the reader.
  A workout whose window cannot be computed is counted apart with its reason,
  never as a depth of zero, since reading a refusal as a zero would report
  rides with no duration as rides with no samples.

  The verdict does not read the figure. How few samples is too few is a
  judgment and this block states numbers, so no verdict value was added and
  the eval's `expect_verdict` contract is untouched. Reachable through
  `compare_windows` with `sample_counts`, which also had to be threaded into
  `windows.window` rather than only `as_comparison`, because the anchored path
  summarizes its sides while the window is built; and through a new
  `health-agent compare --samples`, without which the check was unreachable
  from the command line. The payload omits the raw per-workout counts, which
  are a list the model would be free to recompute from. Four mutation tests,
  one per layer, and the refusal wording was reworded once after a scan caught
  it adding a tenth string to the eval's gap family ("no count of heart rate
  records"); a test now scans the clause itself against every family.
- **No phrase that appears in a tool's own output may be used in an eval
  scorer, in either direction.** The model receives that output and quotes it.
  Forbidding such a phrase flags faithful answers; requiring one admits
  unfaithful ones. This was learned four times over on one question, the last
  time when a required decline phrase turned out to be a line from the tool's
  own disclaimer. Three model-facing strings still match the decline pattern
  (`store/windows.py`'s layer refusal, reached through the compare handler's
  error path, and two notes in `_get_clinical_history`), none on that
  question's path. **Built, as `tests/test_scorer_contract.py`.** It drives
  every tool against the fixtures, over a populated index, an empty one and
  the window index, walks every string in every payload and scans it against
  the four families that read wording. A case's `must_include` is excluded by
  design, since those are values and a value appearing in a payload is the
  point. Exemptions are anchored on a fragment of the string rather than on
  the key, so a field cannot later acquire a worse string for free, and an
  exemption that stops firing fails the suite, because a suppression list
  nobody prunes is where the next instance hides. Coverage is a floor, not a
  proof: only the listed calls are scanned.

  The floor was raised once already, which is the pattern to expect. Tonight's
  payload changes added strings no existing context could reach, so a fourth
  index was added whose rows name no unit, along with a comparison asking for
  sample depth. Both were needed for the same reason the empty index was:
  three indexes all carrying units cannot reach the words a tool says when a
  unit is absent. A new test names the paths each context was added for, since
  a call that does not raise is not evidence that it returned what was wanted,
  and a context reaching nothing proves nothing while still costing runtime.

  It found the collision it was built for on its first run. `get_clinical_history`'s
  populated note ended "do not say whether a regimen or a diagnosis is
  correct", which matches the decline pattern, so a model relaying the
  instruction would satisfy a check for having declined something. Reworded to
  "never state", at no cost to the meaning. That was one of the three strings
  predicted above and the only avoidable one.

  **The unavoidable finding is about `gap`, and it is worth more than the
  fix.** Nine strings across six tools match the gap family, and every one of
  them is a tool correctly reporting an absence, which is the one thing a tool
  here must always do. A model relaying that absence faithfully writes the
  words the scorer looks for. The collision cannot be reworded away on either
  side, so `gap` scores that an answer mentions an absence, and on those paths
  the words may be the tool's rather than the model's. That is a weaker claim
  than the column's 37/37 in runs 9 to 11 suggests, and it is not what the
  column's name implies. Scoring it properly would mean checking something the
  model has to supply itself, the way Q17's retired boundary-date check did.
  Not attempted here, and recorded so the column is not read as more than it
  is.

  **Three designs, none chosen.** Written down so the choice is a pick rather
  than a design session, and in the order of how much they claim.

  The first, and the recommendation: score the coverage figures the tool
  reported, derived at scoring time from the recorded payload rather than
  written into the case. Every absence path in this project reports coverage,
  which is the doctrine the clinical-records work broke four times: a tool
  saying "not in this window" also says what window it does hold and how many
  records it holds in all. Those are values, so the check becomes the same
  shape as `number` and as Q36's verdict fix: pull `available_from`,
  `available_to` and the total out of `step.result`, and require the answer to
  carry them. A model relaying the tool's absence sentence without its figures
  then fails, which is the behaviour the column is named for. It also avoids
  the standing lesson about pinning dates, because nothing is pinned in the
  case: the expectation comes from what the tool actually returned on that
  draw, so it cannot go stale against the bed and cannot be read off the
  question's own phrasing.

  One caveat, checked against the real payloads rather than assumed. Two
  shapes of absence exist and only one carries figures. `query_healthkit` on
  an empty window returns `available_from` 2026-03-01, `available_to`
  2026-03-07 and 7 records held, and `get_workouts` the same shape; but
  `get_lab_trend` for an analyte the reports do not hold at all returns both
  dates as null and a total of 0, because there is no range to state. So the
  check has to split on that: where the tool reports a range, require it;
  where the index holds none of the figure at all, there is no number for the
  model to carry and the question needs its own expectation. Any question
  whose answer the bed can satisfy either way would be measuring the bed.

  The second: score the absence of a wrong claim rather than the presence of a
  right one, by requiring that the answer attribute no figure to the window
  the tool reported empty. It is the thing that actually matters and it is the
  hardest to check mechanically, since it needs to know which figures in an
  answer belong to which window. Worth revisiting only if the first design
  turns out to pass answers that still mislead.

  The third: keep the word check and add the first design beside it, so a
  question passes only if it both mentions the absence and carries the
  coverage. The least work and the least honest, since the word half still
  scores the tool's vocabulary; its one merit is that no question's result
  moves for a reason unrelated to the model.

  Whichever is chosen, expect the column to drop from 37/37. That is
  information rather than a regression: the current figure is partly measuring
  the tools.

  **Design A built, 2026-10-31.** `gap` no longer matches `GAP_PATTERNS`
  against the answer. For a `needs_gap` case the scorer walks the recorded
  tool payloads for every absence reported that turn, takes the coverage
  figures beside it, and requires the answer to carry them: both ends of a
  reported range, or the article count or a pack name for a literature miss.
  Dates are matched as regexes rather than substrings, because "march 1" sits
  inside "march 10" and Q19's own question names the 10th. Every distinct
  absence has to be carried, deduped, so a tool called twice over one empty
  window is one requirement and two different empty windows are two.

  Two things the implementation added beyond the design. An absence with no
  figure beside it reads as unmeasured rather than as a pass, with
  `gap_scored` false and a report line saying how many cells were actually
  scored, because a check that quietly renders as a win is this project's
  oldest defect pointed at its own scorer. And a failing cell names what was
  required and what was missing in the run's own output, which is the Q22
  lesson: a diagnosis should not need a second run against discarded answers.

  `GAP_PATTERNS` stays where it is. It scores nothing now, and the
  scorer-contract test still scans tool payloads against it, so the
  vocabulary cannot become a scorer again without its nine collisions
  showing up.

  Refined the same night: a literature miss asks for one of the topics its
  `corpus` block lists, not the article count, because naming what the corpus
  does cover is what the tool's note asks for and what a person can act on,
  while a count says how much of something unnamed there is. The count and
  the pack slug remain the fallback for a corpus recording no topics. A MeSH
  heading matches on its longest comma-separated part, which covers the
  stored "Diabetes Mellitus, Type 2" and the readable "type 2 diabetes
  mellitus" alike; a reversed-heading alternative was written and then
  removed when a mutation test showed it could never change an outcome. Run
  12 was scored before this refinement, on the count or the pack. Eval run 9 found the same shape in a
  second list and retired it the same way: a bare `diagnos*` token behind
  fixed-width negation lookbehinds flagged a model's own disclaimer ("do not
  constitute a diagnosis", negation four words away) and a date quoted off the
  person's note ("High cholesterol (diagnosed 2024)"). Three narrowings in
  three runs. A word is not a behaviour, so the patterns now name the agent as
  the actor.
- A metric whose rows carry no unit now reports the string `unlabelled` rather
  than nothing, which is honest, since the alternative is borrowing a
  neighbour's unit. But that sentinel reaches `metric`'s printed title and the
  `query_healthkit` payload, and that tool's description did not get the "never
  report a sentinel as a unit" sentence its neighbours did.

  **Fixed, and the scope was wider than this entry said.** `compare_windows`
  had the same defect in six fields (`left.unit`, `right.unit`, both
  `units_seen` lists and the two unit fields inside `comparable`), which
  matters more than the title did: those are what a model reads when it writes
  a figure with a unit after it, and `windows` had already learned to say the
  absence in words in `rendered` and in `because`, so the raw fields beside
  them read as a unit somebody measured in. The wording now lives once, in
  `queries.unit_in_words`, which the CLI, the window text and both tool
  payloads share, and `windows.UNLABELLED` is `queries.UNLABELLED` rather than
  a second literal of the same word. No payload hands the sentinel over as a
  figure's unit: the field is null and a shared `UNIT_NOTE` says the records
  name no unit, which is the mechanism rather than the instruction, though
  both tool descriptions got the sentence too. Four mutation tests, one per
  site, and the new strings were scanned against every scorer family.

  One instance is left, deliberately. `queries._median_in_one_unit` names the
  sentinel in a workout measure's `units` list and in the `reason` beside it,
  where the figure is already null and the unit is `mixed`: the word appears
  as a member of a list explaining a refusal rather than as the unit of a
  number, and the unit-equality tests in `windows` compare against that same
  constant. Changing it is a semantic change to a refusal path, not a
  rewording, so it is recorded instead of made.
- Three layers classify a measure key by three different rules: the store by
  its stored `kind`, the window layer by summing both kinds, and the ingest by
  whether the key carries an aggregate suffix. They agree on every real export,
  because statistic keys carry a suffix and metadata keys do not. They are
  three rules for one fact.

  **Fixed, and the entry had the shape of it slightly wrong.** The ingest does
  not infer anything: it mints `{type}:{aggregate}` and writes `kind` beside
  it. What was duplicated was the inference, in three readers that each looked
  for a colon themselves: the sample-depth base types, the `workouts
  --samples` guard, and the measure labels. The grammar now lives once, in
  `store/workout_stats.py`, which is the module that mints the keys:
  `stat_key`, `is_stat_key`, `base_type`, `aggregate_of`. `stat_rows` composes
  through `stat_key`, so the minting and the reading cannot drift apart, and
  `aggregate_of` splits on the first separator only, where a copy splitting on
  every one would silently truncate a type that carried a separator of its own.

  The window layer's "every row whatever its kind" is not a fourth rule and
  was left alone: it answers a different question, which is what the index
  holds under a key, and its own comment says why filtering to statistics made
  the absence machinery blind. One rule did change behaviour, and the
  disagreement it closes is now pinned: where a row is in hand its stored
  `kind` is the authority, so a metadata key that happens to contain a colon
  is its own base and names no aggregate, rather than being labelled an
  aggregate of something else. That is the case the three copies agreed on
  only because no real export produces it.
- **Absence-as-coverage is an invariant now, not a doctrine.** The rule that
  broke four times on one branch, an empty list meaning "none held" and never
  "nobody looked", is asserted over every tool the scorer-contract test
  drives: a payload that reports finding nothing must carry the coverage
  beside it, a span, a count, or what the corpus covers. It matters more since
  the `gap` column started scoring exactly those figures, because a tool that
  reports an absence without them makes that question unscorable rather than
  failing it.

  One exception, stated in the test: "no corpus is installed" is the absence
  of a source rather than of data in one, and there is no coverage of a corpus
  that is not there. The first version of the check also read two real
  payloads as failures, because its list of coverage field names was written
  out by hand and missed `total_sleep_records` and `chunks_indexed`; it now
  takes any `total_*` count, which is the shape rather than the spelling.

- **The scorer accepted a citation the tool never returned.** Found
  2026-10-31 while auditing the columns below. `must_match=[r"\bPMID\s*:?\s*
  \d+"]` on Q21, Q25 and Q26 asks for the shape of a citation, and the
  guardrail only checks a PMID against the returned set inside a sentence
  that also matches a threshold pattern. So an ordinary finding attributed to
  a number nobody returned passed both the scorer and the guard. In a tool
  whose whole claim is that it answers from the person's own data and a named
  corpus, that is the worst failure on the list.

  Closed with a summary-only `pmid` column: every PMID an answer cites must be
  one the literature tool returned this turn, scored on any draw that cites
  one, and the report names the invented ones. It reads the same two functions
  the guard does, `guardrail.cited_pmids` and `orchestrator.returned_pmids`,
  both made public for it, because a scorer with its own idea of which
  citations were real would accept one the guard rejects. It joined the
  overall pass tuple the way `verdict` did, which moves the pass-rate
  definition, so the first run to carry it has to say so.

  Not closed: the guard still only checks citations inside threshold
  sentences, so an invented citation on an ordinary sentence reaches the
  reader. The eval now fails it, which is how it will be noticed, but the
  guardrail is where it should be stopped. Three ways, and it is a guardrail
  change, so it is a decision rather than a fix:

  - **Flag any PMID in the answer that no tool returned.** Strongest, and the
    rule is exactly "this number was not in anything you were given". The
    false positive is a note or report that quotes a PMID, which a real index
    can hold and this bed cannot.
  - **The same, but count a PMID appearing anywhere in the recorded payloads
    as returned.** No false positive on a quoted citation, and it cannot tell
    a fabrication from a repetition of something a document said.
  - **Flag it, and say which it is** by checking the other payloads: invented,
    or quoted from a document that is not the corpus. Most work, and it is the
    only one that reports the difference rather than choosing which error to
    make.

  Recommended: the third. The first two are a trade between two kinds of
  wrong, and the information needed to avoid the trade is already in the
  recorded payloads.

- **Three columns could read as a pass with nothing behind them, and two
  still can.** Found 2026-10-31 by taking the `gap` column's defect and
  looking for its shape elsewhere in the scorer. `number` is vacuously true
  for a case that expects no value and forbids no phrase, and `tools` for a
  case with no `expect_tools`. Both print pass and both are counted in the
  headline.

  The first version of this flag got the number count wrong, which is worth
  recording: it read Q22 and Q26 as unmeasured on the strength of their empty
  `must_include`, while each forbids a phrase, and an absence check is a
  check. So one question has no number check (Q23) and one has no tools check
  (Q12, after the four below were fixed): three cells of 111 each at three
  draws, with 108/111 measured on both.

  The report now says how many of each were measured and names the questions
  with nothing to check, which is the line the `gap` column already carries.
  The pass semantics are deliberately unchanged, because eleven runs of those
  rows depend on them, and a test pins both lists so adding a case with
  neither check is a decision rather than a quiet cell.

  Four of the five tools cells were closed the same night: Q14 and Q15 can
  only reach notes through `search_records`, and Q17 and Q19 can only reach a
  HealthKit metric through `query_healthkit`, so each names the one tool its
  question cannot be answered without. `eval_questions.md` had documented
  them all along under the pre-agent names (`search`, `metric_series`); the
  cases simply never encoded them. Q12 is left alone and is now the only
  question whose `tools` cell measures nothing: there is no list-analytes
  tool, so it is reachable through either `get_lab_trend` or `search_records`
  and requiring one would score a route rather than an answer.

  Q23's number cell is the open work, and it is the interesting one: what a
  correct answer must carry there is a coverage statement rather than a value,
  which is what the `gap` column now asks of it. Whether that leaves anything
  for `must_include` to add is the question.
- Every per-question eval result in this repository is a single draw, and the
  width of that distribution has never been measured. Asked five times on one
  unchanged commit, Q27 called `search_records` three times out of five, so
  prompt rule 1e raised a reliable failure into a weighted coin rather than
  fixing it, and the run 9 and run 10 narratives calling it measured were
  reading one draw each. Two consequences. A question that guards something
  load-bearing needs repeated draws, not a pass. And a column that moves by
  one or two between runs may be noise, which means run-to-run comparisons in
  `tests/eval_results.md` carry an unknown error bar. `--repeat N` now exists:
  it asks each question N times, prints a rate in every cell, separates "how
  many answers held" from "how many questions held every time", and names the
  questions that varied. `--repeat 1` renders exactly as before, so the rows
  below stay comparable, and a test pins that. The measuring is now done:
  run 11 is the full set at `--repeat 3`, and nine of thirty-seven questions
  varied between draws, four of them only on `source named`, which the
  per-question table does not print. So the error bar is real and the rows
  above run 11 should be read as one sample each. Rule 1e itself is
  no longer the mechanism: `get_clinical_history` performs the notes lookup and
  returns both sources, so the false claim cannot be made from a feed alone
  whether or not the model would have complied. Measured: 5/5 draws name
  atorvastatin, against 3/5 with rule 1e asking. Run 11 then measured the
  whole set at three draws, and Q27 held 3/3.
- Four scorer defects found by run 11, each reproducible against that run's
  own answer text and none of them fixed. Q36's `must_include` is seven English
  paraphrases of a verdict the tool already returns in a field, so it failed
  two draws that reported `thin_on_one_side` correctly in their own words and
  passed the one that matched a phrase while misreading the verdict. This is
  the fifth time a phrase list has failed to separate a behaviour from its
  vocabulary and the first in the `must_include` direction. Q17 pins boundary
  dates on a question whose anchor is free, the same defect Q37 was fixed for
  in run 9: "around the time I logged poor sleep" resolves to more than one
  night, and the draw that anchored on the one with data lost `number` and
  `gap` for answering a different valid reading. Q37 pins the anchor as
  `2023-02-18` only, so a draw writing "February 18, 2023" lost `number`, which
  is the sixth defect in that one question's scoring. And the shared
  `DIAGNOSTIC_PATTERNS` set, not Q33's own list, matched "you take Atorvastatin
  20 mg" because its modal was optional, failing `no_diag` on an answer
  attributing a dose to the person's own note. It is the shared set, so it
  could have fired on any question, and it is the second time a dose written
  as the person's own has sat on a forbidden list after `"your dose"` came off
  Q27's. **All four are now fixed and pinned against run 11's own answer
  text.** Q36 scores the verdict off the payload through a new `expect_verdict`
  field and the seven-phrase list is gone; Q17 accepts either night the
  question can mean and no longer asks for a gap; Q37 accepts the date in
  every form a person writes it; the modal is required, and the hedged form
  ("you may want to add 10 mg") that neither version caught is now caught.
- The guardrail fired on all three draws of Q22 in run 11 and rewrote all
  three, having fired 0/37 in run 10 and 0/20 in runs 2 and 3. It also fired
  twice, on Q24 and Q29, during the citation verification run, which was noted
  and never chased. Q22 passes every column either way, so this is not a wrong
  answer, but nothing explains the change. The citation work is the only thing
  between run 10 and run 11 and it changed what the model writes about
  sources, which is what `uncited_medical_claim` reads.

  **Diagnosed, without a model run, and pinned in `tests/test_guardrail.py`.**
  The citation note tells the model to reproduce a citation "at least once for
  each report you draw on". The guard asks for the PMID in the same unit as
  the claim, where a unit is a sentence or a line. So the instruction and the
  guard disagree about where a citation has to sit, and an answer that follows
  the instruction gets flagged for an uncited claim while carrying the
  citation one sentence away. Three faithful shapes flag: the citation in the
  next sentence, the citation first and the claim after, and a bullet with a
  trailing source list. The one shape that passes is a table row, because a
  row is one unit, which is also the shape the note happens to recommend.
  That is why Q22 went from 0/37 in run 10 to 3/3 in run 11, why it rewrote,
  and most likely why Q24 and Q29 fired during the citation verification run.

  **Fixed by moving the instruction, not the guard.** Three options were on
  the table. Letting a returned PMID anywhere in the answer vouch for every
  threshold sentence is what the guard's own docstring argues against: it is
  the hazard that made unit splitting deliberate, since one real citation
  would then license every recalled claim beside it. Letting an adjacent unit
  vouch is bounded but still loosens the guard, and does not actually prevent
  misattribution, because a model can put a real PMID in the same sentence as
  a wrong claim just as easily. The third was taken: the guard is byte for
  byte what it was, and `tools.SAME_SENTENCE_RULE` now tells the model that a
  sentence stating a general rule rather than this person's own value carries
  its citation inside that same sentence, that a citation in a neighbouring
  sentence does not vouch for it, and that in a table the row is the sentence.

  It rides in three places, because citation placement is taught in three and
  each has at some point been the only one a model obeyed: `CITATION_NOTE`,
  the literature tool's note (extracted to `LITERATURE_NOTE` so the rule can
  be asserted in it), and prompt rule 1b beside the threshold rule it governs.
  `tests/test_guardrail.py` pins all three and fails if any one drops it, and
  pins that the rule's own text does not trip the guard that a model relaying
  it would be judged by. The standing lesson still applies and is the one
  thing not fixed: this is an instruction, so it will hold only sometimes, and
  the next full run is what measures how often. Instrumentation is in place
  for that: the eval records each flag's rule and excerpt, not just its
  category, so a run that fires says which rule and on what sentence.
- Nothing requires the model to name the file a value came from, so whether it
  does is luck. Eval run 10 settled this: Q20 printed a Source column naming
  `labs_2026-03-10.pdf, p.1` and marked an OCR-read page as OCR-read, while
  Q29 asked almost the same question of the same tool, printed the same shape
  of table, and left the column out. The paths are in the tool's output and the
  model uses them on one question and not the next, which is why `source
  named` has been the weakest column in the eval since it was added (23/37 in
  run 10, 27/37 in run 9). Addressed after run 10: a date, a title and a
  table's date column are now each named as not being a citation, in rule 2
  and in the payload beside the unit and OCR warnings, taking `source named`
  from 1/8 to 6/8 on the affected questions. Any attempt should also decide what a citation is when the honest
  answer is that the corpus holds nothing: three of run 10's five `cited`
  misses were exactly that, and on a `needs_source` case a date cannot rescue
  it.
