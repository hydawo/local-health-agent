# Sweep: checks that pass without measuring what they claim

Repo: local_health_agent @ f87c902. Read-only audit of `tests/test_*.py`.

## Coverage of the sweep

All 34 `tests/test_*.py` files were read end to end: 1107 test function
definitions (1288 collected items once parametrization is counted). Every
candidate below was confirmed by running the code path or the test under
instrumentation; mutations are described, not applied.

## Confirmed findings, worst first

### 1. tests/test_no_network.py:183 `test_no_telemetry_on_import` (category 5/3: the fixture cannot reach the behaviour)

The test walks `health_agent.*` with `importlib.import_module` under the
`no_network` socket guard, to prove no module phones home at import time.
`import_module` on an already-imported module returns the cached object and
executes nothing. Pytest collection imports every test module first, and
those import nearly the whole package at module scope (conftest imports
`healthkit` and `sqlite_schema`; `test_cli` imports `cli`; `test_agent`
imports `ollama_client`, `orchestrator`, `tools`, ...). Instrumented with a
plugin that lists uncached modules at the moment the test body runs:

- full suite: `not-yet-loaded: []` (zero modules execute under the guard)
- `tests/test_no_network.py` alone: nine modules are fresh; the other ~20
  are cached by the file's own top-level `from health_agent import offline_check`.

Surviving mutation (source): add at module scope of
`health_agent/ollama_client.py`
```python
try:
    list_models(DEFAULT_HOST, timeout=1)   # phone-home on import
except Exception:
    pass
```
`ollama_client` is cached long before the guard is installed, so the test
imports nothing and passes. `test_the_network_surface_is_exactly_three_modules`
does not catch it either, because `ollama_client.py` is already on its allow
list. The fix is to import each module in a subprocess with the guard
installed first (the file already uses that pattern in
`test_the_query_path_cannot_reach_a_fetcher`), or to pop
`health_agent*` from `sys.modules` inside the test.

### 2. tests/test_no_network.py:102 `test_the_block_catches_our_own_client` (category 4 + 1)

```python
with pytest.raises(Exception) as exc:
    ollama_client.list_models("http://localhost:11434", timeout=1)
assert exc.value is not None
```
Docstring: "under the block it must fail, which confirms the guard sees real
call paths". `pytest.raises(Exception)` accepts any exception, and
`exc.value is not None` is always true inside that block. Nothing checks the
exception is `NetworkAttempted`, or that the wrapped `OllamaUnavailable`
has a `NetworkAttempted` cause.

Surviving mutation (source): in `health_agent/ollama_client.py`, make
`list_models` start with `raise OllamaUnavailable("not implemented")` (or
any `TypeError` from a signature slip). The test still passes while the
guard is never reached. The assertion should be
`pytest.raises(NetworkAttempted)` or inspect `exc.value.__cause__`.

### 3. tests/test_agent.py:369 `test_search_records_can_restrict_to_notes` (category 1)

```python
result = tools.dispatch(ctx, "search_records", {"query": "coffee", "kind": "note"})
assert all(r["kind"] == "note" for r in result["results"])
```
No assertion that `results` is non-empty. Today the call returns 2 hits, so
the `all()` is evaluated, but a filter that excludes everything passes just
as well: the no-match branch returns `{"results": [], ...}` and `all([])`
is `True`. The CLI `search --kind note` tests go through `cli.cmd_search`
straight to `vector_store`, so they do not cover the tool's own `kind`
handling.

Surviving mutation (source): `health_agent/agent/tools.py:1103`, change the
normalisation to `kind = kind.upper()` (or compare to the plural `"notes"`).
`keyword_search` then matches no rows, the tool returns an empty
`no_matches` payload, and the test passes.

### 4. tests/test_literature_tool.py:68 `test_retracted_findings_carry_a_warning_for_the_model` (category 3 + 1)

```python
if any(f["retracted"] for f in payload["findings"]):
    assert "retract" in payload["retraction_warning"].lower()
```
The whole assertion is behind a condition the payload controls. Confirmed
the branch is taken today (PMID 40000008 is returned), but no test asserts
that the tool payload ever carries `retracted: True`, so the condition is
free to become false.

Surviving mutation (source): `health_agent/agent/tools.py:1258`, write
`"retracted": False` for every finding (the kind of slip a refactor of
`Finding` to a dict makes). `any(...)` is false, no assertion runs, the
test passes, and the model never sees a retraction flag or warning. The
store-level test (`test_literature_search.py:120`) checks `Finding.retracted`,
not the tool payload.

### 5. tests/test_agent.py:381 `test_search_result_count_is_capped` (category 3: fixture cannot reach the cap)

