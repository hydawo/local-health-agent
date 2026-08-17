"""Logging configuration.

HARD RULE (plan §5a): logs carry metadata only — filenames, timestamps, record
counts, error types and tracebacks. Never file content, never extracted values,
never query or response text. This is not a style preference; the project's
privacy claim depends on it, so treat any log call that interpolates a health
value as a bug.

`redact_count` exists so error paths can report *how many* records failed and
*why* without echoing the offending record back into a log file.
"""

from __future__ import annotations

import logging
import sys

LOGGER_NAME = "health_agent"


def configure(verbose: bool = False) -> logging.Logger:
    """Configure the package logger to write metadata-only lines to stderr."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")


def redact_count(reason: str, count: int) -> str:
    """Format a skip/failure summary line that carries no record content."""
    return f"{count} record(s) skipped: {reason}"
