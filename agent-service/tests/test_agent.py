"""Unit tests for the diagnosis loop: success, failure boundaries, evidence-insufficient."""

import json

import pytest

from app.agent import Agent, UIDMismatchError
from app.config import Config
from app.connector import ConnectorError
from app.models import DiagnosisRequest, ResourceRef, Trigger
from app.store import SessionStore

from .fakes import ScriptedLLM, StubConnector

RESOURCE = ResourceRef(kind="Pod", namespace="payment", name="payment-api-7b8c9", uid="uid-1")


def make_request() -> DiagnosisRequest:
    return DiagnosisRequest(trigger=Trigger.manual, resource=RESOURCE)


def make_agent(llm, connector=None, max_calls=12) -> Agent:
    cfg = Config()
    cfg.max_tool_calls = max_calls
    return Agent(cfg, connector or StubConnector(), llm)


def run(agent, store=None):
    store = store or SessionStore()
    d = store.create(make_request())
    agent.run(make_request(), store, d.diagnosis_id)
    return store.get(d.diagnosis_id)


def test_successful_diagnosis_completes_with_parsed_result():
    connector = StubConnector()
    llm = ScriptedLLM([
        ScriptedLLM.tool_response("inspect", {"kind": "Pod", "namespace": "payment", "name": "payment-api-7b8c9"}),
        ScriptedLLM.tool_response("submit_result", {
            "symptom": "Pod 持续重启",
            "evidence": [
                {"source": "kubernetes.status", "summary": "Last termination reason is OOMKilled"},
                {"source": "kubernetes.logs", "summary": "java.lang.OutOfMemoryError"},
            ],
            "root_cause": "容器内存上限不足",
            "confidence": "high",
            "recommendations": ["提高 memory limit"],
        }),
    ])
    d = run(make_agent(llm, connector))

    assert d.status == "completed"
    assert d.result is not None
    assert d.result.root_cause == "容器内存上限不足"
    assert d.result.confidence == "high"
    assert len(d.result.evidence) == 2
    assert len(d.result.investigation_steps) == 1
    assert d.result.investigation_steps[0].startswith("获取 Pod 状态")
    # The platform-provided UID must be forwarded to the connector's inspect call.
    assert connector.call_log[0] == "inspect"


def test_insufficient_evidence_is_completed_not_failed():
    llm = ScriptedLLM([
        ScriptedLLM.tool_response("inspect", {"kind": "Pod", "namespace": "payment", "name": "payment-api-7b8c9"}),
        ScriptedLLM.tool_response("submit_result", {
            "symptom": "Pod Pending",
            "evidence": [{"source": "kubernetes.status", "summary": "pod is Pending"}],
            "root_cause": "",
            "confidence": "low",
            "recommendations": [],
            "missing_evidence": ["调度失败的具体事件，节点资源信息"],
        }),
    ])
    d = run(make_agent(llm))

    assert d.status == "completed"
    assert d.result.root_cause is None
    assert d.result.missing_evidence == ["调度失败的具体事件，节点资源信息"]


def test_uid_mismatch_fails_session():
    connector = StubConnector(inspect_response={
        "target": {}, "exists": True, "uid_mismatch": True,
        "desired_state": {}, "actual_state": {}, "conditions": [], "anomalies": [],
    })
    llm = ScriptedLLM([
        ScriptedLLM.tool_response("inspect", {"kind": "Pod", "namespace": "payment", "name": "payment-api-7b8c9"}),
    ])
    d = run(make_agent(llm, connector))

    assert d.status == "failed"
    assert "资源已重建" in (d.error or "")


def test_connector_unreachable_fails_session():
    connector = StubConnector(raise_on="connector")
    llm = ScriptedLLM([
        ScriptedLLM.tool_response("inspect", {"kind": "Pod", "namespace": "payment", "name": "payment-api-7b8c9"}),
    ])
    d = run(make_agent(llm, connector))

    assert d.status == "failed"
    assert "connector unreachable" in (d.error or "")


def test_tool_not_found_is_fed_back_and_loop_continues():
    connector = StubConnector(raise_on="not_found")
    llm = ScriptedLLM([
        ScriptedLLM.tool_response("inspect", {"kind": "Pod", "namespace": "payment", "name": "payment-api-7b8c9"}),
        ScriptedLLM.tool_response("submit_result", {
            "symptom": "Pod 不存在",
            "evidence": [{"source": "kubernetes.status", "summary": "not found"}],
            "root_cause": "Pod 已被删除",
            "confidence": "medium",
            "recommendations": [],
        }),
    ])
    d = run(make_agent(llm, connector))

    assert d.status == "completed"
    assert d.result.root_cause == "Pod 已被删除"


