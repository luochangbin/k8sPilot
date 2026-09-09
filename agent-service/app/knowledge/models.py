"""Knowledge & Experience (Phase 4) data model.

Single-cluster, keyword retrieval over a small, curated corpus (SQLite fts5).
Documents are versioned with validity/ACL metadata; only closed + verified
incidents are retrievable. All IDs are stable and content is reference-safe.
"""

from dataclasses import dataclass, field
from typing import Any, Optional


# Document lifecycle states (design §25.4).
DOC_ACTIVE = "active"
DOC_DISCOVERED = "discovered"
DOC_PARSED = "parsed"
DOC_VALIDATED = "validated"
DOC_INDEXED = "indexed"
DOC_QUARANTINED = "quarantined"
DOC_SUPERSEDED = "superseded"
DOC_DELETED = "deleted"

SOURCE_TYPES = ("runbook", "product_doc", "known_issue", "sop")

# Incident retrievable only when verified (design §25.5).
INCIDENT_VERIFIED = "verified"


@dataclass
class KnowledgeDocument:
    document_id: str
    source_type: str            # runbook | product_doc | known_issue | sop
    title: str
    source_uri: str = ""
    product: str = ""           # empty => cluster/kubernetes-generic
    versions: list[str] = field(default_factory=list)
    environments: list[str] = field(default_factory=list)
    owner: str = ""
    valid_from: Optional[str] = None   # ISO datetime
    valid_until: Optional[str] = None  # ISO datetime or None
    checksum: str = ""
    acl_tags: list[str] = field(default_factory=list)
    status: str = DOC_ACTIVE
    content: str = ""           # normalized body used for chunking


@dataclass
class KnowledgeChunk:
    chunk_id: str
    document_id: str
    section: str
    content: str
    document: Optional[KnowledgeDocument] = None  # denormalized convenience


@dataclass
class IncidentCase:
    incident_id: str
    status: str = INCIDENT_VERIFIED
    product: str = ""
    product_version: str = ""
    environment: str = ""
    resource_kind: str = ""
    symptoms: list[str] = field(default_factory=list)
    evidence_signature: list[dict[str, Any]] = field(default_factory=list)
    root_cause_code: str = ""
    remediation_summary: str = ""
    verification: dict[str, Any] = field(default_factory=dict)
    evidence_summary: str = ""   # bounded text of the original evidence


@dataclass
class KnowledgeReference:
    retrieval_id: str
    type: str                   # "knowledge"
    score: float
    content: str
    citation: dict[str, Any]    # {document_id,title,source_uri,section,version,updated_at}
    used_for: str = ""          # hypothesis|investigation|explanation|recommendation


@dataclass
class HistoricalCase:
    retrieval_id: str
    type: str                   # "incident"
    score: float
    incident_id: str
    product: str
    product_version: str
    resource_kind: str
    symptoms: list[str] = field(default_factory=list)
    root_cause_code: str = ""
    evidence_summary: str = ""
    remediation_summary: str = ""
    verification: dict[str, Any] = field(default_factory=dict)
    used_for: str = ""          # hypothesis|investigation|explanation|recommendation
