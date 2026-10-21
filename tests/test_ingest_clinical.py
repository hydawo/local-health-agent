"""Reading clinical-records out of an export, from a zip or a folder."""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from health_agent.ingest import fhir, healthkit
from health_agent.store import sqlite_schema

FIX = Path(__file__).parent / "fixtures" / "clinical-records"


def _zip_with_clinical(tmp_path: Path) -> Path:
    target = tmp_path / "export.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("apple_health_export/export.xml",
                         '<?xml version="1.0"?><HealthData locale="en_US">'
                         '<ExportDate value="2026-10-21 09:00:00 -0400"/></HealthData>')
        for path in sorted(FIX.glob("*.json")):
            archive.writestr(f"apple_health_export/clinical-records/{path.name}",
                             path.read_bytes())
    return target


def _folder_with_clinical(tmp_path: Path) -> Path:
    root = tmp_path / "apple_health_export"
    (root / "clinical-records").mkdir(parents=True)
    for path in FIX.glob("*.json"):
        shutil.copy(path, root / "clinical-records" / path.name)
    return root


@pytest.fixture
def conn(tmp_path):
    c = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(c)
    return c


def _source(conn, path: Path) -> int:
    """Register whichever file exists for this layout: the zip itself, or a
    file inside the clinical-records folder."""
    target = path if path.is_file() else next((path / "clinical-records").glob("*.json"))
    sid, _ = healthkit.register_source_file(conn, target, "healthkit",
                                            healthkit.sha256_file(target))
    return sid


@pytest.mark.parametrize("make", [_zip_with_clinical, _folder_with_clinical])
def test_both_layouts_ingest(conn, tmp_path, make):
    path = make(tmp_path)
    sid = _source(conn, path)
    stats = fhir.ingest_clinical(conn, path, source_file_id=sid)
    conn.commit()
    assert stats.labs == 3 and stats.vitals == 3
    assert stats.conditions == 1 and stats.medications == 1
    assert stats.allergies == 1 and stats.immunizations == 1
    assert stats.procedures == 1 and stats.notes == 2
    assert stats.unreadable == 1            # malformed.json
    assert conn.execute("SELECT COUNT(*) FROM clinical_fact").fetchone()[0] == 5


def test_reingest_adds_nothing(conn, tmp_path):
    path = _zip_with_clinical(tmp_path)
    sid = _source(conn, path)
    first = fhir.ingest_clinical(conn, path, source_file_id=sid)
    conn.commit()
    fhir.ingest_clinical(conn, path, source_file_id=sid)
    conn.commit()
    for table in ("document", "lab_result", "clinical_vital", "clinical_fact"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert n == {"document": first.documents, "lab_result": 3,
                     "clinical_vital": 3, "clinical_fact": 5}[table], table


@pytest.mark.parametrize("kind,table,column,expected_zero", [
    ("labs", "lab_result", None, True),
    ("vitals", "clinical_vital", None, True),
    ("conditions", "clinical_fact", "condition", True),
    ("medications", "clinical_fact", "medication", True),
])
def test_each_switch_skips_exactly_its_kind(conn, tmp_path, kind, table, column,
                                            expected_zero):
    path = _zip_with_clinical(tmp_path)
    sid = _source(conn, path)
    stats = fhir.ingest_clinical(conn, path, source_file_id=sid,
                                 skip=frozenset({kind}))
    conn.commit()
    if column:
        n = conn.execute("SELECT COUNT(*) FROM clinical_fact WHERE kind = ?",
                         (column,)).fetchone()[0]
    else:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    assert n == 0
    assert getattr(stats, kind) == 0 and stats.skipped > 0
    # the other kinds still landed
    assert conn.execute("SELECT COUNT(*) FROM clinical_fact").fetchone()[0] + \
        conn.execute("SELECT COUNT(*) FROM lab_result").fetchone()[0] > 0


def test_parse_skip_list_accepts_kinds_and_rejects_anything_else():
    assert fhir.parse_skip_list("labs, vitals") == frozenset({"labs", "vitals"})
    assert fhir.parse_skip_list("") == frozenset()
    with pytest.raises(ValueError) as excinfo:
        fhir.parse_skip_list("labs,diagnoses")
    assert "diagnoses" in str(excinfo.value)
    for kind in fhir.CLINICAL_KINDS:
        assert kind in str(excinfo.value)


def test_no_identity_string_reaches_the_database(conn, tmp_path):
    """The fixtures plant markers in every identity field. None may appear
    in any text column of the index."""
    path = _zip_with_clinical(tmp_path)
    fhir.ingest_clinical(conn, path, source_file_id=_source(conn, path))
    conn.commit()
    planted = ("SHOULD-NOT-BE-STORED", "SHOULD-NOT-BE-FETCHED",
               "PATIENTNAME", "FIRSTNAME", "ADDRESS-SHOULD")
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        for col in cols:
            # Raw values, not CAST to text: BLOB columns (embeddings) are not
            # valid UTF-8, so compare as bytes.
            values = conn.execute(
                f"SELECT DISTINCT CAST({col} AS BLOB) AS v FROM {table} "
                f"WHERE {col} IS NOT NULL").fetchall()
            blob = b"\n".join(bytes(r["v"]) for r in values)
            for marker in planted:
                assert marker.encode() not in blob, f"{table}.{col} leaked {marker}"


def test_the_patient_resource_contributes_nothing(conn, tmp_path):
    path = _zip_with_clinical(tmp_path)
    fhir.ingest_clinical(conn, path, source_file_id=_source(conn, path))
    conn.commit()
    rows = conn.execute("SELECT COUNT(*) FROM document "
                        "WHERE fhir_resource_type = 'Patient'").fetchone()[0]
    assert rows == 0
