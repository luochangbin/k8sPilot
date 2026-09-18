"""Diagnosis Center backend tests: sessions keyset/filters, alert projection,
read receipts/notifications, unresolved alerts, timeline projection (§3, §6.1)."""

import json
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from app.models import DiagnosisRequest, ResourceRef, Trigger
from app.store import ALERT_OPEN, ALERT_UNRESOLVED, SessionStore

from .fakes import ScriptedLLM, StubConnector

VIEWER = str(uuid.uuid4())


def _req(name="nginx", uid="uid-1", kind="Pod", namespace="default", trigger=Trigger.manual,
         eval_run_id=None):
    return DiagnosisRequest(
        trigger=trigger,
        resource=ResourceRef(kind=kind, namespace=namespace, name=name, uid=uid),
        eval_run_id=eval_run_id,
    )


def _seed_diagnosis(store, *, status="completed", result=None, trigger=Trigger.manual,
                    eval_run_id=None, name="nginx", uid="uid-1", namespace="default"):
    d = store.create(_req(name=name, uid=uid, trigger=trigger, eval_run_id=eval_run_id,
                          namespace=namespace))
    store.update(d.diagnosis_id, status=status, result=result)
    return store.get(d.diagnosis_id)


def _result(insufficient=False):
    from app.models import DiagnosisResult
    return DiagnosisResult(symptom="s", root_cause=None if insufficient else "r",
                           root_cause_code=None if insufficient else "CONTAINER_OOMKILLED",
                           confidence="high", insufficient_evidence=insufficient)


def make_center(tmp_path):
    cfg = Config()
    cfg.trace_dir = str(tmp_path / "trace")
    Path(cfg.trace_dir).mkdir(parents=True, exist_ok=True)
    store = SessionStore()
    app = create_app(cfg=cfg, store=store, connector=StubConnector(), llm=ScriptedLLM([]))
    return TestClient(app), store, cfg


def _prime(store, *viewers):
    """Establish the notification baseline before seeding what must count as new.

    A viewer's baseline is the first time the server sees that viewer; anything
    created earlier is history and is intentionally not "unread" (§ new-viewer).
    """
    for viewer in viewers:
        store.ensure_viewer(viewer)


# ---- sessions ----

def test_sessions_order_filters_and_unread(tmp_path):
    client, store, _ = make_center(tmp_path)
    _prime(store, VIEWER)
    manual = _seed_diagnosis(store, name="manual-pod", uid="uid-m")
    alert_ok = _seed_diagnosis(store, trigger=Trigger.alert, name="alert-pod", uid="uid-a",
                               result=_result())
    alert_bad = _seed_diagnosis(store, trigger=Trigger.alert, name="alert-pod2", uid="uid-b",
                                result=_result(insufficient=True))

    body = client.get("/api/v1/diagnosis-center/sessions").json()
    ids = [i["diagnosis_id"] for i in body["items"]]
    assert ids == [alert_bad.diagnosis_id, alert_ok.diagnosis_id, manual.diagnosis_id]
    assert all(i["unread"] is None for i in body["items"])
    assert body["items"][0]["summary"]["insufficient_evidence"] is True

    filtered = client.get("/api/v1/diagnosis-center/sessions?uid=uid-a").json()
    assert [i["diagnosis_id"] for i in filtered["items"]] == [alert_ok.diagnosis_id]

    with_viewer = client.get(
        f"/api/v1/diagnosis-center/sessions?viewer_id={VIEWER}").json()
    unread = {i["diagnosis_id"]: i["unread"] for i in with_viewer["items"]}
    assert unread[alert_ok.diagnosis_id] is True
    assert unread[alert_bad.diagnosis_id] is True
    # Manual diagnoses are never reported as unread (auto-diagnoses only).
    assert unread[manual.diagnosis_id] is False


