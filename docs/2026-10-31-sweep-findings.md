# Four audit sweeps, 2026-10-31

Four read-only audits were run against `f87c902`, each pointed at one defect
shape rather than at the repo as a whole. Issue 22 is what prompted them. An
outside contributor found that `sleep_nights` summed every recording device
together, which the module's own docstring forbids, and the question was
whether the same shapes sat elsewhere unfound.

They do. Fourteen findings below were confirmed by reading the code, most of
them also by running it. The full sweep reports are in `docs/sweeps/`, with
the payloads and outputs each one printed.

Every finding here was verified against the source before being written down.
Where a sweep's claim could not be checked without a run that has not
happened, it says so.

## How to use this file

Work the sections in order. Group A is a mechanism the rest of the list
leans on, so it goes first. Within each group the order is by how much
damage the wrong output does to a person reading it.

One commit per finding, with a failing test first. A finding is done when a
test fails before the fix and passes after, and when deleting the fix fails
that test.

---

## Group A: the backstop has no negative form

### A1. The guardrail cannot see a false negative

`health_agent/agent/guardrail.py:62-91`.

All eight patterns are positive. They match "you have hypertension", "you are
diabetic", "your results indicate you have anemia". None of them match:

- "you have not been diagnosed with hypertension"
- "you have no history of diabetes"
- "you are not allergic to penicillin"
- "there is no record of X" rendered as "you do not have X"

So every finding in Group B passes the guardrail untouched. The project has
already learned once that a sentence in a payload is not a mechanism, which
is what prompt rule 1e was patching. The guardrail is the mechanism, and on
this class it is blind.

This is listed first because the Group B fixes are payload wording and shape,
and wording is what the guardrail exists to catch when it fails.

A fix has to decide one thing that is not obvious: a negative claim sourced
from a record is legitimate. "Your allergy list does not include penicillin"
is true and useful if the allergy list was read. "You are not allergic to
penicillin" is not supportable from any index. The distinction is whether the
sentence names what was searched, which is the same rule the cited-record
exemption already uses for diagnoses.

---

## Group B: the app can report an absence that is not there

Ranked by harm. Each one is a sentence a person could reasonably write after
reading the payload, with nothing else in front of them.

### B1. Asking about allergies searches for medication words

`health_agent/agent/tools.py:1336-1362`.

`_NOTE_VOCABULARY` has two keys, `medication` and `condition`. A question
about allergies, immunizations or procedures finds no key and falls back to
joining both, so it searches for "prescribed", "dose", "diagnosis" and never
"allergy", "allergic", "reaction" or "rash".

The search also runs with no kind filter, so lab PDF pages take slots that
the person's own notes should have had.

Observed on the fixture: a note reading "Penicillin (rash as a child)" was
absent from an allergies answer whose five slots held two lab PDF pages. The
payload's own note says the person's notes were already retrieved, so empty
means they hold nothing.

Invites: **"Your own notes do not mention any drug allergy."**

Structural fix: per-kind vocabulary, a kind filter so clinical PDFs cannot
fill the slots, one chunk per document.

### B2. A lab result that is not a number cannot be the latest one

`health_agent/visit_prep.py:100-104`.

`lab_signals_for` filters to points with `value_num is not None` before
sorting by date. A result of ">500", or "see comment", is dropped. The newest
remaining numeric point then becomes "latest".

Observed: a triglycerides history whose newest result is ">500" flagged high
produces a `lab_returned_to_range` signal reading "It was 180 flagged H, and
120 inside the printed range". The ">500" appears nowhere on the sheet.

Invites: **"Triglycerides came back into range."**

No language model is involved. This is the clinician sheet, and prompt rules
do not reach it. The CLI `labs` command has the same numeric-only filter on
its "first to latest" line.

Structural fix: keep text-valued points in the ordering, and either give them
their own signal kind or suppress `lab_returned_to_range` and
`lab_near_limit` entirely when a newer non-numeric point exists.

### B3. The inventory is capped and the prompt says it is complete

