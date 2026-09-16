"""Generate the synthetic drop-folder documents in `documents/`.

**Every value here is invented.** The medication names are real drug names
because OCR and search have to be exercised against words that look like
what people actually write, but no dose, date, condition, clinic, or person
here corresponds to anyone. Same rule as `generate_pdfs.py`.

Run after changing a fixture (python-docx, Pillow, and pillow-heif are dev
dependencies; pillow-heif comes with the `[ocr]` extra):

    python tests/fixtures/generate_documents.py

The generated files are committed so the suite doesn't depend on fonts.

Four files:

  clinic-summary.docx          a Heading 1, prose, a Heading 2, and a table,
                               so heading levels and table flattening are
                               both exercised
  medication-list.png          four lines of large, clean type: the best case
                               for Tesseract, so the test asserts on content
  medication-list-rotated.jpg  the same pixels rotated 90 degrees and tagged
                               with EXIF orientation 6, as a phone would save
                               a sideways photo; the reader must transpose it
  medication-list.heic         the same pixels in the iPhone default format,
                               so the pillow-heif path is proven on a real
                               HEIF container rather than on a renamed file
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parent / "documents"

LINES = [
    "Current medications",
    "Metformin 500 mg twice daily",
    "Atorvastatin 20 mg at night",
    "Lisinopril 10 mg each morning",
    "Allergies: penicillin",
]


def _font(size: int):
    from PIL import ImageFont

    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def make_png(path: Path) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1400, 700), "white")
    draw = ImageDraw.Draw(image)
    font = _font(56)
    y = 60
    for line in LINES:
        draw.text((80, y), line, fill="black", font=font)
        y += 110
    image.save(path)


def make_rotated_jpg(png: Path, path: Path) -> None:
    from PIL import Image

    upright = Image.open(png)
    # Orientation 6 means "rotate 90 CW to display", so the stored pixels
    # must be 90 CCW of upright (PIL's positive angle is CCW). This is what a
    # phone writes for a photo taken sideways; exif_transpose undoes it.
    sideways = upright.rotate(90, expand=True)
    exif = Image.Exif()
    exif[0x0112] = 6
    sideways.save(path, quality=95, exif=exif.tobytes())


def make_heic(png: Path, path: Path) -> None:
    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    with Image.open(png) as image:
        image.save(path, format="HEIF")


def make_docx(path: Path) -> None:
    import docx

    document = docx.Document()
    document.add_heading("Clinic visit summary", level=1)
    document.add_paragraph(
        "Routine follow-up. Blood pressure at the visit was within the "
        "clinic's usual range and no change was made to the plan.")
    document.add_paragraph(
        "The patient reports walking most days and sleeping well.")
    document.add_heading("Current medications", level=2)
    table = document.add_table(rows=1, cols=3)
    table.rows[0].cells[0].text = "Medication"
    table.rows[0].cells[1].text = "Dose"
    table.rows[0].cells[2].text = "Reason"
    for name, dose, reason in (
        ("Metformin", "500 mg twice daily", "type 2 diabetes"),
        ("Atorvastatin", "20 mg at night", "cholesterol"),
    ):
        row = table.add_row().cells
        row[0].text, row[1].text, row[2].text = name, dose, reason
    document.add_heading("Allergies", level=2)
    document.add_paragraph("Penicillin (rash).")
    document.save(path)


def main() -> None:
    HERE.mkdir(exist_ok=True)
    png = HERE / "medication-list.png"
    make_png(png)
    make_rotated_jpg(png, HERE / "medication-list-rotated.jpg")
    make_heic(png, HERE / "medication-list.heic")
    make_docx(HERE / "clinic-summary.docx")
    print(f"wrote 4 files to {HERE}")


if __name__ == "__main__":
    main()
