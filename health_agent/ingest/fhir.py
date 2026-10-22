"""The FHIR half of an Apple Health export: `clinical-records/*.json`.

`export.xml` is what the phone recorded; these files are what connected
health systems sent back. One JSON object becomes one typed record here,
and nothing else: the dataclasses below have no field for a subject, an
encounter, a performer or a recorder, so identity cannot be stored by
accident rather than merely being left out by a careful writer. The
`Patient` resource, which is nothing but identity, is ignored outright.

Attachment URLs on a `DocumentReference` are not kept either. They point
back at the health system behind its own auth, fetching one would put the
query path on the network, and `offline-check` would stop being true.
What is kept is that a note of a given type exists on a given date, which
is coverage rather than content.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

from .. import labs as labs_module
from ..logging_setup import get_logger

log = get_logger("ingest.fhir")

CLINICAL_DIR = "clinical-records"

CLINICAL_KINDS = ("labs", "vitals", "conditions", "medications", "allergies",
                  "immunizations", "procedures", "notes")

# resourceType -> the kind its facts are filed under.
_FACT_TYPES = {
    "Condition": "conditions",
    "MedicationRequest": "medications",
    "AllergyIntolerance": "allergies",
    "Immunization": "immunizations",
    "Procedure": "procedures",
}
_DOC_TYPES = ("DocumentReference", "DiagnosticReport")
_LAB_CATEGORY = "laboratory"


class FhirParseError(ValueError):
    """The file is not a FHIR resource. One bad file is counted and skipped,
    never fatal to an ingest of thousands."""


@dataclass(frozen=True)
class ParsedLab:
    resource_type: str
    resource_id: str
    member: str
    name: str
    codings: tuple[tuple[str, str], ...]
    value_num: float | None
    value_text: str | None
    unit: str | None
    ref_text: str | None
    ref_low: float | None
    ref_high: float | None
    flag: str | None
    collected_date: str | None
    category: str | None


@dataclass(frozen=True)
class ParsedVital:
    resource_type: str
    resource_id: str
    member: str
    name: str
    codings: tuple[tuple[str, str], ...]
    value_num: float | None
    value_text: str | None
    unit: str | None
    measured_at: str | None
    local_date: str | None
    category: str | None


@dataclass(frozen=True)
class ParsedFact:
    resource_type: str
    resource_id: str
    member: str
    kind: str
    name: str
    code: str | None
    code_system: str | None
    status: str | None
    onset_date: str | None
    detail_text: str | None
    category: str | None


@dataclass(frozen=True)
class ParsedClinicalDoc:
    resource_type: str
    resource_id: str
    member: str
    title: str | None
    doc_type: str | None
    doc_date: str | None


def kind_of(parsed) -> str:
    if isinstance(parsed, ParsedLab):
        return "labs"
    if isinstance(parsed, ParsedVital):
        return "vitals"
    if isinstance(parsed, ParsedFact):
        return parsed.kind
    if isinstance(parsed, ParsedClinicalDoc):
        return "notes"
    raise TypeError(f"not a parsed clinical record: {type(parsed).__name__}")


def _text(node, *keys) -> str | None:
    """`node[key]` walked safely, returning None at the first missing step."""
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, str) else None


def _concept(node) -> dict:
    """A CodeableConcept, or an empty one when the field is absent or is not
    an object. Malformed shapes degrade to 'no name, no codings' rather than
    raising anything but FhirParseError out of the parser."""
    return node if isinstance(node, dict) else {}


def _codings(code: dict) -> tuple[tuple[str, str], ...]:
    out = []
    entries = code.get("coding")
    for entry in (entries if isinstance(entries, list) else []):
        if not isinstance(entry, dict):
            continue
        system, value = entry.get("system"), entry.get("code")
        if isinstance(system, str) and isinstance(value, str):
            out.append((system, value))
    return tuple(out)


def _first_category(obj: dict) -> str | None:
    for entry in (obj.get("category") or []):
        if isinstance(entry, dict):
            text = entry.get("text")
            if isinstance(text, str):
                return text
            for coding in (entry.get("coding") or []):
                if isinstance(coding, dict) and isinstance(coding.get("code"), str):
                    return coding["code"]
        elif isinstance(entry, str):
            return entry
    return None


def _date_part(stamp: str | None) -> str | None:
    return stamp[:10] if isinstance(stamp, str) and len(stamp) >= 10 else None


def _quantity(obj: dict) -> tuple[float | None, str | None, str | None]:
    """(value, unit, text). A quantity when there is one, else the string
    form the resource used instead."""
    q = obj.get("valueQuantity")
    if isinstance(q, dict) and isinstance(q.get("value"), (int, float)):
        return float(q["value"]), _text(q, "unit"), None
    for key in ("valueString", "valueBoolean", "valueInteger"):
        if key in obj:
            return None, None, str(obj[key])
    concept = _text(obj, "valueCodeableConcept", "text")
    return None, None, concept


def _reference_range(obj: dict) -> tuple[str | None, float | None, float | None]:
    ranges = obj.get("referenceRange")
    if not isinstance(ranges, list) or not ranges:
        return None, None, None
    first = ranges[0] if isinstance(ranges[0], dict) else {}
    low = first.get("low", {}).get("value") if isinstance(first.get("low"), dict) else None
    high = first.get("high", {}).get("value") if isinstance(first.get("high"), dict) else None
    return (_text(first, "text"),
            float(low) if isinstance(low, (int, float)) else None,
            float(high) if isinstance(high, (int, float)) else None)


def _medication_name(obj: dict) -> str | None:
    """`medicationReference.display` when Epic fills it, else the contained
    Medication's own code text, else the codeable concept."""
    name = _text(obj, "medicationReference", "display")
    if name:
        return name
    for contained in (obj.get("contained") or []):
        if isinstance(contained, dict) and contained.get("resourceType") == "Medication":
            text = _text(contained, "code", "text")
            if text:
                return text
    return _text(obj, "medicationCodeableConcept", "text")


