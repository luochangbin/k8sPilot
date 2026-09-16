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
);

CREATE TABLE IF NOT EXISTS alert_lifecycles (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint    TEXT NOT NULL,
    diagnosis_id   TEXT,
    state          TEXT NOT NULL,
    alertname      TEXT,
    target         TEXT,
    labels         TEXT,
    starts_at      TEXT,
    latest_alert_at TEXT,
    resolved_at    TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alert_lifecycle_fp_state
    ON alert_lifecycles (fingerprint, state);

-- At most one active lifecycle per fingerprint (open or unresolved_target).
CREATE UNIQUE INDEX IF NOT EXISTS uniq_alert_lifecycle_active
    ON alert_lifecycles (fingerprint)
    WHERE state IN ('open', 'unresolved_target');
"""


# Alert lifecycle states (Phase 5, §26.2).
ALERT_OPEN = "open"
ALERT_CLOSED = "closed"
ALERT_UNRESOLVED = "unresolved_target"


class SessionStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        path = db_path or ":memory:"
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
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

    # ---- Phase 5 alert lifecycles (§26.2) ----

    def _latest_alert(self, fingerprint: str, state: Optional[str]) -> Optional[dict[str, Any]]:
        query = "SELECT * FROM alert_lifecycles WHERE fingerprint = ?"
        args: list[Any] = [fingerprint]
        if state:
            query += " AND state = ?"
            args.append(state)
        query += " ORDER BY id DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(query, args).fetchone()
        return dict(row) if row else None

    def get_open_alert_lifecycle(self, fingerprint: str) -> Optional[dict[str, Any]]:
        return self._latest_alert(fingerprint, ALERT_OPEN)

    def latest_alert_lifecycle(self, fingerprint: str) -> Optional[dict[str, Any]]:
        return self._latest_alert(fingerprint, None)

    def list_alert_lifecycles(self, fingerprint: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alert_lifecycles WHERE fingerprint = ? ORDER BY id",
                (fingerprint,),
            ).fetchall()
        return [dict(r) for r in rows]

    def claim_active_alert_lifecycle(self, *, fingerprint: str, state: str,
                                     alertname: Optional[str] = None,
                                     target: Optional[dict] = None,
                                     labels: Optional[dict] = None,
                                     starts_at: Optional[str] = None
                                     ) -> tuple[dict[str, Any], bool]:
        """Atomically get-or-create the single active lifecycle for a fingerprint.

        Returns (row, created). Repeated arrival within a lifecycle only updates
        latest_alert_at. A partial unique index enforces one active row per
        fingerprint even under concurrent requests.
        """
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM alert_lifecycles WHERE fingerprint = ? AND state IN (?, ?) "
                "ORDER BY id DESC LIMIT 1",
                (fingerprint, ALERT_OPEN, ALERT_UNRESOLVED),
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE alert_lifecycles SET latest_alert_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, row["id"]),
                )
                self._conn.commit()
                updated = self._conn.execute(
                    "SELECT * FROM alert_lifecycles WHERE id = ?", (row["id"],)).fetchone()
                return dict(updated), False
            cur = self._conn.execute(
                "INSERT INTO alert_lifecycles (fingerprint, diagnosis_id, state, alertname, "
                "target, labels, starts_at, latest_alert_at, resolved_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (fingerprint, None, state, alertname,
                 json.dumps(target, ensure_ascii=False) if target is not None else None,
                 json.dumps(labels, ensure_ascii=False) if labels is not None else None,
                 starts_at, now, None, now, now),
            )
            self._conn.commit()
            created = self._conn.execute(
                "SELECT * FROM alert_lifecycles WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(created), True

    def link_alert_diagnosis(self, lifecycle_id: int, diagnosis_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE alert_lifecycles SET diagnosis_id = ?, updated_at = ? WHERE id = ?",
                (diagnosis_id, _now(), lifecycle_id),
            )
            self._conn.commit()

    def get_alert_lifecycle(self, lifecycle_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM alert_lifecycles WHERE id = ?", (lifecycle_id,)).fetchone()
        return dict(row) if row else None

    def release_alert_lifecycle(self, lifecycle_id: int) -> bool:
        """Drop an empty (not-yet-linked) claim so a failed attempt can be
        retried. A lifecycle that already has a diagnosis is never released."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM alert_lifecycles WHERE id = ? AND diagnosis_id IS NULL",
                (lifecycle_id,))
            self._conn.commit()
            return cur.rowcount == 1

    def takeover_stale_alert_lifecycle(self, lifecycle_id: int, cutoff: str) -> bool:
        """Atomically take over a stale, not-yet-linked claim.

        The conditional UPDATE (state, no diagnosis, created_at <= cutoff) means
        at most one concurrent caller wins; the winner refreshes the claim so
        other callers no longer see it as stale.
        """
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE alert_lifecycles SET created_at = ?, latest_alert_at = ?, updated_at = ? "
                "WHERE id = ? AND state = ? AND diagnosis_id IS NULL AND created_at <= ?",
                (now, now, now, lifecycle_id, ALERT_OPEN, cutoff),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def upgrade_unresolved_lifecycle(self, lifecycle_id: int, *,
                                     target: Optional[dict] = None,
                                     alertname: Optional[str] = None,
                                     labels: Optional[dict] = None) -> bool:
        """Promote an unresolved_target lifecycle to open once the target is
        resolvable, so the alert can be diagnosed exactly once."""
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE alert_lifecycles SET state = ?, created_at = ?, latest_alert_at = ?, "
                "target = COALESCE(?, target), alertname = COALESCE(?, alertname), "
                "labels = COALESCE(?, labels), updated_at = ? "
                "WHERE id = ? AND state = ? AND diagnosis_id IS NULL",
                (ALERT_OPEN, now, now,
                 json.dumps(target, ensure_ascii=False) if target is not None else None,
                 alertname,
                 json.dumps(labels, ensure_ascii=False) if labels is not None else None,
                 now, lifecycle_id, ALERT_UNRESOLVED),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def close_alert_lifecycle(self, fingerprint: str,
                              starts_at: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Close the active lifecycle, but only when it belongs to the same alert
        instance. A late resolved for an older starts_at must not close a newer
        lifecycle (returns None in that case)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM alert_lifecycles WHERE fingerprint = ? AND state IN (?, ?) "
                "ORDER BY id DESC LIMIT 1",
                (fingerprint, ALERT_OPEN, ALERT_UNRESOLVED),
            ).fetchone()
            if row is None:
                return None
            if starts_at and row["starts_at"] and row["starts_at"] != starts_at:
                return None
            now = _now()
            self._conn.execute(
                "UPDATE alert_lifecycles SET state = ?, resolved_at = ?, updated_at = ? WHERE id = ?",
                (ALERT_CLOSED, now, now, row["id"]),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM alert_lifecycles WHERE id = ?", (row["id"],)).fetchone()
            return dict(updated)

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
