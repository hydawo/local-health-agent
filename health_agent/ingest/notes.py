"""Ingest personal notes: markdown and plain text.

Pipeline per file (plan §3.3):

    .md / .txt -> frontmatter (date, tags)  -> document + document_tag
               -> body split on headings    -> chunk (with a section trail)

Notes are stored as `document` rows of kind `'note'`, sharing the tables records
use. That is deliberate: `search` should return the relevant thing regardless of
whether it came from a lab PDF or something you wrote, and a parallel set of
tables would mean two retrieval paths to keep in sync. What differs is the
citation — a note has no page numbers, so chunks carry the heading trail they
came from and cite as "sleep-log.md > Week of March 10".

**Notes are never parsed for lab values.** "My LDL was 112" in a journal entry is
recollection, not a lab result, and letting prose write into `lab_result` would
put uncited numbers into trends that are supposed to be traceable to a report.

**Frontmatter parsing handles the common subset, not all of YAML.** Scalars,
quoted strings, inline `[a, b]` lists, and block `- item` lists cover what note
frontmatter actually contains. Anything more complex is preserved raw in
`frontmatter_json` under a `_unparsed` key rather than guessed at — a note is
still fully ingested and searchable even when its frontmatter isn't understood.
Adding a YAML dependency for the remainder is a reasonable future call; silently
mis-reading a date or a tag list is not.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..logging_setup import get_logger
from .records import chunk_text

log = get_logger("ingest.notes")

SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt"}

# Common frontmatter keys that mean "when this note is about".
DATE_KEYS = ("date", "created", "created_at", "day", "datetime", "timestamp")
TITLE_KEYS = ("title", "name")
TAG_KEYS = ("tags", "tag", "keywords", "topics")

# A date at the start of a filename: 2026-03-14-headache.md, 2026-03-14.md
_FILENAME_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")

# Inline #tags. Requires a letter first so "#1" and "#404" aren't tags, and
# excludes markdown headings, which are handled by the section splitter.
_INLINE_TAG = re.compile(r"(?:^|\s)#([A-Za-z][\w/-]{1,40})")

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
                          re.DOTALL)


@dataclass
class NoteStats:
    """Counts only — never note content (plan §5a logging policy)."""

    notes: int = 0
    chunks: int = 0
    tagged: int = 0
    dated: int = 0
    undated: int = 0
    unparsed_frontmatter: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


# --------------------------------------------------------------------------- #
# Frontmatter
# --------------------------------------------------------------------------- #

def _scalar(raw: str):
    """Interpret a frontmatter scalar, conservatively."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if text.lower() in {"null", "~", ""}:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split leading `---` frontmatter from the body.

    Returns (metadata, body). Keys the parser can't handle are collected under
    `_unparsed` so they're visible rather than lost.
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text

    block, body = match.group(1), text[match.end():]
    meta: dict = {}
    unparsed: list[str] = []
    current_list_key: str | None = None

    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        # Continuation of a block list: "  - value"
        if current_list_key and re.match(r"^\s*-\s+", line):
            meta[current_list_key].append(_scalar(re.sub(r"^\s*-\s+", "", line)))
            continue
        current_list_key = None

        key_value = re.match(r"^([A-Za-z_][\w .-]*)\s*:\s*(.*)$", line)
        if not key_value:
            unparsed.append(line)
            continue

        key, value = key_value.group(1).strip().lower(), key_value.group(2).strip()

        if not value:                       # a block list follows
            meta[key] = []
            current_list_key = key
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            meta[key] = [_scalar(v) for v in inner.split(",") if v.strip()] if inner else []
        elif value.startswith("{"):
            unparsed.append(line)           # nested maps: out of scope, kept raw
        else:
            meta[key] = _scalar(value)

    # Drop empty block lists that were actually just empty keys.
    meta = {k: v for k, v in meta.items() if not (isinstance(v, list) and not v)}
    if unparsed:
        meta["_unparsed"] = unparsed
    return meta, body


def extract_date(meta: dict, path: Path) -> str | None:
    """A note's date: frontmatter first, then a date at the start of the filename.

    Deliberately does **not** fall back to file mtime: copying a folder rewrites
    mtimes, which would silently re-date years of notes to the day they were
    moved. An undated note is reported as undated.
    """
    for key in DATE_KEYS:
        value = meta.get(key)
        if value is None:
            continue
        parsed = _coerce_date(str(value))
        if parsed:
            return parsed

    match = _FILENAME_DATE.match(path.stem)
    if match:
        candidate = "-".join(match.groups())
        if _coerce_date(candidate):
            return candidate
    return None


def _coerce_date(raw: str) -> str | None:
    text = raw.strip().strip("\"'")
    # Trim a time component: "2026-03-14 08:30" / "2026-03-14T08:30:00Z"
    text = re.split(r"[T ]", text, maxsplit=1)[0]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d-%m-%Y",
                "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def extract_tags(meta: dict, body: str) -> list[str]:
    """Tags from frontmatter plus inline #hashtags, normalized and deduped."""
    found: list[str] = []
    for key in TAG_KEYS:
        value = meta.get(key)
        if isinstance(value, list):
            found.extend(str(v) for v in value if v is not None)
        elif isinstance(value, str):
            # "tags: sleep, headache" or "tags: sleep headache"
            found.extend(re.split(r"[,\s]+", value))

    found.extend(_INLINE_TAG.findall(body))

    seen: dict[str, None] = {}
    for tag in found:
        cleaned = tag.strip().lstrip("#").strip().lower()
        if cleaned and len(cleaned) <= 60:
            seen.setdefault(cleaned, None)
    return list(seen)


