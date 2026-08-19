"""Embed corpus chunks into the corpus vector table.

A thin wrapper over the shared `embed_pending`, which does the resumable batch
work and the embedder-name stamping. It exists so callers never have to
remember which table and parent column the corpus uses.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from ..store import vector_store

if TYPE_CHECKING:
    from ..embeddings import Embedder

TABLE_NAME = "literature_chunks"


def embed_corpus(conn: sqlite3.Connection, store: vector_store.VectorStore,
                 embedder: "Embedder", *, progress=None) -> int:
    return vector_store.embed_pending(
        conn, store, embedder, table="article_chunk",
        parent_column="article_id", progress=progress)


def pending_count(conn: sqlite3.Connection, embedder_name: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM article_chunk WHERE embedded_with IS NOT ?",
        (embedder_name,),
    ).fetchone()["n"]
