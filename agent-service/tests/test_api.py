"""API-level tests using the FastAPI TestClient with injected fakes."""

import time

from fastapi.testclient import TestClient

from app.config import Config
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


def make_client(llm_script=None) -> TestClient:
    cfg = Config()
    store = SessionStore()
    connector = StubConnector()
    llm = ScriptedLLM(llm_script or [
        ScriptedLLM.tool_response("inspect", {"kind": "Pod", "namespace": "payment", "name": "payment-api-7b8c9"}),
        ScriptedLLM.tool_response("submit_result", {
            "symptom": "Pod 持续重启",
            "evidence": [{"source": "kubernetes.status", "summary": "OOMKilled"}],
            "root_cause": "容器内存上限不足",
            "confidence": "high",
            "recommendations": ["提高 memory limit"],
        }),
    ])
    app = create_app(cfg=cfg, store=store, connector=connector, llm=llm)
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
