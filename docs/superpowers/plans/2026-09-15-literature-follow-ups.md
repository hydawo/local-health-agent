# Literature Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `min_tier` honest about what it excludes, map four real PubMed publication types deliberately, and stop storing the abstract in a column nothing reads.

**Architecture:** Three independent changes to the existing `health_agent/literature/` package and the `search_medical_literature` tool in `health_agent/agent/tools.py`. The store's search semantics are untouched; the tool payload grows, the tier table grows and renumbers, and the corpus schema goes to v2 with a `--rebuild` flag. No network code anywhere.

**Tech Stack:** Python 3.11+, SQLite (FTS5), LanceDB via `health_agent/store/vector_store.py`, pytest.

**Spec:** `docs/superpowers/specs/2026-09-15-literature-follow-ups-design.md` — read it first; every task below cites a section of it.

## Global Constraints

- **Tiers derive only from `PublicationType`.** Nothing inspects text. `tier_source` is `publication_type` or `unmapped`; there is no third value.
- **The store's `min_tier` semantics do not change.** `unknown` stays unranked and excluded by any floor. `tiers.tier_at_least`, `store._filters`, and `store._ordered` are not edited.
- **No network code.** Do not add imports of `urllib`, `requests`, `httpx`, or anything under `literature/fetch/`. `tests/test_no_network.py` must keep passing unchanged.
- **The personal index is never touched by a corpus operation.** `--rebuild` deletes `literature.db` and the `literature_chunks` vector table only.
- **Voice.** Module and function docstrings say *why*, not what. Tools return data, never prose. Absence is reported as coverage, never as an empty list. Match the surrounding code; read the file before editing it.
- **Fixture rule.** Any change to `tests/fixtures/literature/corpus.xml` is a change to `tests/eval_questions.md`, and `tests/test_eval.py` must still agree with it (`test_every_question_in_the_markdown_has_a_test`).
- **Commit messages:** imperative summary line, a body that says why, ending with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- **Run the full suite before every commit:** `pytest -q` from the repo root. All green, or do not commit.

---

### Task 1: Tier table — new tiers, renumbered ranks, protocol override

Implements spec §3.

**Files:**
- Modify: `health_agent/literature/tiers.py`
- Modify: `health_agent/agent/tools.py` (the `min_tier` enum only, around line 809)
- Test: `tests/test_literature_tiers.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `tiers.TIERS` (ranked tiers, insertion order strongest-first), `tiers.PROTOCOL = "protocol"`, `tiers.DELIBERATELY_UNMAPPED: frozenset[str]`, and `tiers.resolve()` returning `("protocol", None, "publication_type")` for any list containing `Clinical Trial Protocol`. Task 3's payload code and Task 2's fixture depend on these.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_literature_tiers.py`:

```python
@pytest.mark.parametrize("pub_types,expected,rank", [
    (["Clinical Trial"], "clinical_trial", 4),
    (["Clinical Trial, Phase I"], "clinical_trial", 4),
    (["Clinical Trial, Phase II"], "clinical_trial", 4),
    (["Clinical Trial, Phase III"], "clinical_trial", 4),
    (["Clinical Trial, Phase IV"], "clinical_trial", 4),
    (["Controlled Clinical Trial"], "clinical_trial", 4),
    (["Scoping Review"], "scoping_review", 5),
])
def test_new_mappings_from_real_pubmed_data(pub_types, expected, rank):
    tier, got_rank, source = tiers.resolve(pub_types)
    assert tier == expected
    assert got_rank == rank
    assert source == "publication_type"


def test_ranks_after_renumbering():
    """Pinned as a table so a later edit cannot quietly reorder the ladder."""
    assert tiers.TIERS == {
        "meta_analysis": 1,
        "systematic_review": 1,
        "guideline": 2,
        "rct": 3,
        "clinical_trial": 4,
        "narrative_review": 5,
        "scoping_review": 5,
        "observational": 6,
        "case_report": 7,
    }


def test_controlled_clinical_trial_is_not_an_rct():
    """PubMed's hierarchy: Clinical Trial > Controlled Clinical Trial >
    Randomized Controlled Trial. Controlled does not mean randomized, and
    slice 1's mapping of CCT to `rct` was a small credential inflation."""
    cct, cct_rank, _ = tiers.resolve(["Controlled Clinical Trial"])
    rct, rct_rank, _ = tiers.resolve(["Randomized Controlled Trial"])
    assert cct != rct
    assert cct_rank > rct_rank


def test_scoping_review_is_neither_systematic_nor_narrative():
    scoping, scoping_rank, _ = tiers.resolve(["Scoping Review"])
    _, systematic_rank, _ = tiers.resolve(["Systematic Review"])
    narrative, narrative_rank, _ = tiers.resolve(["Review"])
    assert scoping_rank > systematic_rank
    assert scoping_rank == narrative_rank
    assert scoping != narrative


def test_protocol_overrides_every_other_type():
    """Protocols of RCTs carry both `Clinical Trial Protocol` and `Randomized
    Controlled Trial`. Strongest-wins would mint an rct out of a plan that
    reports no results."""
    tier, rank, source = tiers.resolve(
        ["Journal Article", "Randomized Controlled Trial",
         "Clinical Trial Protocol"])
    assert tier == tiers.PROTOCOL == "protocol"
    assert rank is None
    assert source == "publication_type"


def test_protocol_is_unranked_so_a_floor_excludes_it():
    assert tiers.rank_of(tiers.PROTOCOL) is None
    assert tiers.tier_at_least(tiers.PROTOCOL, "case_report") is False


def test_protocol_is_not_unknown():
    """Both are unranked, and they are different facts: one is known and is
    not evidence, the other is not known. tier_source keeps them apart."""
    protocol = tiers.resolve(["Clinical Trial Protocol"])
    unknown = tiers.resolve(["Journal Article"])
    assert protocol[0] != unknown[0]
    assert protocol[2] == "publication_type"
    assert unknown[2] == "unmapped"


def test_validation_study_is_deliberately_unmapped():
    """Names a purpose, not a design. Mapping it would be inference by
    another name, so the decision is recorded rather than left looking like
    an omission."""
    assert "validation study" in tiers.DELIBERATELY_UNMAPPED
    tier, rank, source = tiers.resolve(["Validation Study"])
    assert tier == tiers.UNKNOWN
    assert rank is None
    assert source == "unmapped"


def test_deliberately_unmapped_types_never_appear_in_the_mapping():
    for raw in tiers.DELIBERATELY_UNMAPPED:
        assert tiers.resolve([raw])[0] == tiers.UNKNOWN
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_literature_tiers.py -q`
Expected: the new tests fail with `AttributeError: ... has no attribute 'PROTOCOL'` / `DELIBERATELY_UNMAPPED`, and the mapping tests fail with `unknown != clinical_trial`. The seven pre-existing tests still pass.

