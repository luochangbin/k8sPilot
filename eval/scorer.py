"""Deterministic scorer (design §23.7).

Scoring uses only the case Ground Truth, the diagnosis result and the trace.
No LLM judging, no free-text inference.
"""

import sys
from pathlib import Path
from typing import Any, Optional

from .cases import Case

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
    if evidence.get("source") != req.source:
        return False
    # Path matching only when the ground truth specifies one (resource-status
    # facts). Events-based evidence is matched on source + value.
    if req.path and evidence.get("path") != req.path:
        return False
    if req.operator and evidence.get("operator") and evidence.get("operator") != req.operator:
        return False
    return str(evidence.get("value")) == str(req.value)


def score_case(case: Case, *, fixture_ready: bool, diagnosis_status: str,
               result: Optional[dict[str, Any]], error: Optional[str],
               trace: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Produce one scored row for a case attempt."""
    gt = case.ground_truth

    if not fixture_ready:
        verdict = VERDICT_FIXTURE_FAILED
    elif diagnosis_status != "completed":
        verdict = VERDICT_SYSTEM_FAILED
    elif result is None:
        verdict = VERDICT_SYSTEM_FAILED
    else:
        code = result.get("root_cause_code")
        # Schema failure = code that is not part of the versioned vocabulary.
        # A code that IS valid vocabulary but not the accepted answer is a
        # wrong diagnosis, not a schema failure.
        if code is not None and code not in ROOT_CAUSE_CODES:
            verdict = VERDICT_SCHEMA_FAILED
        elif gt.abstention_expected:
            verdict = VERDICT_CORRECT if result.get("insufficient_evidence") else VERDICT_INCORRECT
        else:
            verdict = VERDICT_CORRECT if code in gt.accepted_root_cause_codes else VERDICT_INCORRECT

    row: dict[str, Any] = {
        "verdict": verdict,
        "root_cause_correct": None,
        "abstention_correct": None,
        "wrong_root_cause": False,
        "evidence_recall": None,
        "evidence_precision": None,
        "evidence_returned": 0,
        "required_evidence": len(gt.required_evidence),
    }

    if verdict == VERDICT_CORRECT:
        if gt.abstention_expected:
            row["abstention_correct"] = True
        else:
            row["root_cause_correct"] = True

    if verdict == VERDICT_INCORRECT:
        if gt.abstention_expected:
            row["abstention_correct"] = False
        else:
            row["root_cause_correct"] = False
            if result and result.get("root_cause_code"):
                row["wrong_root_cause"] = True

    if result and (verdict == VERDICT_CORRECT or verdict == VERDICT_INCORRECT):
        evidence = result.get("evidence") or []
        matched_req: set[int] = set()
        verifiable = 0
        for ev in evidence:
            hit = False
            for i, req in enumerate(gt.required_evidence):
                if _matches_required(ev, req):
                    matched_req.add(i)
                    hit = True
            if hit:
                verifiable += 1
        row["evidence_returned"] = len(evidence)
        if gt.required_evidence:
            row["evidence_recall"] = round(len(matched_req) / len(gt.required_evidence), 3)
        if evidence:
            row["evidence_precision"] = round(verifiable / len(evidence), 3)

    return row


def summarize_trace(trace: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Extract run-quality metrics from a collected trace (empty when absent)."""
    out = {
        "tool_calls": 0,
        "duplicate_tool_calls": 0,
        "llm_calls": 0,
        "token_usage": 0,
        "duration_ms": None,
        "trace_failure_layer": None,
        "truncated_logs": False,
    }
    if not trace:
        return out
    spans = trace.get("spans", [])
    out["llm_calls"] = sum(1 for s in spans if s.get("kind") == "llm_call")
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
    out["token_usage"] = sum(
        (s.get("attributes") or {}).get("prompt_tokens", 0)
        + (s.get("attributes") or {}).get("completion_tokens", 0)
        for s in spans if s.get("kind") == "llm_call"
    )
    finish = next((s for s in spans if s.get("name") == "diagnosis"
                   and "duration_ms" in (s.get("attributes") or {})), None)
    if finish:
        out["duration_ms"] = finish["attributes"].get("duration_ms")
    layer = next((s.get("failure_layer") for s in spans if s.get("failure_layer")), None)
    if layer and layer in KNOWN_LAYERS:
        out["trace_failure_layer"] = layer
    out["truncated_logs"] = any(
        (s.get("attributes") or {}).get("truncated") for s in tools
        if s.get("name") == "tool.logs"
    )
    return out
