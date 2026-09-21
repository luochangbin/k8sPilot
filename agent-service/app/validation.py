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


# Path-based evidence is also restricted: a path may only address the source's
# business facts, never transport metadata (target/namespace/pod/container/
# metric name/capability/window_*). The first path segment must be an allowed
# root, and for events the leaf must be one of the documented fact fields.
_PATH_ROOTS: dict[str, tuple[str, ...]] = {
    "kubernetes.status": ("desired_state", "actual_state", "conditions", "anomalies"),
    "kubernetes.events": ("events",),
    "kubernetes.logs": ("data",),
    "loki.logs": ("evidence",),
    "prometheus.metrics": ("summary", "series"),
}
_PATH_LEAF_FIELDS: dict[str, tuple[str, ...]] = {
    "kubernetes.events": ("reason", "message", "type"),
}


def _path_segments(path: str) -> list[str]:
    """Top-level keys of a path, ignoring list indexes.

    `_PATH_TOKEN` yields a match per key AND per index; index matches carry an
    empty name and must be dropped, otherwise `events[0].reason` would look like
    ['events', '', 'reason'] and be rejected.
    """
    return [name for name, _index in _PATH_TOKEN.findall(path) if name]


def _path_allowed(source: str, path: str) -> bool:
    roots = _PATH_ROOTS.get(source)
    if roots is None:
        return False
    segments = _path_segments(path)
    if not segments or segments[0] not in roots:
        return False
    leaves = _PATH_LEAF_FIELDS.get(source)
    if leaves and len(segments) > 1 and segments[1] not in leaves:
        return False
    return True


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


# Path-less evidence may only match real business fields of the source. Without
# this allowlist a claim could "verify" against metadata (namespace/pod/container
# names, metric names, ...) that merely happens to contain the string.
_BUSINESS_FIELDS: dict[str, tuple[str, ...]] = {
    "events": ("reason", "message", "type"),
}
_RAW_TEXT_SOURCES = {"kubernetes.logs"}          # container logs: line-accurate
_METRIC_VALUE_SOURCES = {"prometheus.metrics"}   # summary values + series points
_PATH_REQUIRED_SOURCES = {"kubernetes.status"}   # facts must be addressed by path


def _candidate_values(tool: str, tool_name: str, doc: Any) -> list[Any]:
    """Business values a path-less claim may match, per evidence source."""
    if not isinstance(doc, dict):
        return []
    if tool_name == "events":
        out: list[Any] = []
        for event in (doc.get("events") or []):
            if isinstance(event, dict):
                out.extend(event[field] for field in _BUSINESS_FIELDS["events"]
                           if event.get(field) is not None)
        return out
    if tool_name == "query_logs":
        return [item for item in (doc.get("evidence") or []) if item is not None]
    if tool_name == "query_metrics":
        out = [value for value in (doc.get("summary") or {}).values()
               if value is not None]
        for series in (doc.get("series") or []):
            if isinstance(series, dict):
                for point in (series.get("points") or []):
                    if isinstance(point, dict) and point.get("value") is not None:
                        out.append(point["value"])
        return out
    return []


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
        # Single candidate is unambiguous; several candidates require the claim to
        # say where it came from (tool_call_id or resource_uid), otherwise the
        # right value could be found in the wrong resource's result.
        if len(relevant) > 1 and not evidence.get("resource_uid"):
            return {"status": UNVERIFIABLE,
                    "reason": (f"ambiguous provenance: {len(relevant)} {tool} results in "
                               "this run; declare tool_call_id or resource_uid")}
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
        if not _path_allowed(source, str(path)):
            return {"status": UNVERIFIABLE,
                    "reason": (f"path {path!r} is outside the {source} field allowlist "
                               "(metadata is not evidence)")}
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

    # No path: compare only against the source's business fields (see above).
    if source in _PATH_REQUIRED_SOURCES:
        return {"status": UNVERIFIABLE,
                "reason": f"{source} evidence must carry a path to the fact it cites"}
    for result in relevant:
        tool_name = str(result.get("tool") or "")
        doc = _parse(result.get("output"))
        if doc is None:
            if source in _RAW_TEXT_SOURCES:
                # Container logs are raw text: compare per line, not the whole blob.
                text = str(result.get("output") or "")
                if _compare(text, operator, declared) or any(
                        _compare(line, operator, declared) for line in text.splitlines()):
                    return {"status": VERIFIED, "matched": declared, "operator": operator}
            continue
        if source in _RAW_TEXT_SOURCES:
            data = doc.get("data")
            if isinstance(data, str) and (
                    _compare(data, operator, declared)
                    or any(_compare(line, operator, declared) for line in data.splitlines())):
                return {"status": VERIFIED, "matched": declared, "operator": operator}
            continue
        if any(_compare(value, operator, declared)
               for value in _candidate_values(tool, tool_name, doc)):
            return {"status": VERIFIED, "matched": declared, "operator": operator}
    return {"status": UNVERIFIABLE,
            "reason": f"no business field satisfies {operator} {declared!r} in this run's results"}


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
    if not explicit and not insufficient:
        # Third "shape" is illegal: a submission must either state a root cause
        # or formally abstain. "Neither answered nor abstained" is not a result.
        problems.append(
            "最终结果必须明确给出根因（root_cause_code/root_cause），"
            "或设置 insufficient_evidence=true 正式弃权；两者皆无的提交不被接受")
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
