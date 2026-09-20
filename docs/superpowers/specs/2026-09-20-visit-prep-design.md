# Visit prep: questions worth asking, from the data alone

ROADMAP #3. Given the index, produce a short list of questions to raise
with a clinician, each one grounded in a specific value from the person's
own files. The roadmap's framing is the whole design constraint: the output
is *questions to ask*, never answers. "Ask whether your LDL trend warrants
follow-up" is in scope; "your LDL trend warrants follow-up" is not.

## 1. No model

The decision that shapes everything else: `visit-prep` does not call the
model. Three reasons.

- The thing being produced is a list of findings with a fixed sentence
  shape around each. A template does that exactly; a model does it
  approximately, slowly, and with the guardrail as the only thing between
  the person and "your LDL is high, consider a statin". Run 6 showed a
  smaller model reaching for recalled thresholds on the first question
  that invited them. A visit-prep sheet is nothing but invitations.
- It has to work on the machine that cannot run the model. `doctor` now
  tells that person which model to pull; `visit-prep` should not need one.
- Determinism is what makes it testable. The same index produces the same
  sheet, and the guardrail can be run over the rendered output as a test
  oracle: a template that ever trips `uncited_medical_claim` or the
  diagnosis patterns is a failing test, not a runtime surprise.

The roadmap notes the agent already does something adjacent when asked
"anything I should ask my doctor about?". That stays: `ask` can still be
asked, and its answer has the model's flexibility and the guard's limits.
`visit-prep` is the deliberate, proactive version, and it is deliberate
precisely because no model is in it.

## 2. Command

```
health-agent visit-prep [--window DAYS] [--json] [--out FILE] [--no-literature]
```

- Reads the index. Nothing is written unless `--out` is given.
- `--window` (default 30): the HealthKit comparison window. The last
  `DAYS` days are compared against the `DAYS` before them.
- `--json`: the signals as data, for anyone building on it.
- `--no-literature`: skip the corpus even when one is installed.
- Exit 0 whenever the index opens, including when there is nothing to ask
  about. An empty sheet says what was looked at and that nothing stood out;
  it does not invent a question to fill the page.

## 3. Signals

`health_agent/visit_prep.py`. Two halves with one interface between them:
`gather(conn, *, today, window_days) -> Sheet` finds things, `render(sheet)
-> str` writes them. A `Signal` is a kind, a subject, the evidence (values,
dates, ranges, citations), and any caveats. Nothing in `gather` composes
prose; nothing in `render` reads the database.

Four kinds, from the roadmap's own list (a value out of range, a value
moving toward a limit, a metric that has shifted).

**`lab_out_of_range`.** The latest result for an analyte is outside the
printed reference range: the lab flagged it (`flag` set) or the value
falls outside `ref_low`/`ref_high`. Evidence: the value, unit, range, date,
citation, and the previous result if there is one so the direction is
visible. One signal per analyte.

**`lab_returned_to_range`.** An earlier result was out of range and the
latest is not. The question is whether to keep watching it. Evidence: both
results.

**`lab_near_limit`.** The latest result is inside the range, within 10% of
its span of one limit (or within 5% of the limit's own value when only one
limit is printed), *and* moved toward that limit since the previous
result. Both conditions, so a stable value near a limit is not a signal
and a jump that stays comfortably inside is not either. Needs two numeric
points and a numeric range.

**`metric_shift`.** For five HealthKit metrics, the mean over the last
`window_days` against the mean over the `window_days` before, using
`metric_series` by day: resting heart rate (5 bpm), body mass (3%), steps
(25%), HRV SDNN (20%), sleep duration from `sleep_nights` (45 min). A
shift past the threshold in either direction is a signal. Both windows
need at least half their days present; fewer is reported under "not
enough data", not silently skipped. The thresholds are module constants
with a comment each; they are chosen to be well clear of day-to-day
noise, not to be clinically meaningful, and the sheet says so in its own
words (§4).

Every signal built on a value read by OCR carries the caveat the lab tool
already states: check it against the original report.

What is not a signal, on purpose: anything from notes. Medications and
conditions the person wrote down are free text, and reading them is the
model's job (`ask` does it today, Q27). A deterministic sheet cannot tell
"stopped metformin" from "started metformin", and guessing would put a
wrong medication question in front of a clinician.

Analytes with one result get no trend signals and are not listed as gaps;
one result is a fact, not a gap. Metrics absent from the export are listed
under "not enough data" by name so the person knows they were looked for.

## 4. Literature

