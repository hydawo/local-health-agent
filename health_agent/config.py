"""Paths and runtime configuration.

Everything is derived from a single user-controlled data directory. Nothing is
written outside it, and no path here is ever transmitted anywhere (plan §5).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = Path("~/HealthData")

ENV_DATA_DIR = "HEALTH_AGENT_DATA_DIR"
ENV_INDEX = "HEALTH_AGENT_INDEX"


@dataclass(frozen=True)
class Config:
    """Resolved locations for one invocation."""

    data_dir: Path
    index_path: Path

    @property
    def healthkit_dir(self) -> Path:
        return self.data_dir / "healthkit"

    @property
    def default_export_path(self) -> Path:
        return self.healthkit_dir / "export.xml"

    @property
    def records_dir(self) -> Path:
        return self.data_dir / "records"

    @property
    def notes_dir(self) -> Path:
        return self.data_dir / "notes"

    @property
    def index_dir(self) -> Path:
        return self.index_path.parent

    @property
    def vector_path(self) -> Path:
        """LanceDB directory, kept beside the SQLite index so `reset` and
        backups treat the two halves of the index as one thing."""
        return self.index_dir / "vectors"

    @property
    def literature_path(self) -> Path:
        """The corpus index. Beside the personal index, never joined to it."""
        return self.index_dir / "literature.db"

    @property
    def literature_vector_path(self) -> Path:
        return self.index_dir / "vectors"


def resolve(data_dir: str | os.PathLike[str] | None = None,
            index_path: str | os.PathLike[str] | None = None) -> Config:
    """Resolve config from explicit args, then environment, then defaults."""
    raw_data = data_dir or os.environ.get(ENV_DATA_DIR) or DEFAULT_DATA_DIR
    resolved_data = Path(raw_data).expanduser().resolve()

    raw_index = index_path or os.environ.get(ENV_INDEX)
    resolved_index = (
        Path(raw_index).expanduser().resolve()
        if raw_index
        else resolved_data / ".index" / "health.db"
    )
    return Config(data_dir=resolved_data, index_path=resolved_index)