def extract_title(meta: dict, body: str, path: Path) -> str:
    """Title: frontmatter, then the first heading, then the filename."""
    for key in TITLE_KEYS:
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    for line in body.splitlines():
        heading = _HEADING.match(line)
        if heading and heading.group(2).strip():
            return heading.group(2).strip()[:120]
        if line.strip():
            break
    return path.stem


# --------------------------------------------------------------------------- #
# Sectioning
# --------------------------------------------------------------------------- #

def split_sections(body: str) -> list[tuple[str | None, str]]:
    """Split markdown into (heading trail, text) pairs.

    The trail is the chain of enclosing headings, so a chunk taken from deep in a
    note still says where it came from: "Sleep > Week of March 10". Text before
    the first heading gets a None trail rather than being attached to a heading
    it isn't under.
    """
    sections: list[tuple[str | None, str]] = []
    stack: list[tuple[int, str]] = []
    current: list[str] = []
    trail: str | None = None
    in_fence = False

    def flush() -> None:
        text = "\n".join(current).strip()
        if text:
            sections.append((trail, text))
        current.clear()

    for line in body.splitlines():
        # Never treat "# comment" inside a fenced code block as a heading.
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            current.append(line)
            continue

        heading = None if in_fence else _HEADING.match(line)
        if heading:
            flush()
            level, title = len(heading.group(1)), heading.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            trail = " > ".join(t for _, t in stack)
        else:
            current.append(line)

    flush()
    return sections


def chunk_note(body: str, *, target_chars: int = 1000,
               overlap_chars: int = 150) -> list[tuple[str | None, str]]:
    """Chunk a note, preserving each chunk's heading trail."""
    out: list[tuple[str | None, str]] = []
    for trail, text in split_sections(body):
        for piece in chunk_text(text, target_chars=target_chars,
                                overlap_chars=overlap_chars):
            out.append((trail, piece))
    return out


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_text(path: Path) -> str:
    """Read a note, tolerating a non-UTF-8 byte rather than losing the file."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        log.warning("%s: not valid UTF-8, decoding with replacements", path.name)
        return path.read_text(encoding="utf-8", errors="replace")


def ingest_note(conn: sqlite3.Connection, path: Path, *, source_file_id: int,
                stats: NoteStats) -> int:
    """Parse and store one note. Returns the document id."""
    raw = read_text(path)
    meta, body = parse_frontmatter(raw)

    date = extract_date(meta, path)
    tags = extract_tags(meta, body)
    title = extract_title(meta, body, path)

    if "_unparsed" in meta:
        stats.unparsed_frontmatter += 1
    stats.dated += 1 if date else 0
    stats.undated += 0 if date else 1
    stats.tagged += 1 if tags else 0

    cursor = conn.execute(
        "INSERT INTO document(source_file_id, path, kind, title, doc_date, "
        "page_count, extraction, ingested_at, frontmatter_json) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (source_file_id, str(path), "note", title, date, 1, "text", _now(),
         json.dumps(meta, sort_keys=True, default=str) if meta else None),
    )
    document_id = int(cursor.lastrowid)
    stats.notes += 1

    # One "page" holding the body, so notes and records hydrate identically in
    # search. The body excludes frontmatter: embedding YAML is noise.
    conn.execute(
        "INSERT INTO document_page(document_id, page_no, text, char_count, "
        "extraction) VALUES(?,?,?,?,?)",
        (document_id, 1, body, len(body), "text"),
    )

    if tags:
        conn.executemany(
            "INSERT OR IGNORE INTO document_tag(document_id, tag) VALUES(?,?)",
            [(document_id, tag) for tag in tags],
        )

    for index, (section, piece) in enumerate(chunk_note(body)):
        conn.execute(
            "INSERT INTO chunk(document_id, page_no, section, chunk_index, "
            "text, char_count) VALUES(?,?,?,?,?,?)",
            (document_id, 1, section, index, piece, len(piece)),
        )
        stats.chunks += 1

    conn.commit()
    return document_id


def find_notes(root: Path) -> list[Path]:
    """Every supported note under `root`, sorted for stable ordering."""
    if root.is_file():
        return [root] if root.suffix.lower() in SUPPORTED_SUFFIXES else []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
        and not p.name.startswith(".")
    )


def ingest_notes(
    conn: sqlite3.Connection,
    root: Path,
    *,
    register: Callable[[Path], tuple[int, bool]],
    force: bool = False,
    on_file: Callable[[Path], None] | None = None,
) -> NoteStats:
    """Ingest every note under `root`, skipping unchanged files."""
    from ..store import sqlite_schema

    stats = NoteStats()

    for path in find_notes(root):
        source_file_id, already = register(path)
        if already and not force:
            log.info("%s unchanged since last ingest; skipping", path.name)
            continue
        if on_file is not None:
            on_file(path)

        # Replace rather than merge: an edited note is a new version of itself,
        # and keeping both would return stale text alongside current text with
        # no way for a reader to tell which is which.
        sqlite_schema.clear_document_data(conn, source_file_id)
        before = stats.chunks
        try:
            ingest_note(conn, path, source_file_id=source_file_id, stats=stats)
        except Exception as exc:  # noqa: BLE001 - one bad note, not the folder
            log.warning("%s: skipped (%s)", path.name, type(exc).__name__)
            stats.skip(type(exc).__name__)
            continue
        sqlite_schema.mark_source_file(conn, source_file_id,
                                       record_count=stats.chunks - before)

    sqlite_schema.rebuild_chunk_fts(conn)
    return stats
