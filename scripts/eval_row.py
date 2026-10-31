#!/usr/bin/env python3
"""Turn a `--capture` file into the row `tests/eval_results.md` wants.

Every row in that table was transcribed by hand from a run's summary lines,
and the table is the project's record of whether accuracy is moving. A
mistranscribed cell is indistinguishable from a regression, and run 11's
narrative had to be corrected twice for exactly that kind of slip. This reads
the capture file instead, and computes each cell the way `render` does.

    python3 scripts/eval_row.py /tmp/run.json --label "What changed"

It prints the markdown row and, below it, the figures the row cannot hold:
how many cells of each column were actually measured, the questions that
varied between draws, and the ones that were scored on a different set of
checks. Those belong in the run's narrative section rather than in a cell.

No model, no index, no network. The scorer is today's, so a row printed from
an old capture file is a re-score and the narrative should say so.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from run_agent_eval import rescore  # noqa: E402

# The table's columns, in the order it prints them. `Clean` is every scored
# column holding at once, which is what the overall pass rate means.
COLUMNS = (("Number", "number"), ("Cited", "cited"),
           ("Source named", "source_named"), ("Gap named", "gap"),
           ("No diagnosis", "no_diag"), ("Tools", "tools"))
CLEAN = ("number", "cited", "gap", "no_diag", "tools", "context", "verdict",
         "pmid")


def _questions(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row["id"], []).append(row)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture")
    parser.add_argument("--label", default="",
                        help="the Milestone cell, e.g. \"after the citation "
                             "fix\"")
    parser.add_argument("--run", default="?", help="the run number")
    args = parser.parse_args()

    captured = json.loads(Path(args.capture).read_text())
    meta = captured.get("run") or {}
    rows = rescore(captured["draws"])
    groups = _questions(rows)
    repeat = meta.get("repeat", 1)

    cells = []
    for _, key in COLUMNS:
        # A cell counts questions that held on every draw, which is what the
        # run 11 row means and what its own note says.
        held = sum(1 for draws in groups.values() if all(d[key] for d in draws))
        cells.append(f"{held}/{len(groups)}")
    clean = sum(1 for draws in groups.values()
                if all(all(d[k] for k in CLEAN) for d in draws))
    minutes = (meta.get("elapsed_sec") or 0.0) / 60

    print(f"| {args.run} | {args.label} | {meta.get('model', '?')} | "
          + " | ".join(cells)
          + f" | {clean}/{len(groups)} | {minutes:.1f} min |")
    print()
    print(f"- draws per question: {repeat}")
    for label, key in (("number", "number"), ("tools", "tools"),
                       ("gap", "gap"), ("pmid", "pmid")):
        scored = [r for r in rows if r.get(f"{key}_scored")]
        blank = sorted({r["id"] for r in rows if not r.get(f"{key}_scored")})
        print(f"- {label} measured on {len(scored)}/{len(rows)} draws"
              + (f"; nothing to check on {', '.join(blank)}" if blank else ""))
    varied = sorted(qid for qid, draws in groups.items()
                    if any(len({d[k] for d in draws}) > 1 for k in CLEAN))
    print(f"- varied between draws: {len(varied)}/{len(groups)}"
          + (f" ({', '.join(varied)})" if varied else ""))
    fired = sorted({r["id"] for r in rows if r.get("guardrail_flags")})
    print(f"- guardrail fired on: {', '.join(fired) if fired else 'nothing'}")
    invented = {r["id"]: r["pmid_unreturned"] for r in rows
                if r.get("pmid_unreturned")}
    if invented:
        print(f"- cited a PMID the tool did not return: {invented}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
