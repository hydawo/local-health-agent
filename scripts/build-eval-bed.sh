#!/usr/bin/env bash
#
# Builds the bed that `tests/run_agent_eval.py` is scored against, so a row in
# `tests/eval_results.md` can be reproduced instead of described.
#
# Runs 5 onward were all scored against the same bed: the committed fixtures
# plus the published `sample` literature pack, with the window questions in
# their own index. That recipe only existed in prose, spread across four run
# narratives, and the index itself is never committed (it is health data by
# the time anyone points this at a real export, and .gitignore and the
# pre-commit hook both refuse it). So anyone reproducing a row had to
# re-derive the build from the narratives. This is that build.
#
# Usage:
#   ./scripts/build-eval-bed.sh [--yes] [--rebuild] <bed-dir>
#
#   --yes      confirm the one-time literature-pack download (see below)
#   --rebuild  discard an existing index in <bed-dir> and parse from scratch
#
# The pack download is the only step that touches the network, and the CLI
# gates it behind a consent notice it shows once. This script does not grant
# that consent for you: without --yes it stops at that step and prints the
# command to run. Everything before it is local.

set -euo pipefail

yes_flag=""
rebuild=0
bed=""

while [ $# -gt 0 ]; do
    case "$1" in
        --yes)     yes_flag="--yes"; shift ;;
        --rebuild) rebuild=1; shift ;;
        -h|--help)
            sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        -*)
            echo "Unknown option: $1" >&2
            exit 2 ;;
        *)
            [ -n "$bed" ] && { echo "Give one <bed-dir>, not two." >&2; exit 2; }
            bed="$1"; shift ;;
    esac
done

[ -n "$bed" ] || { echo "Usage: $0 [--yes] [--rebuild] <bed-dir>" >&2; exit 2; }

root=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo "Not inside a git repository." >&2
    exit 1
}

# Resolve the target before creating anything: the guard below has to run
# first, or refusing to build inside the real data folder still leaves a new
# directory sitting in it.
bed=$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$bed")

# Never build a bed on top of a real data folder. The fixtures would land
# beside someone's own export and the eval would score against the mixture.
real=${HEALTH_AGENT_DATA_DIR:-$HOME/HealthData}
real=$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$real")
case "$bed" in
    "$real"|"$real"/*)
        echo "Refusing to build the bed inside the real data folder ($real)." >&2
        echo "Pick a scratch directory instead." >&2
        exit 1 ;;
esac

mkdir -p "$bed"

index="$bed/.index/health.db"
window="$bed/window-eval.db"

if [ -e "$index" ] && [ "$rebuild" -eq 0 ]; then
    echo "An index already exists at $index."
    echo "Re-run with --rebuild to discard it, or point at an empty directory."
    exit 1
fi

if [ "$rebuild" -eq 1 ]; then
    # Keep literature.db, literature_vectors/, packs/ and the consent record:
    # the pack is 2,000 articles over the network, and nothing in a rebuild of
    # the HealthKit side changes it. A rebuild is therefore local.
    rm -f "$index" "$index-shm" "$index-wal" "$window"
    rm -rf "$bed/vectors"
fi

cd "$root"
agent() { python3 -m health_agent.cli --data-dir "$bed" "$@"; }

echo "=== 1/3  fixtures -> $index"
# One call covers all of it: export.xml, the clinical-records/ folder beside
# it, records/ and notes/. (Ingesting export.xml separately afterwards is a
# no-op, which is worth saying because the output of the folder form lists the
# PDFs and notes loudly and the clinical block quietly.)
agent ingest tests/fixtures

echo
echo "=== 2/3  literature: sample pack"
# The catalog prints "sample  installed 2026.09, ..." or "sample  not
# installed"; anchoring on the word straight after the name tells them apart.
if agent literature packs 2>/dev/null | grep -Eq '^sample[[:space:]]+installed'; then
    echo "already installed; skipping the download"
elif [ -z "$yes_flag" ]; then
    cat >&2 <<MSG

The remaining step downloads one pack file from github.com. The CLI shows the
full notice and asks once; this script will not answer for you. Either run:

    python3 -m health_agent.cli --data-dir "$bed" literature install sample

in a terminal and answer it, or re-run this script with --yes once you have
read the notice. Then build the window index:

    python3 tests/window_fixture.py "$window"
MSG
    exit 1
else
    agent literature install sample $yes_flag
fi

echo
echo "=== 3/3  window index -> $window"
rm -f "$window"
python3 tests/window_fixture.py "$window"

echo
echo "=== verifying"
python3 scripts/verify_eval_bed.py "$bed"

cat <<MSG

Bed ready. Score a run against it with:

    python3 tests/run_agent_eval.py \\
        --index "$index" \\
        --window-index "$window" \\
        --model qwen3.6:27b --repeat 3 --out run.md --capture run.json

Drop --repeat for a single draw, which is what every row in
tests/eval_results.md before run 11 is.
MSG
