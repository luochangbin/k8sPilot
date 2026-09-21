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
from .llm import ExecutionContext, LLMError, OpenAILLM
from .models import DIAGNOSABLE_KINDS, DiagnosisRequest, DiagnosisResult, Evidence, ResourceRef
from .prompts import SYSTEM_PROMPT, user_message
from .root_causes import is_valid_root_cause_code
from .store import SessionStore
from .tools import apply_alert_anchor, execute_tool, tool_definitions
from .validation import validate_submission
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


BUDGET_EXHAUSTED = "budget_exhausted"

# Tool -> (kind field, namespace field, name field). None kind means the tool is
# Pod-only (logs/query_logs).
_TOOL_SCOPE: dict[str, tuple[Optional[str], str, str]] = {
    "inspect": ("kind", "namespace", "name"),
    "relations": ("kind", "namespace", "name"),
    "events": ("kind", "namespace", "name"),
    "query_metrics": ("kind", "namespace", "name"),
    "logs": (None, "namespace", "name"),
    "query_logs": (None, "namespace", "name"),
}

FINALIZATION_ONE_TOOL = (
    "Terminal-only round: exactly one tool call (submit_result) is allowed. "
    "Multiple calls are rejected as a policy violation; retry with a single "
    "submit_result built from the evidence already gathered."
)

FINALIZATION_ONLY_SUBMIT = (
    "Investigation budgets are exhausted: only submit_result is available now. "
    "Submit your conclusion from the evidence already gathered, or a valid "
    "abstention (insufficient_evidence=true + missing_evidence)."
)

SCOPE_VIOLATION = (
    "resource is outside the current diagnosis scope: {kind}/{namespace}/{name}. "
    "Only the diagnosis target and resources discovered through relations() may "
    "be queried."
)

POLICY_ONE_TOOL = (
    "Each investigation round may execute only one tool. Select the single "
    "highest-value next action based on current evidence and retry with exactly "
    "one tool call; multiple tool calls are rejected as a policy violation."
)


class UIDMismatchError(Exception):
    """Raised when the target resource was recreated (UID differs)."""


class DiagnosisCancelled(Exception):
    """The session was cancelled (eval timeout / explicit cancel). Cooperative
    stop: no further LLM call or tool execution is started."""


