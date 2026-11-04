# Provenance sweep, local_health_agent @ f87c902 (read-only, 2026-10-31)

Shape hunted: a figure chosen between candidates, or derived from one of several inputs,
whose payload or printed output does not say which one it came from.
Cases: (1) output names the origin; (2) output omits it but the caller could reconstruct it;
(3) output omits it and nothing downstream can recover it. Only (3) is a finding.

Fixture used: tests/fixtures/export.xml (steps from Fixture Phone + Fixture Watch on
2026-03-02; sleep from Fixture Ring + Fixture Watch on one night). Synthetic indexes were
built the way tests/test_queries.py and tests/test_visit_prep.py build theirs. Scratch
script: /private/tmp/claude-501/-Users-hydawo-Downloads-Claude-Code-local-health-agent/c7138384-bc46-4f57-8548-6f78dacd1a80/scratchpad/sweep.py (+ sweep2.py, cli_run.py).

39 choice points enumerated. 7 findings (case 3), ranked worst first. 6 case-2 notes. 20+ case-1.

## Findings (case 3), worst first

### F1. visit_prep renders a same-day pdf/fhir disagreement as a change over time
- health_agent/visit_prep.py:90-131 `lab_signals_for`: `points.sort(key=collected_date)`; `latest = points[-1]`, `previous = points[-2]`.
- queries.lab_trend returns both rows of a cross-source disagreement as two points (by design, `_collapse` keeps them and reports them in `trend.disagreements`). visit_prep never reads `disagreements` (grep: no use outside queries.py and tools.py). The stable sort leaves same-day points in SQL order (page_no, id), so which one is "latest" is row-id order.
- Output (pdf LDL 130 and FHIR LDL 160, both 2026-03-10, range 0-100):
  ```
  1. **Ask whether your LDL cholesterol needs follow-up.** It was 160 mg/dL on 2026-03-10
     against the printed range of 0-100 mg/dL, up from 130 mg/dL on 2026-03-10
     (Observation-1.json, p.1; labs.pdf, p.1).
  ```
  With the rows inserted in the other order the sheet prints "130 ... up from 160" (verified).
- What the clinician cannot tell: that these are two sources' readings of one draw, not two draws. "up from" asserts a direction that depends on insert order. Case 3: the sheet is a printed document; nothing on it says a disagreement exists. (The agent tool for the same trend does say it: `source_disagreement` note, verified.)
- Same code path, same defect for `lab_returned_to_range` and `lab_near_limit`.

### F2. visit_prep metric_shift cannot distinguish a device change from a behaviour change
- health_agent/visit_prep.py:199-258 `_daily_values` -> `metric_series(period="day")` (combine=max per day) -> `{date: value}`; `metric_signals` compares two 30-day means. `sources`, `per_source`, `sources_seen`, `multi_source_periods` are all dropped at line 208.
- Output (Phone 4000/day for 60 days, Watch 7000/day added for the last 30):
  ```
  1. **Ask about your steps.** It averaged 7000 count over 2026-05-31 to 2026-06-29 against
     4000 count over 2026-05-01 to 2026-05-30, a shift larger than this tool's cutoff ...
  ```
- The prior window is the phone's figure, the recent window is the watch's. Nothing on the sheet names either device, and the "Looked at" block says only "Apple Health, 30-day windows". Case 3. The test at tests/test_visit_prep.py:223 guards this for sleep (where it was a summing bug); for steps, resting-hr, weight, hrv it is the honest per-day max and still a device artefact the reader cannot see. Sleep via `_daily_values` also drops `source`.

### F3. query_healthkit points name no source; `samples` counts every device while `value` is one device's
- health_agent/agent/tools.py:166-170: `{"period", "value", "samples": p.n}`; `p.sources` and `p.per_source` (present on SeriesPoint, queries.py:57-58) are not emitted. `multi_source_warning` lists `sources_seen` for the whole series, not the winner per period.
- Output (fixture, steps):
  ```
  "points": [{"period": "2026-03-02", "value": 6000.0, "samples": 5},
             {"period": "2026-03-03", "value": 8500.0, "samples": 2}],
  "overall": {"value": 14500.0, "total_samples": 7, ...},
  "multi_source_warning": "1 period(s) had more than one recording device (Fixture Phone, Fixture Watch). Values shown take the largest single source ..."
  ```
  Store layer for the same day: value 6000 (Watch), per_source {Phone 5500, Watch 6000}, n=5 = Phone 2 + Watch 3.
- The model cannot tell which device 6000 is, which periods were the collided ones, or that `samples: 5` is not the sample count behind 6000. `_overall.total_samples` inherits the same mismatch (tools.py:863-897). Case 3 at the tool boundary (the model has no other way to ask; `source` is an input filter, not an output). This is the exact gap sleep closed with `source` + `per_source_asleep_hours`, still open for every quantity metric.

