"""The corpus database is separate from the personal index, on purpose."""
from __future__ import annotations

import pytest

from health_agent.literature import schema


def test_initialize_stamps_version(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    assert schema.read_version(conn) == schema.LITERATURE_SCHEMA_VERSION
    schema.check_version(conn)  # must not raise
    conn.close()


def test_initialize_is_idempotent(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    schema.initialize(conn)
    assert schema.read_version(conn) == schema.LITERATURE_SCHEMA_VERSION
    conn.close()


def test_missing_corpus_is_an_error_not_an_empty_db(tmp_path):
    with pytest.raises(schema.CorpusNotFound):
        schema.connect(tmp_path / "nope.db")


def test_expected_tables_exist(tmp_path):
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"pack", "article", "article_chunk", "mesh_term",
            "article_chunk_fts"} <= names
    conn.close()


def test_tier_source_never_records_an_inference(tmp_path):
    """The CHECK constraint is the enforcement, not a convention."""
    import sqlite3

    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO article(pmid, title, evidence_tier, "
            "tier_source, license) VALUES('1', 't', 'rct', 'inferred', 'x')")
    conn.close()


def test_mesh_term_records_whether_a_term_is_a_major_topic(tmp_path):
    """v3. Without this column coverage() could only count every MeSH
    heading alike, and on a real corpus the most common headings are
    population check tags (Humans, Male, Female), not subjects."""
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(mesh_term)")}
    assert "major" in cols
    assert cols["major"]["notnull"] == 1
    assert cols["major"]["dflt_value"] == "0"
    conn.close()


def test_article_has_no_abstract_column(tmp_path):
    """Written by build() and read by nothing: article_chunk.text is the
    retrievable copy, and on a real corpus the duplicate was a quarter of
    the database."""
    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(article)")}
    assert "abstract" not in columns
    assert "title" in columns
    conn.close()


def test_connect_refuses_a_corpus_at_another_version(tmp_path):
    """check_version existed and was called by nobody; the ask path, status,
    and build all opened a corpus blind. Opening is where the check belongs,
    so no caller can forget it."""
    path = tmp_path / "literature.db"
    conn = schema.connect(path, create=True)
    schema.initialize(conn)
    conn.execute("UPDATE corpus_meta SET value = '1' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    with pytest.raises(schema.CorpusSchemaVersionMismatch) as excinfo:
        schema.connect(path)
    assert "--rebuild" in str(excinfo.value)

    # create=True on an existing file is still an open, not a fresh build.
    with pytest.raises(schema.CorpusSchemaVersionMismatch):
        schema.connect(path, create=True)


def test_connect_creates_a_fresh_corpus_without_a_version_check(tmp_path):
    conn = schema.connect(tmp_path / "new.db", create=True)
    assert schema.read_version(conn) is None
    schema.initialize(conn)
    assert schema.read_version(conn) == schema.LITERATURE_SCHEMA_VERSION
    conn.close()


def test_schema_version_is_4():
    assert schema.LITERATURE_SCHEMA_VERSION == 4


def test_articles_are_unique_by_pmid_and_linked_to_packs(tmp_path):
    """One paper, many packs. An 'exercise and hypertension' article belongs
    to both `exercise` and `cardiovascular`; storing it twice would return
    the same finding twice under two pack names."""
    import sqlite3

    conn = schema.connect(tmp_path / "literature.db", create=True)
    schema.initialize(conn)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(article)")}
    assert "pack_id" not in columns
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "article_pack" in names
    conn.execute("INSERT INTO article(pmid, title, evidence_tier, tier_source, "
                 "license) VALUES('1', 't', 'rct', 'publication_type', 'x')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO article(pmid, title, evidence_tier, "
                     "tier_source, license) VALUES('1', 't2', 'rct', "
                     "'publication_type', 'x')")
    conn.close()
