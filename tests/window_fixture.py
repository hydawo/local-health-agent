"""The synthetic index behind eval questions Q34 to Q37.

These rows live in their own index, not in the one built from
`tests/fixtures/export.xml`. Adding resting heart rate or cycling to that index
would change the answers to Q1, Q7, Q17 and Q19, which are pinned to it, and
the export file itself is not to be edited. So the window questions get an
index of their own:

    python tests/window_fixture.py /tmp/window-eval.db
    python tests/run_agent_eval.py --index <main index> \
        --window-index /tmp/window-eval.db

`tests/test_eval.py` builds the same rows into a temp directory, so the unit
check and the live run read identical data. Every value here is invented. The
spec's target questions came from a real export, and none of its numbers
appears in this repository.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from health_agent.store import sqlite_schema  # noqa: E402

RHR = "HKQuantityTypeIdentifierRestingHeartRate"
HR = "HKQuantityTypeIdentifierHeartRate"


def build(path: Path):
    """Create the index at `path` and return an open connection to it."""
    conn = sqlite_schema.connect(Path(path), create=True)
    sqlite_schema.initialize(conn)
    conn.execute("INSERT INTO source_file(id, path, kind, sha256, size_bytes, "
                 "ingested_at) VALUES(1, 'x', 'healthkit', 'h', 1, 'now')")
    counter = iter(range(1, 10_000))

    def epoch(date, hour):
        return dt.datetime.fromisoformat(f"{date}T{hour:02d}:00:00+00:00").timestamp()

    def resting(date, value):
        conn.execute(
            "INSERT INTO record(type, value_num, unit, source_name, "
            "start_date, start_epoch, local_date, dedup_key) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (RHR, value, "count/min", "Watch", f"{date}T08:00:00+00:00",
             epoch(date, 8), date, next(counter).to_bytes(4, "big")))

    event = dt.date(2023, 2, 18)
    for off in range(-7, 0):
        resting((event + dt.timedelta(days=off)).isoformat(), 67.0 + off)
    for off in range(1, 7):
        resting((event + dt.timedelta(days=off)).isoformat(), 63.0 + off)
    for day in range(1, 32):
        resting(dt.date(2022, 1, day).isoformat(), 58.0 + day % 3)
        resting(dt.date(2024, 1, day).isoformat(), 70.0 + day % 3)
    sqlite_schema.rebuild_daily_metrics(conn)

    cur = conn.execute(
        "INSERT INTO document(source_file_id, path, kind, extraction, "
        "ingested_at) VALUES(1, 'clinical-records/Condition-acl.json', "
        "'fhir', 'text', 'now')")
    conn.execute(
        "INSERT INTO clinical_fact(document_id, kind, name, onset_date, "
        "dedup_key) VALUES(?, 'condition', 'Anterior cruciate ligament tear', "
        "'2023-02-18', ?)", (cur.lastrowid, b"\x01"))

    def cycling(date, minutes):
        cur = conn.execute(
            "INSERT INTO workout(activity_type, duration, duration_unit, "
            "source_name, start_date, start_epoch, local_date, dedup_key) "
            "VALUES('HKWorkoutActivityTypeCycling', ?, 'min', 'Watch', ?, ?, "
            "?, ?)", (minutes, f"{date}T09:00:00+00:00", epoch(date, 9), date,
                      next(counter).to_bytes(4, "big")))
        conn.execute(
            "INSERT INTO workout_stat(workout_id, kind, key, value_num, unit) "
            "VALUES(?, 'statistic', ?, 150.0, 'count/min')",
            (cur.lastrowid, f"{HR}:average"))

    # Earlier block: two 45-minute rides a week, three in its third week so the
    # two blocks' weekly ranges meet. Later block: three a week.
    for week in range(4):
        for day in ((0, 2, 4) if week == 2 else (0, 2)):
            cycling((dt.date(2025, 3, 3) + dt.timedelta(days=7 * week + day))
                    .isoformat(), 45.0)
        for day in (0, 2, 4):
            cycling((dt.date(2026, 3, 2) + dt.timedelta(days=7 * week + day))
                    .isoformat(), 45.0)
    conn.commit()
    return conn




if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python tests/window_fixture.py PATH")
    build(Path(sys.argv[1])).close()
    print(f"wrote {sys.argv[1]}")
