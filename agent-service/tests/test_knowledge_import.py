from __future__ import annotations

from io import BytesIO

import pytest
import yaml
pytest.importorskip("pypdf")
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from app.knowledge.importers import ImportError, import_path
from app.knowledge.ingest import chunk_document
from app.knowledge.models import KnowledgeDocument


class MemoryStore:
    def __init__(self, embeddings=None):
        self.documents = {}
        self.chunks = {}
        self.incidents = {}
        self.embeddings = embeddings

    def list_documents(self, status=None):
        return list(self.documents.values())

    def list_incidents(self, status=None):
        return list(self.incidents.values())

    def upsert_document(self, document, chunks):
        if document.document_id in self.documents:
            old = self.documents[document.document_id]
            if old.checksum == document.checksum:
                return False
        self.documents[document.document_id] = document
        self.chunks[document.document_id] = chunks
        return True

    def upsert_incident(self, incident):
        self.incidents[incident.incident_id] = incident
        return True


def _write_pdf(path, pages):
    writer = PdfWriter()
    for kind, value in pages:
        page = writer.add_blank_page(width=612, height=792)
        if kind == "text":
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
            content.set_data(b"BT /F1 12 Tf 72 720 Td (" + value.encode("ascii") + b") Tj ET")
            page[NameObject("/Contents")] = writer._add_object(content)
        elif kind == "image":
            image = DecodedStreamObject()
            image.set_data(b"\xff")
            image.update({
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(1),
                NameObject("/Height"): NumberObject(1),
                NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                NameObject("/BitsPerComponent"): NumberObject(8),
            })
            image_ref = writer._add_object(image)
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): image_ref})
            })
            content = DecodedStreamObject()
            content.set_data(b"q 10 0 0 10 0 0 cm /Im0 Do Q")
            page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as target:
        writer.write(target)


def _pdf_sidecar(path, extra=""):
    path.with_name(path.stem + ".metadata.yaml").write_text(
        "source_type: product_doc\ntitle: Test guide\n" + extra,
        encoding="utf-8",
    )


def test_markdown_frontmatter_dates_and_incremental_import(tmp_path):
    source = tmp_path / "guide.md"
    source.write_text(
        "---\nsource_type: runbook\ntitle: Guide\nvalid_from: 2026-09-23\n"
        "resource_kinds: [Pod]\n---\n## Check\nRun this command.\n",
        encoding="utf-8",
    )
    store = MemoryStore()

    first = import_path(source, store)
    second = import_path(source, store)

    assert first[0].status == "imported"
    assert second[0].status == "skipped"
    document = next(iter(store.documents.values()))
    assert document.valid_from == "2026-09-23"
    assert document.source_format == "md"
    assert document.resource_kinds == ["Pod"]


def test_incident_defaults_to_draft_and_verified_requires_root_cause_and_verification(tmp_path):
    source = tmp_path / "incident.md"
    store = MemoryStore()
    source.write_text(
        "---\nrecord_type: incident\nproduct: payments\nstatus: draft\n---\n"
        "Unreviewed incident notes.\n",
        encoding="utf-8",
    )
    imported = import_path(source, store)
    assert imported[0].status == "imported"
    assert next(iter(store.incidents.values())).status == "draft"

    source.write_text(
        "---\nrecord_type: incident\nincident_id: reviewed-1\nstatus: verified\n"
        "root_cause_code: CONFIG_ERROR\nverification: {outcome: success}\n---\n"
        "Confirmed after repair.\n",
        encoding="utf-8",
    )
    assert import_path(source, store)[0].document_id == "reviewed-1"

    source.write_text(
        "---\nrecord_type: incident\nstatus: verified\nroot_cause_code: CONFIG_ERROR\n"
        "---\nMissing verification.\n",
        encoding="utf-8",
    )
    rejected = import_path(source, store)
    assert rejected[0].status == "failed"
    assert "verification" in rejected[0].message
    assert "reviewed-1" in store.incidents


def test_real_pdf_text_pages_skip_real_blank_page_and_keep_original_page_numbers(tmp_path):
    source = tmp_path / "guide.pdf"
    _write_pdf(source, [("text", "First page"), ("blank", ""), ("text", "Third page")])
    _pdf_sidecar(source)
    store = MemoryStore()

    result = import_path(source, store)[0]

    assert result.status == "imported"
    assert "blank page 2 skipped" in result.message
    chunks = store.chunks[result.document_id]
    assert len(chunks) == 2
    assert [chunk.page_start for chunk in chunks] == [1, 3]
    assert [chunk.page_end for chunk in chunks] == [1, 3]
    assert "First page" in chunks[0].content
    assert "Third page" in chunks[1].content


