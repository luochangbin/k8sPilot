"""Whitelisted, byte-offset paginated reader for a diagnosis JSONL trace.

The public projection exposes only ``id/seq/timestamp/kind/title/status/
duration_ms/failure_layer`` (design §3.3). Tool arguments, raw errors, prompts,
logs and full outputs are never returned; unknown kinds and malformed lines
advance the offset (with ``gap=true``) instead of leaking content.
"""

import json
import os
from typing import Any, Optional

MAX_LINE_BYTES = 64 * 1024
MAX_LINES_PER_PAGE = 1000
KIND_DIAGNOSIS_STARTED = "diagnosis_started"
KIND_DIAGNOSIS_COMPLETED = "diagnosis_completed"
KIND_DIAGNOSIS_FAILED = "diagnosis_failed"
KIND_TOOL_COMPLETED = "tool_completed"
KIND_LLM_CALL = "llm_call"
KIND_RESULT_DRAFTED = "result_drafted"

_TOOL_TITLES = {
    "inspect": "检查资源状态",
    "relations": "分析关联资源",
    "events": "查询事件",
    "logs": "查询日志",
    "query_metrics": "查询指标",
    "query_logs": "查询日志（Loki）",
    "search_knowledge": "检索知识库",
    "search_incidents": "检索历史案例",
}


class TimelineRangeError(ValueError):
    """after/limit outside the allowed range."""


class MalformedEventError(ValueError):
    """Event shape is unusable (e.g. attributes is not an object)."""


def project_event(event: dict[str, Any], seq: int, diagnosis_id: str) -> Optional[dict[str, Any]]:
    """Map one raw trace event to the public whitelist (None = skip).

    Raises MalformedEventError for structurally broken records so the caller can
    skip them and report a gap instead of failing the request.
    """
    kind = event.get("kind")
    attrs = event.get("attributes")
    if attrs is None:
        attrs = {}
    elif not isinstance(attrs, dict):
        raise MalformedEventError("attributes must be an object")
    item: dict[str, Any] = {
        "id": f"{diagnosis_id}:{seq}",
        "seq": seq,
        "timestamp": event.get("timestamp"),
        "failure_layer": event.get("failure_layer"),
        "duration_ms": attrs.get("duration_ms"),
        "title": "",
        "kind": "",
        "status": "completed",
    }
    if kind == "diagnosis_root":
        status = attrs.get("status")
        if status == "started":
            item["kind"] = KIND_DIAGNOSIS_STARTED
            item["status"] = "running"
            item["title"] = "诊断开始"
        elif status in ("completed", "failed"):
            item["kind"] = (KIND_DIAGNOSIS_COMPLETED if status == "completed"
                            else KIND_DIAGNOSIS_FAILED)
            item["status"] = status
            item["title"] = "诊断完成" if status == "completed" else "诊断失败"
        else:
            return None
    elif kind == "tool_call":
        tool = str(attrs.get("tool") or event.get("name", "").removeprefix("tool."))
        item["kind"] = KIND_TOOL_COMPLETED
        item["status"] = "failed" if event.get("error") else "completed"
        item["title"] = _TOOL_TITLES.get(tool, tool or "工具调用")
    elif kind == "llm_call":
        item["kind"] = KIND_LLM_CALL
        item["status"] = "failed" if event.get("error") else "completed"
        item["title"] = "模型调用"
    elif kind == "llm_final":
        # Not a terminal state (design §3.3).
        item["kind"] = KIND_RESULT_DRAFTED
        item["status"] = "completed" if attrs.get("schema_valid", True) else "failed"
        item["title"] = "生成结论"
    else:
        return None
    return item


def read_timeline(path: str, *, diagnosis_id: str, after: int, limit: int) -> dict[str, Any]:
    """Read a page of the trace file. The tail half-line is never consumed."""
    if after < 0 or limit < 1 or limit > 200:
        raise TimelineRangeError("invalid after/limit")
    try:
        size = os.path.getsize(path)
    except OSError:
        raise FileNotFoundError(path) from None
    if after > size:
        raise TimelineRangeError("after is beyond the end of the trace")

    items: list[dict[str, Any]] = []
    offset = after
    lines = 0
    gap = False
    with open(path, "rb") as handle:
        handle.seek(after)
        while lines < MAX_LINES_PER_PAGE and len(items) < limit:
            line_start = handle.tell()
            raw = handle.readline()
            if not raw or not raw.endswith(b"\n"):
                break  # EOF or incomplete tail line: do not consume it
            lines += 1
            offset = handle.tell()
            if len(raw) > MAX_LINE_BYTES:
                gap = True
                continue
            try:
                event = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                gap = True
                continue
            if not isinstance(event, dict):
                gap = True
                continue
            try:
                item = project_event(event, line_start, diagnosis_id)
            except MalformedEventError:
                gap = True
                continue
            if item is None:
                continue
            items.append(item)

    has_more = offset < size
    return {
        "items": items,
        "next_after": offset,
        "has_more": has_more,
        "available": "available",
        "gap": gap,
    }
