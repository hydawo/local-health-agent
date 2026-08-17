"""Ingest bloodwork and medical-record PDFs.

Pipeline per file (plan §3.2):

    PDF -> per-page text (pdfplumber)
        -> OCR fallback for pages with no extractable text (pytesseract)
        -> structured lab values  -> lab_result   (two passes: tables, then lines)
        -> chunked free text      -> chunk        (embedded separately)

**Text first, OCR only as fallback.** A digitally-generated lab PDF has a real
text layer; OCR would be slower and strictly worse. Scanned reports have no text
layer at all, and pdfplumber returns an empty string rather than failing, so the
page-level character count is what distinguishes the two. OCR is opt-out
(`--no-ocr`) and degrades to a warning when Tesseract isn't installed, because
requiring a system binary to read a text PDF would be a poor trade.

**Two extraction passes, deliberately overlapping.** Lab reports are laid out
either as ruled tables (pdfplumber's table finder gets these cleanly) or as
whitespace-aligned columns (invisible to the table finder, tractable by regex).
Real reports mix both on one page. Both passes run and write through a dedup
key, so a value found twice is stored once and a value only one pass can see is
still captured.

**Precision over recall.** A wrong lab value is worse than a missing one: a
missed value shows up as a gap the user can see, while a mis-parsed one silently
poisons a trend line. The patterns below skip anything ambiguous rather than
guessing, and every stored row keeps `raw_line` so any number can be traced back
to the exact text it came from. Better extraction with confidence scoring is
roadmap item 7.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .. import labs
from ..logging_setup import get_logger

log = get_logger("ingest.records")

# A page with fewer than this many characters of extractable text is treated as
# an image that needs OCR. Chosen above zero because scanned pages often carry a
# few characters of header/footer text stamped on by the scanner.
MIN_TEXT_CHARS = 40

OCR_DPI = 300  # below ~200 small type degrades badly; above ~400 buys nothing

# --psm 6 = "assume a single uniform block of text". The default (3, automatic)
# segments a lab table into separate column blocks, which silently decouples
# every value from its analyte name. See `ocr_page`.
OCR_CONFIG = "--psm 6"

# Tesseract reports per-word confidence; below this the word is more likely to
# be scanner noise than text, and a stray token can break a result line.
OCR_MIN_CONFIDENCE = 30.0

SUPPORTED_SUFFIXES = {".pdf"}


@dataclass
class RecordStats:
    """Counts only — no document content (plan §5a logging policy)."""

    documents: int = 0
    pages_text: int = 0
    pages_ocr: int = 0
    pages_empty: int = 0
    lab_values: int = 0
    lab_values_duplicate: int = 0
    chunks: int = 0
    unit_warnings: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    ocr_available: bool = True

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


class OcrUnavailable(RuntimeError):
    """Tesseract (or its Python binding) is not usable on this machine."""


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #

def _import_pdfplumber():
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError(
            "pdfplumber is required to ingest records. Install with "
            "`pip install -e .`"
        ) from exc
    return pdfplumber


def ocr_available() -> bool:
    """True when pytesseract and the Tesseract binary are both usable."""
    try:
        import pytesseract
    except ImportError:
        return False
    try:
        pytesseract.get_tesseract_version()
    except Exception:  # noqa: BLE001 - any failure means unusable
        return False
    return True


def ocr_page(page, dpi: int = OCR_DPI) -> tuple[str, str]:
    """Rasterize one page and OCR it. Returns (plain text, layout text).

    pdfplumber renders through its bundled pypdfium2, so this needs no external
    rasterizer (no poppler, no ImageMagick) — only the Tesseract binary itself.

    Two Tesseract behaviors have to be worked around, and both fail *silently*
    by producing text that looks fine and parses to nothing:

    1. **Default page segmentation splits tables into column blocks.** With the
       default `--psm 3`, a scanned lab report comes back as every analyte name
       in one run, then every value in another, so no value is on the same line
       as its name. `--psm 6` treats the page as one uniform block and keeps
       rows intact.

    2. **`image_to_string` collapses the column gaps** — the same problem
       pdfplumber's `extract_text()` has. So the layout text is rebuilt here
       from word bounding boxes: words are grouped into lines and placed on a
       character grid derived from the median glyph width, which restores the
       multi-space column separators the line parser needs.
    """
    try:
        import pytesseract
    except ImportError as exc:
        raise OcrUnavailable("pytesseract is not installed") from exc

    try:
        image = page.to_image(resolution=dpi).original
    except Exception as exc:  # noqa: BLE001 - rendering failures are per-page
        raise OcrUnavailable(f"page render failed: {type(exc).__name__}") from exc

    try:
        data = pytesseract.image_to_data(
            image, config=OCR_CONFIG, output_type=pytesseract.Output.DICT
        )
    except Exception as exc:  # noqa: BLE001
        raise OcrUnavailable(f"tesseract failed: {type(exc).__name__}") from exc

    return _reconstruct_ocr_text(data)


def _reconstruct_ocr_text(data: dict) -> tuple[str, str]:
    """Rebuild (plain, layout) text from Tesseract's per-word boxes."""
    words: dict[tuple[int, int, int, int], list[dict]] = {}
    widths: list[float] = []

    for index, text in enumerate(data.get("text", [])):
        if not text or not text.strip():
            continue
        try:
            confidence = float(data["conf"][index])
        except (ValueError, KeyError, IndexError):
            confidence = -1.0
        if confidence < OCR_MIN_CONFIDENCE:
            continue
        key = (data["page_num"][index], data["block_num"][index],
               data["par_num"][index], data["line_num"][index])
        entry = {"text": text.strip(), "left": int(data["left"][index]),
                 "width": int(data["width"][index])}
        words.setdefault(key, []).append(entry)
        if entry["text"]:
            widths.append(entry["width"] / len(entry["text"]))

    if not words:
        return "", ""

    # Median per-character width sets the grid the columns are laid out on.
    widths.sort()
    char_width = widths[len(widths) // 2] or 1.0

    plain_lines: list[str] = []
    layout_lines: list[str] = []
    for key in sorted(words):
        line = sorted(words[key], key=lambda w: w["left"])
        plain_lines.append(" ".join(w["text"] for w in line))

        rendered = ""
        for word in line:
            column = int(round(word["left"] / char_width))
            if column > len(rendered):
                rendered += " " * (column - len(rendered))
            elif rendered:
                rendered += " "
            rendered += word["text"]
        layout_lines.append(rendered)

    return "\n".join(plain_lines), "\n".join(layout_lines)


@dataclass
class ExtractedPage:
    page_no: int
    text: str
    extraction: str  # 'text' | 'ocr' | 'none'
    tables: list[list[list[str | None]]] = field(default_factory=list)
    # Column-preserving rendering of the same page, used only by the line pass.
    # See `extract_pages` for why both exist.
    layout_text: str = ""

    @property
    def line_source(self) -> str:
        return self.layout_text or self.text


def extract_pages(path: Path, *, use_ocr: bool = True,
                  stats: RecordStats | None = None) -> list[ExtractedPage]:
    """Extract per-page text (and tables) from a PDF, OCR-ing where needed.

    Each page is extracted **twice**, and the reason is not obvious:
    `extract_text()` collapses runs of whitespace, so a report whose columns are
    aligned with spaces comes out as "Glucose 92 mg/dL 70-99" — the column
    structure the line parser depends on is destroyed, and it silently finds
    nothing. `extract_text(layout=True)` reconstructs horizontal positions from
    the glyph coordinates and preserves the gaps.

    We keep both because they serve different consumers: the collapsed text is
    what gets chunked and embedded (layout padding would be noise in a vector),
    while the layout text is what the line pass parses.
    """
    pdfplumber = _import_pdfplumber()
    stats = stats if stats is not None else RecordStats()
    pages: list[ExtractedPage] = []

    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001 - one bad page, not the file
                log.warning("%s page %d: text extraction failed (%s)",
                            path.name, index, type(exc).__name__)
                text = ""

            tables: list[list[list[str | None]]] = []
            layout_text = ""
            extraction = "text"

            if len(text.strip()) < MIN_TEXT_CHARS:
                if use_ocr:
                    try:
                        text, layout_text = ocr_page(page)
                        extraction = "ocr"
                    except OcrUnavailable as exc:
                        stats.ocr_available = False
                        log.warning("%s page %d: OCR unavailable (%s)",
                                    path.name, index, exc)
                        extraction = "none"
                else:
                    extraction = "none"
            else:
                try:
                    layout_text = page.extract_text(layout=True) or ""
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s page %d: layout extraction failed (%s)",
                                path.name, index, type(exc).__name__)
                try:
                    tables = page.extract_tables() or []
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s page %d: table extraction failed (%s)",
                                path.name, index, type(exc).__name__)

            if not text.strip():
                extraction = "none"
                stats.pages_empty += 1
            elif extraction == "ocr":
                stats.pages_ocr += 1
            else:
                stats.pages_text += 1

            pages.append(ExtractedPage(index, text, extraction, tables,
                                       layout_text))

    return pages


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #

