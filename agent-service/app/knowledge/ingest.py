"""Knowledge ingestion CLI.

Legacy SQLite seeds remain available through --db; file imports use --import.
"""

import argparse
import hashlib
from pathlib import Path
from typing import Any

from .models import KnowledgeChunk
from .seeds import SEED_DOCUMENTS, SEED_INCIDENTS
from .store import KnowledgeStore


def chunk_document(doc, max_chars: int = 4000) -> list[KnowledgeChunk]:
    """Chunk by Markdown heading and paragraph, preserving fenced code blocks."""
    chunks: list[KnowledgeChunk] = []
    sections: list[tuple[int, str]] = []
    blocks: list[tuple[str, str]] = []
    paragraph: list[str] = []
    code: list[str] = []
    fence_marker = ""

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append((" / ".join(title for _, title in sections),
                           "\n".join(paragraph).strip()))
            paragraph.clear()

    for line in doc.content.splitlines():
        stripped = line.strip()
        marker = stripped[:3]
        if marker in (chr(96) * 3, "~~~"):
            if not fence_marker:
                flush_paragraph()
                fence_marker = marker
                code.append(line)
            elif marker == fence_marker:
                code.append(line)
                blocks.append((" / ".join(title for _, title in sections), "\n".join(code)))
                code.clear()
                fence_marker = ""
            else:
                code.append(line)
            continue
        if fence_marker:
            code.append(line)
            continue
        level = len(line) - len(line.lstrip("#"))
        if 1 <= level <= 6 and line[level:level + 1] == " ":
            flush_paragraph()
            while sections and sections[-1][0] >= level:
                sections.pop()
            sections.append((level, line[level:].strip()))
        elif stripped:
            paragraph.append(line)
        else:
            flush_paragraph()
    if code:
        blocks.append((" / ".join(title for _, title in sections), "\n".join(code)))
    flush_paragraph()

    pending: list[str] = []
    pending_section = ""

    def emit() -> None:
        text = "\n\n".join(pending).strip()
        if text:
            chunks.append(KnowledgeChunk(
                chunk_id=f"{doc.document_id}-{len(chunks) + 1:03d}",
                document_id=doc.document_id,
                section=pending_section,
                content=text,
            ))
        pending.clear()

    for section, block in blocks:
        for part in _split_bounded(block, max_chars):
            combined_length = len("\n\n".join([*pending, part])) if pending else len(part)
            if pending and (section != pending_section or combined_length > max_chars):
                emit()
            if not pending:
                pending_section = section
            pending.append(part)
    emit()
    return chunks


def _split_bounded(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    result: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines() or [text]:
        pieces = [line[i:i + max_chars] for i in range(0, len(line), max_chars)] or [""]
        for piece in pieces:
            if current and size + len(piece) + 1 > max_chars:
                result.append("\n".join(current))
                current, size = [], 0
            current.append(piece)
            size += len(piece) + 1
    if current:
        result.append("\n".join(current))
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    actions = ap.add_mutually_exclusive_group()
    actions.add_argument("--import", dest="import_path", type=Path,
                         help="import one .md/.pdf file or recursively scan a directory")
    actions.add_argument("--list", action="store_true", help="list indexed documents and incidents")
    actions.add_argument("--delete", metavar="ID", help="delete exactly one document or incident")
    ap.add_argument("--db", help="legacy SQLite seed path (seed operation only)")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    if args.db and not (args.import_path or args.list or args.delete):
        _seed(args.db, args.show)
        return
    if args.db:
        ap.error("--db is only valid for the legacy SQLite seed operation")
    if not (args.import_path or args.list or args.delete):
        ap.error("specify --import, --list, --delete, or legacy --db")
    store = _configured_store(require_postgres=args.import_path is not None)
    if args.import_path:
        from .importers import import_path
        results = import_path(args.import_path, store)
        if not results:
            print("no .md/.pdf files found")
        failed = False
        for result in results:
            print(f"{result.status}\t{result.document_id}\t{result.message}")
            failed = failed or result.status == "failed"
        if failed:
            raise SystemExit(1)
    elif args.list:
        for doc in store.list_documents():
            print(f"document\t{doc.document_id}\t{doc.status}\t{doc.source_type}\t{doc.title}")
        for incident in store.list_incidents():
            print(f"incident\t{incident.incident_id}\t{incident.status}\t"
                  f"{incident.root_cause_code}")
    elif args.delete:
        if not _delete_record(store, args.delete):
            ap.error(f"record not found: {args.delete}")
        print(f"deleted\t{args.delete}")


def _seed(db_path: str, show: bool) -> None:
    store = KnowledgeStore(db_path)
    for doc in SEED_DOCUMENTS:
        doc.checksum = hashlib.sha256(doc.content.encode("utf-8")).hexdigest()[:16]
        chunks = chunk_document(doc)
        store.upsert_document(doc, chunks)
        print(f"doc  {doc.document_id}: {len(chunks)} chunks")
    for inc in SEED_INCIDENTS:
        store.upsert_incident(inc)
        print(f"inc  {inc.incident_id}: {inc.root_cause_code}")
    print(f"store ready at {db_path}")
    if show:
        print(f"  documents(active)={len(store.list_documents('active'))} "
              f"incidents(verified)={len(store.list_incidents('verified'))}")


def _configured_store(*, require_postgres: bool = False) -> Any:
    from ..config import Config
    from .factory import create_knowledge_store
    from .postgres_store import PostgresKnowledgeStore

    settings = Config()
    if require_postgres and not settings.knowledge_database_url:
        raise SystemExit("file import requires KNOWLEDGE_DATABASE_URL (PostgreSQL)")
    store = create_knowledge_store(settings)
    if store is None:
        raise SystemExit("knowledge store is disabled or unavailable")
    if require_postgres and not isinstance(store, PostgresKnowledgeStore):
        raise SystemExit("file import requires a PostgreSQL knowledge store")
    return store


def _delete_record(store: Any, record_id: str) -> bool:
    delete = getattr(store, "delete_document", None)
    if delete:
        if delete(record_id):
            return True
        delete_incident = getattr(store, "delete_incident", None)
        return bool(delete_incident(record_id)) if delete_incident else False
    with store._lock:
        store._conn.execute("DELETE FROM kb_chunks WHERE document_id = ?", (record_id,))
        cur = store._conn.execute("DELETE FROM kb_documents WHERE document_id = ?", (record_id,))
        if cur.rowcount == 0:
            cur = store._conn.execute("DELETE FROM incidents WHERE incident_id = ?", (record_id,))
        store._rebuild_fts()
        store._conn.commit()
        return cur.rowcount > 0


if __name__ == "__main__":
    main()
