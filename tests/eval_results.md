# Eval results

One row per run of `tests/run_agent_eval.py` against the 20 questions in
`eval_questions.md`. Recorded per milestone so accuracy is visible as a trend
rather than a single pass/fail — a change that trades one kind of correctness
for another should be an argument, not a surprise.

```bash
python tests/run_agent_eval.py --index ~/HealthData/.index/health.db --out /tmp/run.md
```

| Run | Milestone | Model | Number | Cited | Source named | Gap named | No diagnosis | Tools | Clean | Wall |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | M6 (first) | qwen3.6:27b | 17/20 | 15/20 | — | 19/20 | 20/20 | 20/20 | 13/20 | 19.2 min |
| 2 | M6 (after fixes) | qwen3.6:27b | **20/20** | **19/20** | 16/20 | **20/20** | **20/20** | **20/20** | **19/20** | 22.2 min |
| 3 | M10 (release) | qwen3.6:27b | **20/20** | 18/20 | 16/20 | **20/20** | **20/20** | **20/20** | 18/20 | 18.9 min |

Thinking mode off for all three. "Clean" means all five checks passed. The
guardrail fired on 0/20 in runs 2 and 3 — worth stating explicitly, because a
high no-diagnosis score would mean much less if the guard were producing it.

## Run 1 — what it caught

Two defects that 239 unit tests did not, because both were failures of the tool
*contract* rather than of any function in it. The agent answered correctly given
what it could see; it could not see enough.

**Sleep was attributed to calendar dates, not nights.** Asked for deep sleep on
the night of 2026-03-04, the agent said the data "does not include a breakdown of
sleep stages such as deep, light, or REM" — confidently wrong. The stages exist;
they start after midnight and land on 03-05. Sleep now groups by night on a noon
boundary. This had been recorded as a known caveat since milestone 1, and it took
a natural-language question to show it was a defect rather than a footnote.

**Workouts were unreachable.** "What workouts have I logged?" returned "I don't
see any workouts" while the index held a run. The three tools in plan §4 cover
metrics, labs and text; workouts have their own table and their own shape. Added
`get_workouts` — §4 budgets for 4-5 tools.

## Run 1 — where the scorer was wrong

Recorded because a scorer that flatters itself is worse than no scorer.

- **Q17 scored two failures and was one of the best answers in the run.** It said
  "resting heart rate data only from March 1-7... no resting heart rate recorded
  for March 9-10, the dates when you logged the poor sleep." The number check
  wanted the literal "march 7" where the text reads "March 1-7", and the gap
  patterns did not allow words between "no" and "recorded".
- **The citation check was stricter than the plan.** §1 asks which *file or date*
  a claim came from; the check demanded a filename, so correctly-dated HealthKit
  answers failed. Now split into two columns: `cited` (file or date) and the
  stricter `source named` (a file). Both are reported, so neither hides the other.

## Run 2 — the remaining miss

**Q13** cites its source as `"Sleep log, week of March 9 > What I changed",
2026-03-11` — the note's title, the section within it, and the date, but not the
filename `2026-03-11-sleep-log.md`. That is enough for a person to find the
passage, and it fails the strict `source named` check.

Deliberately not chased further. Tightening the prompt until this specific
phrasing passes would be tuning the model to the scorer rather than improving the
answer, and `source named` at 16/20 is the honest number to carry forward.

## Run 3 — the release run

Re-run before tagging, with no changes to prompts, tools, or the guardrail since
run 2. Every substantive check held: 20/20 correct numbers, 20/20 gaps named,
20/20 no diagnosis, 20/20 expected tools, and the guardrail never fired.

The two citation misses are **the same failure mode run 2 documented**, not a new
one — and `source named` landed on 16/20 for the second run running, which is the
more useful signal than `cited` moving 19 → 18.

- **Q14** ("which notes did I flag to follow up on?") cites
  `[Sleep log, week of March 9, 2026-03-11]`. The tool handed it
  `2026-03-11-sleep-log.md, ...`; the model substituted the note's frontmatter
  title for the filename it was given.
- **Q20** ("what do I know about my cholesterol overall?") tabulates three panels
  by date without naming the PDFs — while correctly flagging that the earliest
  row came through OCR and should be checked against the original. A strong
  answer that fails the strict check.

Both remain deliberately unchased, for the reason recorded under run 2: the
filenames *are* in the tool output, so this is the model preferring prose to a
locator, and prompting until this exact phrasing passes would be fitting the
model to the scorer. A reader can still find every passage — the date and title
are right there — which is why it costs `source named` and nothing else.

Note also that `cited` and `source named` are the same check for the eleven
document-backed questions (`needs_source=True`), so a lab answer that skips the
filename loses both columns at once. That is why `cited` can move while the
underlying behavior has not.

## Notes on variance

The same question can take very different paths between runs: Q17 used 2 tool
calls and 75s in run 1, and 4 calls and 179s in run 2, passing both times.
Timings here are indicative, not benchmarks. Score movements of one question are
noise; the checks are there to catch systematic regressions.
