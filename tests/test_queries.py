"""Aggregation tests. Expected values are hand-computed; see fixtures/README.md."""

from __future__ import annotations

import shutil
from pathlib import Path

from health_agent import metrics
from health_agent.store import queries


def series(conn, name, **kwargs):
    return queries.metric_series(conn, metrics.resolve(name), **kwargs)


# --------------------------------------------------------------------------- #
# Aggregation semantics
# --------------------------------------------------------------------------- #

def test_discrete_metric_defaults_to_mean(ingested):
    conn, _ = ingested
    result = series(conn, "resting-hr", period="month")
    assert result.agg == "avg"
    assert len(result.points) == 1
    # 58 + 60 + 62 + 59 + 61 + 57 + 63 = 420 over 7 samples
    assert result.points[0].value == 60.0
    assert result.points[0].n == 7
    assert result.unit == "count/min"


def test_cumulative_metric_defaults_to_sum(ingested):
    conn, _ = ingested
    result = series(conn, "steps", period="day", start="2026-03-03", end="2026-03-03")
    assert result.agg == "sum"
    assert result.points[0].value == 8500.0


def test_daily_series_has_one_point_per_day_with_data(ingested):
    conn, _ = ingested
    result = series(conn, "resting-hr")
    assert [p.period for p in result.points] == [
        f"2026-03-0{d}" for d in range(1, 8)
    ]
    assert [p.value for p in result.points] == [58, 60, 62, 59, 61, 57, 63]


def test_explicit_aggregation_overrides_the_default(ingested):
    conn, _ = ingested
    result = series(conn, "resting-hr", period="month", agg="min")
    assert result.points[0].value == 57.0
    result = series(conn, "resting-hr", period="month", agg="max")
    assert result.points[0].value == 63.0
    result = series(conn, "resting-hr", period="month", agg="count")
    assert result.points[0].value == 7.0


def test_weekly_average_is_sample_weighted_not_average_of_averages(ingested):
    conn, _ = ingested
    # 2026-03-01 is a Sunday, so it belongs to the week starting 2026-02-23;
    # 03-02..03-07 fall in the week starting 2026-03-02.
    result = series(conn, "resting-hr", period="week")
    by_period = {p.period: p for p in result.points}
    assert by_period["2026-02-23"].value == 58.0
    assert by_period["2026-02-23"].n == 1
    # (60 + 62 + 59 + 61 + 57 + 63) / 6 = 60.333...
    assert round(by_period["2026-03-02"].value, 4) == 60.3333
    assert by_period["2026-03-02"].n == 6


# --------------------------------------------------------------------------- #
# The multi-source problem
# --------------------------------------------------------------------------- #

def test_overlapping_sources_are_not_summed_by_default(ingested):
    """Watch 6000 + Phone 5500 on the same day is not 11500 steps walked."""
    conn, _ = ingested
    result = series(conn, "steps", start="2026-03-02", end="2026-03-02")
    assert result.combine == "max"
    assert result.points[0].value == 6000.0
    assert result.multi_source_periods == 1
    assert result.points[0].per_source == {
        "Fixture Watch": 6000.0,
        "Fixture Phone": 5500.0,
    }


def test_combine_sum_is_available_when_the_caller_knows_better(ingested):
    conn, _ = ingested
    result = series(conn, "steps", start="2026-03-02", end="2026-03-02",
                    combine="sum")
    assert result.points[0].value == 11500.0


def test_source_filter(ingested):
    conn, _ = ingested
    result = series(conn, "steps", start="2026-03-02", end="2026-03-02",
                    source="Fixture Phone")
    assert result.points[0].value == 5500.0
    assert result.multi_source_periods == 0


def test_single_source_days_are_unaffected(ingested):
    conn, _ = ingested
    result = series(conn, "steps", start="2026-03-03", end="2026-03-03")
    assert result.multi_source_periods == 0
    assert result.points[0].value == 8500.0


# --------------------------------------------------------------------------- #
# Category metrics
# --------------------------------------------------------------------------- #

def test_sleep_aggregates_by_duration_per_stage(ingested):
    conn, _ = ingested
    result = series(conn, "sleep")
    assert result.agg == "duration"
    by_key = {(p.period, p.category): p.value for p in result.points}
    assert by_key[("2026-03-04", "HKCategoryValueSleepAnalysisInBed")] == 1800.0
    assert by_key[("2026-03-05", "HKCategoryValueSleepAnalysisAsleepCore")] == 7200.0
    assert by_key[("2026-03-05", "HKCategoryValueSleepAnalysisAsleepDeep")] == 2700.0
    assert by_key[("2026-03-05", "HKCategoryValueSleepAnalysisAsleepREM")] == 2700.0


# --------------------------------------------------------------------------- #
# Windows and missing data
# --------------------------------------------------------------------------- #

