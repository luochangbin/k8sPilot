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


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Additive migration: SQLite has no ADD COLUMN IF NOT EXISTS."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.commit()


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
    failure_reason TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
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

-- Diagnosis Center: per-viewer read receipts (not authentication).
CREATE TABLE IF NOT EXISTS diagnosis_read_receipts (
    viewer_id     TEXT NOT NULL,
    diagnosis_id  TEXT NOT NULL,
    read_at       TEXT NOT NULL,
    PRIMARY KEY (viewer_id, diagnosis_id),
    FOREIGN KEY (diagnosis_id) REFERENCES diagnoses(diagnosis_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_read_receipts_diagnosis
    ON diagnosis_read_receipts (diagnosis_id);

CREATE INDEX IF NOT EXISTS idx_diagnoses_created
    ON diagnoses (created_at DESC, diagnosis_id DESC);

CREATE INDEX IF NOT EXISTS idx_alert_lifecycle_diagnosis
    ON alert_lifecycles (diagnosis_id, id DESC);
-- Diagnosis Center: per-viewer notification baseline (first seen).
CREATE TABLE IF NOT EXISTS viewer_state (
    viewer_id     TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL
);
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
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(_SCHEMA)
            _ensure_column(self._conn, "diagnoses", "failure_reason", "TEXT")
            _ensure_column(self._conn, "diagnoses", "cancel_requested",
                           "INTEGER NOT NULL DEFAULT 0")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()

    # ---- public API used by the agent / api ----

    def create(self, req: DiagnosisRequest) -> Diagnosis:
        d = self._build_diagnosis(req)
        with self._lock:
            self._insert(d)
        return d

    def create_with_alert_link(self, req: DiagnosisRequest, lifecycle_id: int) -> Diagnosis:
        """Create a queued diagnosis and link it to the lifecycle atomically.

        Without this, a crash between the two writes would leave a diagnosis that
        no lifecycle points at (invisible to the Center / read receipts).
        """
        d = self._build_diagnosis(req)
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO diagnoses (diagnosis_id, trigger, resource, status, result, "
                        "error, eval_run_id, case_id, case_version, attempt_index, created_at, "
                        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (d.diagnosis_id, d.trigger, d.resource.model_dump_json(), d.status,
                         None, None, d.eval_run_id, d.case_id, d.case_version, d.attempt_index,
                         d.created_at.isoformat(), d.updated_at.isoformat()),
                    )
                    cur = self._conn.execute(
                        "UPDATE alert_lifecycles SET diagnosis_id = ?, updated_at = ? "
                        "WHERE id = ? AND diagnosis_id IS NULL",
                        (d.diagnosis_id, _now(), lifecycle_id),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(
                            f"alert lifecycle {lifecycle_id} was already linked")
            except Exception:
                # The context manager rolled the transaction back; nothing to clean up.
                raise
        return d

    def _build_diagnosis(self, req: DiagnosisRequest) -> Diagnosis:
        now = _now()
        return Diagnosis(
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

    def get(self, diagnosis_id: str) -> Optional[Diagnosis]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM diagnoses WHERE diagnosis_id = ?", (diagnosis_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def update(self, diagnosis_id: str, *, status: Optional[str] = None,
               result: Optional[Any] = None, error: Optional[str] = None,
               failure_reason: Optional[str] = None) -> Optional[Diagnosis]:
        with self._lock:
            self._conn.execute(
                "UPDATE diagnoses SET status = COALESCE(?, status), "
                "result = COALESCE(?, result), error = COALESCE(?, error), "
                "failure_reason = COALESCE(?, failure_reason), updated_at = ? "
                "WHERE diagnosis_id = ?",
                (status,
                 result.model_dump_json() if result is not None else None,
                 error,
                 failure_reason,
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
                if (starts_at and row["starts_at"] and row["starts_at"] != starts_at):
                    # Same fingerprint but a different alert instance (the previous
                    # lifecycle never got its resolved): archive it and start a new
                    # lifecycle instead of deduping onto the old diagnosis.
                    self._conn.execute(
                        "UPDATE alert_lifecycles SET state = ?, resolved_at = ?, updated_at = ? "
                        "WHERE id = ?",
                        (ALERT_CLOSED, now, now, row["id"]),
                    )
                else:
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

    # ---- Diagnosis Center (list/notifications/unresolved/read) ----

    def get_alert_projection(self, diagnosis_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT fingerprint, alertname, state, starts_at, latest_alert_at, resolved_at "
                "FROM alert_lifecycles WHERE diagnosis_id = ? ORDER BY id DESC LIMIT 1",
                (diagnosis_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, *, limit: int, status: Optional[str] = None,
                      trigger: Optional[str] = None, resource_kind: Optional[str] = None,
                      namespace: Optional[str] = None, name: Optional[str] = None,
                      uid: Optional[str] = None, since: Optional[str] = None,
                      until: Optional[str] = None,
                      after: Optional[tuple[str, str]] = None,
                      viewer_id: Optional[str] = None,
                      unread_only: bool = False
                      ) -> tuple[list[dict[str, Any]], Optional[tuple[str, str]]]:
        """Keyset page over (sort_ts, diagnosis_id) DESC with filters.

        Default ordering is by creation time. The unread view sorts by
        `updated_at` instead, so a diagnosis that was created early but finished
        late is not buried behind newer (already read) rows.
        """
        order_col = "d.updated_at" if unread_only else "d.created_at"
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("d.status = ?")
            params.append(status)
        if trigger:
            where.append("d.trigger = ?")
            params.append(trigger)
        if resource_kind:
            where.append("json_extract(d.resource, '$.kind') = ?")
            params.append(resource_kind)
        if namespace:
            where.append("json_extract(d.resource, '$.namespace') = ?")
            params.append(namespace)
        if name:
            # Fuzzy match on the resource name; escape LIKE wildcards.
            escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("json_extract(d.resource, '$.name') LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        if uid:
            where.append("json_extract(d.resource, '$.uid') = ?")
            params.append(uid)
        if since:
            where.append("d.created_at >= ?")
            params.append(since)
        if until:
            where.append("d.created_at <= ?")
            params.append(until)
        if after:
            where.append(f"({order_col} < ? OR ({order_col} = ? AND d.diagnosis_id < ?))")
            params.extend([after[0], after[0], after[1]])
        join = ""
        unread_expr = "NULL"
        if viewer_id:
            baseline = self.ensure_viewer(viewer_id)
            join = (" LEFT JOIN diagnosis_read_receipts r "
                    "ON r.diagnosis_id = d.diagnosis_id AND r.viewer_id = ?")
            # Unread applies to auto-diagnoses only (same scope as notifications),
            # so manual/eval rows are never reported as unread. The viewer baseline
            # keeps pre-existing history out of "new".
            unread_expr = (
                "CASE WHEN d.trigger = 'alert' AND d.status IN ('completed', 'failed') "
                "AND d.eval_run_id IS NULL AND r.read_at IS NULL AND d.updated_at > ? "
                "THEN 1 ELSE 0 END"
            )
            params = [baseline, viewer_id, *params]
            if unread_only:
                where.extend([
                    "d.trigger = 'alert'", "d.status IN ('completed', 'failed')",
                    "d.eval_run_id IS NULL", "r.read_at IS NULL", "d.updated_at > ?",
                ])
                params.append(baseline)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (f"SELECT d.diagnosis_id, d.trigger, d.status, d.resource, d.result, "
               f"d.created_at, d.updated_at, {unread_expr} AS unread "
               f"FROM diagnoses d{join}{clause} "
               f"ORDER BY {order_col} DESC, d.diagnosis_id DESC LIMIT ?")
        with self._lock:
            rows = self._conn.execute(sql, [*params, limit + 1]).fetchall()
        out = [dict(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit and out:
            sort_key = "updated_at" if unread_only else "created_at"
            next_cursor = (out[-1][sort_key], out[-1]["diagnosis_id"])
        return out, next_cursor

    # ---- task lifecycle (cancel / restart recovery) ----

    def request_cancel(self, diagnosis_id: str) -> str:
        """Ask a running diagnosis to stop. Idempotent.

        Returns ok | not_found | terminal. The worker checks the flag before
        every LLM call and every tool execution, so the stop is cooperative but
        prompt (no further model or connector work is started).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM diagnoses WHERE diagnosis_id = ?", (diagnosis_id,)
            ).fetchone()
            if row is None:
                return "not_found"
            if row["status"] in ("completed", "failed"):
                return "terminal"
            self._conn.execute(
                "UPDATE diagnoses SET cancel_requested = 1, updated_at = ? WHERE diagnosis_id = ?",
                (_now(), diagnosis_id),
            )
            self._conn.commit()
        return "ok"

    def is_cancelled(self, diagnosis_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT cancel_requested FROM diagnoses WHERE diagnosis_id = ?",
                (diagnosis_id,),
            ).fetchone()
        return bool(row["cancel_requested"]) if row else False

    def mark_orphans_failed(self, reason: str) -> int:
        """Fail sessions left non-terminal by a previous process.

        Worker threads do not survive a restart, so a persisted `queued` /
        `investigating` row would otherwise hang forever.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE diagnoses SET status = 'failed', error = ?, failure_reason = ?, "
                "updated_at = ? WHERE status IN ('queued', 'investigating')",
                (reason, "agent_restarted", _now()),
            )
            self._conn.commit()
        return int(cur.rowcount or 0)

    def ensure_viewer(self, viewer_id: str) -> str:
        """Return the viewer's baseline time, creating it on first sight.

        Everything created before the baseline is treated as history, so a new
        browser does not see the whole backlog as "new" notifications.
        """
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO viewer_state (viewer_id, first_seen_at) VALUES (?,?)",
                (viewer_id, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT first_seen_at FROM viewer_state WHERE viewer_id = ?", (viewer_id,)
            ).fetchone()
        return row["first_seen_at"] if row else now

    def count_unread_diagnoses(self, viewer_id: str) -> int:
        """Unread auto-diagnoses for a viewer.

        Unread is determined by the **terminal/update time** (`updated_at`), not
        by creation time: a diagnosis created before the viewer's baseline but
        finishing after it is genuinely new and must be counted.
        """
        baseline = self.ensure_viewer(viewer_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM diagnoses d "
                "LEFT JOIN diagnosis_read_receipts r "
                "  ON r.diagnosis_id = d.diagnosis_id AND r.viewer_id = ? "
                "WHERE d.trigger = 'alert' AND d.status IN ('completed', 'failed') "
                "  AND d.eval_run_id IS NULL AND r.read_at IS NULL AND d.updated_at > ?",
                (viewer_id, baseline),
            ).fetchone()
        return int(row["c"]) if row else 0

    def count_pending_alerts(self) -> int:
        """Unresolved alerts are **pending work**, not unread notifications.

        They are global (not per-viewer) and never affected by read receipts.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM alert_lifecycles "
                "WHERE state = ? AND diagnosis_id IS NULL",
                (ALERT_UNRESOLVED,),
            ).fetchone()
        return int(row["c"]) if row else 0

    def count_unread_notifications(self, viewer_id: str) -> int:
        """Backward-compatible alias: unread notifications are unread diagnoses."""
        return self.count_unread_diagnoses(viewer_id)

    def mark_all_notifications_read(self, viewer_id: str) -> int:
        """Mark every currently-unread auto-diagnosis as read for this viewer.

        Uses the *same* scope as count_unread_diagnoses (including the viewer's
        first_seen baseline): a new browser must not stamp receipts on the
        history it never saw as unread, and the returned count is exactly the
        number of rows the user sees as unread.

        The candidate ids are snapshotted under the same lock as the inserts, so
        diagnoses that reach a terminal state while the request is in flight stay
        unread (they were not part of this snapshot).
        """
        baseline = self.ensure_viewer(viewer_id)
        now = _now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.diagnosis_id FROM diagnoses d "
                "LEFT JOIN diagnosis_read_receipts r "
                "  ON r.diagnosis_id = d.diagnosis_id AND r.viewer_id = ? "
                "WHERE d.trigger = 'alert' AND d.status IN ('completed', 'failed') "
                "  AND d.eval_run_id IS NULL AND r.read_at IS NULL AND d.updated_at > ?",
                (viewer_id, baseline),
            ).fetchall()
            ids = [r["diagnosis_id"] for r in rows]
            for diagnosis_id in ids:
                self._conn.execute(
                    "INSERT OR IGNORE INTO diagnosis_read_receipts "
                    "(viewer_id, diagnosis_id, read_at) VALUES (?,?,?)",
                    (viewer_id, diagnosis_id, now),
                )
            self._conn.commit()
        return len(ids)

    def list_namespaces(self) -> list[str]:
        """Distinct namespaces for the filter dropdown.

        Eval runs create one isolated namespace per case (e.g.
        `eval-pod-oomkilled-001`); those fixtures are excluded so the dropdown
        only shows namespaces from real (manual/alert) diagnoses.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT json_extract(resource, '$.namespace') AS ns FROM diagnoses "
                "WHERE eval_run_id IS NULL "
                "  AND json_extract(resource, '$.namespace') IS NOT NULL "
                "  AND json_extract(resource, '$.namespace') != '' ORDER BY ns"
            ).fetchall()
        return [r["ns"] for r in rows if r["ns"]]

    def list_notifications(self, *, viewer_id: str, limit: int,
                           after: Optional[tuple[str, str]] = None
                           ) -> tuple[list[dict[str, Any]], Optional[tuple[str, str]]]:
        """Unread auto-diagnoses (pending/unresolved alerts are NOT unread).

        Ordering and the baseline both use `updated_at` (terminal time), so a
        diagnosis created before the baseline but completed after it shows up.
        """
        baseline = self.ensure_viewer(viewer_id)
        where = ["d.trigger = 'alert'", "d.status IN ('completed', 'failed')",
                 "d.eval_run_id IS NULL", "r.read_at IS NULL", "d.updated_at > ?"]
        params: list[Any] = [viewer_id, baseline]
        if after:
            where.append("(d.updated_at < ? OR (d.updated_at = ? AND d.diagnosis_id < ?))")
            params.extend([after[0], after[0], after[1]])
        sql = ("SELECT d.diagnosis_id AS ref, 'diagnosis' AS kind, d.status AS status, "
               "d.updated_at AS updated_at FROM diagnoses d "
               "LEFT JOIN diagnosis_read_receipts r "
               "  ON r.diagnosis_id = d.diagnosis_id AND r.viewer_id = ? "
               "WHERE " + " AND ".join(where) +
               " ORDER BY d.updated_at DESC, d.diagnosis_id DESC LIMIT ?")
        with self._lock:
            rows = self._conn.execute(sql, [*params, limit + 1]).fetchall()
        out = [dict(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit and out:
            next_cursor = (out[-1]["updated_at"], out[-1]["ref"])
        return out, next_cursor

    def list_unresolved_alerts(self, *, limit: int,
                               after: Optional[tuple[str, int]] = None
                               ) -> tuple[list[dict[str, Any]], Optional[tuple[str, int]]]:
        where = ["state = ?", "diagnosis_id IS NULL"]
        params: list[Any] = [ALERT_UNRESOLVED]
        if after:
            where.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend([after[0], after[0], after[1]])
        sql = ("SELECT id, alertname, starts_at, latest_alert_at, state, target, created_at "
               "FROM alert_lifecycles WHERE " + " AND ".join(where) +
               " ORDER BY created_at DESC, id DESC LIMIT ?")
        with self._lock:
            rows = self._conn.execute(sql, [*params, limit + 1]).fetchall()
        out = [dict(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit and out:
            next_cursor = (out[-1]["created_at"], out[-1]["id"])
        return out, next_cursor

    def mark_diagnosis_read(self, viewer_id: str, diagnosis_id: str) -> str:
        """Idempotently record a read receipt. Returns ok|not_found|running."""
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM diagnoses WHERE diagnosis_id = ?", (diagnosis_id,)
            ).fetchone()
            if row is None:
                return "not_found"
            if row["status"] not in ("completed", "failed"):
                return "running"
            self._conn.execute(
                "INSERT OR REPLACE INTO diagnosis_read_receipts "
                "(viewer_id, diagnosis_id, read_at) VALUES (?,?,?)",
                (viewer_id, diagnosis_id, _now()),
            )
            self._conn.commit()
        return "ok"

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
            failure_reason=row["failure_reason"],
            eval_run_id=row["eval_run_id"],
            case_id=row["case_id"],
            case_version=row["case_version"],
            attempt_index=row["attempt_index"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
