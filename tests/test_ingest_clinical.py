"""Reading clinical-records out of an export, from a zip or a folder."""
from __future__ import annotations

import json
import logging
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


@pytest.mark.parametrize("kind,table,column", [
    ("labs", "lab_result", None),
    ("vitals", "clinical_vital", None),
    ("conditions", "clinical_fact", "condition"),
    ("medications", "clinical_fact", "medication"),
    ("allergies", "clinical_fact", "allergy"),
    ("immunizations", "clinical_fact", "immunization"),
    ("procedures", "clinical_fact", "procedure"),
    ("notes", "note_reference", None),
])
def test_each_switch_skips_exactly_its_kind(conn, tmp_path, kind, table, column):
    path = _zip_with_clinical(tmp_path)
    sid = _source(conn, path)
    stats = fhir.ingest_clinical(conn, path, source_file_id=sid,
                                 skip=frozenset({kind}))
    conn.commit()
    if column:
        n = conn.execute("SELECT COUNT(*) FROM clinical_fact WHERE kind = ?",
                         (column,)).fetchone()[0]
    elif table == "note_reference":
        # Notes have no table of their own: the record is the document row.
        n = conn.execute(
            "SELECT COUNT(*) FROM document WHERE kind = 'fhir' AND "
            "fhir_resource_type IN ('DocumentReference', 'DiagnosticReport')"
        ).fetchone()[0]
    else:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    assert n == 0
    assert getattr(stats, kind) == 0 and stats.skipped > 0
    # the other kinds still landed
    assert conn.execute("SELECT COUNT(*) FROM clinical_fact").fetchone()[0] + \
        conn.execute("SELECT COUNT(*) FROM lab_result").fetchone()[0] > 0


def test_a_newer_export_updates_a_facts_status(conn, tmp_path):
    """A prescription the health system has stopped must stop reporting as
    active. Everything else about the row is identical, so the dedup key is
    the same and the first write used to win forever."""
    first = _folder_with_clinical(tmp_path / "first")
    sid = _source(conn, first)
    fhir.ingest_clinical(conn, first, source_file_id=sid)
    conn.commit()
    assert conn.execute(
        "SELECT status FROM clinical_fact WHERE kind = 'medication'"
    ).fetchone()[0] == "completed"

    later = _folder_with_clinical(tmp_path / "later")
    member = later / "clinical-records" / "MedicationRequest-ibuprofen.json"
    resource = json.loads(member.read_text())
    resource["id"] = "med-ibu-2"          # a new resource id, as Epic sends
    resource["status"] = "stopped"
    member.write_text(json.dumps(resource))
    stats = fhir.ingest_clinical(conn, later,
                                 source_file_id=_source(conn, later))
    conn.commit()

    row = conn.execute(
        "SELECT f.status, d.path FROM clinical_fact f "
        "JOIN document d ON d.id = f.document_id "
        "WHERE f.kind = 'medication'").fetchone()
    assert row["status"] == "stopped"
    assert row["path"].endswith("MedicationRequest-ibuprofen.json")
    assert stats.medications == 1          # the change is reported, not silent
    assert conn.execute(
        "SELECT COUNT(*) FROM clinical_fact WHERE kind = 'medication'"
    ).fetchone()[0] == 1


def test_a_fact_whose_status_disappears_is_not_counted_as_a_change(conn, tmp_path):
    """The status guard is `IS NOT`, which is null-safe in SQLite. A resource
    that arrives with no status at all must not keep flipping the row, and a
    re-ingest of it must report zeros."""
    def folder(where, status):
        root = _folder_with_clinical(where)
        member = root / "clinical-records" / "MedicationRequest-ibuprofen.json"
        resource = json.loads(member.read_text())
        if status is None:
            resource.pop("status", None)
        else:
            resource["status"] = status
        member.write_text(json.dumps(resource))
        return root

    nostatus = folder(tmp_path / "nostatus", None)
    fhir.ingest_clinical(conn, nostatus, source_file_id=_source(conn, nostatus))
    conn.commit()
    assert conn.execute(
        "SELECT status FROM clinical_fact WHERE kind = 'medication'"
    ).fetchone()[0] is None

    again = folder(tmp_path / "again", None)
    stats = fhir.ingest_clinical(conn, again, source_file_id=_source(conn, again))
    conn.commit()
    assert stats.medications == 0          # null against null is no change

    stopped = folder(tmp_path / "stopped", "stopped")
    stats = fhir.ingest_clinical(conn, stopped,
                                 source_file_id=_source(conn, stopped))
    conn.commit()
    assert stats.medications == 1          # null against a value is a change
    assert conn.execute(
        "SELECT status FROM clinical_fact WHERE kind = 'medication'"
    ).fetchone()[0] == "stopped"


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


def test_a_non_parse_error_in_one_file_does_not_stop_the_run(conn, tmp_path,
                                                             monkeypatch, caplog):
    """One resource that raises something other than FhirParseError (a
    KeyError from a malformed dosageInstruction, say) is counted under its own
    counter, not as the person's unreadable data, and every other member is
    still stored. The log names the member and the exception type, never the
    exception's content."""
    path = _zip_with_clinical(tmp_path)
    sid = _source(conn, path)
    real = fhir.parse_bytes

    def flaky(blob, member):
        if member.endswith("MedicationRequest-ibuprofen.json"):
            raise KeyError("SECRET-HEALTH-DETAIL")
        return real(blob, member)

    monkeypatch.setattr(fhir, "parse_bytes", flaky)
    # The CLI's configure() turns propagation off on the package logger, so
    # attach caplog's handler to it directly to be order-independent.
    package_logger = logging.getLogger("health_agent")
    package_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level("WARNING", logger="health_agent"):
            stats = fhir.ingest_clinical(conn, path, source_file_id=sid)
    finally:
        package_logger.removeHandler(caplog.handler)
    conn.commit()
    assert stats.unreadable == 1            # malformed.json: not valid FHIR
    assert stats.failed == 1                # the KeyError: this tool's defect
    assert stats.medications == 0
    assert stats.labs == 3 and stats.vitals == 3 and stats.conditions == 1
    assert stats.allergies == 1 and stats.immunizations == 1
    assert stats.procedures == 1 and stats.notes == 2
    logged = caplog.text
    assert "MedicationRequest-ibuprofen.json" in logged and "KeyError" in logged
    assert "SECRET-HEALTH-DETAIL" not in logged


