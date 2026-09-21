"""Deterministic scorer (design §23.7; scoring semantics v2).

Scoring uses only the case Ground Truth, the diagnosis result and the trace.
No LLM judging, no free-text inference.

v2 clarifies the result taxonomy so failure categories are not silently
counted as abstention, and makes the evidence metric an explicit
required-evidence match ratio over de-duplicated output entries.

v3 adds the versioned root-cause vocabulary linkage and budget/policy
observability. v4 adds the `contains` evidence operator (event/log messages are
not exact strings) and lets evidence omit the operator instead of forcing an
exact echo of it. v5 makes `contains` symmetric with `equals`: an evidence item
that declares a conflicting operator no longer matches, so the runtime gate and
the scorer agree on operator semantics.
"""

import sys
from pathlib import Path
from typing import Any, Optional

from .cases import Case

# Bump when scoring semantics change. Results produced by different versions
# must not be diffed directly; recompute from raw results with one scorer.
SCORER_VERSION = "5"

# Share the versioned root cause vocabulary with the agent service.
_AGENT_SERVICE = Path(__file__).resolve().parents[1] / "agent-service"
if str(_AGENT_SERVICE) not in sys.path:
    sys.path.insert(0, str(_AGENT_SERVICE))
from app.root_causes import ROOT_CAUSE_CODES  # noqa: E402

VERDICT_FIXTURE_FAILED = "fixture_failed"
VERDICT_SYSTEM_FAILED = "system_failed"
VERDICT_SCHEMA_FAILED = "schema_failed"
VERDICT_CORRECT = "diagnosis_correct"
VERDICT_INCORRECT = "diagnosis_incorrect"

# Valid failure layers reported in traces.
KNOWN_LAYERS = {
    "fixture", "agent_planning", "llm_transport", "tool_arguments",
    "connector", "kubernetes_api", "result_schema", "scoring",
}


def _matches_required(evidence: dict[str, Any], req) -> bool:
    """A required-evidence constraint matches when every field the ground truth
    specifies is present in the output evidence and the value satisfies the
    declared operator (default: exact equality).

    Event/log messages are not exact strings, so ground truth may use
    `operator: contains` (substring). The evidence's own operator, when present,
    must agree with the requirement; omitting it is allowed.
    """
    if req.resource_uid is not None and evidence.get("resource_uid") != req.resource_uid:
        return False
    if evidence.get("source") != req.source:
        return False
    # Path matching only when the ground truth specifies one (resource-status
    # facts). Events-based evidence is matched on source + value.
    if req.path and evidence.get("path") != req.path:
        return False
    declared = evidence.get("value")
    stated = evidence.get("operator")
    if req.operator == "contains":
        # Omitting the operator is allowed, declaring `contains` is allowed;
        # declaring anything else (e.g. equals) is a different claim.
        if stated not in (None, "contains"):
            return False
        return declared is not None and str(req.value) in str(declared)
    # `equals` stays strict: the evidence must declare the same operator, so a
    # looser claim (e.g. contains) cannot masquerade as an exact fact.
    if req.operator and stated != req.operator:
        return False
    return str(declared) == str(req.value)


