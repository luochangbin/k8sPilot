"""Case definitions and loader (design §23.3).

A Case is versioned and bundles fault injection, readiness, ground truth,
budgets and cleanup in one declarative document. Ground truth is never derived
from agent free-text; it comes only from the case definition and real cluster
observations.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

SCHEMA_VERSION = "eval.k8spilot.io/v1alpha1"


class CaseError(Exception):
    pass


@dataclass
class Target:
    apiVersion: str
    kind: str
    namespace: str
    name: str


@dataclass
class ReadyWhen:
    type: str  # jsonpath_equals | jsonpath_exists | jsonpath_gte |
    #            event_message_contains | all
    path: Optional[str] = None
    value: Optional[str] = None
    timeout_seconds: int = 120
    # For `all`: every listed condition (same keys as a single ready_when) must
    # hold, e.g. an Event carrying the expected semantics AND the container
    # status already in its steady state.
    conditions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class EvidenceRequirement:
    source: str
    path: str
    operator: Optional[str]
    value: str
    # Optional resource identity constraint; enforced only when the ground
    # truth specifies it (evidence must carry a matching resource_uid).
    resource_uid: Optional[str] = None


@dataclass
class GroundTruth:
    accepted_root_cause_codes: list[str] = field(default_factory=list)
    required_evidence: list[EvidenceRequirement] = field(default_factory=list)
    abstention_expected: bool = False


@dataclass
class Budgets:
    diagnosis_timeout_seconds: int = 60
    # Investigation budgets are enforced by the agent (rounds != tool calls) and
    # frozen when the session is created; a Case may only shrink them.
    max_tool_calls: int = 12
    max_agent_rounds: int = 12


@dataclass
class Case:
    schema_version: str
    id: str
    case_version: str
    suite: str
    description: str
    target: Target
    setup_manifests: list[Path] = field(default_factory=list)
    preflight: list[dict[str, Any]] = field(default_factory=list)
    inject_action: str = "apply"
    ready_when: ReadyWhen = field(default_factory=ReadyWhen)
    ground_truth: GroundTruth = field(default_factory=GroundTruth)
    budgets: Budgets = field(default_factory=Budgets)
    cleanup_action: str = "delete_manifest"

    def key(self) -> str:
        return f"{self.id}@{self.case_version}"


def load_case(path: Path) -> Case:
    """Parse and validate a single case YAML file."""
    if not path.is_file():
        raise CaseError(f"case file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CaseError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CaseError(f"case {path} must be a mapping")

    schema = raw.get("schema_version")
    if schema != SCHEMA_VERSION:
        raise CaseError(f"case {path}: unsupported schema_version {schema!r}")

    case_id = _require(raw, "id", path)
    case_version = str(_require(raw, "case_version", path))
    # Legacy field: suite membership is defined by eval/suites/*.yaml, not by
    # the individual case file. Kept optional so a case belongs to as many
    # suites as the suite files declare.
    suite = _str(raw.get("suite"))

    t = _require(raw, "target", path)
    kind = _require(t, "kind", path)
    # Cluster-scoped targets (Node, Namespace) have no namespace to declare.
    namespace = _str(t.get("namespace")) if kind not in ("Node", "Namespace") \
        else _str(t.get("namespace"))
    if kind not in ("Node", "Namespace") and "namespace" not in t:
        raise CaseError(f"case {path}: missing required field 'namespace'")
    target = Target(
        apiVersion=t.get("apiVersion", "v1"),
        kind=kind,
        namespace=namespace,
        name=_require(t, "name", path),
    )

    setup = raw.get("setup") or {}
    base_dir = path.parent
    manifests = [base_dir / m for m in (setup.get("manifests") or [])]
    for m in manifests:
        if not m.is_file():
            raise CaseError(f"case {path}: setup manifest not found: {m}")

    rw = raw.get("ready_when") or {}
    ready = ReadyWhen(
        type=rw.get("type", "jsonpath_equals"),
        path=rw.get("path"),
        value=_str(rw.get("value")),
        timeout_seconds=int(rw.get("timeout_seconds", 120)),
        conditions=list(rw.get("conditions") or []),
    )

    gt_raw = raw.get("ground_truth") or {}
    required = [
        EvidenceRequirement(source=e["source"], path=e.get("path", ""),
                            operator=e.get("operator"), value=str(e.get("value", "")),
                            resource_uid=e.get("resource_uid"))
        for e in (gt_raw.get("required_evidence") or [])
    ]
    ground_truth = GroundTruth(
        accepted_root_cause_codes=list(gt_raw.get("accepted_root_cause_codes") or []),
        required_evidence=required,
        abstention_expected=bool(gt_raw.get("abstention_expected", False)),
    )

    b = raw.get("budgets") or {}
    budgets = Budgets(
        diagnosis_timeout_seconds=int(b.get("diagnosis_timeout_seconds", 60)),
        max_tool_calls=int(b.get("max_tool_calls", 12)),
        max_agent_rounds=int(b.get("max_agent_rounds", 12)),
    )

    return Case(
        schema_version=schema,
        id=case_id,
        case_version=case_version,
        suite=suite,
        description=str(raw.get("description", "")),
        target=target,
        setup_manifests=manifests,
        # Top-level field (matching the YAML layout): preflight runs before any
        # resource is created, so it is not part of `setup`.
        preflight=raw.get("preflight") or [],
        inject_action=raw.get("inject", {}).get("action", "apply"),
        ready_when=ready,
        ground_truth=ground_truth,
        budgets=budgets,
        cleanup_action=raw.get("cleanup", {}).get("action", "delete_manifest"),
    )


def load_suite(suite_path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(suite_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CaseError(f"invalid suite YAML {suite_path}: {exc}") from exc
    if not isinstance(raw, dict) or "cases" not in raw:
        raise CaseError(f"suite {suite_path} must declare a 'cases' list")
    return raw


def _require(raw: dict[str, Any], key: str, path: Path) -> Any:
    if key not in raw:
        raise CaseError(f"case {path}: missing required field '{key}'")
    return raw[key]


def _str(v: Any) -> str:
    return "" if v is None else str(v)
