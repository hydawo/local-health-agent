# Sweep: stated rules vs. the functions they cover

Repo: /Users/hydawo/Downloads/Claude Code/local_health_agent at f87c902. Read-only audit.
Scope: every module docstring and header comment under `health_agent/` (41 modules),
`docs/`, `CONTRIBUTING.md`, `tests/fixtures/README.md`.

Rules collected: 34 distinct stated rules. Functions checked against them: 78.
Confirmed violations: 5 (ranked worst-first below). Everything else is under
"checked and clean".

All reproductions below run against a copy of `tests/fixtures/export.xml` ingested
into a scratch data dir, with a few synthetic rows added where the fixture has no
case (noted per finding). Nothing was written to the repo.

---

## Finding 1. A category metric asked for as `sum` or `avg` returns 0.0 and tells the model to use it

**Rules (verbatim):**

- `health_agent/store/sqlite_schema.py:22-26` (module docstring): "**Value columns are
  split.** `value_num` holds the numeric value when one parses; `value_text` holds the
  raw string. Quantity records populate the first, category records (sleep stages,
  stand hours) populate the second. Keeping them in one column would force every
  aggregate query to cast, and would let a non-numeric value quietly become 0.0."
- `health_agent/ingest/healthkit.py:421-423` (comment in `_record_row`): "Numeric values
  go to value_num; anything else (category identifiers such as
  HKCategoryValueSleepAnalysisAsleepREM) stays as text so aggregates can't coerce it
  to 0."
- `tests/fixtures/README.md` ("Category values as text"): "stored in `value_text`,
  never coerced to 0".
- `health_agent/metrics.py:13-15`: "`CATEGORY` types (sleep, stand hours) carry a string
  value instead of a number; their meaningful aggregate is total *duration* per
  category value."

**Function that breaks it:** `queries._source_value` (`health_agent/store/queries.py:1610-1623`)
and `queries._combine` (`:1626-1645`), reached from `queries.metric_series` (`:884-1004`)
whenever `agg` is `sum` or `avg` and the metric is a category type. The accumulator in
`metric_series` starts at `{"n": 0, "sum": 0.0, ...}` (`:940`) and only adds
`sum_value` when it is not NULL (`:952`). A category row has `sum_value` NULL, so
`acc["sum"]` stays `0.0` and `_source_value` returns `0.0` for `sum`, and
`0.0 / n == 0.0` for `avg`. The value is never `None`, so nothing upstream treats it
as absent.

Both surfaces accept this aggregation: `health-agent metric --agg {sum,avg,...}`
(`cli.py:3095`-ish, help text shown below) and the `query_healthkit` tool's
`aggregation` enum (`agent/tools.py:1585-1594`). Sleep is routed around it by
`_query_healthkit` (`tools.py:112-113`), but every other category type
(`stand-hours`, `mindful`, `high-hr-event`, `audio-exposure-event`, `handwashing`,
and any unregistered `HKCategoryTypeIdentifier*`) goes straight through.

**Scenario, CLI (stock fixture, no extra rows):**

```
$ HEALTH_AGENT_DATA_DIR=<scratch>/data health-agent metric sleep --agg sum
Sleep analysis — sum per day

period      category                 value  samples
----------  -----------------------  -----  -------
2026-03-04  SleepAnalysisInBed        0.00        1
2026-03-05  SleepAnalysisAsleepCore   0.00        1
2026-03-05  SleepAnalysisAsleepDeep   0.00        1
...
```

`--agg avg` prints the same table of 0.00.

**Scenario, tool (two synthetic `HKCategoryTypeIdentifierAppleStandHour` rows added,
value_text `HKCategoryValueAppleStandHourStood`, 3600 s each):**

```python
tools.dispatch(ctx, "query_healthkit", {"metric": "stand-hours", "aggregation": "sum"})
```
returns
```
{"aggregation": "sum", "unit": null,
 "overall": {"value": 0.0, "aggregation": "sum", "periods_covered": 2, "total_samples": 2,
             "note": "Use this value directly. Do not recompute it from the points below."},
 "points": [{"period": "2026-03-03", "value": 0.0, "samples": 1, "category": "HKCategoryValueAppleStandHourStood"},
            {"period": "2026-03-04", "value": 0.0, "samples": 1, ...}]}
```