def _dedup_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """De-duplicate output evidence on a stable key. Missing fields are kept as
    None rather than filled in, so unknown entries never merge with real ones."""
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for ev in evidence:
        value = ev.get("value")
        key = (
            ev.get("resource_uid"),
            ev.get("source"),
            ev.get("path"),
            ev.get("operator"),
            None if value is None else str(value),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def _is_explicit(root_cause_code: Optional[str], root_cause: Optional[str]) -> bool:
    return bool((root_cause_code or "").strip() or (root_cause or "").strip())


def score_case(case: Case, *, fixture_ready: bool, diagnosis_status: str,
               result: Optional[dict[str, Any]], error: Optional[str],
               trace: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Produce one scored row for a case attempt (scoring semantics v2)."""
    gt = case.ground_truth

    row: dict[str, Any] = {
        "scorer_version": SCORER_VERSION,
        "fixture_ready": fixture_ready,
        "abstention_expected": gt.abstention_expected,
        "explicit_root_cause": False,
        "valid_abstention": False,
        "conflicting_abstention": False,
        "invalid_output": False,
        "root_cause_correct": None,
        "wrong_root_cause": False,
        "abstention_correct": None,
        "evidence_recall": None,
        "required_evidence_match_ratio": None,
        "evidence_returned": 0,
        "evidence_total_entries": 0,
        "evidence_matched_entries": 0,
        "evidence_extra_entries": 0,
        "evidence_unsupported_entries": 0,
        "required_evidence": len(gt.required_evidence),
    }

    if not fixture_ready:
        row["verdict"] = VERDICT_FIXTURE_FAILED
        return row
    if diagnosis_status != "completed" or result is None:
        row["verdict"] = VERDICT_SYSTEM_FAILED
        return row

    code = result.get("root_cause_code")
    code = code.strip() if isinstance(code, str) else None
    text = result.get("root_cause")
    text = text.strip() if isinstance(text, str) else None
    explicit = _is_explicit(code, text)
    row["explicit_root_cause"] = explicit

    # A code outside the versioned vocabulary is a schema failure. Any explicit
    # conclusion it carries stays visible as a risk (schema_failed_with_root_cause).
    if code is not None and code not in ROOT_CAUSE_CODES:
        row["verdict"] = VERDICT_SCHEMA_FAILED
        return row

    insufficient = bool(result.get("insufficient_evidence"))
    if insufficient and explicit:
        # Contradictory output: invalid, not a valid abstention. It must not be
        # scored as a correct diagnosis even when the code happens to match the
        # ground truth; the explicit conclusion stays visible as a risk marker.
        row["conflicting_abstention"] = True
        row["invalid_output"] = True
        row["explicit_root_cause"] = True
        if gt.abstention_expected:
            row["abstention_correct"] = False
            row["wrong_root_cause"] = True
        else:
            row["root_cause_correct"] = False
            row["wrong_root_cause"] = False
        row["verdict"] = VERDICT_INCORRECT
        return _with_evidence_metrics(row, result, gt)

    valid_abstention = insufficient and not explicit
    row["valid_abstention"] = valid_abstention

    if gt.abstention_expected:
        row["abstention_correct"] = valid_abstention
        # Any explicit conclusion on an abstention case is a wrong root cause.
        row["wrong_root_cause"] = explicit
        row["verdict"] = VERDICT_CORRECT if valid_abstention else VERDICT_INCORRECT
    else:
        correct = explicit and code in gt.accepted_root_cause_codes
        row["root_cause_correct"] = correct
        row["wrong_root_cause"] = explicit and not correct
        row["verdict"] = VERDICT_CORRECT if correct else VERDICT_INCORRECT

    return _with_evidence_metrics(row, result, gt)


def _with_evidence_metrics(row: dict[str, Any], result: dict[str, Any], gt) -> dict[str, Any]:
    if row["verdict"] in (VERDICT_CORRECT, VERDICT_INCORRECT):
        evidence = _dedup_evidence(result.get("evidence") or [])
        matched_req: set[int] = set()
        matched_entries = 0
        extra_entries = 0
        unsupported_entries = 0
        for ev in evidence:
            # Traceability proxy only: an entry is "supported" when it carries
            # the identifiers needed to locate its source fact. This is NOT a
            # truthfulness/fabrication judgment (handoff §3.2).
            traceable = bool(ev.get("source")) and ev.get("value") is not None
            if not traceable:
                unsupported_entries += 1
            hit = False
            for i, req in enumerate(gt.required_evidence):
                if _matches_required(ev, req):
                    matched_req.add(i)
                    hit = True
            if hit:
                matched_entries += 1
            elif traceable:
                # Extra evidence (beyond required) that is still traceable.
                extra_entries += 1
        row["evidence_returned"] = len(evidence)
        row["evidence_total_entries"] = len(evidence)
        row["evidence_matched_entries"] = matched_entries
        row["evidence_extra_entries"] = extra_entries
        row["evidence_unsupported_entries"] = unsupported_entries
        if evidence:
            row["required_evidence_match_ratio"] = round(matched_entries / len(evidence), 3)
        if gt.required_evidence:
            row["evidence_recall"] = round(len(matched_req) / len(gt.required_evidence), 3)
    return row


def summarize_trace(trace: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Extract run-quality metrics from a collected trace (empty when absent).

    Unknown quantities stay None: a missing trace must not be reported as zero
    consumption. ``token_usage`` is the sum of known prompt+completion tokens
    per LLM call; ``token_usage_complete`` says whether every LLM call reported
    both fields.
    """
    out = {
        "trace_present": False,
        "tool_calls": 0,
        "duplicate_tool_calls": 0,
        "llm_calls": 0,
        "token_usage": None,
        "token_usage_complete": False,
        "duration_ms": None,
        "trace_failure_layer": None,
        "truncated_logs": False,
        "multi_tool_rejected_rounds": 0,
        "rounds_used": None,
        "budget_exhausted": False,
        "effective_max_tool_calls": None,
        "effective_max_agent_rounds": None,
    }
    if not trace:
        return out
    out["trace_present"] = True
    spans = trace.get("spans", [])
    llm_spans = [s for s in spans if s.get("kind") == "llm_call"]
    out["llm_calls"] = len(llm_spans)
    tools = [s for s in spans if s.get("kind") == "tool_call"]
    out["tool_calls"] = len(tools)
    seen: set[tuple[str, str]] = set()
    dupes = 0
    for s in tools:
        key = (s.get("name"), s.get("attributes", {}).get("args_summary"))
        if key in seen:
            dupes += 1
        seen.add(key)
    out["duplicate_tool_calls"] = dupes
    known: list[int] = []
    complete = len(llm_spans) > 0
    for s in llm_spans:
        attrs = s.get("attributes") or {}
        pt = attrs.get("prompt_tokens")
        ct = attrs.get("completion_tokens")
        if pt is None or ct is None:
            complete = False
            continue
        known.append(pt + ct)
    if known:
        out["token_usage"] = sum(known)
    out["token_usage_complete"] = complete
    finish = next((s for s in spans if s.get("name") == "diagnosis"
                   and "duration_ms" in (s.get("attributes") or {})), None)
    if finish:
        out["duration_ms"] = finish["attributes"].get("duration_ms")
        out["rounds_used"] = finish["attributes"].get("rounds_used")
        out["multi_tool_rejected_rounds"] = finish["attributes"].get(
            "multi_tool_rejected_rounds", 0) or 0
        out["effective_max_tool_calls"] = finish["attributes"].get("max_tool_calls")
        out["effective_max_agent_rounds"] = finish["attributes"].get("max_agent_rounds")
    layer = next((s.get("failure_layer") for s in spans if s.get("failure_layer")), None)
    if layer and layer in KNOWN_LAYERS:
        out["trace_failure_layer"] = layer
    out["truncated_logs"] = any(
        (s.get("attributes") or {}).get("truncated") for s in tools
        if s.get("name") == "tool.logs"
    )
    return out
