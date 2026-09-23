"""Focused tests for the local embedding adapter and backend factory."""

import math
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.knowledge.embeddings import LocalEmbeddingProvider
from app.knowledge.factory import create_knowledge_store
from app.knowledge.store import KnowledgeStore
from app.knowledge.models import KnowledgeChunk, KnowledgeDocument
from app.knowledge.service import KnowledgeService
from app.config import Config


class FakeFastEmbedModel:
    model_name = "minishlab/potion-multilingual-128M"
    embedding_size = 3

    def query_embed(self, query):
        assert query == "中文查询"
        return [[0.1, 0.2, 0.3]]

    def token_count(self, text):
        return len(text.split())

    def passage_embed(self, texts):
        assert texts == ["文档一", "document two"]
        return iter([[0.3, 0.2, 0.1], [0.4, 0.5, 0.6]])


def provider_for(model):
    provider = LocalEmbeddingProvider.__new__(LocalEmbeddingProvider)
    provider._model = model
    provider.model_id = model.model_name
    provider.dimensions = model.embedding_size
    provider.max_tokens = 512
    provider.max_document_tokens = 480
    import threading
    provider._lock = threading.Lock()
    return provider


def test_local_embedding_adapter_uses_query_and_passage_methods():
    provider = provider_for(FakeFastEmbedModel())
    assert provider.model_id == "minishlab/potion-multilingual-128M"
    assert provider.dimensions == 3
    assert provider.embed_query("中文查询") == [0.1, 0.2, 0.3]
    assert provider.embed_documents(["文档一", "document two"]) == [
        [0.3, 0.2, 0.1], [0.4, 0.5, 0.6]
    ]


@pytest.mark.parametrize("bad_vector", [[0.1, 0.2], [0.1, math.nan, 0.3]])
def test_local_embedding_adapter_rejects_bad_vectors(bad_vector):
    class BadModel(FakeFastEmbedModel):
        def query_embed(self, query):
            return [bad_vector]

    with pytest.raises(ValueError):
        provider_for(BadModel()).embed_query("中文查询")


def test_local_embedding_adapter_rejects_wrong_vector_count():
    class BadModel(FakeFastEmbedModel):
        def passage_embed(self, texts):
            return iter([[0.1, 0.2, 0.3]])

    with pytest.raises(ValueError, match="count mismatch"):
        provider_for(BadModel()).embed_documents(["文档一", "document two"])


def test_local_embedding_adapter_rejects_overlong_documents_before_truncation():
    provider = provider_for(FakeFastEmbedModel())
    provider.max_document_tokens = 2
    with pytest.raises(ValueError, match="safe 2-token limit"):
        provider.embed_documents(["three token document"])


def test_factory_keeps_knowledge_disabled_without_backend():
    settings = SimpleNamespace(knowledge_database_url="", knowledge_db_path="",
                               knowledge_embedding_model="model",
                               knowledge_embedding_cache_dir="")
    assert create_knowledge_store(settings) is None


def test_factory_preserves_legacy_sqlite_backend(tmp_path: Path):
    settings = SimpleNamespace(knowledge_database_url="",
                               knowledge_db_path=str(tmp_path / "knowledge.db"),
                               knowledge_embedding_model="model",
                               knowledge_embedding_cache_dir="")
    store = create_knowledge_store(settings)
    assert isinstance(store, KnowledgeStore)


def test_embedding_cache_path_is_repo_relative_not_cwd_relative(monkeypatch, tmp_path):
    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KNOWLEDGE_EMBEDDING_CACHE_DIR", "PostgreSQL/models")
    config = Config()
    assert Path(config.knowledge_embedding_cache_dir) == repo_root / "PostgreSQL" / "models"


def test_postgres_factory_to_service_retrieval_uses_isolated_schema():
    settings = Config()
    if not settings.knowledge_database_url:
        pytest.skip("KNOWLEDGE_DATABASE_URL is not configured")

    schema = os.getenv("K8SPILOT_TEST_SCHEMA")
    if not schema:
        pytest.skip("K8SPILOT_TEST_SCHEMA must name a pre-provisioned isolated schema")

    test_settings = SimpleNamespace(
        knowledge_database_url=settings.knowledge_database_url,
        knowledge_database_schema=schema,
        knowledge_embedding_model=settings.knowledge_embedding_model,
        knowledge_embedding_cache_dir=settings.knowledge_embedding_cache_dir,
        knowledge_db_path="",
    )
    document_id = f"test-doc-{uuid.uuid4().hex}"
    store = None
    try:
        store = create_knowledge_store(test_settings)
        assert store is not None
        doc = KnowledgeDocument(
            document_id=document_id, source_type="runbook",
            title="Isolated retrieval test", product="kubernetes", status="active",
            content="oommarker database pressure remediation sentinel.",
        )
        chunk = KnowledgeChunk(
            chunk_id=f"{document_id}-001", document_id=document_id,
            section="Memory", content=doc.content,
        )
        assert store.upsert_document(doc, [chunk]) is True
        refs = KnowledgeService(store).search_knowledge("oommarker database", top_k=3)
        assert any(ref.citation["document_id"] == document_id for ref in refs)
    finally:
        if store is not None:
            store.delete_document(document_id)
