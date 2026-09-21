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


def _compare(actual: Any, operator: str, expected: Any) -> bool:
    """The single place where operator semantics are implemented.

    Must stay identical to the scorer's `_matches_required` for equals/contains,
    otherwise the runtime gate and the eval verdict would disagree.
    """
    if operator == "contains":
        return actual is not None and str(expected) in str(actual)
    return str(actual) == str(expected)


def _iter_leaf_values(doc: Any) -> list[Any]:
    """All scalar leaves of a parsed JSON document (structure-aware, so we never
    substring-match against the serialized JSON itself)."""
    if isinstance(doc, dict):
        out: list[Any] = []
        for value in doc.values():
            out.extend(_iter_leaf_values(value))
        return out
    if isinstance(doc, list):
        out = []
        for value in doc:
            out.extend(_iter_leaf_values(value))
        return out
    return [doc]


def verify_evidence(evidence: dict[str, Any],
                    tool_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Mechanically verify one evidence claim against this run's tool results."""
    source = str(evidence.get("source") or "")
    declared = evidence.get("value")
    path = evidence.get("path")
    operator = str(evidence.get("operator") or "equals")
    if operator not in ("equals", "contains"):
        return {"status": UNVERIFIABLE, "reason": f"unsupported operator {operator!r}"}
    tool = _SOURCE_TOOL.get(source)
    if tool is None:
        return {"status": UNVERIFIABLE,
                "reason": f"unknown evidence source {source!r}"}

    # Provenance first: an explicit tool_call_id pins the claim to exactly one
    # result of exactly one tool.
    call_id = evidence.get("tool_call_id")
    if call_id:
        pinned = [r for r in tool_results
                  if r.get("tool_call_id") == call_id and r.get("kind") == "realtime"]
        if not pinned:
            return {"status": UNVERIFIABLE,
                    "reason": f"no real-time tool result with tool_call_id {call_id!r}"}
        if any(r.get("tool") != tool for r in pinned):
            return {"status": UNVERIFIABLE,
                    "reason": f"tool_call_id {call_id!r} did not come from {tool}"}
        relevant = pinned
    else:
        relevant = [r for r in tool_results
                    if r.get("kind") == "realtime" and r.get("tool") == tool]
    # Resource attribution is an AND, not an OR: a pinned tool call must still
    # point at the resource the claim names (tool_call_id + source + resource +
    # fact), so a claim cannot merge one call's provenance with another
    # resource's uid.
    uid = evidence.get("resource_uid")
    if uid:
        relevant = [r for r in relevant if (r.get("args") or {}).get("uid") == uid]
        if not relevant:
            return {"status": UNVERIFIABLE,
                    "reason": f"no {tool} result for resource_uid {uid!r} in this run"}
    if not relevant:
        return {"status": UNVERIFIABLE,
                "reason": f"no {tool} result in this run (source {source!r})"}

    if declared is None:
        return {"status": UNVERIFIABLE, "reason": "evidence has no value"}

    if path:
        mismatch: Optional[dict[str, Any]] = None
        for result in relevant:
            doc = _parse(result.get("output"))
            if doc is None:
                continue
            found, actual = _resolve_path(doc, str(path))
            if not found:
                continue
            if _compare(actual, operator, declared):
                return {"status": VERIFIED, "path": path, "actual": actual,
                        "operator": operator}
            mismatch = {"expected": declared, "actual": actual, "path": path,
                        "operator": operator}
        if mismatch:
            return {"status": MISMATCH, **mismatch}
        return {"status": UNVERIFIABLE,
                "reason": f"path {path!r} not found in this run's tool results"}

    # No path (events/logs): compare against the real business fields, never
    # against the serialized JSON blob.
    for result in relevant:
        doc = _parse(result.get("output"))
        if doc is None:
            # Raw text (e.g. container logs): line-accurate comparison.
            text = str(result.get("output") or "")
            if _compare(text, operator, declared) or any(
                    _compare(line, operator, declared) for line in text.splitlines()):
                return {"status": VERIFIED, "matched": declared, "operator": operator}
            continue
        if any(_compare(leaf, operator, declared) for leaf in _iter_leaf_values(doc)):
            return {"status": VERIFIED, "matched": declared, "operator": operator}
    return {"status": UNVERIFIABLE,
            "reason": f"no field satisfies {operator} {declared!r} in this run's results"}


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
        if not verification:
            problems.append("明确根因必须提供至少一条实时证据")
        # Every evidence item published with the conclusion must be verified:
        # "one real + N unverified" would put unproven claims in front of users.
        unverified = [item for item in verification if item["status"] != VERIFIED]
        if unverified:
            problems.append(
                "存在未通过校验的实时证据（每条证据都必须 VERIFIED）："
                + ", ".join(f"#{item['index']}={item['status']}" for item in unverified))
    elif insufficient:
        # Abstention is a *diagnostic* outcome, not "I did not look": it requires
        # at least one successful real-time tool call and an explicit statement of
        # what evidence is missing. Zero-investigation abstention is rejected.
        realtime_calls = sum(1 for item in tool_results if item.get("kind") == "realtime")
        if realtime_calls == 0:
            problems.append(
                "弃权前至少需要一次成功的实时只读工具调用（不允许零调查直接弃权）")
        if not (result.get("missing_evidence") or []):
            problems.append("弃权必须在 missing_evidence 中说明还缺什么证据")

    return (not problems, problems, verification)
