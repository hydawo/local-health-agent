# Oura: the questions a spec needs answered

ROADMAP §8 settled the one question that had this sub-project parked: build
both paths, an export-file reader and an API path, and let the person choose.
That decision does not answer the six below, and each changes what gets built.
Written 2026-10-31, unbuilt, nothing here is a commitment.

Each question has options with the trade each one makes, and a recommendation.
Pick a letter per question and the spec writes itself.

---

## 1. Which Oura figures are in scope for v1?

- **a. Parity only.** Sleep, resting heart rate, HRV. Every one has a
  HealthKit identifier already in `metrics.py`, so nothing new is registered
  and every existing tool answers about them the day the reader lands.
  *Trade:* leaves out the figures people actually buy an Oura for.
- **b. Parity plus Oura's own scores.** The above, plus readiness and sleep
  score, which have no HealthKit type and need a vendor namespace (question 3).
  *Trade:* a new class of metric key, and the registry grows a second kind of
  entry.
- **c. Everything the export carries.** Also temperature deviation, SpO2,
  respiratory rate, activity.
  *Trade:* most of it is unreachable by any existing question, so it is
  schema and tests with no reader-facing use yet.

**Recommend a** for v1, with b as the first follow-up. The reader is the risky
part; proving it against figures every tool already understands keeps the
first landing small.

---

## 2. How do Oura and Apple Watch figures of the same metric coexist?

Both record resting heart rate and HRV daily. `source_name` is already a
dimension on `record` and `daily_metric`, and `multi_source_warning` already
fires when a period holds more than one source.

- **a. Another source, nothing special.** Oura rows carry
  `source_name = "Oura"` and the existing machinery reports both.
  *Trade:* for additive metrics the combine rule takes the largest single
  source rather than summing, which is right; for averaged ones the two
  devices' values are averaged together, which is the behaviour a reader
  would have to notice in the warning.
- **b. Another source, never combined.** As above, but a period holding two
  sources for an Oura-eligible metric refuses a single figure and reports both,
  the way mixed units already refuse.
  *Trade:* more refusals a person has to read; the honest answer when two
  devices disagree by 6 bpm.
- **c. Separate namespace.** Oura's resting HR is a different metric key from
  Apple's, never merged, and a question about "my resting heart rate" has to
  choose.
  *Trade:* every existing question about resting HR silently keeps answering
  from Apple only, which is a wrong answer by omission.

**Recommend b.** It is the rule this project already applies to units and to
clinic-versus-watch vitals: two kinds of measurement are not one series. (a)
is the cheapest and would let an answer average two devices into a number
neither recorded.

---

## 3. Where do Oura-only scores live, if they are in scope?

Only if question 1 is b or c.

- **a. Vendor-prefixed metric keys** (`oura:readiness`), registered in
  `metrics.py` with their own `Kind`.
  *Trade:* every tool sees them for free; the registry stops being a mirror of
  HealthKit.
- **b. A separate table**, the way `activity_summary` keeps Apple's ring
  totals out of `daily_metric`.
  *Trade:* a new read path and a new tool argument before anything can ask.

**Recommend a.** `activity_summary` is separate because it is Apple's own
pre-aggregation of data the index also holds raw; a readiness score is not a
rollup of anything here.

---

## 4. The API path's token: where does it live?

- **a. macOS keychain**, via `security`.
  *Trade:* no new dependency, no secret in a file, and it is one more platform
  assumption in a codebase that has few.
- **b. The existing config file**, with file permissions checked on read.
  *Trade:* simplest, and a plaintext secret on disk, which `THREAT_MODEL.md`
  would have to say out loud.
- **c. An environment variable only**, never persisted.
  *Trade:* nothing to leak and nothing to steal; the person pastes it every
  session, so in practice it ends up in a shell profile, which is (b) without
  the permission check.

**Recommend a**, falling back to (c) with an explicit message where the
keychain is unavailable. (b) is the one that quietly makes the threat model
worse.

---

## 5. What does the API path do to the threat model?

This is bookkeeping rather than a choice, and it is the part most likely to be
forgotten, so it is written down here. Claim 3 says **exactly three modules
can reach the network**, with a test that asserts the exact set and counts
vendor SDK imports. An Oura client makes it four. The work is:

- the new module in the Claim 3 table, with its destination,
- `test_the_network_surface_is_exactly_three_modules` renamed and its expected
  set extended,
- a one-time consent notice in the consent flow, matching the literature one,
  recorded so it is asked once,
- `tests/test_no_network.py` extended so the query path still cannot reach the
  new fetcher.

The file reader needs none of this, which is the whole reason both paths exist.

---

## 6. What is the fixture, given no real export may be committed?

Both paths need one, and the rule is absolute: synthetic only, no real export
value in any test, fixture, doc or commit message.

- **a. A hand-written synthetic export**, a few days of each figure, shaped
  from Oura's published export format.
  *Trade:* has to be built from documentation rather than from a real file, so
  a format detail can be wrong in a way no test here would catch.
- **b. A recorded-then-scrubbed export**, structure from a real file with
  every value replaced.
  *Trade:* the structure is certainly right, and the scrubbing is a step that
  has to be perfect every time, on a file that must never be committed in its
  original form.

**Recommend a**, and say in the fixture README that the format came from
documentation and which version of it, so a parse failure against a real
export reads as a format gap rather than a mystery.

---

## Order of work, once the above are answered

1. The synthetic fixture and its README.
2. The file reader, scored against the fixture, with the source-collision rule
   from question 2.
3. The threat-model and consent work from question 5.
4. The API client, which by then is a different transport onto a reader that
   already works.