_DATE_LABEL = re.compile(
    r"(?:collected|collection date|date collected|drawn|specimen date|"
    r"date of service|reported|report date|date)\s*[:\-]?\s*"
    r"(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"[A-Z][a-z]{2,8} \d{1,2},? \d{4})",
    re.IGNORECASE,
)

_DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y",
    "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
)


def parse_date(raw: str) -> str | None:
    """Parse a printed date into YYYY-MM-DD, or None."""
    candidate = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def find_document_date(pages: Iterable[ExtractedPage]) -> str | None:
    """Best-effort collection date.

    Prefers an explicitly labelled date ("Collected: 03/14/2026") over a bare
    date anywhere on the page, and takes the earliest labelled match on the
    first page that has one — a report's collection date precedes its print
    date, and the collection date is what a trend should be plotted against.
    """
    for page in pages:
        found: list[str] = []
        for match in _DATE_LABEL.finditer(page.text):
            parsed = parse_date(match.group("date"))
            if parsed:
                found.append(parsed)
        if found:
            return min(found)
    return None


# --------------------------------------------------------------------------- #
# Lab value extraction
# --------------------------------------------------------------------------- #

# "70-99", "0.450 - 4.500", "<100", ">40", "3.5 to 5.1", "Not Estab."
_REF_RANGE = re.compile(
    r"^(?:(?P<low>-?\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(?P<high>-?\d+(?:\.\d+)?)"
    r"|(?P<lt><|<=|less than)\s*(?P<lt_val>-?\d+(?:\.\d+)?)"
    r"|(?P<gt>>|>=|greater than)\s*(?P<gt_val>-?\d+(?:\.\d+)?))",
    re.IGNORECASE,
)