class BudgetExhaustedError(Exception):
    """Investigation budgets ran out and the terminal-only round produced no
    valid submit_result. This is a planning/system failure, NOT an abstention."""



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

    def effective_budgets(self, req: DiagnosisRequest) -> dict[str, int]:
        """Frozen per-session budgets: Case budgets may only *shrink* the
        service defaults, and only for eval runs (checked by the API layer)."""
        max_rounds = self.cfg.max_agent_rounds
        max_tools = self.cfg.max_tool_calls
        if req.eval_run_id:
            if req.eval_max_agent_rounds is not None:
                max_rounds = min(max_rounds, req.eval_max_agent_rounds)
            if req.eval_max_tool_calls is not None:
                max_tools = min(max_tools, req.eval_max_tool_calls)
        return {"max_agent_rounds": max(1, max_rounds), "max_tool_calls": max(1, max_tools)}

    def run(self, req: DiagnosisRequest, store: SessionStore, diagnosis_id: str,
            execution: Optional[ExecutionContext] = None) -> None:
        llm = execution.llm if execution is not None else self.llm
        store.update(diagnosis_id, status="investigating")
        steps: list[str] = []
        retrieved: dict[str, Any] = {}
        budgets = self.effective_budgets(req)
        # Observable budget/policy accounting, surfaced on the root span.
        budget: dict[str, Any] = {
            **budgets,
            "max_finalization_attempts": max(1, self.cfg.max_finalization_attempts),
            "rounds_used": 0,
            "tool_calls_used": 0,
            "multi_tool_rejected_rounds": 0,
        }
        trace = self._new_trace(req, diagnosis_id, execution)
        try:
            self._run_inner(req, store, diagnosis_id, steps, trace, retrieved, llm, execution,
                            budget)
        except UIDMismatchError as exc:
            self._fail(store, diagnosis_id, str(exc), steps, trace, KUBERNETES_API,
                       trace_attributes=dict(budget))
        except DiagnosisCancelled as exc:
            self._fail(store, diagnosis_id, str(exc), steps, trace, AGENT_PLANNING,
                       failure_reason="cancelled", trace_attributes=dict(budget))
        except BudgetExhaustedError as exc:
            self._fail(store, diagnosis_id, str(exc), steps, trace, AGENT_PLANNING,
                       failure_reason=BUDGET_EXHAUSTED, trace_attributes=dict(budget))
        except ConnectorError as exc:
            self._fail(store, diagnosis_id, str(exc), steps, trace, CONNECTOR,
                       trace_attributes=dict(budget))
        except LLMError as exc:
            self._fail(store, diagnosis_id, str(exc), steps, trace, LLM_TRANSPORT,
                       trace_attributes=dict(budget))
        except Exception as exc:  # noqa: BLE001 - fail loudly, never crash silently
            self._fail(store, diagnosis_id, f"unexpected error: {exc}", steps, trace,
                       AGENT_PLANNING, trace_attributes=dict(budget))
        else:
            d = store.get(diagnosis_id)
            if trace is not None:
                layer = AGENT_PLANNING if (d and d.status == "failed") else None
                trace.finish(d.status if d else "unknown",
                             error=d.error if d else None, failure_layer=layer,
                             attributes=dict(budget))

    def _new_trace(self, req: DiagnosisRequest, diagnosis_id: str,
                   execution: Optional[ExecutionContext] = None) -> Optional[DiagnosisTrace]:
        if not self.cfg.trace_dir:
            return None
        recorder = TraceRecorder(self.cfg.trace_dir)
        ctx = build_eval_ctx(
            eval_run_id=req.eval_run_id,
            case_id=req.case_id,
            case_version=req.case_version,
            attempt_index=req.attempt_index,
        )
        if execution is not None:
            ctx.update(execution.metadata)
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
              steps: list[str], trace: Optional[DiagnosisTrace], layer: str,
              failure_reason: Optional[str] = None,
              trace_attributes: Optional[dict[str, Any]] = None) -> None:
        store.update(diagnosis_id, status="failed", error=message,
                     result=DiagnosisResult(investigation_steps=steps),
                     failure_reason=failure_reason)
        if trace is not None:
            # Failed runs are the ones we most need to analyse: the budget
            # snapshot must be on the root span for every exit path.
            trace.finish("failed", error=message, failure_layer=layer,
                          attributes=trace_attributes)

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
                   retrieved: Optional[dict[str, Any]] = None,
                   llm: Any = None,
                   execution: Optional[ExecutionContext] = None,
                   budget: Optional[dict[str, Any]] = None,
                   tool_results: Optional[list[dict[str, Any]]] = None) -> None:
        llm = llm if llm is not None else self.llm
        if req.resource.kind not in DIAGNOSABLE_KINDS:
            raise Exception(f"Phase 3 仅支持诊断 {', '.join(DIAGNOSABLE_KINDS)}，收到 {req.resource.kind}")
        retrieved = retrieved if retrieved is not None else {}

        capabilities = self._fetch_capabilities()
        retrieval = self._retrieval_flags(req)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message(req.resource, req.alert)},
        ]
        tools = tool_definitions(capabilities, retrieval)

        if budget is None:
            budget = {**self.effective_budgets(req),
                      "max_finalization_attempts": max(1, self.cfg.max_finalization_attempts),
                      "rounds_used": 0, "tool_calls_used": 0,
                      "multi_tool_rejected_rounds": 0}
        tool_results = tool_results if tool_results is not None else []
        allowed: set[tuple[str, str, str]] = set()
        if req.resource is not None:
            allowed.add(self._scope_key(req.resource.kind, req.resource.namespace,
                                        req.resource.name))

        def _finalize_only() -> None:
            """Terminal-only round after the investigation budgets ran out.

            Only `submit_result` is exposed, so the model cannot keep
            investigating and the connector is never called again. The model may
            still submit a valid conclusion or a valid abstention from the
            evidence it already gathered; if it cannot, the session fails with
            failure_reason=budget_exhausted (a planning failure, never silently
            converted into an abstention).
            """
            finalization_tools = [d for d in tools
                                  if d["function"]["name"] == "submit_result"]
            forced: Any = {"type": "function", "function": {"name": "submit_result"}}
            attempts = max(1, int(budget.get("max_finalization_attempts", 1)))
            for _attempt in range(attempts):
                if store.is_cancelled(diagnosis_id):
                    raise DiagnosisCancelled("diagnosis cancelled")
                t0 = time.time()
                try:
                    resp = llm.chat(messages, finalization_tools, forced)
                except LLMError as exc:
                    if trace is not None:
                        trace.llm_call(duration_ms=(time.time() - t0) * 1000.0,
                                       retries=int(getattr(llm, "max_retries", 0)),
                                       error="LLM request failed")
                    raise
                if trace is not None:
                    attempts_used = int(getattr(llm, "last_attempt_count", 1) or 1)
                    usage = getattr(resp, "usage", None)
                    trace.llm_call(
                        duration_ms=(time.time() - t0) * 1000.0,
                        retries=max(0, attempts_used - 1), attempts=attempts_used,
                        finish_reason=(getattr(resp.choices[0], "finish_reason", None)
                                       if resp.choices else None),
                        prompt_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
                        completion_tokens=(getattr(usage, "completion_tokens", None)
                                           if usage else None),
                        response_model=getattr(resp, "model", None))
                msg = resp.choices[0].message
                calls = getattr(msg, "tool_calls", None) or []
                assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content}
                if calls:
                    assistant_msg["tool_calls"] = [self._tool_call_payload(tc) for tc in calls]
                messages.append(assistant_msg)

                # Terminal-only is also single-tool-only: a round that returns
                # more than one call (including two submit_result calls) is
                # rejected as a whole, with every tool_call_id answered, so a
                # strict provider never sees a dangling id.
                if len(calls) > 1:
                    for tc in calls:
                        messages.append({"role": "tool", "tool_call_id": tc.id,
                                         "content": FINALIZATION_ONE_TOOL})
                    if trace is not None:
                        trace.emit("agent.finalization_multi_tool_rejected",
                                   "agent_planning",
                                   {"tools": [tc.function.name for tc in calls]})
                    continue

                submit_tc = None
                for tc in calls:
                    if tc.function.name == "submit_result":
                        submit_tc = tc
                    else:
                        messages.append({"role": "tool", "tool_call_id": tc.id,
                                         "content": FINALIZATION_ONLY_SUBMIT})
                if submit_tc is None:
                    messages.append({
                        "role": "user",
                        "content": "调查预算已用尽。请只调用 submit_result 提交最终结论；"
                                   "若证据不足，请设置 insufficient_evidence=true 并填写 "
                                   "missing_evidence，不要继续调用调查工具。",
                    })
                    continue
                try:
                    args = json.loads(submit_tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if self._submit_valid(args):
                    parsed = self._parse_result(args, steps, retrieved)
                    ok, problems, verification = validate_submission(
                        parsed.model_dump(), tool_results)
                    if ok:
                        if trace is not None:
                            schema_valid = (
                                parsed.root_cause_code is None
                                or is_valid_root_cause_code(parsed.root_cause_code))
                            trace.llm_final(root_cause_code=parsed.root_cause_code,
                                            schema_valid=schema_valid,
                                            confidence=parsed.confidence)
                        store.update(diagnosis_id, status="completed", result=parsed)
                        return
                    messages.append({
                        "role": "tool", "tool_call_id": submit_tc.id,
                        "content": ("submit_result 未通过确定性校验：\n- "
                                    + "\n- ".join(problems)
                                    + "\n证据校验：" + json.dumps(verification,
                                                                   ensure_ascii=False)[:600]),
                    })
                    continue
                messages.append({
                    "role": "tool", "tool_call_id": submit_tc.id,
                    "content": "submit_result 参数为空或缺少关键字段，请在本次收口中补全。",
                })
            raise BudgetExhaustedError(
                f"调查预算已用尽（rounds={budget['rounds_used']}/"
                f"{budget['max_agent_rounds']}, tools={budget['tool_calls_used']}/"
                f"{budget['max_tool_calls']}）且未能在收口轮提交合法结论")

        while True:
            if store.is_cancelled(diagnosis_id):
                raise DiagnosisCancelled("diagnosis cancelled")
            if (budget["tool_calls_used"] >= budget["max_tool_calls"]
                    or budget["rounds_used"] >= budget["max_agent_rounds"]):
                _finalize_only()
                return
            budget["rounds_used"] += 1
            tool_choice: Any = "auto"

            t0 = time.time()
            try:
                resp = llm.chat(messages, tools, tool_choice)
            except LLMError as exc:
                if trace is not None:
                    failure_attempts = getattr(exc, "attempts", None)
                    if failure_attempts is None:
                        failure_attempts = int(
                            getattr(llm, "max_retries", self.cfg.llm_max_retries)) + 1
                    trace.llm_call(duration_ms=(time.time() - t0) * 1000.0,
                                   retries=max(0, failure_attempts - 1),
                                   attempts=failure_attempts,
                                   error="LLM request failed")
                raise
            if trace is not None:
                attempts = int(getattr(llm, "last_attempt_count", 1) or 1)
                usage = getattr(resp, "usage", None)
                pt = getattr(usage, "prompt_tokens", None) if usage else None
                ct = getattr(usage, "completion_tokens", None) if usage else None
                fr = getattr(resp.choices[0], "finish_reason", None) if resp.choices else None
                trace.llm_call(duration_ms=(time.time() - t0) * 1000.0,
                               retries=max(0, attempts - 1), attempts=attempts,
                               finish_reason=fr, prompt_tokens=pt, completion_tokens=ct,
                               response_model=getattr(resp, "model", None))
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None) or []

            # Always persist the assistant message so any following "tool"
            # messages reference a preceding message that carries tool_calls
            # (required by OpenAI-compatible APIs, strictly enforced by some).
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    self._tool_call_payload(tc) for tc in tool_calls
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

            if len(tool_calls) > 1:
                # Policy: one investigation tool per round. Executing "the first
                # one" would silently pick a tool for the model; instead reject
                # the whole round, consume the round (not the tool budget), and
                # make every tool_call_id answerable.
                budget["multi_tool_rejected_rounds"] += 1
                for tc in tool_calls:
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": POLICY_ONE_TOOL})
                if trace is not None:
                    trace.emit("agent.multi_tool_rejected", "agent_planning", {
                        "round": budget["rounds_used"],
                        "tool_calls": len(tool_calls),
                        "tools": [tc.function.name for tc in tool_calls],
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
                    parsed = self._parse_result(args, steps, retrieved)
                    ok, problems, verification = validate_submission(
                        parsed.model_dump(), tool_results)
                    if not ok:
                        # Deterministic gate rejected the submission: feed the
                        # report back to the SAME model so it can fix the
                        # evidence/hypothesis, keep investigating, or abstain.
                        messages.append({
                            "role": "tool", "tool_call_id": tc.id,
                            "content": ("submit_result 未通过确定性校验，本次提交作废：\n- "
                                        + "\n- ".join(problems)
                                        + "\n证据校验：" + json.dumps(verification,
                                                                       ensure_ascii=False)[:800]
                                        + "\n请修正证据/结论后重新提交；若证据确实不足，"
                                          "请提交合法弃权（insufficient_evidence=true + "
                                          "missing_evidence）。"),
                        })
                        if trace is not None:
                            trace.emit("agent.submission_rejected", "agent_planning",
                                       {"problems": problems,
                                        "verification": verification[:5]})
                        continue
                    result = parsed
                    if trace is not None:
                        schema_valid = (
                            result.root_cause_code is None
                            or is_valid_root_cause_code(result.root_cause_code)
                        )
                        trace.llm_final(root_cause_code=result.root_cause_code,
                                        schema_valid=schema_valid, confidence=result.confidence)
                    store.update(diagnosis_id, status="completed", result=result)
                    return

                if store.is_cancelled(diagnosis_id):
                    raise DiagnosisCancelled("diagnosis cancelled")
                budget["tool_calls_used"] += 1
                scope_error = self._scope_violation(name, args, allowed)
                args = self._inject_target_uid(name, args, req.resource)
                # Alert runs get the trusted starts_at anchor (and a bounded
                # window); the model's own time arguments are ignored.
                args = apply_alert_anchor(name, args, req.alert)
                t0 = time.time()
                tool_err: Optional[str] = None
                fail_layer: Optional[str] = None
                try:
                    if scope_error:
                        # Never reach the connector with an out-of-scope target:
                        # prompt-injected text must not widen the blast radius.
                        raise ToolError("out_of_scope", scope_error)
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
                    if exc.code == "uid_mismatch":
                        # Hard stop: the object this diagnosis is about no longer
                        # exists; continuing would attribute new-object data to it.
                        raise UIDMismatchError(str(exc)) from exc
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

                if tool_err is None:
                    tool_results.append({
                        "tool": name,
                        "tool_call_id": tc.id,
                        "args": args,
                        "output": output,
                        "kind": ("retrieval" if name in ("search_knowledge",
                                                         "search_incidents")
                                 else "realtime"),
                    })
                    if name == "relations":
                        self._extend_scope(allowed, output)

                # Only successful tool calls are listed as investigation steps;
                # a failed call has no result and must not be shown as a success
                # (the failure itself is recorded in the timeline/trace).
                if tool_err is None:
                    steps.append(self._step_summary(name, args))
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})

        # Unreachable: the loop only exits through _finalize_only() (which either
        # completes the session or raises BudgetExhaustedError).
        raise BudgetExhaustedError("investigation loop exited without a result")

    @staticmethod
    def _tool_call_payload(tc: Any) -> dict[str, Any]:
        """Serialize a tool call for the next request, preserving any
        provider-specific fields. Gemini thinking models attach a
        thought_signature (via `extra_content`) that must be echoed back
        verbatim, otherwise the follow-up request fails with HTTP 400."""
        payload: dict[str, Any] = {
            "id": tc.id,
            "type": "function",
            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
        }
        for key, value in (getattr(tc, "model_extra", None) or {}).items():
            payload.setdefault(key, value)
        extra_content = getattr(tc, "extra_content", None)
        if extra_content is not None:
            payload.setdefault("extra_content", extra_content)
        return payload

    # ---- tool resource scope guard (prompt-injection blast radius) ----

    @staticmethod
    def _scope_key(kind: str, namespace: str, name: str) -> tuple[str, str, str]:
        return (kind or "", namespace or "", name or "")

    @staticmethod
    def _scope_violation(name: str, args: dict[str, Any],
                         allowed: set[tuple[str, str, str]]) -> Optional[str]:
        spec = _TOOL_SCOPE.get(name)
        if spec is None:
            return None
        kind_field, namespace_field, name_field = spec
        kind = (args.get(kind_field) if kind_field else "Pod") or ""
        namespace = args.get(namespace_field) or ""
        resource_name = args.get(name_field) or ""
        if Agent._scope_key(kind, namespace, resource_name) in allowed:
            return None
        return SCOPE_VIOLATION.format(kind=kind, namespace=namespace or "-",
                                      name=resource_name or "-")

    @staticmethod
    def _extend_scope(allowed: set[tuple[str, str, str]], output: str) -> None:
        """Trust only ResourceRefs the connector actually returned."""
        try:
            body = json.loads(output)
        except (TypeError, ValueError):
            return
        for ref in body.get("relations") or []:
            if not isinstance(ref, dict):
                continue
            allowed.add(Agent._scope_key(ref.get("kind"), ref.get("namespace"),
                                         ref.get("name")))

    @staticmethod
    def _inject_target_uid(name: str, args: dict[str, Any],
                           target: Optional[ResourceRef]) -> dict[str, Any]:
        """Pass the platform-provided UID into every tool call that targets the
        diagnosis resource itself.

        The connector uses it to detect a deleted-and-recreated object (same
        name, new UID) so a diagnosis can never mix in the new object's events,
        logs or metrics. Relations-discovered resources have no trusted UID and
        are left untouched.
        """
        if target is None or not target.uid:
            return args
        spec = _TOOL_SCOPE.get(name)
        if spec is None:
            return args
        kind_field, namespace_field, name_field = spec
        kind = (args.get(kind_field) if kind_field else "Pod") or ""
        if kind != target.kind:
            return args
        if (args.get(name_field) or "") != target.name:
            return args
        if (args.get(namespace_field) or "") != (target.namespace or ""):
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

    @staticmethod
    def _as_str_list(value: Any) -> list[str]:
        """Normalize a list-of-str field. Lists pass through; a JSON-encoded
        array string (sometimes emitted by reasoning models) is decoded; any
        other non-empty string is kept as a single item. Never char-split."""
        if value is None:
            return []
        if isinstance(value, list):
            return [str(x) for x in value]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
            return [text]
        return [str(value)]

    @staticmethod
    def _as_dict_list(value: Any) -> list[dict[str, Any]]:
        """Normalize a list-of-dict field; drops anything that is not a dict."""
        if value is None:
            return []
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                return []
            if not isinstance(parsed, list):
                return []
            value = parsed
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    def _parse_result(self, args: dict[str, Any], steps: list[str],
                      retrieved: Optional[dict[str, Any]] = None) -> DiagnosisResult:
        retrieved = retrieved or {}
        evidence: list[Evidence] = []
        for e in self._as_dict_list(args.get("evidence")):
            evidence.append(Evidence(
                source=str(e.get("source", "")),
                observed_at=str(e.get("observed_at")) if e.get("observed_at") else None,
                summary=str(e.get("summary", "")),
                # Provenance must survive parsing, otherwise the runtime gate can
                # never pin a claim to the tool call that produced it.
                tool_call_id=(str(e.get("tool_call_id")) if e.get("tool_call_id") else None),
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
            for it in self._as_dict_list(args.get("knowledge_references"))
            if it.get("retrieval_id") in retrieved
        ]
        historical_cases = [
            {**retrieved[it["retrieval_id"]], "used_for": it.get("used_for", "")}
            for it in self._as_dict_list(args.get("historical_cases"))
            if it.get("retrieval_id") in retrieved
        ]
        return DiagnosisResult(
            symptom=str(args.get("symptom", "")),
            evidence=evidence,
            root_cause=args.get("root_cause") or None,
            root_cause_code=root_cause_code,
            confidence=args.get("confidence"),
            recommendations=self._as_str_list(args.get("recommendations")),
            missing_evidence=self._as_str_list(args.get("missing_evidence")),
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
