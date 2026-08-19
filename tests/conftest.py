"""Shared test fixtures.

Every test in this suite runs against `tests/fixtures/` and a throwaway index in
a tmp_path. Nothing here reads a real health export (plan §6).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from health_agent.ingest import healthkit
from health_agent.store import sqlite_schema

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_export() -> Path:
    return FIXTURES / "export.xml"


@pytest.fixture
def malformed_export() -> Path:
    return FIXTURES / "export_malformed.xml"


@pytest.fixture
def literature_fixture() -> Path:
    return FIXTURES / "literature" / "corpus.xml"


@pytest.fixture
def index_path(tmp_path: Path) -> Path:
    return tmp_path / ".index" / "health.db"


@pytest.fixture
def ingested(index_path: Path, fixture_export: Path):
    """An index with the main fixture ingested and aggregates built."""
    conn = sqlite_schema.connect(index_path, create=True)
    sqlite_schema.initialize(conn)
    stats = healthkit.ingest_file(conn, fixture_export)
    sqlite_schema.rebuild_daily_metrics(conn)
    yield conn, stats
    conn.close()