# A whitespace-aligned result line:
#   Glucose              95      mg/dL      70-99
#   LDL Cholesterol     110  H   mg/dL      <100
# Two or more spaces separate the columns; a single space stays inside a name.
_RESULT_LINE = re.compile(
    r"^\s*(?P<analyte>[A-Za-z][A-Za-z0-9 ,()/%'.+-]{1,58}?)"
    r"\s{2,}"
    r"(?P<value>[<>]?\s*-?\d[\d,]*(?:\.\d+)?)"
    r"(?:\s+(?P<flag>H|L|A|HH|LL|High|Low|Abnormal|Critical)(?=\s|$))?"
    r"(?:\s+(?P<unit>[A-Za-zµ%][A-Za-z0-9µ%/^*.·\-]{0,18}))?"
    # One space is enough before the range: a wide unit such as "mL/min/1.73"
    # can eat the column gap, leaving a single space before ">59".
    r"(?:\s+(?P<ref>.+?))?\s*$"
)

# Lines that match the shape of a result but are not one.
_NOT_A_RESULT = re.compile(
    r"\b(page \d|phone|fax|npi|account|patient id|mrn|dob|date of birth|"
    r"ordering|physician|specimen|requisition|zip|suite|address|"
    r"copyright|all rights reserved|final report|page \d+ of \d+)\b",
    re.IGNORECASE,
)

_UNIT_LIKE = re.compile(
    r"^(?:%|ratio|mg/dl|g/dl|mmol/l|meq/l|iu/l|u/l|ng/ml|ng/dl|pg/ml|ug/dl|"
    r"µg/dl|uiu/ml|µiu/ml|miu/l|mg/l|fl|pg|x?10e?[36]/ul|k/ul|m/ul|10\*[36]/ul|"
    r"ml/min(?:/1\.73)?(?:m2)?|mmhg|units?|index|sec|ratio)$",
    re.IGNORECASE,
)


@dataclass
class LabValue:
    analyte: str
    analyte_key: str
    panel: str | None
    value_num: float | None
    value_text: str | None
    unit: str | None
    ref_low: float | None
    ref_high: float | None
    ref_text: str | None
    flag: str | None
    page_no: int
    raw_line: str
    extracted_by: str
    via_ocr: bool = False


