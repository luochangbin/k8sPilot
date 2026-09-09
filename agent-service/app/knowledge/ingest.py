"""Knowledge ingestion CLI (Phase 4).

Loads seed documents + incidents into the knowledge store: parse -> chunk ->
validate -> index. Documents start active (authored + reviewed seed content).

Usage:
    python -m app.knowledge.ingest --db /path/knowledge.db [--show]
"""

import argparse
import hashlib
import re

from .models import KnowledgeChunk
from .seeds import SEED_DOCUMENTS, SEED_INCIDENTS
from .store import KnowledgeStore


def chunk_document(doc) -> list[KnowledgeChunk]:
    """Naive structural chunking: split on headings/blank lines, keep section."""
    chunks: list[KnowledgeChunk] = []
    section = ""
    buf: list[str] = []
    lines = doc.content.splitlines()

    def flush():
        nonlocal buf
        text = "\n".join(buf).strip()
        if text:
            chunks.append(KnowledgeChunk(
                chunk_id=f"{doc.document_id}-{len(chunks) + 1:03d}",
                document_id=doc.document_id,
                section=section,
                content=text,
            ))
        buf = []

    for line in lines:
        if line.startswith("## "):
            flush()
            section = line[3:].strip()
        elif line.strip():
            buf.append(line.strip())
        else:
            flush()
    flush()
    if not chunks and doc.content.strip():
        chunks.append(KnowledgeChunk(chunk_id=f"{doc.document_id}-001",
                                     document_id=doc.document_id, section="",
                                     content=doc.content.strip()))
    return chunks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="knowledge sqlite path")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    store = KnowledgeStore(args.db)
    for doc in SEED_DOCUMENTS:
        doc.checksum = hashlib.sha256(doc.content.encode("utf-8")).hexdigest()[:16]
        chunks = chunk_document(doc)
        store.upsert_document(doc, chunks)
        print(f"doc  {doc.document_id}: {len(chunks)} chunks")
    for inc in SEED_INCIDENTS:
        store.upsert_incident(inc)
        print(f"inc  {inc.incident_id}: {inc.root_cause_code}")
    print(f"store ready at {args.db}")
    if args.show:
        print(f"  documents(active)={len(store.list_documents('active'))} "
              f"incidents(verified)={len(store.list_incidents('verified'))}")


if __name__ == "__main__":
    main()