def _medication_codings(obj: dict) -> tuple[tuple[str, str], ...]:
    for contained in (obj.get("contained") or []):
        if isinstance(contained, dict) and contained.get("resourceType") == "Medication":
            codings = _codings(_concept(contained.get("code")))
            if codings:
                return codings
    return _codings(_concept(obj.get("medicationCodeableConcept")))


def _observation(obj: dict, member: str, rid: str):
    code = _concept(obj.get("code"))
    name = _text(code, "text") or ""
    category = _first_category(obj)
    value_num, unit, value_text = _quantity(obj)
    codings = _codings(code)
    if (category or "").strip().lower() == _LAB_CATEGORY:
        ref_text, ref_low, ref_high = _reference_range(obj)
        interpretations = obj.get("interpretation") or []
        flag = _text(interpretations[0], "text") if interpretations else None
        return ParsedLab(
            resource_type="Observation", resource_id=rid, member=member,
            name=name, codings=codings, value_num=value_num,
            value_text=value_text, unit=unit, ref_text=ref_text,
            ref_low=ref_low, ref_high=ref_high, flag=flag,
            collected_date=_date_part(_text(obj, "effectiveDateTime")),
            category=category)
    measured_at = _text(obj, "effectiveDateTime")
    return ParsedVital(
        resource_type="Observation", resource_id=rid, member=member,
        name=name, codings=codings, value_num=value_num,
        value_text=value_text, unit=unit, measured_at=measured_at,
        local_date=_date_part(measured_at), category=category)


def _coded_status(obj: dict) -> str | None:
    clinical = obj.get("clinicalStatus")
    if isinstance(clinical, dict):
        for coding in (clinical.get("coding") or []):
            if isinstance(coding, dict) and isinstance(coding.get("code"), str):
                return coding["code"]
        text = clinical.get("text")
        if isinstance(text, str):
            return text
    return _text(obj, "status")