def test_sessions_invalid_inputs_are_422(tmp_path):
    client, _, _ = make_center(tmp_path)
    assert client.get("/api/v1/diagnosis-center/sessions?status=bogus").status_code == 422
    assert client.get("/api/v1/diagnosis-center/sessions?trigger=bogus").status_code == 422
    assert client.get("/api/v1/diagnosis-center/sessions?since=not-a-time").status_code == 422
    assert client.get("/api/v1/diagnosis-center/sessions?limit=0").status_code == 422
    assert client.get("/api/v1/diagnosis-center/sessions?limit=101").status_code == 422
    assert client.get("/api/v1/diagnosis-center/sessions?viewer_id=nope").status_code == 422
    # Well-formed but not a canonical v4: read receipts would be ambiguous.
    assert client.get(
        "/api/v1/diagnosis-center/sessions"
        "?viewer_id=00000000-0000-0000-0000-000000000001").status_code == 422
    assert client.get(
        "/api/v1/diagnosis-center/sessions"
        "?viewer_id=12345678123456781234567812345678").status_code == 422


def test_sessions_cursor_is_filter_bound(tmp_path):
    client, store, _ = make_center(tmp_path)
    for i in range(3):
        _seed_diagnosis(store, name=f"pod-{i}", uid=f"uid-{i}")

    page1 = client.get("/api/v1/diagnosis-center/sessions?limit=1").json()
    assert len(page1["items"]) == 1 and page1["next_cursor"]
    page2 = client.get(
        f"/api/v1/diagnosis-center/sessions?limit=1&after={page1['next_cursor']}").json()
    assert len(page2["items"]) == 1
    assert page2["items"][0]["diagnosis_id"] != page1["items"][0]["diagnosis_id"]

    # Changing a filter must invalidate the cursor.
    resp = client.get(
        f"/api/v1/diagnosis-center/sessions?limit=1&trigger=manual&after={page1['next_cursor']}")
    assert resp.status_code == 422
    assert client.get("/api/v1/diagnosis-center/sessions?after=%%%bad").status_code == 422


# ---- alert projection ----

def test_sessions_include_alert_projection(tmp_path):
    client, store, _ = make_center(tmp_path)
    d = _seed_diagnosis(store, trigger=Trigger.alert, result=_result())
    lifecycle, _ = store.claim_active_alert_lifecycle(
        fingerprint="fp-1", state=ALERT_OPEN, alertname="PodCrashLooping", starts_at="S")
    store.link_alert_diagnosis(lifecycle["id"], d.diagnosis_id)

    item = client.get("/api/v1/diagnosis-center/sessions").json()["items"][0]
    assert item["alert"]["fingerprint"] == "fp-1"
    assert item["alert"]["alertname"] == "PodCrashLooping"
    assert item["alert"]["state"] == ALERT_OPEN
    # legacy detail also exposes the nullable alert projection
    detail = client.get(f"/api/v1/diagnoses/{d.diagnosis_id}").json()
    assert detail["alert"]["fingerprint"] == "fp-1"
    assert client.get("/api/v1/diagnoses/diag_missing").status_code == 404


# ---- notifications / read ----

def test_read_receipt_and_unread_count(tmp_path):
    client, store, _ = make_center(tmp_path)
    other = str(uuid.uuid4())
    _prime(store, VIEWER, other)
    alert_ok = _seed_diagnosis(store, trigger=Trigger.alert, result=_result())
    alert_insufficient = _seed_diagnosis(store, trigger=Trigger.alert,
                                         result=_result(insufficient=True), name="p2", uid="u2")
    alert_failed = _seed_diagnosis(store, status="failed", trigger=Trigger.alert,
                                   name="p3", uid="u3")
    _seed_diagnosis(store, trigger=Trigger.alert, result=_result(), name="p4", uid="u4",
                    eval_run_id="eval-1")          # eval run: excluded
    _seed_diagnosis(store, result=_result(), name="p5", uid="u5")  # manual: excluded
    running = _seed_diagnosis(store, status="queued", trigger=Trigger.alert,
                              name="p6", uid="u6", result=None)

    notes = client.get(f"/api/v1/diagnosis-center/notifications?viewer_id={VIEWER}").json()
    assert notes["unread_count"] == 3
    assert len(notes["items"]) == 3
    assert {i["diagnosis_id"] for i in notes["items"]} == {
        alert_ok.diagnosis_id, alert_insufficient.diagnosis_id, alert_failed.diagnosis_id}

    # running diagnosis cannot be read yet
    assert client.post(f"/api/v1/diagnosis-center/sessions/{running.diagnosis_id}/read",
                       json={"viewer_id": VIEWER}).status_code == 409
    assert client.post("/api/v1/diagnosis-center/sessions/diag_missing/read",
                       json={"viewer_id": VIEWER}).status_code == 404
    assert client.post(f"/api/v1/diagnosis-center/sessions/{alert_ok.diagnosis_id}/read",
                       json={"viewer_id": "not-a-uuid"}).status_code == 422

    # idempotent read, then count drops
    for _ in range(2):
        resp = client.post(f"/api/v1/diagnosis-center/sessions/{alert_ok.diagnosis_id}/read",
                           json={"viewer_id": VIEWER})
        assert resp.status_code == 200 and resp.json() == {"id": alert_ok.diagnosis_id,
                                                           "read": True}
    assert client.get(
        f"/api/v1/diagnosis-center/notifications?viewer_id={VIEWER}").json()["unread_count"] == 2
    # viewer isolation: a different viewer with the same baseline sees the same 3
    assert client.get(
        f"/api/v1/diagnosis-center/notifications?viewer_id={other}").json()["unread_count"] == 3


