"""Notes ingestion tests, against the synthetic markdown fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from health_agent.ingest import healthkit, notes, records
from health_agent.store import queries, sqlite_schema

NOTES = Path(__file__).parent / "fixtures" / "notes"

SLEEP_LOG = NOTES / "2026-03-11-sleep-log.md"
SYMPTOMS = NOTES / "2026-03-12-symptoms.md"
AWKWARD = NOTES / "awkward-frontmatter.md"
PLAIN = NOTES / "no-frontmatter.txt"


# --------------------------------------------------------------------------- #
# Frontmatter
# --------------------------------------------------------------------------- #

def test_inline_list_frontmatter():
    meta, body = notes.parse_frontmatter(SLEEP_LOG.read_text())
    assert meta["title"] == "Sleep log, week of March 9"
    assert meta["tags"] == ["sleep", "energy", "caffeine"]
    assert body.lstrip().startswith("# Sleep log")
    assert "---" not in body.splitlines()[0]


def test_block_list_frontmatter():
    meta, _ = notes.parse_frontmatter(SYMPTOMS.read_text())
    assert meta["tags"] == ["headache", "hydration", "followup"]


def test_frontmatter_is_stripped_from_the_body():
    """YAML in an embedding vector is noise; the body must exclude it."""
    _, body = notes.parse_frontmatter(SYMPTOMS.read_text())
    assert "severity" not in body
    assert "Headache notes" in body


def test_note_without_frontmatter_is_unchanged():
    text = PLAIN.read_text()
    meta, body = notes.parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_unsupported_frontmatter_is_preserved_not_guessed():
    """Nested maps and stray lines are kept verbatim rather than mis-parsed."""
    meta, _ = notes.parse_frontmatter(AWKWARD.read_text())
    assert meta["title"] == "Frontmatter the simple parser only partly understands"
    assert meta["count"] == 3
    assert meta["enabled"] is True
    unparsed = " ".join(meta["_unparsed"])
    assert "not supported" in unparsed
    assert "weird line without a colon" in unparsed


def test_scalar_types():
    meta, _ = notes.parse_frontmatter(
        '---\na: "quoted"\nb: 42\nc: 1.5\nd: true\ne: null\nf: bare\n---\nbody')
    assert meta == {"a": "quoted", "b": 42, "c": 1.5, "d": True, "e": None,
                    "f": "bare"}


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #

def test_date_from_frontmatter():
    meta, _ = notes.parse_frontmatter(SLEEP_LOG.read_text())
    assert notes.extract_date(meta, SLEEP_LOG) == "2026-03-11"


def test_date_from_filename_when_frontmatter_has_none():
    assert notes.extract_date({}, Path("2026-03-11-sleep-log.md")) == "2026-03-11"
    assert notes.extract_date({}, Path("2026-03-11.md")) == "2026-03-11"


def test_us_format_date_in_frontmatter():
    meta, _ = notes.parse_frontmatter(AWKWARD.read_text())
    assert notes.extract_date(meta, AWKWARD) == "2026-03-14"


def test_date_with_a_time_component_is_trimmed():
    assert notes.extract_date({"date": "2026-03-11T08:30:00Z"},
                              Path("x.md")) == "2026-03-11"


def test_undated_notes_are_reported_not_invented():
    """Deliberately no mtime fallback: copying a folder rewrites mtimes and
    would silently re-date years of notes to the day they were moved."""
    assert notes.extract_date({}, PLAIN) is None


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #

def test_tags_from_frontmatter_and_inline_hashtags():
    meta, body = notes.parse_frontmatter(SLEEP_LOG.read_text())
    tags = notes.extract_tags(meta, body)
    assert set(tags) == {"sleep", "energy", "caffeine", "followup"}


def test_comma_separated_tag_string():
    meta, body = notes.parse_frontmatter(AWKWARD.read_text())
    assert set(notes.extract_tags(meta, body)) == {"exercise", "recovery"}


def test_tags_are_deduped_and_lowercased():
    assert notes.extract_tags({"tags": ["Sleep", "sleep"]}, "#SLEEP") == ["sleep"]


def test_numeric_hashtags_are_not_tags():
    """"#1" and "#404" are prose, not tags."""
    assert notes.extract_tags({}, "issue #1 and error #404") == []


def test_markdown_headings_are_not_tags():
    assert notes.extract_tags({}, "# Heading\n## Another") == []


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #

def test_section_trail_records_the_heading_chain():
    _, body = notes.parse_frontmatter(SLEEP_LOG.read_text())
    trails = [trail for trail, _ in notes.split_sections(body)]
    assert "Sleep log, week of March 9 > Night by night" in trails


def test_text_before_the_first_heading_has_no_trail():
    sections = notes.split_sections("intro text\n\n# First\nbody")
    assert sections[0][0] is None
    assert sections[1][0] == "First"


def test_headings_inside_code_fences_are_not_headings():
    """A '# comment' in a Python block would otherwise corrupt every trail
    after it."""
    _, body = notes.parse_frontmatter(AWKWARD.read_text())
    trails = [trail for trail, _ in notes.split_sections(body)]
    assert "Frontmatter edge cases > Code block check" in trails
    assert not any("comment" in (t or "").lower() for t in trails)


def test_deeper_headings_nest_and_siblings_replace():
    sections = notes.split_sections(
        "# A\ntext\n## B\ntext\n### C\ntext\n## D\ntext")
    trails = [t for t, _ in sections]
    assert trails == ["A", "A > B", "A > B > C", "A > D"]


def test_chunk_note_keeps_the_trail_on_every_chunk():
    _, body = notes.parse_frontmatter(SLEEP_LOG.read_text())
    chunks = notes.chunk_note(body)
    assert chunks
    assert all(isinstance(text, str) and text for _, text in chunks)
    assert any(trail and ">" in trail for trail, _ in chunks)


# --------------------------------------------------------------------------- #
# Titles
# --------------------------------------------------------------------------- #

def test_title_prefers_frontmatter_then_heading_then_filename():
    assert notes.extract_title({"title": "From meta"}, "# Heading",
                               Path("f.md")) == "From meta"
    assert notes.extract_title({}, "# Heading\nbody", Path("f.md")) == "Heading"
    assert notes.extract_title({}, "just text", Path("f.md")) == "f"


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

@pytest.fixture
def notes_index(tmp_path):
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)

    def register(path: Path) -> tuple[int, bool]:
        return healthkit.register_source_file(
            conn, path, "note", healthkit.sha256_file(path))

    stats = notes.ingest_notes(conn, NOTES, register=register)
    yield conn, stats
    conn.close()


def test_all_notes_are_stored(notes_index):
    conn, stats = notes_index
    assert stats.notes == 4
    assert stats.chunks > 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM document WHERE kind = 'note'"
    ).fetchone()["n"] == 4


def test_notes_never_produce_lab_results(notes_index):
    """"My LDL was 112" in a journal is recollection, not a lab result — it must
    not enter a trend that is supposed to cite a report."""
    conn, _ = notes_index
    assert conn.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 0


def test_tags_are_stored_and_queryable(notes_index):
    conn, _ = notes_index
    tagged = queries.notes_by_tag(conn, "followup")
    assert len(tagged) == 2
    assert queries.notes_by_tag(conn, "#Followup") == tagged  # normalized


def test_frontmatter_is_kept_whole(notes_index):
    conn, _ = notes_index
    row = conn.execute(
        "SELECT frontmatter_json FROM document WHERE path LIKE '%awkward%'"
    ).fetchone()
    assert "_unparsed" in row["frontmatter_json"]


def test_chunks_carry_their_section(notes_index):
    conn, _ = notes_index
    sections = [
        r["section"] for r in conn.execute(
            "SELECT c.section FROM chunk c JOIN document d ON d.id = c.document_id "
            "WHERE d.path LIKE '%sleep-log%'")
    ]
    assert any(s and "Night by night" in s for s in sections)


def test_note_summary(notes_index):
    conn, _ = notes_index
    summary = queries.note_summary(conn)
    assert summary["notes"] == 4
    assert summary["undated"] == 1
    assert summary["first"] == "2026-03-11"
    assert summary["last"] == "2026-03-14"


def test_editing_a_note_replaces_it(notes_index, tmp_path):
    """An edited note is a new version of itself; keeping both would return
    stale text next to current text with no way to tell them apart."""
    conn, _ = notes_index
    folder = tmp_path / "notes"
    folder.mkdir()
    target = folder / "journal.md"
    target.write_text("---\ndate: 2026-03-01\n---\n\n# Journal\n\noriginal text")

    def register(path: Path) -> tuple[int, bool]:
        return healthkit.register_source_file(
            conn, path, "note", healthkit.sha256_file(path))

    notes.ingest_notes(conn, folder, register=register)
    target.write_text("---\ndate: 2026-03-01\n---\n\n# Journal\n\nrevised text")
    notes.ingest_notes(conn, folder, register=register)

    rows = conn.execute(
        "SELECT c.text FROM chunk c JOIN document d ON d.id = c.document_id "
        "WHERE d.path LIKE '%journal%'"
    ).fetchall()
    combined = " ".join(r["text"] for r in rows)
    assert "revised text" in combined
    assert "original text" not in combined


def test_unchanged_notes_are_skipped(notes_index):
    conn, _ = notes_index

    def register(path: Path) -> tuple[int, bool]:
        return healthkit.register_source_file(
            conn, path, "note", healthkit.sha256_file(path))

    stats = notes.ingest_notes(conn, NOTES, register=register)
    assert stats.notes == 0


def test_unreadable_note_is_skipped_and_run_continues(tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "binary.md").write_bytes(b"\xff\xfe\x00valid enough after replace")
    (folder / "good.md").write_text("# Good\n\nreal content here")

    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)

    def register(path: Path) -> tuple[int, bool]:
        return healthkit.register_source_file(
            conn, path, "note", healthkit.sha256_file(path))

    stats = notes.ingest_notes(conn, folder, register=register)
    # The undecodable file is salvaged with replacements rather than lost.
    assert stats.notes == 2
    conn.close()


def test_find_notes_picks_up_md_and_txt():
    found = {p.name for p in notes.find_notes(NOTES)}
    assert "no-frontmatter.txt" in found
    assert "2026-03-11-sleep-log.md" in found


DOCS = Path(__file__).parent / "fixtures" / "documents"

needs_ocr = pytest.mark.skipif(
    not records.ocr_available(),
    reason="Tesseract not installed; the image path is exercised in CI",
)


def _register_for(conn):
    def register(path: Path) -> tuple[int, bool]:
        return healthkit.register_source_file(
            conn, path, "note", healthkit.sha256_file(path))
    return register


@pytest.fixture
def fresh_index(tmp_path):
    conn = sqlite_schema.connect(tmp_path / "health.db", create=True)
    sqlite_schema.initialize(conn)
    yield conn
    conn.close()


def test_docx_ingests_as_a_note_with_a_heading_trail(fresh_index, tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    shutil.copy(DOCS / "clinic-summary.docx", folder / "clinic-summary.docx")
    stats = notes.ingest_notes(fresh_index, folder, register=_register_for(fresh_index))

    assert stats.notes == 1
    doc = fresh_index.execute("SELECT kind, extraction, title FROM document").fetchone()
    assert doc["kind"] == "note"
    assert doc["extraction"] == "text"
    sections = [r["section"] for r in fresh_index.execute("SELECT section FROM chunk")]
    assert any(s and "Current medications" in s for s in sections)
    assert fresh_index.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 0


@needs_ocr
def test_image_ingests_as_kind_image_read_by_ocr(fresh_index, tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    shutil.copy(DOCS / "medication-list.png", folder / "IMG_0042.PNG")
    stats = notes.ingest_notes(fresh_index, folder, register=_register_for(fresh_index))

    assert stats.notes == 1
    assert stats.images == 1
    doc = fresh_index.execute("SELECT kind, extraction FROM document").fetchone()
    assert doc["kind"] == "image"
    assert doc["extraction"] == "ocr"
    text = fresh_index.execute("SELECT text FROM document_page").fetchone()["text"].lower()
    assert "metformin" in text
    # Photos are evidence you can search, never data you can trend.
    assert fresh_index.execute("SELECT COUNT(*) AS n FROM lab_result").fetchone()["n"] == 0


def test_image_is_skipped_and_counted_when_ocr_is_unavailable(
        fresh_index, tmp_path, monkeypatch):
    from health_agent.ingest import readers, records as records_mod

    monkeypatch.setattr(records_mod, "ocr_available", lambda: False)
    monkeypatch.setattr(readers, "read_image", lambda path: (_ for _ in ()).throw(
        records_mod.OcrUnavailable("no tesseract")))
    folder = tmp_path / "notes"
    folder.mkdir()
    shutil.copy(DOCS / "medication-list.png", folder / "photo.png")
    stats = notes.ingest_notes(fresh_index, folder, register=_register_for(fresh_index))

    assert stats.notes == 0
    assert stats.skipped == {"ocr unavailable": 1}
    assert stats.ocr_available is False


def test_no_ocr_skips_images_with_its_own_reason(fresh_index, tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    shutil.copy(DOCS / "medication-list.png", folder / "photo.png")
    stats = notes.ingest_notes(fresh_index, folder, register=_register_for(fresh_index),
                               use_ocr=False)
    assert stats.notes == 0
    assert stats.skipped == {"ocr disabled": 1}


def test_heic_without_support_is_skipped_with_its_reason(
        fresh_index, tmp_path, monkeypatch):
    from health_agent.ingest import readers

    monkeypatch.setattr(readers, "_heic_supported", lambda: False)
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "IMG_0001.heic").write_bytes(b"")
    stats = notes.ingest_notes(fresh_index, folder, register=_register_for(fresh_index))
    assert stats.skipped == {"heic unsupported": 1}


def test_find_notes_discovers_the_new_suffixes_case_insensitively(tmp_path):
    for name in ("a.md", "b.DOCX", "c.PNG", "d.jpeg", "e.heic", "f.pdf", "g.rtf"):
        (tmp_path / name).write_bytes(b"")
    found = {p.name for p in notes.find_notes(tmp_path)}
    assert found == {"a.md", "b.DOCX", "c.PNG", "d.jpeg", "e.heic"}


def test_corrupt_image_is_skipped_by_exception_name(fresh_index, tmp_path):
    """A file with an image suffix but non-image bytes lets PIL's own error
    propagate; the generic except-Exception fallthrough in ingest_notes counts
    it by exception class name rather than aborting the folder."""
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "broken.png").write_bytes(b"not an image")
    stats = notes.ingest_notes(fresh_index, folder, register=_register_for(fresh_index))
    assert stats.skipped == {"UnidentifiedImageError": 1}
    assert stats.notes == 0
