"""Phase 5 alert lifecycle tests: fingerprint dedup, resolve, refire, unresolved,
concurrency, rate limiting and persistence (design §26)."""

import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from app.store import ALERT_CLOSED, ALERT_OPEN, ALERT_UNRESOLVED, SessionStore

from .fakes import ScriptedLLM, StubConnector


class RefillLLM(ScriptedLLM):
    """Reusable two-step script so several diagnoses can share one client."""

    def __init__(self) -> None:
        template = [
            ScriptedLLM.tool_response("inspect", {"kind": "Pod", "namespace": "payment", "name": "payment-api"}),
            ScriptedLLM.tool_response("submit_result", {
                "symptom": "s", "evidence": [{"source": "kubernetes.status", "summary": "x"}],
                "root_cause": "r", "confidence": "high", "recommendations": [],
            }),
        ]
        super().__init__(list(template))
        self._template = template

    def chat(self, messages, tools, tool_choice):
        if not self.script:
            self.script = list(self._template)
        return super().chat(messages, tools, tool_choice)


def make_client_with_store(cfg=None):
    cfg = cfg or Config()
    store = SessionStore()
    app = create_app(cfg=cfg, store=store, connector=StubConnector(), llm=RefillLLM())
    return TestClient(app), store


def make_client(cfg=None) -> TestClient:
    return make_client_with_store(cfg)[0]


def _alert_body(fingerprint, *, status="firing", unresolved=False, resource=True,
                starts_at="2026-09-15T00:00:00Z"):
    body = {
        "trigger": "alert",
        "alert": {"fingerprint": fingerprint, "status": status, "alertname": "PodHighMemory",
                  "starts_at": starts_at, "labels": {"alertname": "PodHighMemory"}},
    }
    if unresolved:
        body["alert"]["unresolved_target"] = True
    if resource and not unresolved:
        body["resource"] = {"apiVersion": "v1", "kind": "Pod", "namespace": "payment",
                            "name": "payment-api", "uid": "uid-1"}
    return body


