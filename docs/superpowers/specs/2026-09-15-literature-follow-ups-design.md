# Literature grounding — three follow-ups from real PubMed data

Follow-up to [2026-08-18-literature-grounding-design.md](2026-08-18-literature-grounding-design.md).
That document left two questions open because slice 1 produced no data to
answer them. Both were answered after slice 1 merged, against 2,000 real
abstracts fetched from NCBI E-utilities (a MeSH query over cholesterol,
hypertension, type 2 diabetes, sleep, exercise, HDL and LDL; 2015–2026;
`hasabstract`). Both original estimates were wrong, and the measurements
produced three changes this document specifies. The measurements themselves
are recorded in §1 because they exist nowhere else in the repository.

Nothing here adds network code. `offline-check`'s claim is untouched.

---

## 1. Measurements

### Size — the text estimate was low by 4–5×

| | per article | 24k articles | 61k articles |
|---|---|---|---|
| `literature.db` | 7.3 KB | ~178 MB | ~443 MB |
| vectors at 768 dimensions | 7.8 KB | ~191 MB | ~474 MB |
| total | 15.1 KB | ~370 MB | ~917 MB |

The original design predicted 50–90 MB of text at 30–60k articles; it is
219–443 MB. The vector estimate was roughly right. The unfiltered query
matches 415,318 articles, about 6.3 GB, which makes distributable topic packs
(ROADMAP #2a) a requirement rather than a preference. Two scope reference
points from PubMed: meta-analyses plus systematic reviews come to 24,437
articles; adding guidelines and RCTs makes 60,752.

**Why the text estimate missed.** `literature.db` came out at 3.9× the raw
abstract text (14.8 MB against 3.8 MB). The abstract was written to
`article.abstract`, to `article_chunk.text`, and indexed by FTS5; 75% of
articles are a single chunk byte-identical to `article.abstract`, so that
column bought nothing. The vector store adds a further copy: LanceDB rows carry
`text` beside the vector, which is why 7.8 KB per article is more than double
the 3 KB a 768-float vector occupies.

The personal store's duplication is justified: chunks are the durable copy so
re-embedding never re-parses a PDF. Abstracts arrive already parsed, so that
reasoning does not carry over.

### Tier distribution on the same 2,000

```
unknown            65.5%     narrative_review   12.4%
rct                 7.7%     systematic_review   6.0%
observational       5.5%     case_report         1.6%
meta_analysis       1.2%     guideline           0.1%
```

The `Review` / `Systematic Review` split was the right call and now has a
magnitude: narrative reviews (12.4%) outnumber systematic reviews and
meta-analyses combined (7.2%). Mapping `Review` to tier 1 would have nearly
tripled the apparent top-tier pool.

**The unanticipated finding is 65.5% `unknown`, and it is not a mapping gap.**
Of the 1,309 unknown articles, 1,086 carry `Journal Article` and nothing else.
PubMed does not record a study design for most primary research. This
vindicates the never-infer rule, since any tier assigned there would be
invented, but it changes what `min_tier` means in practice (§2).

Four real publication types were unmapped and appeared in the sample:
`Validation Study` (29), `Scoping Review` (16), `Clinical Trial Protocol` (13),
`Clinical Trial` (3). §3 decides each.

Zero retracted articles appeared, so that path is still exercised only by the
synthetic fixture.

---

## 2. `min_tier` excludes two-thirds of the corpus

`unknown` is deliberately unranked so that a floor excludes it. That is correct
for "only strong evidence" and wrong as a casual default, and the tool's
parameter description invites a model to pass `min_tier` freely. Worse, a
filtered search that returns nothing currently tells the model "the corpus
holds nothing on this", which with a floor set is simply false: the corpus may
hold plenty, untagged.

**Decision: report the exclusion; add no parameter; fix the misleading miss.**
An `include_unknown` switch was rejected because it is one more argument for
the model to reason about, and `min_tier="rct", include_unknown=true` reads as
a contradiction. Removing `min_tier` from the tool was rejected because an
"only strong evidence" question is a real use, exercised by eval Q25.

The store's semantics do not change. `unknown` stays unranked,
`tier_at_least` and `_filters` are untouched, and every existing store test
stands. All three changes are in `agent/tools.py`:

1. **Filtered miss.** When `min_tier` or `since_year` was set and nothing came
   back, the payload carries `filtered: true`, echoes the filters that were
   applied, and replaces the note: nothing matched under these filters; retry
   without them before concluding the corpus does not cover the topic. When
   `min_tier` was among the filters, the note adds that N of M articles
   (share) in this corpus have no recorded study design and are excluded by
   any floor. The "holds nothing on this" wording is reserved for unfiltered
   misses.
2. **Filtered hit.** When `min_tier` was set and findings came back, the
   payload gains a `filter` block —
   `{"min_tier": ..., "unranked_articles_excluded": N, "unranked_share": 0.65}`
   — with a one-line note. The numbers come from `coverage()["tiers"]`,
   which already counts `unknown`. They are corpus-level and exact. A
   per-query count was rejected: keyword search is an FTS OR-match over every
   token, so "4,812 matching articles excluded" would be true and useless.
3. **Parameter description.** Rewritten to say that most primary research in
   PubMed carries no study-design tag, so any floor excludes the majority of
   the corpus; that results are already ordered strongest-first without it;
   and that it is for questions that ask for evidence at a stated strength.

**Fixture.** No article in `tests/fixtures/literature/corpus.xml` is
`unknown`: every record carries a mapped type. So
`test_min_tier_excludes_unknown_rather_than_ranking_it_last` passes vacuously
and the tool's `tier_warning` branch has never run against a real row. One
`Journal Article`-only record is added. Per the eval set's own rule, a fixture
change is a change to `tests/eval_questions.md`; Q21–Q26's expectations are
re-checked against the new record, which must not match Q25's query at or
above `systematic_review`.

---

## 3. Four unmapped publication types

Tiers still derive only from `PublicationType`. Mapping a type deliberately is
not inference; mapping a type that does not name a design would be.

| PublicationType | Decision | Reason |
|---|---|---|
| `Clinical Trial`; `Clinical Trial, Phase I` through `Phase IV` | new tier `clinical_trial`, rank 4 | Interventional but not randomized: below RCT, above reviews and observational. The phase variants are PubMed's own children of `Clinical Trial`; unmapped, a Phase III trial would be `unknown`. |
| `Controlled Clinical Trial` | moved from `rct` to `clinical_trial` | Changes a slice 1 mapping. In PubMed's hierarchy it is the parent of `Randomized Controlled Trial`, not a synonym; controlled does not mean randomized. Mapping it to `rct` was a small instance of the inflation the module exists to prevent. |
| `Scoping Review` | new tier `scoping_review`, rank 5, beside `narrative_review` | A structured method, but no quality appraisal and no synthesis, so not rank 1. Not a narrative review either, so it gets its own label at the same rank. Two tiers sharing a rank is already the pattern at rank 1. |
| `Clinical Trial Protocol` | new tier `protocol`, unranked, overriding every other type on the article | A protocol reports no results. Protocols of RCTs are tagged both `Clinical Trial Protocol` and `Randomized Controlled Trial`, so strongest-wins would mint an `rct` out of a plan. Unranked means `min_tier` excludes it and ordering puts it last. Retained rather than dropped at build, matching the retraction precedent: keep, and flag. |
| `Validation Study` | deliberately unmapped, recorded in code | Names a purpose, not a design; a validation study can be any design. It goes in an explicit `DELIBERATELY_UNMAPPED` set in `tiers.py` with a test, so the decision reads as a decision rather than an oversight. |

### The revised table

| Rank | Tier | PublicationType values |
|---|---|---|
| 1 | `meta_analysis` | Meta-Analysis |
| 1 | `systematic_review` | Systematic Review |
| 2 | `guideline` | Practice Guideline, Guideline |
| 3 | `rct` | Randomized Controlled Trial |
| 4 | `clinical_trial` | Clinical Trial; Clinical Trial, Phase I–IV; Controlled Clinical Trial |
| 5 | `narrative_review` | Review |
| 5 | `scoping_review` | Scoping Review |
| 6 | `observational` | Observational Study, Comparative Study, Cohort Studies, Case-Control Studies |
| 7 | `case_report` | Case Reports |
| — | `protocol` | Clinical Trial Protocol (overrides; `tier_source` is `publication_type`) |
| — | `unknown` | anything unmapped (`tier_source` is `unmapped`) |

`protocol` and `unknown` are both unranked, and they are different facts:
one says "we know what this is, and it is not evidence", the other says "we do
not know what this is". `tier_source` keeps them apart.

`resolve()` gains the override: if any type is `Clinical Trial Protocol`, the
result is `protocol` regardless of what else is present. Otherwise
strongest-wins is unchanged.

**Ranks renumber.** `evidence_rank` is derived at build time, never edited in
place, and §4 forces a rebuild, so no migration is needed. `TIERS` in
`tiers.py`, the `min_tier` enum in the tool schema, the tier table in
`README.md`, and ROADMAP #1's summary all change together. Neither `protocol`
nor `unknown` appears in the `min_tier` enum: a floor at an unranked tier
means nothing.

**Tool payload.** Beside the existing `tier_warning`, a `protocol_warning`
appears when any finding is a protocol: the finding describes a planned study
and reports no results; say so, and do not present it as evidence.

**Fixture.** One record tagged both `Clinical Trial Protocol` and
`Randomized Controlled Trial`, so the override is proven end to end, not only
in `tiers.py`'s unit tests.

---

## 4. The write-only abstract

`article.abstract` is written by `corpus.build()` and read by nothing.
`store._SELECT` joins `article` to `article_chunk` and takes `c.text`; the
tool, the CLI's `status`, and `coverage()` never touch the column.

**Decision: drop it.** `article_chunk.text` is the retrievable copy, as it
already was in practice. The FTS5 table is external-content
(`content='article_chunk'`), so it holds an inverted index rather than a copy
of the text; it stays as it is.

- `LITERATURE_SCHEMA_VERSION` becomes 2. `SCHEMA_SQL` loses the column;
  `build()` stops writing it. `ParsedArticle.abstract` is unchanged: it is the
  in-memory value the chunks are cut from.
- **`literature build --rebuild` is added.** `schema.check_version` already
  tells the reader to run it, and the flag does not exist; it has been
  harmless only because no corpus has yet been at a different version. With
  v2 the message will fire for anyone holding a v1 corpus. `--rebuild` deletes
  `literature.db` and drops the `literature_chunks` vector table, then builds.
  It touches the corpus and nothing else; a test proves `health.db` and the
  personal vector table survive it, which is the mirror of slice 1's whole-
  branch finding that `reset` was destroying the corpus's vectors.
- Without `--rebuild`, building into a corpus at the wrong version fails with
  the existing `CorpusSchemaVersionMismatch` message, which now names a real
  flag.

**Not built: the LanceDB text copy.** After this change the abstract is still
held twice, in `article_chunk.text` and in the vector row's `text` column.
Removing the second would change `vector_store.embed_pending`, which the
personal path depends on, and the original design ruled out touching tested
personal-store code for the corpus's benefit. It is recorded here as measured
and deferred, worth roughly half the per-article vector bytes.

---

## 5. Documentation

- `docs/superpowers/specs/2026-08-18-literature-grounding-design.md`: the two
  open questions gain a pointer to §1 of this document. The original text is
  left in place; the answers live here.
- `README.md`: the corpus section's tier description gains the new tiers and
  the `min_tier` caveat.
- `ROADMAP.md`: #1's slice 1 summary notes the revised tiers and links here
  for the measurements; #2a's topic-pack option cites the 6.3 GB figure.
- `LICENSES.md`: unaffected.

---

## 6. Testing

- `tests/test_literature_tiers.py`: every new mapping; the protocol override
  with an RCT present; `Validation Study` resolving to `unknown` and appearing
  in `DELIBERATELY_UNMAPPED`; the renumbered ranks.
- `tests/test_literature_tool.py`: filtered miss carries `filtered: true` and
  the exclusion numbers; unfiltered miss keeps the "holds nothing" note;
  filtered hit carries the `filter` block; a protocol finding triggers
  `protocol_warning`; an unknown finding triggers `tier_warning` against the
  new fixture row.
- `tests/test_literature_schema.py`: version is 2; `article` has no
  `abstract` column.
- `tests/test_cli.py`: `--rebuild` recreates the corpus and leaves the
  personal index intact; a v1 corpus without `--rebuild` fails with the
  mismatch message.
- `tests/test_literature_search.py`: the vacuous unknown test now has a row to
  exclude.
- `tests/eval_questions.md` and `tests/test_eval.py`: Q21–Q26 re-verified
  against the two new fixture records.
- `offline-check` and `tests/test_no_network.py`: unchanged, still passing.

Delivered as one branch, one commit per section 2–4, plus one for §5, with a
whole-branch review before the PR.
