# Absence-as-coverage sweep, local_health_agent @ f87c902

Read-only audit of every tool in `health_agent/agent/tools.py`, the payload builders it calls
(`store/queries.py`, `store/windows.py`), the two non-model surfaces (`cli.py`, `visit_prep.py`),
the lab-context block (`agent/context.py`) and the prompt/tool-result handling in
`agent/orchestrator.py`. Payloads below were printed from real runs against three scratch indexes
built from `tests/fixtures/` (A: export + records + notes + clinical-records + literature corpus;
C: same without clinical-records; B: schema only, a new install). Script: `/tmp/sweep/run.py`,
`/tmp/sweep/verify.py`; raw output `/tmp/sweep/out.txt`.

Defect shape hunted: a payload or printed line that reports "the index holds nothing about X" in a
form a reader will relay as "X did not happen to you". Workout-measure substitution (`_instead`) is
excluded per the brief.

Backstop note: `agent/guardrail.py` PATTERNS (lines 62-91) are all positive-form ("you have X",
"you are diabetic"). There is no pattern for "you have not been diagnosed", "you have no X",
"you are not allergic to". A negative claim passes the second layer unflagged, so every finding
below reaches the person unless the prompt rule happens to hold.

## Enumeration

8 tools (`query_healthkit` incl. the sleep branch, `get_lab_trend` single + batch, `get_workouts`
plain + detail + series, `compare_windows`, `search_records`, `search_medical_literature`,
`get_clinical_history`, `get_clinical_vitals`), plus `data_inventory` (system prompt),
`context.render` (lab block), `visit_prep.render`, and 10 CLI commands.

41 empty/partial/absent states enumerated and run:

| # | Surface | State | Verdict |
|---|---|---|---|
| 1 | query_healthkit | metric held, none in window | scoped (note + available_from/to) |
| 2 | query_healthkit | metric never held (registered name) | scoped |
| 3 | query_healthkit | **unregistered name** ("blood pressure", "bp") | FINDING 6 |
| 4 | query_healthkit | empty index | scoped |
| 5 | query_healthkit | rows held, value NULL on all (`overall: null`) | not reachable in fixture; no note exists for it, low |
| 6 | query_healthkit/sleep | none in window | scoped |
| 7 | query_healthkit/sleep | empty index | scoped, wording says "in that range" with no range (minor) |
| 8 | get_workouts | none in window | scoped |
| 9 | get_workouts | empty index | scoped, same "in that range" wording (minor) |
| 10 | get_workouts detail | filter matched nothing | scoped (`asked`, `activities_in_range`) |
| 11 | get_workouts detail | measure not held / held elsewhere | scoped (`_measure_absence`) |
| 12 | compare_windows | one side empty | scoped (`because` + coverage tail) |
| 13 | compare_windows | both sides empty | scoped |
| 14 | compare_windows | empty index | scoped ("holds no records of this figure at all") |
| 15 | compare_windows | anchor_fact not found, facts held | scoped ("in the clinical record" + `clinical_kinds_held`); see caveat A |
| 16 | compare_windows | anchor_fact not found, no facts | scoped |
| 17 | compare_windows | anchor ambiguous | scoped |
| 18 | compare_windows | measure not held | fixed (`_instead`), excluded |
| 19 | get_lab_trend single | analyte unknown to reports | scoped |
| 20 | get_lab_trend single | held, none in window | scoped |
| 21 | get_lab_trend single | empty index | scoped |
| 22 | get_lab_trend single | **point with no printed range / text value** (`out_of_range: null`) | FINDING 5 |
| 23 | get_lab_trend batch | **some analytes missing** (`no_results_for`) | FINDING 3 |
| 24 | get_lab_trend batch | **empty index** (`out_of_range_on_latest_report: []`) | FINDING 3 |
| 25 | search_records | no match, chunks held | FINDING 7 |
| 26 | search_records | empty index | scoped enough (`chunks_indexed: 0`) |
| 27 | search_medical_literature | no corpus | scoped |
| 28 | search_medical_literature | corpus, no match | scoped |
| 29 | search_medical_literature | filtered to nothing | scoped |
| 30 | get_clinical_history | no clinical records (C, B) | scoped + notes attached |
| 31 | get_clinical_history | clinical but no facts | scoped |
| 32 | get_clinical_history | facts, none of kind / none in window | scoped (`facts_of_other_kinds`) |
| 33 | get_clinical_history | **`also_in_personal_notes` for allergies/immunizations/procedures** | FINDING 1 |
| 34 | get_clinical_history | **`also_in_personal_notes` slots taken by lab PDFs** | FINDING 1 |
| 35 | get_clinical_vitals | no clinical records | scoped |
| 36 | get_clinical_vitals | clinical, no vitals | scoped |
| 37 | get_clinical_vitals | **vital not held** | FINDING 8 |
| 38 | get_clinical_vitals | held, none in window | scoped |
| 39 | data_inventory | **lists capped at 25/30 under "Anything not listed above is not available"** | FINDING 4 |
| 40 | data_inventory | clinical block absent keys when not ingested | see caveat B |
| 41 | visit_prep | **text-valued latest lab result dropped** | FINDING 2 |
| 42 | visit_prep | no labs / no metrics / nothing stood out / gaps | scoped |
| 43 | context.render | no citable finding | scoped ("in the installed packs") |
| 44 | CLI clinical --kind | **feed only, no notes companion** | FINDING 9 |
| 45 | CLI labs | text row, no star | FINDING 5 (CLI surface) |
| 46 | CLI metric/sleep/workouts/vitals/search/documents/notes | empty | scoped |

