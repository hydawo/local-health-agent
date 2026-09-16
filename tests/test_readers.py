"""Containers become text; nothing here knows about the database."""
from __future__ import annotations

from pathlib import Path

import pytest

from health_agent.ingest import readers, records

DOCS = Path(__file__).parent / "fixtures" / "documents"

needs_ocr = pytest.mark.skipif(
    not records.ocr_available(),
    reason="Tesseract not installed; the image path is exercised in CI",
)
needs_heif = pytest.mark.skipif(
    not readers._heic_supported(),
    reason="pillow-heif not installed (the [ocr] extra); .heic is exercised in CI",
)


def test_docx_headings_become_markdown_headings():
    text = readers.read_docx(DOCS / "clinic-summary.docx")
    assert "# Clinic visit summary" in text
    assert "## Current medications" in text
    assert "## Allergies" in text


def test_docx_paragraphs_keep_their_order():
    text = readers.read_docx(DOCS / "clinic-summary.docx")
    assert text.index("Routine follow-up") < text.index("walking most days")
    assert text.index("walking most days") < text.index("## Current medications")
    assert text.index("## Current medications") < text.index("## Allergies")


def test_docx_tables_are_flattened_in_place():
    """A medication list is the likeliest thing to be in a table, and it
    must land under its heading, not at the end of the document."""
    text = readers.read_docx(DOCS / "clinic-summary.docx")
    assert "Metformin | 500 mg twice daily | type 2 diabetes" in text
    assert text.index("## Current medications") < text.index("Metformin |")
    assert text.index("Metformin |") < text.index("## Allergies")


def test_suffix_sets_are_lowercase_and_disjoint():
    assert all(s == s.lower() for s in readers.IMAGE_SUFFIXES | readers.DOCX_SUFFIXES)
    assert not (readers.IMAGE_SUFFIXES & readers.DOCX_SUFFIXES)
    assert {".png", ".jpg", ".jpeg", ".heic"} <= readers.IMAGE_SUFFIXES
    assert readers.DOCX_SUFFIXES == {".docx"}


@needs_ocr
def test_image_is_read_by_ocr():
    text = readers.read_image(DOCS / "medication-list.png").lower()
    assert "metformin" in text
    assert "atorvastatin" in text
    assert "500" in text


@needs_ocr
def test_sideways_phone_photo_is_transposed_before_ocr():
    """EXIF orientation is metadata; Tesseract reads pixels. Without the
    transpose a sideways photo OCRs to nothing usable."""
    upright = readers.read_image(DOCS / "medication-list.png").lower()
    rotated = readers.read_image(DOCS / "medication-list-rotated.jpg").lower()
    for word in ("metformin", "atorvastatin", "lisinopril"):
        assert word in upright
        assert word in rotated


@needs_ocr
@needs_heif
def test_heic_photo_is_read_by_ocr():
    """The iPhone default format, on a real HEIF container rather than a
    renamed PNG, so the pillow-heif opener is what gets proven."""
    text = readers.read_image(DOCS / "medication-list.heic").lower()
    assert "metformin" in text


def test_image_without_tesseract_raises_the_typed_error(monkeypatch):
    monkeypatch.setattr(records, "ocr_image",
                        lambda image: (_ for _ in ()).throw(
                            records.OcrUnavailable("tesseract missing")))
    with pytest.raises(records.OcrUnavailable):
        readers.read_image(DOCS / "medication-list.png")


def test_heic_without_pillow_heif_raises_the_typed_error(monkeypatch, tmp_path):
    monkeypatch.setattr(readers, "_heic_supported", lambda: False)
    fake = tmp_path / "IMG_0001.HEIC"
    fake.write_bytes(b"")
    with pytest.raises(readers.HeicUnsupported):
        readers.read_image(fake)


def test_ocr_image_is_the_single_ocr_path():
    """records.ocr_page and readers.read_image must share one Tesseract call;
    a second copy is how the --psm 6 and confidence-floor fixes get lost."""
    import inspect

    assert "ocr_image(" in inspect.getsource(records.ocr_page)
    assert "ocr_image(" in inspect.getsource(readers.read_image)
    assert "image_to_data" not in inspect.getsource(readers)
