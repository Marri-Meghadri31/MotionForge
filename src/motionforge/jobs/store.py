from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any

from motionforge.errors import ErrorCode
from motionforge.models import JobError, JobResponse, JobStage, JobStatus, utc_now


class JobStore:
    """Small durable SQLite store with restart recovery and event history."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._initialize()
        self._recover_interrupted()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress REAL NOT NULL,
                    error_json TEXT,
                    result_json TEXT,
                    timings_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS job_events_job_id_sequence
                    ON job_events(job_id, sequence);
                """
            )

    def _recover_interrupted(self) -> None:
        now = utc_now()
        error = json.dumps(
            JobError(
                code=ErrorCode.INTERNAL_ERROR.value,
                message="The job was interrupted when MotionForge stopped.",
                retriable=True,
            ).contract_dump()
        )
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT job_id FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchall()
            connection.execute(
                """UPDATE jobs SET status='failed', stage='failed', error_json=?, updated_at=?
                   WHERE status IN ('queued', 'running')""",
                (error, now),
            )
            for row in rows:
                job = self._get_with_connection(connection, row["job_id"])
                if job:
                    self._append_event(connection, job)

    def create(self, job: JobResponse) -> JobResponse:
        payload = job.contract_dump()
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO jobs
                (job_id, kind, status, stage, progress, error_json, result_json, timings_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.job_id,
                    job.kind,
                    job.status.value,
                    job.stage.value,
                    job.progress,
                    _json_or_none(payload.get("error")),
                    _json_or_none(payload.get("result")),
                    json.dumps(payload.get("timings", {})),
                    job.created_at,
                    job.updated_at,
                ),
            )
            self._append_event(connection, job)
        return job

    def update(self, job_id: str, **changes: Any) -> JobResponse:
        with self._lock, self._connection() as connection:
            current = self._get_with_connection(connection, job_id)
            if current is None:
                raise KeyError(job_id)
            if current.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
                return current
            updated = current.model_copy(update={**changes, "updated_at": utc_now()})
            payload = updated.contract_dump()
            connection.execute(
                """UPDATE jobs SET status=?, stage=?, progress=?, error_json=?, result_json=?,
                   timings_json=?, updated_at=? WHERE job_id=?""",
                (
                    updated.status.value,
                    updated.stage.value,
                    updated.progress,
                    _json_or_none(payload.get("error")),
                    _json_or_none(payload.get("result")),
                    json.dumps(payload.get("timings", {})),
                    updated.updated_at,
                    job_id,
                ),
            )
            self._append_event(connection, updated)
            return updated

    def get(self, job_id: str) -> JobResponse | None:
        with self._connection() as connection:
            return self._get_with_connection(connection, job_id)

    def _get_with_connection(self, connection: sqlite3.Connection, job_id: str) -> JobResponse | None:
        row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return JobResponse(
            job_id=row["job_id"],
            kind=row["kind"],
            status=row["status"],
            stage=row["stage"],
            progress=row["progress"],
            error=json.loads(row["error_json"]) if row["error_json"] else None,
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            timings=json.loads(row["timings_json"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _append_event(self, connection: sqlite3.Connection, job: JobResponse) -> None:
        connection.execute(
            "INSERT INTO job_events(job_id, created_at, payload_json) VALUES (?, ?, ?)",
            (job.job_id, utc_now(), json.dumps(job.contract_dump(), ensure_ascii=False)),
        )

    def events(self, job_id: str, after: int = 0) -> list[tuple[int, dict[str, Any]]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT sequence, payload_json FROM job_events WHERE job_id=? AND sequence>? ORDER BY sequence",
                (job_id, after),
            ).fetchall()
        return [(row["sequence"], json.loads(row["payload_json"])) for row in rows]


def _json_or_none(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)
