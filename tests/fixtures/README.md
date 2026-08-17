# Test fixtures

**Everything in this folder is synthetic.** No file here contains, or is derived
from, anyone's real health data. The values are invented; only the *shape* — the
element names, attribute names, timestamp format, HealthKit type identifiers and
category values — mirrors what Apple's HealthKit exporter actually emits
(Export Version 14).

This is a hard project rule (plan §6): all development, unit tests, and agent
iteration run against these fixtures, never against a real `~/HealthData/`
folder. It also gives anyone cloning the repo something safe to try the tool
against.

## Files

| File | Purpose |
| --- | --- |
| `export.xml` | The main fixture. 25 top-level records, 1 workout, 2 activity summaries, spanning 2026-03-01 to 2026-03-09. |
| `export_malformed.xml` | Bad records mixed with good ones, for the skip-and-continue policy in plan §5a. |
| `records/labs_2026-03-10.pdf` | Bloodwork with whitespace-aligned columns and no table structure. 23 analytes. |
| `records/labs_2025-09-12.pdf` | Bloodwork as a ruled table plus prose. 16 analytes, same names at different values. |
| `records/scan_2025-03-04.pdf` | An image-only page with no text layer, forcing the OCR path. |
| `generate_pdfs.py` | Rebuilds the three PDFs. They are committed, so the suite doesn't need reportlab. |
| `notes/2026-03-11-sleep-log.md` | Frontmatter with an inline tag list, nested headings, one inline `#hashtag`. |
| `notes/2026-03-12-symptoms.md` | Frontmatter with a block tag list; prose that mentions a lab value. |
| `notes/awkward-frontmatter.md` | US-format date, comma-separated tags, a nested map, and a fenced code block containing a `#` line. |
| `notes/no-frontmatter.txt` | Plain text: no frontmatter, no headings, no date anywhere. |

## What `export.xml` is built to exercise

Each of these is a real-world behavior that a naive parser gets wrong, so the
fixture contains a case for it and `tests/test_healthkit_ingest.py` asserts on
hand-computed expected values.

| Case | In the fixture | Hand-verified expectation |
| --- | --- | --- |
| Discrete metric averaging | Resting HR, one sample/day, 2026-03-01..07: 58, 60, 62, 59, 61, 57, 63 | sum 420, mean exactly **60.0** |
| Cumulative metric summing | Steps on 2026-03-03, one source: 4000 + 4500 | **8500** |
| Multi-source double counting | Steps on 2026-03-02: Watch 1000+2000+3000, Phone 2500+3000 | `combine=max` → **6000**, `combine=sum` → **11500** |
| Correlation duplicates | Blood pressure appears once at top level and again inside `<Correlation>` | **2** records stored, **2** nested children skipped |
| Sleep across midnight | InBed 22:30–23:00 on 03-04; Core/Deep/REM after midnight on 03-05 | 03-04: 1800s InBed. 03-05: 7200s Core, 2700s Deep, 2700s REM |
| Category values as text | Sleep stages are strings, not numbers | stored in `value_text`, never coerced to 0 |
| DST offset change | Records before 2026-03-08 carry `-0500`, after carry `-0400` | each record's calendar date comes from its own offset |
| Metadata children | One HRV record has a `<MetadataEntry>` | stored as JSON |
| Workout statistics | Running workout with a `<WorkoutStatistics>` child | stored as JSON |

### Known caveat, recorded deliberately

Sleep is attributed to the calendar date each **segment starts on**, so a single
night's sleep splits across two dates (03-04 and 03-05 above). "Night of" framing
needs a session-stitching pass; that is a milestone 5+ concern, and the fixture
keeps the current behavior visible rather than hiding it.

## What the record PDFs are built to exercise

