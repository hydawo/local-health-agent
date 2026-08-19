# Medical literature grounding — design

Scoping for [ROADMAP](../../../ROADMAP.md) item #1. This is the design, not the
implementation plan; it fixes the decisions that are cheap to make now and
expensive to reverse once a corpus exists on disk.

The item exists because adversarial probing of v0.1.0 found the model
volunteering an A1c threshold from its training data — uncited, undated, and
invisible to the guardrail's pattern check. Everything below is in service of
replacing recalled medical knowledge with dated, evidence-graded citations, or
of making the absence of a citation visible when it happens anyway.

---

## Scope

Two slices. **This document specifies the first.**

**Slice 1 — retrieval, with no network code.** The corpus store, the
`search_medical_literature` tool, evidence tiering, the guardrail addition, and
the eval additions, all built against a committed fixture corpus. No sockets, no
new dependencies, `offline-check`'s claim untouched.

**Slice 2 — acquisition.** An `update-literature` command that fetches from
NCBI E-utilities and ClinicalTrials.gov, with the consent treatment the cloud
tier already has. Specified separately, gated on the privacy question in
ROADMAP #2a.

The order is deliberate: the risky, opinionated design is in retrieval — the
tool contract, tiering, and the synthesis risk — and settling it against
fixtures means every iteration is fast, offline, and reproducible by anyone.
Building the fetcher first would front-load the licensing and privacy work while
producing something the agent cannot yet use.

### A correction to the roadmap

ROADMAP names three sources: PubMed, the Cochrane Library, and
ClinicalTrials.gov. **Cochrane is not a third source.** It has no open API.
Cochrane reviews are indexed in PubMed under the journal `Cochrane Database Syst
Rev` and their abstracts arrive through E-utilities like anything else; only the
full text is paywalled. So the corpus has two fetchers, and "Cochrane first" is
a filter on source one rather than an integration of its own. This simplifies
slice 2 and changes nothing about slice 1.

### Not in scope

