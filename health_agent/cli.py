"""`health-agent` command line interface.

Milestone 1 commands (no LLM involved yet — this tier is pure SQL):

    health-agent ingest [PATH]      parse an Apple Health export into the index
    health-agent stats              what's in the index
    health-agent types [--search]   which HealthKit types are present
    health-agent metric NAME        aggregate one metric over time
    health-agent workouts           workout totals by activity type
    health-agent reset              wipe the index (confirms first)

`ask` (the natural-language entry point) arrives at milestone 5.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

from . import (__version__, agent, config, consent, embeddings, labs, metrics,
               offline_check,
               ollama_client)
from .ingest import healthkit, notes, records
from .logging_setup import configure, get_logger
from .store import queries, sqlite_schema, vector_store

log = get_logger("cli")


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

def _fmt_number(value: float | None, unit: str | None = None) -> str:
    if value is None:
        return "-"
    if unit == "s":
        return _fmt_duration(value)
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 10:
        return f"{value:,.1f}"
    return f"{value:,.2f}"


def _fmt_size(num_bytes: int) -> str:
    if num_bytes >= 1e9:
        return f"{num_bytes / 1e9:,.1f} GB"
    if num_bytes >= 1e6:
        return f"{num_bytes / 1e6:,.0f} MB"
    return f"{num_bytes / 1e3:,.0f} KB"


def _fmt_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _table(headers: list[str], rows: list[list[str]], indent: str = "") -> str:
    if not rows:
        return ""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    right = {i for i, h in enumerate(headers) if h.lower() in
             {"value", "n", "samples", "count", "records", "total", "avg", "min", "max"}}

    def line(cells: list[str]) -> str:
        out = []
        for i, cell in enumerate(cells):
            out.append(cell.rjust(widths[i]) if i in right else cell.ljust(widths[i]))
        return indent + "  ".join(out).rstrip()

    sep = indent + "  ".join("-" * w for w in widths)
    return "\n".join([line(headers), sep, *(line(r) for r in rows)])


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def _plan_ingest(args: argparse.Namespace,
                 cfg: config.Config) -> tuple[Path | None, list[Path], list[Path]]:
    """Decide what to ingest: (healthkit export, record files, note files).

    Accepts an explicit path (an export, a PDF, a note, or a folder) or nothing,
    in which case the data folder's `healthkit/`, `records/` and `notes/`
    subfolders are used.
    """
    if args.path:
        target = Path(args.path).expanduser()
        if target.is_file():
            suffix = target.suffix.lower()
            if suffix == ".pdf":
                return None, [target], []
            if suffix in notes.SUPPORTED_SUFFIXES:
                return None, [], [target]
            return _resolve_export_path(target), [], []
        # A folder laid out like a data directory (with records/ and/or notes/
        # subfolders) is read by convention. Otherwise everything under it is
        # fair game. Without this, pointing at a data directory would sweep up
        # every stray .md in the tree — a README next to your records is
        # documentation, not a health note.
        if (target / "notes").is_dir() or (target / "records").is_dir():
            return (_resolve_export_path(target),
                    records.find_records(target / "records")
                    if (target / "records").is_dir() else [],
                    notes.find_notes(target / "notes")
                    if (target / "notes").is_dir() else [])
        return (_resolve_export_path(target),
                records.find_records(target),
                notes.find_notes(target))

    export = _resolve_export_path(cfg.default_export_path)
    if export is None:
        export = _resolve_export_path(cfg.healthkit_dir)
    found_records = (records.find_records(cfg.records_dir)
                     if cfg.records_dir.is_dir() else [])
    found_notes = notes.find_notes(cfg.notes_dir) if cfg.notes_dir.is_dir() else []
    return export, found_records, found_notes


def cmd_ingest(args: argparse.Namespace, cfg: config.Config) -> int:
    export, record_files, note_files = _plan_ingest(args, cfg)

    if export is None and not record_files and not note_files:
        print(
            f"Nothing to ingest. Looked for an Apple Health export at "
            f"{args.path or cfg.default_export_path}, PDFs under "
            f"{cfg.records_dir}, and notes under {cfg.notes_dir}.\n"
            f"Export from Health app > profile > Export All Health Data, then run:\n"
            f"  health-agent ingest /path/to/export.zip",
            file=sys.stderr,
        )
        return 2

    if args.rebuild and cfg.index_path.exists():
        cfg.index_path.unlink()
        for suffix in ("-wal", "-shm"):
            extra = cfg.index_path.with_name(cfg.index_path.name + suffix)
            extra.unlink(missing_ok=True)
        log.info("removed existing index for rebuild")

    conn = sqlite_schema.connect(cfg.index_path, create=True)
    try:
        if cfg.index_path.stat().st_size > 0 and _has_tables(conn):
            sqlite_schema.check_version(conn)
        sqlite_schema.initialize(conn)

        force = args.force or args.rebuild

        if export is not None:
            _ingest_healthkit(conn, export, cfg, force=force)
        if record_files:
            _ingest_records(conn, record_files, cfg, force=force,
                            use_ocr=not args.no_ocr)
        if note_files:
            _ingest_notes(conn, note_files, force=force)
        if (record_files or note_files) and not args.no_embed:
            _embed_chunks(conn, cfg, args.embedder, quiet_if_unavailable=True)
        return 0
    finally:
        conn.close()


def _ingest_healthkit(conn: sqlite3.Connection, target: Path,
                      cfg: config.Config, *, force: bool) -> None:
    print(f"Ingesting {target.name} ({_fmt_size(target.stat().st_size)}) "
          f"-> {cfg.index_path}", flush=True)

    def progress(stats: healthkit.IngestStats) -> None:
        print(f"  ... {stats.records_seen:,} records read "
              f"({stats.elapsed_sec:,.0f}s)", flush=True)

    stats = healthkit.ingest_file(conn, target, force=force, progress=progress)
    if stats is None:
        print("  already ingested and unchanged (use --force to re-parse)")
        return

    print("  rebuilding daily aggregates ...", flush=True)
    daily_rows = sqlite_schema.rebuild_daily_metrics(conn)

    print()
    print(f"HealthKit parsed in {stats.elapsed_sec:,.1f}s")
    print(f"  records read        {stats.records_seen:,}")
    print(f"  records inserted    {stats.records_inserted:,}")
    if stats.records_duplicate:
        print(f"  already in index    {stats.records_duplicate:,}")
    if stats.correlation_children_skipped:
        print(f"  correlation dupes   {stats.correlation_children_skipped:,} "
              f"(children of <Correlation>, present as top-level records)")
    print(f"  workouts            {stats.workouts_inserted:,}")
    print(f"  activity summaries  {stats.activity_summaries:,}")
    print(f"  daily aggregate rows {daily_rows:,}")
    for reason, count in stats.skipped.most_common():
        print(f"  skipped: {reason}: {count:,}")
    if stats.status == "partial":
        print("\nWARNING: the export ended unexpectedly and was ingested only "
              "up to the parse error. Re-export and run `ingest --force`.",
              file=sys.stderr)
    print()


def _ingest_records(conn: sqlite3.Connection, files: list[Path],
                    cfg: config.Config, *, force: bool, use_ocr: bool) -> None:
    print(f"Ingesting {len(files)} record file(s)", flush=True)

    def register(path: Path) -> tuple[int, bool]:
        digest = healthkit.sha256_file(path)
        return healthkit.register_source_file(conn, path, "record", digest)

    def on_file(path: Path) -> None:
        print(f"  {path.name}", flush=True)

    root = files[0].parent if len(files) == 1 else _common_parent(files)
    stats = records.ingest_records(
        conn, root, register=register, use_ocr=use_ocr, force=force,
        on_file=on_file,
    )

    print()
    print(f"Records: {stats.documents} document(s)")
    print(f"  pages (text layer)  {stats.pages_text}")
    print(f"  pages (OCR)         {stats.pages_ocr}")
    if stats.pages_empty:
        print(f"  pages with no text  {stats.pages_empty}")
    print(f"  lab values          {stats.lab_values}")
    if stats.lab_values_duplicate:
        print(f"  duplicate values    {stats.lab_values_duplicate} "
              f"(found by both the table and line passes)")
    print(f"  text chunks         {stats.chunks}")
    if stats.unit_warnings:
        print(f"  unit mismatches     {stats.unit_warnings} "
              f"(unit differs from the usual one for that analyte)")
    for reason, count in stats.skipped.items():
        print(f"  skipped: {reason}: {count}")
    if stats.pages_empty and not stats.ocr_available:
        print("\nNote: some pages have no text layer and OCR is unavailable, so "
              "those pages were not read. Install Tesseract to enable OCR:\n"
              "  macOS:  brew install tesseract\n"
              "  Debian: apt install tesseract-ocr\n"
              "Then re-run with `ingest --force`.")
    print()


def _ingest_notes(conn: sqlite3.Connection, files: list[Path], *,
                  force: bool) -> None:
    print(f"Ingesting {len(files)} note(s)", flush=True)

    def register(path: Path) -> tuple[int, bool]:
        digest = healthkit.sha256_file(path)
        return healthkit.register_source_file(conn, path, "note", digest)

    def on_file(path: Path) -> None:
        print(f"  {path.name}", flush=True)

    root = files[0].parent if len(files) == 1 else _common_parent(files)
    stats = notes.ingest_notes(conn, root, register=register, force=force,
                               on_file=on_file)

    print()
    print(f"Notes: {stats.notes} note(s)")
    print(f"  text chunks         {stats.chunks}")
    print(f"  with tags           {stats.tagged}")
    print(f"  dated               {stats.dated}")
    if stats.undated:
        print(f"  undated             {stats.undated} "
              f"(no date in frontmatter or filename)")
    if stats.unparsed_frontmatter:
        print(f"  partial frontmatter {stats.unparsed_frontmatter} "
              f"(kept raw; only simple keys are interpreted)")
    for reason, count in stats.skipped.items():
        print(f"  skipped: {reason}: {count}")
    print()


def _common_parent(files: list[Path]) -> Path:
    parents = {p.parent for p in files}
    if len(parents) == 1:
        return parents.pop()
    import os
    return Path(os.path.commonpath([str(p.parent) for p in files]))


def _embed_chunks(conn: sqlite3.Connection, cfg: config.Config, backend: str,
                  *, quiet_if_unavailable: bool = False) -> int:
    """Embed pending chunks. Returns the number embedded (0 when unavailable)."""
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM chunk WHERE embedded_with IS NULL"
    ).fetchone()["n"]
    if not pending:
        return 0

    try:
        embedder = embeddings.get_embedder(backend)
        store = vector_store.VectorStore(cfg.vector_path)

        def progress(done: int, total: int) -> None:
            print(f"  embedded {done}/{total} chunks", flush=True)

        print(f"Embedding {pending} chunk(s) with {backend} ...", flush=True)
        count = vector_store.embed_pending(conn, store, embedder,
                                           progress=progress)
        print(f"  done: {count} chunk(s) embedded with {embedder.name}\n")
        return count
    except (embeddings.EmbeddingUnavailable, embeddings.RemoteHostRefused) as exc:
        message = (
            f"Chunks are stored but not embedded: {exc}\n"
            f"Keyword search works now; run `health-agent embed` once Ollama is "
            f"available to enable semantic search."
        )
        if quiet_if_unavailable:
            print(f"\nNote: {message}\n")
            return 0
        print(f"error: {message}", file=sys.stderr)
        raise


def _resolve_export_path(target: Path) -> Path | None:
    """Accept an export.xml, an export.zip, or a directory containing either."""
    if target.is_file():
        return target
    if target.is_dir():
        for candidate in (
            target / "export.xml",
            target / "export.zip",
            target / "apple_health_export" / "export.xml",
            target / "healthkit" / "export.xml",
        ):
            if candidate.is_file():
                return candidate
    return None


def _has_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name='record'"
    ).fetchone()
    return bool(row and row["n"])


def cmd_stats(args: argparse.Namespace, cfg: config.Config) -> int:
    conn = sqlite_schema.open_for_read(cfg.index_path)
    try:
        summary = queries.index_summary(conn)
        if args.json:
            print(json.dumps(summary, indent=2))
            return 0

        print(f"Index: {cfg.index_path}")
        print(f"  records            {summary['records']:,} "
              f"across {summary['distinct_types']} HealthKit types")
        if summary["first_date"]:
            print(f"  date range         {summary['first_date']} to {summary['last_date']}")
        print(f"  workouts           {summary['workouts']:,}")
        print(f"  activity summaries {summary['activity_summaries']:,}")
        if summary["sources"]:
            print(f"  sources            {', '.join(summary['sources'][:8])}"
                  + (" ..." if len(summary["sources"]) > 8 else ""))

        print("\nSource files:")
        rows = [
            [Path(f["path"]).name, f["kind"], f"{f['size_bytes'] / 1e6:,.0f} MB",
             f"{f['record_count']:,}", f["status"], f["ingested_at"][:19]]
            for f in summary["files"]
        ]
        print(_table(["file", "kind", "size", "records", "status", "ingested"],
                     rows, indent="  "))

        note_stats = queries.note_summary(conn)
        if note_stats["notes"]:
            print("\nNotes:")
            print(f"  notes              {note_stats['notes']:,}"
                  + (f"   {note_stats['first']} to {note_stats['last']}"
                     if note_stats["first"] else ""))
            print(f"  tags               {note_stats['tags']:,}")
            print(f"  text chunks        {note_stats['chunks']:,}")
            if note_stats["undated"]:
                print(f"  undated            {note_stats['undated']:,}")

        docs = queries.document_summary(conn)
        if docs["documents"]:
            print("\nDocuments:")
            print(f"  documents          {docs['documents']:,}"
                  + (f"   {docs['doc_first']} to {docs['doc_last']}"
                     if docs["doc_first"] else ""))
            print(f"  pages              {docs['pages']:,}"
                  + (f"   ({docs['pages_ocr']:,} via OCR)"
                     if docs["pages_ocr"] else ""))
            print(f"  lab values         {docs['lab_results']:,} "
                  f"across {docs['analytes']} analytes")
            print(f"  text chunks        {docs['chunks']:,}   "
                  f"({docs['chunks_embedded']:,} embedded)")

        top = queries.list_types(conn, limit=12)
        print("\nMost common types:")
        rows = [[t["type"].replace("HKQuantityTypeIdentifier", "")
                 .replace("HKCategoryTypeIdentifier", "*"),
                 f"{t['n']:,}", t["first"] or "-", t["last"] or "-"]
                for t in top]
        print(_table(["type", "records", "first", "last"], rows, indent="  "))
        print("  (* = category type)")
        return 0
    finally:
        conn.close()


def cmd_types(args: argparse.Namespace, cfg: config.Config) -> int:
    conn = sqlite_schema.open_for_read(cfg.index_path)
    try:
        found = queries.list_types(conn, search=args.search, limit=args.limit)
        if args.json:
            print(json.dumps(found, indent=2))
            return 0
        if not found:
            print(f"No HealthKit types matching {args.search!r} in the index.")
            return 1
        rows = []
        for t in found:
            metric = metrics.lookup(t["type"])
            rows.append([
                t["type"],
                metric.aliases[0] if metric and metric.aliases else "-",
                f"{t['n']:,}",
                t["first"] or "-",
                t["last"] or "-",
                str(t["sources"]),
            ])
        print(_table(["healthkit type", "alias", "records", "first", "last", "srcs"],
                     rows))
        return 0
    finally:
        conn.close()


def cmd_metric(args: argparse.Namespace, cfg: config.Config) -> int:
    conn = sqlite_schema.open_for_read(cfg.index_path)
    try:
        metric = metrics.resolve(args.name)
        result = queries.metric_series(
            conn, metric,
            period=args.by, agg=args.agg, start=getattr(args, "from"), end=args.to,
            source=args.source, combine=args.combine,
        )

        if args.json:
            print(json.dumps(_series_as_dict(result), indent=2))
            return 0 if result.points else 1

        if result.is_empty:
            return _report_missing(conn, args, metric, result)

        points = result.points
        if args.limit and len(points) > args.limit:
            points = points[-args.limit:]
            truncated = len(result.points) - len(points)
        else:
            truncated = 0

        unit = result.unit or ""
        title = f"{metric.label} — {result.agg} per {result.period}"
        if unit and result.agg != "duration":
            title += f" ({unit})"
        print(title)
        if getattr(args, "from") or args.to:
            print(f"window: {getattr(args, 'from') or 'start'} to {args.to or 'end'}")
        print()

        has_category = any(p.category for p in points)
        headers = ["period"] + (["category"] if has_category else []) + \
                  ["value", "samples"] + (["sources"] if args.show_sources else [])
        rows = []
        for point in points:
            row = [point.period]
            if has_category:
                row.append((point.category or "-").replace("HKCategoryValue", ""))
            row.append(_fmt_number(point.value, result.unit))
            row.append(f"{point.n:,}")
            if args.show_sources:
                row.append(", ".join(point.sources))
            rows.append(row)
        print(_table(headers, rows))

        if truncated:
            print(f"\n({truncated:,} earlier period(s) not shown; use --limit 0 for all)")

        values = [p.value for p in points if p.value is not None]
        if values and not has_category:
            print()
            print(f"periods: {len(values)}   "
                  f"mean across periods shown: "
                  f"{_fmt_number(sum(values) / len(values), result.unit)}   "
                  f"min: {_fmt_number(min(values), result.unit)}   "
                  f"max: {_fmt_number(max(values), result.unit)}")

        _print_caveats(result, args)
        return 0
    finally:
        conn.close()


def _print_caveats(result: queries.SeriesResult, args: argparse.Namespace) -> None:
    notes: list[str] = []
    if result.multi_source_periods and not args.source:
        if result.combine == "sum":
            how = ("Totals shown ADD every source together (--combine sum), which "
                   "double-counts any period the devices both covered.")
            fix = "Drop --combine sum, or use --source NAME, to avoid that."
        else:
            how = ("Totals shown take the largest single source's total, because "
                   "summing overlapping devices double-counts.")
            fix = ("Use --source NAME to pin one source, or --combine sum if you "
                   "know they don't overlap.")
        notes.append(
            f"{result.multi_source_periods} period(s) had more than one recording "
            f"source for this metric. {how} Sources present: "
            f"{', '.join(result.sources_seen)}. {fix}"
        )
    if len(result.units_seen) > 1:
        notes.append(
            f"Mixed units in this range ({', '.join(result.units_seen)}); values "
            f"are not converted. Filter by source or narrow the window."
        )
    for note in notes:
        print(f"\nNote: {note}")


def _report_missing(conn: sqlite3.Connection, args: argparse.Namespace,
                    metric: metrics.Metric, result: queries.SeriesResult) -> int:
    """Missing-data behavior from plan §5a: answer with what we have, name what's
    missing, and ask whether it exists rather than just printing 'no data'."""
    window = ""
    if getattr(args, "from") or args.to:
        window = f" between {getattr(args, 'from') or 'the start of your data'} " \
                 f"and {args.to or 'now'}"

    if result.total_records:
        print(f"I have {result.total_records:,} {metric.label} record(s), but none"
              f"{window}.")
        print(f"Available range for this metric: {result.available_from} to "
              f"{result.available_to}.")
        print("\nWant to widen the window, or is data missing from that period "
              "(device not worn, export taken before then)?")
        return 1

    print(f"I don't have any {metric.label} data "
          f"({metric.identifier}) in this index.")
    suggestions = metrics.suggest(args.name)
    close = [s for s in suggestions if s.identifier != metric.identifier][:5]
    if close:
        print("\nRelated metrics I know about:")
        for s in close:
            print(f"  {s.aliases[0] if s.aliases else s.identifier:<22} {s.label}")
    present = queries.list_types(conn, search=metric.label.split()[0].lower(), limit=5)
    if present:
        print("\nSimilar types that ARE in your index:")
        for t in present:
            print(f"  {t['type']:<55} {t['n']:,} records")
    print("\nDo you track this (a device or app that writes it to Health), and "
          "should it be included in the next export?")
    return 1


def _series_as_dict(result: queries.SeriesResult) -> dict:
    return {
        "metric": result.metric.identifier,
        "label": result.metric.label,
        "kind": result.metric.kind.value,
        "aggregation": result.agg,
        "period": result.period,
        "combine": result.combine,
        "unit": result.unit,
        "units_seen": list(result.units_seen),
        "sources_seen": list(result.sources_seen),
        "multi_source_periods": result.multi_source_periods,
        "available_from": result.available_from,
        "available_to": result.available_to,
        "total_records": result.total_records,
        "points": [
            {
                "period": p.period,
                "period_start": p.period_start,
                "period_end": p.period_end,
                "category": p.category,
                "value": p.value,
                "samples": p.n,
                "sources": list(p.sources),
                "per_source": p.per_source,
            }
            for p in result.points
        ],
    }


def cmd_sleep(args: argparse.Namespace, cfg: config.Config) -> int:
    """Sleep by night rather than by calendar date.

    `metric sleep` groups by the date each segment starts on, which splits one
    night across two rows. This groups the way the question is usually asked.
    """
    conn = sqlite_schema.open_for_read(cfg.index_path)
    try:
        nights = queries.sleep_nights(conn, start=getattr(args, "from"),
                                      end=args.to)
        if args.json:
            print(json.dumps(nights, indent=2, default=str))
            return 0 if nights else 1
        if not nights:
            total, first, last = queries.sleep_coverage(conn)
            if not total:
                print("No sleep data in this index.")
                print("Do you wear something to bed that writes sleep to Health?")
            else:
                print(f"No sleep in that window. Available: {first} to {last}.")
            return 1

        if args.limit and len(nights) > args.limit:
            nights = nights[-args.limit:]

        stages = sorted({s for n in nights for s in n["stages"]})
        headers = ["night of", "asleep"] + [s.replace("Asleep", "") for s in stages]
        rows = []
        for night in nights:
            row = [night["night"], _fmt_duration(night["asleep_seconds"])]
            for stage in stages:
                seconds = night["stages"].get(stage)
                row.append(_fmt_duration(seconds) if seconds else "-")
            rows.append(row)
        print(_table(headers, rows))
        print("\nNights are labelled by the evening they began; segments after "
              "midnight count toward the night before.")
        return 0
    finally:
        conn.close()


def cmd_workouts(args: argparse.Namespace, cfg: config.Config) -> int:
    conn = sqlite_schema.open_for_read(cfg.index_path)
    try:
        found = queries.workout_summary(
            conn, start=getattr(args, "from"), end=args.to, limit=args.limit
        )
        if args.json:
            print(json.dumps(found, indent=2))
            return 0
        if not found:
            print("No workouts in this index"
                  + (" for that window." if getattr(args, "from") or args.to else "."))
            print("Do you record workouts on a device that writes to Health?")
            return 1
        rows = [[
            w["activity_type"].replace("HKWorkoutActivityType", ""),
            f"{w['n']:,}",
            f"{_fmt_number(w['total_duration'])} {w['duration_unit'] or ''}".strip(),
            f"{_fmt_number(w['total_distance'])} {w['total_distance_unit'] or ''}".strip()
            if w["total_distance"] else "-",
            f"{_fmt_number(w['total_energy'])} {w['total_energy_unit'] or ''}".strip()
            if w["total_energy"] else "-",
            w["first"], w["last"],
        ] for w in found]
        print(_table(["activity", "n", "duration", "distance", "energy",
                      "first", "last"], rows))
        return 0
    finally:
        conn.close()


def cmd_documents(args: argparse.Namespace, cfg: config.Config) -> int:
    conn = sqlite_schema.open_for_read(cfg.index_path)
    try:
        docs = queries.list_documents(conn)
        if args.json:
            print(json.dumps(docs, indent=2))
            return 0
        if not docs:
            print("No documents in this index.")
            print(f"Put bloodwork or record PDFs in {cfg.records_dir} and run "
                  f"`health-agent ingest`.")
            return 1
        rows = [[
            Path(d["path"]).name,
            d["doc_date"] or "-",
            str(d["page_count"]),
            d["extraction"],
            f"{d['labs']:,}",
            f"{d['chunks']:,}",
            (d["title"] or "")[:38],
        ] for d in docs]
        print(_table(["file", "date", "pages", "read via", "labs", "chunks",
                      "title"], rows))
        return 0
    finally:
        conn.close()


def _confirm_cloud(cfg: config.Config, model: str, *, assume_yes: bool) -> bool:
    """One-time consent for the cloud tier (plan §4, §5).

    Returns True when the caller may proceed. The notice is shown in full the
    first time and never again, unless its substance changes — a warning shown
    on every run is a warning nobody reads.
    """
    if not consent.needs_prompt(cfg.index_dir):
        return True

    print(consent.NOTICE, file=sys.stderr)
    print(f"\nModel: {model}", file=sys.stderr)

    if assume_yes:
        # Scripted use still records consent, so the decision is auditable
        # afterwards rather than invisible.
        consent.record(cfg.index_dir, model)
        print("Consent recorded via --yes.\n", file=sys.stderr)
        return True

    if not sys.stdin.isatty():
        print("\nCloud mode needs a one-time confirmation and this is not an "
              "interactive terminal. Re-run in a terminal, or pass --yes if "
              "you have read the notice above.", file=sys.stderr)
        return False

    try:
        answer = input("\nSend your health data to Anthropic's API? "
                       "Type 'yes' to agree: ").strip().lower()
    except EOFError:
        answer = ""

    # Deliberately not accepting "y": this is the one prompt in the tool where
    # a reflexive keystroke should not be enough.
    if answer != "yes":
        print("Cancelled. Nothing was sent.", file=sys.stderr)
        return False

    consent.record(cfg.index_dir, model)
    print(f"Recorded in {consent.consent_path(cfg.index_dir)}\n"
          f"Revoke with `health-agent cloud-consent --revoke`.\n",
          file=sys.stderr)
    return True


def cmd_ask(args: argparse.Namespace, cfg: config.Config) -> int:
    """Natural-language question, answered by a model using the tools."""
    backend = None
    if args.cloud:
        model = args.model or agent.backends.DEFAULT_CLOUD_MODEL
        if not _confirm_cloud(cfg, model, assume_yes=args.yes):
            return 1
        try:
            backend = agent.backends.CloudBackend(model=args.model,
                                                  effort=args.effort)
        except agent.backends.CloudUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    conn = sqlite_schema.open_for_read(cfg.index_path)
    literature_conn = None
    try:
        def embedder_factory():
            return embeddings.get_embedder(args.embedder)

        from .literature import schema as lit_schema

        try:
            literature_conn = lit_schema.connect(cfg.literature_path)
        except lit_schema.CorpusNotFound:
            # Optional. The tool reports its own absence in terms the model
            # can use.
            pass

        ctx = agent.ToolContext(
            conn=conn,
            vector_path=cfg.vector_path,
            embedder_factory=embedder_factory,
            literature_conn=literature_conn,
            literature_vector_path=cfg.literature_vector_path,
        )

        show_progress = not args.json and not args.quiet

        def on_event(event: str, data: dict) -> None:
            if not show_progress:
                return
            if event == "tool_call":
                shown = {k: v for k, v in data["arguments"].items() if v not in
                         (None, "", [])}
                print(f"  → {data['name']}({_fmt_args(shown)})",
                      file=sys.stderr, flush=True)
            elif event == "tool_result" and data.get("error"):
                print(f"    ! {data['error']}", file=sys.stderr, flush=True)
            elif event == "step_limit":
                print(f"  (reached the {data['max_steps']}-step limit; "
                      f"answering from what was gathered)",
                      file=sys.stderr, flush=True)
            elif event == "guardrail_rewrite":
                print("  ↻ guardrail: asking for a restatement without "
                      "interpretation", file=sys.stderr, flush=True)
            elif event == "guardrail":
                print(f"  ⚠ guardrail: {', '.join(data['categories'])}"
                      + (" (rewritten)" if data["rewritten"] else ""),
                      file=sys.stderr, flush=True)

        orchestrator = agent.Orchestrator(
            ctx, model=args.model, think=args.think,
            max_steps=args.max_steps, on_event=on_event, backend=backend,
        )

        if show_progress:
            # The tier is always stated, never inferred from the flag: a user
            # should not have to remember what they typed to know whether their
            # data left the machine.
            tier = ("CLOUD — data is sent to Anthropic"
                    if orchestrator.tier == "cloud" else "local")
            print(f"Asking {orchestrator.model} [{tier}]"
                  f"{' (thinking shown)' if args.think else ''} ...",
                  file=sys.stderr, flush=True)

        try:
            answer = orchestrator.ask(args.question)
        except Exception as exc:  # noqa: BLE001 - mapped to an actionable message
            if backend is not None and isinstance(exc, backend.sdk_errors):
                print(f"error: {backend.describe_error(exc)}", file=sys.stderr)
                return 2
            raise

        if args.json:
            print(json.dumps({
                "question": args.question,
                "answer": answer.text,
                "model": answer.model,
                "tier": answer.tier,
                "refused": answer.refused,
                "elapsed_sec": round(answer.elapsed_sec, 2),
                "hit_step_limit": answer.hit_step_limit,
                "tools_used": answer.tools_used,
                "citations_available": answer.citations,
                "guardrail": {
                    "flags": [str(f) for f in answer.guardrail.flags],
                    "categories": answer.guardrail.categories,
                    "rewritten": answer.guardrail.rewritten,
                    "disclaimer_added": answer.guardrail.disclaimer_added,
                } if answer.guardrail else None,
                "steps": [
                    {"step": s.step, "tool": s.name, "arguments": s.arguments,
                     "elapsed_sec": round(s.elapsed_sec, 2),
                     "error": s.result.get("error")}
                    for s in answer.steps
                ],
            }, indent=2, default=str))
            return 0

        print()
        print(answer.text)

        if args.show_tools and answer.steps:
            print("\n--- tool calls ---")
            for step in answer.steps:
                status = f"error: {step.result['error']}" if step.failed else "ok"
                print(f"  {step.step}. {step.name}({_fmt_args(step.arguments)}) "
                      f"[{step.elapsed_sec:.2f}s, {status}]")

        if args.show_tools and answer.guardrail and answer.guardrail.flags:
            print("\n--- guardrail ---")
            for flag in answer.guardrail.flags:
                print(f"  {flag}")
            if answer.guardrail.rewritten:
                print("  (answer was rewritten and re-checked)")

        if show_progress:
            summary = (f"\n({answer.elapsed_sec:.1f}s, "
                       f"{len(answer.steps)} tool call"
                       f"{'' if len(answer.steps) == 1 else 's'})")
            print(summary, file=sys.stderr)

        if not answer.steps:
            # No tool was called, so nothing in the answer is grounded in the
            # user's data. Worth flagging rather than letting it read as an
            # answer about them.
            print("\nNote: this answer used none of your data — no tool was "
                  "called. Ask something more specific, or use "
                  "`health-agent labs` / `health-agent metric` directly.",
                  file=sys.stderr)
        return 0
    except (ollama_client.OllamaUnavailable,
            ollama_client.RemoteHostRefused) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if literature_conn is not None:
            literature_conn.close()
        conn.close()


def _fmt_args(arguments: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in arguments.items())


def cmd_notes(args: argparse.Namespace, cfg: config.Config) -> int:
    conn = sqlite_schema.open_for_read(cfg.index_path)
    try:
        if args.tags:
            tags = queries.list_tags(conn, limit=args.limit)
            if args.json:
                print(json.dumps(tags, indent=2))
                return 0
            if not tags:
                print("No tags found. Notes pick up tags from frontmatter "
                      "(`tags: [sleep, headache]`) and inline #hashtags.")
                return 1
            rows = [[t["tag"], f"{t['n']:,}", t["first"] or "-", t["last"] or "-"]
                    for t in tags]
            print(_table(["tag", "notes", "first", "last"], rows))
            return 0

        if args.tag:
            found = queries.notes_by_tag(conn, args.tag)
            if args.json:
                print(json.dumps(found, indent=2))
                return 0 if found else 1
            if not found:
                print(f"No notes tagged {args.tag!r}.")
                print("List what exists with `health-agent notes --tags`.")
                return 1
            rows = [[Path(n["path"]).name, n["doc_date"] or "-",
                     (n["title"] or "")[:50]] for n in found]
            print(_table(["file", "date", "title"], rows))
            return 0

        found = queries.list_documents(conn, kind="note")
        if args.json:
            print(json.dumps(found, indent=2))
            return 0 if found else 1
        if not found:
            print("No notes in this index.")
            print(f"Put markdown or text files in {cfg.notes_dir} and run "
                  f"`health-agent ingest`.")
            return 1
        rows = [[
            Path(n["path"]).name,
            n["doc_date"] or "-",
            f"{n['chunks']:,}",
            (n["tags"] or "-")[:34],
            (n["title"] or "")[:38],
        ] for n in found]
        print(_table(["file", "date", "chunks", "tags", "title"], rows))

        summary = queries.note_summary(conn)
        if summary["undated"]:
            print(f"\n{summary['undated']} note(s) have no date. Add a "
                  f"`date:` to the frontmatter or name the file "
                  f"`YYYY-MM-DD-something.md` to place them in time.")
        return 0
    finally:
        conn.close()


def cmd_labs(args: argparse.Namespace, cfg: config.Config) -> int:
    conn = sqlite_schema.open_for_read(cfg.index_path)
    try:
        if not args.analyte:
            return _list_analytes(conn, args)

        key, _ = labs.resolve_key(args.analyte)
        trend = queries.lab_trend(conn, key, start=getattr(args, "from"),
                                  end=args.to)

        if args.json:
            print(json.dumps(_trend_as_dict(trend), indent=2))
            return 0 if trend.points else 1

        if trend.is_empty:
            return _report_missing_lab(conn, args, key, trend)

        print(f"{trend.label} ({key})")
        print()
        rows = []
        for point in trend.points:
            value = (f"{point.value_num:g}" if point.value_num is not None
                     else (point.value_text or "-"))
            marker = " *" if point.out_of_range is True else ""
            rows.append([
                point.collected_date or "unknown",
                value + marker,
                point.unit or "-",
                point.ref_text or "-",
                point.flag or "-",
                point.citation + (" [OCR]" if point.via_ocr else ""),
            ])
        print(_table(["collected", "value", "unit", "reference", "flag",
                      "source"], rows))

        if any(p.out_of_range for p in trend.points):
            print("\n* outside the reference range printed on that report")

        if any(p.via_ocr for p in trend.points):
            print("\n[OCR] read from a scanned page with no text layer. OCR "
                  "misreads digits — a reference range of 100-199 can arrive "
                  "as 100-139 — so check these against the original before "
                  "relying on them.")

        numeric = [p.value_num for p in trend.points if p.value_num is not None]
        if len(numeric) > 1:
            first, last = numeric[0], numeric[-1]
            delta = last - first
            direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
            print(f"\nfirst {first:g} -> latest {last:g} "
                  f"({direction} {abs(delta):g}"
                  + (f", {abs(delta) / first * 100:.1f}%" if first else "") + ")")

        if len(trend.units_seen) > 1:
            print(f"\nNote: mixed units across reports "
                  f"({', '.join(trend.units_seen)}). Values are shown as printed "
                  f"and are NOT converted — compare with care.")

        print("\nThese are the numbers as printed on your reports. "
              "Interpretation is for you and your clinician.")
        return 0
    finally:
        conn.close()


def _list_analytes(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    found = queries.list_analytes(conn, search=args.search, limit=args.limit)
    if args.json:
        print(json.dumps(found, indent=2))
        return 0
    if not found:
        print("No lab results in this index."
              if not args.search else
              f"No lab results matching {args.search!r}.")
        return 1
    rows = [[
        a["analyte_key"],
        (a["printed_as"] or "")[:30],
        a["panel"] or "-",
        f"{a['n']:,}",
        a["first"] or "-",
        a["last"] or "-",
    ] for a in found]
    print(_table(["analyte", "printed as", "panel", "n", "first", "last"], rows))
    print(f"\n{len(found)} analyte(s). "
          f"Show one over time with `health-agent labs <analyte>`.")
    return 0


def _report_missing_lab(conn: sqlite3.Connection, args: argparse.Namespace,
                        key: str, trend: queries.LabTrend) -> int:
    """Missing-data behavior (plan §5a) for lab values."""
    window = ""
    if getattr(args, "from") or args.to:
        window = (f" between {getattr(args, 'from') or 'the earliest report'} "
                  f"and {args.to or 'now'}")

    if trend.total_results:
        print(f"I have {trend.total_results} {trend.label} result(s), but none"
              f"{window}.")
        print(f"Available range: {trend.available_from} to {trend.available_to}.")
        print("\nWant to widen the window?")
        return 1

    print(f"I don't have any {trend.label} results in this index.")
    close = labs.search(args.analyte)
    if close:
        print("\nAnalytes I know how to normalize:")
        for analyte in close[:5]:
            print(f"  {analyte.key:<22} {analyte.label}")
    present = queries.list_analytes(conn, search=args.analyte, limit=5)
    if present:
        print("\nSimilar analytes that ARE in your reports:")
        for a in present:
            print(f"  {a['analyte_key']:<22} {a['n']} result(s)")
    docs = conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"]
    if not docs:
        print(f"\nNo reports are ingested yet. Put PDFs in your records folder "
              f"and run `health-agent ingest`.")
    else:
        print("\nIs this on a report you haven't added yet, or was it not part "
              "of the panels you've had run?")
    return 1


def _trend_as_dict(trend: queries.LabTrend) -> dict:
    return {
        "analyte_key": trend.key,
        "label": trend.label,
        "total_results": trend.total_results,
        "available_from": trend.available_from,
        "available_to": trend.available_to,
        "units_seen": list(trend.units_seen),
        "points": [
            {
                "collected_date": p.collected_date,
                "value": p.value_num,
                "value_text": p.value_text,
                "unit": p.unit,
                "ref_low": p.ref_low,
                "ref_high": p.ref_high,
                "ref_text": p.ref_text,
                "flag": p.flag,
                "out_of_range": p.out_of_range,
                "via_ocr": p.via_ocr,
                "citation": p.citation,
                "source_path": p.path,
                "page_no": p.page_no,
            }
            for p in trend.points
        ],
    }


def cmd_search(args: argparse.Namespace, cfg: config.Config) -> int:
    conn = sqlite_schema.open_for_read(cfg.index_path)
    try:
        hits: list[vector_store.SearchHit] = []
        note = ""

        if not args.keyword:
            try:
                embedder = embeddings.get_embedder(args.embedder)
                embedded = conn.execute(
                    "SELECT COUNT(*) AS n FROM chunk WHERE embedded_with = ?",
                    (embedder.name,),
                ).fetchone()["n"]
                if embedded:
                    store = vector_store.VectorStore(cfg.vector_path)
                    hits = vector_store.semantic_search(
                        conn, store, embedder, args.query, limit=args.limit,
                        kind=args.kind)
                else:
                    note = (f"No chunks are embedded with {embedder.name} yet, "
                            f"so this is a keyword search. Run "
                            f"`health-agent embed` for semantic search.")
            except (embeddings.EmbeddingUnavailable,
                    embeddings.RemoteHostRefused) as exc:
                note = f"Semantic search unavailable ({exc}). Falling back to keywords."

        if not hits:
            hits = vector_store.keyword_search(conn, args.query,
                                               limit=args.limit, kind=args.kind)

        if args.json:
            print(json.dumps([{
                "citation": h.citation, "score": h.score, "method": h.method,
                "kind": h.kind, "section": h.section, "page_no": h.page_no,
                "path": h.path, "text": h.text,
            } for h in hits], indent=2))
            return 0 if hits else 1

        if note:
            print(f"Note: {note}\n")

        if not hits:
            total = conn.execute("SELECT COUNT(*) AS n FROM chunk").fetchone()["n"]
            if not total:
                print("No document text is indexed yet. Add PDFs or notes to "
                      "your data folder and run `health-agent ingest`.")
                return 1

            print(f"Nothing in your {total:,} indexed chunk(s) matched "
                  f"{args.query!r}.")
            # Frontmatter is stripped before chunking, so a tag is not body
            # text and won't match here. If the query names one, say so rather
            # than leaving the user to conclude the note isn't indexed.
            matched_tags = _tags_matching(conn, args.query)
            if matched_tags:
                print(f"\n{', '.join(matched_tags)} "
                      f"{'is a tag' if len(matched_tags) == 1 else 'are tags'} "
                      f"on your notes, not text inside them. Try:")
                for tag in matched_tags[:3]:
                    print(f"  health-agent notes --tag {tag}")
            else:
                print("Try different wording, or `health-agent labs` for "
                      "structured lab values.")
            return 1

        for index, hit in enumerate(hits, start=1):
            # :.4g rather than a fixed 3 decimals: bm25 scores for weak matches
            # are ~1e-6 and would all render as "0.000", making a correctly
            # ranked result list look broken.
            print(f"{index}. {hit.citation}   [{hit.method}, score {hit.score:.4g}]")
            snippet = " ".join(hit.text.split())
            print(f"   {snippet[:400]}{'...' if len(snippet) > 400 else ''}")
            print()
        return 0
    finally:
        conn.close()


def _tags_matching(conn: sqlite3.Connection, query: str) -> list[str]:
    """Tags whose name appears among the query's words."""
    import re

    words = {w.lower() for w in re.findall(r"[A-Za-z0-9/-]{2,}", query)}
    if not words:
        return []
    return [
        row["tag"] for row in conn.execute("SELECT DISTINCT tag FROM document_tag")
        if row["tag"] in words
    ]