`health_agent/agent/tools.py:1905-1969` and
`health_agent/agent/orchestrator.py:280`.

`data_inventory` caps its lists at 30 analytes, 25 metrics, 25 tags, 25
vitals, with no marker when it truncates. The analytes are ordered by sample
count descending, so an analyte tested once is the first to fall off.

The prompt then tells the model: "Anything not listed above is not
available."

Observed with the cap lowered to 10 against a 23-analyte index: no marker of
any kind in the payload.

Invites: **"Your reports do not include a PSA"**, said before any tool call
is made, about an analyte that is in the index.

Each half is defensible. Together they are a false statement. Structural fix:
an `and_more` count per list, or drop the prompt sentence for any list that
can be capped.

### B4. The batch lab path reports absence with no coverage

`health_agent/agent/tools.py:1029-1095`.

`no_results_for` is a bare list of names. It does not say whether the analyte
was never tested, or tested outside the window asked about. Unknown names are
slugged silently, so a typo looks like an absent result.

On an empty index the payload still carries `out_of_range_on_latest_report:
[]` and a note saying every result above carries a citation, with no
`no_data` flag.

Invites: **"LDL is the only value out of range, PSA and troponin were
normal"**, and on an empty index, **"Nothing is out of range on your latest
report"** when no report exists.

Structural fix: per-analyte coverage objects, as the single-analyte path
already builds. A denominator on `out_of_range_on_latest_report`. A top-level
`no_data` when the index holds nothing.

### B5. Four different situations all report as null

`health_agent/store/queries.py:1050-1062`, emitted at `tools.py:977-985` and
`:1050-1060`, and starred in the CLI labs table.

`out_of_range` is a tri-state, and `null` currently means all of: no printed
reference range on the report, a value that is not numeric, a range that
could not be parsed, and genuinely unknown.

Observed on the fixture: an LDL of 118 from a FHIR record with no range
attached comes back `out_of_range: null, flag: null, reference_range: null`.
The CLI prints a dash and no star, under a legend saying a star marks
anything outside the printed range.

Invites: **"LDL was 118 and not flagged"**, and for a value of "see
comment", **"HbA1c was within range."**

Structural fix: an enumerated status, something like `in_range`,
`out_of_range`, `no_printed_range`, `not_numeric`, so absence of a range
cannot read as presence of a normal result.

### B6. An unrecognised metric name reports as no data

`health_agent/agent/tools.py:106-140`.

A name the metric registry does not know, "blood pressure", "bp",
"blood-pressure-systolic", returns "There is no blood pressure data in this
index at all. Do not substitute a related metric, say it is missing and ask
whether the user tracks it."

The index holds systolic and diastolic under their registered names. The CLI
path already prints suggestions from `metrics.suggest` and lists similar held
types. The tool does not.

Invites: **"You have no blood pressure data in your export."**

Structural fix: an `unknown_metric` error with suggestions, distinct from a
zero-coverage answer about a metric that does exist.

### B7. A keyword miss reads as a subject the notes do not cover

`health_agent/agent/tools.py:1130-1144`.

When the semantic search falls back to keywords and the keywords miss, the
payload has no `method` field, so the model cannot tell a keyword miss from a
semantic one, and the note tells it to say the notes do not cover the
subject.

Observed: "hypertension" misses a note reading "Blood pressure on the high
side of normal" and another naming lisinopril for blood pressure. Searching
"blood pressure" finds both.

Invites: **"Your notes do not mention any blood pressure concern."**

Structural fix: carry `method` into the miss payload, say "no chunk contains
these words" on a keyword miss, and include the tag-match hint the CLI
already prints.

### B8. Not held and held-but-out-of-window share a sentence

`health_agent/agent/tools.py:1558-1568`, `get_clinical_vitals`.

Lower tier than the rest, because the payload does carry a coverage tail
naming what the index holds. But a vital that was never in the feed and a
vital recorded outside the range asked about produce the same sentence.

Invites: **"No clinician has recorded your blood pressure."**

### B9. The CLI clinical command has no notes companion

`health_agent/cli.py:1858-1904`.

