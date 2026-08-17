"""Embedding backends.

The HTTP transport lives in `ollama_client` — the package's single network
module — so this file holds only the embedder contract and the two backends.

`HashingEmbedder` is a deterministic offline stand-in used by the test suite so
the storage and retrieval paths can be exercised without a model server. It is
**not semantically meaningful** — it can only match on exact token overlap — and
the CLI never selects it silently.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Protocol, runtime_checkable

from . import ollama_client
from .ollama_client import (DEFAULT_EMBED_MODEL, ENV_ALLOW_REMOTE,
                            ENV_EMBED_MODEL, OllamaUnavailable,
                            RemoteHostRefused)

# Re-exported so callers can keep catching one embedding-specific error type.
EmbeddingUnavailable = OllamaUnavailable

__all__ = [
    "Embedder", "OllamaEmbedder", "HashingEmbedder", "get_embedder",
    "EmbeddingUnavailable", "RemoteHostRefused", "ENV_ALLOW_REMOTE",
    "ENV_EMBED_MODEL", "DEFAULT_EMBED_MODEL",
]


@runtime_checkable
class Embedder(Protocol):
    """Contract every backend satisfies.

    `name` is stored on each embedded chunk so an index built with one model is
    never silently mixed with another's vectors — different models produce
    incomparable vector spaces, and cosine distance between them is noise.
    """

    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class OllamaEmbedder:
    """Embeddings from a local Ollama server."""

    def __init__(self, model: str | None = None, host: str | None = None,
                 timeout: float = 120.0) -> None:
        self.model = model or os.environ.get(ENV_EMBED_MODEL) or DEFAULT_EMBED_MODEL
        self.host = ollama_client.resolve_host(host)
        self.timeout = timeout
        self.name = f"ollama:{self.model}"
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed(["dimension probe"])[0])
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return ollama_client.embed(self.host, self.model, texts, self.timeout)

    def health_check(self) -> str:
        """Verify the server is up and the model is present. Returns a summary."""
        vectors = ollama_client.embed(self.host, self.model, ["ping"], self.timeout)
        return (f"Ollama at {self.host}, model {self.model}, "
                f"{len(vectors[0])} dimensions")


class HashingEmbedder:
    """Deterministic bag-of-words hashing embedder. NOT semantic.

    Exists so the chunk -> embed -> store -> search path is testable on a machine
    with no model server. It matches on shared tokens only: "elevated glucose"
    and "high blood sugar" score zero against each other, where a real embedding
    model would score them close. Never selected implicitly — a caller has to ask
    for it by name.
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        self.name = f"hashing:{dim}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector


def get_embedder(backend: str = "ollama", *, model: str | None = None,
                 host: str | None = None) -> Embedder:
    """Construct an embedder by backend name ('ollama' or 'hashing')."""
    if backend == "ollama":
        return OllamaEmbedder(model=model, host=host)
    if backend == "hashing":
        return HashingEmbedder()
    raise ValueError(f"unknown embedding backend: {backend!r}")