```python
result = tools.dispatch(ctx, "search_records", {"query": "cholesterol", "limit": 999})
assert len(result["results"]) <= tools.MAX_SEARCH_RESULTS
```
`MAX_SEARCH_RESULTS` is 8. "cholesterol" matches 6 chunks in the fixtures
(measured with `vector_store.keyword_search(conn, "cholesterol", limit=999)`),
so the cap never binds.

Surviving mutation (source): `health_agent/agent/tools.py:1101`, replace
`limit = min(int(args.get("limit") or 5), MAX_SEARCH_RESULTS)` with
`limit = int(args.get("limit") or 5)`. Still 6 results, still `<= 8`.

### 6. tests/test_literature_tool.py:76 `test_findings_are_capped` (category 3)

Same shape. `MAX_LITERATURE_FINDINGS` is 5; "blood pressure" matches 3 of
the 10 fixture articles (measured with `limit=99`). Removing the
`min(..., MAX_LITERATURE_FINDINGS)` at `tools.py:1182` leaves 3 findings and
the test passes. A query that matches more than 5 articles ("study", which
hits 7+), or a corpus built from a duplicated fixture, is needed for either
cap test to measure anything.

### 7. tests/test_eval.py:1657 `test_the_report_names_the_questions_those_columns_do_not_measure` (category 1 + 6)

```python
assert "correct number actually scored: 1/2 draws (Q22 has no value to "
assert "correct number actually scored: 1/2" in report
assert "expected tools actually scored: 1/2" in report
assert "Q22" in report
```
Line 1666 asserts a bare string literal: always true, never evaluated
against `report`. The test's name is about the report *naming the question*,
but the only check for the name is `"Q22" in report`, and the report always
contains a `### Q22` heading in its `## Answers` section regardless of
what the summary says. (The literal is also stale: the live wording is
`(nothing to check on Q22)`, which is why it was never turned into a real
assertion.)

Surviving mutation (source): in `run_agent_eval.render`, drop the
`(nothing to check on ...)` parenthetical from both summary lines. The two
`1/2` assertions still match, `"Q22" in report` is satisfied by the answers
section, and the test passes while the report stops naming the unmeasured
questions. Fix: assert against `report.split("## Answers")[0]` and include
the parenthetical.

## Suspected but not confirmed as a surviving mutation

- `tests/test_windows.py:1030` `test_a_side_that_carries_values_always_carries_a_unit_name`:
  the docstring pins an invariant over four spec shapes, but the fixture
  leaves every metric side unit-mixed and every duration/pace side mixed or
  empty, so the inner `assert` fires on 2 of the 24 sides, both for the
  `measure=HR_AVG` spec. A `unit=None` regression on the metric or derived
  paths would not be seen by this test; it would be caught by
  `test_sides_in_different_units_are_never_called_comparable` and
  `test_a_derivation_is_read_from_the_periods_derived_figures`, so no
  mutation survives the suite, but the test covers one quarter of what it
  says it pins.
- `tests/test_literature_search.py:54` `test_every_finding_carries_its_tier_and_provenance`
  and `:107` `test_since_year_filters_by_publication_year`: loop / `all()`
  with no non-empty guard. Both are evaluated today (3 and 2 hits), and the
  realistic filter inversions I tried would still surface a pre-2020 row and
  fail, so I could not name a plausible surviving source mutation. A fixture
  edit that stops "blood" matching would void them silently.
- `tests/test_literature_search.py:71-72`: `assert all(... for h in hits)` two
  lines after `assert hits == []`. Dead lines, but the test as a whole
  measures the filter.
- `tests/test_notes_ingest.py:285` `test_unreadable_note_is_skipped_and_run_continues`:
  the name says skipped; the body asserts `stats.notes == 2`, i.e. the file
  was salvaged and stored. It measures real behaviour; the name predates it.
- `tests/test_context.py:231` and `tests/test_visit_prep.py:535`: the
  guardrail oracle passes trivially if `lab_context`/`gather` return nothing
  (empty text has no flags). Each is protected only by a neighbouring test
  that asserts findings exist on the same fixture.

## Things checked and found sound (so they are not repeated above)

The `pytest.raises` without `match=` list (about 75 sites) was reviewed; the
rest either raise a project-specific exception type that nothing earlier in
the call could produce, or follow with a `str(exc.value)` check. The
monkeypatches in `test_visit_prep`, `test_context`, `test_cloud_tier`,
`test_literature_fetch` and `test_ingest_clinical` were checked to reach the
real code path (each has an assertion that only the patched call can
satisfy, e.g. `seen["limit"] == 5`, `calls == []`, `stats.failed == 1`).
`test_scorer_contract.py` guards its own crawl against emptiness
(`len(payloads) > 100`, planted-phrase mutation test), which is the shape
the rest of the suite should copy.
