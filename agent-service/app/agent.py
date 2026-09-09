"""The LLM-driven diagnosis loop.

Flow (bounded, on-demand investigation):
    inspect(target) -> relations(target) -> events(target) -> logs(...)
    hypothesis verification, then submit_result() for the structured output.

Failure boundaries (design §22.8):
    - UID mismatch                -> session failed, resource rebuilt message
    - connector unreachable       -> session failed, locatable error
    - LLM timeout/invalid output  -> limited retries, then failed (steps kept)
    - insufficient evidence       -> completed, empty root_cause + missing_evidence
"""

import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger("k8spilot.agent")

from .config import Config
from .connector import ConnectorClient, ConnectorError, ToolError
from .models import DIAGNOSABLE_KINDS, DiagnosisRequest, DiagnosisResult, Evidence, ResourceRef
from .prompts import SYSTEM_PROMPT, user_message
from .root_causes import is_valid_root_cause_code
from .store import SessionStore
from .tools import execute_tool, tool_definitions
from .trace import (
    AGENT_PLANNING,
    CONNECTOR,
    KUBERNETES_API,
    LLM_TRANSPORT,
    TOOL_ARGUMENTS,
    DiagnosisTrace,
    TraceRecorder,
    build_eval_ctx,
)


class LLMError(Exception):
    """Raised when the LLM endpoint fails after retries."""


class UIDMismatchError(Exception):
    """Raised when the target resource was recreated (UID differs)."""


class OpenAILLM:
    """OpenAI-compatible chat completions client with bounded retries."""

    def __init__(self, cfg: Config) -> None:
        from openai import OpenAI

        self._client = OpenAI(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key or "sk-not-needed")
        self._model = cfg.llm_model
        self._timeout = cfg.llm_timeout
        self._max_retries = cfg.llm_max_retries
        self._max_tokens = cfg.llm_max_tokens

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: Any):
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    timeout=self._timeout,
                    max_tokens=self._max_tokens,
                )
            except Exception as exc:  # noqa: BLE001 - surface any provider failure
                last_exc = exc
                if attempt == self._max_retries:
                    break
        raise LLMError(f"LLM request failed after {self._max_retries + 1} attempts: {last_exc}")


_STEP_LABELS = {
    "inspect": "获取 {kind} 状态",
    "relations": "分析关联资源",
    "events": "查询事件",
    "logs": "查询日志",
}


