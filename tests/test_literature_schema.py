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
    conn.execute("INSERT INTO pack(slug, version, built_at, article_count) "
                 "VALUES('t', '1', '2026-01-01', 0)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO article(pack_id, pmid, title, evidence_tier, "
            "tier_source, license) VALUES(1, '1', 't', 'rct', 'inferred', 'x')")
    conn.close()