def _fact(obj: dict, member: str, rid: str, resource_type: str, kind: str):
    if resource_type == "MedicationRequest":
        name = _medication_name(obj) or ""
        codings = _medication_codings(obj)
        instructions = obj.get("dosageInstruction") or []
        detail = _text(instructions[0], "text") if instructions else None
        onset = _date_part(_text(obj, "authoredOn"))
        status = _text(obj, "status")
    else:
        code = _concept(obj.get("code")) or _concept(obj.get("vaccineCode"))
        name = _text(code, "text") or ""
        codings = _codings(code)
        detail = None
        status = _coded_status(obj)
        onset = _date_part(
            _text(obj, "onsetDateTime") or _text(obj, "occurrenceDateTime")
            or _text(obj, "performedDateTime")
            or _text(obj, "performedPeriod", "start")
            or _text(obj, "recordedDate"))
    return ParsedFact(
        resource_type=resource_type, resource_id=rid, member=member, kind=kind,
        name=name, code=codings[0][1] if codings else None,
        code_system=codings[0][0] if codings else None, status=status,
        onset_date=onset, detail_text=detail, category=_first_category(obj))


def _clinical_doc(obj: dict, member: str, rid: str, resource_type: str):
    doc_type = _text(obj, "type", "text") or _text(obj, "code", "text")
    date = (_text(obj, "date") or _text(obj, "effectiveDateTime")
            or _text(obj, "issued"))
    return ParsedClinicalDoc(
        resource_type=resource_type, resource_id=rid, member=member,
        title=_text(obj, "description"), doc_type=doc_type,
        doc_date=_date_part(date))


def parse_resource(obj, member: str):
    """One resource to one record, or None for a type we do not store.

    None rather than an exception for an unknown type: a future export that
    adds a resource must not break an ingest of the ones we understand.
    """
    if not isinstance(obj, dict) or not isinstance(obj.get("resourceType"), str):
        raise FhirParseError(f"{member}: not a FHIR resource")
    resource_type = obj["resourceType"]
    rid = obj.get("id") if isinstance(obj.get("id"), str) else ""
    if resource_type == "Observation":
        return _observation(obj, member, rid)
    if resource_type in _FACT_TYPES:
        return _fact(obj, member, rid, resource_type, _FACT_TYPES[resource_type])
    if resource_type in _DOC_TYPES:
        return _clinical_doc(obj, member, rid, resource_type)
    return None


def parse_bytes(blob: bytes, member: str):
    """`parse_resource` over raw file bytes."""
    try:
        obj = json.loads(blob)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FhirParseError(f"{member}: not valid JSON: {exc}") from exc
    return parse_resource(obj, member)


# kind -> the singular `clinical_fact.kind` value stored in the row.
_FACT_ROW_KIND = {"conditions": "condition", "medications": "medication",
                  "allergies": "allergy", "immunizations": "immunization",
                  "procedures": "procedure"}


@dataclass
class ClinicalStats:
    documents: int = 0
    labs: int = 0
    vitals: int = 0
    conditions: int = 0
    medications: int = 0
    allergies: int = 0
    immunizations: int = 0
    procedures: int = 0
    notes: int = 0
    # The person's data: a file that is not valid FHIR.
    unreadable: int = 0
    # This tool's own defect: anything else that went wrong storing a file.
    # Separate, because a systematic store failure counted as "unreadable"
    # would describe thousands of the person's good files as bad ones.
    failed: int = 0
    skipped: int = 0

    def counted(self) -> dict[str, int]:
        """Only the kinds that produced rows, for a report line that does
        not list six zeroes."""
        return {k: getattr(self, k) for k in CLINICAL_KINDS
                if getattr(self, k, 0)}


def _dedup_key(*parts) -> bytes:
    joined = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.blake2b(joined.encode("utf-8"), digest_size=16).digest()