- [ ] **Step 3: Rewrite `tiers.py`**

Replace the whole of `health_agent/literature/tiers.py` with:

```python
"""Evidence tier from the source's own metadata, and from nothing else.

A tier is read off MEDLINE's structured `PublicationType` field or it is
`unknown`. Nothing here inspects a title or an abstract, and nothing asks a
model. An inferred tier is a fabricated credential, and the feature's whole
value is that its citations can be trusted.

**`Review` is not `Systematic Review`.** PubMed applies `Review` to an enormous
number of narrative articles; mapping it to the top tier would inflate exactly
the credential this module exists to report honestly. Measured against 2,000
real abstracts, narrative reviews (12.4%) outnumbered systematic reviews and
meta-analyses combined (7.2%), so the split is worth roughly a tripling of the
apparent top tier. It gets rank 5 — below trials, above observational —
because an expert review is weak *evidence* but usually a reliable *summary*.

**Most of PubMed has no tier at all.** In the same sample 65.5% of articles
resolved to `unknown`, and five in six of those carried `Journal Article` and
nothing else. That is not a mapping gap to close: PubMed does not record a
study design for most primary research, and any tier assigned there would be
invented. It does mean a `min_tier` floor excludes most of a real corpus,
which the search tool now says out loud rather than leaving implicit.

**A protocol is not a result.** `Clinical Trial Protocol` overrides every
other type on the article, because protocols of RCTs are tagged with both and
strongest-wins would mint an `rct` out of a plan. It is unranked, like
`unknown`, but for the opposite reason: we know exactly what it is, and it is
not evidence.
"""

from __future__ import annotations

UNKNOWN = "unknown"
PROTOCOL = "protocol"

# tier key -> rank. Lower is stronger. Insertion order is strongest-first and
# is what the tool's `min_tier` enum is built from. `unknown` and `protocol`
# are deliberately absent: unranked rather than ranked last, so a min_tier
# filter excludes them instead of admitting them at the bottom.
TIERS: dict[str, int] = {
    "meta_analysis": 1,
    "systematic_review": 1,
    "guideline": 2,
    "rct": 3,
    "clinical_trial": 4,
    "narrative_review": 5,
    "scoping_review": 5,
    "observational": 6,
    "case_report": 7,
}

# MEDLINE PublicationType (lowercased) -> tier key.
#
# `Controlled Clinical Trial` sits under `clinical_trial`, not `rct`: in
# PubMed's own hierarchy it is the parent of `Randomized Controlled Trial`,
# and controlled does not mean randomized. The phase variants are PubMed's
# children of `Clinical Trial`; left unmapped, a Phase III trial would be
# `unknown`.
_BY_PUBLICATION_TYPE: dict[str, str] = {
    "meta-analysis": "meta_analysis",
    "systematic review": "systematic_review",
    "practice guideline": "guideline",
    "guideline": "guideline",
    "randomized controlled trial": "rct",
    "clinical trial": "clinical_trial",
    "clinical trial, phase i": "clinical_trial",
    "clinical trial, phase ii": "clinical_trial",
    "clinical trial, phase iii": "clinical_trial",
    "clinical trial, phase iv": "clinical_trial",
    "controlled clinical trial": "clinical_trial",
    "review": "narrative_review",
    "scoping review": "scoping_review",
    "observational study": "observational",
    "comparative study": "observational",
    "cohort studies": "observational",
    "case-control studies": "observational",
    "case reports": "case_report",
}

_PROTOCOL_TYPE = "clinical trial protocol"

# Types that appeared in real data and were considered and left unmapped,
# on purpose. `Validation Study` names what a study is for, not how it was
# designed — a validation study can be a cohort, a trial, or a case series —
# so any tier would be a guess. Listed so the decision reads as a decision.
DELIBERATELY_UNMAPPED: frozenset[str] = frozenset({
    "validation study",
})


def rank_of(tier: str) -> int | None:
    return TIERS.get(tier)


def _normalise(raw: object) -> str:
    return str(raw).strip().lower()


def resolve(publication_types: list[str]) -> tuple[str, int | None, str]:
    """Return `(tier, rank, tier_source)` for a MEDLINE publication type list.

    A protocol overrides everything else on the article. Otherwise, when
    several types map, the strongest wins — an article tagged both `Review`
    and `Meta-Analysis` is a meta-analysis.
    """
    normalised = [_normalise(raw) for raw in publication_types]
    if _PROTOCOL_TYPE in normalised:
        return PROTOCOL, None, "publication_type"

    best: str | None = None
    for raw in normalised:
        tier = _BY_PUBLICATION_TYPE.get(raw)
        if tier is None:
            continue
        if best is None or TIERS[tier] < TIERS[best]:
            best = tier
    if best is None:
        return UNKNOWN, None, "unmapped"
    return best, TIERS[best], "publication_type"


def tier_at_least(tier: str, min_tier: str) -> bool:
    """True when `tier` is at least as strong as `min_tier`.

    `unknown` and `protocol` are never at least anything: neither has a rank
    to compare.
    """
    rank, floor = rank_of(tier), rank_of(min_tier)
    if rank is None or floor is None:
        return False
    return rank <= floor
```

- [ ] **Step 4: Derive the tool's `min_tier` enum from `TIERS`**

