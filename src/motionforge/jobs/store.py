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
                CREATE TABLE IF NOT EXISTS visualizations (
                    visualization_id TEXT PRIMARY KEY,
                    compile_request_json TEXT NOT NULL,
                    simulation_options_json TEXT NOT NULL,
                    current_job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS visualization_jobs (
                    visualization_id TEXT NOT NULL REFERENCES visualizations(visualization_id) ON DELETE CASCADE,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    PRIMARY KEY (visualization_id, job_id)
                );
                CREATE INDEX IF NOT EXISTS visualization_jobs_visualization_id
                    ON visualization_jobs(visualization_id);
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

    def create_visualization(
        self,
        visualization_id: str,
        compile_request: dict[str, Any],
        simulation_options: dict[str, Any],
        job_id: str,
    ) -> None:
        now = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO visualizations
                (visualization_id, compile_request_json, simulation_options_json, current_job_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    visualization_id,
                    json.dumps(compile_request, ensure_ascii=False),
                    json.dumps(simulation_options, ensure_ascii=False),
                    job_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO visualization_jobs(visualization_id, job_id, role) VALUES (?, ?, 'visualization')",
                (visualization_id, job_id),
            )

    def replace_visualization_job(
        self,
        visualization_id: str,
        compile_request: dict[str, Any],
        simulation_options: dict[str, Any],
        job_id: str,
    ) -> None:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """UPDATE visualizations SET compile_request_json=?, simulation_options_json=?,
                   current_job_id=?, updated_at=? WHERE visualization_id=?""",
                (
                    json.dumps(compile_request, ensure_ascii=False),
                    json.dumps(simulation_options, ensure_ascii=False),
                    job_id,
                    utc_now(),
                    visualization_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(visualization_id)
            connection.execute(
                "INSERT INTO visualization_jobs(visualization_id, job_id, role) VALUES (?, ?, 'visualization')",
                (visualization_id, job_id),
            )

    def link_visualization_job(self, visualization_id: str, job_id: str, role: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO visualization_jobs(visualization_id, job_id, role) VALUES (?, ?, ?)",
                (visualization_id, job_id, role),
            )

    def get_visualization(self, visualization_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM visualizations WHERE visualization_id=?",
                (visualization_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "visualizationId": row["visualization_id"],
            "compileRequest": json.loads(row["compile_request_json"]),
            "simulationOptions": json.loads(row["simulation_options_json"]),
            "currentJobId": row["current_job_id"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def visualization_jobs(self, visualization_id: str) -> list[tuple[str, str]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT job_id, role FROM visualization_jobs WHERE visualization_id=? ORDER BY rowid",
                (visualization_id,),
            ).fetchall()
        return [(row["job_id"], row["role"]) for row in rows]

    def visualization_events(self, visualization_id: str, after: int = 0) -> list[tuple[int, dict[str, Any]]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT event.sequence, event.payload_json
                   FROM job_events AS event
                   JOIN visualization_jobs AS link ON link.job_id=event.job_id
                   WHERE link.visualization_id=? AND event.sequence>?
                   ORDER BY event.sequence""",
                (visualization_id, after),
            ).fetchall()
        return [(row["sequence"], json.loads(row["payload_json"])) for row in rows]


def _json_or_none(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)