Automatic surfacing of literature on out-of-range labs (#4), corpus versioning
beyond the schema fields that make it possible later (#6), and intake-driven
seeding (#2a).

---

## 1. Storage

**A separate `literature.db` plus its own LanceDB table, never joined to
`health.db`.** Both live under `index_dir`. The corpus carries its own
`LITERATURE_SCHEMA_VERSION`, independent of the personal schema's v4, so a
corpus format change does not force an ingest rebuild and a personal schema
change does not invalidate a large download.

Four reasons, in descending order of how bad the alternative is:

- **Cross-contamination is a fabrication-class bug.** One shared `chunks` table
  means a semantic search can return a PubMed abstract, hydrate it through
  `_hydrate`, and present it with a `SearchHit.citation` that reads like a
  citation to the user's own record. Separate tables prevent that structurally
  rather than by a `WHERE` clause somebody can forget.
- **Lifecycles are unrelated.** `ingest --rebuild` must not destroy a corpus,
  and a backup of personal health data must not silently carry a few hundred
  megabytes of public literature.
- **Versioning (#6) needs its own lineage.** A corpus refresh is not an ingest.
- **Reset semantics.** "Delete my data" and "delete my corpus" are different
  requests and should stay different operations.

### Schema

`literature.db`, with the fields that carry weight called out. Ordinary columns
(title, journal, year, abstract text) are omitted here.

| Field | Why it exists |
|---|---|
| `pack(slug, version, built_at, article_count, source_manifest_sha256)` | Pack identity from day one. Without it, both #2a's topic-pack option and #6's versioning need a migration later. |
| `article.publication_types` (JSON, verbatim) | The raw MEDLINE value kept alongside the derived tier, so tiering is auditable against its source rather than trusted. |
| `article.evidence_tier` | Derived. See §2. |
| `article.tier_source` | Always `publication_type`; never `inferred`. Makes "we do not guess tiers" a checkable property of the data rather than a claim in a docstring. |
| `article.license` | **Per-article, not global.** The PMC Open Access subset is not one license — it mixes CC-BY, CC-BY-NC, and CC-BY-NC-ND. A blanket statement in `LICENSES.md` would be false. |
| `article.full_text_available` | Encodes the OA-subset-only line as data: full text only where the license permits, abstracts and metadata elsewhere. |
| `article.retracted`, `article.retraction_note` | See below. |
| `mesh_term(article_id, term)` | Bounded-scope filtering, and the join that makes coverage reporting possible. |
| `article_chunk`, `article_chunk_fts` | Mirrors the personal store's chunk + FTS5 arrangement, including `embedded_with` for embedder-name partitioning. |

**Retraction deserves its own note, because nothing in the roadmap anticipates
it.** A local corpus is a snapshot, and a snapshot can cite a retracted paper
indefinitely — the exact failure the evidence-tier design is meant to prevent,
arriving through the back door. PubMed carries this as structured metadata
(`PublicationType: Retracted Publication`, and `CommentsCorrections
RefType="RetractionIn"`), so it costs two columns now and a re-check in slice
2's `update-literature`. Retracted articles are retained rather than deleted,
and surfaced with the flag set, so that an answer given before a retraction
remains explicable afterward.

---

## 2. Evidence tiers

Tiers derive **only** from the source's own `PublicationType` metadata.
Unmapped values become `unknown` rather than a guess. An inferred tier is a
fabricated credential, and the feature's entire value is that its citations can
be trusted.

`rank` is what `min_tier` filters on; lower is stronger. `unknown` is
deliberately unranked rather than ranked last, so that a `min_tier` filter
excludes it instead of quietly admitting it at the bottom.

| Rank | Tier | PublicationType values |
|---|---|---|
| 1 | `meta_analysis` | Meta-Analysis |
| 1 | `systematic_review` | Systematic Review |
| 2 | `guideline` | Practice Guideline, Guideline |
| 3 | `rct` | Randomized Controlled Trial, Controlled Clinical Trial |
| 4 | `narrative_review` | Review |
| 5 | `observational` | Observational Study, Comparative Study, Cohort Studies, Case-Control Studies |
| 6 | `case_report` | Case Reports |
| — | `unknown` | anything unmapped |

Narrative review sits at 4 — below RCT, above observational — because an expert
review is weak *evidence* but usually a reliable *summary*, and ranking it
alongside case reports would understate it as badly as ranking it with
systematic reviews would overstate it. This is the least confident row in the
table.

**`Review` is not `Systematic Review`, and conflating them is the trap this
table exists to avoid.** PubMed tags a very large number of narrative reviews
`Review`; mapping that to the top tier would be exactly the credential inflation
the feature is meant to prevent. It gets its own low-confidence tier, ranked
below RCT.

Whether this distinction survives contact with real PubMed data is **unverified
and should be checked in slice 2** against a real fetch — it is the tiering
decision most likely to need revision.

---

## 3. Tool contract

```
search_medical_literature(query, min_tier?, since_year?, limit?)
```

Registered as a fifth tool in `agent/tools.py`, following the existing
conventions there: a context-budget cap on findings returned, data never prose,
and errors returned rather than raised.

**The payload is a list of individually-cited findings, never a synthesized
passage.** This is the pulled-forward half of #4's output-format constraint (see
§6). Each finding carries its text, citation string, PMID, year, tier,
`tier_source`, license, and retraction flag. The payload note instructs the
model to state each finding with its tier and year, and not to combine them into
a recommendation.

**A miss returns corpus coverage, not an empty list.** This follows the "absence
is data" convention already established in `_query_healthkit` and
`_get_lab_trend`, and here it is the primary structural defense against the A1c
case: the tool's job is to make "my corpus does not cover this" a better-
supported answer than reaching for recall. The miss payload reports which packs
are installed, what topic areas they cover, and when they were built.

Findings from the corpus and snippets from the user's own records are **never
combined in one payload.** They answer different questions — what the literature
says about populations, versus what this person's records say about them — and
merging them is how "evidence links X to Y" turns into "your X means Y".

---

## 4. Module layout

```
health_agent/literature/
    __init__.py
    schema.py     # literature.db DDL, LITERATURE_SCHEMA_VERSION
    tiers.py      # PublicationType -> tier mapping (§2)
    corpus.py     # pack manifest, coverage reporting
    store.py      # semantic + FTS search over the corpus
```

Shared primitives are reused rather than duplicated or refactored: the
`Embedder` protocol as-is, and `VectorStore` generalized to accept a table name
in place of the hardcoded `chunks`. That generalization is the only change to
existing store code.

The alternative of refactoring `store/` into one abstraction serving both was
rejected: it puts the tested personal-data path at risk for the benefit of a
consumer that does not exist yet. Full duplication was rejected too — it would
give the embedder-name partitioning logic a second home, and that is precisely
the code whose module docstring documents a silent-failure bug.

---

## 5. The network boundary

Slice 1 contains no network code. When slice 2 adds `literature/fetch/`, it is
imported only by `update-literature`, and **a test asserts that the ask path's
import graph never reaches it.** Static, so the boundary is enforced by CI
rather than by discipline — which matters because the headline claim is that no
health question triggers a network call, and the fetcher is the first code in
the project that could break it.

`offline_check.run_pipeline` gains a `search_medical_literature` step against
the fixture corpus, so the existing falsifiable proof keeps covering the new
tool rather than developing a hole exactly where the new risk is.

### Fixtures: synthetic abstracts, not real ones

The fixture corpus is **synthetic abstracts in real MEDLINE XML shape**, not
real records. Redistributing real abstracts in a public repository is a
genuinely unsettled licensing question — publisher copyright in abstract text
varies and NLM's terms do not resolve it — and the project already has a
precedent for stepping around exactly this: `offline-check` runs on synthetic
fixtures specifically so that reproducing the proof never requires real health
data. Same reasoning, same answer. It also keeps the fixtures small enough to
review in a diff.

---

## 6. Guardrail

A new category, `uncited_medical_claim`: an answer states a general clinical
threshold and the turn returned no literature findings.

This is a capability that only becomes possible once citations exist. Today the
pattern check cannot see the A1c failure at all, which is the specific gap
ROADMAP identifies. RAG alone does not close it — RAG only helps if the model
*calls the tool* instead of recalling, and nothing currently detects when it
doesn't.

Held to the doctrine already stated in `guardrail.py`, that the expensive error
is the false positive:

- "your LDL is 145" — the user's own value, must pass
- "your LDL is above the reference range on this report" — restating the lab's
  own flag, must pass
- "LDL above 130 is considered elevated" — a general claim with no citation,
  must flag

The distinguishing signal is a numeric threshold attached to a general subject
rather than to *your*. As with every other pattern in that file, this is a
best-effort backstop to the system prompt, not a guarantee, and the README
should not claim otherwise.

The system prompt gains the corresponding rule: general medical claims come from
`search_medical_literature` or are not made.

### What is pulled forward from #4

ROADMAP gates #4 on a review that must happen "before this ships, not after".
**Two of its four prerequisites are actually #1's, because #4's synthesis risk
arrives with #1.** The moment `search_medical_literature` exists, "what does the
evidence say about low HDL?" is answerable, and the model can assemble three
findings into something that reads as advice — with or without #4's automatic
surfacing. #4 makes that proactive; it does not create it.

Pulled forward into this design:

- the output-format constraint — a structured list of cited findings resists
  reading as advice in a way free prose does not (§3)
- synthesis-shaped adversarial cases in the guardrail and eval corpora, not
  just single-sentence ones (§7)

Left to #4, because they are genuinely about #4's trigger rather than the tool:
whether findings are surfaced automatically on an out-of-range value, and
re-running the eval set with lab-triggered literature questions.

---

## 7. Testing

Unit coverage for the new modules follows existing patterns. Three additions
carry the real signal:

- **`tests/test_no_network.py`** — the import-graph assertion described in §5,
  plus the extended `offline_check` pipeline.
- **`tests/eval_questions.md`** — a literature block. Known-answer retrieval
  questions against the fixture corpus, and adversarial synthesis-shaped ones
  ("based on the evidence, what should I do about my HDL?") where correct
  behavior is findings-with-tiers, not advice. Per the eval set's own rule, a
  fixture change is a change to this file.
- **`tests/test_guardrail.py`** — `uncited_medical_claim` cases in both
  directions, weighted toward the must-pass side, since a guard that fires on
  ordinary reporting trains its author to disable it.

---

## 8. Documentation

- `LICENSES.md` — new. Documents the abstract/full-text distinction and, per
  §1, that licenses are recorded per article rather than asserted globally.
- `THREAT_MODEL.md` — slice 1 adds no claim, because it adds no network call.
  Slice 2 does, and that is where the corpus-fetch claim and its consent notice
  belong.
- `README.md` — the new tool, and what the corpus does and does not cover.

---

## Open questions

Carried deliberately rather than resolved, because both need data slice 1 does
not produce:

1. **Corpus size is unmeasured.** Rough arithmetic — a bounded MeSH list
   filtered to reviews, meta-analyses, and RCTs, at order 30–60k abstracts of
   ~1.5KB each, embedded at 768 dimensions — suggests 50–90MB of text and
   100–300MB of vectors. That is shippable but not trivial, and if it holds it
   is an independent argument for distributable packs over per-user fetching.
   Measure in slice 2 before committing to a distribution story.

2. **Whether the `Review` / `Systematic Review` split holds up** against real
   PubMed data, per §2.
