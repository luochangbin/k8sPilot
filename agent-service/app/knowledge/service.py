"""Retrieval service (agent-internal) for knowledge & experience.

Exposes strongly-typed, bounded searches. It only filters/ranks/returns
references with citations; it never decides a root cause. Real-time connector
evidence always outranks any retrieval result (design §25.1).
"""

import uuid
from typing import Any, Optional

from .models import HistoricalCase, KnowledgeReference
from .store import KnowledgeStore

# Hard limits (design §25.3).
MAX_TOP_K = 5
MAX_CONTENT_CHARS = 600
MAX_TOTAL_CHARS = 3000


class KnowledgeService:
    def __init__(self, store: KnowledgeStore) -> None:
        self._store = store

    def enabled(self) -> bool:
        return True

    def search_knowledge(self, query: str, top_k: int = 5,
                         filters: Optional[dict[str, Any]] = None) -> list[KnowledgeReference]:
        k = max(1, min(top_k, MAX_TOP_K))
        hits = self._store.search_chunks(query, k, filters)
        refs: list[KnowledgeReference] = []
        total = 0
        for h in hits:
            if total >= MAX_TOTAL_CHARS:
                break
            doc = h["document"]
            content = h["chunk_content"]
            if len(content) > MAX_CONTENT_CHARS:
                content = content[:MAX_CONTENT_CHARS - 3] + "..."
            remaining = MAX_TOTAL_CHARS - total
            if len(content) > remaining:
                if remaining <= 0:
                    break
                content = content[:remaining - 3] + "..." if remaining > 3 else content[:remaining]
            total += len(content)
            refs.append(KnowledgeReference(
                retrieval_id=f"kb_{uuid.uuid4().hex[:10]}",
                type="knowledge",
                score=round(-float(h.get("rank", 0.0)), 4) if h.get("rank") is not None else 0.0,
                content=content,
                citation={
                    "document_id": doc.document_id,
                    "title": doc.title,
                    "source_uri": doc.source_uri,
                    "section": h["section"],
                    "version": ",".join(doc.versions) if doc.versions else "",
                    "updated_at": doc.updated_at or "",
                    "page_start": h.get("page_start"),
                    "page_end": h.get("page_end"),
                },
            ))
        return refs

    def search_incidents(self, query: str, top_k: int = 5,
                         filters: Optional[dict[str, Any]] = None) -> list[HistoricalCase]:
        k = max(1, min(top_k, MAX_TOP_K))
        rows = self._store.search_incidents(query, k, filters)
        cases: list[HistoricalCase] = []
        for r in rows:
            cases.append(HistoricalCase(
                retrieval_id=f"inc_{uuid.uuid4().hex[:10]}",
                type="incident",
                score=round(-float(r.get("rank", 0.0)), 4) if r.get("rank") is not None else 0.0,
                incident_id=r["incident_id"],
                product=r["product"],
                product_version=r["product_version"],
                resource_kind=r["resource_kind"],
                symptoms=_load_list(r["symptoms"]),
                root_cause_code=r["root_cause_code"],
                evidence_summary=_truncate(r["evidence_summary"], MAX_CONTENT_CHARS),
                remediation_summary=_truncate(r["remediation_summary"], MAX_CONTENT_CHARS),
                verification=_load_dict(r["verification"]),
            ))
        return cases


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "..."


def _load_list(s: Any) -> list:
    import json
    try:
        return json.loads(s) if isinstance(s, str) else (s or [])
    except ValueError:
        return []


def _load_dict(s: Any) -> dict:
    import json
    try:
        return json.loads(s) if isinstance(s, str) else (s or {})
    except ValueError:
        return {}
