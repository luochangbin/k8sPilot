"""SQLite persistence tests: sessions survive a store rebuild (= restart)."""

import json

from app.models import DiagnosisRequest, DiagnosisResult, ResourceRef, Trigger
from app.store import SessionStore

RES = ResourceRef(kind="Pod", namespace="payment", name="payment-api", uid="u1")


def make_req(**kw) -> DiagnosisRequest:
    return DiagnosisRequest(trigger=Trigger.manual, resource=RES, **kw)


def test_in_memory_store_roundtrip():
    s = SessionStore()
    d = s.create(make_req())
    assert s.get(d.diagnosis_id) is not None
    assert s.get("missing") is None
    s.update(d.diagnosis_id, status="failed", error="boom")
    got = s.get(d.diagnosis_id)
    assert got.status == "failed"
    assert got.error == "boom"


def test_history_survives_restart(tmp_path):
    db = str(tmp_path / "diagnoses.db")

    s1 = SessionStore(db)
    d1 = s1.create(make_req(eval_run_id="run-1", case_id="case-oom", case_version="1", attempt_index=0))
    d2 = s1.create(make_req())
    s1.update(d1.diagnosis_id, status="completed",
              result=DiagnosisResult(symptom="s", root_cause="r", root_cause_code="CONTAINER_OOMKILLED"))

    # A brand new store over the same file = process restart.
    s2 = SessionStore(db)
    got = s2.get(d1.diagnosis_id)
    assert got is not None
    assert got.status == "completed"
    assert got.result.root_cause_code == "CONTAINER_OOMKILLED"
    assert got.case_id == "case-oom" and got.eval_run_id == "run-1"

    history = s2.list()
    assert len(history) == 2
    ids = [h.diagnosis_id for h in history]
    assert d1.diagnosis_id in ids and d2.diagnosis_id in ids


def test_list_is_newest_first(tmp_path):
    db = str(tmp_path / "d.db")
    s = SessionStore(db)
    a = s.create(make_req())
    b = s.create(make_req())
    c = s.create(make_req())
    ids = [x.diagnosis_id for x in s.list(limit=10)]
    assert ids.index(c.diagnosis_id) < ids.index(b.diagnosis_id) < ids.index(a.diagnosis_id)


def test_update_does_not_clobber_result(tmp_path):
    s = SessionStore(str(tmp_path / "d.db"))
    d = s.create(make_req())
    s.update(d.diagnosis_id, status="completed",
             result=DiagnosisResult(symptom="s", root_cause="r"))
    got = s.update(d.diagnosis_id, error="later")
    assert got.result is not None
    assert got.result.root_cause == "r"


def test_request_cancel_is_idempotent_and_terminal_aware():
    from app.models import DiagnosisRequest, ResourceRef, Trigger

    store = SessionStore()
    req = DiagnosisRequest(trigger=Trigger.manual,
                           resource=ResourceRef(kind="Pod", namespace="ns", name="p", uid="u"))
    d = store.create(req)

    assert store.is_cancelled(d.diagnosis_id) is False
    assert store.request_cancel(d.diagnosis_id) == "ok"
    assert store.is_cancelled(d.diagnosis_id) is True
    assert store.request_cancel(d.diagnosis_id) == "ok"  # idempotent
    assert store.request_cancel("diag_missing") == "not_found"

    store.update(d.diagnosis_id, status="completed")
    assert store.request_cancel(d.diagnosis_id) == "terminal"


def test_mark_orphans_failed_only_touches_non_terminal_sessions():
    from app.models import DiagnosisRequest, ResourceRef, Trigger

    store = SessionStore()
    made = {}
    for status in ("queued", "investigating", "completed", "failed"):
        d = store.create(DiagnosisRequest(
            trigger=Trigger.manual,
            resource=ResourceRef(kind="Pod", namespace="ns", name=status, uid=status)))
        store.update(d.diagnosis_id, status=status)
        made[status] = d.diagnosis_id

    assert store.mark_orphans_failed("agent service restarted during diagnosis") == 2

    for status in ("queued", "investigating"):
        row = store.get(made[status])
        assert row.status == "failed"
        assert row.failure_reason == "agent_restarted"
        assert "restarted" in (row.error or "")
    assert store.get(made["completed"]).status == "completed"
    assert store.get(made["failed"]).status == "failed"
