"""Generate the synthetic bloodwork PDFs in this folder.

**Every value here is invented.** These are not derived from anyone's real lab
results — the analytes and reference ranges are realistic in *format* so the
parser is exercised against the shapes real reports use, but the numbers,
patient, provider, and lab are fictional.

Run after changing a fixture (reportlab is a dev dependency):

    python tests/fixtures/generate_pdfs.py

The generated PDFs are committed so the test suite doesn't depend on reportlab.

Three files, covering the three layouts the parser has to handle:

  labs_2026-03-10.pdf   whitespace-aligned columns, no ruled table — the
                        common "text PDF from a lab portal" case, invisible to
                        pdfplumber's table finder, handled by the line regex
  labs_2025-09-12.pdf   a ruled table plus prose, so the table pass and the
                        line pass both have something to find; same analytes as
                        above at different values, so trends have >1 point
  scan_2025-03-04.pdf   an image-only page with no text layer, which forces the
                        OCR path (skipped when Tesseract isn't installed)
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

HERE = Path(__file__).parent

MONO = "Courier"
MONO_BOLD = "Courier-Bold"

# (analyte, value, flag, unit, reference range) — all invented.
PANEL_MARCH = [
    ("Glucose", "92", "", "mg/dL", "70-99"),
    ("BUN", "15", "", "mg/dL", "6-24"),
    ("Creatinine", "0.95", "", "mg/dL", "0.76-1.27"),
    ("eGFR", "98", "", "mL/min/1.73", ">59"),
    ("Sodium", "140", "", "mmol/L", "134-144"),
    ("Potassium", "4.3", "", "mmol/L", "3.5-5.2"),
    ("Chloride", "101", "", "mmol/L", "96-106"),
    ("Calcium", "9.4", "", "mg/dL", "8.7-10.2"),
    ("Total Protein", "6.9", "", "g/dL", "6.0-8.5"),
    ("Albumin", "4.5", "", "g/dL", "3.8-4.9"),
    ("Bilirubin Total", "0.6", "", "mg/dL", "0.0-1.2"),
    ("Alkaline Phosphatase", "68", "", "IU/L", "44-121"),
    ("AST (SGOT)", "22", "", "IU/L", "0-40"),
    ("ALT (SGPT)", "27", "", "IU/L", "0-44"),
    ("Cholesterol, Total", "186", "", "mg/dL", "100-199"),
    ("Triglycerides", "118", "", "mg/dL", "0-149"),
    ("HDL Cholesterol", "52", "", "mg/dL", ">39"),
    ("LDL Chol Calc (NIH)", "112", "H", "mg/dL", "0-99"),
    ("Hemoglobin A1c", "5.4", "", "%", "4.8-5.6"),
    ("TSH", "2.10", "", "uIU/mL", "0.450-4.500"),
    ("Vitamin D, 25-Hydroxy", "28.4", "L", "ng/mL", "30.0-100.0"),
    ("Ferritin", "88", "", "ng/mL", "30-400"),
    ("hs-CRP", "0.9", "", "mg/L", "<1.0"),
]

# Same analytes, earlier date, different values — gives every trend two points.
PANEL_SEPTEMBER = [
    ("Glucose", "99", "", "mg/dL", "70-99"),
    ("BUN", "17", "", "mg/dL", "6-24"),
    ("Creatinine", "1.02", "", "mg/dL", "0.76-1.27"),
    ("Sodium", "139", "", "mmol/L", "134-144"),
    ("Potassium", "4.1", "", "mmol/L", "3.5-5.2"),
    ("Calcium", "9.2", "", "mg/dL", "8.7-10.2"),
    ("Albumin", "4.4", "", "g/dL", "3.8-4.9"),
    ("AST (SGOT)", "25", "", "IU/L", "0-40"),
    ("ALT (SGPT)", "31", "", "IU/L", "0-44"),
    ("Cholesterol, Total", "204", "H", "mg/dL", "100-199"),
    ("Triglycerides", "155", "H", "mg/dL", "0-149"),
    ("HDL Cholesterol", "47", "", "mg/dL", ">39"),
    ("LDL Chol Calc (NIH)", "128", "H", "mg/dL", "0-99"),
    ("Hemoglobin A1c", "5.7", "H", "%", "4.8-5.6"),
    ("TSH", "1.87", "", "uIU/mL", "0.450-4.500"),
    ("Vitamin D, 25-Hydroxy", "22.1", "L", "ng/mL", "30.0-100.0"),
]


def build_aligned_report(path: Path) -> None:
    """Whitespace-aligned columns drawn directly — no table structure at all."""
    c = pdfcanvas.Canvas(str(path), pagesize=LETTER)
    width, height = LETTER
    y = height - 0.9 * inch

    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, y, "NORTHSIDE CLINICAL LABORATORY")
    y -= 18
    c.setFont("Helvetica", 9)
    c.drawString(1 * inch, y, "1400 Example Parkway, Springfield  ·  CLIA 00D0000000")
    y -= 22

    c.setFont("Helvetica", 10)
    for line in (
        "Patient: SAMPLE, PAT A            DOB: 01/01/1990        Sex: U",
        "Specimen: 2026-0310-0042          Ordering: A. Provider, MD",
        "Collected: 03/10/2026             Reported: 03/12/2026",
    ):
        c.drawString(1 * inch, y, line)
        y -= 14
    y -= 10

    c.setFont(MONO_BOLD, 9)
    c.drawString(1 * inch, y, f"{'TEST':<28}{'RESULT':>8}  {'':2}{'UNITS':<14}{'REFERENCE':<16}")
    y -= 4
    c.line(1 * inch, y, width - 1 * inch, y)
    y -= 12

    c.setFont(MONO, 9)
    for analyte, value, flag, unit, ref in PANEL_MARCH:
        if y < 1.2 * inch:
            c.showPage()
            y = height - 1 * inch
            c.setFont(MONO, 9)
        # Two-plus spaces between every column: the layout the line regex targets.
        c.drawString(
            1 * inch,
            y,
            f"{analyte:<28}{value:>8}  {flag:<2}{unit:<14}{ref:<16}",
        )
        y -= 12

    y -= 16
    c.setFont("Helvetica", 9)
    for line in (
        "Comment: Fasting specimen. Lipid panel calculated by the NIH equation.",
        "Vitamin D below the stated reference interval; repeat testing suggested",
        "by the ordering provider at the next scheduled visit.",
        "This is synthetic data generated for software testing. Not a real report.",
    ):
        c.drawString(1 * inch, y, line)
        y -= 12

    c.showPage()
    c.save()


def build_table_report(path: Path) -> None:
    """A ruled table plus prose, so both extraction passes have work to do."""
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(path), pagesize=LETTER,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        topMargin=0.9 * inch, bottomMargin=0.9 * inch,
        title="Synthetic Laboratory Report",
    )

    story = [
        Paragraph("VALLEY REFERENCE LABS", styles["Title"]),
        Paragraph("Comprehensive Metabolic Panel with Lipids", styles["Heading3"]),
        Spacer(1, 8),
        Paragraph(
            "Patient: SAMPLE, PAT A &nbsp;&nbsp;|&nbsp;&nbsp; DOB: 01/01/1990"
            " &nbsp;&nbsp;|&nbsp;&nbsp; Accession: VR-2025-88121",
            styles["Normal"]),
        Paragraph(
            "Collected: 09/12/2025 &nbsp;&nbsp;|&nbsp;&nbsp; Received: 09/12/2025"
            " &nbsp;&nbsp;|&nbsp;&nbsp; Reported: 09/15/2025",
            styles["Normal"]),
        Spacer(1, 14),
    ]

    data = [["Test", "Result", "Flag", "Units", "Reference Range"]]
    data.extend([[a, v, f, u, r] for a, v, f, u, r in PANEL_SEPTEMBER])

    table = Table(data, colWidths=[2.3 * inch, 0.85 * inch, 0.5 * inch,
                                   0.95 * inch, 1.5 * inch])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dfe6ec")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 1), (2, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(table)

    story.extend([
        Spacer(1, 18),
        Paragraph("Interpretation", styles["Heading4"]),
        Paragraph(
            "Lipid values are above the stated reference intervals, with total "
            "cholesterol and calculated LDL both flagged high. Hemoglobin A1c "
            "of 5.7 percent falls in the range the laboratory annotates as "
            "increased risk. Renal and hepatic markers are within the printed "
            "intervals. Repeat fasting lipids in six months were suggested by "
            "the ordering provider.",
            styles["BodyText"]),
        Paragraph(
            "Vitamin D remains below the reference interval despite "
            "supplementation noted on the requisition.",
            styles["BodyText"]),
        Spacer(1, 10),
        Paragraph(
            "<i>Synthetic data generated for software testing. Not a real "
            "laboratory report and not associated with any person.</i>",
            styles["BodyText"]),
    ])
    doc.build(story)


def build_scanned_report(path: Path) -> None:
    """An image-only page: text drawn into a bitmap, so there is no text layer.

    This is what a phone photo or a fax-scanned report looks like to pdfplumber —
    `extract_text()` returns nothing and the OCR fallback has to carry it.
    """
    from PIL import Image, ImageDraw, ImageFont

    scale = 2
    img = Image.new("RGB", (850 * scale, 1100 * scale), "white")
    draw = ImageDraw.Draw(img)

    def font(size: int, bold: bool = False):
        for candidate in (
            "/System/Library/Fonts/Supplemental/Courier New Bold.ttf" if bold
            else "/System/Library/Fonts/Supplemental/Courier New.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        ):
            try:
                return ImageFont.truetype(candidate, size * scale)
            except OSError:
                continue
        return ImageFont.load_default()

    y = 60 * scale
    draw.text((60 * scale, y), "HILLCREST MEDICAL GROUP", fill="black",
              font=font(20, bold=True))
    y += 40 * scale
    for line in (
        "Patient: SAMPLE, PAT A          DOB: 01/01/1990",
        "Collected: 03/04/2025           Reported: 03/06/2025",
        "",
        "TEST                    RESULT    UNITS      REFERENCE",
        "--------------------------------------------------------",
        "Glucose                     104 H  mg/dL      70-99",
        "Hemoglobin A1c              5.9 H  %          4.8-5.6",
        "Cholesterol, Total          212 H  mg/dL      100-199",
        "HDL Cholesterol              44    mg/dL      >39",
        "Triglycerides               168 H  mg/dL      0-149",
        "TSH                        2.44    uIU/mL     0.450-4.500",
        "",
        "Synthetic data for software testing. Not a real report.",
    ):
        draw.text((60 * scale, y), line, fill="black", font=font(13))
        y += 26 * scale

    # A little grey noise band, the way a real scan has artifacts.
    draw.rectangle([(0, 1060 * scale), (850 * scale, 1064 * scale)], fill="#d8d8d8")

    # Saved at the full 1700x2200 (200 DPI on Letter), which is what a real
    # scanner or a phone photo produces. An earlier version downsampled this to
    # 100 DPI and Tesseract misread most of the digits — that made the fixture a
    # test of OCR on a bad scan rather than of our own pipeline.
    img.save(path, "PDF", resolution=200.0)


def main() -> None:
    targets = [
        (HERE / "records" / "labs_2026-03-10.pdf", build_aligned_report),
        (HERE / "records" / "labs_2025-09-12.pdf", build_table_report),
        (HERE / "records" / "scan_2025-03-04.pdf", build_scanned_report),
    ]
    (HERE / "records").mkdir(parents=True, exist_ok=True)
    for path, builder in targets:
        builder(path)
        print(f"wrote {path.relative_to(HERE.parent.parent)} "
              f"({path.stat().st_size / 1024:,.0f} KB)")


if __name__ == "__main__":
    main()
