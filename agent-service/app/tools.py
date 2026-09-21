"""LLM tool schemas and dispatch to the connector.

The agent exposes only a small set of generic read-only tools; the connector
owns fact-gathering, the LLM owns investigation orchestration.
"""

import json
from typing import Any, Optional

from .connector import ConnectorClient
from .models import ResourceRef
from .root_causes import ROOT_CAUSE_CODES

SUPPORTED_KINDS = [
    "Pod",
    "Deployment",
    "ReplicaSet",
    "StatefulSet",
    "DaemonSet",
    "Service",
    "Node",
    "PersistentVolumeClaim",
    "Namespace",
]

# Single, deterministic time-window definition shared with the connector
# (see docs/diagnosis-center.md "告警时间窗"):
#   * window length W = min(requested, MAX_RANGE_MINUTES) — the connector's
#     existing default, never increased; no arbitrary start/end is accepted.
#   * manual run:  [now - W, now]
#   * alert run:   [starts_at - W, starts_at]  (anchor injected by code below,
#     never taken from the model).
MAX_RANGE_MINUTES = 30

# Tools whose time window must be anchored to the trusted alert timestamp.
ALERT_ANCHOR_TOOLS = frozenset({"query_metrics", "query_logs"})

# Tools that accept a range_minutes window (see MAX_RANGE_MINUTES).
RANGE_TOOLS = ALERT_ANCHOR_TOOLS


def apply_alert_anchor(name: str, args: dict[str, Any],
                       alert: Any = None) -> dict[str, Any]:
    """Inject the alert time anchor and bound the window (deterministic).

    The model's tool arguments are untrusted: any `alert_time`/`alert_expected`
    it emits is dropped, and for alert runs the anchor is set from the trusted
    `AlertContext.starts_at`. Manual runs carry no anchor at all (now-relative
    semantics are preserved in the connector).

    An alert run whose alert context has no usable `starts_at` keeps
    `alert_expected=True` with no `alert_time`: the connector then fails closed
    with an explicit degraded result instead of silently answering a
    now-relative query. `range_minutes` is clamped to MAX_RANGE_MINUTES so a
    model cannot widen the window.
    """
    if name not in RANGE_TOOLS:
        return args
    out = dict(args)
    out.pop("alert_time", None)
    out.pop("alert_expected", None)
    starts_at = getattr(alert, "starts_at", None) if alert is not None else None
    if alert is not None:
        out["alert_expected"] = True
        if starts_at:
            out["alert_time"] = starts_at
    requested = out.get("range_minutes")
    if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
        out["range_minutes"] = MAX_RANGE_MINUTES
    else:
        out["range_minutes"] = min(requested, MAX_RANGE_MINUTES)
    return out


def _resource_param() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": SUPPORTED_KINDS},
            "namespace": {"type": "string", "description": "Required except for cluster-scoped kinds (Node, Namespace)."},
            "name": {"type": "string"},
        },
        "required": ["kind", "name"],
    }


