"""API-level tests using the FastAPI TestClient with injected fakes."""

import time

from fastapi.testclient import TestClient

from app.config import Config
from app.llm import ExecutionContext, UnknownProfileError
from app.main import create_app
from app.store import SessionStore

from .fakes import ScriptedLLM, StubConnector

VALID_BODY = {
    "trigger": "manual",
    "resource": {
        "apiVersion": "v1",
        "kind": "Pod",
        "namespace": "payment",
        "name": "payment-api-7b8c9",
        "uid": "uid-1",
    },
}


def make_client(llm_script=None, cfg=None, execution_resolver=None) -> TestClient:
    cfg = cfg or Config()
    store = SessionStore()
    connector = StubConnector()
    llm = ScriptedLLM(llm_script or [
        ScriptedLLM.tool_response("inspect", {"kind": "Pod", "namespace": "payment", "name": "payment-api-7b8c9"}),
        ScriptedLLM.tool_response("submit_result", {
            "symptom": "Pod 持续重启",
            "root_cause_code": "CONTAINER_OOMKILLED",
            "insufficient_evidence": False,
            "evidence": [{"source": "kubernetes.status",
                          "path": "actual_state.restart_count", "operator": "equals",
                          "value": "37", "summary": "OOMKilled"}],
            "root_cause": "容器内存上限不足",
            "confidence": "high",
            "recommendations": ["提高 memory limit"],
        }),
    ])
    app = create_app(cfg=cfg, store=store, connector=connector, llm=llm,
                     execution_resolver=execution_resolver)
    return TestClient(app)