| Case | Where | Hand-verified expectation |
| --- | --- | --- |
| Whitespace-aligned layout | `labs_2026-03-10.pdf` | 23 values, all found by the line pass |
| Ruled table layout | `labs_2025-09-12.pdf` | 16 values found by the table pass |
| Both passes seeing one value | `labs_2025-09-12.pdf` | 16 duplicates collapsed; 39 rows stored in total, not 55 |
| Cross-lab name merging | both | "LDL Chol Calc (NIH)" in one and the table row in the other both key to `ldl` |
| Two-point trends | both | LDL 128 → 112, HbA1c 5.7 → 5.4 |
| One-sided reference ranges | `labs_2026-03-10.pdf` | `<1.0` → (None, 1.0); `>39` → (39.0, None) |
| Collection vs report date | both | collected 03/10 is used, not reported 03/12 |
| Flags as printed | both | LDL flagged `H`, vitamin D flagged `L` |
| Out-of-range detection | both | HbA1c 5.7 vs 4.8-5.6 → out of range; 5.4 → in range |
| No text layer at all | `scan_2025-03-04.pdf` | `extraction == 'none'` without OCR, `'ocr'` with it |
| Header/address lines | all | "Patient:", "Specimen:", "Page 1 of 2" never parse as results |

### Why the aligned report exists as its own fixture

pdfplumber's `extract_text()` collapses runs of whitespace, so a report whose
columns are aligned with spaces arrives as `Glucose 92 mg/dL 70-99` and the line
parser finds nothing at all — silently, with no error. The parser extracts each
page a second time with `layout=True` to recover the column gaps. This fixture is
what keeps that regression from coming back.

## What the notes are built to exercise

| Case | Where | Hand-verified expectation |
| --- | --- | --- |
| Inline list frontmatter | sleep-log | `tags: [sleep, energy, caffeine]` |
| Block list frontmatter | symptoms | `- headache` / `- hydration` / `- followup` |
| Comma-separated tag string | awkward | `tags: exercise, recovery` → two tags |
| Inline `#hashtag` | sleep-log | `#followup` joins the frontmatter tags |
| Numeric `#1` is not a tag | unit test | prose like "issue #1" yields no tags |
| Date from frontmatter | sleep-log | 2026-03-11 |
| Date from filename | any | `2026-03-12-symptoms.md` → 2026-03-12 |
| US-format date | awkward | `03/14/2026` → 2026-03-14 |
| Undated stays undated | no-frontmatter | `None`, never the file's mtime |
| Heading trail | sleep-log | `Sleep log, week of March 9 > Night by night` |
| `#` inside a code fence | awkward | not treated as a heading |
| Unsupported frontmatter | awkward | nested map kept under `_unparsed`, note still ingested |
| Notes never yield lab values | all | `lab_result` stays empty after a notes-only ingest |

### Why "undated" is a real outcome

A note with no date in its frontmatter or filename is reported as undated. The
obvious fallback — file modification time — is actively wrong: copying a folder
rewrites every mtime, silently re-dating years of notes to the day they moved.

### OCR tests skip when Tesseract is missing

`scan_2025-03-04.pdf` needs the Tesseract binary. Tests that require it are
marked skipped rather than failed, so the suite is green on a machine without it
while still reporting that the path went unverified.

### Measured OCR accuracy on this fixture

Tesseract 5.5.3, page rendered at 300 DPI from a 200 DPI source, `--psm 6`:

| | Correct |
| --- | --- |
| Analytes found | 6 / 6 |
| **Values** | **6 / 6** |
| **Flags (H/L)** | **6 / 6** |
| Units | 4 / 6 |
| Reference ranges | 4 / 6 |

The shape of the errors matters more than the count: the primary datum — the
result value — comes through reliably, while the surrounding metadata degrades.
One reference range of `100-199` is read as `100-139`, which is a *plausible
wrong number*, exactly the failure this project treats as worse than a missing
one. That is why every OCR-derived value is stored with `via_ocr = 1` and shown
with an `[OCR]` marker and a warning to check it against the original.

Two earlier versions of this fixture were worse in instructive ways:

- **Rendered at 100 DPI**, Tesseract misread most digits — that made the fixture
  a test of OCR on a bad scan rather than of this pipeline.
- **Read with default page segmentation**, the table came back as column blocks:
  every analyte name in one run, every value in another, so nothing shared a
  line. OCR "worked" and produced **zero** lab values.

## Coverage

All three source types in the plan's MVP (HealthKit, records, notes) now have
fixtures here. Later milestones extend these rather than adding new folders.