def test_max_tool_calls_without_conclusion_fails():
    llm = ScriptedLLM([
        ScriptedLLM.tool_response("inspect", {"kind": "Pod", "namespace": "payment", "name": "payment-api-7b8c9"})
        for _ in range(3)
    ])
    d = run(make_agent(llm, max_calls=3))

    assert d.status == "failed"
    assert "最大工具调用次数" in (d.error or "")


def test_plain_text_answer_steered_back_to_submit_result():
    llm = ScriptedLLM([
        ScriptedLLM.tool_response("inspect", {"kind": "Pod", "namespace": "payment", "name": "payment-api-7b8c9"}),
        ScriptedLLM.text_response("Pod 持续重启，根因是 OOMKilled。"),
        ScriptedLLM.tool_response("submit_result", {
            "symptom": "Pod 持续重启",
            "evidence": [{"source": "kubernetes.status", "summary": "OOMKilled"}],
            "root_cause": "容器内存上限不足",
            "confidence": "medium",
            "recommendations": ["提高 memory limit"],
        }),
    ])
    d = run(make_agent(llm))

    assert d.status == "completed"
    assert d.result.root_cause == "容器内存上限不足"


def test_uid_is_forwarded_to_inspect_for_target():
    seen: list[dict] = []

    class RecordingConnector(StubConnector):
        def inspect(self, target: dict):
            seen.append(target)
            return super().inspect(target)

    llm = ScriptedLLM([
        ScriptedLLM.tool_response("inspect", {"kind": "Pod", "namespace": "payment", "name": "payment-api-7b8c9"}),
        ScriptedLLM.tool_response("submit_result", {
            "symptom": "s", "evidence": [], "root_cause": "r",
            "confidence": "low", "recommendations": [],
        }),
    ])
    run(make_agent(llm, RecordingConnector()))

    assert seen[0].get("uid") == "uid-1"


def test_tool_messages_follow_assistant_message_with_tool_calls():
    """Each role=tool message must be preceded by an assistant message carrying
    a matching tool_call id (strictly enforced by DeepSeek and other
    OpenAI-compatible providers)."""

    seen_messages: list[list[dict]] = []

    class RecordingLLM(ScriptedLLM):
        def chat(self, messages, tools, tool_choice):
            seen_messages.append(list(messages))
            return super().chat(messages, tools, tool_choice)

    llm = RecordingLLM([
        ScriptedLLM.tool_response("inspect", {"kind": "Pod", "namespace": "payment", "name": "payment-api-7b8c9"}, call_id="call_inspect"),
        ScriptedLLM.tool_response("submit_result", {
            "symptom": "s", "evidence": [], "root_cause": "r",
            "confidence": "low", "recommendations": [],
        }, call_id="call_submit"),
    ])
    run(make_agent(llm))

    # The second request carries the tool result after the first response.
    messages = seen_messages[1]
    for i, m in enumerate(messages):
        if m.get("role") != "tool":
            continue
        assert i > 0 and messages[i - 1]["role"] == "assistant", f"tool message at {i} has no preceding assistant message"
        tool_ids = {tc["id"] for tc in messages[i - 1].get("tool_calls", [])}
        assert m["tool_call_id"] in tool_ids, f"tool_call_id {m['tool_call_id']} not in preceding assistant tool_calls"


def test_reasoning_content_passed_back():
    """Reasoning models must receive their reasoning_content back verbatim;
    otherwise DeepSeek rejects the request with a 400."""

    seen_messages: list[list[dict]] = []

    class RecordingLLM(ScriptedLLM):
        def chat(self, messages, tools, tool_choice):
            seen_messages.append(list(messages))
            return super().chat(messages, tools, tool_choice)

    llm = RecordingLLM([
        ScriptedLLM.tool_response_with_reasoning(
            "inspect", {"kind": "Pod", "namespace": "payment", "name": "payment-api-7b8c9"},
            reasoning="step 1: fetch pod state",
        ),
        ScriptedLLM.tool_response("submit_result", {
            "symptom": "s", "evidence": [], "root_cause": "r",
            "confidence": "low", "recommendations": [],
        }),
    ])
    run(make_agent(llm))

    # The inspect response (with reasoning_content) must be forwarded unchanged
    # in the next request: messages[1] == [system, user, assistant(reasoning+tool), tool].
    assistant_msg = seen_messages[1][2]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["reasoning_content"] == "step 1: fetch pod state"


