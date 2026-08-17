"""Stream-parse an Apple Health `export.xml` into the SQLite store.

Why streaming: a real export is routinely gigabytes (the file this parser was
developed against is 3.7 GB / 8.3M `<Record>` elements). `iterparse` plus an
explicit clear of processed elements keeps memory flat regardless of file size;
`ElementTree.parse` would need many gigabytes of RAM (plan §3.1).

Shape of the file, as emitted by HealthKit Export Version 14:

    <HealthData locale="en_US">
      <ExportDate value="..."/>
      <Me HKCharacteristicTypeIdentifierDateOfBirth="..." .../>
      <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="..."
              sourceVersion="..." device="..." unit="count/min"
              creationDate="..." startDate="..." endDate="..." value="72">
        <MetadataEntry key="..." value="..."/>
      </Record>
      <Correlation ...><Record .../></Correlation>
      <Workout workoutActivityType="..." duration="..." ...>
        <WorkoutStatistics .../><WorkoutEvent .../><WorkoutRoute .../>
      </Workout>
      <ActivitySummary dateComponents="2026-03-14" .../>
      <ClinicalRecord ... resourceFilePath="clinical-records/..."/>
    </HealthData>

Three details that are easy to get wrong and are handled explicitly below:

1. **Timestamps are "YYYY-MM-DD HH:MM:SS ±HHMM"**, not ISO 8601 — space-separated,
   offset without a colon. The offset varies within a single file (DST, travel),
   so the calendar date a sample belongs to must come from its own offset.

2. **Records inside `<Correlation>` are duplicates.** Apple's own DTD comment
   says so: "Any Records that appear as children of a correlation also appear as
   top-level records in this document." Ingesting both double-counts blood
   pressure and nutrition samples.

3. **`unit` and `value` are optional** (`#IMPLIED`), and `value` is a string:
   quantity types carry numbers, category types carry identifiers such as
   `HKCategoryValueSleepAnalysisAsleepREM`.

Deliberately not ingested in milestone 1:
  * `<InstantaneousBeatsPerMinute>` beat-by-beat lists inside HRV records —
    hundreds of thousands of rows supporting no query the agent will ask yet.
  * `<WorkoutRoute>` / GPS (roadmap item 6) and `<ClinicalRecord>` FHIR JSON
    (milestone 2 territory, and it lives in sibling files rather than the XML).
  * `<Audiogram>` / `<VisionPrescription>` — rare and query-irrelevant for now.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from ..logging_setup import get_logger

log = get_logger("ingest.healthkit")

BATCH_SIZE = 20_000
HASH_CHUNK = 8 * 1024 * 1024

HK_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S %z"


@dataclass
class IngestStats:
    """Counts only — never record content (plan §5a logging policy)."""

    records_seen: int = 0
    records_inserted: int = 0
    workouts_inserted: int = 0
    activity_summaries: int = 0
    characteristics: int = 0
    correlation_children_skipped: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    elapsed_sec: float = 0.0
    status: str = "complete"
    export_date: str | None = None

    @property
    def records_duplicate(self) -> int:
        """Rows that hit the dedup index — i.e. already present from a prior export."""
        return self.records_seen - self.records_inserted - sum(self.skipped.values())


class MalformedExport(RuntimeError):
    """The XML could not be parsed far enough to be useful."""


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #

def parse_hk_datetime(raw: str) -> datetime:
    """Parse a HealthKit timestamp into an offset-aware datetime.

    Primary format is "2026-03-14 06:12:33 -0400". `fromisoformat` is tried as a
    fallback because some third-party writers emit true ISO 8601 into the same
    attribute.
    """
    try:
        return datetime.strptime(raw, HK_DATETIME_FORMAT)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("unparseable timestamp") from exc
    if parsed.tzinfo is None:
        # No offset at all: assume UTC and say so, rather than inventing a local
        # timezone that would shift the calendar date silently.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _normalize(raw: str | None) -> tuple[str | None, float | None, str | None]:
    """Return (ISO 8601 string, UTC epoch seconds, local calendar date).

    An absent or unparseable timestamp yields all-None rather than raising, so
    the caller decides whether it is fatal for that element: a bad `startDate`
    means the record can't be placed in time and is skipped, while a bad
    `endDate` only costs the duration.
    """
    if not raw:
        return None, None, None
    try:
        dt = parse_hk_datetime(raw)
    except ValueError:
        return None, None, None
    return dt.isoformat(), dt.timestamp(), dt.date().isoformat()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _dedup_key(*parts: Any) -> bytes:
    """Stable 16-byte identity for a sample.

    HealthKit `<Record>` elements carry no UUID, so identity is the tuple of
    fields that make a sample what it is. Every export re-contains the full
    history, so without this a second export doubles the store.
    """
    joined = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.blake2b(joined.encode("utf-8"), digest_size=16).digest()


def _as_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _metadata(elem: ET.Element) -> str | None:
    entries = {
        child.get("key"): child.get("value")
        for child in elem
        if child.tag == "MetadataEntry" and child.get("key")
    }
    return json.dumps(entries, sort_keys=True) if entries else None


def _workout_statistics(elem: ET.Element) -> str | None:
    stats = [
        {k: v for k, v in child.attrib.items()}
        for child in elem
        if child.tag == "WorkoutStatistics"
    ]
    return json.dumps(stats, sort_keys=True) if stats else None


# --------------------------------------------------------------------------- #
# Source-file bookkeeping (plan §3.4 incremental refresh)
# --------------------------------------------------------------------------- #

def register_source_file(conn: sqlite3.Connection, path: Path, kind: str,
                         digest: str) -> tuple[int, bool]:
    """Upsert a source_file row. Returns (id, already_ingested).

    `already_ingested` is True only when the same path was previously ingested
    to completion with an identical hash — the signal to skip re-parsing.
    """
    stat = path.stat()
    row = conn.execute(
        "SELECT id, sha256, status FROM source_file WHERE path = ?", (str(path),)
    ).fetchone()

    if row is not None:
        unchanged = row["sha256"] == digest and row["status"] == "complete"
        if not unchanged:
            conn.execute(
                "UPDATE source_file SET sha256 = ?, size_bytes = ?, mtime = ?, "
                "ingested_at = ?, status = 'partial' WHERE id = ?",
                (digest, stat.st_size, stat.st_mtime, _now(), row["id"]),
            )
            conn.commit()
        return int(row["id"]), unchanged

    cur = conn.execute(
        "INSERT INTO source_file(path, kind, sha256, size_bytes, mtime, "
        "ingested_at, record_count, status) VALUES(?,?,?,?,?,?,0,'partial')",
        (str(path), kind, digest, stat.st_size, stat.st_mtime, _now()),
    )
    conn.commit()
    return int(cur.lastrowid), False


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _finalize_source_file(conn: sqlite3.Connection, source_file_id: int,
                          record_count: int, status: str) -> None:
    from ..store import sqlite_schema

    sqlite_schema.mark_source_file(conn, source_file_id,
                                   record_count=record_count, status=status)


# --------------------------------------------------------------------------- #
# Main parse
# --------------------------------------------------------------------------- #

def ingest_export(
    conn: sqlite3.Connection,
    path: Path,
    *,
    source_file_id: int,
    stream: IO[bytes] | None = None,
    progress: Callable[[IngestStats], None] | None = None,
    progress_every: int = 250_000,
) -> IngestStats:
    """Parse `path` (or `stream`, when the XML lives inside a zip) into `conn`.

    Returns counts; raises MalformedExport if the file is unusable from the very
    start.

    A parse error partway through is *not* fatal: whatever parsed cleanly is
    kept, the file is marked 'partial', and the caller reports it. Truncated
    exports are a real failure mode (interrupted AirDrop, partial unzip) and
    losing an entire ingest over the last few bytes of a 3 GB file is worse than
    keeping what was readable.
    """
    stats = IngestStats()
    started = time.monotonic()

    record_rows: list[tuple] = []
    workout_rows: list[tuple] = []
    summary_rows: list[tuple] = []

    def flush() -> None:
        nonlocal record_rows, workout_rows, summary_rows
        if record_rows:
            before = conn.total_changes
            conn.executemany(_INSERT_RECORD, record_rows)
            stats.records_inserted += conn.total_changes - before
            record_rows = []
        if workout_rows:
            before = conn.total_changes
            conn.executemany(_INSERT_WORKOUT, workout_rows)
            stats.workouts_inserted += conn.total_changes - before
            workout_rows = []
        if summary_rows:
            conn.executemany(_INSERT_SUMMARY, summary_rows)
            stats.activity_summaries += len(summary_rows)
            summary_rows = []
        conn.commit()

    try:
        context = ET.iterparse(stream if stream is not None else str(path),
                               events=("start", "end"))
        event, root = next(context)
    except (ET.ParseError, StopIteration, OSError) as exc:
        raise MalformedExport(f"{path.name}: {type(exc).__name__}") from exc

    if root.tag != "HealthData":
        raise MalformedExport(
            f"{path.name}: root element is <{root.tag}>, expected <HealthData>"
        )

    stack: list[str] = []
    try:
        for event, elem in context:
            if event == "start":
                stack.append(elem.tag)
                continue

            tag = stack.pop() if stack else elem.tag
            nested = bool(stack)

            try:
                if tag == "Record":
                    if "Correlation" in stack:
                        # Duplicate of a top-level record; see module docstring.
                        stats.correlation_children_skipped += 1
                    else:
                        stats.records_seen += 1
                        row = _record_row(elem, source_file_id)
                        if row is None:
                            stats.skipped["missing or unparseable startDate"] += 1
                        else:
                            record_rows.append(row)
                elif tag == "Workout" and not nested:
                    row = _workout_row(elem, source_file_id)
                    if row is None:
                        stats.skipped["workout with unparseable startDate"] += 1
                    else:
                        workout_rows.append(row)
                elif tag == "ActivitySummary" and not nested:
                    row = _summary_row(elem, source_file_id)
                    if row is None:
                        stats.skipped["activity summary without date"] += 1
                    else:
                        summary_rows.append(row)
                elif tag == "Me" and not nested:
                    stats.characteristics += _store_characteristics(
                        conn, elem, source_file_id
                    )
                elif tag == "ExportDate" and not nested:
                    stats.export_date = elem.get("value")
            except Exception as exc:  # noqa: BLE001 - one bad element must not stop the run
                stats.skipped[type(exc).__name__] += 1

            if not nested:
                # Direct child of <HealthData>: safe to drop everything parsed so
                # far. Without this, root accumulates every element and the
                # streaming parse degrades into a full in-memory load.
                elem.clear()
                root.clear()

            if len(record_rows) >= BATCH_SIZE:
                flush()
                if progress and stats.records_seen % progress_every < BATCH_SIZE:
                    stats.elapsed_sec = time.monotonic() - started
                    progress(stats)

    except ET.ParseError as exc:
        stats.status = "partial"
        # Position only, never the offending text.
        log.warning(
            "%s: XML parse error at line %s, column %s; keeping %d records "
            "parsed before the error",
            path.name, getattr(exc, "position", ("?", "?"))[0],
            getattr(exc, "position", ("?", "?"))[1], stats.records_seen,
        )

    flush()
    stats.elapsed_sec = time.monotonic() - started
    return stats


_INSERT_RECORD = """
INSERT OR IGNORE INTO record (
    type, value_num, value_text, unit, source_name, source_version, device,
    start_date, end_date, creation_date, start_epoch, duration_sec, local_date,
    metadata_json, source_file_id, dedup_key
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

_INSERT_WORKOUT = """
INSERT OR IGNORE INTO workout (
    activity_type, duration, duration_unit, total_distance, total_distance_unit,
    total_energy, total_energy_unit, source_name, source_version, device,
    start_date, end_date, creation_date, start_epoch, local_date,
    metadata_json, statistics_json, source_file_id, dedup_key
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

_INSERT_SUMMARY = """
INSERT OR REPLACE INTO activity_summary (
    local_date, active_energy, active_energy_goal, active_energy_unit,
    move_time, move_time_goal, exercise_time, exercise_time_goal,
    stand_hours, stand_hours_goal, source_file_id
) VALUES (?,?,?,?,?,?,?,?,?,?,?)
"""


def _record_row(elem: ET.Element, source_file_id: int) -> tuple | None:
    get = elem.get
    start_raw = get("startDate")
    if not start_raw:
        return None
    start_iso, start_epoch, local_date = _normalize(start_raw)
    if start_iso is None or start_epoch is None or local_date is None:
        return None
    end_iso, end_epoch, _ = _normalize(get("endDate"))
    created_iso, _, _ = _normalize(get("creationDate"))

    raw_value = get("value")
    numeric = _as_float(raw_value)
    # Numeric values go to value_num; anything else (category identifiers such as
    # HKCategoryValueSleepAnalysisAsleepREM) stays as text so aggregates can't
    # coerce it to 0.
    value_text = None if numeric is not None else raw_value

    record_type = get("type")
    source_name = get("sourceName")
    unit = get("unit")

    duration = (end_epoch - start_epoch) if end_epoch is not None else None

    return (
        record_type, numeric, value_text, unit, source_name, get("sourceVersion"),
        get("device"), start_iso, end_iso, created_iso, start_epoch, duration,
        local_date, _metadata(elem), source_file_id,
        _dedup_key(record_type, start_iso, end_iso, source_name, raw_value, unit),
    )


def _workout_row(elem: ET.Element, source_file_id: int) -> tuple | None:
    get = elem.get
    start_raw = get("startDate")
    if not start_raw:
        return None
    start_iso, start_epoch, local_date = _normalize(start_raw)
    if start_iso is None or start_epoch is None or local_date is None:
        return None
    end_iso, _, _ = _normalize(get("endDate"))
    created_iso, _, _ = _normalize(get("creationDate"))

    activity = get("workoutActivityType")
    source_name = get("sourceName")
    duration = _as_float(get("duration"))

    return (
        activity, duration, get("durationUnit"),
        _as_float(get("totalDistance")), get("totalDistanceUnit"),
        _as_float(get("totalEnergyBurned")), get("totalEnergyBurnedUnit"),
        source_name, get("sourceVersion"), get("device"),
        start_iso, end_iso, created_iso, start_epoch, local_date,
        _metadata(elem), _workout_statistics(elem), source_file_id,
        _dedup_key(activity, start_iso, end_iso, source_name, duration),
    )


def _summary_row(elem: ET.Element, source_file_id: int) -> tuple | None:
    date = elem.get("dateComponents")
    if not date:
        return None
    get = elem.get
    return (
        date,
        _as_float(get("activeEnergyBurned")), _as_float(get("activeEnergyBurnedGoal")),
        get("activeEnergyBurnedUnit"),
        _as_float(get("appleMoveTime")), _as_float(get("appleMoveTimeGoal")),
        _as_float(get("appleExerciseTime")), _as_float(get("appleExerciseTimeGoal")),
        _as_float(get("appleStandHours")), _as_float(get("appleStandHoursGoal")),
        source_file_id,
    )


def _store_characteristics(conn: sqlite3.Connection, elem: ET.Element,
                           source_file_id: int) -> int:
    rows = [(k, v, source_file_id) for k, v in elem.attrib.items() if v]
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO characteristic(key, value, source_file_id) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "source_file_id = excluded.source_file_id",
        rows,
    )
    conn.commit()
    return len(rows)


# --------------------------------------------------------------------------- #
# Entry point used by the CLI
# --------------------------------------------------------------------------- #

def _export_member(archive: zipfile.ZipFile, path: Path) -> str:
    """Locate `export.xml` inside an Apple Health `export.zip`.

    Streamed rather than extracted: the XML is often several GB and there is no
    reason to write a second copy of the user's health data to disk. Note that
    `export_cda.xml` (the CDA clinical-document sibling) must not match.
    """
    candidates = [
        name for name in archive.namelist()
        if name.endswith("export.xml") and not name.endswith("export_cda.xml")
    ]
    if not candidates:
        raise MalformedExport(f"{path.name}: no export.xml inside the archive")
    # Prefer the canonical apple_health_export/export.xml over any stray copy.
    canonical = [c for c in candidates if c.endswith("apple_health_export/export.xml")]
    return (canonical or candidates)[0]


def ingest_file(
    conn: sqlite3.Connection,
    path: Path,
    *,
    force: bool = False,
    progress: Callable[[IngestStats], None] | None = None,
) -> IngestStats | None:
    """Hash, register, and parse one export file.

    Returns None when the file was already ingested unchanged (incremental skip).
    """
    path = Path(path)
    digest = sha256_file(path)
    source_file_id, already = register_source_file(conn, path, "healthkit", digest)

    if already and not force:
        log.info("%s unchanged since last ingest; skipping", path.name)
        return None

    # Ingest-time pragmas: this is a bulk load into a local file the user can
    # rebuild from source at any time, so durability per-transaction buys nothing
    # and costs a lot.
    #
    # Measured on a 3.7 GB / 8.3M-record export: 6.5 min to parse, ~1.4 GB peak
    # RSS. The parse itself is flat at ~13 MB regardless of file size (verified
    # separately); essentially all of that peak is SQLite's page cache and index
    # maintenance, so `cache_size` below is the knob if a machine is memory-tight.
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -262144")  # ~256 MB
    try:
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                member = _export_member(archive, path)
                with archive.open(member) as handle:
                    stats = ingest_export(
                        conn, path, source_file_id=source_file_id,
                        stream=handle, progress=progress,
                    )
        else:
            stats = ingest_export(
                conn, path, source_file_id=source_file_id, progress=progress
            )
    finally:
        conn.execute("PRAGMA synchronous = NORMAL")

    _finalize_source_file(conn, source_file_id, stats.records_inserted, stats.status)
    return stats