### F4. compare_windows for a metric carries no source information at all, and drops the multi-source count
- health_agent/store/windows.py:437-490 `_summarize` builds SideSummary from `point.value` only; `series.multi_source_periods`, `sources_seen`, `per_source` are not read. `counted_n` sums `p.n` across all devices. tools.py:672-703 `_side_payload`, 705-720 `_points_payload` emit `value`/`n` only.
- Output (fixture steps, left 03-02..03-03 vs right 03-03): the full payload and the `rendered` text contain no device name and no multi-source warning; `left_points[0] = {"value": 6000.0, "n": 5}` (Watch value, Phone+Watch count). The CLI `compare steps ...` prints "6,000 count | 5 records" likewise.
- A comparison whose two sides come from different devices (F2's scenario) is reported as `comparable: ok` with nothing to say so. Case 3. Only `sources_excluded` (an input) is echoed.

### F5. windows `_held` names one matched activity while counting all of them
- health_agent/store/windows.py:412-421: `mine` = every activity matching the substring; `"activity": _activity_name(mine[0]["activity_type"])`, `"workouts": sum(r["n"] for r in mine)`.
- Output (fixture + one inserted Cycling workout, `compare --workouts --activity ing ...`):
  ```
  ... the index holds 2 workouts of Cycling, from 2026-03-06 to 2026-03-06.
  ```
  The two workouts are one Running and one Cycling. Case 3: the text attributes both to the first row's activity.

### F6. Workout period series is labelled by the caller's substring and names neither activity nor source
- health_agent/store/queries.py:777 `label = activity or "all workouts"` (the LIKE pattern, not what matched); `_summed` 669-679 adds every workout in the period with no source list; WorkoutSeriesPoint has no `sources`/`activities`. tools.py:523-527 emits `"series": series.label`; cli.py `_workouts_series` table has no label/activity/source column at all.
- Output (tool, activity "ing"): `{"series": "ing", "points": [{"period": "2026-03", "workouts": 2, "duration": 75.0, ...}]}` over Running (Watch) + Cycling (Phone). CLI prints `2026-03 | 2 | 75.0 min | 10.0 km | 600.0 kcal` with no heading.
- This is the same shape as the fixed `"series": "Cycling nope"` case: the series is named by what was asked, not what was read. Case 3 for the period payload itself; case 2 overall since `_get_workouts_detail` without `period` lists each workout with `activity` and `source`.

### F7. `out_of_range_on_latest_report` picks one of two conflicting same-day values by row id
- health_agent/agent/tools.py:1066-1075 (`_get_lab_trends_batch`): `latest = trend.points[-1]`; on a disagreement date this is whichever source's row has the higher id.
- Output (pdf LDL 130 out of range, FHIR LDL 95 in range, same date): `"out_of_range_on_latest_report": []`, while `analytes.ldl.source_disagreement` says "130.0 mg/dL from pdf; 95.0 mg/dL from fhir". Insert in the other order and the block flags 130.
- Borderline case 2/3: the disagreement note is in the same payload, but the flagged block is the precomputed summary the model is told to trust ("so the model doesn't have to scan every result"), and it silently resolved what the note says not to resolve. Ranked last because the note is present.

## Case 2 (omits the origin, reconstructable), one line each
- cli.py `metric` table: `samples` is all devices, `--show-sources` lists every device on the period, not the winner; `--json` carries `per_source` (verified).
- cli.py `sleep` table: no source column, and the chosen device is not named per night; the note says `--json carries every device's own totals` (verified).
- cli.py `_trend_as_dict` / `labs` table: `source` (pdf/fhir), `also_from`, `disagreements` not emitted; `source_path` / citation name the file, and both same-day points are listed.
- tools.py `_get_lab_trend` point: `also_from` not emitted (a value both sources agreed on reads as one source's); `source` and `citation` are.
- queries.py:579 `workout_measures`: `MAX(s.unit)` picks one unit per key for the absence message when a measure was stored in two units.
- queries.py `workout_summary` (CLI `workouts` default, tool `totals_by_activity`): sums every source per activity, no source list; per-workout rows beside it carry `source`.

## Choice points that already name their origin (case 1)
- queries.sleep_nights: `source`, `sources`, `multi_source`, `per_source`, chosen source's own span (queries.py:268-294); tools `_query_sleep` passes `source` and `per_source_asleep_hours`; CLI prints the per-night caveat.
- queries.metric_series at the store layer: `SeriesPoint.sources`, `per_source`, `SeriesResult.combine`, `sources_seen`, `multi_source_periods`, `units_seen`; the `avg` path reports `combine="weighted"`.
- queries._collapse: survivor carries citation (file, page), `source` pdf/fhir, `also_from`; `disagreements` lists each source's value; tools `_source_disagreement_note` says "do not choose".
- queries._median_in_one_unit / `_summed` / workout_series unit: refuse with `unit="mixed"` and list `units` rather than pick one; `pct_measure` only when exactly one measure.
- clinical_vitals: `unit` None when >1 unit; every row carries `citation`; tool note separates clinician from watch.
- windows `_instead`: `substituted` block + `measure_substituted` in the payload, `series` label is the effective spec.
- windows.resolve_anchor: ambiguity refused (`AnchorAmbiguous`), same-date names joined into one label.
- windows unit: `sorted(seen)[0]` only when one unit; `units_seen`, `unit_mixed` carried.
- tools `_get_workouts_detail` rows and `_workout_row`: `source`, `activity`, `citation` per workout.
- Literature: every finding carries `citation`, `pmid`, `doi`, `evidence_tier`, `tier_source`; visit_prep `attach_literature` `min(...)` names the chosen article's title/year/pmid.
- visit_prep lab evidence: `citation` on `latest` and `previous`; `_ocr_caveats`; "Looked at" names the window and its end date.
- labs.resolve_key: payload returns the resolved `analyte` key and `label`.

## Weak or unproduced
- F2 and F4 are shown on synthetic data (the fixture has only two days of steps, so no 30-day windows); F3, F4-fixture, F5, F6 are on the fixture. F7's reversed-order variant was verified for F1's code path and asserted for F7 by reading the same `points[-1]` logic; its empty-block output is shown.