def _wait(client, diagnosis_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/v1/diagnoses/{diagnosis_id}").json()
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError("diagnosis did not reach terminal state")


def test_alert_firing_creates_then_dedups_same_lifecycle():
    client = make_client()
    first = client.post("/api/v1/diagnoses", json=_alert_body("fp-1"))
    assert first.status_code == 201
    body = first.json()
    assert body["deduped"] is False
    _wait(client, body["diagnosis_id"])

    second = client.post("/api/v1/diagnoses", json=_alert_body("fp-1"))
    assert second.status_code == 201
    again = second.json()
    assert again["deduped"] is True
    assert again["diagnosis_id"] == body["diagnosis_id"]
    assert len(client.get("/api/v1/diagnoses").json()) == 1


def test_alert_resolved_then_refire_creates_new_lifecycle():
    client = make_client()
    first = client.post("/api/v1/diagnoses", json=_alert_body("fp-2")).json()
    _wait(client, first["diagnosis_id"])

    resolved = client.post("/api/v1/diagnoses", json=_alert_body("fp-2", status="resolved"))
    assert resolved.status_code == 201
    assert resolved.json()["status"] == ALERT_CLOSED
    assert resolved.json()["closed"] is True

    refire = client.post("/api/v1/diagnoses", json=_alert_body("fp-2")).json()
    assert refire["deduped"] is False
    assert refire["diagnosis_id"] != first["diagnosis_id"]
    assert len(client.get("/api/v1/diagnoses").json()) == 2


def test_alert_unresolved_target_recorded_without_diagnosis():
    client, store = make_client_with_store()
    resp = client.post("/api/v1/diagnoses", json=_alert_body("fp-3", unresolved=True))
    assert resp.status_code == 201
    assert resp.json()["status"] == ALERT_UNRESOLVED
    assert client.get("/api/v1/diagnoses").json() == []
    assert store.latest_alert_lifecycle("fp-3")["state"] == ALERT_UNRESOLVED


def test_repeated_unresolved_is_deduped():
    client, store = make_client_with_store()
    first = client.post("/api/v1/diagnoses", json=_alert_body("fp-u", unresolved=True)).json()
    second = client.post("/api/v1/diagnoses", json=_alert_body("fp-u", unresolved=True)).json()
    assert first["deduped"] is False
    assert second["deduped"] is True
    lifecycles = store.list_alert_lifecycles("fp-u")
    assert len(lifecycles) == 1


def test_resolved_is_not_rate_limited_and_needs_no_resource():
    cfg = Config()
    cfg.alert_rate_limit_per_minute = 1
    client, store = make_client_with_store(cfg)
    # The only quota slot is consumed by one unresolved firing alert.
    assert client.post("/api/v1/diagnoses",
                       json=_alert_body("fp-r", unresolved=True)).status_code == 201
    # resolved must not be limited (no resource provided either) and must close it.
    resolved = client.post("/api/v1/diagnoses",
                           json=_alert_body("fp-r", status="resolved", resource=False))
    assert resolved.status_code == 201
    assert resolved.json()["closed"] is True
    assert store.latest_alert_lifecycle("fp-r")["state"] == ALERT_CLOSED


def test_late_resolved_does_not_close_newer_lifecycle():
    client, store = make_client_with_store()
    first = client.post("/api/v1/diagnoses",
                        json=_alert_body("fp-late", starts_at="A")).json()
    _wait(client, first["diagnosis_id"])

    stale = client.post("/api/v1/diagnoses",
                        json=_alert_body("fp-late", status="resolved", resource=False,
                                         starts_at="B")).json()
    assert stale["closed"] is False
    assert store.latest_alert_lifecycle("fp-late")["state"] == ALERT_OPEN

    current = client.post("/api/v1/diagnoses",
                          json=_alert_body("fp-late", status="resolved", resource=False,
                                           starts_at="A")).json()
    assert current["closed"] is True
    assert store.latest_alert_lifecycle("fp-late")["state"] == ALERT_CLOSED


def test_concurrent_firing_creates_single_diagnosis():
    client, store = make_client_with_store()
    body = _alert_body("fp-conc")
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(
            lambda _: client.post("/api/v1/diagnoses", json=body), range(5)))
    assert all(r.status_code == 201 for r in responses)
    assert len(client.get("/api/v1/diagnoses").json()) == 1
    active = [lc for lc in store.list_alert_lifecycles("fp-conc") if lc["state"] == ALERT_OPEN]
    assert len(active) == 1
    assert active[0]["diagnosis_id"]


def test_alert_lifecycle_persists_across_store_reopen(tmp_path):
    db = str(tmp_path / "diagnoses.db")
    first = SessionStore(db)
    row, created = first.claim_active_alert_lifecycle(
        fingerprint="fp-p", state=ALERT_OPEN, starts_at="A")
    assert created is True
    first.link_alert_diagnosis(row["id"], "diag_persist")

    reopened = SessionStore(db)
    again, created2 = reopened.claim_active_alert_lifecycle(
        fingerprint="fp-p", state=ALERT_OPEN, starts_at="A")
    assert created2 is False
    assert again["diagnosis_id"] == "diag_persist"
    closed = reopened.close_alert_lifecycle("fp-p", starts_at="A")
    assert closed is not None and closed["state"] == ALERT_CLOSED


def test_alert_storm_rate_limited():
    """The limiter bounds *new investigations*, so distinct firings are capped."""
    cfg = Config()
    cfg.alert_rate_limit_per_minute = 2
    client, store = make_client_with_store(cfg)
    ok1 = client.post("/api/v1/diagnoses", json=_alert_body("fp-a"))
    ok2 = client.post("/api/v1/diagnoses", json=_alert_body("fp-b"))
    limited = client.post("/api/v1/diagnoses", json=_alert_body("fp-c"))
    assert ok1.status_code == 201 and ok2.status_code == 201
    assert limited.status_code == 429
    # The rejected delivery leaves no claim behind: a later retry can still run.
    assert store.list_alert_lifecycles("fp-c") == []


def test_alert_trigger_requires_alert_context():
    client = make_client()
    body = {"trigger": "alert", "resource": {"kind": "Pod", "namespace": "payment",
                                             "name": "payment-api", "uid": "uid-1"}}
    resp = client.post("/api/v1/diagnoses", json=body)
    assert resp.status_code == 400


