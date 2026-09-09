"""Lightweight in-process trace recorder for evaluation (design §23.6 / §31).

Spans are appended as JSONL, one file per diagnosis_id, under TRACE_DIR.
No OTLP/Collector dependency in Phase 2: exports are correlated by
diagnosis_id (plus eval_run_id/case_id when running under the Eval Runner).
"""

import json
import os
import threading
import time
import uuid
from typing import Any, Optional

# Failure layers (design §31).
FIXTURE = "fixture"
AGENT_PLANNING = "agent_planning"
LLM_TRANSPORT = "llm_transport"
TOOL_ARGUMENTS = "tool_arguments"
CONNECTOR = "connector"
KUBERNETES_API = "kubernetes_api"
RESULT_SCHEMA = "result_schema"
SCORING = "scoring"


def _now_ms() -> float:
    return time.time() * 1000.0


class TraceRecorder:
    """Thread-safe appender of JSONL trace events keyed by diagnosis_id."""

    def __init__(self, trace_dir: str) -> None:
        os.makedirs(trace_dir, exist_ok=True)
        self._dir = trace_dir
        self._lock = threading.Lock()

    def append(self, diagnosis_id: str, event: dict[str, Any]) -> None:
        path = os.path.join(self._dir, f"{diagnosis_id}.jsonl")
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")


class DiagnosisTrace:
    """Root span for one diagnosis; emits child span events.

    We record observable execution behavior and structured decision outputs,
    not hidden chain-of-thought, full prompts, secrets or raw logs (§31).
    """

    def __init__(self, recorder: TraceRecorder, diagnosis_id: str,
                 eval_ctx: Optional[dict[str, Any]] = None) -> None:
        self._recorder = recorder
        self._diagnosis_id = diagnosis_id
        self._eval_ctx = eval_ctx or {}
        self._trace_id = uuid.uuid4().hex
        self._seq = 0
        self._start_ms = _now_ms()
        self.emit("diagnosis", "diagnosis_root", {"status": "started", "trace_id": self._trace_id})

    def _span_id(self) -> str:
        self._seq += 1
        return f"{self._seq:04d}"

    def emit(self, name: str, kind: str, attributes: Optional[dict[str, Any]] = None,
             error: Optional[str] = None, failure_layer: Optional[str] = None) -> None:
        event: dict[str, Any] = {
            "timestamp": _now_ms(),
            "diagnosis_id": self._diagnosis_id,
            "trace_id": self._trace_id,
            "span_id": self._span_id(),
            "name": name,
            "kind": kind,
            **self._eval_ctx,
        }
        if attributes:
            event["attributes"] = attributes
        if error:
            event["error"] = error
        if failure_layer:
            event["failure_layer"] = failure_layer
        self._recorder.append(self._diagnosis_id, event)

    def tool_call(self, name: str, args: dict[str, Any], *, duration_ms: float,
                  output: Optional[str] = None, error: Optional[str] = None,
                  failure_layer: Optional[str] = None, truncated: Optional[bool] = None) -> None:
        attrs: dict[str, Any] = {
            "tool": name,
            "args_summary": json.dumps(args, ensure_ascii=False)[:200],
            "duration_ms": round(duration_ms, 1),
        }
        if output is not None:
            attrs["result_size"] = len(output)
        if truncated is not None:
            attrs["truncated"] = truncated
        self.emit(f"tool.{name}", "tool_call", attrs, error=error, failure_layer=failure_layer)

    def llm_call(self, *, duration_ms: float, retries: int,
                 finish_reason: Optional[str] = None,
                 prompt_tokens: Optional[int] = None,
                 completion_tokens: Optional[int] = None,
                 error: Optional[str] = None) -> None:
        attrs: dict[str, Any] = {"duration_ms": round(duration_ms, 1), "retries": retries}
        if finish_reason is not None:
            attrs["finish_reason"] = finish_reason
        if prompt_tokens is not None:
            attrs["prompt_tokens"] = prompt_tokens
        if completion_tokens is not None:
            attrs["completion_tokens"] = completion_tokens
        layer = LLM_TRANSPORT if error else None
        self.emit("llm.call", "llm_call", attrs, error=error, failure_layer=layer)

    def llm_final(self, *, root_cause_code: Optional[str], schema_valid: bool,
                  confidence: Optional[str] = None) -> None:
        attrs: dict[str, Any] = {"schema_valid": schema_valid}
        if root_cause_code is not None:
            attrs["root_cause_code"] = root_cause_code
        if confidence is not None:
            attrs["confidence"] = confidence
        layer = None if schema_valid else RESULT_SCHEMA
        self.emit("llm.final", "llm_final", attrs, failure_layer=layer)

    def finish(self, status: str, error: Optional[str] = None,
               failure_layer: Optional[str] = None) -> None:
        attrs = {"status": status, "duration_ms": round(_now_ms() - self._start_ms, 1)}
        self.emit("diagnosis", "diagnosis_root", attrs, error=error, failure_layer=failure_layer)


def build_eval_ctx(*, eval_run_id: Optional[str] = None, case_id: Optional[str] = None,
                   case_version: Optional[str] = None, attempt_index: Optional[int] = None) -> dict[str, Any]:
    ctx: dict[str, Any] = {}
    if eval_run_id:
        ctx["eval_run_id"] = eval_run_id
    if case_id:
        ctx["case_id"] = case_id
    if case_version:
        ctx["case_version"] = case_version
    if attempt_index is not None:
        ctx["attempt_index"] = attempt_index
    return ctx