# ---- unresolved alerts ----

def test_unresolved_alerts_area(tmp_path):
    client, store, _ = make_center(tmp_path)
    row, _ = store.claim_active_alert_lifecycle(
        fingerprint="fp-unresolved", state="unresolved_target", alertname="Mystery",
        labels={"foo": "bar"})
    linked = _seed_diagnosis(store, trigger=Trigger.alert, result=_result())
    lifecycle, _ = store.claim_active_alert_lifecycle(
        fingerprint="fp-linked", state=ALERT_OPEN, alertname="Linked")
    store.link_alert_diagnosis(lifecycle["id"], linked.diagnosis_id)

    body = client.get("/api/v1/diagnosis-center/unresolved-alerts").json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == row["id"] and item["state"] == "unresolved_target"
    assert "unread" not in item and "diagnosis_id" not in item
    assert client.get(
        "/api/v1/diagnosis-center/unresolved-alerts?limit=0").status_code == 422


# ---- timeline ----

def _write_trace(trace_dir: Path, diagnosis_id: str, *, half_line: bool = False) -> None:
    path = trace_dir / f"{diagnosis_id}.jsonl"
    events = [
        {"kind": "diagnosis_root", "name": "diagnosis",
         "attributes": {"status": "started", "trace_id": "t"}},
        {"kind": "tool_call", "name": "tool.inspect",
         "attributes": {"tool": "inspect", "args_summary": "{\"secret\":\"x\"}",
                        "duration_ms": 12.5}},
        None,  # placeholder for a malformed line
        {"kind": "tool_call", "name": "tool.logs", "error": "connector boom",
         "attributes": {"tool": "logs", "duration_ms": 3.0}},
        {"kind": "llm_call", "name": "llm.call",
         "attributes": {"duration_ms": 100.0, "prompt_tokens": 10}},
        {"kind": "llm_final", "name": "llm.final", "attributes": {"schema_valid": True}},
        {"kind": "diagnosis_root", "name": "diagnosis",
         "attributes": {"status": "completed", "duration_ms": 200.0}},
    ]
    lines = []
    for event in events:
        lines.append("{not json" if event is None else json.dumps(event))
    payload = "\n".join(lines) + "\n"
    if half_line:
        payload += json.dumps({"kind": "tool_call", "name": "tool.partial"})  # no newline
    path.write_text(payload, encoding="utf-8")


def test_timeline_projection_gap_and_half_line(tmp_path):
    client, store, cfg = make_center(tmp_path)
    d = _seed_diagnosis(store, status="completed", result=_result())
    _write_trace(Path(cfg.trace_dir), d.diagnosis_id, half_line=True)

    page = client.get(f"/api/v1/diagnoses/{d.diagnosis_id}/timeline").json()
    kinds = [i["kind"] for i in page["items"]]
    assert kinds == ["diagnosis_started", "tool_completed", "tool_completed", "llm_call",
                     "result_drafted", "diagnosis_completed"]
    assert page["gap"] is True                      # malformed line skipped
    path = Path(cfg.trace_dir) / f"{d.diagnosis_id}.jsonl"
    total = path.stat().st_size
    partial = json.dumps({"kind": "tool_call", "name": "tool.partial"}).encode()
    # The incomplete tail line is not consumed: the offset stops at its start.
    assert page["has_more"] is True
    assert page["next_after"] == total - len(partial)
    for item in page["items"]:
        assert set(item) <= {"id", "seq", "timestamp", "kind", "title", "status",
                             "duration_ms", "failure_layer"}
    text = json.dumps(page)
    assert "args_summary" not in text and "connector boom" not in text
    # Resuming from the offset yields nothing new until the line is complete.
    again = client.get(
        f"/api/v1/diagnoses/{d.diagnosis_id}/timeline?after={page['next_after']}").json()
    assert again["items"] == [] and again["has_more"] is True