def test_diagnosis_creation_failure_can_retry():
    cfg = Config()
    base_client, store = make_client_with_store(cfg)
    client = TestClient(base_client.app, raise_server_exceptions=False)
    original_create = store.create_with_alert_link
    store.create_with_alert_link = lambda req, lifecycle_id: (
        _ for _ in ()).throw(RuntimeError("db down"))
    failed = client.post("/api/v1/diagnoses", json=_alert_body("fp-retry"))
    assert failed.status_code == 500
    # The empty claim must be released so a retry can run.
    assert store.list_alert_lifecycles("fp-retry") == []

    store.create_with_alert_link = original_create
    retried = client.post("/api/v1/diagnoses", json=_alert_body("fp-retry"))
    assert retried.status_code == 201
    body = retried.json()
    assert body["deduped"] is False and body["diagnosis_id"]
    _wait(client, body["diagnosis_id"])
    lifecycles = store.list_alert_lifecycles("fp-retry")
    assert len(lifecycles) == 1
    assert lifecycles[0]["diagnosis_id"] == body["diagnosis_id"]


def test_unresolved_target_upgraded_when_resolvable():
    client, store = make_client_with_store()
    first = client.post("/api/v1/diagnoses",
                        json=_alert_body("fp-up", unresolved=True)).json()
    assert first["status"] == ALERT_UNRESOLVED and first["deduped"] is False

    second = client.post("/api/v1/diagnoses", json=_alert_body("fp-up"))
    assert second.status_code == 201
    body = second.json()
    assert body["deduped"] is False and body["diagnosis_id"]
    _wait(client, body["diagnosis_id"])

    lifecycles = store.list_alert_lifecycles("fp-up")
    assert len(lifecycles) == 1
    assert lifecycles[0]["state"] == ALERT_OPEN
    assert lifecycles[0]["diagnosis_id"] == body["diagnosis_id"]

    third = client.post("/api/v1/diagnoses", json=_alert_body("fp-up")).json()
    assert third["deduped"] is True
    assert third["diagnosis_id"] == body["diagnosis_id"]


def test_stale_empty_claim_can_be_taken_over():
    client, store = make_client_with_store()
    row, created = store.claim_active_alert_lifecycle(
        fingerprint="fp-stale", state=ALERT_OPEN, starts_at="A")
    assert created is True
    store._conn.execute(
        "UPDATE alert_lifecycles SET created_at = ?, latest_alert_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", row["id"]))
    store._conn.commit()

    resp = client.post("/api/v1/diagnoses", json=_alert_body("fp-stale", starts_at="A"))
    assert resp.status_code == 201
    body = resp.json()
    assert body["deduped"] is False and body["diagnosis_id"]
    _wait(client, body["diagnosis_id"])


def test_concurrent_stale_takeover_starts_single_diagnosis():
    client, store = make_client_with_store()
    row, created = store.claim_active_alert_lifecycle(
        fingerprint="fp-race", state=ALERT_OPEN, starts_at="A")
    assert created is True
    store._conn.execute(
        "UPDATE alert_lifecycles SET created_at = ?, latest_alert_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", row["id"]))
    store._conn.commit()

    body = _alert_body("fp-race", starts_at="A")
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(
            lambda _: client.post("/api/v1/diagnoses", json=body), range(5)))

    assert all(r.status_code == 201 for r in responses)
    started = [r.json() for r in responses if r.json().get("deduped") is False]
    assert len(started) == 1, responses
    assert len(client.get("/api/v1/diagnoses").json()) == 1
    lifecycles = store.list_alert_lifecycles("fp-race")
    assert len(lifecycles) == 1 and lifecycles[0]["diagnosis_id"] == started[0]["diagnosis_id"]


def test_concurrent_upgrade_of_unresolved_starts_single_diagnosis():
    client, store = make_client_with_store()
    first = client.post("/api/v1/diagnoses",
                        json=_alert_body("fp-uprace", unresolved=True)).json()
    assert first["status"] == ALERT_UNRESOLVED and first["deduped"] is False
    # Age the unresolved lifecycle so the upgrade path is also "stale".
    store._conn.execute(
        "UPDATE alert_lifecycles SET created_at = ?, latest_alert_at = ? WHERE fingerprint = ?",
        ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", "fp-uprace"))
    store._conn.commit()

    body = _alert_body("fp-uprace")
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(
            lambda _: client.post("/api/v1/diagnoses", json=body), range(5)))

    assert all(r.status_code == 201 for r in responses)
    started = [r.json() for r in responses if r.json().get("deduped") is False]
    assert len(started) == 1, responses
    assert len(client.get("/api/v1/diagnoses").json()) == 1
    lifecycles = store.list_alert_lifecycles("fp-uprace")
    assert len(lifecycles) == 1 and lifecycles[0]["state"] == ALERT_OPEN
    assert lifecycles[0]["diagnosis_id"] == started[0]["diagnosis_id"]