def cmd_embed(args: argparse.Namespace, cfg: config.Config) -> int:
    conn = sqlite_schema.open_for_read(cfg.index_path)
    try:
        count = _embed_chunks(conn, cfg, args.embedder)
        if not count:
            print("Nothing to embed; every chunk is already embedded.")
        return 0
    except (embeddings.EmbeddingUnavailable, embeddings.RemoteHostRefused):
        return 2
    finally:
        conn.close()


def cmd_literature_build(args: argparse.Namespace, cfg: config.Config) -> int:
    from .literature import corpus as lit_corpus
    from .literature import embed as lit_embed
    from .literature import medline, schema as lit_schema

    source = Path(args.source).expanduser()
    if not source.is_file():
        print(f"No such file: {source}", file=sys.stderr)
        return 2

    try:
        articles = medline.parse_articles(source.read_bytes())
    except medline.MedlineParseError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    conn = lit_schema.connect(cfg.literature_path, create=True)
    try:
        lit_schema.initialize(conn)
        stats = lit_corpus.build(conn, articles, slug=args.slug,
                                 version=args.version, license=args.license)
        print(f"{stats.articles} articles, {stats.chunks} chunks"
              f"{f', {stats.skipped} skipped (no abstract)' if stats.skipped else ''}")

        if not args.no_embed:
            embedder = embeddings.get_embedder(args.embed_backend)
            store = vector_store.VectorStore(cfg.literature_vector_path,
                                             table_name=lit_embed.TABLE_NAME)
            try:
                done = lit_embed.embed_corpus(conn, store, embedder)
                print(f"embedded {done} chunks with {embedder.name}")
            except (embeddings.EmbeddingUnavailable, embeddings.RemoteHostRefused):
                print("Ollama unavailable; corpus search will use keyword "
                      "matching until you run this again.", file=sys.stderr)
        return 0
    finally:
        conn.close()