def _to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    cleaned = raw.strip().replace(",", "").lstrip("<>=").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_reference_range(raw: str | None) -> tuple[float | None, float | None, str | None]:
    """Parse a printed reference range into (low, high, raw_text).

    One-sided ranges ("<100", ">40") set only the bound they state. Guessing the
    other end would invent a boundary the lab never printed.
    """
    if not raw:
        return None, None, None
    text = raw.strip()
    match = _REF_RANGE.match(text)
    if not match:
        return None, None, text or None
    if match.group("low") is not None:
        return _to_float(match.group("low")), _to_float(match.group("high")), text
    if match.group("lt") is not None:
        return None, _to_float(match.group("lt_val")), text
    return _to_float(match.group("gt_val")), None, text


def _looks_like_unit(candidate: str | None) -> bool:
    return bool(candidate) and bool(_UNIT_LIKE.match(candidate.strip()))


def extract_from_line(line: str, page_no: int) -> LabValue | None:
    """Parse one whitespace-aligned result line, or return None."""
    if not line.strip() or _NOT_A_RESULT.search(line):
        return None
    match = _RESULT_LINE.match(line)
    if not match:
        return None

    analyte = match.group("analyte").strip(" .:-")
    if len(analyte) < 2 or not re.search(r"[A-Za-z]{2}", analyte):
        return None
    # A name that is mostly digits is a table header or an address line.
    if sum(c.isdigit() for c in analyte) > len(analyte) / 3:
        return None

    raw_value = match.group("value").replace(" ", "")
    value_num = _to_float(raw_value)
    value_text = raw_value if value_num is None or raw_value[0] in "<>" else None

    unit = match.group("unit")
    ref_raw = match.group("ref")
    # When the "unit" column doesn't look like a unit, it is more likely the
    # start of the reference range that lost its column separator.
    if unit and not _looks_like_unit(unit):
        ref_raw = f"{unit} {ref_raw}".strip() if ref_raw else unit
        unit = None

    ref_low, ref_high, ref_text = parse_reference_range(ref_raw)
    key, panel = labs.resolve_key(analyte)

    return LabValue(
        analyte=analyte,
        analyte_key=key,
        panel=panel,
        value_num=value_num,
        value_text=value_text,
        unit=unit.strip() if unit else None,
        ref_low=ref_low,
        ref_high=ref_high,
        ref_text=ref_text,
        flag=(match.group("flag") or "").strip().upper()[:8] or None,
        page_no=page_no,
        raw_line=line.strip()[:300],
        extracted_by="line",
    )


def extract_from_table(table: list[list[str | None]], page_no: int) -> list[LabValue]:
    """Parse a pdfplumber table into lab values.

    Column roles are inferred from the header row when there is one, and from
    cell shape otherwise (first text cell is the analyte, first numeric cell is
    the value). Rows that don't yield an analyte and a value are skipped.
    """
    if not table or len(table) < 2:
        return []

    header = [(cell or "").strip().lower() for cell in table[0]]
    roles: dict[str, int] = {}
    for index, cell in enumerate(header):
        if not cell:
            continue
        if any(k in cell for k in ("test", "analyte", "component", "name")):
            roles.setdefault("analyte", index)
        elif "result" in cell or cell == "value":
            roles.setdefault("value", index)
        elif "unit" in cell:
            roles.setdefault("unit", index)
        elif "range" in cell or "reference" in cell or "interval" in cell:
            roles.setdefault("ref", index)
        elif "flag" in cell or cell in {"abn", "abnormal"}:
            roles.setdefault("flag", index)

    has_header = "analyte" in roles or "value" in roles
    rows = table[1:] if has_header else table
    out: list[LabValue] = []

    for row in rows:
        cells = [(cell or "").strip() for cell in row]
        if not any(cells):
            continue

        if has_header:
            analyte = cells[roles["analyte"]] if "analyte" in roles and roles["analyte"] < len(cells) else ""
            value_raw = cells[roles["value"]] if "value" in roles and roles["value"] < len(cells) else ""
            unit = cells[roles["unit"]] if "unit" in roles and roles["unit"] < len(cells) else None
            ref_raw = cells[roles["ref"]] if "ref" in roles and roles["ref"] < len(cells) else None
            flag = cells[roles["flag"]] if "flag" in roles and roles["flag"] < len(cells) else None
        else:
            analyte = cells[0] if cells else ""
            numeric = [c for c in cells[1:] if _to_float(c) is not None]
            value_raw = numeric[0] if numeric else ""
            unit = next((c for c in cells[1:] if _looks_like_unit(c)), None)
            ref_raw = next((c for c in cells[1:] if _REF_RANGE.match(c)), None)
            flag = None

        analyte = analyte.strip(" .:-")
        if len(analyte) < 2 or not re.search(r"[A-Za-z]{2}", analyte):
            continue
        if _NOT_A_RESULT.search(analyte):
            continue

        value_num = _to_float(value_raw)
        if value_num is None and not value_raw:
            continue
        value_text = value_raw if value_num is None else None

        ref_low, ref_high, ref_text = parse_reference_range(ref_raw)
        key, panel = labs.resolve_key(analyte)
        out.append(LabValue(
            analyte=analyte,
            analyte_key=key,
            panel=panel,
            value_num=value_num,
            value_text=value_text,
            unit=(unit or None) if _looks_like_unit(unit) else (unit or None),
            ref_low=ref_low,
            ref_high=ref_high,
            ref_text=ref_text,
            flag=(flag or "").strip().upper()[:8] or None,
            page_no=page_no,
            raw_line=" | ".join(c for c in cells if c)[:300],
            extracted_by="table",
        ))

    return out