`aggregation: "avg"` gives `overall.value 0.0` as well. The model is handed a
fabricated zero with an instruction to quote it. Only `min`/`max` come back `None`
(`_source_value` returns `acc["min"]`, which stays `None`), and `count`/`duration`
are correct. A one-line guard in `metric_series` (refuse `sum`/`avg` for
`Kind.CATEGORY`, or return `None` when no row carried `sum_value`) would close it.

---

## Finding 2. Two devices recording the same averaged metric are averaged together with no multi-source warning, although the docs say the warning fires

**Rules (verbatim):**

- `health_agent/store/queries.py:10-17` (module docstring): "A real export has several
  devices writing the same metric: an iPhone and an Apple Watch both record step
  counts, a Watch and an Oura ring both record sleep. ... the default here is to
  compute each source's total independently and report the largest ... and to *tell
  the caller* how many periods had more than one contributing source, so a suspicious
  total is visible rather than silent."
- `docs/oura-open-questions.md` §2: "`source_name` is already a dimension on `record`
  and `daily_metric`, and `multi_source_warning` already fires when a period holds
  more than one source." And option (a): "for averaged ones the two devices' values are
  averaged together, which is the behaviour a reader would have to notice in the
  warning."

**Function that breaks it:** `queries.metric_series`, `health_agent/store/queries.py:971-972`:

```python
if len(by_source) > 1 and agg in ADDITIVE:
    multi_source += 1
```

`multi_source_periods` is only incremented for `sum`/`count`/`duration`. For `avg`
(the default for every `Kind.DISCRETE` metric: heart rate, resting HR, HRV, weight,
SpO2, respiratory rate, all of which a Watch and a ring both record) it stays 0, so
`tools._query_healthkit` (`tools.py:190`) never attaches `multi_source_warning`, and
`cli._print_caveats` (`cli.py:727`) prints nothing either. The values ARE combined
across devices (`_combine` `:1635-1641`, sample-weighted), so the figure the reader
sees is one neither device recorded, and the only trace is `--show-sources` on the
CLI. The tool payload has no per-source field at all for this case.