def _slug(text: str) -> str:
    return labs_module.slug(text)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _document_id(conn, parsed, *, source_file_id: int,
                 stats: ClinicalStats) -> tuple[int, bool]:
    """One document row per FHIR resource, created once and reused.

    Returns (id, newly_inserted). A resource with no id is identified by its
    member path, which is unique per file, so id-less siblings stay apart."""
    title = None
    doc_date = None
    if isinstance(parsed, ParsedClinicalDoc):
        title, doc_date = (parsed.doc_type or parsed.title), parsed.doc_date
    elif isinstance(parsed, ParsedLab):
        title, doc_date = parsed.name, parsed.collected_date
    elif isinstance(parsed, ParsedVital):
        title, doc_date = parsed.name, parsed.local_date
    elif isinstance(parsed, ParsedFact):
        title, doc_date = parsed.name, parsed.onset_date
    fhir_id = parsed.resource_id or parsed.member
    before = conn.total_changes
    conn.execute(
        "INSERT INTO document(source_file_id, path, kind, title, doc_date, "
        "page_count, extraction, ingested_at, fhir_resource_type, fhir_id) "
        "VALUES(?,?,'fhir',?,?,0,'none',?,?,?) "
        "ON CONFLICT(fhir_resource_type, fhir_id) WHERE fhir_id IS NOT NULL "
        "DO NOTHING",
        (source_file_id, parsed.member, title, doc_date, _now(),
         parsed.resource_type, fhir_id))
    inserted = conn.total_changes > before
    if inserted:
        stats.documents += 1
    row = conn.execute(
        "SELECT id FROM document WHERE fhir_resource_type = ? AND fhir_id = ?",
        (parsed.resource_type, fhir_id)).fetchone()
    return row["id"], inserted


def store_clinical(conn, parsed, *, source_file_id: int,
                   stats: ClinicalStats) -> None:
    """Write one parsed record. Idempotent: the document is keyed by its
    resource id and every row carries a dedup key."""
    document_id, doc_new = _document_id(conn, parsed, source_file_id=source_file_id,
                                       stats=stats)

    if isinstance(parsed, ParsedLab):
        key, panel = labs_module.resolve_coded(parsed.codings, parsed.name)
        value = (f"{parsed.value_num:g}" if parsed.value_num is not None
                 else (parsed.value_text or ""))
        raw_line = f"{parsed.name}: {value} {parsed.unit or ''}".strip()
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO lab_result(document_id, page_no, analyte, "
            "analyte_key, panel, value_num, value_text, unit, ref_low, "
            "ref_high, ref_text, flag, collected_date, raw_line, "
            "extracted_by, via_ocr, dedup_key) "
            "VALUES(?,0,?,?,?,?,?,?,?,?,?,?,?,?,'fhir',0,?)",
            (document_id, parsed.name, key, panel, parsed.value_num,
             parsed.value_text, parsed.unit, parsed.ref_low, parsed.ref_high,
             parsed.ref_text, parsed.flag, parsed.collected_date, raw_line,
             _dedup_key(document_id, 0, key, parsed.value_num,
                        parsed.value_text, parsed.unit)))
        stats.labs += conn.total_changes - before
        return

    if isinstance(parsed, ParsedVital):
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO clinical_vital(document_id, metric, "
            "metric_key, code, code_system, category, value_num, value_text, "
            "unit, measured_at, local_date, dedup_key) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (document_id, parsed.name, _slug(parsed.name),
             parsed.codings[0][1] if parsed.codings else None,
             parsed.codings[0][0] if parsed.codings else None,
             parsed.category, parsed.value_num, parsed.value_text, parsed.unit,
             parsed.measured_at, parsed.local_date,
             _dedup_key(_slug(parsed.name), parsed.measured_at,
                        parsed.value_num, parsed.value_text, parsed.unit)))
        stats.vitals += conn.total_changes - before
        return

    if isinstance(parsed, ParsedFact):
        before = conn.total_changes
        # `status` is the one column whose staleness changes what the row
        # means: a prescription the health system has stopped kept reporting
        # as active, because the dedup key omits status and the write ignored
        # the conflict. Newest write wins, and the citation moves with it to
        # the file the new status came from. The WHERE keeps a re-ingest of an
        # unchanged export reporting zeros, as every other store here does.
        conn.execute(
            "INSERT INTO clinical_fact(document_id, kind, name, "
            "code, code_system, status, onset_date, detail_text, category, "
            "dedup_key) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(dedup_key) DO UPDATE SET "
            "status = excluded.status, document_id = excluded.document_id "
            "WHERE clinical_fact.status IS NOT excluded.status",
            (document_id, _FACT_ROW_KIND[parsed.kind], parsed.name,
             parsed.code, parsed.code_system, parsed.status, parsed.onset_date,
             parsed.detail_text, parsed.category,
             _dedup_key(parsed.kind, parsed.name, parsed.code,
                        parsed.onset_date, parsed.detail_text)))
        setattr(stats, parsed.kind,
                getattr(stats, parsed.kind) + conn.total_changes - before)
        return

    stats.notes += int(doc_new)


