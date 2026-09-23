"""Opt-in PostgreSQL + local embedding import/retrieval integration test.

Run with K8SPILOT_TEST_SCHEMA set to an existing, non-public schema and the
normal KNOWLEDGE_DATABASE_URL configured in agent-service/.env or the process.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from app.config import Config
from app.knowledge.embeddings import LocalEmbeddingProvider
from app.knowledge.importers import import_path
from app.knowledge.postgres_store import PostgresKnowledgeStore
from app.knowledge.service import KnowledgeService


def _write_text_pdf(path: Path) -> None:
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()

    def add_text_page(text: str) -> None:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        font_ref = writer._add_object(font)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})
        })
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 12 Tf 72 720 Td (" + text.encode("ascii") + b") Tj ET")
        page[NameObject("/Contents")] = writer._add_object(content)

    add_text_page("E2E pdf page one k8spdfprobe")
    writer.add_blank_page(width=612, height=792)
    add_text_page("E2E pdf page three k8spdfprobe")
    with path.open("wb") as target:
        writer.write(target)


def test_postgres_import_update_retrieval_and_exact_cleanup(tmp_path: Path) -> None:
    schema = os.environ.get("K8SPILOT_TEST_SCHEMA", "").strip()
    if not schema:
        pytest.skip("set K8SPILOT_TEST_SCHEMA to opt into PostgreSQL integration")

    settings = Config()
    assert settings.knowledge_database_url, (
        "K8SPILOT_TEST_SCHEMA is set but KNOWLEDGE_DATABASE_URL is not configured"
    )
    assert schema != "public", "integration test must not use the public schema"
    psycopg = pytest.importorskip("psycopg")
    try:
        with psycopg.connect(settings.knowledge_database_url, connect_timeout=5) as conn:
            namespace = conn.execute(
                "SELECT to_regnamespace(%s)", (schema,)
            ).fetchone()[0]
    except Exception as exc:
        pytest.fail(f"test schema existence check failed ({type(exc).__name__})")
    assert namespace is not None, "K8SPILOT_TEST_SCHEMA must name an existing schema"

    embeddings = LocalEmbeddingProvider(
        settings.knowledge_embedding_model,
        settings.knowledge_embedding_cache_dir,
    )
    store = PostgresKnowledgeStore(
        settings.knowledge_database_url,
        embeddings,
        schema=schema,
    )
    service = KnowledgeService(store)

    document_id, pdf_id, draft_id, verified_id = (
        f"e2e-{uuid.uuid4().hex}" for _ in range(4)
    )
    with store._connect() as conn:
        exists = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM knowledge_documents WHERE document_id = ANY(%s)) "
            "OR EXISTS(SELECT 1 FROM knowledge_incidents WHERE incident_id = ANY(%s))",
            ([document_id, pdf_id], [draft_id, verified_id]),
        ).fetchone()[0]
    assert not exists, "generated integration IDs unexpectedly already exist"
    baseline = store.snapshot_hash()

    try:
        markdown = tmp_path / "guide.md"
        markdown.write_text(
            f"""---
document_id: {document_id}
source_type: runbook
title: E2E Probe Runbook
source_uri: https://example.invalid/k8spilot/e2e
resource_kinds: [Pod]
---
# E2E Probe

The k8se2eprobe marker explains **first revision**.
""",
            encoding="utf-8",
        )
        assert import_path(markdown, store)[0].status == "imported"
        assert import_path(markdown, store)[0].status == "skipped"

        markdown.write_text(
            f"""---
document_id: {document_id}
source_type: runbook
title: E2E Probe Runbook
source_uri: https://example.invalid/k8spilot/e2e
resource_kinds: [Pod]
---
# E2E Probe

The k8se2eprobe marker explains **updated revision**.
""",
            encoding="utf-8",
        )
        assert import_path(markdown, store)[0].status == "imported"
        with store._connect() as conn:
            chunks = conn.execute(
                "SELECT content FROM knowledge_chunks WHERE document_id=%s", (document_id,)
            ).fetchall()
        assert chunks and all("first revision" not in row[0] for row in chunks)
        assert any("updated revision" in row[0] for row in chunks)
        assert any(
            hit.citation["document_id"] == document_id
            for hit in service.search_knowledge("k8se2eprobe updated revision")
        )

        pdf = tmp_path / "guide.pdf"
        _write_text_pdf(pdf)
        pdf.with_name("guide.metadata.yaml").write_text(
            f"document_id: {pdf_id}\nsource_type: product_doc\ntitle: E2E PDF Guide\n"
            "source_uri: https://example.invalid/k8spilot/guide.pdf\n",
            encoding="utf-8",
        )
        pdf_result = import_path(pdf, store)[0]
        assert pdf_result.status == "imported"
        assert "blank page 2 skipped" in pdf_result.message
        pdf_hit = next(
            hit for hit in service.search_knowledge("k8spdfprobe page three")
            if hit.citation["document_id"] == pdf_id
        )
        assert (pdf_hit.citation["page_start"], pdf_hit.citation["page_end"]) == (3, 3)
        with store._connect() as conn:
            page_ranges = conn.execute(
                "SELECT page_start,page_end FROM knowledge_chunks WHERE document_id=%s",
                (pdf_id,),
            ).fetchall()
        assert (1, 1) in page_ranges and (3, 3) in page_ranges

        draft = tmp_path / "incident-draft.md"
        draft.write_text(
            f"""---
record_type: incident
incident_id: {draft_id}
status: draft
symptoms: [k8sincidentprobe]
evidence_summary: k8sincidentprobe draft unverified marker
---
Draft only.
""",
            encoding="utf-8",
        )
        verified = tmp_path / "incident-verified.md"
        verified.write_text(
            f"""---
record_type: incident
incident_id: {verified_id}
status: verified
symptoms: [k8sincidentprobe]
root_cause_code: E2E_VALIDATED
evidence_summary: k8sincidentprobe verified marker
verification:
  outcome: repaired
---
Validated incident.
""",
            encoding="utf-8",
        )
        assert import_path(draft, store)[0].status == "imported"
        assert import_path(verified, store)[0].status == "imported"
        incident_hits = service.search_incidents("k8sincidentprobe")
        assert all(hit.incident_id != draft_id for hit in incident_hits)
        assert any(hit.incident_id == verified_id for hit in incident_hits)
    finally:
        # These UUIDs were verified absent before the test; only these records
        # are deleted. Never create or drop a schema in this test.
        store.delete_document(document_id)
        store.delete_document(pdf_id)
        store.delete_incident(draft_id)
        store.delete_incident(verified_id)

    assert store.snapshot_hash() == baseline