def cmd_literature_status(args: argparse.Namespace, cfg: config.Config) -> int:
    from .literature import corpus as lit_corpus
    from .literature import schema as lit_schema

    try:
        conn = lit_schema.connect(cfg.literature_path)
    except lit_schema.CorpusNotFound as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        report = lit_corpus.coverage(conn)
        print(f"packs:    {', '.join(report['packs']) or '(none)'}")
        print(f"articles: {report['article_count']}")
        print(f"years:    {report['year_range'][0]}-{report['year_range'][1]}")
        print(f"built:    {report['built']}")
        print("tiers:    " + ", ".join(f"{k}={v}" for k, v in report["tiers"].items()))
        print("topics:   " + ", ".join(report["topics"][:10]))
        return 0
    finally:
        conn.close()


def cmd_offline_check(args: argparse.Namespace, cfg: config.Config) -> int:
    """Prove the local tier makes no outbound connections (plan §5)."""
    data_dir = Path(args.data).expanduser() if args.data else _fixture_dir()
    if data_dir is None or not data_dir.exists():
        print(f"error: no data to check against ({args.data or 'fixtures'})",
              file=sys.stderr)
        return 2

    print("Running the local pipeline with network access denied.")
    print(f"  data      {data_dir}")

    result = offline_check.run(data_dir, force_mechanism=args.mechanism)

    label = {
        "os-sandbox": "OS sandbox — the kernel denies every socket syscall, "
                      "including from native code",
        "python": "Python-level — socket patched to raise in the child; "
                  "covers this codebase, not native libraries",
        "none": "none",
    }.get(result.mechanism, result.mechanism)
    print(f"  enforced  {label}")
    print(f"            {result.mechanism_detail}\n")

    for step in result.steps:
        mark = "ok  " if step["ok"] else "FAIL"
        detail = "" if step["detail"] is None else f"   {step['detail']}"
        print(f"  [{mark}] {step['step']}{detail}")

    print()
    if result.error:
        print(f"FAILED: {result.error}", file=sys.stderr)
        if result.stderr_tail:
            print(f"\n{result.stderr_tail}", file=sys.stderr)
        # A network attempt surfaces here: the syscall is denied, the step
        # raises, and the run does not complete.
        return 1

    if result.mechanism == "os-sandbox":
        print("PASS — the whole local pipeline ran to completion with every "
              "network syscall denied at the kernel level.")
    else:
        print("PASS — the local pipeline ran to completion with Python "
              "networking disabled.")
        print("\nNote: this is the weaker of the two checks. Install/enable an "
              "OS sandbox (macOS has `sandbox-exec`; Linux needs `unshare`) "
              "to also cover native code.")
    print("\nThis proves the LOCAL tier only. `ask --cloud` sends data to "
          "Anthropic by design — see THREAT_MODEL.md.")
    return 0


