"""Parsing one FHIR resource into one typed record.

The parser is pure: a dict in, a dataclass or None out. Identity is
absent by construction, since no dataclass has a field for a subject, an
encounter, a performer or a recorder.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from health_agent.ingest import fhir

FIX = Path(__file__).parent / "fixtures" / "clinical-records"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_bytes())


def parse(name: str):
    return fhir.parse_resource(load(name), f"clinical-records/{name}")


def test_laboratory_observation_becomes_a_lab():
    lab = parse("Observation-lab-chol.json")
    assert isinstance(lab, fhir.ParsedLab)
    assert fhir.kind_of(lab) == "labs"
    assert lab.name == "CHOLESTEROL"
    assert ("http://loinc.org", "2093-3") in lab.codings
    assert lab.value_num == 192 and lab.unit == "mg/dL"
    assert lab.ref_text == "<200" and lab.ref_high == 200 and lab.ref_low is None
    assert lab.collected_date == "2024-11-06"
    assert lab.category == "Laboratory"
    assert lab.resource_id == "lab-chol"
    assert lab.member == "clinical-records/Observation-lab-chol.json"


def test_a_lab_without_a_reference_range_keeps_its_value():
    lab = parse("Observation-lab-ldl-norange.json")
    assert lab.value_num == 118
    assert lab.ref_text is None and lab.ref_low is None and lab.ref_high is None
    assert lab.flag is None


def test_a_lab_with_no_quantity_keeps_its_text():
    lab = parse("Observation-lab-text.json")
    assert lab.value_num is None
    assert lab.value_text == "see comment"


def test_vital_signs_observation_becomes_a_vital():
    vital = parse("Observation-vital-pulse.json")
    assert isinstance(vital, fhir.ParsedVital)
    assert fhir.kind_of(vital) == "vitals"
    assert vital.name == "Pulse" and vital.value_num == 72 and vital.unit == "bpm"
    assert vital.measured_at == "2023-09-15T14:22:00Z"
    assert vital.local_date == "2023-09-15"
    assert vital.category == "Vital Signs"


def test_an_unknown_observation_category_is_kept_as_a_vital():
    """Imaging today, something else in a future export. Dropping the data
    would be worse than storing it beside the vitals with its category."""
    imaging = parse("Observation-imaging.json")
    assert isinstance(imaging, fhir.ParsedVital)
    assert imaging.category == "Imaging"


@pytest.mark.parametrize("name,kind,display", [
    ("Condition-knee.json", "conditions", "Left anterior cruciate ligament tear"),
    ("MedicationRequest-ibuprofen.json", "medications", "ibuprofen 600 MG tablet"),
    ("AllergyIntolerance-pollen.json", "allergies", "Pollen"),
    ("Immunization-flu.json", "immunizations", "Influenza vaccine"),
    ("Procedure-acl.json", "procedures", "ACL reconstruction"),
])
def test_each_fact_resource_becomes_a_fact(name, kind, display):
    fact = parse(name)
    assert isinstance(fact, fhir.ParsedFact)
    assert fact.kind == kind and fhir.kind_of(fact) == kind
    assert fact.name == display


def test_condition_carries_status_onset_and_category():
    fact = parse("Condition-knee.json")
    assert fact.status == "active"
    assert fact.onset_date == "2023-09-15"
    assert fact.category == "Problem List Item"
    assert fact.code == "239720000" and fact.code_system == "http://snomed.info/sct"


def test_medication_carries_its_dosage_text_and_authored_date():
    fact = parse("MedicationRequest-ibuprofen.json")
    assert fact.detail_text == ("Take 1 tablet (600 mg total) by mouth "
                               "every 6 hours as needed.")
    assert fact.onset_date == "2023-09-15"
    assert fact.status == "completed"
    assert fact.code == "197806"


def test_immunization_and_procedure_dates_come_from_their_own_fields():
    assert parse("Immunization-flu.json").onset_date == "2024-10-02"
    assert parse("Procedure-acl.json").onset_date == "2023-09-15"


@pytest.mark.parametrize("name,doc_type,doc_date", [
    ("DocumentReference-progress.json", "Progress Notes", "2023-09-16"),
    ("DiagnosticReport-lipid.json", "LIPID PANEL", "2024-11-06"),
])
def test_note_references_become_dated_documents_without_content(
        name, doc_type, doc_date):
    doc = parse(name)
    assert isinstance(doc, fhir.ParsedClinicalDoc)
    assert fhir.kind_of(doc) == "notes"
    assert doc.doc_type == doc_type
    assert doc.doc_date == doc_date
    # No field exists for an attachment body or its URL.
    assert not any("url" in f for f in doc.__dataclass_fields__)


def test_the_patient_resource_is_ignored():
    assert parse("Patient-me.json") is None


def test_an_unknown_resource_type_is_ignored_rather_than_raised():
    assert fhir.parse_resource({"resourceType": "Encounter", "id": "x"}, "m") is None


def test_a_file_that_is_not_a_resource_raises_a_typed_error():
    with pytest.raises(fhir.FhirParseError):
        fhir.parse_resource({"no": "type"}, "m")
    with pytest.raises(fhir.FhirParseError):
        fhir.parse_resource([], "m")


def test_malformed_json_bytes_raise_a_typed_error():
    with pytest.raises(fhir.FhirParseError):
        fhir.parse_bytes((FIX / "malformed.json").read_bytes(),
                         "clinical-records/malformed.json")


def test_no_dataclass_can_hold_identity():
    """Structural, not incidental: the fields do not exist."""
    forbidden = {"subject", "encounter", "performer", "recorder", "patient"}
    for cls in (fhir.ParsedLab, fhir.ParsedVital, fhir.ParsedFact,
                fhir.ParsedClinicalDoc):
        assert not (set(cls.__dataclass_fields__) & forbidden)


def test_identity_strings_never_survive_parsing():
    planted = [p.name for p in sorted(FIX.glob("*.json"))
               if b"SHOULD-NOT-BE" in p.read_bytes()
               and not p.name.startswith("Patient-")]
    assert len(planted) >= 10, planted
    assert "Immunization-flu.json" in planted
    assert "AllergyIntolerance-pollen.json" in planted
    for name in planted:
        blob = repr(parse(name))
        assert "SHOULD-NOT-BE-STORED" not in blob
        assert "SHOULD-NOT-BE-FETCHED" not in blob


def test_kinds_list_is_the_single_source_of_truth():
    derived = set(fhir._FACT_TYPES.values()) | {"labs", "vitals", "notes"}
    assert derived == set(fhir.CLINICAL_KINDS)
    assert len(set(fhir.CLINICAL_KINDS)) == len(fhir.CLINICAL_KINDS)
    assert fhir.CLINICAL_KINDS == ("labs", "vitals", "conditions", "medications",
                                   "allergies", "immunizations", "procedures",
                                   "notes")


@pytest.mark.parametrize("resource", [
    {"resourceType": "Observation", "id": "a", "code": "oops"},
    {"resourceType": "Observation", "id": "a", "code": {"coding": ["oops"]}},
    {"resourceType": "Condition", "id": "a", "code": "oops"},
    {"resourceType": "Condition", "id": "a", "code": {"coding": ["oops", 3]}},
    {"resourceType": "Immunization", "id": "a", "vaccineCode": "oops"},
    {"resourceType": "MedicationRequest", "id": "a",
     "medicationCodeableConcept": {"coding": ["oops"]},
     "contained": [{"resourceType": "Medication", "code": "oops"}]},
])
def test_a_structurally_wrong_code_degrades_instead_of_raising(resource):
    parsed = fhir.parse_resource(resource, "m")
    assert parsed.name == ""
    assert not getattr(parsed, "codings", ())
    assert getattr(parsed, "code", None) is None
