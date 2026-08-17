"""Record ingestion tests, all against synthetic PDFs.

Expected values are the ones written into tests/fixtures/generate_pdfs.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from health_agent import labs
from health_agent.ingest import records
from health_agent.store import queries, sqlite_schema

FIXTURES = Path(__file__).parent / "fixtures"
RECORDS = FIXTURES / "records"

ALIGNED = RECORDS / "labs_2026-03-10.pdf"
TABLED = RECORDS / "labs_2025-09-12.pdf"
SCANNED = RECORDS / "scan_2025-03-04.pdf"

needs_ocr = pytest.mark.skipif(
    not records.ocr_available(),
    reason="Tesseract is not installed; OCR path cannot run here",
)


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #

def test_text_pdf_uses_the_text_layer():
    pages = records.extract_pages(ALIGNED, use_ocr=False)
    assert len(pages) == 1
    assert pages[0].extraction == "text"
    assert "NORTHSIDE" in pages[0].text


def test_layout_text_preserves_columns_that_plain_text_collapses():
    """The bug this guards: `extract_text()` collapses the whitespace that
    separates a lab report's columns, so the line parser sees
    "Glucose 92 mg/dL 70-99" and matches nothing. `layout=True` keeps the gaps.
    """
    page = records.extract_pages(ALIGNED, use_ocr=False)[0]
    plain = next(ln for ln in page.text.splitlines() if ln.startswith("Glucose"))
    laid_out = next(ln for ln in page.layout_text.splitlines()
                    if ln.strip().startswith("Glucose"))
    assert "  " not in plain          # columns are gone
    assert "  " in laid_out.strip()   # columns survive
    assert page.line_source is page.layout_text


def test_scanned_pdf_has_no_text_layer():
    """Sanity check on the fixture itself: if this ever gains a text layer, the
    OCR tests below would silently stop testing OCR."""
    pages = records.extract_pages(SCANNED, use_ocr=False)
    assert pages[0].extraction == "none"
    assert len(pages[0].text.strip()) < records.MIN_TEXT_CHARS


@needs_ocr
def test_scanned_pdf_is_read_via_ocr():
    pages = records.extract_pages(SCANNED, use_ocr=True)
    assert pages[0].extraction == "ocr"
    assert "Glucose" in pages[0].text


@needs_ocr
def test_ocr_actually_yields_lab_values():
    """The regression this guards against is subtle and was live for a while:
    OCR "succeeded" (text came back, the extraction was marked 'ocr') while
    producing zero lab values, because Tesseract's default page segmentation
    split the table into column blocks and no value shared a line with its
    analyte name. Asserting that OCR *ran* did not catch it; asserting that it
    produced data does.
    """
    pages = records.extract_pages(SCANNED, use_ocr=True)
    values = {v.analyte_key: v for v in records.extract_lab_values(pages)}
    assert len(values) >= 5
    assert values["glucose"].value_num == 104.0
    assert values["glucose"].flag == "H"
    assert values["hdl"].value_num == 44.0


@needs_ocr
def test_ocr_layout_text_preserves_columns():
    page = records.extract_pages(SCANNED, use_ocr=True)[0]
    result_line = next(ln for ln in page.layout_text.splitlines()
                       if ln.strip().startswith("Glucose"))
    assert "  " in result_line.strip()


@needs_ocr
def test_ocr_derived_values_are_marked():
    """OCR misreads digits, so these values carry uncertainty a text-layer
    value does not. They are stored, but never presented as equally solid."""
    pages = records.extract_pages(SCANNED, use_ocr=True)
    values = records.extract_lab_values(pages)
    assert values and all(v.via_ocr for v in values)


def test_text_layer_values_are_not_marked_as_ocr():
    pages = records.extract_pages(ALIGNED, use_ocr=False)
    values = records.extract_lab_values(pages)
    assert values and not any(v.via_ocr for v in values)


def test_ocr_reconstruction_from_word_boxes():
    """Column positions come from word bounding boxes, not from Tesseract's
    own line rendering, which collapses the gaps."""
    data = {
        "text": ["Glucose", "104", "mg/dL", ""],
        "conf": ["96", "95", "90", "-1"],
        "left": [100, 400, 600, 0],
        "width": [140, 45, 90, 0],
        "page_num": [1, 1, 1, 1],
        "block_num": [1, 1, 1, 1],
        "par_num": [1, 1, 1, 1],
        "line_num": [1, 1, 1, 1],
    }
    plain, layout = records._reconstruct_ocr_text(data)
    assert plain == "Glucose 104 mg/dL"
    assert "  " in layout
    assert layout.split()[:3] == ["Glucose", "104", "mg/dL"]


def test_low_confidence_words_are_dropped():
    data = {
        "text": ["Glucose", "~~", "104"],
        "conf": ["96", "3", "95"],
        "left": [100, 300, 400], "width": [140, 20, 45],
        "page_num": [1, 1, 1], "block_num": [1, 1, 1],
        "par_num": [1, 1, 1], "line_num": [1, 1, 1],
    }
    plain, _ = records._reconstruct_ocr_text(data)
    assert plain == "Glucose 104"


def test_missing_ocr_degrades_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(records, "ocr_page",
                        lambda *a, **k: (_ for _ in ()).throw(
                            records.OcrUnavailable("no tesseract")))
    stats = records.RecordStats()
    pages = records.extract_pages(SCANNED, use_ocr=True, stats=stats)
    assert pages[0].extraction == "none"
    assert stats.ocr_available is False
    assert stats.pages_empty == 1


# --------------------------------------------------------------------------- #
# Lab value extraction
# --------------------------------------------------------------------------- #

def test_aligned_report_yields_every_row():
    pages = records.extract_pages(ALIGNED, use_ocr=False)
    values = records.extract_lab_values(pages)
    assert len(values) == 23
    assert all(v.extracted_by == "line" for v in values)


def test_table_report_is_found_by_the_table_pass():
    pages = records.extract_pages(TABLED, use_ocr=False)
    values = records.extract_lab_values(pages)
    by_pass = {v.extracted_by for v in values}
    assert "table" in by_pass
    assert sum(1 for v in values if v.extracted_by == "table") == 16


def test_values_units_and_flags():
    pages = records.extract_pages(ALIGNED, use_ocr=False)
    values = {v.analyte_key: v for v in records.extract_lab_values(pages)}

    glucose = values["glucose"]
    assert glucose.value_num == 92.0
    assert glucose.unit == "mg/dL"
    assert (glucose.ref_low, glucose.ref_high) == (70.0, 99.0)
    assert glucose.flag is None

    ldl = values["ldl"]
    assert ldl.value_num == 112.0
    assert ldl.flag == "H"

    vitamin_d = values["vitamin_d"]
    assert vitamin_d.value_num == 28.4
    assert vitamin_d.flag == "L"


def test_one_sided_reference_ranges():
    """"<1.0" sets only a high bound and ">39" only a low one. Inventing the
    other end would fabricate a boundary the lab never printed."""
    pages = records.extract_pages(ALIGNED, use_ocr=False)
    values = {v.analyte_key: v for v in records.extract_lab_values(pages)}
    assert (values["hs_crp"].ref_low, values["hs_crp"].ref_high) == (None, 1.0)
    assert (values["hdl"].ref_low, values["hdl"].ref_high) == (39.0, None)
    assert (values["egfr"].ref_low, values["egfr"].ref_high) == (59.0, None)


def test_parse_reference_range_forms():
    assert records.parse_reference_range("70-99")[:2] == (70.0, 99.0)
    assert records.parse_reference_range("0.450 - 4.500")[:2] == (0.45, 4.5)
    assert records.parse_reference_range("3.5 to 5.1")[:2] == (3.5, 5.1)
    assert records.parse_reference_range("<100")[:2] == (None, 100.0)
    assert records.parse_reference_range(">40")[:2] == (40.0, None)
    # Unparseable ranges keep their printed text rather than being dropped.
    low, high, text = records.parse_reference_range("Not Estab.")
    assert (low, high) == (None, None)
    assert text == "Not Estab."


def test_header_and_address_lines_are_not_parsed_as_results():
    for line in (
        "Patient: SAMPLE, PAT A            DOB: 01/01/1990",
        "1400 Example Parkway, Springfield   Suite 200",
        "Page 1 of 2",
        "Specimen: 2026-0310-0042          Ordering: A. Provider, MD",
    ):
        assert records.extract_from_line(line, 1) is None


def test_every_value_carries_a_citation():
    """Plan §3.2: file path and page number attach to every extracted value."""
    pages = records.extract_pages(ALIGNED, use_ocr=False)
    values = records.extract_lab_values(pages)
    assert all(v.page_no == 1 for v in values)
    assert all(v.raw_line for v in values)


# --------------------------------------------------------------------------- #
# Analyte normalization
# --------------------------------------------------------------------------- #

def test_aliases_with_punctuation_resolve():
    """Regression: aliases were indexed raw but looked up normalized, so every
    alias containing punctuation silently never matched."""
    assert labs.resolve_key("AST (SGOT)")[0] == "ast"
    assert labs.resolve_key("ALT (SGPT)")[0] == "alt"
    assert labs.resolve_key("LDL Chol Calc (NIH)")[0] == "ldl"
    assert labs.resolve_key("Vitamin D, 25-Hydroxy")[0] == "vitamin_d"
    assert labs.resolve_key("Cholesterol, Total")[0] == "cholesterol_total"


def test_two_labs_naming_the_same_analyte_differently_merge():
    assert labs.resolve_key("Hemoglobin A1c")[0] == labs.resolve_key("HbA1c")[0]
    assert labs.resolve_key("LDL Cholesterol")[0] == labs.resolve_key("LDL-C")[0]


def test_ocr_digit_lookalikes_in_analyte_names_still_resolve():
    """OCR reads "A1c" as "Alc" or "AIc". Without this, a scanned report's A1c
    silently never joins the trend built from text-layer reports."""
    assert labs.resolve_key("Hemoglobin Alc")[0] == "hba1c"
    assert labs.resolve_key("Hemoglobin AIc")[0] == "hba1c"
    assert labs.resolve_key("HbAlc")[0] == "hba1c"


def test_ocr_correction_leaves_long_words_alone():
    """Folding letters to digits across a whole name would mangle real words
    ("alkaline" -> "a1ka1ine"), so only short tokens are eligible."""
    assert labs._ocr_variant("alkaline phosphatase") is None
    assert labs._ocr_variant("cholesterol") is None
    assert labs.resolve_key("Alkaline Phosphatase")[0] == "alk_phos"
    assert labs.resolve_key("Sodium")[0] == "sodium"


def test_no_analyte_name_ocr_folds_onto_a_different_analyte():
    """The real risk of digit-correcting names is mapping one analyte onto
    another — silently attributing a value to the wrong test. This proves no
    name in the registry does that, and re-proves it whenever one is added.
    """
    collisions = []
    for analyte in labs.REGISTRY:
        names = [analyte.key, labs.normalize_name(analyte.label)]
        names += [labs.normalize_name(a) for a in analyte.aliases]
        for name in names:
            variant = labs._ocr_variant(name)
            if variant and variant in labs._BY_ALIAS:
                landed = labs._BY_ALIAS[variant]
                if landed.key != analyte.key:
                    collisions.append((name, variant, analyte.key, landed.key))
    assert collisions == []


def test_unknown_analyte_is_kept_not_discarded():
    key, panel = labs.resolve_key("Widget Factor XII")
    assert key == "widget_factor_xii"
    assert panel is None


def test_unit_mismatch_is_flagged_but_advisory():
    assert labs.unit_looks_wrong("glucose", "mmol/L") is False   # a known unit
    assert labs.unit_looks_wrong("glucose", "ng/mL") is True     # implausible
    assert labs.unit_looks_wrong("glucose", None) is False
    assert labs.unit_looks_wrong("widget_factor", "furlongs") is False


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #

def test_collection_date_is_preferred_over_report_date():
    """Collected 03/10, reported 03/12 — a trend should plot the earlier one."""
    pages = records.extract_pages(ALIGNED, use_ocr=False)
    assert records.find_document_date(pages) == "2026-03-10"


def test_table_report_date():
    pages = records.extract_pages(TABLED, use_ocr=False)
    assert records.find_document_date(pages) == "2025-09-12"


def test_parse_date_formats():
    assert records.parse_date("03/10/2026") == "2026-03-10"
    assert records.parse_date("2026-03-10") == "2026-03-10"
    assert records.parse_date("March 10, 2026") == "2026-03-10"
    assert records.parse_date("not a date") is None


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #

def test_short_text_is_one_chunk():
    assert records.chunk_text("short note") == ["short note"]


def test_chunks_respect_the_target_size_and_overlap():
    text = "\n\n".join(f"Paragraph {i} " + "filler " * 30 for i in range(12))
    chunks = records.chunk_text(text, target_chars=500, overlap_chars=80)
    assert len(chunks) > 1
    # Every chunk after the first carries a tail of its predecessor.
    for previous, chunk in zip(chunks, chunks[1:]):
        assert chunk.startswith(previous[-80:].lstrip()[:20])


def test_empty_text_yields_no_chunks():
    assert records.chunk_text("   \n  ") == []


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

@pytest.fixture
def records_index(tmp_path):
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)

    def register(path: Path) -> tuple[int, bool]:
        from health_agent.ingest import healthkit
        return healthkit.register_source_file(
            conn, path, "record", healthkit.sha256_file(path))

    stats = records.ingest_records(conn, RECORDS, register=register, use_ocr=False)
    yield conn, stats
    conn.close()


def test_documents_pages_and_labs_are_stored(records_index):
    conn, stats = records_index
    assert stats.documents == 3
    assert conn.execute("SELECT COUNT(*) AS n FROM document").fetchone()["n"] == 3
    assert conn.execute("SELECT COUNT(*) AS n FROM document_page").fetchone()["n"] == 3
    # 23 from the aligned report + 16 from the table report.
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 39


def test_both_passes_finding_the_same_value_stores_it_once(records_index):
    """The table report's rows are visible to both passes; the dedup key means
    16 values are stored, not 32."""
    conn, stats = records_index
    assert stats.lab_values_duplicate == 16
    doc_id = conn.execute(
        "SELECT id FROM document WHERE path LIKE '%labs_2025-09-12%'"
    ).fetchone()["id"]
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM lab_result WHERE document_id = ? "
        "AND analyte_key = 'glucose'", (doc_id,),
    ).fetchone()["n"]
    assert n == 1


def test_reingesting_replaces_rather_than_duplicates(records_index):
    conn, _ = records_index
    before = conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"]

    from health_agent.ingest import healthkit

    def register(path: Path) -> tuple[int, bool]:
        return healthkit.register_source_file(
            conn, path, "record", healthkit.sha256_file(path))

    records.ingest_records(conn, RECORDS, register=register, use_ocr=False,
                           force=True)
    after = conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"]
    assert after == before


def test_fully_read_files_are_skipped_on_a_second_run(records_index):
    """Incremental refresh (plan §3.4), with one deliberate exception.

    The two text PDFs were read completely, so they're skipped. The scan had a
    page we couldn't read (no OCR here), so it stays 'partial' and is retried —
    otherwise installing Tesseract later would leave it permanently empty unless
    the user knew to pass --force.
    """
    conn, _ = records_index
    from health_agent.ingest import healthkit

    def register(path: Path) -> tuple[int, bool]:
        return healthkit.register_source_file(
            conn, path, "record", healthkit.sha256_file(path))

    stats = records.ingest_records(conn, RECORDS, register=register, use_ocr=False)
    assert stats.documents == 1

    statuses = {
        Path(r["path"]).name: r["status"]
        for r in conn.execute("SELECT path, status FROM source_file")
    }
    assert statuses["labs_2026-03-10.pdf"] == "complete"
    assert statuses["labs_2025-09-12.pdf"] == "complete"
    assert statuses["scan_2025-03-04.pdf"] == "partial"


def test_chunks_and_fts_are_populated(records_index):
    conn, stats = records_index
    assert stats.chunks > 0
    hits = conn.execute(
        "SELECT COUNT(*) AS n FROM chunk_fts WHERE chunk_fts MATCH '\"cholesterol\"'"
    ).fetchone()["n"]
    assert hits > 0


def test_corrupt_pdf_is_skipped_and_the_run_continues(tmp_path):
    """Plan §5a: one bad file must not cost the whole folder."""
    folder = tmp_path / "records"
    folder.mkdir()
    (folder / "broken.pdf").write_bytes(b"%PDF-1.4\nnot really a pdf")
    import shutil
    shutil.copy(ALIGNED, folder / "good.pdf")

    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)

    from health_agent.ingest import healthkit

    def register(path: Path) -> tuple[int, bool]:
        return healthkit.register_source_file(
            conn, path, "record", healthkit.sha256_file(path))

    stats = records.ingest_records(conn, folder, register=register, use_ocr=False)
    assert stats.documents == 1
    assert sum(stats.skipped.values()) == 1
    conn.close()


# --------------------------------------------------------------------------- #
# Lab trends
# --------------------------------------------------------------------------- #

def test_lab_trend_spans_reports_from_different_labs(records_index):
    conn, _ = records_index
    trend = queries.lab_trend(conn, "ldl")
    assert [p.value_num for p in trend.points] == [128.0, 112.0]
    assert [p.collected_date for p in trend.points] == ["2025-09-12", "2026-03-10"]
    # Each report printed the analyte differently; both land on the same key.
    assert {p.analyte for p in trend.points} == {"LDL Chol Calc (NIH)"}


def test_lab_trend_points_carry_citations(records_index):
    conn, _ = records_index
    trend = queries.lab_trend(conn, "hba1c")
    assert all(p.page_no == 1 for p in trend.points)
    assert trend.points[0].citation.startswith("labs_2025-09-12.pdf, p.1")


def test_out_of_range_detection(records_index):
    conn, _ = records_index
    trend = queries.lab_trend(conn, "hba1c")
    assert trend.points[0].out_of_range is True    # 5.7 vs 4.8-5.6
    assert trend.points[1].out_of_range is False   # 5.4 vs 4.8-5.6


def test_out_of_range_is_none_without_a_printed_range(records_index):
    conn, _ = records_index
    point = queries.LabPoint(
        collected_date="2026-01-01", value_num=5.0, value_text=None, unit=None,
        ref_low=None, ref_high=None, ref_text=None, flag=None, analyte="X",
        path="/tmp/x.pdf", page_no=1, raw_line="",
    )
    assert point.out_of_range is None


def test_missing_analyte_reports_coverage(records_index):
    conn, _ = records_index
    trend = queries.lab_trend(conn, "psa")
    assert trend.is_empty
    assert trend.total_results == 0


def test_window_outside_available_range(records_index):
    conn, _ = records_index
    trend = queries.lab_trend(conn, "ldl", start="2020-01-01", end="2020-12-31")
    assert trend.is_empty
    assert trend.total_results == 2
    assert trend.available_from == "2025-09-12"