def _fixture_dir() -> Path | None:
    candidate = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
    return candidate if candidate.exists() else None


def cmd_cloud_consent(args: argparse.Namespace, cfg: config.Config) -> int:
    if args.show_notice:
        print(consent.NOTICE)
        return 0

    if args.revoke:
        if consent.revoke(cfg.index_dir):
            print("Cloud consent withdrawn. The full notice will be shown "
                  "again before the next `--cloud` query.")
            return 0
        print("No cloud consent was recorded; nothing to revoke.")
        return 1

    record = consent.load(cfg.index_dir)
    if record is None:
        print("Cloud tier: not consented.")
        print("Everything runs locally. `health-agent ask --cloud` shows the "
              "full disclosure and asks once before sending anything.")
        return 0

    print(f"Cloud tier: consented {record.granted_at}")
    print(f"  model at the time   {record.model}")
    print(f"  recorded in         {consent.consent_path(cfg.index_dir)}")
    if not record.is_current():
        print("\nWhat cloud mode sends has changed since you agreed. The "
              "notice will be shown again before the next `--cloud` query.")
    print("\nWithdraw with `health-agent cloud-consent --revoke`.")
    print("Re-read the disclosure with `--show-notice`.")
    return 0


def cmd_doctor(args: argparse.Namespace, cfg: config.Config) -> int:
    """Environment check (plan §5a): report what works and what doesn't."""
    ok = True
    print("health-agent doctor\n")

    print(f"data folder    {cfg.data_dir}"
          f"{'' if cfg.data_dir.is_dir() else '   (does not exist yet)'}")
    print(f"index          {cfg.index_path}"
          f"{'' if cfg.index_path.exists() else '   (not created yet)'}")

    if cfg.index_path.exists():
        try:
            conn = sqlite_schema.open_for_read(cfg.index_path)
            summary = queries.index_summary(conn)
            docs = queries.document_summary(conn)
            print(f"               {summary['records']:,} HealthKit records, "
                  f"{docs['documents']} document(s), "
                  f"{docs['lab_results']} lab value(s), "
                  f"{docs['chunks_embedded']}/{docs['chunks']} chunks embedded")
            conn.close()
        except (sqlite_schema.SchemaVersionMismatch,
                sqlite_schema.AggregatesMissing) as exc:
            print(f"               PROBLEM: {exc}")
            ok = False

    free_gb = shutil.disk_usage(
        cfg.index_dir if cfg.index_dir.exists() else Path.home()
    ).free / 1e9
    disk_note = "" if free_gb >= 10 else "   (low — a large export needs several GB)"
    print(f"free disk      {free_gb:,.1f} GB{disk_note}")

    print("\nOCR (for scanned records)")
    if records.ocr_available():
        print("  Tesseract      available")
    else:
        print("  Tesseract      NOT available — scanned PDFs will be skipped")
        print("                 macOS: brew install tesseract")
        print("                 Debian/Ubuntu: apt install tesseract-ocr")

    print("\nLocal model (Ollama)")
    try:
        embedder = embeddings.OllamaEmbedder()
        print(f"  host           {embedder.host}")
        print(f"  embeddings     {embedder.health_check()}")
    except embeddings.RemoteHostRefused as exc:
        print(f"  PROBLEM        {exc}")
        ok = False
    except embeddings.EmbeddingUnavailable as exc:
        print(f"  embeddings     NOT available")
        print(f"                 {exc}")
        print(f"                 Semantic search is disabled until this works; "
              f"keyword search still runs.")

    print("\nNetwork posture")
    print("  This build makes no outbound calls except to the Ollama host above,")
    print("  which is refused unless it is loopback. Ingestion and all queries")
    print("  are pure local computation.")

    print(f"\n{'All good.' if ok else 'Some checks reported problems (above).'}")
    return 0 if ok else 1


