"""PostgreSQL knowledge-store invariants; requires an explicitly isolated test DSN."""

from __future__ import annotations

import importlib
import os
import uuid

import pytest

from app.knowledge.models import IncidentCase, KnowledgeChunk, KnowledgeDocument
from app.knowledge.service import KnowledgeService


class FakeEmbeddings:
    model_id = "test-local-embedding-v1"
    dimensions = 3

    def __init__(self):
        self.fail_documents = False
        self.document_calls = 0

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls += 1
        if self.fail_documents:
            raise RuntimeError("embedding failed")
        return [[1.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


def _store_type():
    try:
        return importlib.import_module("app.knowledge.postgres_store").PostgresKnowledgeStore
    except (ImportError, AttributeError) as exc:
        pytest.fail(f"PostgresKnowledgeStore is not implemented: {exc}")


def test_lexical_terms_segment_chinese_and_strip_tsquery_operators():
    mod = importlib.import_module("app.knowledge.postgres_store")
    terms = mod._lex_terms("配置缺失 k8s.io:443; foo | bar")
    assert "配置" in terms and "缺失" in terms
    assert {"k8s", "io", "443"}.issubset(terms)
    assert not {"|", ":", ";"}.intersection(terms)


def test_document_filter_sql_and_values_keep_composite_filter_order():
    mod = importlib.import_module("app.knowledge.postgres_store")
    where, values = mod._document_filter_parts({
        "product": "kubernetes", "environment": "production", "resource_kind": "Pod",
        "versions": ["1.30"], "source_types": ["runbook"],
    }, {"team:sre"})
    assert where.index("d.product=%s") < where.index("d.environments ? %s")
    assert where.index("d.environments ? %s") < where.index("d.resource_kinds ? %s")
    assert where.index("d.versions ?| %s") < where.index("d.source_type = ANY(%s)")
    assert values == [["team:sre"], "kubernetes", "production", "Pod", ["1.30"], ["runbook"]]


def test_knowledge_service_preserves_page_and_updated_at_with_hard_character_caps():
    doc, _ = _doc("doc-service", content="x" * 900)

    class OneHitStore:
        def search_chunks(self, query, top_k, filters=None):
            return [{"chunk_id": "c1", "section": "操作", "chunk_content": "x" * 900,
                     "page_start": 2, "page_end": 3, "rank": -0.5, "document": doc}]

        def search_incidents(self, query, top_k, filters=None):
            return []

    references = KnowledgeService(OneHitStore()).search_knowledge("query")
    assert len(references[0].content) == 600
    assert references[0].citation["page_start"] == 2
    assert references[0].citation["page_end"] == 3
    assert references[0].citation["updated_at"] == "2026-01-02T03:04:05+00:00"


def test_unsafe_schema_identifier_is_rejected_before_connecting():
    store_type = _store_type()
    with pytest.raises(ValueError, match="schema"):
        store_type("postgresql://invalid", FakeEmbeddings(), schema='kb"; DROP SCHEMA public CASCADE;--')


def test_existing_schema_does_not_require_database_create_privilege():
    store_type = _store_type()
    store = object.__new__(store_type)
    store.schema = "k8spilot_knowledge"

    class ExistingSchemaConnection:
        calls = 0

        def execute(self, query, params):
            self.calls += 1
            assert query == "SELECT to_regnamespace(%s)"
            assert params == ("k8spilot_knowledge",)
            return self

        def fetchone(self):
            return ("k8spilot_knowledge",)

    conn = ExistingSchemaConnection()
    store._ensure_schema(conn)
    assert conn.calls == 1


@pytest.fixture
def pg_store():
    dsn = os.environ.get("K8SPILOT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set K8SPILOT_TEST_DATABASE_URL to a dedicated PostgreSQL test database")

    schema = f"k8spilot_test_{uuid.uuid4().hex}"
    store_type = _store_type()
    embeddings = FakeEmbeddings()
    try:
        store = store_type(dsn, embeddings, schema=schema)
    except RuntimeError as exc:
        if "database role cannot create it" in str(exc):
            pytest.skip("dedicated test schema requires database CREATE permission; use an isolated test DSN with schema-creation rights")
        raise
    yield store, embeddings, schema

    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _doc(document_id: str, *, content: str, checksum: str = "sha256-v1",
         acl_tags: list[str] | None = None) -> tuple[KnowledgeDocument, list[KnowledgeChunk]]:
    doc = KnowledgeDocument(
        document_id=document_id,
        source_type="runbook",
        title="Pod CrashLoop runbook",
        source_uri="file:///ops/crashloop.md",
        product="kubernetes",
        versions=["1.30"],
        environments=["production"],
        resource_kinds=["Pod"],
        valid_from="2025-01-01T00:00:00+00:00",
        checksum=checksum,
        acl_tags=acl_tags or [],
        content=content,
        updated_at="2026-01-02T03:04:05+00:00",
        source_format="md",
    )
    chunks = [KnowledgeChunk(
        chunk_id=f"{document_id}-001",
        document_id=document_id,
        section="排查",
        content=content,
        page_start=3,
        page_end=4,
    )]
    return doc, chunks


def test_document_import_is_idempotent_for_same_checksum_and_embedding_model(pg_store):
    store, embeddings, _ = pg_store
    doc, chunks = _doc("doc-idempotent", content="CrashLoopBackOff restart container")

    assert store.upsert_document(doc, chunks) is True
    calls_after_insert = embeddings.document_calls
    assert store.document_checksum(doc.document_id) == "sha256-v1"
    assert store.upsert_document(doc, chunks) is False
    assert embeddings.document_calls == calls_after_insert
    assert store.list_documents(status="active")[0].document_id == doc.document_id


def test_document_search_returns_page_updated_at_and_applies_metadata_filters(pg_store):
    store, _, _ = pg_store
    doc, chunks = _doc("doc-citation", content="CrashLoopBackOff inspect previous logs")
    store.upsert_document(doc, chunks)

    hits = store.search_chunks("CrashLoopBackOff", 5, {
        "product": "kubernetes", "versions": ["1.30"],
        "environment": "production", "resource_kind": "Pod",
    })
    assert len(hits) == 1
    assert hits[0]["page_start"] == 3
    assert hits[0]["page_end"] == 4
    assert hits[0]["document"].updated_at == "2026-01-02T03:04:05+00:00"
    assert len(store.search_chunks("CrashLoopBackOff", 5, {
        "product": "kubernetes", "versions": ["1.30"], "environment": "production",
        "resource_kind": "Pod", "source_types": ["runbook"],
    })) == 1
    assert store.search_chunks("CrashLoopBackOff", 5, {
        "product": "kubernetes", "versions": ["1.30"], "environment": "production",
        "resource_kind": "Pod", "source_types": ["sop"],
    }) == []
    assert store.search_chunks("CrashLoopBackOff", 5, {"product": "other"}) == []


def test_chinese_lexical_index_matches_unsegmented_query(pg_store):
    store, _, _ = pg_store
    irrelevant, irrelevant_chunks = _doc("a-irrelevant", content="普通容器信息")
    relevant, relevant_chunks = _doc("z-relevant", content="配置缺失导致应用启动失败")
    store.upsert_document(irrelevant, irrelevant_chunks)
    store.upsert_document(relevant, relevant_chunks)

    hits = store.search_chunks("配置缺失", 5)
    assert hits[0]["document"].document_id == "z-relevant"


def test_nonempty_acl_is_hidden_without_server_trusted_acl_tags(pg_store):
    store, _, schema = pg_store
    doc, chunks = _doc("doc-private", content="CrashLoopBackOff restricted procedure",
                       acl_tags=["team:sre"])
    store.upsert_document(doc, chunks)

    assert store.search_chunks("restricted procedure", 5) == []
    trusted_store = _store_type()(os.environ["K8SPILOT_TEST_DATABASE_URL"], FakeEmbeddings(),
                                  schema=schema, allowed_acl_tags=["team:sre"])
    assert len(trusted_store.search_chunks("restricted procedure", 5)) == 1
    # A caller-supplied filter cannot grant itself access.
    assert store.search_chunks("restricted procedure", 5,
                               {"allowed_acl_tags": ["team:sre"]}) == []


def test_embedding_failure_keeps_previous_document_and_chunks(pg_store):
    store, embeddings, _ = pg_store
    old_doc, old_chunks = _doc("doc-atomic", content="Old recovery instructions",
                               checksum="sha256-old")
    store.upsert_document(old_doc, old_chunks)
    new_doc, new_chunks = _doc("doc-atomic", content="New recovery instructions",
                               checksum="sha256-new")
    embeddings.fail_documents = True

    with pytest.raises(RuntimeError, match="embedding failed"):
        store.upsert_document(new_doc, new_chunks)
    assert store.document_checksum("doc-atomic") == "sha256-old"
    old_hits = store.search_chunks("Old recovery", 5)
    assert [hit["chunk_content"] for hit in old_hits] == ["Old recovery instructions"]
    assert all("New recovery" not in hit["chunk_content"] for hit in old_hits)


def test_incident_search_exposes_only_verified_cases(pg_store):
    store, _, _ = pg_store
    store.upsert_incident(IncidentCase(
        incident_id="incident-open", status="open", product="kubernetes",
        resource_kind="Pod", symptoms=["CrashLoopBackOff"],
        root_cause_code="CONFIG_ERROR"))
    store.upsert_incident(IncidentCase(
        incident_id="incident-verified", status="verified", product="kubernetes",
        product_version="1.30", resource_kind="Pod", symptoms=["CrashLoopBackOff"],
        root_cause_code="CONFIG_ERROR", evidence_summary="current version ConfigMap absent",
        remediation_summary="restore referenced ConfigMap", checksum="incident-sha-v1",
        verification={"outcome": "success"}))
    assert store.upsert_incident(IncidentCase(
        incident_id="incident-verified", status="verified", product="kubernetes",
        product_version="1.30", resource_kind="Pod", symptoms=["CrashLoopBackOff"],
        root_cause_code="CONFIG_ERROR", evidence_summary="current version ConfigMap absent",
        remediation_summary="restore referenced ConfigMap", checksum="incident-sha-v1",
        verification={"outcome": "success"})) is False
    assert store.list_incidents(status="verified")[0].checksum == "incident-sha-v1"

    cases = store.search_incidents("CrashLoopBackOff ConfigMap", 5,
                                   {"resource_kind": "Pod", "product_version": "1.30"})
    assert [case["incident_id"] for case in cases] == ["incident-verified"]
    assert store.delete_incident("incident-verified") is True
    assert store.search_incidents("CrashLoopBackOff", 5) == []


def test_snapshot_hash_changes_when_document_changes(pg_store):
    store, _, _ = pg_store
    public_doc, public_chunks = _doc("doc-public", content="public runbook")
    private_doc, private_chunks = _doc("doc-hidden", content="private runbook",
                                       acl_tags=["team:secret"])
    store.upsert_document(public_doc, public_chunks)
    store.upsert_document(private_doc, private_chunks)

    initial = store.snapshot_hash()
    changed_doc, changed_chunks = _doc("doc-hidden", content="different private runbook",
                                       checksum="sha256-private-changed",
                                       acl_tags=["team:secret"])
    store.upsert_document(changed_doc, changed_chunks)
    assert store.snapshot_hash() != initial
    assert store.delete_document("doc-hidden") is True
    assert store.document_checksum("doc-hidden") is None