`clinical --kind allergies` prints the clinical feed only, with a source line
naming the folder. The penicillin note is never shown. The source line scopes
it, so this is the mildest of the group, but the surface has no equivalent of
the 1e mechanism and no companion search.

---

## Group C: the number itself is wrong

### C1. A category metric totals to zero

`health_agent/store/queries.py:940` and `:1610-1623`.

The per-source accumulator starts at `"sum": 0.0` and only adds when
`sum_value is not None`. A category metric's rows carry no numeric value, so
`sum` stays 0.0 and `avg` returns `0.0 / n`. Neither returns None.

`min` and `max` return None on exactly the same rows, so two aggregates are
honest about having nothing and two invent a zero.

Observed: `health-agent metric sleep --agg sum` prints 0.00 for every stage.
Through the tool, a stand-hours sum returns `overall.value 0.0` alongside
"Use this value directly."

Three separate docstrings forbid this. `store/sqlite_schema.py:22-26` says a
non-numeric value must not quietly become 0.0,
`ingest/healthkit.py:421-423` says the same, and the fixtures README says
"never coerced to 0". Sleep is routed around this in the tool. Every other
category type is not.

### C2. Two devices on an averaged metric are blended silently

`health_agent/store/queries.py:971`.

`if len(by_source) > 1 and agg in ADDITIVE`, and `ADDITIVE` is
`{"sum", "count", "duration"}`. `avg` is the default aggregate for every
discrete metric, so the multi-source counter never increments for them and
neither the tool payload nor the CLI prints a warning.

Observed: a watch reading 62 and a ring reading 70 on the same day come back
as 66.0 with `multi_source_periods = 0`.

This is issue 22's sibling. The figure is not doubled, because the combine
rule for an average is weighted rather than summed, but a number blended
from two devices is presented as one measurement.

`docs/oura-open-questions.md` §2 states that the warning "already fires when
a period holds more than one source". That is false, and the memo needs
correcting in the same change.

### C3. A same-day source disagreement is rendered as a trend

`health_agent/visit_prep.py:93-97`.

`lab_signals_for` sorts on `collected_date` alone. Two rows for one draw, a
PDF reading and a FHIR reading that disagree, become "latest" and "previous".

Observed: a PDF LDL of 130 and a FHIR LDL of 160 on 2026-03-10 render as "It
was 160 on 2026-03-10, up from 130 on 2026-03-10". Reverse the insert order
and the sheet says the opposite.

`lab_trend` deliberately keeps both rows and exposes `trend.disagreements`.
`lab_signals_for` never reads it. The agent tool for the same trend does
carry a `source_disagreement` note, so the model-facing path is honest and
the clinician-facing one is not.

Same path for `lab_returned_to_range` and `lab_near_limit`.

### C4. A new device reads as a change in behaviour

`health_agent/visit_prep.py:199-258`.

`_daily_values` returns `{date: value}` and drops everything else the query
layer knows: the source, the per-source breakdown, the multi-source period
count. `metric_signals` then compares two windows and reports a shift.

The figures are not doubled, because `metric_series` already takes the
largest single source per period. But a phone-to-watch switch moves the
level, and the sheet reports that as a change in the person worth asking a
clinician about, naming no device anywhere.

`test_a_second_sleep_device_is_not_a_change_in_sleep` covers sleep. Steps,
resting heart rate, weight and HRV are open. Sleep's own `source` is dropped
by `_daily_values` too, so sleep is protected by the test rather than by the
code.

### C5. A per-period figure does not say which device it is

`health_agent/agent/tools.py:166-170`.

`_query_healthkit` emits the per-period max across devices without the
source. `SeriesPoint.sources` and the per-source figures exist at the store
layer and are not carried through.

Observed: `{"period": "2026-03-02", "value": 6000.0, "samples": 5}`, where
6000 is one device's figure and 5 counts both devices' records.
`overall.total_samples` has the same mix. The series-level
`multi_source_warning` names both devices but not which one won a given
period.

This is the gap sleep closed with its `source` and `per_source` keys. It is
open for every quantity metric.

