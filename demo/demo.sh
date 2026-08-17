#!/usr/bin/env bash
#
# The demo, as a runnable script.
#
# Shows LOCAL mode — no flags, nothing leaves the machine — against the
# synthetic fixtures, so anyone can run it without owning an Apple Health
# export and without touching real health data.
#
#   ./demo/demo.sh          run it
#   ./demo/demo.sh --fast   skip the `ask` steps (no local model needed)
#
# `demo/demo.tape` drives this same sequence to produce the GIF.

set -euo pipefail

FAST=0
[ "${1:-}" = "--fast" ] && FAST=1

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
INDEX=$(mktemp -d)/demo.db
trap 'rm -rf "$(dirname "$INDEX")"' EXIT

HA=(health-agent --index "$INDEX")
command -v health-agent >/dev/null || HA=(python -m health_agent.cli --index "$INDEX")

say()  { printf '\n\033[1;36m# %s\033[0m\n' "$*"; }
run()  { printf '\033[1;32m$\033[0m %s\n' "$*"; "$@"; }
pause(){ sleep "${DEMO_PAUSE:-1}"; }

say "Ingest an Apple Health export, lab PDFs, and personal notes."
run "${HA[@]}" ingest "$ROOT/tests/fixtures" --no-embed
pause

say "What's in there?"
run "${HA[@]}" stats
pause

say "Aggregation follows HealthKit semantics: discrete metrics average,"
say "cumulative metrics sum. Getting that backwards gives confident nonsense."
run "${HA[@]}" metric resting-hr
pause

say "Two devices recorded steps on 03-02. Adding them would double the day,"
say "so the tool takes the largest single source and tells you why."
run "${HA[@]}" metric steps --show-sources
pause

say "Sleep is grouped by night, not calendar date — one night stays one row."
run "${HA[@]}" sleep
pause

say "Lab values trend across reports that name the analyte differently,"
say "each one citing the file and page it came from."
run "${HA[@]}" labs ldl
pause

say "The oldest report was a scan. OCR values are marked, because OCR"
say "misreads digits and a reference range is easy to get wrong."
run "${HA[@]}" labs hba1c
pause

say "Search spans records and notes, with the right citation style for each."
run "${HA[@]}" search "vitamin d" --limit 3
pause

if [ "$FAST" -eq 0 ]; then
    say "Now in plain language. This runs on a local model — no flags,"
    say "nothing leaves the machine."
    run "${HA[@]}" ask "Was my vitamin D ever below range?"
    pause

    say "And the honest answer when the data isn't there."
    run "${HA[@]}" ask "What was my resting heart rate on March 15th?"
    pause
fi

say "Finally: prove the local tier never touched the network."
run "${HA[@]}" offline-check