def test_duplicate_delivery_does_not_consume_rate_limit():
    """Dedup runs before the limiter: repeats of one alert cannot exhaust the
    quota that exists to bound *new* investigations."""
    cfg = Config()
    cfg.alert_rate_limit_per_minute = 1
    client = make_client(cfg)
    first = client.post("/api/v1/diagnoses", json=_alert_body("fp-quota")).json()
    _wait(client, first["diagnosis_id"])
    for _ in range(5):
        resp = client.post("/api/v1/diagnoses", json=_alert_body("fp-quota"))
        assert resp.status_code == 201
        assert resp.json()["deduped"] is True
    assert client.get("/api/v1/diagnoses").json() != []


def test_unresolved_alerts_do_not_consume_rate_limit():
    cfg = Config()
    cfg.alert_rate_limit_per_minute = 1
    client = make_client(cfg)
    for i in range(5):
        resp = client.post("/api/v1/diagnoses",
                           json=_alert_body(f"fp-unres-{i}", unresolved=True))
        assert resp.status_code == 201
        assert resp.json()["status"] == ALERT_UNRESOLVED


def test_same_fingerprint_new_starts_at_starts_new_lifecycle():
    """A missed resolved must not hide the next firing of the same alert."""
    client, store = make_client_with_store()
    first = client.post("/api/v1/diagnoses", json=_alert_body("fp-inst", starts_at="A")).json()
    _wait(client, first["diagnosis_id"])
    retried = client.post("/api/v1/diagnoses", json=_alert_body("fp-inst", starts_at="A")).json()
    assert retried["deduped"] is True
    assert retried["diagnosis_id"] == first["diagnosis_id"]

    second = client.post("/api/v1/diagnoses", json=_alert_body("fp-inst", starts_at="B")).json()
    assert second["deduped"] is False
    assert second["diagnosis_id"] != first["diagnosis_id"]
    lifecycles = store.list_alert_lifecycles("fp-inst")
    assert [item["state"] for item in lifecycles] == [ALERT_CLOSED, ALERT_OPEN]
    # The old (unresolved) instance stays closed even if its resolved arrives late.
    late = client.post("/api/v1/diagnoses",
                       json=_alert_body("fp-inst", status="resolved", starts_at="A")).json()
    assert late["closed"] is False


def test_worker_thread_start_failure_is_visible_not_silently_queued():
    """If the worker cannot start, the diagnosis must not sit in "queued"
    forever: it is marked failed and stays linked, so the alert has a visible
    outcome and a repeat delivery dedups onto it (design §3.2)."""
    client, store = make_client_with_store()
    raw = TestClient(client.app, raise_server_exceptions=False)

    class _BoomPool:
        """Stands in for the worker pool; submit() fails like a shut-down pool."""

        def submit(self, *args, **kwargs):
            raise RuntimeError("no workers")

    # Patch only the app's worker pool; the agent is dispatched through it now.
    with patch.object(client.app.state, "executor", _BoomPool()):
        resp = raw.post("/api/v1/diagnoses", json=_alert_body("fp-thread"))
    assert resp.status_code == 500
    rows = store.list(10)
    assert len(rows) == 1 and rows[0].status == "failed"
    assert "worker thread" in (rows[0].error or "")
    lifecycles = store.list_alert_lifecycles("fp-thread")
    assert len(lifecycles) == 1
    assert lifecycles[0]["diagnosis_id"] == rows[0].diagnosis_id

    # A later delivery is a dedup onto the visible failed outcome, not a black hole.
    again = client.post("/api/v1/diagnoses", json=_alert_body("fp-thread"))
    assert again.status_code == 201
    assert again.json()["deduped"] is True
    assert again.json()["diagnosis_id"] == rows[0].diagnosis_id
