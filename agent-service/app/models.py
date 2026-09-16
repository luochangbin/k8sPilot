"""Data models for the Phase 1 diagnosis API.

The public contract follows the design's "最小公共契约":
- POST /api/v1/diagnoses accepts a DiagnosisRequest
- GET /api/v1/diagnoses/{id} returns a Diagnosis
Status values: queued | investigating | completed | failed.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class Trigger(str, Enum):
    manual = "manual"
    alert = "alert"  # reserved for Phase 4


# Phase 3 diagnosis surface. These kinds have a「智能诊断」entry in the UI.
DIAGNOSABLE_KINDS = ("Pod", "Deployment", "Node", "PersistentVolumeClaim")


class ResourceRef(BaseModel):
    """Resource identity as sent by the management platform."""

    apiVersion: str = "v1"
    kind: str
    namespace: Optional[str] = None
    name: str
    uid: Optional[str] = None


class AlertStatus(str, Enum):
    firing = "firing"
    resolved = "resolved"


class AlertContext(BaseModel):
    """Normalized Alertmanager alert (Phase 5).

    Carried by the connector's alert webhook adapter; `snapshot` is the
    connector's lightweight fact snapshot, never a full data crawl.
    """

    fingerprint: str
    status: AlertStatus = AlertStatus.firing
    alertname: Optional[str] = None
    starts_at: Optional[str] = None
    group_key: Optional[str] = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    snapshot: Optional[dict] = None
    # True when the connector could not map the alert to a unique resource.
    unresolved_target: bool = False


class DiagnosisRequest(BaseModel):
    """Unified diagnosis request (manual and alert triggers share this shape).

    Single-cluster contract: no cluster_id field (the project is fixed to one
    cluster; the management platform picks the cluster, we only get the resource).
    """

    trigger: Trigger = Trigger.manual
    # Optional only for alert triggers whose target is unresolved; manual and
    # resolved-target alerts still require a resource (validated in the API).
    resource: Optional[ResourceRef] = None
    # Phase 5: normalized Alertmanager context (present when trigger=alert).
    alert: Optional[AlertContext] = None
    # Phase 4 reserves an `alert` field; not used in Phase 1.

    # --- Phase 2 eval fields (optional, backward-compatible) ---
    # Supplied by the Eval Runner; propagated into trace spans for correlation.
    eval_run_id: Optional[str] = None
    case_id: Optional[str] = None
    case_version: Optional[str] = None
    attempt_index: Optional[int] = None

    # --- Phase 4 retrieval gating (optional) ---
    # Overrides for the knowledge/incident retrieval tools. None = enabled when
    # the knowledge module is configured. Used by the Eval Runner for the
    # four-group ablation.
    enable_knowledge: Optional[bool] = None
    enable_incidents: Optional[bool] = None

    # --- Phase 5 / model benchmark (optional) ---
    # Server-side preconfigured model profile name. None = default profile.
    # Rejected (403) unless ENABLE_MODEL_PROFILE_SELECTION is on; unknown
    # profiles are rejected (422) before a session is created.
    model_profile: Optional[str] = None


class Evidence(BaseModel):
    """Evidence item. Structured fields (resource_uid/path/operator/value) are
    the deterministic scoring inputs (design §23.4); summary is for humans."""

    source: str
    observed_at: Optional[str] = None
    summary: str
    # --- Phase 2 eval fields (backward-compatible optional extensions) ---
    resource_uid: Optional[str] = None
    path: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[str] = None


class DiagnosisResult(BaseModel):
    """Structured diagnosis result, never a free-form LLM text blob."""

    symptom: str = ""
    evidence: List[Evidence] = Field(default_factory=list)
    root_cause: Optional[str] = None
    # Machine-scorable root cause code (versioned vocabulary, see root_causes.py).
    root_cause_code: Optional[str] = None
    confidence: Optional[str] = None  # high | medium | low
    recommendations: List[str] = Field(default_factory=list)
    # Explicitly list what is missing when evidence is insufficient. An empty
    # root_cause combined with missing_evidence is a valid "completed" state.
    missing_evidence: List[str] = Field(default_factory=list)
    # True when the agent abstains (insufficient evidence); root_cause_code and
    # root_cause must both be empty then (design §23.4).
    insufficient_evidence: bool = False
    # Investigation trace populated by the agent (tool calls performed).
    investigation_steps: List[str] = Field(default_factory=list)
    # --- Phase 4 retrieval references (never root-cause evidence) ---
    historical_cases: List[dict] = Field(default_factory=list)
    knowledge_references: List[dict] = Field(default_factory=list)


class Diagnosis(BaseModel):
    diagnosis_id: str
    trigger: str
    resource: ResourceRef
    status: str  # queued | investigating | completed | failed
    result: Optional[DiagnosisResult] = None
    error: Optional[str] = None
    # Phase 2 eval correlation fields (optional; present for eval runs).
    eval_run_id: Optional[str] = None
    case_id: Optional[str] = None
    case_version: Optional[str] = None
    attempt_index: Optional[int] = None
    created_at: datetime
    updated_at: datetime