## Findings, ranked

### 1. `get_clinical_history` promises the notes were retrieved, and for three kinds they were not
**Where:** `tools.py:1326-1340` (`_NOTE_VOCABULARY` has entries for `medication` and `condition`
only), `tools.py:1343-1363` (`_notes_beside_clinical`), `tools.py:1366-1388`
(`_attach_personal_notes`).

**State:** `kind=allergies`, `immunizations` or `procedures`. The vocabulary falls through to the
union of the medication and condition terms, and `keyword_search` is called with `kind=None`, so
lab PDFs compete for the five slots.

**Real run (index A, `kind=allergies`):** the fixture note has a `# Allergies` section reading
"Penicillin (rash as a child). No food allergies." `also_in_personal_notes` came back with five
entries: the Conditions chunk, the Current-medications chunk, `no-frontmatter.txt`, and two pages
of lab PDFs. `penicillin present: False`. Same five for immunizations and procedures. For
`kind=medications`, 3 of the 5 slots were lab PDF pages and an undated text note; the medication
note held one slot.

**The payload says, in `two_sources_note`:** "`also_in_personal_notes` is what they wrote down or
photographed themselves, already retrieved for you ... an empty `also_in_personal_notes` means
their notes hold nothing on this."

**Sentence invited:** "Your health system records a pollen allergy. Your own notes do not mention
any drug allergy." The notes do. Rule 1e tells the model it does not have to go looking, so it
will not. On a real corpus with hundreds of lab pages containing "mg", the medication note itself
can be pushed out of the five slots and the same sentence appears for medications, which is the
exact Q27 failure this mechanism was built to close.