def test_a_failure_while_storing_one_file_does_not_stop_the_run(conn, tmp_path,
                                                                monkeypatch):
    """The failing record raises AFTER its document row is written, inside the
    real store. It must leave no document and no child row, be counted under
    the store-failure counter rather than as unreadable data, and cost no
    other member its rows."""
    path = _zip_with_clinical(tmp_path)
    baseline_conn = sqlite_schema.connect(tmp_path / "baseline.db", create=True)
    sqlite_schema.initialize(baseline_conn)
    baseline = fhir.ingest_clinical(
        baseline_conn, path, source_file_id=_source(baseline_conn, path))
    baseline_conn.commit()

    target = "Observation-lab-chol.json"
    state = {"armed": False}
    real_store = fhir.store_clinical
    real_key = fhir._dedup_key

    def store(c, parsed, *, source_file_id, stats):
        state["armed"] = parsed.member.endswith(target)
        return real_store(c, parsed, source_file_id=source_file_id, stats=stats)

    def key(*parts):
        if state["armed"]:
            raise RuntimeError("SECRET-HEALTH-DETAIL")   # after the document insert
        return real_key(*parts)

    monkeypatch.setattr(fhir, "store_clinical", store)
    monkeypatch.setattr(fhir, "_dedup_key", key)
    stats = fhir.ingest_clinical(conn, path, source_file_id=_source(conn, path))
    conn.commit()

    assert stats.unreadable == baseline.unreadable
    assert stats.failed == baseline.failed + 1
    assert stats.labs == baseline.labs - 1
    assert stats.documents == baseline.documents - 1
    for name in ("vitals", "conditions", "medications", "allergies",
                 "immunizations", "procedures", "notes"):
        assert getattr(stats, name) == getattr(baseline, name), name
    assert conn.execute("SELECT COUNT(*) FROM document WHERE path LIKE ?",
                        (f"%{target}",)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM document d WHERE NOT EXISTS "
                        "(SELECT 1 FROM lab_result l WHERE l.document_id = d.id) "
                        "AND NOT EXISTS (SELECT 1 FROM clinical_vital v "
                        "WHERE v.document_id = d.id) AND NOT EXISTS "
                        "(SELECT 1 FROM clinical_fact f WHERE f.document_id = d.id) "
                        "AND d.fhir_resource_type NOT IN "
                        "('DocumentReference','DiagnosticReport')"
                        ).fetchone()[0] == 0
    for table in ("lab_result", "clinical_vital", "clinical_fact", "document"):
        got = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        want = baseline_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert got == want - (1 if table in ("lab_result", "document") else 0), table


def test_the_nested_export_folder_layout_is_read(conn, tmp_path):
    root = tmp_path / "outer"
    nested = root / "apple_health_export" / "clinical-records"
    nested.mkdir(parents=True)
    for member in FIX.glob("*.json"):
        shutil.copy(member, nested / member.name)
    members = [name for name, _ in fhir.iter_members(root)]
    assert members and all(n.startswith("clinical-records/") for n in members)


def test_a_clinical_folder_outside_the_given_path_is_never_read(tmp_path):
    """A sibling clinical-records/ next to the export's folder is not ours."""
    outer = tmp_path / "outer"
    (outer / "clinical-records").mkdir(parents=True)
    for member in FIX.glob("*.json"):
        shutil.copy(member, outer / "clinical-records" / member.name)
    inner = outer / "export"
    inner.mkdir()
    (inner / "export.xml").write_text("<HealthData/>")
    assert list(fhir.iter_members(inner)) == []
    assert list(fhir.iter_members(inner / "export.xml")) == []


def test_a_skipped_run_still_reports_the_unreadable_file(tmp_path, capsys):
    """Minor 1: with every kind skipped and one file unreadable, the report
    used to print the skip alone and the unreadable file vanished."""
    from health_agent.cli import main
    target = _zip_with_clinical(tmp_path)
    every = ",".join(fhir.CLINICAL_KINDS)
    code = main(["--index", str(tmp_path / "h.db"), "ingest", str(target),
                 "--no-embed", "--skip-clinical", every])
    out = capsys.readouterr().out
    assert code == 0
    assert "all 13 skipped" in out
    assert "1 unreadable (not valid FHIR)" in out


def test_all_unreadable_still_prints_a_clinical_line(tmp_path, capsys):
    from health_agent.cli import main
    target = tmp_path / "export.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("apple_health_export/export.xml",
                         '<?xml version="1.0"?><HealthData locale="en_US"/>')
        archive.writestr("apple_health_export/clinical-records/bad.json", "{")
    code = main(["--index", str(tmp_path / "h.db"), "ingest", str(target),
                 "--no-embed"])
    out = capsys.readouterr().out
    assert code == 0
    assert "clinical records    none readable" in out
    assert "1 unreadable (not valid FHIR)" in out
