"""Deterministic submission validation: schema gate + evidence assertions.

Code owns the deterministic boundary (design: "LLM 负责语义推理，代码负责确定性
边界"):

* an explicit root cause must carry at least one **verified** real-time tool
  result — the claim must be locatable in, and consistent with, what the tools
  actually returned in this run;
* an abstention must carry no conclusion;
* root cause codes must come from the versioned vocabulary.

Nothing here reasons about hypotheses: it only checks *where* a claim came from
and whether it matches. Log/semantic evidence cannot be resolved by JSONPath, so
those are matched as substrings and reported as UNVERIFIABLE when they cannot be
found — never silently accepted.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from .root_causes import ROOT_CAUSE_CODES

VERIFIED = "verified"
MISMATCH = "mismatch"
UNVERIFIABLE = "unverifiable"

# Strict evidence-source -> tool mapping. A claim's source must name the tool
# that could actually have produced it: "kubernetes.status" can only come from
# inspect, "kubernetes.events" only from events, and so on. No startswith()
# families: a value that merely happens to appear in another tool's output must
# not verify a mis-attributed source.
_SOURCE_TOOL = {
    "kubernetes.status": "inspect",
    "kubernetes.events": "events",
    "kubernetes.logs": "logs",
    "prometheus.metrics": "query_metrics",
    "loki.logs": "query_logs",
}

_PATH_TOKEN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


def _resolve_path(doc: Any, path: str) -> tuple[bool, Any]:
    """Resolve `a.b[0].c` over parsed JSON. Returns (found, value)."""
    current = doc
    for name, index in _PATH_TOKEN.findall(path):
        if name:
            if not isinstance(current, dict) or name not in current:
                return False, None
            current = current[name]
        else:
            position = int(index)
            if not isinstance(current, list) or position >= len(current):
                return False, None
            current = current[position]
    return True, current


def _parse(text: Optional[str]) -> Any:
    try:
        return json.loads(text or "")
    except (TypeError, ValueError):
        return None


def verify_evidence(evidence: dict[str, Any],
                    tool_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Mechanically verify one evidence claim against this run's tool results."""
    source = str(evidence.get("source") or "")
    declared = evidence.get("value")
    path = evidence.get("path")
    tool = _SOURCE_TOOL.get(source)
    if tool is None:
        return {"status": UNVERIFIABLE,
                "reason": f"unknown evidence source {source!r}"}
    relevant = [r for r in tool_results
                if r.get("kind") == "realtime" and r.get("tool") == tool]
    # Resource attribution: when the claim names a resource, only that resource's
    # tool result may verify it (otherwise Pod B could verify a claim about Pod A).
    uid = evidence.get("resource_uid")
    if uid:
        relevant = [r for r in relevant if (r.get("args") or {}).get("uid") == uid]
        if not relevant:
            return {"status": UNVERIFIABLE,
                    "reason": f"no {tool} result for resource_uid {uid!r} in this run"}
    if not relevant:
        return {"status": UNVERIFIABLE, "reason": f"no {tool} result in this run"}

    if path:
        mismatch: Optional[dict[str, Any]] = None
        for result in relevant:
            doc = _parse(result.get("output"))
            if doc is None:
                continue
            found, actual = _resolve_path(doc, str(path))
            if not found:
                continue
            if declared is not None and str(actual) != str(declared):
                # Keep looking: another result may hold the declared value.
                mismatch = {"expected": declared, "actual": actual, "path": path}
                continue
            return {"status": VERIFIED, "path": path, "actual": actual}
        if mismatch:
            return {"status": MISMATCH, **mismatch}
        return {"status": UNVERIFIABLE,
                "reason": f"path {path!r} not found in this run's tool results"}

    if declared is None:
        return {"status": UNVERIFIABLE, "reason": "evidence has neither path nor value"}
    needle = str(declared)
    for result in relevant:
        if needle and needle in (result.get("output") or ""):
            return {"status": VERIFIED, "matched": needle}
    return {"status": UNVERIFIABLE,
            "reason": "declared value not present in this run's tool results"}


def validate_submission(result: dict[str, Any], tool_results: list[dict[str, Any]],
                        ) -> tuple[bool, list[str], list[dict[str, Any]]]:
    """Final gate. Returns (ok, problems, evidence verification report)."""
    code_raw = result.get("root_cause_code")
    code = code_raw.strip() if isinstance(code_raw, str) else (code_raw or None)
    text_raw = result.get("root_cause")
    text = text_raw.strip() if isinstance(text_raw, str) else (text_raw or None)
    explicit = bool(code or text)
    insufficient = bool(result.get("insufficient_evidence"))

    problems: list[str] = []
    if insufficient and explicit:
        problems.append(
            "insufficient_evidence=true 时不得给出 root_cause / root_cause_code")
    if code and code not in ROOT_CAUSE_CODES:
        problems.append(f"root_cause_code {code!r} 不在版本化词表中")
    if explicit and not insufficient and not code:
        problems.append("给出明确根因时必须填写 root_cause_code（枚举值）")

    evidence = result.get("evidence") or []
    verification = [{"index": index, **verify_evidence(item, tool_results)}
                    for index, item in enumerate(evidence) if isinstance(item, dict)]
    if explicit and not insufficient:
        if not any(item["status"] == VERIFIED for item in verification):
            problems.append(
                "明确根因缺少至少一条可验证的实时证据（verified real-time evidence）")

    return (not problems, problems, verification)