**Why structural:** the note is already right; the retrieval under it is wrong. Fix is in the
query: a vocabulary entry per kind (allergy: "allergy allergies allergic reaction rash
intolerance"; immunization: "vaccine vaccinated shot immunization booster"; procedure: "surgery
procedure operation"), `kind` restricted to note/image (`keyword_search` already takes `kind`),
and either a higher limit or one chunk per document. Also worth a test: for each kind, the
fixture note's matching section must appear.

### 2. Visit-prep sheet turns a flagged text-valued latest result into "returned to range"
**Where:** `visit_prep.py:100-104` (`lab_signals_for`: `points = [p for p in trend.points if
p.value_num is not None and p.collected_date]`), then lines 108-126.

**State:** the newest result for an analyte is non-numeric (">500", "<5", "POSITIVE", "see
comment") and flagged; earlier results are numeric. Labs print these routinely for values past the
assay ceiling.

**Real run (`/tmp/sweep/verify.py`, synthetic trend 180 H -> 120 in range -> ">500" H):**
```
kind: lab_returned_to_range
1. **Ask whether your triglycerides should keep being checked.** It was 180 mg/dL on 2024-01-01,
   flagged H, and 120 mg/dL on 2025-01-01, inside the printed range of 0-149 mg/dL (...).
```
The ">500 H" result from 2026 is on no line of the sheet, and the sheet's newest-dated sentence
says the value is inside range.

**Sentence invited (clinician reading the sheet):** "Triglycerides came back into range; nothing
current to act on." The latest report says the opposite. No model is involved and the prompt rule
cannot reach this surface. The same filter makes `lab_out_of_range` cite a stale "latest" date
when the real latest is text.

**Why structural:** the drop is a data decision, not wording. Keep text-valued points in the
chronological order; when the newest point is non-numeric, either emit a distinct signal
(`lab_latest_not_numeric`, "It was reported as >500 on ..., flagged H") or at minimum refuse to
emit `lab_returned_to_range` / `lab_near_limit` when any newer non-numeric point exists. The CLI
`labs` summary line `first X -> latest Y` (`cli.py` ~2002-2010) uses the same numeric-only filter
and would print "first 180 -> latest 120 (down)" for the same trend.

### 3. `get_lab_trend` batch mode reports missing analytes as a bare list and an empty out-of-range list as a clean bill
**Where:** `tools.py:1029-1095` (`_get_lab_trends_batch`), the `no_results_for` and
`out_of_range_on_latest_report` fields.

**State:** some requested analytes have no results (never on any report, or only outside the
date window, or an unregistered name slugged by `labs.resolve_key`); or the index is empty.

**Real runs:**
```
[A] analytes=["ldl","psa","tsh","troponin"] ->
  "out_of_range_on_latest_report": [ {ldl ...} ],
  "no_results_for": ["psa", "troponin"]
[B] analytes=["ldl","hba1c"] ->
  {"analytes": {}, "citation_note": "Every result above carries a `citation`...",
   "out_of_range_on_latest_report": [], "no_results_for": ["ldl","hba1c"]}
```
Compare the single-analyte path, which carries `no_data`, `total_results_for_analyte`,
`available_from/to`, `analytes_present_in_reports` and a note. Batch mode carries none of that,
does not say whether `psa` was never on a report or only outside `start_date`, and has no
top-level `no_data` when `analytes` is `{}`. `out_of_range_on_latest_report` is precomputed with
no denominator, so a model asked "is anything out of range?" reads `[ldl]` as the complete
answer over everything it asked for.

**Sentence invited:** "LDL is the only value out of range; your PSA, TSH and troponin were
normal." (PSA and troponin have no result.) On an empty index: "Nothing is out of range on your
latest report." (there is no report). Top tier: these are lab values the batch path is the
default route for, per the tool description ("pass several names in `analytes` in ONE call").

**Why structural:** `no_results_for` should be the same coverage object the single path builds
(`{key, label, total_results, available_from, available_to}`), `out_of_range_on_latest_report`
should carry `analytes_with_results: N` and `analytes_without_results: M` beside it, and
`analytes == {}` should set `no_data: true` and drop `citation_note`, which currently asserts
results exist.

### 4. The system-prompt inventory is capped and silently truncated under "Anything not listed above is not available"
**Where:** `tools.py:1905-1969` (`data_inventory`: `max_metrics=25`, `max_analytes=30`,
`list_tags(limit=25)`, `vital_metrics[:25]`); `orchestrator.py:231-244` (`system_prompt` appends
"Anything not listed above is not available. Use the tools to read actual values; the inventory
only tells you what exists.").

**State:** an index with more than 30 analytes (one CBC + CMP + lipid panel is ~33), more than
25 HealthKit types, 25 tags or 25 clinician vitals. `list_analytes` orders by `n DESC`, so the
analytes tested once, which are the ones a person asks "have I ever had" about, fall off first.

**Real run (`data_inventory(conn, max_analytes=10)` on the 23-analyte index A):**
`"analytes_available": ["cholesterol_total", ... "glucose"]`, 10 keys, no `truncated`, no
`and_n_more`. The labs block has exactly three keys: `reports`, `date_range`,
`analytes_available`.

**Sentence invited, before any tool is called:** "Your reports do not include a PSA / ferritin /
vitamin D." The prompt told the model so in as many words. Rule 4a ("always call the tools") is
the only thing standing between the inventory and the answer, and it is a prompt rule.

**Why structural:** either the caps carry a marker (`"analytes_available": [...], "and_more": 7`)
or the "not available" sentence must not apply to capped lists. The cheapest fix is a count
field per list plus rewording the sentence to "a metric or analyte not listed may still exist;
the tools are authoritative".

### 5. A lab point with no printed range, or a text value, is indistinguishable from an in-range one
**Where:** `queries.py:1050-1062` (`LabPoint.out_of_range` returns `None` both for "no range
printed" and "value not numeric"); `tools.py:977-985` and `1050-1060` emit `out_of_range: null,
flag: null, reference_range: null`; `cli.py` labs table marks `*` only when `out_of_range is
True`.

**Real runs (index A):**
```
hba1c  2023-05-02  value "see comment"  unit null  reference_range null  flag null  out_of_range null
ldl    2024-11-06  value 118            unit mg/dL reference_range null  flag null  out_of_range null
```
CLI `labs ldl` prints the 118 row with `-` for reference and flag and no `*`, under a legend
reading "* outside the reference range printed on that report".

**Sentence invited:** "Your LDL was 118 mg/dL on 2024-11-06 and was not flagged" / "HbA1c on
2023-05-02 was within range". The index holds no range for either, and the 118 would be flagged H
against the 0-99 range the next two reports print. No payload field or note says what `null`
means; the `panel_note` and `citation_note` ride with the data but nothing about the tri-state.

**Why structural:** a three-valued `null` is the mechanism failing. Replace with an enumerated
`range_status` (`in_range`, `out_of_range`, `no_printed_range`, `not_numeric`) on every point and
on `out_of_range_on_latest_report` rows, and have the CLI print a distinct marker for
`no_printed_range`. The visit-prep templates already distinguish "with no printed range", so the
vocabulary exists.

### 6. `query_healthkit` reports a name it cannot resolve as "no data in this index at all"
**Where:** `tools.py:106-140` (`_query_healthkit`), relying on `metrics.resolve`, which returns an
unregistered `Metric` whose identifier is the raw string.

**Real run (index A, which holds systolic and diastolic records):**
```
{"metric": "blood-pressure-systolic", "label": "blood-pressure-systolic", "points": [],
 "no_data_in_range": true, "total_records_for_metric": 0,
 "note": "There is no blood-pressure-systolic data in this index at all. Do not substitute a
          related metric; say it is missing and ask whether the user tracks it."}
```
`metrics.resolve("blood pressure")` and `("bp")` behave the same. `metrics.suggest("blood
pressure")` returns the two held identifiers, and the CLI `metric "blood pressure"` prints
"Related metrics I know about: systolic, diastolic / Similar types that ARE in your index: ...".
The tool does neither.

**Sentence invited:** "There is no blood pressure data in your export. Do you track it?" The
note instructs exactly that sentence. Mid tier (a vital, not a diagnosis), but the instruction
actively pushes the wrong claim.

**Why structural:** when the resolved identifier is unregistered and `type_coverage` is zero,
return `{"error": "unknown_metric", "suggestions": [...], "similar_types_held": [...]}` the way
`_report_missing` already does for the CLI, so the model corrects the name instead of telling the
person they do not track the thing.

### 7. `search_records` no-match says "the documents and notes do not cover this" on a keyword miss
**Where:** `tools.py:1130-1144`.

**Real run (index A, query "hypertension"):** no match (semantic search unavailable, keyword
fallback). Query "blood pressure" on the same index returns the note "Blood pressure on the high
side of normal, monitored at home" and "Lisinopril 10 mg each morning, for blood pressure". The
no-match payload carries no `method` field (the hit payload does), and its note tells the model
to say the notes "do not cover this".

**Sentence invited:** "Your notes do not mention any blood pressure concern or diagnosis." The
scope phrase is present but names the wrong scope: it vouches for the corpus when only the
lexical match failed.

**Why structural:** add `method` to the miss; when `method == "keyword"`, the note should say "no
indexed chunk contains these words" and the payload should carry the CLI's `_tags_matching`
result (`cli.py:2195`) so a tag hit is visible. Lower than 3-6 because the claim is about notes,
not a lab or a feed, but it touches a condition name.

### 8. `get_clinical_vitals` for a vital the feed never carried says "in the requested range" when no range was requested
**Where:** `tools.py:1558-1568`.

**Real run (index A, `metric="blood pressure"`, no dates):**
`"note": "No clinician-measured blood pressure in the requested range. The index does hold
knee_effusion_size, pulse, spo2, from 2023-09-15 to 2023-09-20. Say what is absent."`

**Sentence invited:** "No clinician has recorded your blood pressure." The feed holds one
encounter's pulse and SpO2; blood pressure was almost certainly taken and not sent. The coverage
tail is there, so this is lower tier; the gap is that "metric not held at all" and "held, none in
window" share one sentence, which `_measure_absence` keeps apart for workouts.

### 9. CLI `clinical --kind` prints the feed with no personal-notes companion
**Where:** `cli.py:1858-1904`.

**Real run (index A):**
```
kind     name    status  date        detail
allergy  Pollen  active  2018-06-05
1 record(s). Source: the clinical-records folder of your Apple Health export.
```
The person's own note records a penicillin allergy. The source line scopes the table, which is
why this is not higher, but the mechanism rule 1e bought for the model (`also_in_personal_notes`)
has no equivalent on the printed surface, and this is the surface someone would paste into a
form. Fix would be the same retrieval as finding 1, printed beneath the table as "Your notes
also mention: ...".

### Minor wording (not ranked)
- `tools.py:293-304` (`_query_sleep`) and `:390-400`, `:559-570` (`get_workouts`): on an empty
  index with no dates given, the note says "No sleep/workouts in that range" beside
  `total_*: 0`. The counts carry the truth; the phrase implies a range was applied.

## Caveats on two states that are scoped but worth knowing
- **A.** `compare_windows` `anchor_not_found` (`tools.py:790-805`) says "No dated row in the
  clinical record matches that name" and lists `clinical_kinds_held`. Scoped, so not reported.
  It is the one diagnosis-naming absence payload that does not carry `also_in_personal_notes`;
  "hypertension" as an anchor on index A returns this while the note records lisinopril "for
  blood pressure". A model will report the anchor as absent correctly; whether it then says
  "there is no hypertension in your history" rests on rule 1e alone.
- **B.** `data_inventory` on index C (clinical folder not ingested) emits a `clinical` block
  with no `conditions`/`medications` keys at all, under the "not available" sentence. The CLI
  `check` distinguishes "no folder", "skipped by --no-clinical" and "ingested" via the
  `clinical_ingest` meta marker (`cli.py:309-326`, `2838-2864`); the inventory does not read it.
  The model is told nothing about whether the feed was ever looked at.

## States checked and found properly scoped
query_healthkit (registered metric out of window / never held / empty index); sleep out of
window; get_workouts out of window and filter-matched-nothing (`asked`, `activities_in_range`);
workout measure absence (`measures_available`, `measures_not_in_this_answer`); compare_windows
one side empty, both empty, empty index, anchor not found (both branches), anchor ambiguous,
measure substituted; get_lab_trend single-analyte miss (both branches, with
`analytes_present_in_reports`); search_medical_literature no corpus / no match / filtered;
get_clinical_history no clinical / clinical-but-no-facts / none-of-kind-or-window
(`facts_of_other_kinds`); get_clinical_vitals no clinical / clinical-but-no-vitals / held but out
of window; context.render `NONE_FOUND`; visit_prep "No lab values in the index", "Nothing stood
out ... says nothing about your health", gaps "none in the export"; orchestrator's no-tool-called
stderr note and step-limit instruction; CLI metric, sleep, workouts, labs-miss, vitals-miss,
search-miss ("Nothing in your N indexed chunk(s) matched"), documents, notes.