def test_timeline_pagination_and_errors(tmp_path):
    client, store, cfg = make_center(tmp_path)
    d = _seed_diagnosis(store, status="completed", result=_result())
    _write_trace(Path(cfg.trace_dir), d.diagnosis_id)

    first = client.get(f"/api/v1/diagnoses/{d.diagnosis_id}/timeline?limit=2").json()
    assert len(first["items"]) == 2 and first["has_more"] is True
    second = client.get(
        f"/api/v1/diagnoses/{d.diagnosis_id}/timeline?limit=2&after={first['next_after']}").json()
    assert len(second["items"]) == 2
    # Bad lines may be skipped, so the first resumed seq is at or after next_after.
    assert second["items"][0]["seq"] >= first["next_after"]
    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})

    total = (Path(cfg.trace_dir) / f"{d.diagnosis_id}.jsonl").stat().st_size
    assert client.get(
        f"/api/v1/diagnoses/{d.diagnosis_id}/timeline?after={total + 5}").status_code == 422
    assert client.get(
        f"/api/v1/diagnoses/{d.diagnosis_id}/timeline?after=-1").status_code == 422
    assert client.get("/api/v1/diagnoses/diag_missing/timeline").status_code == 404


def test_timeline_pending_and_unavailable(tmp_path):
    client, store, cfg = make_center(tmp_path)
    d = _seed_diagnosis(store, status="completed", result=_result())
    # no trace file yet -> pending
    assert client.get(
        f"/api/v1/diagnoses/{d.diagnosis_id}/timeline").json()["available"] == "pending"

    cfg.trace_dir = ""
    assert client.get(
        f"/api/v1/diagnoses/{d.diagnosis_id}/timeline").json()["available"] == "unavailable"