When a corpus is installed and `--no-literature` is not given, each
`lab_*` signal gets at most one finding: the analyte's label as the
query, through the same path the tool uses (semantic when the embedder is
up, keyword otherwise; `_literature_hits` moves from `agent/tools.py` to
`literature/store.py` as `hits(...)` so both callers share it, and the
tool keeps its behaviour). The best-tiered result of the top three is
rendered as "Evidence you could bring up: *title* (year, tier, PMID n)".
Nothing from the abstract is quoted, and no finding is attached to a
`metric_shift`: a paper about resting heart rate says nothing about this
person's watch.

Without a corpus the sheet says "No literature corpus is installed;
`health-agent literature packs` lists what is available", once, in the
"looked at" section.

## 5. The sheet

Markdown, on stdout or to `--out`.

```
# Questions for your next visit

Prepared 2026-09-20 from your own files. Nothing here is a conclusion;
each item is a value that stood out and a question it might be worth
asking. Bring the reports named.

## Looked at
- 3 lab reports, 2025-03-04 to 2026-03-10, 23 analytes
- Apple Health, 2026-08-21 to 2026-09-20 against the 30 days before
- Literature: sample pack (2,000 articles)

## Questions
1. **Ask whether your LDL cholesterol needs follow-up.** It was 112 mg/dL
   on 2026-03-10, flagged H against the printed range of 0–99 mg/dL, down
   from 128 mg/dL on 2025-09-12 (labs_2026-03-10.pdf, p.1;
   labs_2025-09-12.pdf, p.1).
   Evidence you could bring up: *Title of paper* (2026, meta-analysis,
   PMID 42613609).
2. **Ask whether your HbA1c should keep being checked.** It was 5.9% on
   2025-03-04 (flagged H against 4.8–5.6%) and 5.4% on 2026-03-10, inside
   the range (scan_2025-03-04.pdf, p.1; labs_2026-03-10.pdf, p.1). The
   2025-03-04 value was read by OCR; check it against the original.
3. **Ask about your resting heart rate.** It averaged 64 bpm over the
   last 30 days against 58 bpm the 30 before, a shift larger than this
   tool's cutoff for pointing it out (5 bpm). Whether that matters is
   not something this tool can say.

## Not enough data to check
- HRV (SDNN): 4 days in the last 30
- Body mass: none in the export

_These are the values recorded in your own files, not medical advice.
What they mean for you is a conversation for you and your clinician._
```

Every question opens with "Ask". The sentence after it is the evidence and
contains only the person's values, the printed range, dates, and
citations, which is exactly the register `_PERSONAL_CONTEXT` and
`_REPORTING_CONTEXT` exempt, and it is checked (§7). No template
contains a threshold, a category word applied to the person ("high",
"elevated"), or a verb of interpretation. "Flagged H" quotes the lab.

## 6. Where the code goes

- `health_agent/visit_prep.py`: `Signal`, `Sheet`, `gather`, `render`,
  the thresholds, `literature_for(signal, ...)`.
- `health_agent/literature/store.py`: `hits(conn, vector_path, embedder_factory, query, ...)`,
  moved from `agent/tools.py::_literature_hits`, which becomes a one-line
  call.
- `health_agent/cli.py`: `cmd_visit_prep` and its parser entry. The
  `doctor` network posture text stays as it is: this command is offline.
- `README.md`: a section after "Medical literature corpus", and the
  roadmap pointer in "What's next" updated.
- `ROADMAP.md` #3: shipped, with what it does not do (notes) stated.

## 7. Testing

`tests/test_visit_prep.py`, over the committed fixtures (which already hold
an LDL flagged H, a vitamin D flagged L, and an HbA1c that returned to
range) plus a synthetic index for the metric cases:

- Each signal kind fires on the case built for it and not on its nearest
  non-case: a stable value near a limit; a jump that stays inside; a
  window with too few days; an analyte with one result.
- The rendered sheet for the fixtures, with and without a corpus, run
  through `guardrail.check(text, used_tools=True)` yields no flags. This
  is the test that makes §1's argument hold, and it is pinned so a future
  template edit that trips the guard fails here.
- Every question line starts with "Ask"; every evidence line cites a
  file or names the HealthKit window.
- `_literature_hits` moved: the tool's existing tests still pass unchanged.
- CLI: `visit-prep --json` round-trips; `--out` writes and prints
  nothing else; no index gives the usual message and exit 1.
- `tests/test_no_network.py`: `visit_prep` imports nothing from
  `literature/fetch/`.

Not in scope: an appointment date or any notion of "last visit"; a tool
exposing the sheet to the model; notes-derived questions; any threshold
that comes from anywhere but the printed reference range.
