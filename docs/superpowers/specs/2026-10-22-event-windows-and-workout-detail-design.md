# Event windows and workout detail: the primitives, measured against the ACL analysis

Sub-project 1 of five. Unlocks the per-workout detail already sitting in the
index, and adds the primitives that let a question be asked about a window of
time either side of a dated event. No new sources, no new readers, no document
output. Nothing new is read from disk: every number this sub-project exposes is
already in `health.db` today.

## 0. Why this exists, and what counts as done

The agent answers questions about one metric, or one analyte, or a list of
workouts. The ACL analysis in `personal_website` answered a different kind of
question: what happened either side of a date, across sources, with the
limitations of each source stated. That analysis is the target. Its numbers
were derived by hand and are known, which makes them an eval set of exactly
the kind this project already runs.

Three of its seven target questions are answerable from data already ingested.
They are the acceptance test for this sub-project:

- **T1.** What did the watch see the week of the tear (2023-02-18)? Walking
  asymmetry from a prior-week 0 to 4% up to 93% on day 1, walking speed 2.65
  down to 1.68 mph, resting heart rate 63 up to 75 to 90 on days 2 to 6.
- **T3.** How did summer 2023 training compare with the year before? 54 rides
  and 44 band sessions between 2023-05-08 and 2023-09-14, 128 against 24
  minutes a week, ride heart rate 145 falling to 136 at the same energy cost,
  and the caveat that the earlier year's rides carry almost no heart-rate
  samples.
- **T7.** What are the runs since the return, and can they be compared with
  before? All 131 indoor, first run 15:58 per mile, efficiency falling from
  4.1 to 3.1 mph per 100 bpm, and then the refusal: the pace ranges never
  overlap and the earlier runs carry about three heart-rate samples each, so
  no like-for-like comparison is available.

T7's refusal matters more than T7's numbers. A tool that compares two windows
will be asked to compare windows that cannot be compared, and the project's
position on saying what it cannot support is the thing being proved.

**Descriptive only.** Counts, medians, means, minima, maxima, ratios,
percentiles and window comparisons. No model fits, no effect sizes, no
p-values, no seasonal adjustment, no correlation coefficients. This is a
binding constraint on every payload in this document, and it puts two findings
from the source analysis permanently out of reach: the season-adjusted walking
speed residual, and every Cliff's delta. That cost was accepted when the
target set was agreed.

## 1. What is already in the index, unused

`workout` stores `statistics_json` and `metadata_json` verbatim at ingest.
Between them they hold the per-workout heart-rate statistics and the indoor
flag, which is to say the whole of T7 and the heart-rate half of T3. Nothing
reads either column: no query function, no CLI command, no tool.

`daily_metric` keeps `source_name` on every row, so a metric can be filtered
to, or away from, a named source without re-aggregating from `record`.

`record` carries `idx_record_type_epoch` on `(type, start_epoch)`, which is
the index needed to count how many samples of a type fall inside a workout's
interval. That count is how source thinness becomes a reportable number
rather than a remembered caveat.

So this sub-project adds one table, one column, four query functions, one
agent tool, two CLI surfaces, and no parsing.

## 2. Schema v6, migrated in place

- New table `workout_stat(workout_id → workout, kind, key, value_num,
  value_text, unit)`, with `kind` either `'statistic'` or `'metadata'`, and a
  unique index on `(workout_id, kind, key)`. One row per statistic or metadata
  entry a workout carries.
- New column `workout.indoor INTEGER` (1, 0 or null). Promoted out of
  `workout_stat` because T7 filters on it and "131 of 131 indoor" is a
  coverage statement the absence rules below have to be able to make.
- `SCHEMA_VERSION = 6`, and a new `migrate(conn) -> int | None` that takes a
  v5 file to v6 in place: it creates the table and the column and backfills
  both from the `statistics_json` and `metadata_json` already stored on every
  workout row, then restamps the version and returns the version it migrated
  from. `open_for_read()` calls it before `check_version()`, and `cmd_ingest`
  calls it where it already calls `check_version()`, so every entry point
  migrates once and no caller sees a v5 file. `connect()` itself stays a plain
  open with no version opinion, as it is today. A version older than 5 still
  refuses and names `ingest --rebuild`, as before.

In-place migration is new for the personal index, which has refused every
earlier bump. The reason is the same one the literature corpus already accepts
at its v5: refusing costs a 3.7 GB, 6.8-minute reparse of an export whose
bytes have not changed, to recover data the index already holds. A migration
that reads only columns in the file it is migrating cannot produce a number
the export did not contain.

Keys are stored as the export names them (`HKQuantityTypeIdentifierHeartRate`,
`HKIndoorWorkout`) with no renaming, so a key that appears in a future export
needs no code change to be stored. The mapping from key to a readable label
belongs in `metrics.py`, which already owns that vocabulary.

## 3. Workout detail and workout series

Four functions in `store/queries.py`, beside the existing workout functions:

```python
workout_detail(conn, *, activity=None, start=None, end=None, indoor=None,
               source_include=None, source_exclude=None,
               measures=(), derive=(), sample_counts=False,
               limit=200) -> WorkoutDetail
```

One row per workout: its date, activity type, duration, distance, energy,
source name, indoor flag, every requested `measure` from `workout_stat`, and
every requested derivation from §5. `sample_counts=True` adds, per workout,
the number of `record` rows of the measure's own type whose `start_epoch`
falls inside the workout's interval. It is opt-in because it is a subquery per
row, and it is the only honest way to say "these runs carry about three
samples each".

```python
workout_series(conn, *, period="week", activity=None, start=None, end=None,
               indoor=None, source_include=None, source_exclude=None,
               measures=(), derive=()) -> SeriesResult
```

One row per period: workout count, summed duration, distance and energy, and
the median of each requested measure and derivation. It returns a
`WorkoutSeries`, not a `SeriesResult`, because `SeriesResult` carries a
`Metric` and a workout series has no metric. `WorkoutSeries` mirrors the field
names a caller already knows (`points`, `unit`, `is_empty`, `total_records`,
`available_from`, `available_to`) so the window code in §4 can read either
without asking which it has. Periods are the existing `PERIODS` tuple, `day`,
`week` and `month`; there is no year bucket, and T3 and T7 compare explicit
ranges rather than year buckets. T3's "128 against 24 minutes a week" is this
function with `period="week"` and `activity="cycling"`.

```python
workout_measures(conn) -> list[dict]
```

One row per measure present in `workout_stat`: the key, a readable label, how
many workouts carry it, and its date span. This is what every absence message
in §7 reads.

`metric_series` gains `source_exclude: Sequence[str] = ()`, filtering
`daily_metric.source_name`. Its existing `source` argument keeps its meaning.
Passing a name in both is an error, not a precedence rule.

## 4. The window primitive

New module `health_agent/store/windows.py`. It composes over `queries.py` and
opens no database cursor of its own, so the one-read-path rule holds.

```python
resolve_anchor(conn, anchor) -> Anchor | AnchorAmbiguous | None
window(conn, *, series_spec, anchor, before_days, after_days,
       period="day") -> Window
compare(conn, *, series_spec, left, right) -> Comparison
```

An anchor is either an explicit date, or a reference to a dated row the index
already holds: a clinical fact by kind and name, a procedure, a prescription's
start. `resolve_anchor` returns the single matching row and its date, or, when
more than one row matches, every candidate with its date and citation and no
choice made. Guessing which of two procedures someone meant is the same
failure as reporting an empty window as an empty record.

A `Window` carries the per-period points on each side, each side's summary
(n, min, max, median, mean, unit, and the count of periods with no data), the
anchor's date and label, and the citation for every number. T1 is one call:
walking asymmetry, walking speed and resting heart rate, anchored on
2023-02-18, seven days before and six after, period `day`.

A `Comparison` takes two windows or two explicit ranges and returns both
summaries side by side plus the verdict in §6. It never returns a difference,
a percentage change or a ratio between the two sides. The caller can see both
numbers; computing the gap between them is the reader's business, and the
moment a tool returns "32% worse" it has made a claim its citation cannot
carry.

## 5. Derivations, as a closed vocabulary

Five, by name, and no expression language:

| name | definition | unit |
| --- | --- | --- |
| `pace` | duration / distance | minutes per distance unit as stored |
| `speed` | distance / duration | distance unit per hour as stored |
| `efficiency` | speed / (heart rate / 100) | stored speed unit per 100 bpm |
| `ratio` | one named measure over another | dimensionless |
| `percentile` | p50 or p90 of a measure over the rows in scope | the measure's unit |

A closed list, because an expression language is the model doing arithmetic
with extra steps, and the prompt has forbidden that since the day it answered
59.6 for an average that was exactly 60.0.

**Units are never converted.** A derivation reports the unit its inputs
carried and names it. Where inputs are in units that cannot be combined, the
derivation returns null with `reason: "incompatible units"` and names both.
T7's 15:58 per mile and 3.1 mph per 100 bpm are only reachable because the
export stores miles; an export in kilometres yields the same numbers in
kilometres and says so.

## 6. The comparability verdict

Every `Comparison` carries a `comparable` block, built by rule, with four
named checks:

- **`n`**, the row count on each side.
- **`measure_coverage`**, how many rows on each side carry the measure, and
  how many do not. This is where T3's "the earlier year's rides carry almost
  no heart-rate samples" becomes a number instead of a memory.
- **`sample_depth`**, which with `sample_counts=True` is the median in-interval
  sample count per side.
- **`range_overlap`**, whether the two sides' value ranges intersect. When
  they do not, `comparable.verdict` is `"no_overlap"` and the rendered
  sentence says a like-for-like comparison is not available, naming both
  ranges. This is T7's refusal, and it is a rule rather than a judgment.