**Scenario (one synthetic row added: `RestingHeartRate` 70 count/min from
"Fixture Ring" on 2026-03-03; the fixture's Watch has 62 that day):**

```
queries.metric_series(conn, resolve("resting-hr"), period="day")
2026-03-03 66.0 n=2 sources=('Fixture Ring','Fixture Watch') per_source={'Fixture Ring': 70.0, 'Fixture Watch': 62.0}
multi_source_periods = 0   sources_seen = ('Fixture Ring', 'Fixture Watch')

tools.dispatch(ctx, "query_healthkit", {"metric": "resting-hr"})
  -> "multi_source_warning" in payload: False ; overall.value = 61.25
```

The reader is told resting HR on the 3rd was 66 bpm. Neither device said that. The
Oura memo's recommendation (b, refuse a single figure) is explicitly unbuilt, but the
memo's premise that the *warning* already covers the averaged case is false today.

---

## Finding 3. `get_workouts` cuts the session list to 25 (or 50) and never says so

**Rule (verbatim), `health_agent/agent/tools.py:9-16` (module docstring):** "Every tool
here caps what it returns *to the model*, separately from what it queried, and says so
in the payload when it truncates. ... the `truncated` flag exists so the model can say
'I looked at the last 60 days' rather than silently answering from a slice it didn't
know was a slice."

**Functions that break it:**

- `tools._get_workouts_plain`, `tools.py:385-429`. `limit = min(int(args.get("limit") or 25), 50)`
  then `queries.list_workouts(..., limit=limit)`. No count of what was cut, no
  `truncated` key, no note. The companion `totals_by_activity` comes from
  `queries.workout_summary(conn, start, end)` with its default `limit=20` activity
  rows (`queries.py:331-332`), also unflagged.
- `tools._get_workouts_detail`, non-period branch, `tools.py:544-556`. Same cap, same
  silence. (The `period` branch at `:528-533` does flag `truncated`, so the rule was
  applied to one of the three paths.)

**Scenario (30 synthetic Walking workouts added in Feb 2026, plus the fixture's one
run = 31 rows):**

```
tools.dispatch(ctx, "get_workouts", {})
  workouts returned: 25   payload keys: ['totals_by_activity', 'workouts']
tools.dispatch(ctx, "get_workouts", {"detail": True})
  workouts returned: 25   payload keys: ['workouts']
tools.dispatch(ctx, "get_workouts", {"limit": 500})
  workouts returned: 31   (cap of 50 not reached)
```

A model asked "what workouts did I log in February?" sees 25 sessions and nothing
saying six were dropped. `totals_by_activity` does carry `count: 30`, so a careful
model could notice, but the rule is that the payload says it, not that it can be
inferred. Every other tool with a cap (`query_healthkit`, `get_lab_trend`,
`compare_windows`, `get_clinical_history`, `get_clinical_vitals`) sets the flag.

Smaller instances of the same rule, confirmed by reading only (the fixture has too
few rows to show them):

- `tools._get_lab_trends_batch`, `tools.py:1046`: `points = trend.points[-MAX_BATCH_POINTS:]`
  (6) per analyte, with no per-analyte `truncated`. The tool *description* says
  "most recent few results for each", which is where the model would have to have
  learned it.
- `tools._get_clinical_history`, `tools.py:1444-1446`: `facts_of_other_kinds` is capped
  at `MAX_CLINICAL_FACTS` (200) with no flag; the main `facts` list at `:1461` is
  flagged.

---

## Finding 4. `health-agent metric` and the `query_healthkit` tool report different overall averages for the same metric and range

**Rules (verbatim):**

- `health_agent/agent/tools.py:4-8`: "Each is a thin adapter over `store/queries.py` —
  the same functions the CLI calls — so the model and `health-agent metric` cannot
  report different numbers for the same question. A tool that reimplemented its own
  aggregation would be a second source of truth, and the two would drift."
- `health_agent/store/queries.py:4-9`: "the CLI and the LLM see identical numbers by
  construction rather than by two implementations agreeing".
- `CONTRIBUTING.md`, "Working on the agent": "`store/queries.py` is the read contract.
  The CLI uses it today and the agent's tools will use the same functions, so both
  report identical numbers by construction".

**Functions that break it:** the range-level figure is computed in two places, neither
of them `queries.py`, with two different formulas:

- `cli.cmd_metric`, `cli.py:710-717`: `sum(values) / len(values)` over the periods
  *shown* (after `--limit`), unweighted. Printed as "mean across periods shown".
- `tools._overall`, `tools.py:862-896`: `sum(v * n) / total_samples` over *all*
  points, sample-weighted. Handed to the model as `overall.value` with "Use this
  value directly."
- A third formula sits in `windows._summarize`, `store/windows.py:481`:
  `sum(values) / len(values)` over periods, unweighted, which `compare_windows`
  returns as each side's `mean`.

The per-period values agree everywhere (they do come from `metric_series`). The
figure a person actually asks for, the average over a range, does not, whenever
sample counts differ between days, which for heart-rate-class metrics is every day.

**Scenario (same synthetic ring row as Finding 2; resting HR, 2026-03-01..07, daily
n = 1,1,2,1,1,1,1):**

```
$ health-agent metric resting-hr
...
periods: 7   mean across periods shown: 60.6   min: 57.0   max: 66.0

tools.dispatch(ctx, "query_healthkit", {"metric":"resting-hr", "start_date":"2026-03-01", "end_date":"2026-03-07"})
  overall.value = 61.25

tools.dispatch(ctx, "compare_windows", {"metric":"resting-hr", "left_start":"2026-03-01", "left_end":"2026-03-07", ...})
  left.mean = 60.571...
```

Three answers to "what was my average resting heart rate that week": 60.6, 61.25,
60.57. On the stock fixture they coincide only because every day has n=1. On a real
export (heart rate at hundreds of samples per day, resting HR at one) the CLI and the
tool will disagree on every averaged metric, and the CLI number also silently changes
with `--limit`. Fix is the one the docstring prescribes: put the range aggregate in
`queries` (on `SeriesResult`) and have all three read it.

---

## Finding 5. `compare_windows` and `query_healthkit` each hand the model a mean the other does not (part of 4; listed so it is not lost)

Covered above. `windows._summarize` reports `mean` unweighted while the module it
"composes over" (`windows.py:4`: "Composes over `queries.py` and holds no SQL of its
own") provides no range aggregate at all, so every layer invents one. No separate
scenario needed; see Finding 4's third figure.

---

## Checked and clean

Each line: rule, functions covered, one-line verdict.

- `queries.py` multi-source/largest-source rule: `metric_series` (`combine="max"` per period after per-source totals), `sleep_nights` (fixed, per the brief), `workout_series._summed`/`workout_summary` (sum across workouts, not across devices of one metric; the Strong memo lists device-duplicated sessions as an open question, so the rule's wording does not cover them). Clean apart from Finding 2.
- `metrics.py` cumulative=SUM / discrete=MEAN / category=duration defaults: `Metric.default_agg`, `metric_series(agg=None)`, `unregistered()` (discrete by default, visible failure). Clean apart from Finding 1's explicit-override path.
- `sqlite_schema.py` dedup idempotence, `local_date` precomputed, `daily_metric` real table: `healthkit._dedup_key`, `_normalize` (date from the record's own offset), `rebuild_daily_metrics`. Clean. Note the docstring line "Opening an index written by a different version raises `SchemaVersionMismatch`" is now only true below v5; `open_for_read` migrates v5→v6 in place, which `migrate`'s own docstring explains. Stale wording, not a defect.
- `healthkit.py` three details (timestamp offset, Correlation duplicates, optional unit/value): `_record_row`, `ingest_export` (`"Correlation" in stack`), `parse_hk_datetime`. Clean; fixture README's expected counts reproduce (2 stored, 2 skipped).
- `tools.py` "Absence is data": `_query_healthkit`, `_query_sleep`, `_get_workouts_*` / `_no_workouts_payload`, `_get_lab_trend`, `_get_lab_trends_batch` (`no_results_for`), `_search_records` (chunks indexed + tags), `_search_medical_literature` (`coverage()`), `_get_clinical_history`, `_get_clinical_vitals`, `_compare_windows` (coverage per side). All return coverage, not an empty list.
- `tools.py` "Tools return data, never prose": every handler returns JSON; notes are fixed strings. Clean.
- `windows.py` no SQL, never computes the gap, distinct absences kept distinct: `_series`, `_summarize`, `_verdict`, `render`, `_coverage_tail`, `_held_clauses`. Clean (the only arithmetic between sides is the overlap test).
- `visit_prep.py` gather composes no prose / render reads no DB / no model: `gather`, `metric_signals`, `lab_signals_for`, `render`, `attach_literature` (reads literature only, called separately). Clean. Thresholds are stated as the tool's cutoffs in `_evidence_sentence`.
- `context.py` model never sees findings, runs after guard: `orchestrator.ask` builds `literature_context` after `_guard` and never appends it to `transcript`. Clean.
- `guardrail.py` worked examples: "Your LDL is high" passes; "You have high cholesterol" trips `diagnosis`; "You should ask your doctor" passes; "You should take 2000 IU of vitamin D" trips `treatment` twice; threshold sentence without a PMID trips `uncited_medical_claim`; with a returned PMID passes; with an unreturned PMID trips "cites a PMID the literature tool did not return". All verified by running `guardrail.apply`.
- `logging_setup.py` metadata-only logs: all 30 `log.*` calls outside the module reviewed. Filenames, counts, exception type names, categories, positions. `records.py:268` logs `exc` itself, but `OcrUnavailable` is constructed only with fixed strings ("pytesseract is not installed", "tesseract failed: <TypeName>", "page render failed: <TypeName>"). Clean.
- `derive.py` units never converted / incompatible inputs return no value and a reason: `pace`, `speed` (time composition only), `efficiency`, `ratio` (refuses unit mismatch), `median`, `percentile` (nearest rank, no interpolation); `queries._summed` / `_median_in_one_unit` return `(None, "mixed")`. Clean.
- `labs.py` unknown analyte gets a slug, `expected_units` advisory only: `resolve_key`, `resolve_coded`, `unit_looks_wrong` (no caller converts). Clean.
- `literature/store.py` strength first, relevance second; result says which path ran: `_ordered` (rank then retrieval order, unranked last), `keyword_search`, `search` (first chunk per article in relevance order, then `_ordered`), `hits`, `Finding.method`. Clean.
- `literature/tiers.py` protocol overrides, Review ≠ Systematic Review, unknown unranked: `resolve`, `tier_at_least`, `_filters` (`IS NOT NULL`). Clean.
- `literature/packs.py` installer re-derives tiers: `cli.py:2428` calls `lit_tiers.resolve(article.publication_types)` on install. Clean.
- `literature/fetch/client.py` one `get`, redirects re-checked: `_allowed` (hostname, https, no userinfo), `_AllowListRedirects.redirect_request`, `_open`. Clean.
- `literature/fetch/refresh.py` "Nothing here prints": no `print(` in `refresh.py`, `eutils.py`, `fetch/packs.py`, or `corpus.py`. Clean.
- `ollama_client.py` non-loopback refused unless env set: `assert_local` via `resolve_host`. Clean. (Its docstring's "the only module that opens a socket" predates `backends.py` and `fetch/client.py`; `CONTRIBUTING.md` and the surface test name the current set of three, so the claim is stale wording rather than a function breaking a rule.)
- `consent.py` per-folder record, version stored, separate file per notice, fails closed: `consent_path`, `load` (any read error → None → ask again), `record`, `ConsentRecord.is_current`. Clean.
- `backends.py` tier is a property of the backend and the CLI prints it: `cmd_ask` prints `[CLOUD — data is sent to Anthropic]` or `[local]` from `orchestrator.tier`, never from the flag. `CloudBackend` is only constructed after `_confirm_cloud`. Clean.
- `offline_check.py` refuses a pass under `none`: `CheckResult.proven` requires mechanism in `("os-sandbox", "python")`. Clean.
- `vector_store.py` vectors from different models never mixed, fallback says so: `VectorStore.search` prefilters `embedder = name`; `_search_records` returns `method`. Clean.
- `embeddings.py` hashing never selected implicitly: every `--embedder` argparse default is `ollama`; `hashing` must be named. Clean.
- `notes.py` never writes `lab_result`; unparsed frontmatter kept under `_unparsed`: `parse_frontmatter` (nested maps and unmatched lines → `_unparsed`), `ingest_note` writes `document`/`chunk`/`document_tag` only. Clean.
- `ingest/fhir.py` no identity fields, attachment URLs dropped, Patient ignored: `ParsedLab`/`ParsedVital`/`ParsedFact`/`ParsedClinicalDoc` carry no subject/encounter/performer/recorder/url field; `_DOC_TYPES` excludes Patient. Clean by construction, as the docstring claims.
- `ingest/records.py` every stored lab row keeps `raw_line`: both passes set it (`:483`, `:562`) and the INSERT writes it (`:691`). Clean.
- `orchestrator.py` every turn recorded, thinking off by default: `Answer.steps` appended per call in `_run_call`; `think=False` default. Clean.
- `queries._collapse` cross-source lab dedup only collapses across sources, reports disagreements rather than resolving: read end to end. Clean.
- `CONTRIBUTING.md` "never make the model do arithmetic / any tool returning a series also returns the aggregate": `query_healthkit` (`overall`), `compare_windows` (per-side min/max/median/mean), `get_workouts` period mode (sums + medians). Satisfied as stated; the aggregates disagree with each other, which is Finding 4, not this rule.
