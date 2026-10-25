"""Check that an eval bed holds what the recorded runs were scored against.

`scripts/build-eval-bed.sh` calls this as its last step. A bed that is missing
a piece still answers every question in `tests/eval_questions.md`. It answers
them from less data, and the run looks like a model regression rather than a
build mistake. Run 8's clinical block and run 5's literature corpus are both
silent in that way: no error, just thinner answers and a column that drops a
few points. So the build asserts its own contents instead of trusting that the
commands it ran did what they print.

Usage: python3 scripts/verify_eval_bed.py <bed-dir>

Every expected number here is a count of synthetic fixture rows, which is why
it can be written down at all.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SCHEMA_VERSION = 6

# tests/fixtures, as ingested by `health-agent ingest tests/fixtures`.
MAIN_COUNTS = {
    "record": 25,
    "workout": 1,
    "activity_summary": 2,
    "lab_result": 48,
    "clinical_fact": 5,
    "clinical_vital": 3,
    "chunk": 19,
}
MAIN_DOCUMENTS = {"fhir": 13, "note": 5, "pdf": 3}

# What the one image-only page (records/scan_2025-03-04.pdf) contributes, and
# therefore what is missing from a bed built where OCR was unavailable. Split
# out because the unit suite ingests with --no-ocr and checks these numbers
# against the fixtures; see tests/test_eval.py. An OCR-less bed is a real
# failure mode, not a variant: `ingest` warns and carries on, so the bed looks
# built and six lab values are simply absent.
OCR_ONLY = {"lab_result": 6, "chunk": 1}

# tests/window_fixture.py, the separate index behind Q34 to Q37.
WINDOW_COUNTS = {"record": 75, "workout": 21, "daily_metric": 75}

LITERATURE_ARTICLES = 2000


def _count(conn, table: str) -> int:
    return conn.execute(f"select count(*) from {table}").fetchone()[0]


def _check(problems: list[str], label: str, got, want) -> None:
    if got != want:
        problems.append(f"{label}: {got!r}, expected {want!r}")


def verify(bed: Path) -> list[str]:
    problems: list[str] = []
    index = bed / ".index" / "health.db"
    window = bed / "window-eval.db"
    literature = bed / ".index" / "literature.db"

    for path in (index, window, literature):
        if not path.exists():
            problems.append(f"missing: {path}")
    if problems:
        return problems

    with sqlite3.connect(f"file:{index}?mode=ro", uri=True) as conn:
        version = conn.execute(
            "select value from schema_meta where key = 'schema_version'"
        ).fetchone()
        _check(problems, "schema_version", int(version[0]) if version else None,
               SCHEMA_VERSION)
        for table, want in MAIN_COUNTS.items():
            _check(problems, f"{table} rows", _count(conn, table), want)
        kinds = dict(conn.execute(
            "select kind, count(*) from document group by kind"))
        _check(problems, "document kinds", kinds, MAIN_DOCUMENTS)

        # An unembedded chunk is the quiet one: search still runs, over the
        # keyword index alone, and only the questions that need a semantic
        # hit get worse.
        backends = dict(conn.execute(
            "select coalesce(embedded_with, '(none)'), count(*) "
            "from chunk group by 1"))
        if "(none)" in backends:
            problems.append(f"{backends['(none)']} chunk(s) not embedded")
        elif list(backends) != ["ollama:nomic-embed-text"]:
            problems.append(
                "chunks embedded with "
                f"{', '.join(sorted(backends))}; every recorded run used "
                "ollama:nomic-embed-text, so scores are not comparable")

        folder = conn.execute(
            "select value from schema_meta where key = 'clinical_ingest'"
        ).fetchone()
        if not folder or '"folder_seen": true' not in folder[0]:
            problems.append("the clinical-records folder was never read")

    with sqlite3.connect(f"file:{window}?mode=ro", uri=True) as conn:
        for table, want in WINDOW_COUNTS.items():
            _check(problems, f"window {table} rows", _count(conn, table), want)

    with sqlite3.connect(f"file:{literature}?mode=ro", uri=True) as conn:
        tables = {row[0] for row in conn.execute(
            "select name from sqlite_master where type = 'table'")}
        table = "article" if "article" in tables else None
        if table is None:
            problems.append("literature.db holds no article table")
        else:
            _check(problems, "literature articles", _count(conn, table),
                   LITERATURE_ARTICLES)

    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    bed = Path(argv[1]).expanduser()
    problems = verify(bed)
    if problems:
        print(f"This bed does not match the recorded runs ({bed}):",
              file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("\nA run scored against it is not comparable with any row in "
              "tests/eval_results.md.", file=sys.stderr)
        return 1
    print(f"Bed matches the recorded runs: {bed}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