class Agent:
    def __init__(self, cfg: Config, connector: ConnectorClient, llm: Any,
                 knowledge: Any = None) -> None:
        self.cfg = cfg
        self.connector = connector
        self.llm = llm
        self.knowledge = knowledge  # Optional KnowledgeService (Phase 4)

    def run(self, req: DiagnosisRequest, store: SessionStore, diagnosis_id: str) -> None:
        store.update(diagnosis_id, status="investigating")
        steps: list[str] = []
        retrieved: dict[str, Any] = {}
        trace = self._new_trace(req, diagnosis_id)
        try:
            self._run_inner(req, store, diagnosis_id, steps, trace, retrieved)
        except UIDMismatchError as exc:
            self._fail(store, diagnosis_id, str(exc), steps, trace, KUBERNETES_API)
        except ConnectorError as exc:
            self._fail(store, diagnosis_id, str(exc), steps, trace, CONNECTOR)
        except LLMError as exc:
            self._fail(store, diagnosis_id, str(exc), steps, trace, LLM_TRANSPORT)
        except Exception as exc:  # noqa: BLE001 - fail loudly, never crash silently
            self._fail(store, diagnosis_id, f"unexpected error: {exc}", steps, trace, AGENT_PLANNING)
        else:
            d = store.get(diagnosis_id)
            if trace is not None:
                # Normal return: either completed, or _run_inner failed the
                # session after exhausting the tool-call budget (planning).
                layer = AGENT_PLANNING if (d and d.status == "failed") else None
                trace.finish(d.status if d else "unknown",
                             error=d.error if d else None, failure_layer=layer)

    def _new_trace(self, req: DiagnosisRequest, diagnosis_id: str) -> Optional[DiagnosisTrace]:
        if not self.cfg.trace_dir:
            return None
        recorder = TraceRecorder(self.cfg.trace_dir)
        ctx = build_eval_ctx(
            eval_run_id=req.eval_run_id,
            case_id=req.case_id,
            case_version=req.case_version,
            attempt_index=req.attempt_index,
        )
        return DiagnosisTrace(recorder, diagnosis_id, ctx)

    def _fetch_capabilities(self) -> dict[str, Any]:
        """Query the connector for optional data-source capabilities. A
        connector that is unreachable can still serve nothing anyway; fall back
        to Kubernetes-only and let the first tool call surface the failure."""
        try:
            caps = self.connector.capabilities()
            logger.info("connector capabilities: %s", caps)
            return caps
        except ConnectorError as exc:
            logger.warning("capabilities check failed (%s); continuing Kubernetes-only", exc)
            return {}

    def _retrieval_flags(self, req: DiagnosisRequest) -> dict[str, bool]:
        """Effective retrieval gating: request overrides; default on when the
        knowledge module is configured. Retrieval tools are simply not exposed
        when off (ablation groups A/B/C)."""
        if self.knowledge is None:
            return {"knowledge": False, "incidents": False}
        return {
            "knowledge": req.enable_knowledge is not False,
            "incidents": req.enable_incidents is not False,
        }

    def _search_knowledge(self, args: dict[str, Any]) -> str:
        filters = {}
        for k in ("source_types", "product", "versions", "environment"):
            if args.get(k):
                filters[k] = args[k]
        refs = self.knowledge.search_knowledge(args.get("query", ""),
                                               top_k=args.get("top_k") or 5,
                                               filters=filters)
        return json.dumps([self._ref_to_dict(r) for r in refs], ensure_ascii=False)

    def _search_incidents(self, args: dict[str, Any]) -> str:
        symptoms = args.get("symptoms") or []
        cands = args.get("root_cause_candidates") or []
        query = " ".join(symptoms + cands)
        filters = {}
        if args.get("resource_kind"):
            filters["resource_kind"] = args["resource_kind"]
        if cands:
            filters["root_cause_candidates"] = cands
        if args.get("product_version"):
            filters["product_version"] = args["product_version"]
        cases = self.knowledge.search_incidents(query, top_k=args.get("top_k") or 5,
                                                filters=filters)
        return json.dumps([self._case_to_dict(c) for c in cases], ensure_ascii=False)

    @staticmethod
    def _ref_to_dict(r) -> dict[str, Any]:
        return {"retrieval_id": r.retrieval_id, "type": r.type, "score": r.score,
                "content": r.content, "citation": r.citation}

    @staticmethod
    def _case_to_dict(c) -> dict[str, Any]:
        return {"retrieval_id": c.retrieval_id, "type": c.type, "score": c.score,
                "incident_id": c.incident_id, "product": c.product,
                "product_version": c.product_version, "resource_kind": c.resource_kind,
                "symptoms": c.symptoms, "root_cause_code": c.root_cause_code,
                "evidence_summary": c.evidence_summary,
                "remediation_summary": c.remediation_summary,
                "verification": c.verification}

    @staticmethod
    def _fail(store: SessionStore, diagnosis_id: str, message: str,
              steps: list[str], trace: Optional[DiagnosisTrace], layer: str) -> None:
        store.update(diagnosis_id, status="failed", error=message,
                     result=DiagnosisResult(investigation_steps=steps))
        if trace is not None:
            trace.finish("failed", error=message, failure_layer=layer)

    @staticmethod
    def _tool_failure_layer(exc: Exception) -> str:
        if isinstance(exc, ToolError):
            if exc.code in ("not_found", "forbidden"):
                return KUBERNETES_API
            if exc.code in ("invalid_request", "not_supported"):
                return TOOL_ARGUMENTS
            return CONNECTOR
        return CONNECTOR

    def _run_inner(self, req: DiagnosisRequest, store: SessionStore, diagnosis_id: str,
                   steps: list[str], trace: Optional[DiagnosisTrace],
                   retrieved: Optional[dict[str, Any]] = None) -> None:
        if req.resource.kind not in DIAGNOSABLE_KINDS:
            raise Exception(f"Phase 3 仅支持诊断 {', '.join(DIAGNOSABLE_KINDS)}，收到 {req.resource.kind}")
        retrieved = retrieved if retrieved is not None else {}

        capabilities = self._fetch_capabilities()
        retrieval = self._retrieval_flags(req)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message(req.resource)},
        ]
        tools = tool_definitions(capabilities, retrieval)

        for iteration in range(self.cfg.max_tool_calls):
            tool_choice: Any = "auto"
            if iteration == self.cfg.max_tool_calls - 1:
                # Force a structured final answer on the last allowed call.
                tool_choice = {"type": "function", "function": {"name": "submit_result"}}

            t0 = time.time()
            try:
                resp = self.llm.chat(messages, tools, tool_choice)
            except LLMError:
                if trace is not None:
                    trace.llm_call(duration_ms=(time.time() - t0) * 1000.0,
                                   retries=self.cfg.llm_max_retries, error="LLM request failed")
                raise
            if trace is not None:
                usage = getattr(resp, "usage", None)
                pt = getattr(usage, "prompt_tokens", None) if usage else None
                ct = getattr(usage, "completion_tokens", None) if usage else None
                fr = getattr(resp.choices[0], "finish_reason", None) if resp.choices else None
                trace.llm_call(duration_ms=(time.time() - t0) * 1000.0, retries=0,
                               finish_reason=fr, prompt_tokens=pt, completion_tokens=ct)
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None) or []

            # Always persist the assistant message so any following "tool"
            # messages reference a preceding message that carries tool_calls
            # (required by OpenAI-compatible APIs, strictly enforced by some).
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ]
            # Reasoning models (e.g. DeepSeek thinking mode) require their
            # reasoning_content to be passed back verbatim on the next turn.
            reasoning = getattr(msg, "reasoning_content", None)
            if reasoning is None:
                extra = getattr(msg, "model_extra", None) or {}
                reasoning = extra.get("reasoning_content")
            if reasoning is not None:
                assistant_msg["reasoning_content"] = reasoning
            messages.append(assistant_msg)

            if not tool_calls:
                content = (msg.content or "").strip()
                if content:
                    messages.append({
                        "role": "user",
                        "content": "请直接调用 submit_result 提交最终结构化诊断，不要用纯文本回答。",
                    })
                continue

            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                if name == "submit_result":
                    logger.info("diagnosis %s: submit_result raw args=%s",
                                diagnosis_id, json.dumps(args, ensure_ascii=False))
                    if not self._submit_valid(args):
                        # Reasoning models sometimes emit submit_result with empty
                        # arguments. Reply as a tool result for this call (the
                        # API requires assistant tool_calls to be answered by tool
                        # messages), steering the model to retry with content.
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": "submit_result 参数为空或缺少关键字段。请基于已收集的证据，完整填写 symptom、root_cause_code/root_cause、evidence、confidence、recommendations 后重新调用 submit_result。若证据确实不足，请正确设置 insufficient_evidence=true 并说明 missing_evidence。",
                        })
                        continue
                    result = self._parse_result(args, steps, retrieved)
                    if trace is not None:
                        schema_valid = (
                            result.root_cause_code is None
                            or is_valid_root_cause_code(result.root_cause_code)
                        )
                        trace.llm_final(root_cause_code=result.root_cause_code,
                                        schema_valid=schema_valid, confidence=result.confidence)
                    store.update(diagnosis_id, status="completed", result=result)
                    return

                args = self._inject_target_uid(name, args, req.resource)
                t0 = time.time()
                tool_err: Optional[str] = None
                fail_layer: Optional[str] = None
                try:
                    if name == "search_knowledge":
                        refs = self.knowledge.search_knowledge(
                            args.get("query", ""),
                            top_k=args.get("top_k") or 5,
                            filters={k: args[k] for k in
                                     ("source_types", "product", "versions", "environment")
                                     if args.get(k)})
                        for r in refs:
                            retrieved[r.retrieval_id] = self._ref_to_dict(r)
                        output = json.dumps([self._ref_to_dict(r) for r in refs],
                                            ensure_ascii=False)
                    elif name == "search_incidents":
                        symptoms = args.get("symptoms") or []
                        cands = args.get("root_cause_candidates") or []
                        filters = {}
                        if args.get("resource_kind"):
                            filters["resource_kind"] = args["resource_kind"]
                        if cands:
                            filters["root_cause_candidates"] = cands
                        if args.get("product_version"):
                            filters["product_version"] = args["product_version"]
                        cases = self.knowledge.search_incidents(
                            " ".join(symptoms + cands), top_k=args.get("top_k") or 5,
                            filters=filters)
                        for c in cases:
                            retrieved[c.retrieval_id] = self._case_to_dict(c)
                        output = json.dumps([self._case_to_dict(c) for c in cases],
                                            ensure_ascii=False)
                    else:
                        output = execute_tool(self.connector, name, args)
                    logger.info(
                        "diagnosis %s: tool %s(%s) -> %.300s",
                        diagnosis_id, name, json.dumps(args, ensure_ascii=False), output,
                    )
                except ToolError as exc:
                    logger.error(
                        "diagnosis %s: tool %s(%s) FAILED connector error [%s]: %s",
                        diagnosis_id, name, json.dumps(args, ensure_ascii=False), exc.code, exc,
                    )
                    tool_err = str(exc)
                    fail_layer = self._tool_failure_layer(exc)
                    output = json.dumps({"error": f"connector tool error [{exc.code}]: {exc}"})
                if trace is not None:
                    truncated = None
                    if name == "logs" and output.startswith("{"):
                        try:
                            truncated = bool(json.loads(output).get("truncated"))
                        except json.JSONDecodeError:
                            pass
                    trace.tool_call(
                        name, args,
                        duration_ms=(time.time() - t0) * 1000.0,
                        output=output, error=tool_err,
                        failure_layer=fail_layer, truncated=truncated,
                    )

                if name == "inspect" and self._uid_mismatch(output):
                    raise UIDMismatchError("目标资源已重建（当前 UID 与请求不一致），拒绝本次诊断")

                steps.append(self._step_summary(name, args))
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})

        store.update(diagnosis_id, status="failed",
                     error=f"达到最大工具调用次数（{self.cfg.max_tool_calls}）仍未获得结论",
                     result=DiagnosisResult(investigation_steps=steps))

    @staticmethod
    def _inject_target_uid(name: str, args: dict[str, Any], target: ResourceRef) -> dict[str, Any]:
        """Pass the platform-provided UID into inspect calls for the target itself.

        The connector's inspect tool verifies UID to detect resource recreation.
        """
        if name != "inspect":
            return args
        if args.get("kind") != target.kind:
            return args
        if args.get("name") != target.name:
            return args
        if (args.get("namespace") or "") != (target.namespace or ""):
            return args
        return {**args, "uid": target.uid}

    @staticmethod
    def _uid_mismatch(output: str) -> bool:
        try:
            body = json.loads(output)
        except json.JSONDecodeError:
            return False
        return bool(body.get("uid_mismatch"))

    @staticmethod
    def _submit_valid(args: dict[str, Any]) -> bool:
        """A submit_result is acceptable when it carries at least symptom, a
        root cause code/text, or evidence; a completely empty dict is not."""
        if not isinstance(args, dict):
            return False
        return bool(args.get("symptom") or args.get("root_cause_code")
                    or args.get("root_cause") or args.get("evidence"))

    def _parse_result(self, args: dict[str, Any], steps: list[str],
                      retrieved: Optional[dict[str, Any]] = None) -> DiagnosisResult:
        retrieved = retrieved or {}
        evidence: list[Evidence] = []
        for e in args.get("evidence") or []:
            if not isinstance(e, dict):
                continue
            evidence.append(Evidence(
                source=str(e.get("source", "")),
                observed_at=str(e.get("observed_at")) if e.get("observed_at") else None,
                summary=str(e.get("summary", "")),
                resource_uid=str(e.get("resource_uid")) if e.get("resource_uid") else None,
                path=str(e.get("path")) if e.get("path") else None,
                operator=str(e.get("operator")) if e.get("operator") else None,
                value=str(e.get("value")) if e.get("value") is not None else None,
            ))
        root_cause_code = args.get("root_cause_code")
        if isinstance(root_cause_code, str):
            root_cause_code = root_cause_code.strip() or None
        knowledge_references = [
            {**retrieved[it["retrieval_id"]], "used_for": it.get("used_for", "")}
            for it in (args.get("knowledge_references") or [])
            if isinstance(it, dict) and it.get("retrieval_id") in retrieved
        ]
        historical_cases = [
            {**retrieved[it["retrieval_id"]], "used_for": it.get("used_for", "")}
            for it in (args.get("historical_cases") or [])
            if isinstance(it, dict) and it.get("retrieval_id") in retrieved
        ]
        return DiagnosisResult(
            symptom=str(args.get("symptom", "")),
            evidence=evidence,
            root_cause=args.get("root_cause") or None,
            root_cause_code=root_cause_code,
            confidence=args.get("confidence"),
            recommendations=[str(r) for r in (args.get("recommendations") or [])],
            missing_evidence=[str(m) for m in (args.get("missing_evidence") or [])],
            insufficient_evidence=bool(args.get("insufficient_evidence", False)),
            investigation_steps=steps,
            knowledge_references=knowledge_references,
            historical_cases=historical_cases,
        )

    @staticmethod
    def _step_summary(name: str, args: dict[str, Any]) -> str:
        label = _STEP_LABELS.get(name, name)
        if name in ("inspect", "relations"):
            label = label.format(kind=args.get("kind", "资源"))
        elif name == "logs":
            label = f"查询日志 ({args.get('name', '')})"
            if args.get("previous"):
                label += " [上一容器]"
        elif name == "events":
            label = f"查询事件 ({args.get('name', '')})"
        return label
