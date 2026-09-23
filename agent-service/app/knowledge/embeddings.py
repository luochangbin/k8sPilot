"""Local, deterministic embedding provider for the optional knowledge store."""

from __future__ import annotations

import math
import re
import threading
from pathlib import Path
from typing import Iterable, Sequence

DEFAULT_MODEL = "minishlab/potion-multilingual-128M"


class LocalEmbeddingProvider:
    """FastEmbed-backed local embeddings; document content never leaves process."""

    def __init__(self, model_name: str = DEFAULT_MODEL,
                 cache_dir: str | Path | None = None) -> None:
        from fastembed import TextEmbedding

        # Pin CPU explicitly so behavior does not depend on optional CUDA setup.
        kwargs = {"model_name": model_name, "cuda": False}
        if cache_dir:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            kwargs["cache_dir"] = str(cache_dir)
        self._model = TextEmbedding(**kwargs)
        self.model_id = self._model.model_name
        self.dimensions = int(self._model.embedding_size)
        description = next(
            (item["description"] for item in TextEmbedding.list_supported_models()
             if item["model"].casefold() == self.model_id.casefold()),
            "",
        )
        token_limit = re.search(r"(\d+) input tokens truncation", description)
        if token_limit is None:
            raise ValueError("configured embedding model has no declared token limit")
        self.max_tokens = int(token_limit.group(1))
        # Leave a margin below the model cap for tokenizer/special-token behavior.
        self.max_document_tokens = max(1, min(480, self.max_tokens - 32))
        self._lock = threading.Lock()

    def count_tokens(self, texts: list[str]) -> list[int]:
        """Return FastEmbed-tokenizer counts, including model special tokens."""
        if not texts:
            return []
        with self._lock:
            return [int(self._model.token_count(text)) for text in texts]

    def token_count(self, text: str) -> int:
        """Convenience single-text form used by ingestion chunking."""
        return self.count_tokens([text])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        # FastEmbed's passage_embed applies the model's document-side semantics.
        if any(count > self.max_document_tokens for count in self.count_tokens(texts)):
            raise ValueError(
                f"document embedding input exceeds safe {self.max_document_tokens}-token limit")
        with self._lock:
            return self._collect(self._model.passage_embed(texts), len(texts))

    def embed_query(self, text: str) -> list[float]:
        # FastEmbed's query_embed applies the model's query-side semantics.
        if self.token_count(text) > self.max_document_tokens:
            raise ValueError(
                f"query embedding input exceeds safe {self.max_document_tokens}-token limit")
        with self._lock:
            rows = self._collect(self._model.query_embed(text), 1)
        return rows[0]

    def _collect(self, values: Iterable[Sequence[float]], expected_count: int
                 ) -> list[list[float]]:
        rows: list[list[float]] = []
        for value in values:
            row = [float(item) for item in value]
            if len(row) != self.dimensions:
                raise ValueError(
                    f"embedding dimension mismatch: expected {self.dimensions}, got {len(row)}")
            if not all(math.isfinite(item) for item in row):
                raise ValueError("embedding contains non-finite values")
            rows.append(row)
        if len(rows) != expected_count:
            raise ValueError(
                f"embedding count mismatch: expected {expected_count}, got {len(rows)}")
        return rows