In `health_agent/agent/tools.py`, add near the other top-level imports (check the file's existing import block; `from ..literature import tiers as lit_tiers` is safe — `tiers.py` imports nothing):

```python
from ..literature import tiers as lit_tiers
```

Then replace the hard-coded enum in the `search_medical_literature` tool definition:

```python
                "min_tier": {
                    "type": "string",
                    "enum": list(lit_tiers.TIERS),
```

Leave the `description` string alone in this task; Task 3 rewrites it.

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_literature_tiers.py tests/test_literature_tool.py tests/test_eval.py tests/test_no_network.py -q`
Expected: all pass. `test_eval.py` asserts `evidence_rank == 3` for rct and `== 2` for guideline, both unchanged by the renumbering.

- [ ] **Step 6: Run the full suite and commit**

Run: `pytest -q`
Expected: all pass.

```bash
git add health_agent/literature/tiers.py health_agent/agent/tools.py tests/test_literature_tiers.py
git commit -m "Map four real publication types; protocols override, validation studies stay unknown

Against 2,000 real abstracts four PublicationType values appeared unmapped:
Validation Study, Scoping Review, Clinical Trial Protocol, Clinical Trial.
Each is now decided rather than defaulted. Clinical Trial (and its phase
variants) gets its own tier below rct, and Controlled Clinical Trial moves
there from rct, since in PubMed's hierarchy it is the parent of RCT rather
than a synonym. Scoping Review sits beside narrative review, not at the top.
Clinical Trial Protocol overrides everything else on the article and is
unranked, because protocols of RCTs carry both tags and strongest-wins
would mint an rct out of a plan. Validation Study is recorded as
deliberately unmapped: it names a purpose, not a design.

Ranks renumber; evidence_rank is derived at build and the schema bump in a
later commit forces a rebuild. The tool's min_tier enum is now derived from
the table so the two cannot drift.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Fixture — an `unknown` article and an RCT-tagged protocol

Implements spec §2 (fixture paragraph) and §3 (fixture paragraph).

**Files:**
- Modify: `tests/fixtures/literature/corpus.xml`
- Modify: `tests/eval_questions.md` (the "Literature grounding" section, from line ~186)
- Modify: `tests/test_literature_search.py:75-78`
- Test: `tests/test_eval.py`, `tests/test_literature_search.py`

**Interfaces:**
- Consumes: `tiers.resolve()` from Task 1 (the protocol override must exist before the fixture is built, or PMID 40000010 resolves to `rct`).
- Produces: two fixture PMIDs Task 3's tool tests query by name: **40000009** (`unknown`, matched by the tokens `cuff` and `confidence`) and **40000010** (`protocol`, matched by `reminders` and `statin`).

**Why these MeSH terms.** `test_q23_hip_replacement_recovery_is_a_corpus_miss` pins `coverage()["topics"]` as `["Cholesterol", "Hypertension", "LDL", "Exercise", "Heart Rate", "Sleep"]`. The order is `Counter.most_common`, which is by count, then insertion order on ties. Current counts: Cholesterol 4, Hypertension 3, LDL 3, Exercise 2, Heart Rate 2, Sleep 2. Giving 40000009 `Hypertension` + `Cholesterol` and 40000010 `LDL` + `Cholesterol` yields 6, 4, 4, 2, 2, 2: the same tie groups as before, so the order is preserved. Do not pick other terms.

**Why these words.** Several tests assert exact hit lists from FTS, which OR-matches every token. The new abstracts must not contain: `heart`, `rescreening`, `interval`, `diabetes`, `A1c`, `hip`, `replacement`, `recovery`, `sleep`, `metabolic`, `diet`, `fibre`, `supplement`, `exercise`. The texts below have been checked against that list; keep them verbatim.

- [ ] **Step 1: Add the two records to the fixture**

Insert before the closing `</PubmedArticleSet>` in `tests/fixtures/literature/corpus.xml`:

```xml
 <PubmedArticle>
  <MedlineCitation>
   <PMID>40000009</PMID>
   <Article>
    <Journal>
     <JournalIssue><PubDate><Year>2023</Year></PubDate></JournalIssue>
     <Title>Synthetic Primary Research Letters</Title>
    </Journal>
    <ArticleTitle>Home blood pressure monitoring adherence in adults with hypertension.</ArticleTitle>
    <Abstract>
     <AbstractText Label="BACKGROUND">Adherence to home blood pressure monitoring is variable among adults with hypertension, and the reasons are not well described.</AbstractText>
     <AbstractText Label="RESULTS">Among adults asked to record home blood pressure twice daily, adherence declined over twelve weeks and was lowest among participants who reported little confidence in operating the cuff.</AbstractText>
    </Abstract>
    <PublicationTypeList>
     <PublicationType>Journal Article</PublicationType>
    </PublicationTypeList>
   </Article>
   <MeshHeadingList>
    <MeshHeading><DescriptorName>Hypertension</DescriptorName></MeshHeading>
    <MeshHeading><DescriptorName>Cholesterol</DescriptorName></MeshHeading>
   </MeshHeadingList>
  </MedlineCitation>
  <PubmedData>
   <ArticleIdList>
    <ArticleId IdType="doi">10.9999/synth.40000009</ArticleId>
   </ArticleIdList>
  </PubmedData>
 </PubmedArticle>

 <PubmedArticle>
  <MedlineCitation>
   <PMID>40000010</PMID>
   <Article>
    <Journal>
     <JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue>
     <Title>Synthetic Trial Protocols</Title>
    </Journal>
    <ArticleTitle>Text-message reminders for statin adherence in adults with elevated LDL cholesterol: protocol for a randomized controlled trial.</ArticleTitle>
    <Abstract>
     <AbstractText Label="BACKGROUND">Adherence to statin therapy is incomplete in adults with elevated LDL cholesterol.</AbstractText>
     <AbstractText Label="METHODS">We will randomize adults prescribed a statin to daily text-message reminders or usual care for six months. The primary outcome is pharmacy-refill adherence.</AbstractText>
     <AbstractText Label="DISCUSSION">This protocol describes a planned trial. No results are available.</AbstractText>
    </Abstract>
    <PublicationTypeList>
     <PublicationType>Journal Article</PublicationType>
     <PublicationType>Clinical Trial Protocol</PublicationType>
     <PublicationType>Randomized Controlled Trial</PublicationType>
    </PublicationTypeList>
   </Article>
   <MeshHeadingList>
    <MeshHeading><DescriptorName>LDL</DescriptorName></MeshHeading>
    <MeshHeading><DescriptorName>Cholesterol</DescriptorName></MeshHeading>
   </MeshHeadingList>
  </MedlineCitation>
  <PubmedData>
   <ArticleIdList>
    <ArticleId IdType="doi">10.9999/synth.40000010</ArticleId>
   </ArticleIdList>
  </PubmedData>
 </PubmedArticle>
```

- [ ] **Step 2: Make the vacuous search test real**

Replace `test_min_tier_excludes_unknown_rather_than_ranking_it_last` in `tests/test_literature_search.py` (currently lines 75-78) with:

```python
def test_min_tier_excludes_unknown_rather_than_ranking_it_last(built):
    """PMID 40000009 carries `Journal Article` and nothing else, so it is
    `unknown`. Unfiltered it is findable; under any floor it is gone. Before
    the fixture held an unknown row this test passed vacuously."""
    conn, _, _ = built
    unfiltered = lit_store.keyword_search(conn, "cuff confidence")
    assert "40000009" in {h.pmid for h in unfiltered}
    assert any(h.evidence_tier == "unknown" for h in unfiltered)

    hits = lit_store.keyword_search(conn, "cuff confidence",
                                    min_tier="case_report")
    assert "40000009" not in {h.pmid for h in hits}
    assert all(h.evidence_tier != "unknown" for h in hits)


def test_protocol_is_findable_unfiltered_and_excluded_by_any_floor(built):
    """PMID 40000010 is tagged both Clinical Trial Protocol and Randomized
    Controlled Trial. It resolves to `protocol`, unranked, so a floor excludes
    it exactly as it excludes `unknown`."""
    conn, _, _ = built
    unfiltered = lit_store.keyword_search(conn, "reminders statin")
    protocol = next(h for h in unfiltered if h.pmid == "40000010")
    assert protocol.evidence_tier == "protocol"
    assert protocol.evidence_rank is None
    assert protocol.tier_source == "publication_type"

    hits = lit_store.keyword_search(conn, "reminders statin",
                                    min_tier="case_report")
    assert "40000010" not in {h.pmid for h in hits}
```

- [ ] **Step 3: Run the literature and eval tests**

Run: `pytest tests/test_literature_search.py tests/test_eval.py tests/test_literature_corpus.py tests/test_literature_medline.py tests/test_no_network.py -q`
Expected: all pass, including `test_q23_...` with the unchanged topics list and `test_q25_...` still returning only 40000002 under the filter. If `test_q23` fails on topic order, the MeSH choice above was not followed; fix the fixture, not the test.

- [ ] **Step 4: Update `tests/eval_questions.md`**

In the "Literature grounding" section, change the opening sentence `Six questions against tests/fixtures/literature/corpus.xml (built with the test@1 pack).` to read:

```markdown
Six questions against `tests/fixtures/literature/corpus.xml` (built with the
`test@1` pack). The fixture holds ten synthetic records. Two exist to
exercise unranked tiers rather than to be cited by any question here:
**PMID 40000009** carries `Journal Article` and nothing else, so it is
`unknown` — the shape 65.5% of a real corpus takes — and **PMID 40000010**
is tagged both `Clinical Trial Protocol` and `Randomized Controlled Trial`,
so it resolves to `protocol` rather than `rct`. Both are findable unfiltered
and excluded by any `min_tier` floor; neither matches the queries Q21–Q26
use, by construction (their abstracts avoid every token those queries
carry).
```

Then in Q25's `expected` bullet, after `which the unfiltered query for the same terms also returns.` add:

```markdown
  The filter also excludes the corpus's unranked rows (`unknown`, `protocol`),
  which is the behaviour §2 of the follow-ups spec makes visible in the tool
  payload: a filtered miss reports how much of the corpus has no recorded
  study design instead of claiming the corpus holds nothing.
```

- [ ] **Step 5: Run the whole suite and commit**

Run: `pytest -q`
Expected: all pass.

```bash
git add tests/fixtures/literature/corpus.xml tests/eval_questions.md tests/test_literature_search.py
git commit -m "Fixture: add an unknown-tier article and an RCT-tagged protocol

No fixture article was unknown, so the test asserting that min_tier excludes
unknown rather than ranking it last passed against nothing, and the tool's
tier_warning branch had never run against a real row. Against real PubMed
data unknown is 65.5% of the corpus. One Journal-Article-only record fixes
that. A second record tagged both Clinical Trial Protocol and Randomized
Controlled Trial proves the protocol override end to end rather than only in
tiers.py's unit tests.

MeSH terms and abstract wording are chosen so every existing exact-match
assertion (Q23's topic order, Q25's filtered hit list, Q26's single guideline)
is unchanged; eval_questions.md says so.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Tool payload — report what `min_tier` excludes; flag protocols

Implements spec §2 (items 1–3) and §3 (tool payload paragraph).

**Files:**
- Modify: `health_agent/agent/tools.py` — `_search_medical_literature` (around lines 540–625) and the `min_tier` parameter description (around line 812)
- Test: `tests/test_literature_tool.py`

**Interfaces:**
- Consumes: `lit_corpus.coverage(conn)["tiers"]` (a `{tier: count}` dict, already present), `lit_corpus.coverage(conn)["article_count"]`, `lit_tiers.PROTOCOL`, `lit_tiers.UNKNOWN`, fixture PMIDs 40000009 and 40000010 from Task 2.
- Produces: payload keys `filtered: bool`, `filter: dict`, `protocol_warning: str`. Shape:

```python
payload["filter"] = {
    "min_tier": "rct" | None,
    "since_year": 2020 | None,
    # present only when min_tier was set:
    "unranked_articles_excluded": 2,
    "unranked_share": 0.2,
}
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_literature_tool.py`:

```python
def _search(ctx, **args):
    return agent_tools.dispatch(ctx, "search_medical_literature", args)


def test_a_filtered_miss_is_not_reported_as_a_coverage_gap(ctx):
    """With a floor set, 'the corpus holds nothing on this' is false: PMID
    40000009 matches these terms and is simply untagged. The payload must say
    the filter did the excluding, and how much of the corpus it excludes."""
    payload = _search(ctx, query="cuff confidence", min_tier="rct")
    assert payload["no_matches"] is True
    assert payload["filtered"] is True
    assert payload["filter"]["min_tier"] == "rct"
    assert payload["filter"]["unranked_articles_excluded"] >= 1
    assert 0 < payload["filter"]["unranked_share"] < 1
    note = payload["note"].lower()
    assert "holds nothing" not in note
    assert "no recorded study design" in note
    assert "without min_tier" in note
    assert "do not answer from general knowledge" in note


def test_an_unfiltered_miss_keeps_the_coverage_wording(ctx):
    payload = _search(ctx, query="zzzz nonexistent orthopedic arthroplasty topic")
    assert payload["no_matches"] is True
    assert "filtered" not in payload
    assert "filter" not in payload
    assert "holds nothing" in payload["note"].lower()


def test_a_since_year_only_miss_is_filtered_without_the_unranked_numbers(ctx):
    """The unranked share only explains a tier floor. Quoting it on a
    year-only miss would blame the wrong filter."""
    payload = _search(ctx, query="cuff confidence", since_year=2090)
    assert payload["no_matches"] is True
    assert payload["filtered"] is True
    assert payload["filter"]["since_year"] == 2090
    assert "unranked_articles_excluded" not in payload["filter"]
    assert "no recorded study design" not in payload["note"].lower()


def test_a_filtered_hit_reports_what_the_floor_excluded(ctx):
    payload = _search(ctx, query="blood pressure", min_tier="rct")
    assert payload["findings"]
    assert payload["filter"]["min_tier"] == "rct"
    assert payload["filter"]["unranked_articles_excluded"] >= 1
    assert "no recorded study design" in payload["filter_note"].lower()


def test_an_unfiltered_hit_carries_no_filter_block(ctx):
    payload = _search(ctx, query="blood pressure")
    assert payload["findings"]
    assert "filter" not in payload
    assert "filter_note" not in payload


def test_unknown_findings_carry_the_tier_warning(ctx):
    """Runs against a real unknown row (PMID 40000009) for the first time."""
    payload = _search(ctx, query="cuff confidence")
    assert any(f["pmid"] == "40000009" and f["evidence_tier"] == "unknown"
               for f in payload["findings"])
    assert "study design is unknown" in payload["tier_warning"].lower()


def test_protocol_findings_carry_their_own_warning(ctx):
    payload = _search(ctx, query="reminders statin")
    protocol = next(f for f in payload["findings"] if f["pmid"] == "40000010")
    assert protocol["evidence_tier"] == "protocol"
    assert protocol["tier_source"] == "publication_type"
    assert "no results" in payload["protocol_warning"].lower()
    # A protocol is not an unknown; its warning is its own.
    assert "tier_warning" not in payload or "40000010" not in payload["tier_warning"]


def test_min_tier_description_warns_that_most_of_the_corpus_is_untagged():
    schema = agent_tools.BY_NAME["search_medical_literature"].schema()
    params = schema["function"]["parameters"]
    description = params["properties"]["min_tier"]["description"].lower()
    assert "no study-design tag" in description or "no recorded study design" in description
    assert "strongest-first" in description or "strongest first" in description
    assert "protocol" not in params["properties"]["min_tier"]["enum"]
    assert "unknown" not in params["properties"]["min_tier"]["enum"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_literature_tool.py -q`
Expected: the seven new tests fail on missing keys (`KeyError: 'filtered'`, `'filter'`, `'protocol_warning'`, `'filter_note'`) and on the description assertion. The nine pre-existing tests pass.

- [ ] **Step 3: Implement the payload changes**

In `_search_medical_literature` in `health_agent/agent/tools.py`, after `coverage = lit_corpus.coverage(ctx.literature_conn)` and before `if not findings:`, add:

```python
    # A floor excludes every unranked article, and on a real corpus that is
    # most of it: 65.5% of 2,000 PubMed abstracts resolved to `unknown`
    # because PubMed records no study design for most primary research. The
    # numbers here are corpus-level and exact, from coverage(); a per-query
    # count over an FTS OR-match would be true and useless.
    filter_block: dict | None = None
    if min_tier or since_year:
        filter_block = {"min_tier": min_tier, "since_year": since_year}
        if min_tier:
            unranked = (coverage["tiers"].get(lit_tiers.UNKNOWN, 0)
                        + coverage["tiers"].get(lit_tiers.PROTOCOL, 0))
            total = coverage["article_count"] or 1
            filter_block["unranked_articles_excluded"] = unranked
            filter_block["unranked_share"] = round(unranked / total, 3)
```

Replace the existing `if not findings:` block with:

```python
    if not findings:
        miss: dict = {
            "corpus_installed": True,
            "findings": [],
            "no_matches": True,
            "corpus": coverage,
        }
        if filter_block is None:
            miss["note"] = (
                "The corpus holds nothing on this. Say that your literature "
                "does not cover it, name what it does cover, and do not answer "
                "from general knowledge.")
        else:
            # Not a coverage gap: the corpus may hold plenty on this, untagged.
            # Saying 'holds nothing' here would send the model to recall.
            miss["filtered"] = True
            miss["filter"] = filter_block
            reason = "Nothing matched under these filters. "
            if min_tier:
                reason += (
                    f"{filter_block['unranked_articles_excluded']} of "
                    f"{coverage['article_count']} articles in this corpus "
                    f"({filter_block['unranked_share']:.0%}) have no recorded "
                    f"study design and are excluded by any min_tier floor; "
                    f"that is normal for PubMed, not a sign of thin coverage. ")
            miss["note"] = (
                reason + "Retry without min_tier and since_year before "
                "concluding that the corpus does not cover this, and do not "
                "answer from general knowledge.")
        return miss
```

In the hit payload, after the existing `tier_warning` block, add:

```python
    if filter_block is not None:
        payload["filter"] = filter_block
        if min_tier:
            payload["filter_note"] = (
                f"min_tier={min_tier} excluded the "
                f"{filter_block['unranked_articles_excluded']} articles "
                f"({filter_block['unranked_share']:.0%} of this corpus) that "
                f"have no recorded study design. Stronger evidence is not "
                f"hidden behind them: results are ordered strongest-first "
                f"without any floor.")
    if any(f.evidence_tier == lit_tiers.PROTOCOL for f in findings):
        payload["protocol_warning"] = (
            "One or more findings are trial PROTOCOLS: they describe a "
            "planned study and report no results. Say so beside the citation, "
            "and do not present a protocol as evidence of anything.")
```

Then change the existing `tier_warning` condition so it fires for `unknown` only (it already does — `f.evidence_tier == "unknown"` — but replace the literal with `lit_tiers.UNKNOWN` for consistency).

- [ ] **Step 4: Rewrite the `min_tier` parameter description**

In the tool definition, replace the `description` under `"min_tier"` with:

```python
                    "description": (
                        "Floor on study design, strongest to weakest. Most "
                        "primary research in PubMed carries no study-design "
                        "tag, so ANY floor excludes the majority of the "
                        "corpus, not just weak studies. Results are already "
                        "ordered strongest-first without it. Set it only when "
                        "the user asks for evidence at a stated strength, and "
                        "if it returns nothing, retry without it."
                    ),
```

- [ ] **Step 5: Run the tool tests**

Run: `pytest tests/test_literature_tool.py -q`
Expected: all pass.

- [ ] **Step 6: Run the whole suite and commit**

Run: `pytest -q`
Expected: all pass. `test_guardrail.py` and `test_agent.py` are unaffected but must be green.

```bash
git add health_agent/agent/tools.py tests/test_literature_tool.py
git commit -m "Say what min_tier excludes instead of calling a filtered miss a coverage gap

On a real corpus 65.5% of articles have no recorded study design and any
min_tier floor drops all of them. The tool used to answer a filtered miss
with 'the corpus holds nothing on this', which with a floor set is false
and sends the model to recall, the exact failure the tool exists to
prevent. A filtered miss now says the filter did the excluding, quotes how
much of the corpus has no recorded design, and says to retry without the
floor; a filtered hit carries the same numbers; the parameter description
says most of the corpus is untagged and that results are strongest-first
anyway. No new parameter: an include_unknown switch would be one more
argument to misuse. The store's semantics are unchanged.

Protocol findings get their own warning, since a plan that reports no
results is a different fact from an article whose design is unknown.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Schema v2 — drop the write-only abstract, check the version, add `--rebuild`

Implements spec §4.

**Files:**
- Modify: `health_agent/literature/schema.py`
- Modify: `health_agent/literature/corpus.py:69-98` (the `INSERT INTO article`)
- Modify: `health_agent/cli.py` — `cmd_literature_build` (~line 1223), `cmd_literature_status` (~1262), the ask path's corpus open (~line 773), and the `literature build` argparse block (~line 1676)
- Test: `tests/test_literature_schema.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `vector_store.VectorStore(path, table_name=...).drop()` (exists, line ~153 of `store/vector_store.py`), `lit_embed.TABLE_NAME`, `cfg.literature_path`, `cfg.literature_vector_path`.
- Produces: `schema.connect()` raises `CorpusSchemaVersionMismatch` when opening an existing corpus at the wrong version; `health-agent literature build --rebuild`.

**Two latent bugs this task fixes, beyond the spec's wording.** `schema.check_version` is defined and never called anywhere, so the ask path, `status`, and `build` all open a corpus blind. And `cmd_literature_build` calls `initialize()` on an existing file, which would re-stamp a v1 corpus as v2 without changing its shape or its stale `evidence_rank` values. Both matter now that a v2 exists.

- [ ] **Step 1: Write the failing schema tests**

Append to `tests/test_literature_schema.py`:

```python
def test_schema_version_is_2():
    assert schema.LITERATURE_SCHEMA_VERSION == 2


def test_article_has_no_abstract_column(tmp_path):
    """Written by build() and read by nothing: article_chunk.text is the
    retrievable copy, and on a real corpus the duplicate was a quarter of
    the database."""
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(article)")}
    assert "abstract" not in columns
    assert "title" in columns
    conn.close()


def test_connect_refuses_a_corpus_at_another_version(tmp_path):
    """check_version existed and was called by nobody; the ask path, status,
    and build all opened a corpus blind. Opening is where the check belongs,
    so no caller can forget it."""
    path = tmp_path / "literature.db"
    conn = schema.connect(path, create=True)
    schema.initialize(conn)
    conn.execute("UPDATE corpus_meta SET value = '1' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    with pytest.raises(schema.CorpusSchemaVersionMismatch) as excinfo:
        schema.connect(path)
    assert "--rebuild" in str(excinfo.value)

    # create=True on an existing file is still an open, not a fresh build.
    with pytest.raises(schema.CorpusSchemaVersionMismatch):
        schema.connect(path, create=True)


def test_connect_creates_a_fresh_corpus_without_a_version_check(tmp_path):
    conn = schema.connect(tmp_path / "new.db", create=True)
    assert schema.read_version(conn) is None
    schema.initialize(conn)
    assert schema.read_version(conn) == schema.LITERATURE_SCHEMA_VERSION
    conn.close()
```

- [ ] **Step 2: Write the failing CLI tests**

Append to `tests/test_cli.py`:

```python
def test_literature_rebuild_replaces_the_corpus_and_spares_the_personal_index(
        cli, tmp_path):
    """The mirror of test_reset_does_not_destroy_the_literature_corpus:
    --rebuild reaches literature.db and the literature_chunks table, and
    nothing else."""
    from health_agent import config as config_mod
    from health_agent.literature import schema as lit_schema
    from health_agent.store import sqlite_schema

    lit_fixture = Path(__file__).parent / "fixtures" / "literature" / "corpus.xml"
    code, _ = cli("literature", "build", "--from", str(lit_fixture),
                  "--slug", "test", "--version", "1",
                  "--embed-backend", "hashing")
    assert code == 0
    cfg = config_mod.resolve(index_path=str(tmp_path / "health.db"))

    personal = sqlite_schema.connect(cfg.index_path)
    records_before = personal.execute("SELECT COUNT(*) AS n FROM record").fetchone()["n"]
    personal.close()
    assert records_before > 0

    # Pretend the corpus is from an older schema.
    lit = lit_schema.connect(cfg.literature_path)
    lit.execute("UPDATE corpus_meta SET value = '1' WHERE key = 'schema_version'")
    lit.commit()
    lit.close()

    code, out = cli("literature", "build", "--from", str(lit_fixture),
                    "--slug", "test", "--version", "1",
                    "--embed-backend", "hashing")
    assert code != 0                      # refused: wrong version, no --rebuild

    code, out = cli("literature", "build", "--from", str(lit_fixture),
                    "--slug", "test", "--version", "1",
                    "--embed-backend", "hashing", "--rebuild")
    assert code == 0
    assert "articles" in out

    lit = lit_schema.connect(cfg.literature_path)
    assert lit_schema.read_version(lit) == lit_schema.LITERATURE_SCHEMA_VERSION
    assert lit.execute("SELECT COUNT(*) AS n FROM article").fetchone()["n"] > 0
    lit.close()

    personal = sqlite_schema.connect(cfg.index_path)
    assert personal.execute("SELECT COUNT(*) AS n FROM record").fetchone()["n"] == records_before
    personal.close()
    assert cfg.vector_path.exists()


def test_literature_build_into_a_stale_corpus_names_the_rebuild_flag(
        tmp_path, capsys):
    from health_agent.cli import main
    from health_agent import config as config_mod
    from health_agent.literature import schema as lit_schema

    fixture = Path(__file__).parent / "fixtures" / "literature" / "corpus.xml"
    index = tmp_path / ".index" / "health.db"
    assert main(["--index", str(index), "literature", "build", "--from",
                 str(fixture), "--no-embed"]) == 0
    capsys.readouterr()

    cfg = config_mod.resolve(index_path=str(index))
    lit = lit_schema.connect(cfg.literature_path)
    lit.execute("UPDATE corpus_meta SET value = '1' WHERE key = 'schema_version'")
    lit.commit()
    lit.close()

    code = main(["--index", str(index), "literature", "build", "--from",
                 str(fixture), "--no-embed"])
    assert code == 2
    assert "--rebuild" in capsys.readouterr().err

    code = main(["--index", str(index), "literature", "status"])
    assert code != 0
    assert "--rebuild" in capsys.readouterr().err


def test_ask_path_survives_a_stale_corpus(tmp_path, capsys):
    """A wrong-version corpus must not crash `ask`; it is reported and the
    tool sees no corpus, which it already knows how to say."""
    from health_agent.cli import main
    from health_agent import config as config_mod
    from health_agent.literature import schema as lit_schema

    fixture = Path(__file__).parent / "fixtures" / "literature" / "corpus.xml"
    index = tmp_path / ".index" / "health.db"
    assert main(["--index", str(index), "literature", "build", "--from",
                 str(fixture), "--no-embed"]) == 0
    cfg = config_mod.resolve(index_path=str(index))
    lit = lit_schema.connect(cfg.literature_path)
    lit.execute("UPDATE corpus_meta SET value = '1' WHERE key = 'schema_version'")
    lit.commit()
    lit.close()
    capsys.readouterr()

    from health_agent import cli as cli_mod
    opened = cli_mod._open_literature_corpus(cfg)
    err = capsys.readouterr().err
    assert opened is None
    assert "--rebuild" in err
```

Before writing the third test, read how the ask path is structured around `cli.py:765-790`. The plan extracts the corpus-open into a helper `_open_literature_corpus(cfg) -> sqlite3.Connection | None` so it can be tested without running the model; if the file has a different natural seam, use it and adjust the test, but the behaviour (stderr message naming `--rebuild`, corpus treated as absent, no exception) is fixed. The personal `record` table exists under that name.

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `pytest tests/test_literature_schema.py tests/test_cli.py -q -k "version_is_2 or abstract_column or refuses_a_corpus or fresh_corpus or rebuild or stale_corpus"`
Expected: fail — version is 1, `abstract` column present, `connect` does not raise, `--rebuild` is an unrecognised argument, `_open_literature_corpus` does not exist.

- [ ] **Step 4: Bump the schema and drop the column**

In `health_agent/literature/schema.py`:

```python
LITERATURE_SCHEMA_VERSION = 2
```

Remove the line `    abstract            TEXT,` from `SCHEMA_SQL`'s `article` table and add, in its place, a comment:

```sql
    -- No abstract column. article_chunk.text is the retrievable copy; a
    -- second copy here was written by build() and read by nothing, and on a
    -- real corpus it was a quarter of the database. (v1 had it.)
```

Replace `connect()` with:

```python
def connect(path: Path, *, create: bool = False) -> sqlite3.Connection:
    """Open a corpus, refusing one written by another schema version.

    The version check lives here rather than in callers because every caller
    forgot it: the ask path, `status`, and `build` all opened a corpus blind
    while `check_version` sat unused. A fresh file (create=True, nothing on
    disk yet) has no version to check and is stamped by `initialize`.
    """
    path = Path(path)
    existed = path.exists()
    if not existed:
        if not create:
            raise CorpusNotFound(
                f"No literature corpus at {path}. Run "
                f"`health-agent literature build` first."
            )
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    if existed:
        try:
            check_version(conn)
        except CorpusSchemaVersionMismatch:
            conn.close()
            raise
    return conn
```

Update `check_version`'s message so the flag it names is spelled exactly as argparse will accept it (it already says `--rebuild`; keep it, and it now points at a real flag).

- [ ] **Step 5: Stop writing the abstract in `build()`**

In `health_agent/literature/corpus.py`, the `INSERT INTO article(...)` statement: remove `abstract` from the column list, remove one `?` from the `VALUES` list (16 → 15), remove `abstract = excluded.abstract, ` from the `ON CONFLICT` clause, and remove `body` from the parameter tuple (the tuple starts `(pack_id, article.pmid, article.doi, article.title, article.journal, ...)`). `body` is still used for the chunk loop below; leave that.

- [ ] **Step 6: Add `--rebuild` and the version handling to the CLI**

In `health_agent/cli.py`, in the `literature build` argparse block, add after `--no-embed`:

```python
    p_lit_build.add_argument(
        "--rebuild", action="store_true",
        help="delete the existing corpus (literature.db and its vector table) "
             "and build from scratch; the personal index is not touched")
```

Rewrite `cmd_literature_build` so the corpus open handles the version:

```python
def cmd_literature_build(args: argparse.Namespace, cfg: config.Config) -> int:
    from .literature import corpus as lit_corpus
    from .literature import embed as lit_embed
    from .literature import medline, schema as lit_schema

    source = Path(args.source).expanduser()
    if not source.is_file():
        print(f"No such file: {source}", file=sys.stderr)
        return 2

    try:
        articles = medline.parse_articles(source.read_bytes())
    except medline.MedlineParseError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.rebuild:
        _delete_literature_corpus(cfg)

    try:
        conn = lit_schema.connect(cfg.literature_path, create=True)
    except lit_schema.CorpusSchemaVersionMismatch as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        lit_schema.initialize(conn)
        ...  # the rest of the function is unchanged from here
```

Add the helper beside it:

```python
def _delete_literature_corpus(cfg: config.Config) -> None:
    """The corpus and its vector table, and nothing else.

    `reset` once destroyed the corpus's vectors because two paths resolved
    to one directory; this is the same boundary from the other side, and
    the test for it asserts the personal index is untouched.
    """
    from .literature import embed as lit_embed

    if cfg.literature_path.exists():
        cfg.literature_path.unlink()
        for suffix in ("-wal", "-shm"):
            cfg.literature_path.with_name(
                cfg.literature_path.name + suffix).unlink(missing_ok=True)
    if cfg.literature_vector_path.exists():
        vector_store.VectorStore(cfg.literature_vector_path,
                                 table_name=lit_embed.TABLE_NAME).drop()
```

`vector_store` is already imported at the top of `cli.py` (`from .store import queries, sqlite_schema, vector_store`).

In `cmd_literature_status`, widen the `except` on the open:

```python
    try:
        conn = lit_schema.connect(cfg.literature_path)
    except (lit_schema.CorpusNotFound,
            lit_schema.CorpusSchemaVersionMismatch) as exc:
        print(str(exc), file=sys.stderr)
        return 1
```

In the ask path (around line 773), replace the inline `try: literature_conn = lit_schema.connect(...) except CorpusNotFound: pass` with a call to a new helper:

```python
        literature_conn = _open_literature_corpus(cfg)
```

and define, near `cmd_literature_status`:

```python
def _open_literature_corpus(cfg: config.Config):
    """The corpus for `ask`, or None when there is none to offer.

    Absent and stale are both 'none to offer' from the tool's point of view —
    it already reports its own absence in terms the model can act on — but a
    stale corpus is the user's problem to fix, so it is said on stderr
    rather than swallowed.
    """
    from .literature import schema as lit_schema

    try:
        return lit_schema.connect(cfg.literature_path)
    except lit_schema.CorpusNotFound:
        return None
    except lit_schema.CorpusSchemaVersionMismatch as exc:
        print(f"warning: literature corpus not used — {exc}", file=sys.stderr)
        return None
```

- [ ] **Step 7: Run the targeted tests, then everything**

Run: `pytest tests/test_literature_schema.py tests/test_cli.py tests/test_literature_corpus.py tests/test_no_network.py -q`
Expected: all pass. `offline_check.run_pipeline` builds a fresh corpus under `create=True` on a new path, so it is unaffected; confirm with `python -m health_agent offline-check` if the CLI entry point is available (`health-agent offline-check` after `pip install -e .`), expected `PASS`.

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add health_agent/literature/schema.py health_agent/literature/corpus.py health_agent/cli.py tests/test_literature_schema.py tests/test_cli.py
git commit -m "Corpus schema v2: drop the write-only abstract; check the version on open; add --rebuild

article.abstract was written by build() and read by nothing: store._SELECT
takes article_chunk.text, and on a real corpus the duplicate was about a
quarter of literature.db. Gone, behind a schema bump.

The bump exposed two latent gaps. schema.check_version was defined and
called by no one, so the ask path, status, and build all opened a corpus
blind; the check now lives in connect(), where no caller can forget it.
And the mismatch message has always told the reader to run
`literature build --rebuild`, a flag that did not exist; harmless while
every corpus was v1, and a dead end the moment one was not. --rebuild now
exists and deletes literature.db and the literature_chunks table, and
nothing else, which a test proves from the personal index's side.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Documentation — README, ROADMAP, and the original design doc

Implements spec §5.

**Files:**
- Modify: `README.md` (the "Medical literature corpus" section, ~lines 322–352, and the "known hole" paragraph ~586–605)
- Modify: `ROADMAP.md` (§1 "Slice 1 — shipped" paragraph; §2a topic-packs bullet)
- Modify: `docs/superpowers/specs/2026-08-18-literature-grounding-design.md` ("Open questions" at the end)

**Interfaces:** none; prose only. Run the `humanizer` skill (`.claude/skills/humanizer/SKILL.md`, which points at `brainiac/skills/humanizer/SKILL.md`) over every paragraph you write before committing. No em dashes; the repo's existing prose uses them, but the workspace convention for new prose is not to.

- [ ] **Step 1: README — the tier bullet**

Replace the bullet beginning `- **Evidence tiers come from publication metadata, not judgement.**` with:

```markdown
- **Evidence tiers come from publication metadata, not judgement.** A tier is
  read off MEDLINE's own `PublicationType` field, or it is `unknown`. The
  ladder, strongest first: meta-analysis and systematic review; guideline;
  randomized controlled trial; other clinical trials (including phase trials
  and non-randomized controlled trials); narrative and scoping reviews;
  observational; case report. Nothing inspects a title, an abstract, or asks
  a model to guess; an inferred tier would be a fabricated credential, and
  the feature's whole value is that its citations can be trusted.
- **Most of a real corpus has no tier, and that is not a bug.** Measured
  against 2,000 real PubMed abstracts, 65.5% resolved to `unknown`, and five
  in six of those carried `Journal Article` and nothing else: PubMed simply
  does not record a study design for most primary research. A `min_tier`
  floor therefore excludes most of the corpus, not just weak studies. The
  tool says so in its payload whenever a floor is set, and a filtered miss
  is reported as a filter result rather than as "the corpus holds nothing".
  Trial protocols are kept, flagged, and unranked: a plan reports no results.
```

- [ ] **Step 2: README — the `--rebuild` mention**

In the same section, after the `health-agent literature status` line in the fenced command block, add:

```
health-agent literature build --from medline_export.xml --slug cardiometabolic --rebuild
```

and in the bullet beginning `- **\`health-agent literature build\`** is what creates one` append the sentence:

```markdown
  A corpus built by an older release is refused on open, with a message
  naming `--rebuild`, which deletes the corpus (and only the corpus) before
  building.
```

- [ ] **Step 3: ROADMAP — slice 1 summary and the size figure**

In ROADMAP §1, after the "Slice 1 — shipped" paragraph's closing sentence (`...under what terms.`), add a paragraph:

```markdown
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
```

In §2a, in the "Pre-built topic packs" bullet, after `reproducible across users.` append:

```markdown
  The size measurement above makes this a requirement rather than a
  preference: an unbounded fetch is 6.3 GB.
```

- [ ] **Step 4: The original design doc — point the open questions at their answers**

At the end of `docs/superpowers/specs/2026-08-18-literature-grounding-design.md`, after the two numbered open questions, add:

```markdown
**Both answered on 2026-09-15**, in
[2026-09-15-literature-follow-ups-design.md](2026-09-15-literature-follow-ups-design.md) §1.
The size estimate was low by 4–5× on text; the `Review` / `Systematic Review`
split held and has a measured magnitude. The text above is left as written.
```

- [ ] **Step 5: Verify links and commit**

Run: `grep -n "2026-09-15-literature-follow-ups" README.md ROADMAP.md docs/superpowers/specs/2026-08-18-literature-grounding-design.md` and confirm every relative path resolves (`ls` each).
Run: `pytest -q` (docs only, but the suite is the commit gate).

```bash
git add README.md ROADMAP.md docs/superpowers/specs/2026-08-18-literature-grounding-design.md
git commit -m "Docs: revised tier ladder, the untagged majority, --rebuild, and the measured corpus size

The README's tier bullet now lists the ladder as it is after the four new
mappings and says plainly that most of a real corpus has no tier and why.
ROADMAP #1 records the size and tier-distribution measurements that until
now lived only in a session handoff, and #2a cites the 6.3 GB unbounded
figure as the reason topic packs are a requirement. The original design
doc's two open questions point at their answers.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Whole-branch review and PR

Not a code task; the gate the spec's last paragraph requires.

- [ ] **Step 1: Whole-branch review**

Run a review of `main..literature-follow-ups` as one diff (the `/code-review` skill at `high`, or the `superpowers:requesting-code-review` flow). Slice 1's per-task reviews all passed and the whole-branch review still found a Critical defect; do not skip this. Fix anything found with a follow-up commit per finding, then re-run `pytest -q`.

- [ ] **Step 2: Push and open the PR**

Write the PR body, run the `humanizer` skill over it, and show it to Hassan before opening. This is not an `onnela-lab` repo, so no "confirmed, post it" phrase is required; ordinary approval is enough. End the body with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

```bash
git push -u origin literature-follow-ups
gh pr create --title "Literature follow-ups: honest min_tier, four more tiers, schema v2" --body-file <path>
```

- [ ] **Step 3: Watch CI**

Use the `ccd_pr` tools to bind and monitor the PR rather than polling `gh`. All six checks (`test` × 4, `accuracy`, `no-health-data`) must be green before reporting done.