def test_timeline_malformed_attributes_is_gap_not_500(tmp_path):
    client, store, cfg = make_center(tmp_path)
    d = _seed_diagnosis(store, status="completed", result=_result())
    path = Path(cfg.trace_dir) / f"{d.diagnosis_id}.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"kind": "diagnosis_root", "attributes": {"status": "started"}}),
                json.dumps({"kind": "tool_call", "attributes": "oops"}),
                json.dumps({"kind": "diagnosis_root", "attributes": {"status": "completed"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    resp = client.get(f"/api/v1/diagnoses/{d.diagnosis_id}/timeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["gap"] is True
    assert [i["kind"] for i in body["items"]] == ["diagnosis_started", "diagnosis_completed"]


def test_name_filter_is_fuzzy_and_namespace_scoped(tmp_path):
    client, store, _ = make_center(tmp_path)
    _seed_diagnosis(store, name="payment-api", uid="u1")
    _seed_diagnosis(store, name="payment-worker", uid="u2")
    _seed_diagnosis(store, name="other-api", uid="u3", namespace="other")

    fuzzy = client.get("/api/v1/diagnosis-center/sessions?name=payment").json()
    assert {i["resource"]["name"] for i in fuzzy["items"]} == {"payment-api", "payment-worker"}

    substring = client.get("/api/v1/diagnosis-center/sessions?name=api").json()
    assert {i["resource"]["name"] for i in substring["items"]} == {"payment-api", "other-api"}

    scoped = client.get("/api/v1/diagnosis-center/sessions?name=api&namespace=default").json()
    assert [i["resource"]["name"] for i in scoped["items"]] == ["payment-api"]

    # LIKE wildcards in user input are escaped (literal match, no matches here).
    assert client.get("/api/v1/diagnosis-center/sessions?name=%25").json()["items"] == []


def test_namespaces_endpoint_lists_real_namespaces_only(tmp_path):
    client, store, _ = make_center(tmp_path)
    _seed_diagnosis(store, name="a", uid="u1", namespace="team-b")
    _seed_diagnosis(store, name="b", uid="u2", namespace="team-a")
    _seed_diagnosis(store, name="c", uid="u3", namespace="team-b")
    # Eval fixtures (eval_run_id set) must not pollute the dropdown.
    _seed_diagnosis(store, name="d", uid="u4", namespace="eval-pod-oomkilled-001",
                    eval_run_id="run-1")
    _seed_diagnosis(store, name="e", uid="u5", namespace="team-a", eval_run_id="run-1")

    assert client.get("/api/v1/diagnosis-center/namespaces").json() == {
        "items": ["team-a", "team-b"]
    }


def test_mark_all_read_is_scoped_and_leaves_new_diagnoses_unread(tmp_path):
    client, store, _ = make_center(tmp_path)
    other = str(uuid.uuid4())
    _prime(store, VIEWER, other)
    _seed_diagnosis(store, trigger=Trigger.alert, result=_result())
    _seed_diagnosis(store, trigger=Trigger.alert, result=_result(insufficient=True),
                    name="p2", uid="u2")
    _seed_diagnosis(store, result=_result(), name="p3", uid="u3")          # manual: excluded
    _seed_diagnosis(store, trigger=Trigger.alert, eval_run_id="eval-1",
                    name="p4", uid="u4")                                    # eval run: excluded

    resp = client.post("/api/v1/diagnosis-center/notifications/read-all",
                       json={"viewer_id": VIEWER})
    assert resp.status_code == 200 and resp.json()["read"] == 2
    assert client.get(
        f"/api/v1/diagnosis-center/notifications?viewer_id={VIEWER}").json()["unread_count"] == 0

    # Idempotent: nothing left to mark.
    again = client.post("/api/v1/diagnosis-center/notifications/read-all",
                        json={"viewer_id": VIEWER})
    assert again.json()["read"] == 0

    # A diagnosis completing afterwards stays unread.
    _seed_diagnosis(store, trigger=Trigger.alert, result=_result(), name="p5", uid="u5")
    assert client.get(
        f"/api/v1/diagnosis-center/notifications?viewer_id={VIEWER}").json()["unread_count"] == 1

    # Viewer isolation (same baseline).
    assert client.get(
        f"/api/v1/diagnosis-center/notifications?viewer_id={other}").json()["unread_count"] == 3

    # Invalid viewer is rejected.
    assert client.post("/api/v1/diagnosis-center/notifications/read-all",
                       json={"viewer_id": "bad"}).status_code == 422


def test_unread_scope_matches_read_all(tmp_path):
    client, store, _ = make_center(tmp_path)
    _prime(store, VIEWER)
    manual = _seed_diagnosis(store, name="m", uid="u1")
    alert_old = _seed_diagnosis(store, trigger=Trigger.alert, result=_result(),
                                name="a1", uid="u2")
    eval_row = _seed_diagnosis(store, trigger=Trigger.alert, eval_run_id="eval-1",
                               result=_result(), name="a2", uid="u3")
    running = _seed_diagnosis(store, trigger=Trigger.alert, status="queued",
                              result=None, name="a3", uid="u4")

    def unread_map():
        body = client.get(
            f"/api/v1/diagnosis-center/sessions?viewer_id={VIEWER}&limit=50").json()
        return {i["diagnosis_id"]: i["unread"] for i in body["items"]}

    before = unread_map()
    assert before[alert_old.diagnosis_id] is True
    assert before[manual.diagnosis_id] is False
    assert before[eval_row.diagnosis_id] is False
    assert before[running.diagnosis_id] is False

    # read-all clears exactly what the list reports as unread: nothing is stuck.
    resp = client.post("/api/v1/diagnosis-center/notifications/read-all",
                       json={"viewer_id": VIEWER})
    assert resp.json()["read"] == 1
    assert all(value is False for value in unread_map().values())


def test_new_viewer_does_not_inherit_history_as_unread(tmp_path):
    """Opening the Center in a fresh browser must not surface the whole backlog."""
    client, store, _ = make_center(tmp_path)
    _seed_diagnosis(store, trigger=Trigger.alert, result=_result())
    _seed_diagnosis(store, trigger=Trigger.alert, result=_result(), name="p2", uid="u2")

    fresh = str(uuid.uuid4())
    assert client.get(
        f"/api/v1/diagnosis-center/notifications?viewer_id={fresh}").json()["unread_count"] == 0

    # Diagnoses finishing after the first sighting are unread for that viewer.
    _seed_diagnosis(store, trigger=Trigger.alert, result=_result(), name="p3", uid="u3")
    assert client.get(
        f"/api/v1/diagnosis-center/notifications?viewer_id={fresh}").json()["unread_count"] == 1


def test_unresolved_alerts_are_pending_not_unread(tmp_path):
    """Unresolved alerts are pending work: counted separately, absent from the
    unread feed, and cleared when the alert resolves."""
    client, store, _ = make_center(tmp_path)
    _prime(store, VIEWER)
    store.claim_active_alert_lifecycle(
        fingerprint="fp-note", state=ALERT_UNRESOLVED, alertname="Mystery")

    notes = client.get(f"/api/v1/diagnosis-center/notifications?viewer_id={VIEWER}").json()
    assert notes["unread_diagnosis_count"] == 0
    assert notes["pending_alert_count"] == 1
    assert notes["items"] == []

    # Still visible in its own section list.
    unresolved = client.get("/api/v1/diagnosis-center/unresolved-alerts").json()
    assert len(unresolved["items"]) == 1
    assert unresolved["items"][0]["alertname"] == "Mystery"

    store.close_alert_lifecycle("fp-note")
    after = client.get(f"/api/v1/diagnosis-center/notifications?viewer_id={VIEWER}").json()
    assert after["pending_alert_count"] == 0


def test_sessions_unread_filter_returns_only_unread(tmp_path):
    client, store, _ = make_center(tmp_path)
    _prime(store, VIEWER)
    unread = _seed_diagnosis(store, trigger=Trigger.alert, result=_result())
    seen = _seed_diagnosis(store, trigger=Trigger.alert, result=_result(), name="p2", uid="u2")
    assert client.post(f"/api/v1/diagnosis-center/sessions/{seen.diagnosis_id}/read",
                       json={"viewer_id": VIEWER}).status_code == 200

    body = client.get(
        f"/api/v1/diagnosis-center/sessions?viewer_id={VIEWER}&unread=true").json()
    assert [item["diagnosis_id"] for item in body["items"]] == [unread.diagnosis_id]
    assert client.get("/api/v1/diagnosis-center/sessions?unread=true").status_code == 422


def test_unread_view_orders_by_completion_not_creation(tmp_path):
    """A diagnosis created early but finished late must not be buried behind
    newer, already-read rows in the unread entry point."""
    client, store, _ = make_center(tmp_path)
    _prime(store, VIEWER)
    older = _seed_diagnosis(store, trigger=Trigger.alert, status="queued", result=None,
                            name="old", uid="u1")
    newer = _seed_diagnosis(store, trigger=Trigger.alert, result=_result(),
                            name="new", uid="u2")
    assert client.post(f"/api/v1/diagnosis-center/sessions/{newer.diagnosis_id}/read",
                       json={"viewer_id": VIEWER}).status_code == 200
    # The older one finishes last, after the newer one was already read.
    store.update(older.diagnosis_id, status="completed", result=_result())

    page = client.get(f"/api/v1/diagnosis-center/sessions?viewer_id={VIEWER}&unread=true").json()
    assert [item["diagnosis_id"] for item in page["items"]] == [older.diagnosis_id]

    # Keyset pagination follows the same ordering (updated_at, id).
    both = _seed_diagnosis(store, trigger=Trigger.alert, result=_result(), name="newer2", uid="u3")
    first = client.get(
        f"/api/v1/diagnosis-center/sessions?viewer_id={VIEWER}&unread=true&limit=1").json()
    assert [item["diagnosis_id"] for item in first["items"]] == [both.diagnosis_id]
    second = client.get(
        "/api/v1/diagnosis-center/sessions"
        f"?viewer_id={VIEWER}&unread=true&limit=1&after={first['next_cursor']}").json()
    assert [item["diagnosis_id"] for item in second["items"]] == [older.diagnosis_id]

    # The unread view sorts by updated_at, so its cursor must not be accepted by
    # the default (created_at) list: the fingerprint covers the unread flag.
    cross = client.get(
        "/api/v1/diagnosis-center/sessions"
        f"?viewer_id={VIEWER}&limit=1&after={first['next_cursor']}")
    assert cross.status_code == 422


def test_completed_after_baseline_is_unread_even_if_created_before(tmp_path):
    """A queued diagnosis created before the viewer baseline but finishing after
    it is genuinely new: all three query paths must agree (terminal time)."""
    client, store, _ = make_center(tmp_path)
    queued = _seed_diagnosis(store, trigger=Trigger.alert, status="queued", result=None,
                             name="queued", uid="u1")
    _prime(store, VIEWER)  # baseline established AFTER creation
    store.update(queued.diagnosis_id, status="completed", result=_result())

    notes = client.get(f"/api/v1/diagnosis-center/notifications?viewer_id={VIEWER}").json()
    assert notes["unread_diagnosis_count"] == 1
    assert notes["unread_count"] == 1
    assert [i["diagnosis_id"] for i in notes["items"]] == [queued.diagnosis_id]

    sessions = client.get(
        f"/api/v1/diagnosis-center/sessions?viewer_id={VIEWER}&unread=true").json()
    assert [i["diagnosis_id"] for i in sessions["items"]] == [queued.diagnosis_id]

    plain = client.get(f"/api/v1/diagnosis-center/sessions?viewer_id={VIEWER}").json()
    assert {i["diagnosis_id"]: i["unread"] for i in plain["items"]}[queued.diagnosis_id] is True


def test_unread_diagnoses_and_pending_alerts_are_independent(tmp_path):
    """Pending (unresolved) alerts are not unread, are not in the notification
    feed, and are unaffected by read receipts."""
    client, store, _ = make_center(tmp_path)
    _prime(store, VIEWER)
    alert_ok = _seed_diagnosis(store, trigger=Trigger.alert, result=_result())
    store.claim_active_alert_lifecycle(
        fingerprint="fp-pending", state=ALERT_UNRESOLVED, alertname="Mystery")

    notes = client.get(f"/api/v1/diagnosis-center/notifications?viewer_id={VIEWER}").json()
    assert notes["unread_diagnosis_count"] == 1
    assert notes["pending_alert_count"] == 1
    # The feed only carries diagnoses now.
    assert [i["kind"] for i in notes["items"]] == ["diagnosis"]

    read = client.post("/api/v1/diagnosis-center/notifications/read-all",
                       json={"viewer_id": VIEWER}).json()
    assert read["read"] == 1

    after = client.get(f"/api/v1/diagnosis-center/notifications?viewer_id={VIEWER}").json()
    assert after["unread_diagnosis_count"] == 0
    assert after["pending_alert_count"] == 1  # read receipts never touch pending alerts
    pending, _ = store.list_unresolved_alerts(limit=10)
    assert len(pending) == 1


def test_unread_cursor_is_bound_to_the_viewer(tmp_path):
    client, store, _ = make_center(tmp_path)
    other = str(uuid.uuid4())
    _prime(store, VIEWER, other)
    _seed_diagnosis(store, trigger=Trigger.alert, result=_result(), name="a", uid="u1")
    _seed_diagnosis(store, trigger=Trigger.alert, result=_result(), name="b", uid="u2")

    page1 = client.get(
        f"/api/v1/diagnosis-center/sessions?viewer_id={VIEWER}&unread=true&limit=1").json()
    cursor = page1["next_cursor"]
    assert cursor

    # Same viewer: fine. Different viewer: rejected (their unread set differs).
    assert client.get(
        "/api/v1/diagnosis-center/sessions"
        f"?viewer_id={VIEWER}&unread=true&limit=1&after={cursor}").status_code == 200
    assert client.get(
        "/api/v1/diagnosis-center/sessions"
        f"?viewer_id={other}&unread=true&limit=1&after={cursor}").status_code == 422

    # Non-unread cursors stay viewer-independent.
    plain = client.get("/api/v1/diagnosis-center/sessions?limit=1").json()
    plain_cursor = plain["next_cursor"]
    assert client.get(
        "/api/v1/diagnosis-center/sessions"
        f"?viewer_id={VIEWER}&limit=1&after={plain_cursor}").status_code == 200
