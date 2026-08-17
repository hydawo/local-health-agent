"""Ingestion pipelines.

`healthkit` (milestone 1) is implemented. `records` (PDF/OCR bloodwork, milestone
2) and `notes` (markdown, milestone 3) follow.

No module in this package may make a network call, in any mode — that is a hard
invariant of the project (plan §5) and milestone 9 adds a test that enforces it.
"""