### C6. A comparison's two sides can come from different devices

`health_agent/store/windows.py:437-490` and `tools.py:672-720`.

`SideSummary` is built from `point.value` only. The multi-source period
count, the sources seen and the per-source figures are never read, and
`counted_n` sums every device's records.

Observed: a `compare_windows` payload and its rendered text carry no device
name and no warning, and the CLI prints "6,000 count | 5 records". A
comparison whose before and after come from different devices is reported
`comparable: ok`.

### C7. A workout filter names one activity and counts several

`health_agent/store/windows.py:412-421` and
`health_agent/store/queries.py:777`.

`_held` names `mine[0]`'s activity while the count sums every activity the
substring matched. Observed: `compare --workouts --activity ing` over a run
and a ride prints "the index holds 2 workouts of Cycling".

`workout_series` has the matching shape. Its label is the caller's search
pattern rather than what was found, so a tool payload reads
`{"series": "ing", "points": [{"workouts": 2, ...}]}` over one run and one
ride. This is the same shape as the "Cycling nope" label bug fixed in the
duration work.

---

## Group D: checks that pass without measuring

Not user-visible. They are here because the suite is the only accuracy signal
this project has, and four of its checks are not signals.

### D1. A test asserting a bare string

`tests/test_eval.py:1666`.

`assert "correct number actually scored: 1/2 draws (Q22 has no value to "`,
with no `in report`. A non-empty string literal is always true, so the line
measures nothing. The wording is also stale against what the report now
prints.

The next line does check `in report` for a shorter prefix, so the test is not
worthless, but this line should either assert or go.

### D2. A test that accepts any exception

`tests/test_no_network.py:102`.

`pytest.raises(Exception)` followed by `assert exc.value is not None`, which
is true for every exception. The test claims the network guard sees real call
paths. A `raise NotImplementedError` at the top of `list_models` would
satisfy it without the guard being reached.

Should name the guard's own exception type, or check `__cause__`.

### D3. A test that passes on an empty result

`tests/test_agent.py:369`.

`assert all(r["kind"] == "note" for r in result["results"])` with no check
that anything came back. `all([])` is true, so any change that makes the
search return nothing, an upper-cased kind, a pluralised comparison, leaves
the test green.

### D4. A test that imports nothing

`tests/test_no_network.py:183`.

The body walks the package and calls `importlib.import_module` on each
module. By the time it runs in the full suite every one of those modules is
already imported, and importing a cached module executes no code. The sweep
measured zero uncached modules in a suite run, nine when the file runs alone.

The claim is that no module phones home at import. A module-scope network
call would not be caught. The same file already imports in a subprocess
elsewhere, which is the shape this needs.

---

## Unverified

Two claims from the sweeps are not in the list above, because confirming them
needs a run that has not happened.

1. `get_workouts` cuts its session list to 25 without the truncation marker
   that `agent/tools.py:9-16` says every tool carries. The period branch does
   flag. Same claim for the batch lab path's per-analyte point trim and for
   `facts_of_other_kinds`.
2. `cli.cmd_metric`, `tools._overall` and `windows._summarize` compute a
   range average three different ways: unweighted over the periods shown, so
   `--limit` changes it; sample-weighted over all points; unweighted over
   periods. They agree on the fixture because every day there holds one
   sample. On a real export with several samples a day they would not, and
   `CONTRIBUTING.md` promises the model and the CLI cannot report different
   numbers for the same question.

Both are worth a run before anyone builds on them.

## Not in scope

The sweeps also noted, and this file records without acting on:

- `store/sqlite_schema.py`'s docstring still says a different schema version
  raises, which v5 changed to migrating in place. Stale wording, explained
  correctly in `migrate`.
- `ollama_client`'s "only module that opens a socket" line is stale.
  `CONTRIBUTING.md` names three.
- `compare_windows`'s `anchor_not_found` is the one absence payload naming a
  diagnosis without a notes companion.
- `data_inventory` never reads the clinical ingest marker, so the model cannot
  tell "no clinical folder" from "folder skipped".