def extract_lab_values(pages: Iterable[ExtractedPage]) -> list[LabValue]:
    """Run both extraction passes over every page."""
    out: list[LabValue] = []
    for page in pages:
        via_ocr = page.extraction == "ocr"
        for table in page.tables:
            for value in extract_from_table(table, page.page_no):
                value.via_ocr = via_ocr
                out.append(value)
        for line in page.line_source.splitlines():
            value = extract_from_line(line, page.page_no)
            if value is not None:
                value.via_ocr = via_ocr
                out.append(value)
    return out


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #

def chunk_text(text: str, *, target_chars: int = 1000,
               overlap_chars: int = 150) -> list[str]:
    """Split text into overlapping chunks on paragraph then line boundaries.

    Overlap exists so a fact split across a boundary is retrievable from at
    least one chunk. Splitting on structure rather than a fixed character count
    keeps table rows and headings intact, which matters here: "Glucose 95 mg/dL"
    is useless if the analyte and its value land in different chunks.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= target_chars:
        return [text]

    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for block in blocks:
        if len(block) > target_chars:
            flush()
            for line in block.splitlines():
                if len(current) + len(line) + 1 > target_chars:
                    flush()
                current += line + "\n"
            flush()
            continue
        if len(current) + len(block) + 2 > target_chars:
            flush()
        current += block + "\n\n"
    flush()

    if overlap_chars <= 0 or len(chunks) < 2:
        return chunks

    overlapped = [chunks[0]]
    for previous, chunk in zip(chunks, chunks[1:]):
        tail = previous[-overlap_chars:].lstrip()
        overlapped.append(f"{tail}\n{chunk}" if tail else chunk)
    return overlapped


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def _dedup_key(*parts) -> bytes:
    joined = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.blake2b(joined.encode("utf-8"), digest_size=16).digest()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def store_document(conn: sqlite3.Connection, path: Path, *, source_file_id: int,
                   pages: list[ExtractedPage], lab_values: list[LabValue],
                   stats: RecordStats) -> int:
    """Write one document, its pages, lab values, and chunks. Returns document id."""
    extractions = {p.extraction for p in pages if p.extraction != "none"}
    if not extractions:
        overall = "none"
    elif len(extractions) == 1:
        overall = extractions.pop()
    else:
        overall = "mixed"

    doc_date = find_document_date(pages)
    title = _document_title(pages, path)

    cursor = conn.execute(
        "INSERT INTO document(source_file_id, path, kind, title, doc_date, "
        "page_count, extraction, ingested_at) VALUES(?,?,?,?,?,?,?,?)",
        (source_file_id, str(path), "pdf", title, doc_date, len(pages),
         overall, _now()),
    )
    document_id = int(cursor.lastrowid)
    stats.documents += 1

    conn.executemany(
        "INSERT INTO document_page(document_id, page_no, text, char_count, "
        "extraction) VALUES(?,?,?,?,?)",
        [(document_id, p.page_no, p.text, len(p.text), p.extraction) for p in pages],
    )

    for value in lab_values:
        if labs.unit_looks_wrong(value.analyte_key, value.unit):
            stats.unit_warnings += 1
        key = _dedup_key(document_id, value.page_no, value.analyte_key,
                         value.value_num, value.value_text, value.unit)
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO lab_result(document_id, page_no, analyte, "
            "analyte_key, panel, value_num, value_text, unit, ref_low, ref_high, "
            "ref_text, flag, collected_date, raw_line, extracted_by, via_ocr, "
            "dedup_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (document_id, value.page_no, value.analyte, value.analyte_key,
             value.panel, value.value_num, value.value_text, value.unit,
             value.ref_low, value.ref_high, value.ref_text, value.flag,
             doc_date, value.raw_line, value.extracted_by,
             1 if value.via_ocr else 0, key),
        )
        if conn.total_changes > before:
            stats.lab_values += 1
        else:
            stats.lab_values_duplicate += 1

    chunk_index = 0
    for page in pages:
        for piece in chunk_text(page.text):
            conn.execute(
                "INSERT INTO chunk(document_id, page_no, chunk_index, text, "
                "char_count) VALUES(?,?,?,?,?)",
                (document_id, page.page_no, chunk_index, piece, len(piece)),
            )
            chunk_index += 1
            stats.chunks += 1

    conn.commit()
    return document_id


def _document_title(pages: list[ExtractedPage], path: Path) -> str:
    """First substantive line of page 1, falling back to the filename."""
    if pages:
        for line in pages[0].text.splitlines():
            candidate = line.strip()
            if len(candidate) >= 4 and re.search(r"[A-Za-z]{3}", candidate):
                return candidate[:120]
    return path.stem


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

def ingest_pdf(conn: sqlite3.Connection, path: Path, *, source_file_id: int,
               use_ocr: bool = True, stats: RecordStats | None = None) -> RecordStats:
    """Parse and store one PDF."""
    stats = stats if stats is not None else RecordStats()
    pages = extract_pages(path, use_ocr=use_ocr, stats=stats)
    lab_values = extract_lab_values(pages)
    store_document(conn, path, source_file_id=source_file_id, pages=pages,
                   lab_values=lab_values, stats=stats)
    return stats


def find_records(root: Path) -> list[Path]:
    """Every supported record file under `root`, sorted for stable ordering."""
    if root.is_file():
        return [root] if root.suffix.lower() in SUPPORTED_SUFFIXES else []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
        and not p.name.startswith(".")
    )


def ingest_records(
    conn: sqlite3.Connection,
    root: Path,
    *,
    register: Callable[[Path], tuple[int, bool]],
    use_ocr: bool = True,
    force: bool = False,
    on_file: Callable[[Path], None] | None = None,
) -> RecordStats:
    """Ingest every record file under `root`.

    `register` is injected rather than imported so this module stays unaware of
    source-file bookkeeping; it returns (source_file_id, already_ingested).

    A file that fails mid-parse is skipped with a metadata-only warning and the
    run continues (plan §5a) — one corrupt scan must not cost a whole folder.
    """
    from ..store import sqlite_schema

    stats = RecordStats()
    stats.ocr_available = ocr_available() if use_ocr else False

    for path in find_records(root):
        source_file_id, already = register(path)
        if already and not force:
            log.info("%s unchanged since last ingest; skipping", path.name)
            continue
        if on_file is not None:
            on_file(path)

        # Replace rather than merge: a re-parsed PDF is the same report again.
        sqlite_schema.clear_document_data(conn, source_file_id)
        before_values = stats.lab_values
        before_unread = stats.pages_empty
        try:
            ingest_pdf(conn, path, source_file_id=source_file_id,
                       use_ocr=use_ocr, stats=stats)
        except Exception as exc:  # noqa: BLE001 - never lose a run to one file
            log.warning("%s: skipped (%s)", path.name, type(exc).__name__)
            stats.skip(type(exc).__name__)
            # Left 'partial' on purpose, so a fixed or re-exported file is
            # retried on the next run instead of being treated as done.
            continue

        # A file with pages we couldn't read stays 'partial', so installing
        # Tesseract and re-running picks it up without needing --force. Marking
        # it complete would strand a scanned report as permanently empty.
        fully_read = stats.pages_empty == before_unread
        sqlite_schema.mark_source_file(
            conn, source_file_id,
            record_count=stats.lab_values - before_values,
            status="complete" if fully_read else "partial",
        )

    sqlite_schema.rebuild_chunk_fts(conn)
    return stats
