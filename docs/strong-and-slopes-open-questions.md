# Strong and Slopes: the questions a spec needs answered

The event-windows design listed both as out of scope, and nothing has been
written down since. This is the same treatment the Oura memo got: the
questions whose answers change what gets built, each with options, the trade
each option makes, and a recommendation. Written 2026-10-31, unbuilt, nothing
here is a commitment.

One thing to settle before any of it: **the export formats here are described
from memory and have to be confirmed against a real file first.** A real
export is never committed, and nothing in this memo is a reason to skip that
check. If a column name below turns out wrong, the question it sits under is
still the question.

---

## 1. Is a Strong export data or is it a note?

Strong exports a CSV, one row per set: the workout's date and name, the
exercise, the set's order, weight, reps, and whatever notes were typed. That
is structured data about an event, which makes it unlike every reader the
drop folder has: markdown, text, docx and photos all become searchable prose.

- **a. Data.** Rows land in the workout tables, so `get_workouts`,
  `compare_windows` and the CLI all answer about them with no new surface.
  *Trade:* the schema grows a concept it does not have, the set, and every
  existing workout answer has to keep meaning what it meant.
- **b. Prose.** The CSV is rendered to text and ingested as a note, which is
  one function and no schema change at all.
  *Trade:* nothing can be trended, compared or aggregated, and the agent can
  only quote it back. A reader that cannot answer "how has my squat moved"
  has missed the point of reading it.
- **c. Both.** Rows as data, and the typed notes as note text.
  *Trade:* two ingest paths over one file, and a citation that has to say
  which half it came from.

**Recommend a**, with the typed notes carried as a field on the row rather
than as a second document. (c) is (a) plus a second path for text nobody has
asked a question about yet.

---

## 2. Where does a set live, given the index's unit is a workout?

Only if question 1 is a or c. `workout` holds one row per session with
aggregate statistics, and `workout_stats` owns the measure-key grammar.

- **a. A new `workout_set` table**, keyed to a workout row, holding exercise,
  order, weight, reps and unit.
  *Trade:* a new read path and a new tool argument before anything can ask
  about it, which is the `activity_summary` shape and the reason that table
  is still hard to reach.
- **b. Sets collapsed to per-exercise aggregates on the existing workout
  row**: heaviest set, total volume, set count per exercise.
  *Trade:* "what did I actually lift on the third set" becomes unanswerable
  from the index, and the aggregate choice is a claim about what matters.
- **c. Both: the table, and the aggregates computed from it.**
  *Trade:* the most work, and the aggregates have exactly one owner, which
  is the rule the measure-key grammar had to learn twice.

**Recommend c**, built in that order, with the aggregates derived rather than
stored twice. The one thing to avoid is (b) alone: a derived figure whose
source was discarded cannot be checked, and this project's lab path exists
because a figure you cannot trace is a figure you cannot trust.

---

## 3. Strong also writes to Apple Health. What happens to the same session
twice?

A strength session logged in Strong appears in the Apple Health export as a
workout, and in the CSV as its sets. Both are the same event.

- **a. Treat the CSV as the detail and the HealthKit workout as the
  session.** Match on start time within a tolerance and attach the sets to
  the existing workout row.
  *Trade:* a matching rule, and a tolerance that will be wrong for some
  session whose clocks disagree.
- **b. Keep them separate, both reported.** A question about workouts gets
  two answers for one session, with their sources named.
  *Trade:* every count doubles for anyone who has both, which reads as data
  loss in the other direction.
- **c. CSV only where HealthKit has nothing, by date.**
  *Trade:* the simplest rule that is never wrong in the obvious way, and it
  silently drops set detail for every session HealthKit already holds, which
  is most of them.

**Recommend a**, and report the match in the payload so an answer can say the
sets came from Strong and the session from Apple Health. This is the same
question as Oura's device overlap and should get a consistent answer: two
kinds of measurement are not one series, but two records of one event are one
event.

---

## 4. Does Slopes need a reader at all?

Slopes syncs its sessions to Apple Health, so a ski day already arrives as a
workout with its distance and duration. What the export adds is run-level
detail: individual runs, vertical, speed.

- **a. No reader.** Say so in the docs and spend the work on Strong.
  *Trade:* "how many runs did I do" stays unanswerable, and the project has
  one fewer reader than its README suggests it might.
- **b. GPX only.** Slopes exports GPX, and a GPX reader is reusable for every
  other app that exports one, which is most of them.
  *Trade:* a track is points, and turning points into runs is inference,
  which this project does not do. Without that inference the GPX adds a
  polyline nothing can ask about.
- **c. The run-level export**, whatever the app's own format turns out to be.
  *Trade:* one vendor's format for one sport, and the question it answers is
  narrow.

**Recommend a** for now, and revisit only if a question needs it. This is the
scope rule the Oura memo also applies: schema with no reader-facing question
behind it is cost without a reader. The honest version of this answer is in
the docs rather than in the code.

---

## 5. What question does any of this let a person ask that they cannot ask
today?

The test every piece of this has to pass, and the reason question 4 answers
itself. For Strong the candidates are:

- how a lift has moved over months, which is `compare_windows` over a measure
  that does not exist yet,
- what the heaviest set of an exercise was, and when,
- whether training volume dropped around a dated event, which is the ACL
  shape the event-windows work was built for and the one real argument for
  this reader.

If a spec is written, it should name the eval questions it adds at the same
time, because a reader whose answers nothing measures is how the clinical
records path nearly shipped with absence reading as coverage.

---

## 6. What is the fixture?

Synthetic only, no real export value in any test, fixture, doc or commit
message. A hand-written CSV of two or three sessions, built from the column
names a real export is confirmed to use, with the format's provenance in the
fixture README so a parse failure against a real file reads as a format gap
rather than a mystery. This is the same answer the Oura memo reaches, for the
same reason.

---

## Order of work, once the above are answered

1. Confirm the Strong export's columns against a real file, which is not
   committed.
2. The synthetic fixture and its README.
3. The `workout_set` table and the reader, with the overlap rule from
   question 3.
4. The derived aggregates, with one owner.
5. The eval questions from question 5, before the reader is called done.