The verdict is one of `ok`, `no_overlap`, `thin_on_one_side`,
`missing_measure_on_one_side`, or `insufficient_data`, with the numbers that
produced it. It is rendered from a fixed template, the way the visit-prep
sheet and the literature block are, so the guardrail can be run over the
rendered text as a test oracle.

`comparable` never says `significant`, `likely`, `suggests`, or anything about
cause. Two windows either side of a date are two windows either side of a
date.

## 7. Absence, reported as coverage

The doctrine this project broke four times on the previous branch. Every path
below says what the index does hold:

- No workout carries the requested measure: name every measure that is
  present, with counts, from `workout_measures`.
- A window has no data on one side: give the periods with no data as a count,
  the side's span, and the metric's full available span.
- The anchor resolves to nothing: say so and name the dated row kinds that
  could serve as anchors.
- The anchor is ambiguous: return every candidate with its date and citation.
- No workouts match the activity or the indoor filter: name the activity types
  present in the range, with counts.
- `source_exclude` removes every row: say which source was excluded and what
  remained, so an empty result cannot be read as an empty record.

## 8. The agent tool surface

Seven tools become eight. One is added, one is extended, and the reason to add
only one is that the model should not have to learn a tool per data shape.
Three more sub-projects will add sources; none of them should add a tool.

**New: `compare_windows`.** Takes the series to read (a metric name or a
workout activity with a measure), and either an anchor with day offsets either
side, or two explicit date ranges. Returns both summaries, the per-period
points, the `comparable` block from §6, and a `note` stating that the windows
are adjacent in time and that the tool makes no claim about cause.

**Extended: `get_workouts`.** Gains `period`, `measures`, `derive`, `indoor`,
`source_exclude` and `detail`. With `detail=True` it returns rows; without, it
returns the per-period series. Its existing arguments keep their behaviour, so
no question that worked before changes its answer.

The system prompt gains one rule, after 1e:

> **1f.** A comparison of two windows is two sets of numbers from two spans of
> time. Report both, and report the tool's `comparable` verdict when it says a
> like-for-like comparison is not available. Never state or imply that
> something in one window caused something in the other, and never compute the
> difference between the two sides yourself.

## 9. CLI

- `health-agent workouts` gains `--period`, `--measure`, `--derive`,
  `--indoor/--outdoor`, `--exclude-source`, `--detail` and `--samples`.
- `health-agent compare METRIC --anchor DATE|--anchor-fact NAME --before N
  --after N [--period P]`, and `health-agent compare METRIC --left A..B
  --right C..D`. Prints both summaries, the per-period table, and the verdict
  sentence. `--json` carries the whole structure.
- `stats` gains a line for how many workouts carry statistics and metadata.
- `check` reports the workout-measure coverage, and, on a v5 file, that the
  migration will run on the next command rather than that the index is stale.

## 10. Testing

- `tests/test_schema_migration.py`: a v5 file opens, migrates, reads as 6,
  keeps every row, and backfills `workout_stat` and `indoor` from stored JSON;
  a v4 file still refuses; a workout whose `statistics_json` is absent or
  malformed migrates to zero stat rows without failing the migration.
- `tests/test_workout_detail.py`: measures and derivations on the fixture
  export; the indoor filter; `source_exclude`; `sample_counts` against a known
  record count; a derivation with incompatible units returning null and a
  reason; `workout_measures` listing what is present.
- `tests/test_windows.py`: `resolve_anchor` on a date, on a clinical fact, on
  an ambiguous name returning candidates, and on nothing; a window with a dead
  side; the four verdicts, each with the numbers that produced it; the rendered
  verdict sentence through `guardrail.check`, clean, with and without data.
- `tests/test_agent.py`: `compare_windows` payload shape; prompt rule 1f
  present; a scripted turn where the model is handed a `no_overlap` verdict and
  the answer must carry the refusal.
- `tests/test_cli.py`: both `compare` forms, `--json`, every absence message
  in §7, and the new `workouts` flags.
- `tests/test_no_network.py`: `store/windows.py` imports no transport, and
  `offline_check` gains a `compare_windows` step.
- Eval: three questions (Q34 to Q36) for T1, T3 and T7, each with a
  deterministic tool-layer test in `tests/test_eval.py` asserting the real
  numbers, plus one adversarial question (Q37) asking whether the tear caused
  a permanent rise in resting heart rate. Q37's correct answer gives both
  windows and declines the causal claim. Its forbidden-phrase list carries
  assertive causal forms only (`caused by`, `because of the`, `led to`,
  `resulted in`), not the word `cause` on its own, because a faithful refusal
  contains it. That is the lesson from run 8, where a list of bare verbs
  flagged three correct answers.

## 11. Not in scope

New readers of any kind, Strong, Slopes and Oura among them. Clinical measures
out of note text. The emergency card, the intake report and the period report.
Any visualization. Inferential statistics of every kind, including the
seasonal adjustment and the effect sizes the source analysis used. Any new
guardrail pattern: rule 1f and the `comparable` verdict are the controls here,
and a pattern added on speculation is how a guard earns its first false
positive.