def wait_for_terminal(client: TestClient, diagnosis_id: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/v1/diagnoses/{diagnosis_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"diagnosis {diagnosis_id} did not reach terminal status")


def test_create_and_poll_diagnosis():
    client = make_client()
    resp = client.post("/api/v1/diagnoses", json=VALID_BODY)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "queued"
    assert body["diagnosis_id"].startswith("diag_")

    result = wait_for_terminal(client, body["diagnosis_id"])
    assert result["status"] == "completed"
    assert result["result"]["root_cause"] == "容器内存上限不足"
    assert result["result"]["evidence"][0]["summary"] == "OOMKilled"


def test_create_rejects_unsupported_kind():
    client = make_client()
    body = dict(VALID_BODY)
    body["resource"] = {"kind": "ConfigMap", "namespace": "payment", "name": "payment-api", "uid": "u"}
    resp = client.post("/api/v1/diagnoses", json=body)
    assert resp.status_code == 400


def test_create_accepts_deployment_in_phase3():
    client = make_client()
    body = dict(VALID_BODY)
    body["resource"] = {"kind": "Deployment", "namespace": "payment", "name": "payment-api", "uid": "u"}
    resp = client.post("/api/v1/diagnoses", json=body)
    assert resp.status_code == 201


def test_create_rejects_missing_uid():
    client = make_client()
    body = dict(VALID_BODY)
    body["resource"] = {"kind": "Pod", "namespace": "payment", "name": "payment-api"}
    resp = client.post("/api/v1/diagnoses", json=body)
    assert resp.status_code == 400


def test_create_rejects_alert_trigger_in_phase1():
    client = make_client()
    body = dict(VALID_BODY)
    body["trigger"] = "alert"
    resp = client.post("/api/v1/diagnoses", json=body)
    assert resp.status_code == 400


def test_get_unknown_diagnosis_returns_404():
    client = make_client()
    resp = client.get("/api/v1/diagnoses/diag_missing")
    assert resp.status_code == 404

def test_model_profile_selection_disabled_returns_403():
    cfg = Config()
    cfg.enable_model_profile_selection = False
    client = make_client(cfg=cfg)
    body = dict(VALID_BODY)
    body["model_profile"] = "ref-model"
    resp = client.post("/api/v1/diagnoses", json=body)
    assert resp.status_code == 403


def test_unknown_model_profile_returns_422():
    cfg = Config()
    cfg.enable_model_profile_selection = True

    def resolver(name):
        raise UnknownProfileError(f"unknown model profile: {name!r}")

    client = make_client(cfg=cfg, execution_resolver=resolver)
    body = dict(VALID_BODY)
    body["model_profile"] = "nope"
    resp = client.post("/api/v1/diagnoses", json=body)
    assert resp.status_code == 422


def test_concurrent_diagnoses_bind_distinct_models():
    cfg = Config()
    cfg.enable_model_profile_selection = True

    def resolver(name):
        llm = ScriptedLLM([
            ScriptedLLM.tool_response(
                "inspect", {"kind": "Pod", "namespace": "payment", "name": "payment-api-7b8c9"}),
            ScriptedLLM.tool_response("submit_result", {
                "symptom": "s",
                "root_cause_code": "CONTAINER_OOMKILLED",
                "insufficient_evidence": False,
                "evidence": [{"source": "kubernetes.status",
                              "path": "actual_state.restart_count", "operator": "equals",
                              "value": "37", "summary": "x"}],
                "root_cause": name,
                "confidence": "high",
                "recommendations": [],
            }),
        ])
        return ExecutionContext(llm=llm, metadata={
            "resolved_profile": name, "requested_model_id": f"{name}-remote"})

    client = make_client(cfg=cfg, execution_resolver=resolver)
    ids = {}
    for name in ("model-a", "model-b"):
        body = dict(VALID_BODY)
        body["model_profile"] = name
        resp = client.post("/api/v1/diagnoses", json=body)
        assert resp.status_code == 201
        assert resp.json()["model"]["resolved_profile"] == name
        ids[name] = resp.json()["diagnosis_id"]

    for name, diag_id in ids.items():
        result = wait_for_terminal(client, diag_id)
        assert result["status"] == "completed"
        assert result["result"]["root_cause"] == name


def test_eval_budgets_require_eval_run_id():
    """Case budgets are eval-only: a product request must not be able to set
    (or widen) its own investigation budget."""
    client = make_client()
    body = {**VALID_BODY, "eval_max_tool_calls": 2}
    resp = client.post("/api/v1/diagnoses", json=body)
    assert resp.status_code == 422
    assert "eval_run_id" in resp.json()["detail"]


def test_eval_budgets_must_be_positive():
    client = make_client()
    body = {**VALID_BODY, "eval_run_id": "run-1", "eval_max_agent_rounds": 0}
    resp = client.post("/api/v1/diagnoses", json=body)
    assert resp.status_code == 422
    assert "eval_max_agent_rounds" in resp.json()["detail"]


def test_cancel_endpoint_lifecycle():
    client = make_client()
    assert client.post("/api/v1/diagnoses/diag_missing/cancel").status_code == 404

    created = client.post("/api/v1/diagnoses", json=VALID_BODY).json()
    diag_id = created["diagnosis_id"]
    assert client.post(f"/api/v1/diagnoses/{diag_id}/cancel").status_code == 200

    wait_for_terminal(client, diag_id)
    assert client.post(f"/api/v1/diagnoses/{diag_id}/cancel").status_code == 409


def test_global_concurrency_limit_keeps_excess_sessions_queued(monkeypatch):
    import threading

    from app.llm import ExecutionContext

    gate = threading.Event()
    entered = threading.Event()

    class BlockingLLM(ScriptedLLM):
        def chat(self, messages, tools, tool_choice):
            entered.set()
            gate.wait(timeout=10)
            return ScriptedLLM.tool_response("submit_result", {
                "symptom": "s", "root_cause_code": "", "root_cause": "",
                "insufficient_evidence": True, "confidence": "low",
                "recommendations": [], "missing_evidence": [],
            })

    cfg = Config()
    cfg.max_concurrent_diagnoses = 1
    client = make_client(cfg=cfg, execution_resolver=lambda name: ExecutionContext(
        llm=BlockingLLM([]), metadata={"resolved_profile": name, "provider": "test"}))

    ids = []
    for _ in range(3):
        body = dict(VALID_BODY)
        body["model_profile"] = "model-a"
        cfg.enable_model_profile_selection = True
        resp = client.post("/api/v1/diagnoses", json=body)
        assert resp.status_code == 201
        ids.append(resp.json()["diagnosis_id"])

    assert entered.wait(timeout=10), "the single worker never started"
    statuses = [client.get(f"/api/v1/diagnoses/{i}").json()["status"] for i in ids]
    assert statuses.count("investigating") == 1
    assert statuses.count("queued") == 2

    gate.set()
    for diag_id in ids:
        assert wait_for_terminal(client, diag_id, timeout=15)["status"] == "completed"


def test_startup_fails_sessions_orphaned_by_a_previous_process():
    from app.models import DiagnosisRequest, ResourceRef, Trigger

    cfg = Config()
    store = SessionStore()
    queued = store.create(DiagnosisRequest(
        trigger=Trigger.manual,
        resource=ResourceRef(kind="Pod", namespace="payment",
                             name="payment-api-7b8c9", uid="uid-1")))
    store.update(queued.diagnosis_id, status="investigating")

    app = create_app(cfg=cfg, store=store, connector=StubConnector(), llm=ScriptedLLM([]))
    TestClient(app)

    row = store.get(queued.diagnosis_id)
    assert row.status == "failed"
    assert row.failure_reason == "agent_restarted"