def parse_skip_list(raw: str) -> frozenset[str]:
    """A comma-separated list of kinds to skip. An unknown name is an error
    naming the valid ones, rather than a silent no-op that looks like it
    worked."""
    names = [part.strip().lower() for part in (raw or "").split(",") if part.strip()]
    unknown = [n for n in names if n not in CLINICAL_KINDS]
    if unknown:
        raise ValueError(
            f"unknown clinical kind(s): {', '.join(unknown)}. "
            f"Valid kinds: {', '.join(CLINICAL_KINDS)}")
    return frozenset(names)


def iter_members(path: Path) -> Iterator[tuple[str, bytes]]:
    """(member path, bytes) for every clinical record under `path`.

    Accepts an `export.zip`, a directory holding `clinical-records/`, or a
    directory holding `apple_health_export/clinical-records/`. Zip members
    are read one at a time rather than extracted: a real archive is
    hundreds of megabytes and there is no reason to write a second copy of
    the user's health data to disk.
    """
    path = Path(path)
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for name in sorted(archive.namelist()):
                if f"/{CLINICAL_DIR}/" in f"/{name}" and name.endswith(".json"):
                    yield name, archive.read(name)
        return
    root = path if path.is_dir() else path.parent
    # Only folders inside the path the user pointed at: never reach up a
    # level and read a clinical-records/ that belongs to something else.
    for candidate in (root / CLINICAL_DIR,
                      root / "apple_health_export" / CLINICAL_DIR):
        if candidate.is_dir():
            for member in sorted(candidate.glob("*.json")):
                yield f"{CLINICAL_DIR}/{member.name}", member.read_bytes()
            return


def has_clinical_records(path: Path) -> bool:
    """Whether a clinical-records folder sits beside `path` at all.

    Asked separately from ingesting it, because `--no-clinical` reads nothing
    and `check` still has to tell "your export had no clinical records" from
    "you skipped them". Stops at the first member, so it costs a directory
    listing rather than a read of the archive.
    """
    for _ in iter_members(path):
        return True
    return False


def ingest_clinical(conn, path: Path, *, source_file_id: int,
                    skip: frozenset[str] = frozenset(),
                    on_progress=None) -> ClinicalStats:
    """Parse and store every clinical record beside `path`.

    A file that will not parse is counted and skipped: one bad resource in
    an archive of thousands must not cost the rest. The log line carries the
    member path and the exception type only, never the exception's content,
    which could quote health data.

    Two counters, not one. `unreadable` is the person's data: a file that is
    not valid FHIR. `failed` is anything else, which is a defect in this tool
    rather than in their export, and is reported in its own words so a
    systematic store failure can never read as thousands of bad files.
    """
    stats = ClinicalStats()
    for index, (member, blob) in enumerate(iter_members(path), start=1):
        # Each record lands completely or not at all: the document row and
        # its child rows are written inside one savepoint, and the counters
        # are restored with it, so a failure leaves no childless document and
        # no half-counted record.
        if not conn.in_transaction:
            conn.execute("BEGIN")
        conn.execute("SAVEPOINT clinical_record")
        before = {f.name: getattr(stats, f.name) for f in fields(stats)}
        try:
            parsed = parse_bytes(blob, member)
            if parsed is not None:
                if kind_of(parsed) in skip:
                    stats.skipped += 1
                else:
                    store_clinical(conn, parsed, source_file_id=source_file_id,
                                   stats=stats)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
            conn.execute("ROLLBACK TO clinical_record")
            for name, value in before.items():
                setattr(stats, name, value)
            log.warning("skipped a clinical record: %s (%s)", member,
                        type(exc).__name__)
            if isinstance(exc, FhirParseError):
                stats.unreadable += 1
            else:
                stats.failed += 1
        finally:
            conn.execute("RELEASE clinical_record")
        if on_progress is not None and index % 500 == 0:
            on_progress(index)
    return stats
