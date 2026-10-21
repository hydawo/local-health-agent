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

import json
from dataclasses import dataclass

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