def test_parses_scorable_root_cause_and_structured_evidence():
    llm = ScriptedLLM([
        ScriptedLLM.tool_response("submit_result", {
            "symptom": "Pod 持续重启",
            "root_cause_code": "CONTAINER_OOMKILLED",
            "insufficient_evidence": False,
            "evidence": [
                {
                    "source": "kubernetes.status",
                    "resource_uid": "uid-1",
                    "path": "status.containerStatuses[0].lastState.terminated.reason",
                    "operator": "equals",
                    "value": "OOMKilled",
                    "summary": "Last termination reason is OOMKilled",
                },
            ],
            "root_cause": "容器内存上限不足",
            "confidence": "high",
            "recommendations": ["提高 memory limit"],
        }),
    ])
    d = run(make_agent(llm))

    assert d.status == "completed"
    assert d.result.root_cause_code == "CONTAINER_OOMKILLED"
    assert d.result.insufficient_evidence is False
    ev = d.result.evidence[0]
    assert ev.resource_uid == "uid-1"
    assert ev.path == "status.containerStatuses[0].lastState.terminated.reason"
    assert ev.operator == "equals"
    assert ev.value == "OOMKilled"


def test_parses_insufficient_evidence_abstention():
    llm = ScriptedLLM([
        ScriptedLLM.tool_response("submit_result", {
            "symptom": "Pod Pending",
            "root_cause_code": "",
            "insufficient_evidence": True,
            "evidence": [{"source": "kubernetes.status", "summary": "pod is Pending"}],
            "root_cause": "",
            "confidence": "low",
            "recommendations": [],
            "missing_evidence": ["调度失败的具体事件"],
        }),
    ])
    d = run(make_agent(llm))

    assert d.status == "completed"
    assert d.result.insufficient_evidence is True
    assert d.result.root_cause_code is None
    assert d.result.root_cause is None


def test_trace_recorded_when_enabled(tmp_path):
    cfg = Config()
    cfg.trace_dir = str(tmp_path)
    cfg.max_tool_calls = 12
    req = make_request()
    req.eval_run_id = "run-1"
    req.case_id = "case-oom"
    req.case_version = "1"
    req.attempt_index = 0

    store = SessionStore()
    d = store.create(req)
    llm = ScriptedLLM([
        ScriptedLLM.tool_response("inspect", {"kind": "Pod", "namespace": "payment", "name": "payment-api-7b8c9"}),
        ScriptedLLM.tool_response("submit_result", {
            "symptom": "s", "root_cause_code": "CONTAINER_OOMKILLED", "insufficient_evidence": False,
            "evidence": [], "root_cause": "r", "confidence": "high", "recommendations": [],
        }),
    ])
    Agent(cfg, StubConnector(), llm).run(req, store, d.diagnosis_id)

    trace_file = tmp_path / f"{d.diagnosis_id}.jsonl"
    assert trace_file.exists()
    lines = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
    names = [line["name"] for line in lines]
    assert "diagnosis" in names and "llm.call" in names and "tool.inspect" in names and "llm.final" in names
    # eval context is propagated to every span
    assert all(line["eval_run_id"] == "run-1" for line in lines)
    assert all(line["case_id"] == "case-oom" for line in lines)
    # root span finished as completed
    finished = [l for l in lines if l["name"] == "diagnosis" and l["attributes"].get("status") == "completed"]
    assert finished, names


def test_trace_failure_layer_connector(tmp_path):
    cfg = Config()
    cfg.trace_dir = str(tmp_path)
    req = make_request()
    store = SessionStore()
    d = store.create(req)
    llm = ScriptedLLM([
        ScriptedLLM.tool_response("inspect", {"kind": "Pod", "namespace": "payment", "name": "payment-api-7b8c9"}),
    ])
    Agent(cfg, StubConnector(raise_on="connector"), llm).run(req, store, d.diagnosis_id)

    trace_file = tmp_path / f"{d.diagnosis_id}.jsonl"
    lines = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
    failed = [l for l in lines if l["name"] == "diagnosis" and l["attributes"].get("status") == "failed"]
    assert failed
    assert failed[0]["failure_layer"] == "connector"


