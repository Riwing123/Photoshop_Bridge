from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1"
FINAL_STATUSES = {"done", "error", "cancelled", "expired"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def epoch_now() -> float:
    return time.time()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


class JobQueue:
    def __init__(self, db_path: str | Path | None = None, lease_seconds: float = 90.0) -> None:
        if db_path is None:
            db_path = Path(__file__).resolve().parents[1] / "runtime" / "ps-agent.sqlite3"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = max(10.0, float(lease_seconds))
        self._condition = threading.Condition(threading.RLock())
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()
        self.recover_stale_jobs()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    timeout_ms INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL DEFAULT 1,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    claimed_by TEXT,
                    claimed_at TEXT,
                    lease_expires_at REAL,
                    completed_at TEXT
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at)")
            self._ensure_column("jobs", "workflow_id", "TEXT")
            self._ensure_column("jobs", "stage_id", "TEXT")
            self._ensure_column("jobs", "parent_job_id", "TEXT")
            self._ensure_column("jobs", "stage_status", "TEXT")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_workflow_stage ON jobs(workflow_id, stage_id)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS heartbeats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at TEXT NOT NULL,
                    received_at_epoch REAL NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_heartbeats_received ON heartbeats(received_at_epoch)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    job_id TEXT,
                    details_json TEXT NOT NULL
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at)")
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        columns = {str(row["name"]) for row in rows}
        if column not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        with self._condition:
            self._conn.close()

    def event(self, event_type: str, details: dict[str, Any] | None = None, job_id: str | None = None) -> None:
        with self._condition:
            self._event_locked(event_type, details or {}, job_id)
            self._condition.notify_all()

    def _event_locked(self, event_type: str, details: dict[str, Any], job_id: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO events(created_at, event_type, job_id, details_json) VALUES(?, ?, ?, ?)",
            (utc_now(), event_type, job_id, json_dumps(details)),
        )
        self._conn.commit()

    def recover_stale_jobs(self) -> dict[str, int]:
        now_epoch = epoch_now()
        recovered = {"requeued": 0, "expired": 0}
        with self._condition:
            rows = self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (now_epoch,),
            ).fetchall()
            for row in rows:
                job_id = str(row["job_id"])
                attempts = int(row["attempts"] or 0)
                max_attempts = int(row["max_attempts"] or 1)
                if attempts < max_attempts:
                    self._conn.execute(
                        """
                        UPDATE jobs
                        SET status = 'pending',
                            updated_at = ?,
                            claimed_by = NULL,
                            claimed_at = NULL,
                            lease_expires_at = NULL
                        WHERE job_id = ?
                        """,
                        (utc_now(), job_id),
                    )
                    self._event_locked("job_requeued_after_stale_lease", {"attempts": attempts}, job_id)
                    recovered["requeued"] += 1
                else:
                    error = {
                        "code": "job_lease_expired",
                        "message": "The job was running when the backend restarted or its lease expired.",
                    }
                    self._conn.execute(
                        """
                        UPDATE jobs
                        SET status = 'expired',
                            updated_at = ?,
                            completed_at = ?,
                            error_json = ?,
                            claimed_by = NULL,
                            claimed_at = NULL,
                            lease_expires_at = NULL
                        WHERE job_id = ?
                        """,
                        (utc_now(), utc_now(), json_dumps(error), job_id),
                    )
                    self._event_locked("job_expired_after_stale_lease", {"attempts": attempts}, job_id)
                    recovered["expired"] += 1
            self._conn.commit()
            self._condition.notify_all()
        return recovered

    def set_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._condition:
            self._conn.execute(
                "INSERT INTO heartbeats(received_at, received_at_epoch, payload_json) VALUES(?, ?, ?)",
                (utc_now(), epoch_now(), json_dumps(payload)),
            )
            self._conn.commit()
            self._condition.notify_all()
            return self.health()

    def latest_heartbeat(self) -> dict[str, Any] | None:
        with self._condition:
            row = self._latest_heartbeat_row_locked()
            return self._heartbeat_from_row(row) if row else None

    def uxp_connected(self, stale_after_seconds: float = 6.5) -> bool:
        with self._condition:
            row = self._latest_heartbeat_row_locked()
            if row is None:
                return False
            return epoch_now() - float(row["received_at_epoch"]) <= stale_after_seconds

    def create_job(
        self,
        job_type: str,
        payload: dict[str, Any] | None = None,
        timeout_ms: int = 60000,
    ) -> dict[str, Any]:
        payload = payload or {}
        now = utc_now()
        job_id = f"job-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        max_attempts = payload.get("max_attempts", 1)
        try:
            max_attempts = max(1, min(int(max_attempts), 5))
        except (TypeError, ValueError):
            max_attempts = 1
        with self._condition:
            self._conn.execute(
                """
                INSERT INTO jobs(
                    job_id, schema_version, job_type, status, payload_json,
                    created_at, updated_at, timeout_ms, max_attempts, attempts,
                    workflow_id, stage_id, parent_job_id, stage_status
                )
                VALUES(?, 'ps-agent/v1', ?, 'pending', ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job_type,
                    json_dumps(payload),
                    now,
                    now,
                    int(timeout_ms),
                    int(max_attempts),
                    payload.get("workflow_id"),
                    payload.get("stage_id"),
                    payload.get("parent_job_id"),
                    payload.get("stage_status", "pending") if payload.get("stage_id") else None,
                ),
            )
            self._event_locked(
                "job_created",
                {
                    "job_type": job_type,
                    "timeout_ms": timeout_ms,
                    "workflow_id": payload.get("workflow_id"),
                    "stage_id": payload.get("stage_id"),
                },
                job_id,
            )
            self._conn.commit()
            self._condition.notify_all()
            return self._copy_job_locked(job_id)

    def next_job(self, claimed_by: str = "uxp-plugin") -> dict[str, Any] | None:
        with self._condition:
            self.recover_stale_jobs()
            row = self._conn.execute(
                """
                SELECT job_id FROM jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None

            job_id = str(row["job_id"])
            now = utc_now()
            lease_expires_at = epoch_now() + self.lease_seconds
            self._conn.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    updated_at = ?,
                    claimed_by = ?,
                    claimed_at = ?,
                    lease_expires_at = ?,
                    stage_status = CASE WHEN stage_id IS NOT NULL THEN 'running' ELSE stage_status END,
                    attempts = attempts + 1
                WHERE job_id = ? AND status = 'pending'
                """,
                (now, claimed_by, now, lease_expires_at, job_id),
            )
            self._event_locked("job_claimed", {"claimed_by": claimed_by}, job_id)
            self._conn.commit()
            self._condition.notify_all()
            return self._copy_job_locked(job_id)

    def finish_job(self, job_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
        with self._condition:
            row = self._conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                return None

            current_status = str(row["status"])
            if current_status in FINAL_STATUSES:
                self._event_locked("job_duplicate_result", {"current_status": current_status}, job_id)
                self._condition.notify_all()
                return self._copy_job_locked(job_id)

            result_status = result.get("status", "ok")
            if result_status == "ok":
                status = "done"
                error = None
            elif result_status == "cancelled":
                status = "cancelled"
                error = result.get("error")
            else:
                status = "error"
                error = result.get("error") or {
                    "code": "uxp_job_error",
                    "message": "UXP plugin returned an error result.",
                }

            now = utc_now()
            self._conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    result_json = ?,
                    error_json = ?,
                    completed_at = ?,
                    updated_at = ?,
                    stage_status = CASE WHEN stage_id IS NOT NULL THEN ? ELSE stage_status END,
                    lease_expires_at = NULL
                WHERE job_id = ?
                """,
                (status, json_dumps(result), json_dumps(error) if error else None, now, now, status, job_id),
            )
            self._event_locked(
                "job_finished",
                {
                    "status": status,
                    "workflow_id": result.get("workflow_id"),
                    "stage_id": result.get("stage_id"),
                },
                job_id,
            )
            self._conn.commit()
            self._condition.notify_all()
            return self._copy_job_locked(job_id)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._condition:
            row = self._conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._job_from_row(row) if row else None

    def wait_for_job(self, job_id: str, timeout_ms: int) -> dict[str, Any] | None:
        deadline = time.monotonic() + (timeout_ms / 1000)
        with self._condition:
            while True:
                job = self.get_job(job_id)
                if job is None:
                    return None
                if job["status"] in FINAL_STATUSES:
                    return job
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return job
                self._condition.wait(timeout=remaining)

    def health(self) -> dict[str, Any]:
        with self._condition:
            latest = self._latest_heartbeat_row_locked()
            heartbeat = self._heartbeat_from_row(latest) if latest else None
            plugin_version = None
            uxp_age_seconds = None
            if heartbeat:
                payload = heartbeat.get("payload") or {}
                plugin_version = payload.get("plugin_version")
                uxp_age_seconds = heartbeat.get("age_seconds")
            return {
                "status": "ok",
                "schema_version": "ps-agent/v1",
                "server_time": utc_now(),
                "uxp_connected": self.uxp_connected(),
                "uxp_age_seconds": uxp_age_seconds,
                "plugin_version": plugin_version,
                "latest_heartbeat": heartbeat,
                "queue": self.stats_locked(),
                "last_error": self.last_error_locked(),
            }

    def diagnostics(self) -> dict[str, Any]:
        with self._condition:
            return {
                "db": {
                    "path": str(self.db_path),
                    "exists": self.db_path.is_file(),
                    "size_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
                    "schema_version": self._meta_locked("schema_version"),
                },
                "queue": self.stats_locked(),
                "active_jobs": self.active_jobs_locked(),
                "recent_events": self.recent_events_locked(20),
                "latest_heartbeat": self._heartbeat_from_row(self._latest_heartbeat_row_locked()),
                "last_error": self.last_error_locked(),
            }

    def stats_locked(self) -> dict[str, int]:
        statuses = {
            "pending": 0,
            "running": 0,
            "done": 0,
            "error": 0,
            "cancelled": 0,
            "expired": 0,
            "total": 0,
        }
        rows = self._conn.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
        for row in rows:
            status = str(row["status"])
            count = int(row["count"])
            statuses[status] = count
            statuses["total"] += count
        return statuses

    def active_jobs_locked(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM jobs
            WHERE status IN ('pending', 'running')
            ORDER BY created_at ASC
            LIMIT 10
            """
        ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def recent_events_locked(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM events
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        events = []
        for row in rows:
            events.append(
                {
                    "id": row["id"],
                    "created_at": row["created_at"],
                    "event_type": row["event_type"],
                    "job_id": row["job_id"],
                    "details": json_loads(row["details_json"], {}),
                }
            )
        return events

    def last_error_locked(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT * FROM events
            WHERE event_type IN ('job_finished', 'job_expired_after_stale_lease', 'backend_error')
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        details = json_loads(row["details_json"], {})
        if details.get("status") == "done":
            return None
        return {
            "created_at": row["created_at"],
            "event_type": row["event_type"],
            "job_id": row["job_id"],
            "details": details,
        }

    def _copy_job_locked(self, job_id: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job_from_row(row)

    def _job_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema_version": row["schema_version"],
            "job_id": row["job_id"],
            "job_type": row["job_type"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "timeout_ms": row["timeout_ms"],
            "status": row["status"],
            "payload": json_loads(row["payload_json"], {}),
            "result": json_loads(row["result_json"], None),
            "error": json_loads(row["error_json"], None),
            "max_attempts": row["max_attempts"],
            "attempts": row["attempts"],
            "claimed_by": row["claimed_by"],
            "claimed_at": row["claimed_at"],
            "lease_expires_at": row["lease_expires_at"],
            "completed_at": row["completed_at"],
            "workflow_id": row["workflow_id"],
            "stage_id": row["stage_id"],
            "parent_job_id": row["parent_job_id"],
            "stage_status": row["stage_status"],
        }

    def _latest_heartbeat_row_locked(self) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT * FROM heartbeats
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    def _heartbeat_from_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        age = max(0.0, epoch_now() - float(row["received_at_epoch"]))
        return {
            "received_at": row["received_at"],
            "age_seconds": round(age, 3),
            "payload": json_loads(row["payload_json"], {}),
        }

    def _meta_locked(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None