def test_real_pdf_scan_image_page_is_rejected_without_replacing_existing_document(tmp_path):
    source = tmp_path / "guide.pdf"
    _write_pdf(source, [("text", "Known good content")])
    _pdf_sidecar(source)
    store = MemoryStore()
    good = import_path(source, store)[0]
    previous = store.documents[good.document_id]

    _write_pdf(source, [("text", "Changed page"), ("image", "")])
    rejected = import_path(source, store)

    assert rejected[0].status == "failed"
    assert "OCR is unsupported" in rejected[0].message
    assert store.documents[good.document_id] is previous
    assert "Known good content" in previous.content


def test_long_unbroken_chunk_is_split_without_losing_text():
    source = KnowledgeDocument(
        document_id="long", source_type="runbook", title="Long",
        content="# Parent\n### Deep\n" + "容" * 900,
    )

    chunks = chunk_document(source, max_chars=120)

    assert len(chunks) > 1
    assert all(len(chunk.content) <= 120 for chunk in chunks)
    assert "".join(chunk.content for chunk in chunks) == "容" * 900
    assert all(chunk.section == "Parent / Deep" for chunk in chunks)


def test_token_limited_chunks_use_embedding_text_and_preserve_content():
    from app.knowledge.importers import _fit_tokens
    from app.knowledge.models import KnowledgeChunk

    class TokenCounter:
        def __call__(self, texts):
            return [len(text) for text in texts]

    original = KnowledgeChunk("d-001", "d", "Page 3", "x" * 1100,
                              page_start=3, page_end=3)
    pieces = _fit_tokens(original, TokenCounter(), 480)

    assert len(pieces) == 3
    assert "".join(piece.content for piece in pieces) == original.content
    assert all(len(piece.section + "\n" + piece.content) <= 480 for piece in pieces)
    assert all(piece.page_start == 3 and piece.page_end == 3 for piece in pieces)


@pytest.mark.parametrize(("model_limit", "safe_limit"), [(256, 224), (512, 480)])
def test_import_chunks_use_embedding_provider_safe_limit(model_limit, safe_limit):
    from app.knowledge.importers import _make_chunks

    class Embeddings:
        max_tokens = model_limit
        max_document_tokens = safe_limit

        def count_tokens(self, texts):
            return [len(text) for text in texts]

    doc = KnowledgeDocument(
        document_id="limited", source_type="runbook", title="Limited",
        content="x" * 1000,
    )
    chunks = _make_chunks(doc, [], MemoryStore(Embeddings()))

    assert len(chunks) > 1
    assert "".join(chunk.content for chunk in chunks) == doc.content
    assert all(len(f"{chunk.section}\n{chunk.content}") <= safe_limit for chunk in chunks)


def test_file_import_rejects_sqlite_only_configuration_without_creating_legacy_db(
    monkeypatch, tmp_path
):
    import sys

    from app.knowledge import ingest

    sqlite_path = tmp_path / "legacy.db"
    monkeypatch.setenv("KNOWLEDGE_DATABASE_URL", "")
    monkeypatch.setenv("KNOWLEDGE_DB", str(sqlite_path))
    monkeypatch.setattr(sys, "argv", ["ingest", "--import", str(tmp_path)])

    with pytest.raises(SystemExit):
        ingest.main()

    assert not sqlite_path.exists()


def test_verified_incident_sample_checksum_is_stable(tmp_path):
    source = tmp_path / "verified.md"
    source.write_text(
        "---\nrecord_type: incident\nincident_id: verified-1\nstatus: verified\n"
        "root_cause_code: OOM\nverification: {outcome: success}\n---\n"
        "Evidence summary.\n",
        encoding="utf-8",
    )
    store = MemoryStore()
    first = import_path(source, store)[0]
    incident = store.incidents[first.document_id]
    first_checksum = incident.checksum
    # The PG store performs checksum-aware skip; this verifies importer supplies
    # the same source+metadata+parser digest on repeated imports.
    from app.knowledge.importers import _checksum
    metadata = yaml.safe_load(source.read_text().split("---\n")[1])
    assert first_checksum == _checksum(source.read_bytes(), metadata)