def test_tools_gated_by_capabilities():
    """query_metrics / query_logs must only be advertised when the connector
    reports the prometheus.metrics / loki.logs capabilities."""

    seen_tools: list[list[dict]] = []

    class RecordingLLM(ScriptedLLM):
        def chat(self, messages, tools, tool_choice):
            seen_tools.append([t["function"]["name"] for t in tools])
            return super().chat(messages, tools, tool_choice)

    # No data sources -> base tools only.
    llm0 = RecordingLLM([ScriptedLLM.tool_response("submit_result", {
        "symptom": "s", "root_cause_code": "", "insufficient_evidence": True,
        "evidence": [], "root_cause": "", "confidence": "low", "recommendations": [],
    })])
    run(make_agent(llm0, StubConnector(capabilities={})))
    assert seen_tools[0] == ["inspect", "relations", "events", "logs", "submit_result"]

    # Both data sources available -> data tools added.
    seen_tools.clear()
    llm1 = RecordingLLM([ScriptedLLM.tool_response("submit_result", {
        "symptom": "s", "root_cause_code": "", "insufficient_evidence": True,
        "evidence": [], "root_cause": "", "confidence": "low", "recommendations": [],
    })])
    run(make_agent(llm1, StubConnector(capabilities={"prometheus.metrics": True, "loki.logs": True})))
    assert "query_metrics" in seen_tools[0]
    assert "query_logs" in seen_tools[0]


def test_degraded_metrics_tool_fed_back_to_llm():
    """A degraded query_metrics response (prometheus unavailable) is passed to
    the LLM as a fact it must accept, not an error that fails the session."""

    seen_tool_outputs: list[str] = []

    class RecordingConnector(StubConnector):
        def query_metrics(self, target, **kwargs):
            return {"target": target, "capability": "prometheus.metrics",
                    "available": False, "degraded_reason": "prometheus not configured on connector",
                    "metric": kwargs.get("metric"), "summary": {}, "series": []}

    class RecordingLLM(ScriptedLLM):
        def chat(self, messages, tools, tool_choice):
            for m in messages:
                if m.get("role") == "tool":
                    seen_tool_outputs.append(m["content"])
            return super().chat(messages, tools, tool_choice)

    llm = RecordingLLM([
        ScriptedLLM.tool_response("query_metrics", {"kind": "Pod", "namespace": "payment", "name": "payment-api-7b8c9", "metric": "memory"}),
        ScriptedLLM.tool_response("submit_result", {
            "symptom": "s", "root_cause_code": "", "insufficient_evidence": True,
            "evidence": [], "root_cause": "", "confidence": "low", "recommendations": [],
        }),
    ])
    connector = RecordingConnector(capabilities={"prometheus.metrics": True})
    d = run(make_agent(llm, connector))

    assert d.status == "completed"
    assert any("prometheus not configured" in out for out in seen_tool_outputs)


def test_non_pod_target_accepted_in_phase3():
    req = DiagnosisRequest(trigger=Trigger.manual,
                           resource=ResourceRef(kind="Deployment", namespace="payment", name="payment-api", uid="u"))
    store = SessionStore()
    d = store.create(req)
    llm = ScriptedLLM([
        ScriptedLLM.tool_response("submit_result", {
            "symptom": "s", "root_cause_code": "", "insufficient_evidence": True,
            "evidence": [], "root_cause": "", "confidence": "low", "recommendations": [],
        }),
    ])
    Agent(Config(), StubConnector(), llm).run(req, store, d.diagnosis_id)
    assert store.get(d.diagnosis_id).status == "completed"


def test_empty_submit_result_is_steered_not_accepted():
    """Reasoning models sometimes emit submit_result with empty arguments; the
    agent must ask for a retry instead of silently completing with an empty
    structured result."""

    seen_steers = 0

    class RecordingLLM(ScriptedLLM):
        def chat(self, messages, tools, tool_choice):
            nonlocal seen_steers
            seen_steers = sum(1 for m in messages
                              if m.get("role") == "tool" and "submit_result 参数为空" in m.get("content", ""))
            return super().chat(messages, tools, tool_choice)

    llm = RecordingLLM([
        # First: an empty submit_result (empty args {}).
        ScriptedLLM.tool_response("submit_result", {}),
        # After steering, the model submits a proper result.
        ScriptedLLM.tool_response("submit_result", {
            "symptom": "Pod 持续重启", "root_cause_code": "CRASH_LOOP_BACKOFF",
            "insufficient_evidence": False,
            "evidence": [{"source": "kubernetes.status", "summary": "CrashLoopBackOff"}],
            "root_cause": "应用崩溃", "confidence": "high", "recommendations": ["修复应用"],
        }),
    ])
    d = run(make_agent(llm))

    assert d.status == "completed"
    assert d.result.root_cause_code == "CRASH_LOOP_BACKOFF"
    assert seen_steers == 1
