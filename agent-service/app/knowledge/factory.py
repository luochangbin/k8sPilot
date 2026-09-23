"""Factory for optional knowledge storage backends."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def create_knowledge_store(settings: Any):
    """Create the configured store, or return ``None`` when retrieval is off.

    PostgreSQL takes precedence over the legacy SQLite path. Initialization
    failures are intentionally sanitized: connection exceptions may include a
    DSN, so logs contain only the exception type and never its message.
    """
    database_url = getattr(settings, "knowledge_database_url", "").strip()
    if database_url:
        try:
            from .embeddings import LocalEmbeddingProvider
            from .postgres_store import PostgresKnowledgeStore

            embeddings = LocalEmbeddingProvider(
                model_name=settings.knowledge_embedding_model,
                cache_dir=settings.knowledge_embedding_cache_dir,
            )
            return PostgresKnowledgeStore(
                database_url, embeddings,
                schema=getattr(settings, "knowledge_database_schema", "public"),
            )
        except Exception as exc:
            logger.error("PostgreSQL knowledge store disabled during initialization (%s)",
                         type(exc).__name__)
            return None

    sqlite_path = getattr(settings, "knowledge_db_path", "").strip()
    if sqlite_path:
        try:
            from .store import KnowledgeStore
            return KnowledgeStore(sqlite_path)
        except Exception as exc:
            logger.error("SQLite knowledge store disabled during initialization (%s)",
                         type(exc).__name__)
    return None
