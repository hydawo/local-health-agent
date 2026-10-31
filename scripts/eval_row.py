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

from run_agent_eval import CLEAN_KEYS, clean_checks_measured  # noqa: E402
from run_agent_eval import _commit as rescored_by  # noqa: E402
from run_agent_eval import rescore  # noqa: E402

# The table's columns, in the order it prints them. `Clean` is every scored
# column holding at once, which is what the overall pass rate means.
COLUMNS = (("Number", "number"), ("Cited", "cited"),
           ("Source named", "source_named"), ("Gap named", "gap"),
           ("No diagnosis", "no_diag"), ("Tools", "tools"))
CLEAN = CLEAN_KEYS


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
    # Provenance, because a row printed from an old capture is a re-score and
    # the two commits are what say so.
    answered_at = meta.get("commit") or "not recorded"
    print(f"- answers produced at: {answered_at}; scored by: "
          f"{rescored_by() or 'unknown'}")
    for label, key in (("number", "number"), ("tools", "tools"),
                       ("gap", "gap"), ("pmid", "pmid")):
        # The gap column's denominator is draws of a gap question. Counted
        # over every draw it would list every other question as having
        # nothing to check, which is true and says nothing.
        over = [r for r in rows if r.get("gap_case")] if key == "gap" else rows
        scored = [r for r in over if r.get(f"{key}_scored")]
        blank = sorted({r["id"] for r in over if not r.get(f"{key}_scored")})
        print(f"- {label} measured on {len(scored)}/{len(over)} draws"
              + (f"; nothing to check on {', '.join(blank)}" if blank else ""))
    # What the clean cell is a conjunction of, per question that earned it.
    # A check with nothing to check passes, so a clean question measured on
    # four of the eight is reporting eligibility as much as accuracy.
    spread: dict[int, int] = {}
    for draws in groups.values():
        if all(all(d[k] for k in CLEAN) for d in draws):
            n = clean_checks_measured(draws)
            spread[n] = spread.get(n, 0) + 1
    if spread:
        print("- clean questions, by how many of the "
              f"{len(CLEAN)} checks were measured: "
              + ", ".join(f"{n} of {len(CLEAN)} on {c}"
                          for n, c in sorted(spread.items())))
    varied = sorted(qid for qid, draws in groups.items()
                    if any(len({d[k] for d in draws}) > 1 for k in CLEAN))
    print(f"- varied between draws: {len(varied)}/{len(groups)}"
          + (f" ({', '.join(varied)})" if varied else ""))
    # The cell counts every question, and 14 of them have no file in
    # existence to name, so the figure that means something goes beside it.
    document = [r for r in rows if r.get("needs_source")]
    if document:
        named = sum(1 for r in document if r["source_named"])
        doc_q = {r["id"] for r in document}
        held = sum(1 for qid in doc_q
                   if all(r["source_named"] for r in document
                          if r["id"] == qid))
        print(f"- named a source file, of the questions that have one: "
              f"{named}/{len(document)} draws, {held}/{len(doc_q)} questions")
    uncitable = sorted({r["id"] for r in rows if r.get("cited_uncitable")})
    print("- cited failed with no locator in the payloads: "
          + (", ".join(uncitable) if uncitable else "nothing"))
    echoed = sorted({r["id"] for r in rows if r.get("cited_echo")})
    print("- cited on nothing but the date the question supplied: "
          + (", ".join(echoed) if echoed else "nothing"))
    ruled = [r for r in rows if r.get("block_due") is not None]
    if ruled:
        held = sum(1 for r in ruled if r["context_unruled"] is None)
        print(f"- the lab literature block followed its own rule on "
              f"{held}/{len(ruled)} draws that called the lab tool")
        for state, label in ((True, "arrived where the rule forbids one"),
                             (False, "was due and did not arrive")):
            bad = sorted({r["id"] for r in ruled
                          if r["context_unruled"] is state})
            if bad:
                print(f"    - a block {label}: {', '.join(bad)}")
    diag = [r for r in rows if not r["no_diag"]]
    if diag:
        names = ", ".join(sorted({r["id"] for r in diag}))
        attributed = sum(1 for r in diag if r.get("diag_attributed"))
        print(f"- no diagnosis failed on {len(diag)} draws ({names}), of "
              f"which {attributed} cited a record in the same sentence")
        for pattern in sorted({p for r in diag for p in r.get("diagnostic_hits", ())}):
            print(f"    - {pattern}")
    fired = sorted({r["id"] for r in rows if r.get("guardrail_flags")})
    print(f"- guardrail fired on: {', '.join(fired) if fired else 'nothing'}")
    invented = {r["id"]: r["pmid_unreturned"] for r in rows
                if r.get("pmid_unreturned")}
    if invented:
        print(f"- cited a PMID the tool did not return: {invented}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
