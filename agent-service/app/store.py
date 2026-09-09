"""Diagnosis Session store backed by SQLite (Phase 3).

Sessions survive restarts when DIAGNOSIS_DB points at a file; ":memory:" (or an
empty path) keeps the Phase 1 in-memory behavior for tests and ad-hoc dev.

The store is thread-safe: diagnoses run on worker threads while HTTP handlers
read/write on other threads, so a single guarded connection is used.
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import Diagnosis, DiagnosisRequest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_diagnosis_id() -> str:
    return f"diag_{uuid.uuid4().hex[:12]}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS diagnoses (
    diagnosis_id   TEXT PRIMARY KEY,
    trigger        TEXT NOT NULL,
    resource       TEXT NOT NULL,
    status         TEXT NOT NULL,
    result         TEXT,
    error          TEXT,
    eval_run_id    TEXT,
    case_id        TEXT,
    case_version   TEXT,
    attempt_index  INTEGER,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
)
"""


class SessionStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        path = db_path or ":memory:"
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(_SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()

    # ---- public API used by the agent / api ----

    def create(self, req: DiagnosisRequest) -> Diagnosis:
        now = _now()
        d = Diagnosis(
            diagnosis_id=new_diagnosis_id(),
            trigger=req.trigger.value,
            resource=req.resource,
            status="queued",
            result=None,
            error=None,
            eval_run_id=req.eval_run_id,
            case_id=req.case_id,
            case_version=req.case_version,
            attempt_index=req.attempt_index,
            created_at=datetime.fromisoformat(now),
            updated_at=datetime.fromisoformat(now),
        )
        with self._lock:
            self._insert(d)
        return d

    def get(self, diagnosis_id: str) -> Optional[Diagnosis]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM diagnoses WHERE diagnosis_id = ?", (diagnosis_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def update(self, diagnosis_id: str, *, status: Optional[str] = None,
               result: Optional[Any] = None, error: Optional[str] = None) -> Optional[Diagnosis]:
        with self._lock:
            self._conn.execute(
                "UPDATE diagnoses SET status = COALESCE(?, status), "
                "result = COALESCE(?, result), error = COALESCE(?, error), updated_at = ? "
                "WHERE diagnosis_id = ?",
                (status,
                 result.model_dump_json() if result is not None else None,
                 error,
                 _now(),
                 diagnosis_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM diagnoses WHERE diagnosis_id = ?", (diagnosis_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def list(self, limit: int = 50) -> list[Diagnosis]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM diagnoses ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._from_row(r) for r in rows]

    # ---- helpers ----

    def _insert(self, d: Diagnosis) -> None:
        self._conn.execute(
            "INSERT INTO diagnoses (diagnosis_id, trigger, resource, status, result, error, "
            "eval_run_id, case_id, case_version, attempt_index, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (d.diagnosis_id, d.trigger, d.resource.model_dump_json(), d.status,
             d.result.model_dump_json() if d.result else None, d.error,
             d.eval_run_id, d.case_id, d.case_version, d.attempt_index,
             d.created_at.isoformat(), d.updated_at.isoformat()),
        )
        self._conn.commit()

    def _from_row(self, row: sqlite3.Row) -> Diagnosis:
        from .models import DiagnosisResult, ResourceRef

        resource = ResourceRef.model_validate_json(row["resource"])
        result = DiagnosisResult.model_validate_json(row["result"]) if row["result"] else None
        return Diagnosis(
            diagnosis_id=row["diagnosis_id"],
            trigger=row["trigger"],
            resource=resource,
            status=row["status"],
            result=result,
            error=row["error"],
            eval_run_id=row["eval_run_id"],
            case_id=row["case_id"],
            case_version=row["case_version"],
            attempt_index=row["attempt_index"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
