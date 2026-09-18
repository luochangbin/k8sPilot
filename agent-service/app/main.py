"""FastAPI entrypoint for the agent service."""

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from .agent import Agent
from .center import (
    CursorError,
    VALID_STATUSES,
    VALID_TRIGGERS,
    decode_cursor,
    encode_cursor,
    filter_fingerprint,
    parse_iso,
    validate_enum,
    validate_viewer_id,
)
from .config import Config
from .connector import ConnectorClient
from .llm import (
    ExecutionContext,
    OpenAILLM,
    ProfileError,
    UnknownProfileError,
    resolve_model,
)
from .models import DIAGNOSABLE_KINDS, AlertStatus, DiagnosisRequest
from .store import ALERT_CLOSED, ALERT_OPEN, ALERT_UNRESOLVED, SessionStore
from .timeline import TimelineRangeError, read_timeline


class _AlertRateLimiter:
    """Sliding-window limiter for alert-triggered requests (Phase 5 storm guard)."""

    def __init__(self, per_minute: int) -> None:
        self._per_minute = per_minute
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        if self._per_minute <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            while self._hits and now - self._hits[0] > 60.0:
                self._hits.popleft()
            if len(self._hits) >= self._per_minute:
                return False
            self._hits.append(now)
            return True


def create_app(cfg: Optional[Config] = None, store: Optional[SessionStore] = None,
               connector: Optional[ConnectorClient] = None, llm: Any = None,
               execution_resolver: Any = None) -> FastAPI:
    """App factory with injectable dependencies for testing."""
    cfg = cfg or Config()
    store = store or SessionStore(cfg.db_path or None)
    connector = connector or ConnectorClient(cfg.connector_base_url, cfg.connector_timeout)
    llm_provided = llm is not None
    llm = llm or OpenAILLM(cfg)

    def _metadata(resolved) -> dict[str, Any]:
        return {**resolved.public_metadata(), "config_fingerprint": resolved.config_fingerprint()}

    def _default_execution() -> ExecutionContext:
        # An injected client is the default-profile client (tests / embedding);
        # otherwise resolve the configured default profile.
        if llm_provided:
            return ExecutionContext(llm=llm,
                                    metadata={"resolved_profile": "default", "provider": "injected"})
        resolved = resolve_model(cfg, None)
        client = llm if resolved.provider == "env" else OpenAILLM.from_resolved(resolved)
        return ExecutionContext(llm=client, metadata=_metadata(resolved))

    def _requested_execution(name: str) -> ExecutionContext:
        if execution_resolver is not None:
            return execution_resolver(name)
        resolved = resolve_model(cfg, name)
        return ExecutionContext(llm=OpenAILLM.from_resolved(resolved), metadata=_metadata(resolved))

    knowledge = None
    if cfg.knowledge_db_path:
        from .knowledge.service import KnowledgeService
        from .knowledge.store import KnowledgeStore
        knowledge = KnowledgeService(KnowledgeStore(cfg.knowledge_db_path))
    agent = Agent(cfg, connector, llm, knowledge=knowledge)

    logger = logging.getLogger("k8spilot.agent")
    logger.info("agent service started: connector=%s llm_base=%s model=%s db=%s knowledge=%s",
                cfg.connector_base_url, cfg.llm_base_url, cfg.llm_model,
                cfg.db_path or "(in-memory)",
                cfg.knowledge_db_path or "(disabled)")

    app = FastAPI(title="k8sPilot Agent Service", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Phase 1: dev-friendly; hardening is not a launch gate
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    alert_limiter = _AlertRateLimiter(cfg.alert_rate_limit_per_minute)

    def _resolve_execution(req: DiagnosisRequest) -> ExecutionContext:
        # Fix an immutable execution context before creating the session, so
        # concurrent diagnoses never share a model configuration (handoff §5).
        if req.model_profile:
            if not cfg.enable_model_profile_selection:
                raise HTTPException(status_code=403, detail="model profile selection is disabled")
            try:
                return _requested_execution(req.model_profile)
            except UnknownProfileError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            except ProfileError as exc:
                raise HTTPException(status_code=500, detail=f"model profile error: {exc}")
        try:
            return _default_execution()
        except ProfileError as exc:
            raise HTTPException(status_code=500, detail=f"model profile error: {exc}")

    def _validate_target(req: DiagnosisRequest) -> None:
        if req.resource is None:
            raise HTTPException(status_code=400, detail="需要提供 resource")
        if req.resource.kind not in DIAGNOSABLE_KINDS:
            raise HTTPException(status_code=400,
                                detail=f"仅支持诊断 {', '.join(DIAGNOSABLE_KINDS)}")
        if req.resource.uid is None and req.resource.kind in ("Pod", "Deployment", "PersistentVolumeClaim"):
            raise HTTPException(status_code=400, detail="需要提供 resource.uid")

    def _start(req: DiagnosisRequest, execution: ExecutionContext,
               lifecycle_id: Optional[int] = None) -> dict[str, Any]:
        # For alerts, the diagnosis row and its lifecycle link are written in one
        # transaction (a crash must not orphan the diagnosis). The worker thread
        # starts only after that commit.
        d = (store.create(req) if lifecycle_id is None
             else store.create_with_alert_link(req, lifecycle_id))
        try:
            threading.Thread(
                target=agent.run,
                args=(req, store, d.diagnosis_id),
                kwargs={"execution": execution},
                daemon=True,
            ).start()
        except BaseException:
            # A queued row without a worker would poll forever: mark it failed so
            # the caller can release the claim and a retry can re-run it.
            store.update(d.diagnosis_id, status="failed", error="worker thread failed to start")
            raise
        # Acceptance response is always "queued"; the client polls for progress.
        return {"diagnosis_id": d.diagnosis_id, "status": "queued", "model": execution.metadata}

    def _claim_is_stale(lifecycle: dict[str, Any]) -> bool:
        """An empty claim older than the threshold is treated as orphaned."""
        stamp = lifecycle.get("created_at") or lifecycle.get("latest_alert_at")
        if not stamp:
            return True
        try:
            created = datetime.fromisoformat(stamp)
        except ValueError:
            return True
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - created).total_seconds()
        return age >= cfg.alert_claim_stale_seconds

    def _handle_alert(req: DiagnosisRequest) -> dict[str, Any]:
        """Phase 5 alert lifecycle: dedup by fingerprint (§26.2)."""
        alert = req.alert
        if alert is None:
            raise HTTPException(status_code=400, detail="alert trigger requires alert context")
        fingerprint = alert.fingerprint

        # Resolved is handled first: it closes the matching active lifecycle,
        # needs only the fingerprint, and must never consume the firing quota.
        if alert.status == AlertStatus.resolved:
            closed = store.close_alert_lifecycle(fingerprint, alert.starts_at)
            return {"status": ALERT_CLOSED if closed else "resolved_noop",
                    "fingerprint": fingerprint, "closed": closed is not None}

        if alert.unresolved_target or req.resource is None:
            # Never guess a target. Dedup runs before the rate limiter: an unresolved
            # alert starts no investigation, so it must not spend the diagnosis
            # quota (and a storm of repeats is already collapsed by the lifecycle).
            _row, created = store.claim_active_alert_lifecycle(
                fingerprint=fingerprint, state=ALERT_UNRESOLVED, alertname=alert.alertname,
                labels=alert.labels, starts_at=alert.starts_at)
            return {"status": ALERT_UNRESOLVED, "fingerprint": fingerprint, "deduped": not created}

        _validate_target(req)
        execution = _resolve_execution(req)
        # Claim the lifecycle before creating a diagnosis so concurrent requests
        # for the same alert never pay for duplicate investigations.
        lifecycle, created = store.claim_active_alert_lifecycle(
            fingerprint=fingerprint, state=ALERT_OPEN, alertname=alert.alertname,
            target=req.resource.model_dump(), labels=alert.labels, starts_at=alert.starts_at)
        if not created:
            # A previously unresolved target can now be diagnosed: promote the
            # lifecycle once so the alert is not stuck without a diagnosis.
            if lifecycle.get("state") == ALERT_UNRESOLVED and not lifecycle.get("diagnosis_id"):
                upgraded = store.upgrade_unresolved_lifecycle(
                    lifecycle["id"], target=req.resource.model_dump(),
                    alertname=alert.alertname, labels=alert.labels)
                # Re-read: another delivery may have upgraded or linked it first.
                lifecycle = store.get_alert_lifecycle(lifecycle["id"]) or lifecycle
                created = upgraded
        if not created:
            existing_id = lifecycle.get("diagnosis_id")
            if existing_id:
                d = store.get(existing_id)
                return {"diagnosis_id": existing_id,
                        "status": d.status if d else "unknown",
                        "fingerprint": fingerprint, "deduped": True}
            if not _claim_is_stale(lifecycle):
                # Another delivery is starting this diagnosis right now.
                return {"diagnosis_id": None, "status": "in_progress",
                        "fingerprint": fingerprint, "deduped": True}
            # Atomically take over the stale claim; only the winner may start.
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(seconds=cfg.alert_claim_stale_seconds)).isoformat()
            if not store.takeover_stale_alert_lifecycle(lifecycle["id"], cutoff):
                return {"diagnosis_id": None, "status": "in_progress",
                        "fingerprint": fingerprint, "deduped": True}

        # Only a delivery that actually starts a new investigation pays the quota;
        # duplicates above returned early, so repeats no longer eat the budget.
        if not alert_limiter.allow():
            store.release_alert_lifecycle(lifecycle["id"])
            raise HTTPException(status_code=429, detail="alert storm rate limited")

        try:
            result = _start(req, execution, lifecycle_id=lifecycle["id"])
        except Exception:
            # Never leave an empty claim behind: a retry must be able to run.
            store.release_alert_lifecycle(lifecycle["id"])
            raise
        result["fingerprint"] = fingerprint
        result["deduped"] = False
        return result

    @app.post("/api/v1/diagnoses", status_code=201)
    def create_diagnosis(req: DiagnosisRequest) -> dict[str, Any]:
        if req.trigger == "alert":
            return _handle_alert(req)
        if req.trigger != "manual":
            raise HTTPException(status_code=400, detail="unsupported trigger")
        _validate_target(req)
        return _start(req, _resolve_execution(req))

    @app.get("/api/v1/diagnoses")
    def list_diagnoses(limit: int = 50) -> list[Any]:
        if limit < 1 or limit > 200:
            limit = 50
        return store.list(limit)

    @app.get("/api/v1/diagnoses/{diagnosis_id}")
    def get_diagnosis(diagnosis_id: str) -> Any:
        d = store.get(diagnosis_id)
        if d is None:
            raise HTTPException(status_code=404, detail="diagnosis not found")
        # Legacy detail: only adds a nullable alert projection.
        return {**d.model_dump(mode="json"),
                "alert": store.get_alert_projection(diagnosis_id)}

    # ---- Diagnosis Center (design §3) ----

    def _parse_center_filters(limit: int, status: Optional[str], trigger: Optional[str],
                              resource_kind: Optional[str], namespace: Optional[str],
                              name: Optional[str], uid: Optional[str],
                              since: Optional[str], until: Optional[str]) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail="limit must be 1..100")
        try:
            filters = {
                "status": validate_enum(status, VALID_STATUSES, "status"),
                "trigger": validate_enum(trigger, VALID_TRIGGERS, "trigger"),
                "resource_kind": resource_kind,
                "namespace": namespace,
                "name": name,
                "uid": uid,
                "since": parse_iso(since) if since else None,
                "until": parse_iso(until) if until else None,
            }
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return filters

    def _session_item(row: dict[str, Any]) -> dict[str, Any]:
        resource = json.loads(row["resource"]) if row["resource"] else {}
        result = json.loads(row["result"]) if row["result"] else None
        summary = None
        if result:
            summary = {k: result.get(k) for k in
                       ("symptom", "root_cause", "root_cause_code", "confidence",
                        "insufficient_evidence")}
        unread = None if row.get("unread") is None else bool(row["unread"])
        return {
            "diagnosis_id": row["diagnosis_id"],
            "trigger": row["trigger"],
            "status": row["status"],
            "resource": {k: resource.get(k) for k in ("kind", "namespace", "name", "uid")},
            "alert": store.get_alert_projection(row["diagnosis_id"]),
            "summary": summary,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "unread": unread,
        }

    @app.get("/api/v1/diagnosis-center/sessions")
    def center_sessions(limit: int = 50, after: Optional[str] = None,
                        status: Optional[str] = None, trigger: Optional[str] = None,
                        resource_kind: Optional[str] = None, namespace: Optional[str] = None,
                        name: Optional[str] = None, uid: Optional[str] = None,
                        since: Optional[str] = None, until: Optional[str] = None,
                        viewer_id: Optional[str] = None, unread: bool = False) -> dict[str, Any]:
        if unread and viewer_id is None:
            raise HTTPException(status_code=422, detail="unread=true requires viewer_id")
        filters = _parse_center_filters(limit, status, trigger, resource_kind, namespace,
                                        name, uid, since, until)
        # `unread` changes both the row set and the sort key, so it must be part
        # of the cursor fingerprint: a cursor from the unread view must never be
        # accepted by the default list (or vice versa).
        fingerprint = filter_fingerprint({**filters, "unread": unread})
        after_keys = None
        if after:
            try:
                data = decode_cursor(after, fingerprint)
                after_keys = (data["ca"], data["id"])
            except (CursorError, KeyError) as exc:
                raise HTTPException(status_code=422, detail=str(exc))
        if viewer_id is not None:
            try:
                validate_viewer_id(viewer_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
        rows, next_keys = store.list_sessions(
            limit=limit, status=filters["status"], trigger=filters["trigger"],
            resource_kind=filters["resource_kind"], namespace=filters["namespace"],
            name=filters["name"], uid=filters["uid"], since=filters["since"],
            until=filters["until"], after=after_keys, viewer_id=viewer_id,
            unread_only=unread)
        next_cursor = (encode_cursor(fingerprint, ca=next_keys[0], id=next_keys[1])
                       if next_keys else None)
        return {"items": [_session_item(r) for r in rows], "next_cursor": next_cursor}

    @app.get("/api/v1/diagnosis-center/notifications")
    def center_notifications(viewer_id: str, limit: int = 50,
                             after: Optional[str] = None) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail="limit must be 1..100")
        try:
            validate_viewer_id(viewer_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        fingerprint = filter_fingerprint({"viewer_id": viewer_id})
        after_keys = None
        if after:
            try:
                data = decode_cursor(after, fingerprint)
                after_keys = (data["ca"], data["id"])
            except (CursorError, KeyError) as exc:
                raise HTTPException(status_code=422, detail=str(exc))
        rows, next_keys = store.list_notifications(
            viewer_id=viewer_id, limit=limit, after=after_keys)
        next_cursor = (encode_cursor(fingerprint, ca=next_keys[0], id=next_keys[1])
                       if next_keys else None)
        return {
            "unread_count": store.count_unread_notifications(viewer_id),
            "items": [{
                "kind": r["kind"],
                "id": r["ref"],
                "diagnosis_id": r["ref"] if r["kind"] == "diagnosis" else None,
                "status": r["status"],
                "unread": True,
            } for r in rows],
            "next_cursor": next_cursor,
        }

    @app.post("/api/v1/diagnosis-center/sessions/{diagnosis_id}/read")
    def center_mark_read(diagnosis_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        viewer_id = str(payload.get("viewer_id") or "")
        try:
            validate_viewer_id(viewer_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        outcome = store.mark_diagnosis_read(viewer_id, diagnosis_id)
        if outcome == "not_found":
            raise HTTPException(status_code=404, detail="diagnosis not found")
        if outcome == "running":
            raise HTTPException(status_code=409, detail="diagnosis is not in a terminal state")
        return {"id": diagnosis_id, "read": True}

    @app.get("/api/v1/diagnosis-center/unresolved-alerts")
    def center_unresolved_alerts(limit: int = 50,
                                 after: Optional[str] = None) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail="limit must be 1..100")
        fingerprint = filter_fingerprint({"area": "unresolved"})
        after_keys = None
        if after:
            try:
                data = decode_cursor(after, fingerprint)
                after_keys = (data["ca"], int(data["id"]))
            except (CursorError, KeyError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc))
        rows, next_keys = store.list_unresolved_alerts(limit=limit, after=after_keys)
        next_cursor = (encode_cursor(fingerprint, ca=next_keys[0], id=next_keys[1])
                       if next_keys else None)
        return {
            "items": [{
                "id": r["id"], "alertname": r["alertname"], "starts_at": r["starts_at"],
                "latest_alert_at": r["latest_alert_at"], "state": r["state"],
                "target": json.loads(r["target"]) if r["target"] else None,
            } for r in rows],
            "next_cursor": next_cursor,
        }

    @app.post("/api/v1/diagnosis-center/notifications/read-all")
    def center_mark_all_read(payload: dict[str, Any]) -> dict[str, Any]:
        """Mark all currently-unread auto-diagnoses as read for this viewer."""
        viewer_id = str(payload.get("viewer_id") or "")
        try:
            validate_viewer_id(viewer_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return {"read": store.mark_all_notifications_read(viewer_id)}

    @app.get("/api/v1/diagnosis-center/namespaces")
    def center_namespaces() -> dict[str, Any]:
        """Distinct namespaces for the filter dropdown."""
        return {"items": store.list_namespaces()}

    @app.get("/api/v1/diagnoses/{diagnosis_id}/timeline")
    def diagnosis_timeline(diagnosis_id: str, after: int = 0, limit: int = 100
                           ) -> dict[str, Any]:
        if store.get(diagnosis_id) is None:
            raise HTTPException(status_code=404, detail="diagnosis not found")
        if not cfg.trace_dir:
            return {"items": [], "next_after": after, "has_more": False,
                    "available": "unavailable", "gap": False}
        path = Path(cfg.trace_dir) / f"{diagnosis_id}.jsonl"
        if not path.is_file():
            return {"items": [], "next_after": after, "has_more": False,
                    "available": "pending", "gap": False}
        try:
            return read_timeline(str(path), diagnosis_id=diagnosis_id,
                                 after=after, limit=limit)
        except TimelineRangeError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    return app


app = create_app()