def test_date_window_is_inclusive(ingested):
    conn, _ = ingested
    result = series(conn, "resting-hr", start="2026-03-02", end="2026-03-04")
    assert [p.period for p in result.points] == ["2026-03-02", "2026-03-03", "2026-03-04"]


def test_empty_window_still_reports_what_exists(ingested):
    """Feeds the missing-data behavior in plan §5a: name what's missing and where
    the data actually is, rather than an unqualified 'no data'."""
    conn, _ = ingested
    result = series(conn, "resting-hr", start="2026-05-01", end="2026-05-31")
    assert result.is_empty
    assert result.total_records == 7
    assert result.available_from == "2026-03-01"
    assert result.available_to == "2026-03-07"


def test_metric_absent_from_the_index(ingested):
    conn, _ = ingested
    result = series(conn, "vo2max")
    assert result.is_empty
    assert result.total_records == 0
    assert result.available_from is None


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

def test_aliases_and_identifiers_both_resolve():
    by_alias = metrics.resolve("resting-hr")
    by_identifier = metrics.resolve("HKQuantityTypeIdentifierRestingHeartRate")
    by_bare_name = metrics.resolve("RestingHeartRate")
    assert by_alias == by_identifier == by_bare_name


def test_unknown_identifier_falls_back_to_discrete():
    """Averaging a cumulative type fails visibly; summing a discrete one produces
    a plausible-looking wrong number. Prefer the visible failure."""
    metric = metrics.resolve("HKQuantityTypeIdentifierMadeUpThing")
    assert metric.kind is metrics.Kind.DISCRETE
    assert metric.default_agg == "avg"


def test_category_identifier_fallback_is_category():
    metric = metrics.resolve("HKCategoryTypeIdentifierMadeUpThing")
    assert metric.kind is metrics.Kind.CATEGORY


# --------------------------------------------------------------------------- #
# Index-level summaries
# --------------------------------------------------------------------------- #

def test_index_summary(ingested):
    conn, _ = ingested
    summary = queries.index_summary(conn)
    assert summary["records"] == 25
    assert summary["first_date"] == "2026-03-01"
    assert summary["last_date"] == "2026-03-09"
    assert summary["workouts"] == 1
    assert summary["activity_summaries"] == 2
    assert "Fixture Watch" in summary["sources"]


def test_list_types_search(ingested):
    conn, _ = ingested
    found = queries.list_types(conn, search="sleep")
    assert len(found) == 1
    assert found[0]["type"] == "HKCategoryTypeIdentifierSleepAnalysis"
    assert found[0]["n"] == 4


def test_workout_summary(ingested):
    conn, _ = ingested
    found = queries.workout_summary(conn)
    assert found[0]["activity_type"] == "HKWorkoutActivityTypeRunning"
    assert found[0]["total_distance"] == 5.0


# --------------------------------------------------------------------------- #
# Clinical records (FHIR): one draw reported twice is one point
# --------------------------------------------------------------------------- #

import pytest  # noqa: E402

from health_agent.ingest import fhir, healthkit  # noqa: E402
from health_agent.store import sqlite_schema  # noqa: E402

CLINICAL_FIX = Path(__file__).parent / "fixtures" / "clinical-records"


def _new_index(tmp_path):
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    return conn


@pytest.fixture
def two_docs(tmp_path):
    conn = _new_index(tmp_path)
    marker = tmp_path / "marker.txt"
    marker.write_text("x")
    sid, _ = healthkit.register_source_file(conn, marker, "healthkit",
                                            healthkit.sha256_file(marker))
    ids = []
    for kind, path in (("pdf", "labs.pdf"), ("fhir", "clinical-records/a.json")):
        cur = conn.execute(
            "INSERT INTO document(source_file_id, path, kind, page_count, "
            "extraction, ingested_at) VALUES(?,?,?,1,'text','2026-10-21')",
            (sid, path, kind))
        ids.append(cur.lastrowid)
    return conn, ids[0], ids[1]


@pytest.fixture
def clinical_index(tmp_path):
    conn = _new_index(tmp_path)
    root = tmp_path / "apple_health_export"
    shutil.copytree(CLINICAL_FIX, root / "clinical-records")
    target = next((root / "clinical-records").glob("*.json"))
    sid, _ = healthkit.register_source_file(conn, target, "healthkit",
                                            healthkit.sha256_file(target))
    fhir.ingest_clinical(conn, root, source_file_id=sid)
    conn.commit()
    return conn


def _add_lab(conn, *, document_id, analyte_key="ldl", value, unit="mg/dL",
             date="2026-03-10", ref_text=None, extracted_by="line", via_ocr=0):
    conn.execute(
        "INSERT INTO lab_result(document_id, page_no, analyte, analyte_key, "
        "value_num, unit, ref_text, collected_date, raw_line, extracted_by, "
        "via_ocr, dedup_key) VALUES(?,1,?,?,?,?,?,?,'x',?,?,?)",
        (document_id, analyte_key.upper(), analyte_key, value, unit, ref_text,
         date, extracted_by, via_ocr, f"{document_id}{value}{date}".encode()))