def tool_definitions(capabilities: dict[str, Any] | None = None,
                     retrieval: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Tool list for the LLM.

    - capabilities: connector data-source flags (prometheus/loki).
    - retrieval: {knowledge: bool, incidents: bool} gating the Phase 4
      retrieval tools (design §25.3)."""
    capabilities = capabilities or {}
    retrieval = retrieval or {}

    defs: list[dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "inspect",
                "description": (
                    "Get the normalized desired_state, actual_state, conditions and anomalies "
                    "for a Kubernetes resource (Pod, Deployment, Node, PVC, Service, ...). "
                    "Always start here with the target resource."
                ),
                "parameters": _resource_param(),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "relations",
                "description": (
                    "Find resources related to a resource: owner chain (ReplicaSet/Deployment), "
                    "and for Pods also the Node, matching Services, PVCs and ServiceAccount. "
                    "Use to navigate the resource graph."
                ),
                "parameters": _resource_param(),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "events",
                "description": (
                    "Get recent Kubernetes events for a resource (e.g. FailedScheduling, BackOff, "
                    "FailedMount, OOMKilling). High-value failure semantics."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **_resource_param()["properties"],
                        "limit": {"type": "integer", "description": "Max events to return (default 50)."},
                        "since_hours": {"type": "integer", "description": "Lookback window in hours (default 1)."},
                    },
                    "required": ["kind", "name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "logs",
                "description": (
                    "Get container logs from the Kubernetes Pod Logs API. Only Pod targets are supported. "
                    "Set previous=true to inspect the previous (crashed) container, useful for CrashLoopBackOff."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "name": {"type": "string", "description": "Pod name."},
                        "container": {"type": "string", "description": "Optional container name; defaults to the first container."},
                        "previous": {"type": "boolean", "description": "Read logs from the previous container instance."},
                        "tail_lines": {"type": "integer", "description": "Number of tail lines (default 300)."},
                    },
                    "required": ["namespace", "name"],
                },
            },
        },
    ]

    if capabilities.get("prometheus.metrics"):
        defs.append({
            "type": "function",
            "function": {
                "name": "query_metrics",
                "description": (
                    "Query Prometheus metrics (memory or cpu) for the target Pod or Node over a "
                    "recent window. Use to confirm resource-exhaustion hypotheses (OOM, CPU saturation). "
                    "Returns a bounded summary (latest/max/avg) plus samples; unavailable data degrades explicitly. "
                    "Time window: for auto-triggered alert diagnoses the server anchors the window to the "
                    "alert's starts_at automatically (you cannot and need not pass a timestamp); for manual "
                    "diagnoses it is relative to now. range_minutes only sets the window length (max 30)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["Pod", "Node"]},
                        "namespace": {"type": "string", "description": "Namespace of the Pod (empty for Node)."},
                        "name": {"type": "string"},
                        "metric": {"type": "string", "enum": ["memory", "cpu"]},
                        "range_minutes": {"type": "integer", "description": "Window length in minutes (default 30, max 30). The end of the window is set by the server (alert time or now)."},
                    },
                    "required": ["kind", "name", "metric"],
                },
            },
        })

    if capabilities.get("loki.logs"):
        defs.append({
            "type": "function",
            "function": {
                "name": "query_logs",
                "description": (
                    "Query aggregated container logs for a Pod from Loki over a recent window. "
                    "Returns error-line counts, top patterns and a bounded evidence list. "
                    "Complement the kubernetes.logs tool when deeper/historical log evidence is needed. "
                    "Time window: for auto-triggered alert diagnoses the server anchors the window to the "
                    "alert's starts_at automatically (you cannot and need not pass a timestamp); for manual "
                    "diagnoses it is relative to now. range_minutes only sets the window length (max 30)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string"},
                        "name": {"type": "string", "description": "Pod name."},
                        "range_minutes": {"type": "integer", "description": "Window length in minutes (default 30, max 30). The end of the window is set by the server (alert time or now)."},
                        "filter": {"type": "string", "description": "Optional substring filter, e.g. 'error'."},
                        "max_lines": {"type": "integer", "description": "Max lines (default 200)."},
                    },
                    "required": ["namespace", "name"],
                },
            },
        })

    if retrieval.get("knowledge"):
        defs.append({
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": (
                    "Search the ops knowledge base (runbooks / product docs / known issues) for "
                    "guidance on a symptom or hypothesis. Returns cited, versioned references "
                    "with retrieval_id. Only use to fill a knowledge gap; references must never "
                    "be treated as real-time evidence or override it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural-language symptom/hypothesis, e.g. 'OOMKilled 容器重启'. English works too."},
                        "source_types": {"type": "array", "items": {"type": "string", "enum": ["runbook", "product_doc", "known_issue", "sop"]}},
                        "product": {"type": "string"},
                        "versions": {"type": "array", "items": {"type": "string"}},
                        "environment": {"type": "string"},
                        "top_k": {"type": "integer", "description": "1..5"},
                    },
                    "required": ["query"],
                },
            },
        })

    if retrieval.get("incidents"):
        defs.append({
            "type": "function",
            "function": {
                "name": "search_incidents",
                "description": (
                    "Search verified historical incidents for similar symptoms/root-cause codes. "
                    "Returns incidents with retrieval_id, evidence summary, remediation and "
                    "verification. Historical cases only help form hypotheses; they are never "
                    "evidence for the current diagnosis."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symptoms": {"type": "array", "items": {"type": "string"}, "description": "e.g. ['CrashLoopBackOff','OOMKilled']"},
                        "root_cause_candidates": {"type": "array", "items": {"type": "string"}, "description": "e.g. ['CONTAINER_OOMKILLED']"},
                        "resource_kind": {"type": "string"},
                        "product": {"type": "string"},
                        "product_version": {"type": "string"},
                        "top_k": {"type": "integer", "description": "1..5"},
                    },
                    "required": ["symptoms"],
                },
            },
        })

    defs.append({
        "type": "function",
        "function": {
            "name": "submit_result",
                "description": (
                    "Call when the investigation is complete to submit the final structured diagnosis. "
                    "This is mandatory: you cannot finish without calling it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symptom": {"type": "string", "description": "Short description of the user-visible problem (Simplified Chinese)."},
                        "root_cause_code": {
                            "type": "string",
                            "enum": sorted(list(ROOT_CAUSE_CODES)) + [""],
                            "description": (
                                "Machine-scorable root cause code. Set to the exact enum value matching "
                                "the evidence. Set to empty string \"\" ONLY when evidence is insufficient "
                                "to determine a unique root cause (then also set insufficient_evidence=true)."
                            ),
                        },
                        "insufficient_evidence": {
                            "type": "boolean",
                            "description": "true when evidence is insufficient for a unique root cause; root_cause_code and root_cause must be empty then.",
                        },
                        "evidence": {
                            "type": "array",
                            "description": "Evidence supporting the conclusion. Prefer structured fields (source, resource_uid, path, operator, value) that exactly match what the tools returned.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "source": {"type": "string", "description": "e.g. kubernetes.status, kubernetes.events, kubernetes.logs"},
                                    "resource_uid": {"type": "string"},
                                    "path": {"type": "string", "description": "JSONPath/field path of the fact in the resource, e.g. status.containerStatuses[0].lastState.terminated.reason"},
                                    "operator": {"type": "string", "enum": ["equals", "contains"]},
                                    "value": {"type": "string", "description": "The observed value at the path."},
                                    "observed_at": {"type": "string"},
                                    "summary": {"type": "string", "description": "Human-readable summary."},
                                },
                                "required": ["source", "summary"],
                            },
                        },
                        "root_cause": {
                            "type": "string",
                            "description": "Precise root cause in Simplified Chinese. Leave EMPTY if evidence is insufficient.",
                        },
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "recommendations": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Concrete remediation actions in Simplified Chinese.",
                        },
                        "missing_evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "What additional data would be needed to be conclusive.",
                        },
                        "knowledge_references": {
                            "type": "array",
                            "description": "Optional knowledge-base references that guided hypothesis/investigation. Never root-cause evidence.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "retrieval_id": {"type": "string", "description": "A retrieval_id returned by search_knowledge."},
                                    "used_for": {"type": "string", "enum": ["hypothesis", "investigation", "explanation", "recommendation"]},
                                },
                                "required": ["retrieval_id", "used_for"],
                            },
                        },
                        "historical_cases": {
                            "type": "array",
                            "description": "Optional similar verified incidents. Never root-cause evidence.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "retrieval_id": {"type": "string", "description": "A retrieval_id returned by search_incidents."},
                                    "used_for": {"type": "string", "enum": ["hypothesis", "investigation", "explanation", "recommendation"]},
                                },
                                "required": ["retrieval_id", "used_for"],
                            },
                        },
                    },
                    "required": ["symptom", "root_cause_code", "insufficient_evidence", "evidence", "confidence", "recommendations"],
                },
            },
        })

    return defs


def target_payload(kind: str, namespace: str, name: str,
                   uid: Optional[str] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"kind": kind, "name": name}
    if namespace:
        payload["namespace"] = namespace
    if uid:
        # The diagnosis target's UID lets the connector detect recreation.
        payload["uid"] = uid
    return payload


def execute_tool(connector: ConnectorClient, name: str, args: dict[str, Any]) -> str:
    """Execute a tool call against the connector and return a JSON string for the LLM."""

    def _target() -> dict[str, Any]:
        payload = target_payload(args.get("kind", ""), args.get("namespace", ""), args.get("name", ""))
        if args.get("uid"):
            payload["uid"] = args["uid"]
        return payload

    if name == "inspect":
        return json.dumps(connector.inspect(_target()), ensure_ascii=False)
    if name == "relations":
        return json.dumps(connector.relations(_target()), ensure_ascii=False)
    if name == "events":
        return json.dumps(
            connector.events(
                _target(),
                limit=args.get("limit"),
                since_hours=args.get("since_hours"),
            ),
            ensure_ascii=False,
        )
    if name == "logs":
        return json.dumps(
            connector.logs(
                target_payload("Pod", args.get("namespace", ""), args.get("name", ""),
                               args.get("uid")),
                container=args.get("container"),
                previous=bool(args.get("previous", False)),
                tail_lines=args.get("tail_lines"),
            ),
            ensure_ascii=False,
        )
    if name == "query_metrics":
        return json.dumps(
            connector.query_metrics(
                _target(),
                metric=args.get("metric"),
                range_minutes=args.get("range_minutes"),
                alert_time=args.get("alert_time"),
                alert_expected=args.get("alert_expected"),
            ),
            ensure_ascii=False,
        )
    if name == "query_logs":
        return json.dumps(
            connector.query_logs(
                target_payload("Pod", args.get("namespace", ""), args.get("name", ""),
                               args.get("uid")),
                range_minutes=args.get("range_minutes"),
                filter=args.get("filter"),
                max_lines=args.get("max_lines"),
                alert_time=args.get("alert_time"),
                alert_expected=args.get("alert_expected"),
            ),
            ensure_ascii=False,
        )
    return json.dumps({"error": f"unknown tool: {name}"})
