"""FastAPI entrypoint for the agent service."""

import logging
import threading
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from .agent import Agent, OpenAILLM
from .config import Config
from .connector import ConnectorClient
from .models import DIAGNOSABLE_KINDS, DiagnosisRequest
from .store import SessionStore


def create_app(cfg: Optional[Config] = None, store: Optional[SessionStore] = None,
               connector: Optional[ConnectorClient] = None, llm: Any = None) -> FastAPI:
    """App factory with injectable dependencies for testing."""
    cfg = cfg or Config()
    store = store or SessionStore(cfg.db_path or None)
    connector = connector or ConnectorClient(cfg.connector_base_url, cfg.connector_timeout)
    llm = llm or OpenAILLM(cfg)
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

    @app.post("/api/v1/diagnoses", status_code=201)
    def create_diagnosis(req: DiagnosisRequest) -> dict[str, str]:
        if req.trigger != "manual":
            raise HTTPException(status_code=400, detail="Phase 1 仅支持 manual 触发")
        if req.resource.kind not in DIAGNOSABLE_KINDS:
            raise HTTPException(status_code=400,
                                detail=f"仅支持诊断 {', '.join(DIAGNOSABLE_KINDS)}")
        if req.resource.uid is None and req.resource.kind in ("Pod", "Deployment", "PersistentVolumeClaim"):
            raise HTTPException(status_code=400, detail="需要提供 resource.uid")

        d = store.create(req)
        threading.Thread(
            target=agent.run,
            args=(req, store, d.diagnosis_id),
            daemon=True,
        ).start()
        # Acceptance response is always "queued"; the client polls for progress.
        return {"diagnosis_id": d.diagnosis_id, "status": "queued"}

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
