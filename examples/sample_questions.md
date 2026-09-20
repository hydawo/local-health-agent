# Sample questions

`health-agent ask` answers in plain language using a local model. The structured
commands below it are what the model calls under the hood, and are worth knowing
because they are instant where `ask` takes 25-60 seconds.

The formal accuracy set lives in [`../tests/eval_questions.md`](../tests/eval_questions.md).

## Asking in plain language

```bash
health-agent ask "How has my LDL changed?"
```

```bash
health-agent ask "My notes mention a vitamin D result — what did the lab say?" --show-tools
```

Real answers against the fixtures, abbreviated:

> **How many steps did I take on 2026-03-02?**
> You took 6,000 steps on 2026-03-02. *Note: this value comes from the largest
> single source to avoid double-counting, as both devices recorded that day.*

> **Around the time I logged poor sleep, what was my resting heart rate?**
> Your Apple Health export does not contain resting heart rate data for
> March 9-11, the days you noted poor sleep. The only data available is from
> March 1 to March 7, 2026. Would you like me to pull that earlier week instead?

That second answer is the behavior the project cares most about: the question
reaches for data that doesn't exist, and the correct response is to say so
rather than to answer with whatever is nearby.

All examples run against the synthetic fixtures, so you can paste them as-is:

```bash
health-agent --index /tmp/demo.db ingest tests/fixtures
```

## "What's in my export?"

```bash
health-agent --index /tmp/demo.db stats
```

```bash
health-agent --index /tmp/demo.db types --search heart
```

## "How has my resting heart rate changed?"

```bash
health-agent --index /tmp/demo.db metric resting-hr --by week
```

Discrete metrics average by default. The fixture's seven samples (58, 60, 62, 59,
61, 57, 63) average to exactly 60.0.

## "How many steps did I take in March?"

```bash
health-agent --index /tmp/demo.db metric steps --from 2026-03-01 --to 2026-03-31 --by month
```

Cumulative metrics sum by default. Where two devices recorded the same day, the
output says so and reports the larger single-source total rather than adding
them.

## "Show me which device recorded what"

```bash
health-agent --index /tmp/demo.db metric steps --show-sources
```

```bash
health-agent --index /tmp/demo.db metric steps --source "Fixture Watch"
```

## "How much deep sleep did I get?"

```bash
health-agent --index /tmp/demo.db metric sleep --by day
```

Sleep is broken out by stage and totalled by duration. Note the caveat in
`tests/fixtures/README.md`: segments are attributed to the calendar date they
start on, so one night splits across two dates until session stitching lands.

## "What about data I don't have?"

```bash
health-agent --index /tmp/demo.db metric vo2max
```

Rather than "no data found", the tool names what's missing and asks whether it's
something you track — the missing-data behavior from plan §5a.

## "What workouts have I logged?"

```bash
health-agent --index /tmp/demo.db workouts --from 2026-03-01
```

## "Has my LDL come down since my last panel?"

```bash
health-agent --index /tmp/demo.db labs ldl
```

Every point cites the file, page, and collection date it came from. The two
fixture reports name the analyte differently and still merge into one trend.
Values outside the range printed on that report are marked — the tool reports
what the lab said, and does not interpret it.

## "Which lab values do I have at all?"

```bash
health-agent --index /tmp/demo.db labs
```

```bash
health-agent --index /tmp/demo.db labs --search chol
```

## "What did the report say about vitamin D?"

```bash
health-agent --index /tmp/demo.db search "vitamin d supplementation"
```

Semantic search when the chunks are embedded, keyword search otherwise — the
output always states which one it used, rather than quietly returning worse
results.

## "What reports have I added?"

```bash
health-agent --index /tmp/demo.db documents
```

## "What did I write down about my sleep that week?"

```bash
health-agent --index /tmp/demo.db search "coffee sleep" --kind note
```

Note results cite the heading they came from rather than a page number.

## "Which notes did I flag to follow up on?"

```bash
health-agent --index /tmp/demo.db notes --tag followup
```

Tags come from frontmatter (`tags: [sleep, headache]`) and from inline
`#hashtags` in the body.

## "Show me everything about vitamin D, wherever it is"

```bash
health-agent --index /tmp/demo.db search "vitamin d"
```

This is the multi-source case the project exists for: the lab report and the
note that mentions it come back in one ranked list, each citing its own source.

## "Is my setup working?"

```bash
health-agent check
```

## Machine-readable output

Every query command takes `--json`, which is the shape the agent's
`query_healthkit` tool will consume at milestone 5.

```bash
health-agent --index /tmp/demo.db metric resting-hr --by week --json
```
