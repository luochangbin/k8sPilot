"""SQLite (fts5) store for the knowledge & experience module.

Reliable approach for keyword retrieval over a small curated corpus: content
lives in regular tables; an fts5 virtual table is (re)built after every ingest
so we never fight external-content triggers.
"""

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from .models import IncidentCase, KnowledgeChunk, KnowledgeDocument

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_documents (
    document_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    title       TEXT NOT NULL,
    source_uri  TEXT DEFAULT '',
    product     TEXT DEFAULT '',
    versions    TEXT DEFAULT '[]',
    environments TEXT DEFAULT '[]',
    owner       TEXT DEFAULT '',
    valid_from  TEXT,
    valid_until TEXT,
    checksum    TEXT DEFAULT '',
    acl_tags    TEXT DEFAULT '[]',
    status      TEXT NOT NULL,
    content     TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kb_chunks (
    chunk_id    TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES kb_documents(document_id),
    section     TEXT DEFAULT '',
    content     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id     TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    product         TEXT DEFAULT '',
    product_version TEXT DEFAULT '',
    environment     TEXT DEFAULT '',
    resource_kind   TEXT DEFAULT '',
    symptoms        TEXT DEFAULT '[]',
    evidence_signature TEXT DEFAULT '[]',
    root_cause_code TEXT DEFAULT '',
    remediation_summary TEXT DEFAULT '',
    verification    TEXT DEFAULT '{}',
    evidence_summary TEXT DEFAULT '',
    content         TEXT DEFAULT ''
);
"""


def _json(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def _load(s: str, default: Any) -> Any:
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


class KnowledgeStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        path = db_path or ":memory:"
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._rebuild_fts()
            self._conn.commit()

    # ---------- ingestion ----------

    def upsert_document(self, doc: KnowledgeDocument, chunks: list[KnowledgeChunk]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kb_documents "
                "(document_id, source_type, title, source_uri, product, versions, environments, "
                " owner, valid_from, valid_until, checksum, acl_tags, status, content) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc.document_id, doc.source_type, doc.title, doc.source_uri, doc.product,
                 _json(doc.versions), _json(doc.environments), doc.owner,
                 doc.valid_from, doc.valid_until, doc.checksum,
                 _json(doc.acl_tags), doc.status, doc.content),
            )
            self._conn.execute("DELETE FROM kb_chunks WHERE document_id = ?", (doc.document_id,))
            self._conn.executemany(
                "INSERT INTO kb_chunks (chunk_id, document_id, section, content) VALUES (?,?,?,?)",
                [(c.chunk_id, c.document_id, c.section, c.content) for c in chunks],
            )
            self._rebuild_fts()
            self._conn.commit()

    def set_document_status(self, document_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE kb_documents SET status=? WHERE document_id=?",
                               (status, document_id))
            self._conn.commit()

    def list_documents(self, status: Optional[str] = None) -> list[KnowledgeDocument]:
        with self._lock:
            if status:
                rows = self._conn.execute("SELECT * FROM kb_documents WHERE status=?",
                                          (status,)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM kb_documents").fetchall()
        return [self._doc_from_row(r) for r in rows]

    def upsert_incident(self, inc: IncidentCase) -> None:
        content = " ".join(inc.symptoms) + " " + inc.root_cause_code + " " \
            + inc.remediation_summary + " " + inc.evidence_summary
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO incidents "
                "(incident_id, status, product, product_version, environment, resource_kind, "
                " symptoms, evidence_signature, root_cause_code, remediation_summary, "
                " verification, evidence_summary, content) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (inc.incident_id, inc.status, inc.product, inc.product_version, inc.environment,
                 inc.resource_kind, _json(inc.symptoms), _json(inc.evidence_signature),
                 inc.root_cause_code, inc.remediation_summary,
                 _json(inc.verification), inc.evidence_summary, content),
            )
            self._rebuild_fts()
            self._conn.commit()

    def list_incidents(self, status: Optional[str] = None) -> list[IncidentCase]:
        with self._lock:
            if status:
                rows = self._conn.execute("SELECT * FROM incidents WHERE status=?",
                                          (status,)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM incidents").fetchall()
        return [self._inc_from_row(r) for r in rows]

    # ---------- search ----------

    def search_chunks(self, query: str, top_k: int,
                      filters: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        """fts5 keyword search over active docs, pre-filtered by document attrs."""
        filters = filters or {}
        now = _now_iso()
        sql = [
            "SELECT c.chunk_id, c.section, c.content AS chunk_content, d.document_id, d.title,",
            "       d.source_uri, d.source_type, d.product, d.versions, d.environments,",
            "       d.valid_until, d.acl_tags, d.checksum, d.owner, d.valid_from, d.content,",
            "       d.status,",
            "       bm25(chunks_fts) AS rank",
            "  FROM chunks_fts JOIN kb_chunks c ON c.rowid = chunks_fts.rowid",
            "  JOIN kb_documents d ON d.document_id = c.document_id",
            " WHERE chunks_fts MATCH ?",
            "   AND d.status = 'active'",
            "   AND (d.valid_from IS NULL OR d.valid_from <= ?)",
            "   AND (d.valid_until IS NULL OR d.valid_until >= ?)",
        ]
        args: list[Any] = [self._match_expr(query), now, now]
        for key, col, wrap_json in (
            ("source_types", "d.source_type", False),
        ):
            vals = filters.get(key)
            if vals:
                placeholders = ",".join("?" * len(vals))
                sql.append(f" AND {col} IN ({placeholders})")
                args.extend(vals)
        prod = filters.get("product")
        if prod:
            sql.append(" AND d.product = ?")
            args.append(prod)
        versions = filters.get("versions")
        if versions:
            # document versions like ["2.4.x"]: match if any overlaps the request set.
            joins = " OR ".join("d.versions LIKE ?" for _ in versions)
            sql.append(f" AND ({joins})")
            args.extend(f'%"{v}"%' for v in versions)
        env = filters.get("environment")
        if env:
            sql.append(" AND d.environments LIKE ?")
            args.append(f'%"{env}"%')
        sql.append(" ORDER BY rank LIMIT ?")
        args.append(top_k)

        with self._lock:
            rows = self._conn.execute(" ".join(sql), args).fetchall()
        out = []
        for r in rows:
            doc = self._doc_from_row(r)
            out.append({
                "chunk_id": r["chunk_id"],
                "section": r["section"],
                "chunk_content": r["chunk_content"],
                "document": doc,
            })
        return out

    def search_incidents(self, query: str, top_k: int,
                         filters: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        filters = filters or {}
        sql = ["SELECT *, bm25(incidents_fts) AS rank FROM incidents_fts",
               "  JOIN incidents i ON i.rowid = incidents_fts.rowid",
               " WHERE incidents_fts MATCH ? AND i.status = 'verified'"]
        args: list[Any] = [self._match_expr(query)]
        kind = filters.get("resource_kind")
        if kind:
            sql.append(" AND i.resource_kind = ?")
            args.append(kind)
        rcc = filters.get("root_cause_candidates")
        if rcc:
            ph = ",".join("?" * len(rcc))
            sql.append(f" AND i.root_cause_code IN ({ph})")
            args.extend(rcc)
        ver = filters.get("product_version")
        if ver:
            sql.append(" AND i.product_version = ?")
            args.append(ver)
        sql.append(" ORDER BY rank LIMIT ?")
        args.append(top_k)
        with self._lock:
            rows = self._conn.execute(" ".join(sql), args).fetchall()
        return [dict(r) for r in rows]

    # ---------- helpers ----------

    def _match_expr(self, query: str) -> str:
        # Small curated corpus: OR terms (rank handles relevance) so camelCase /
        # inflected tokens (e.g. CrashLoopBackOff) still surface on partial hits.
        words = [w for w in query.replace('"', " ").split() if len(w) > 1]
        return " OR ".join(f'"{w}"' for w in words) if words else '""'

    def _rebuild_fts(self) -> None:
        # (re)create fts5 tables so index always matches content.
        self._conn.execute("DROP TABLE IF EXISTS chunks_fts")
        self._conn.execute(
            "CREATE VIRTUAL TABLE chunks_fts USING fts5(content, tokenize='unicode61')")
        self._conn.execute(
            "INSERT INTO chunks_fts(rowid, content) "
            "SELECT rowid, content FROM kb_chunks")
        self._conn.execute("DROP TABLE IF EXISTS incidents_fts")
        self._conn.execute(
            "CREATE VIRTUAL TABLE incidents_fts USING fts5(content, tokenize='unicode61')")
        self._conn.execute(
            "INSERT INTO incidents_fts(rowid, content) "
            "SELECT rowid, content FROM incidents")

    def _doc_from_row(self, r: sqlite3.Row) -> KnowledgeDocument:
        return KnowledgeDocument(
            document_id=r["document_id"], source_type=r["source_type"], title=r["title"],
            source_uri=r["source_uri"], product=r["product"],
            versions=_load(r["versions"], []), environments=_load(r["environments"], []),
            owner=r["owner"], valid_from=r["valid_from"], valid_until=r["valid_until"],
            checksum=r["checksum"], acl_tags=_load(r["acl_tags"], []), status=r["status"],
            content=r["content"] if "content" in r.keys() else "",
        )

    def _inc_from_row(self, r: sqlite3.Row) -> IncidentCase:
        return IncidentCase(
            incident_id=r["incident_id"], status=r["status"], product=r["product"],
            product_version=r["product_version"], environment=r["environment"],
            resource_kind=r["resource_kind"], symptoms=_load(r["symptoms"], []),
            evidence_signature=_load(r["evidence_signature"], []),
            root_cause_code=r["root_cause_code"], remediation_summary=r["remediation_summary"],
            verification=_load(r["verification"], {}),
            evidence_summary=r["evidence_summary"],
        )


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