def cmd_reset(args: argparse.Namespace, cfg: config.Config) -> int:
    """Wipe the index. Interactive confirmation by default (plan §5a)."""
    if not cfg.index_path.exists():
        print(f"No index at {cfg.index_path}; nothing to reset.")
        return 0

    size_mb = cfg.index_path.stat().st_size / 1e6
    if cfg.vector_path.exists():
        size_mb += sum(f.stat().st_size for f in cfg.vector_path.rglob("*")
                       if f.is_file()) / 1e6
    if not args.yes:
        print(f"This deletes the local index at {cfg.index_path} "
              f"and its vector store ({size_mb:,.1f} MB total).")
        print("Your source files in your data folder are NOT touched; the index "
              "can be rebuilt with `health-agent ingest`.")
        try:
            answer = input("Delete the index? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in {"y", "yes"}:
            print("Cancelled.")
            return 1

    cfg.index_path.unlink()
    for suffix in ("-wal", "-shm"):
        cfg.index_path.with_name(cfg.index_path.name + suffix).unlink(missing_ok=True)
    if cfg.vector_path.exists():
        shutil.rmtree(cfg.vector_path)
    print(f"Deleted {cfg.index_path} and its vector store")
    if cfg.literature_path.exists():
        print("Your literature corpus was not touched; "
              "use `health-agent literature build` to replace it.")
    return 0


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="health-agent",
        description="Local-first queries over your own health data. "
                    "Milestone 1: HealthKit ingestion and aggregate queries.",
    )
    parser.add_argument("--version", action="version", version=f"health-agent {__version__}")
    parser.add_argument("--data-dir", help="data folder (default: ~/HealthData, "
                                           "or $HEALTH_AGENT_DATA_DIR)")
    parser.add_argument("--index", help="path to the SQLite index "
                                        "(default: <data-dir>/.index/health.db)")
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser(
        "ingest", help="parse health exports and record PDFs into the index",
        description="With no path, ingests <data-dir>/healthkit and "
                    "<data-dir>/records. With a path, ingests that export, PDF, "
                    "or folder.",
    )
    p_ingest.add_argument("path", nargs="?",
                          help="export.xml/.zip, a record PDF, or a folder")
    p_ingest.add_argument("--rebuild", action="store_true",
                          help="delete the index and parse from scratch")
    p_ingest.add_argument("--force", action="store_true",
                          help="re-parse even if the file hash is unchanged")
    p_ingest.add_argument("--no-ocr", action="store_true",
                          help="skip OCR for pages with no text layer")
    p_ingest.add_argument("--no-embed", action="store_true",
                          help="store chunks without embedding them")
    p_ingest.add_argument("--embedder", default="ollama",
                          choices=("ollama", "hashing"),
                          help="embedding backend (default: ollama)")
    p_ingest.set_defaults(func=cmd_ingest)

    p_stats = sub.add_parser("stats", help="what's in the index")
    p_stats.add_argument("--json", action="store_true")
    p_stats.set_defaults(func=cmd_stats)

    p_types = sub.add_parser("types", help="list HealthKit types present in the index")
    p_types.add_argument("--search", help="substring filter on the type identifier")
    p_types.add_argument("--limit", type=int, default=40)
    p_types.add_argument("--json", action="store_true")
    p_types.set_defaults(func=cmd_types)

    p_metric = sub.add_parser(
        "metric", help="aggregate one metric over time",
        description="Aggregate a metric. The default aggregation follows the "
                    "metric's HealthKit semantics: cumulative types (steps, "
                    "energy) are summed, discrete types (heart rate, weight) are "
                    "averaged, category types (sleep) are totalled by duration.",
    )
    p_metric.add_argument("name", help="alias (e.g. resting-hr, steps, sleep) or "
                                       "full HealthKit identifier")
    p_metric.add_argument("--by", choices=queries.PERIODS, default="day")
    p_metric.add_argument("--agg", choices=queries.AGGREGATIONS,
                          help="override the metric's default aggregation")
    p_metric.add_argument("--from", dest="from", metavar="YYYY-MM-DD")
    p_metric.add_argument("--to", metavar="YYYY-MM-DD")
    p_metric.add_argument("--source", help="restrict to one recording source")
    p_metric.add_argument("--combine", choices=("auto", "max", "sum"), default="auto",
                          help="how to combine additive totals across sources "
                               "(default: auto -> max, avoiding double-counting)")
    p_metric.add_argument("--limit", type=int, default=40,
                          help="show only the most recent N periods (0 = all)")
    p_metric.add_argument("--show-sources", action="store_true")
    p_metric.add_argument("--json", action="store_true")
    p_metric.set_defaults(func=cmd_metric)

    p_sleep = sub.add_parser(
        "sleep", help="sleep by night, with stages broken out",
        description="Groups sleep into nights rather than calendar dates, so a "
                    "night that runs past midnight stays one row.",
    )
    p_sleep.add_argument("--from", dest="from", metavar="YYYY-MM-DD")
    p_sleep.add_argument("--to", metavar="YYYY-MM-DD")
    p_sleep.add_argument("--limit", type=int, default=30)
    p_sleep.add_argument("--json", action="store_true")
    p_sleep.set_defaults(func=cmd_sleep)

    p_workouts = sub.add_parser("workouts", help="workout totals by activity type")
    p_workouts.add_argument("--from", dest="from", metavar="YYYY-MM-DD")
    p_workouts.add_argument("--to", metavar="YYYY-MM-DD")
    p_workouts.add_argument("--limit", type=int, default=20)
    p_workouts.add_argument("--json", action="store_true")
    p_workouts.set_defaults(func=cmd_workouts)

    p_docs = sub.add_parser("documents", help="list ingested record documents")
    p_docs.add_argument("--json", action="store_true")
    p_docs.set_defaults(func=cmd_documents)

    p_ask = sub.add_parser(
        "ask", help="ask a question about your data in plain language",
        description="Answered by a local model using the same query tools the "
                    "other commands use. Nothing leaves your machine.",
    )
    p_ask.add_argument("question")
    p_ask.add_argument("--cloud", action="store_true",
                       help="HYBRID MODE: answer using Anthropic's API instead "
                            "of the local model. Sends your question and the "
                            "retrieved data off this machine. Asks once before "
                            "the first use.")
    p_ask.add_argument("--yes", action="store_true",
                       help="skip the one-time cloud confirmation (for scripts)")
    p_ask.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"),
                       help="cloud only: reasoning effort")
    p_ask.add_argument("--model",
                       help=f"model name (local default: "
                            f"{ollama_client.DEFAULT_CHAT_MODEL}; cloud default: "
                            f"{agent.backends.DEFAULT_CLOUD_MODEL})")
    p_ask.add_argument("--think", action="store_true",
                       help="enable the model's reasoning mode (slower)")
    p_ask.add_argument("--max-steps", type=int,
                       default=agent.orchestrator.DEFAULT_MAX_STEPS,
                       help="maximum tool-calling rounds")
    p_ask.add_argument("--show-tools", action="store_true",
                       help="list the tool calls that produced the answer")
    p_ask.add_argument("--embedder", default="ollama",
                       choices=("ollama", "hashing"))
    p_ask.add_argument("--quiet", action="store_true",
                       help="suppress progress output")
    p_ask.add_argument("--json", action="store_true")
    p_ask.set_defaults(func=cmd_ask)

    p_notes = sub.add_parser(
        "notes", help="list ingested notes, or browse them by tag",
    )
    p_notes.add_argument("--tags", action="store_true",
                         help="list tags instead of notes")
    p_notes.add_argument("--tag", help="show notes carrying one tag")
    p_notes.add_argument("--limit", type=int, default=50)
    p_notes.add_argument("--json", action="store_true")
    p_notes.set_defaults(func=cmd_notes)

    p_labs = sub.add_parser(
        "labs", help="list lab analytes, or show one over time",
        description="With no analyte, lists what's been extracted from your "
                    "reports. With one, shows every result over time, each with "
                    "the file and page it came from.",
    )
    p_labs.add_argument("analyte", nargs="?",
                        help="analyte name or key (e.g. ldl, hba1c, tsh)")
    p_labs.add_argument("--search", help="filter the analyte list")
    p_labs.add_argument("--from", dest="from", metavar="YYYY-MM-DD")
    p_labs.add_argument("--to", metavar="YYYY-MM-DD")
    p_labs.add_argument("--limit", type=int, default=60)
    p_labs.add_argument("--json", action="store_true")
    p_labs.set_defaults(func=cmd_labs)

    p_search = sub.add_parser(
        "search", help="search the text of your records",
        description="Semantic search when chunks are embedded, keyword search "
                    "otherwise. The method used is always shown.",
    )
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=5)
    p_search.add_argument("--keyword", action="store_true",
                          help="force keyword search, skip embeddings")
    p_search.add_argument("--kind", choices=("pdf", "note"),
                          help="restrict to records (pdf) or notes")
    p_search.add_argument("--embedder", default="ollama",
                          choices=("ollama", "hashing"))
    p_search.add_argument("--json", action="store_true")
    p_search.set_defaults(func=cmd_search)

    p_embed = sub.add_parser("embed", help="embed chunks that aren't embedded yet")
    p_embed.add_argument("--embedder", default="ollama",
                         choices=("ollama", "hashing"))
    p_embed.set_defaults(func=cmd_embed)

    p_doctor = sub.add_parser("doctor", help="check the environment and index")
    p_doctor.set_defaults(func=cmd_doctor)

    p_offline = sub.add_parser(
        "offline-check", help="prove the local tier makes no network calls",
        description="Runs ingest, aggregation, search, and the agent's tools "
                    "inside a sandbox with network access denied, and reports "
                    "which enforcement mechanism applied. Uses the synthetic "
                    "fixtures unless --data points elsewhere.",
    )
    p_offline.add_argument("--data", help="folder to run the proof against "
                                          "(default: the bundled fixtures)")
    p_offline.add_argument("--mechanism", choices=("os-sandbox", "python"),
                           help="force an enforcement mechanism instead of "
                                "using the strongest available")
    p_offline.set_defaults(func=cmd_offline_check)

    p_consent = sub.add_parser(
        "cloud-consent", help="show or revoke consent for the cloud tier",
        description="Cloud mode requires a one-time confirmation. This shows "
                    "what was agreed to and when, and can withdraw it.",
    )
    p_consent.add_argument("--revoke", action="store_true",
                           help="withdraw consent; the notice is shown again "
                                "before the next cloud query")
    p_consent.add_argument("--show-notice", action="store_true",
                           help="print the full disclosure and exit")
    p_consent.set_defaults(func=cmd_cloud_consent)

    literature = sub.add_parser(
        "literature", help="manage the local medical literature corpus")
    lit_sub = literature.add_subparsers(dest="literature_command", required=True)

    p_lit_build = lit_sub.add_parser("build", help="build a corpus from MEDLINE XML")
    p_lit_build.add_argument("--from", dest="source", required=True,
                             help="path to a MEDLINE PubmedArticleSet XML file")
    p_lit_build.add_argument("--slug", default="local")
    p_lit_build.add_argument("--version", default="1")
    p_lit_build.add_argument("--license", default="abstract-only",
                             help="license recorded on every article in this pack")
    p_lit_build.add_argument("--embed-backend", default="ollama",
                             choices=["ollama", "hashing"])
    p_lit_build.add_argument("--no-embed", action="store_true")
    p_lit_build.set_defaults(func=cmd_literature_build)

    p_lit_status = lit_sub.add_parser("status", help="what the corpus holds")
    p_lit_status.set_defaults(func=cmd_literature_status)

    p_reset = sub.add_parser("reset", help="delete the local index")
    p_reset.add_argument("--yes", action="store_true",
                         help="skip the confirmation prompt (for scripting)")
    p_reset.set_defaults(func=cmd_reset)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure(verbose=args.verbose)
    cfg = config.resolve(args.data_dir, args.index)

    try:
        return args.func(args, cfg)
    except (sqlite_schema.IndexNotFound, sqlite_schema.SchemaVersionMismatch,
            sqlite_schema.AggregatesMissing) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except healthkit.MalformedExport as exc:
        print(f"error: could not parse the export: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