def test_the_same_value_from_a_pdf_and_from_fhir_is_one_point(two_docs):
    conn, pdf_doc, fhir_doc = two_docs
    _add_lab(conn, document_id=pdf_doc, value=112.0, ref_text="0-99")
    _add_lab(conn, document_id=fhir_doc, value=112.0, extracted_by="fhir")
    trend = queries.lab_trend(conn, "ldl")
    assert len(trend.points) == 1
    point = trend.points[0]
    assert point.value_num == 112.0
    assert point.ref_text == "0-99"          # the row carrying a range won
    assert point.source == "pdf"
    assert point.also_from == ("fhir",)
    assert trend.disagreements == ()


def test_the_fhir_row_wins_when_only_it_carries_a_range(two_docs):
    conn, pdf_doc, fhir_doc = two_docs
    _add_lab(conn, document_id=pdf_doc, value=112.0, via_ocr=1)
    _add_lab(conn, document_id=fhir_doc, value=112.0, ref_text="0-99",
             extracted_by="fhir")
    point = queries.lab_trend(conn, "ldl").points[0]
    assert point.source == "fhir" and point.ref_text == "0-99"
    assert point.also_from == ("pdf",)


def test_a_non_ocr_row_wins_when_neither_carries_a_range(two_docs):
    conn, pdf_doc, fhir_doc = two_docs
    _add_lab(conn, document_id=pdf_doc, value=112.0, via_ocr=1)
    _add_lab(conn, document_id=fhir_doc, value=112.0, extracted_by="fhir")
    assert queries.lab_trend(conn, "ldl").points[0].source == "fhir"


def test_different_values_on_one_date_are_kept_and_reported(two_docs):
    """Two draws in a day are real, and an OCR misread disagreeing with a
    FHIR value is exactly what this project reports rather than hides."""
    conn, pdf_doc, fhir_doc = two_docs
    _add_lab(conn, document_id=pdf_doc, value=112.0, via_ocr=1)
    _add_lab(conn, document_id=fhir_doc, value=118.0, extracted_by="fhir")
    trend = queries.lab_trend(conn, "ldl")
    assert sorted(p.value_num for p in trend.points) == [112.0, 118.0]
    (clash,) = trend.disagreements
    assert clash["collected_date"] == "2026-03-10"
    assert sorted(clash["values"]) == [112.0, 118.0]
    assert sorted(clash["sources"]) == ["fhir", "pdf"]


def test_a_different_unit_is_not_the_same_measurement(two_docs):
    conn, pdf_doc, fhir_doc = two_docs
    _add_lab(conn, document_id=pdf_doc, value=5.4, unit="%")
    _add_lab(conn, document_id=fhir_doc, value=5.4, unit="mmol/mol",
             extracted_by="fhir")
    assert len(queries.lab_trend(conn, "ldl").points) == 2


def test_two_identical_rows_from_one_source_are_not_collapsed(two_docs):
    """Collapse is for a draw reported by two sources. Two rows from one
    source (a repeat test the same day with the same result) stay two."""
    conn, pdf_doc, _ = two_docs
    _add_lab(conn, document_id=pdf_doc, value=112.0)
    conn.execute(
        "INSERT INTO lab_result(document_id, page_no, analyte, analyte_key, "
        "value_num, unit, collected_date, raw_line, extracted_by, via_ocr, "
        "dedup_key) VALUES(?,2,'LDL','ldl',112.0,'mg/dL','2026-03-10','x',"
        "'line',0,?)", (pdf_doc, b"other"))
    assert len(queries.lab_trend(conn, "ldl").points) == 2


def test_clinical_facts_filter_by_kind_and_report_coverage(clinical_index):
    conn = clinical_index
    all_rows = queries.clinical_facts(conn)
    assert {r["kind"] for r in all_rows} >= {"condition", "medication"}
    meds = queries.clinical_facts(conn, kind="medication")
    assert meds and all(r["kind"] == "medication" for r in meds)
    assert meds[0]["detail_text"].startswith("Take 1 tablet")
    assert meds[0]["citation"].startswith("clinical-records/")
    coverage = queries.clinical_fact_coverage(conn)
    assert coverage["condition"]["count"] == 1
    assert coverage["condition"]["first"] == "2023-09-15"


def test_clinical_vitals_filter_by_metric_and_date(clinical_index):
    conn = clinical_index
    rows = queries.clinical_vitals(conn, metric="pulse")
    assert rows and rows[0]["metric"] == "Pulse" and rows[0]["unit"] == "bpm"
    assert rows[0]["citation"].startswith("clinical-records/")
    assert queries.clinical_vitals(conn, start="2030-01-01") == []
    metrics = {m["metric_key"]: m for m in queries.clinical_vital_metrics(conn)}
    assert metrics["pulse"]["n"] == 1
    assert metrics["pulse"]["first"] == "2023-09-15"
