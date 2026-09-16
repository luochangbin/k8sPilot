"""FastAPI entrypoint for the agent service."""

import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from .agent import Agent
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

    def _start(req: DiagnosisRequest, execution: ExecutionContext) -> dict[str, Any]:
        d = store.create(req)
        threading.Thread(
            target=agent.run,
            args=(req, store, d.diagnosis_id),
            kwargs={"execution": execution},
            daemon=True,
        ).start()
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

        if not alert_limiter.allow():
            raise HTTPException(status_code=429, detail="alert storm rate limited")

        if alert.unresolved_target or req.resource is None:
            # Never guess a target; a repeated unresolved alert is deduped too.
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

        try:
            result = _start(req, execution)
        except Exception:
            # Never leave an empty claim behind: a retry must be able to run.
            store.release_alert_lifecycle(lifecycle["id"])
            raise
        store.link_alert_diagnosis(lifecycle["id"], result["diagnosis_id"])
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
        return d

    return app


app = create_app()
