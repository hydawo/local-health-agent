"""Literature context on out-of-range labs (ROADMAP #4), surfaced by the
tool, not by the model.

The roadmap asked for a decision on synthesis before this was built: a
model that paraphrases three findings into a paragraph can produce advice
no clause states. The decision is that the model never sees the findings.
This module runs after the answer text is final, reads the lab results the
model already used, asks the corpus for citable papers on each flagged
analyte, and renders a fixed block. Nothing here enters the transcript, so
there is nothing for the model to synthesize, and the block can be held to
the guardrail as a test oracle the way the visit-prep sheet is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..literature import store

MAX_ANALYTES = 3
PER_ANALYTE = 2
# `hits` is asked for this many so the citable filter has something left.
HITS = 5

HEADER = (
    "---\n"
    "Published research on your out-of-range results. These are findings about\n"
    "populations, found by the tool for the analyte's name, not chosen by the\n"
    "model and not about your result.\n"
)
NONE_FOUND = "no citable finding in the installed packs."


@dataclass
class AnalyteContext:
    analyte: str
    label: str
    value: float | None
    unit: str | None
    date: str | None
    flag: str | None
    citation: str | None
    findings: list[dict] = field(default_factory=list)


def _from_batch_row(row: dict) -> dict:
    return {"analyte": row.get("analyte"), "label": row.get("label"),
            "value": row.get("value"), "unit": row.get("unit"),
            "date": row.get("collected_date"), "flag": row.get("flag"),
            "citation": row.get("citation")}


def flagged_analytes(steps) -> list[dict]:
    """The analytes whose latest result the lab tool flagged this turn, in
    tool order, one entry each, at most MAX_ANALYTES. Empty when the model
    searched the literature itself: the block would duplicate its answer."""
    if any(s.name == "search_medical_literature" for s in steps):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for step in steps:
        if step.name != "get_lab_trend" or not isinstance(step.result, dict):
            continue
        result = step.result
        if "error" in result:
            continue
        rows: list[dict] = []
        if "out_of_range_on_latest_report" in result:
            rows = [_from_batch_row(r) for r in result["out_of_range_on_latest_report"]]
        elif result.get("points"):
            last = result["points"][-1]
            if last.get("out_of_range") or last.get("flag"):
                rows = [{"analyte": result.get("analyte"), "label": result.get("label"),
                         "value": last.get("value"), "unit": last.get("unit"),
                         "date": last.get("collected_date"), "flag": last.get("flag"),
                         "citation": last.get("citation")}]
        for row in rows:
            key = row["analyte"]
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(row)
            if len(out) == MAX_ANALYTES:
                return out
    return out


def lab_context(steps, literature_conn, *, vector_path=None,
                embedder_factory=None) -> list[AnalyteContext]:
    """Up to PER_ANALYTE citable findings per flagged analyte, best tier
    first. An empty list when there is no corpus or nothing was flagged."""
    if literature_conn is None:
        return []
    rows = flagged_analytes(steps)
    if not rows:
        return []
    once = store.once_embedder(embedder_factory)
    out: list[AnalyteContext] = []
    for row in rows:
        found = store.hits(literature_conn, row["label"], limit=HITS,
                           vector_path=vector_path, embedder_factory=once.factory)
        good = sorted((f for f in found if store.citable(f)),
                      key=lambda f: (f.evidence_rank is None, f.evidence_rank or 0))
        out.append(AnalyteContext(
            analyte=row["analyte"], label=row["label"], value=row["value"],
            unit=row["unit"], date=row["date"], flag=row["flag"],
            citation=row["citation"],
            findings=[{"title": f.title, "year": f.year, "tier": f.evidence_tier,
                       "pmid": f.pmid} for f in good[:PER_ANALYTE]]))
    return out


def _num(v) -> str:
    if v is None:
        return "?"
    text = f"{v:.10f}".rstrip("0").rstrip(".")
    return text or "0"


def _finding_text(f: dict) -> str:
    year = f"{f['year']}, " if f.get("year") else ""
    return f"*{f['title']}* ({year}{store.tier_label(f['tier'])}, PMID {f['pmid']})"


def render(contexts: list[AnalyteContext]) -> str:
    if not contexts:
        return ""
    lines = [HEADER]
    for c in contexts:
        unit = "" if not c.unit else ("%" if c.unit == "%" else f" {c.unit}")
        head = f"- **{c.label}**, {_num(c.value)}{unit} on {c.date or 'an undated report'}"
        if c.flag:
            head += f", flagged {c.flag}"
        lines.append(head + ":")
        if not c.findings:
            lines.append(f"  {NONE_FOUND}")
        elif len(c.findings) == 1:
            # A single line, attributed in the same sentence rather than a
            # list: a quoted title can itself read as a clinical claim (a
            # study titled "An A1c above 6.5% is considered diabetic..."),
            # and the guard's per-sentence check only sees this one line, so
            # the attribution has to live here rather than in the framing
            # paragraph above, which is a separate line the guard cannot
            # see from this one.
            lines.append(f"  According to the corpus, {_finding_text(c.findings[0])}.")
        else:
            parts = [f"  {_finding_text(f)}" for f in c.findings]
            lines.append(";\n".join(parts) + ".")
    return "\n".join(lines) + "\n"
