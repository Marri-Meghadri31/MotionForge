from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock, Semaphore
from typing import Any, Callable
from uuid import uuid4

from pydantic import ValidationError

from motionforge.cache import JsonCache, cache_key
from motionforge.constants import RENDERER_VERSION
from motionforge.core import compile_scene, simulate_scene
from motionforge.errors import ErrorCode, MotionForgeError, validation_diagnostics
from motionforge.jobs.store import JobStore
from motionforge.models import (
    CompileRequest,
    ExportRequest,
    ExportResult,
    JobError,
    JobResponse,
    JobStage,
    JobStatus,
    SceneSpec,
    SimulationRequest,
    Timeline,
    utc_now,
)
from motionforge.paths import AppPaths


class JobManager:
    def __init__(self, paths: AppPaths, *, max_workers: int = 4, max_exports: int = 1) -> None:
        self.paths = paths.ensure()
        self.store = JobStore(paths.database)
        self.cache = JsonCache(paths.cache)
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="motionforge-job")
        self.export_slots = Semaphore(max_exports)
        self._cancellations: dict[str, Event] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._lock = Lock()
        self.logger = logging.getLogger(f"motionforge.jobs.{id(self)}")
        self.logger.setLevel(logging.INFO)
        handler = RotatingFileHandler(paths.logs / "motionforge.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(handler)
        self.logger.propagate = False

    def _create(self, kind: str) -> JobResponse:
        now = utc_now()
        job = JobResponse(
            job_id=uuid4().hex,
            kind=kind,
            status=JobStatus.QUEUED,
            stage=JobStage.QUEUED,
            progress=0,
            created_at=now,
            updated_at=now,
        )
        self.store.create(job)
        with self._lock:
            self._cancellations[job.job_id] = Event()
        return job

    def start_compile(self, request: CompileRequest) -> JobResponse:
        job = self._create("compile")
        self.executor.submit(self._run_compile, job.job_id, request)
        return job

    def start_simulation(self, request: SimulationRequest) -> JobResponse:
        job = self._create("simulation")
        self.executor.submit(self._run_simulation, job.job_id, request)
        return job

    def start_export(self, request: ExportRequest) -> JobResponse:
        job = self._create("export")
        self.executor.submit(self._run_export, job.job_id, request)
        return job

    def _run_compile(self, job_id: str, request: CompileRequest) -> None:
        started = time.perf_counter()
        cancel = self._cancellations[job_id]
        try:
            if cancel.is_set():
                raise MotionForgeError(ErrorCode.CANCELLED, "Compilation was cancelled.")
            self.store.update(job_id, status=JobStatus.RUNNING, stage=JobStage.COMPILING, progress=0.02)
            key_payload = request.contract_dump(exclude={"timeout_seconds", "privacy"})
            key = cache_key("scene", key_payload, extra_versions={"provider": request.provider, "model": request.model})
            cached = self.cache.get("scenes", key)
            if cached:
                scene = SceneSpec.model_validate(cached)
                cache_hit = True
            else:
                def progress(stage: str, value: float) -> None:
                    self.store.update(job_id, stage=JobStage(stage), progress=min(0.95, value * 0.95))

                scene = compile_scene(request, cancel_event=cancel, progress=progress)
                self.cache.put("scenes", key, scene)
                cache_hit = False
            elapsed = time.perf_counter() - started
            if cancel.is_set():
                raise MotionForgeError(ErrorCode.CANCELLED, "Compilation was cancelled.")
            self.store.update(
                job_id,
                status=JobStatus.COMPLETED,
                stage=JobStage.READY,
                progress=1,
                result={"scene": scene.contract_dump(), "cacheHit": cache_hit},
                timings={"compileSeconds": elapsed},
            )
        except Exception as error:
            self._fail(job_id, error)
        finally:
            self._forget(job_id)

    def _run_simulation(self, job_id: str, request: SimulationRequest) -> None:
        started = time.perf_counter()
        cancel = self._cancellations[job_id]
        try:
            if cancel.is_set():
                raise MotionForgeError(ErrorCode.CANCELLED, "Simulation was cancelled.")
            self.store.update(job_id, status=JobStatus.RUNNING, stage=JobStage.SIMULATING, progress=0.02)
            scene = request.scene or self._scene_from_job(request.compile_job_id or "")
            key = cache_key("timeline", {"scene": scene, "options": request.options})
            cached = self.cache.get("timelines", key)
            if cached:
                timeline = Timeline.model_validate(cached)
                cache_hit = True
            else:
                def progress(stage: str, value: float) -> None:
                    self.store.update(job_id, stage=JobStage(stage), progress=min(0.98, value))

                timeline = simulate_scene(scene, request.options, cancel_event=cancel, progress=progress)
                self.cache.put("timelines", key, timeline)
                cache_hit = False
            elapsed = time.perf_counter() - started
            if cancel.is_set():
                raise MotionForgeError(ErrorCode.CANCELLED, "Simulation was cancelled.")
            self.store.update(
                job_id,
                status=JobStatus.COMPLETED,
                stage=JobStage.READY,
                progress=1,
                result={"timeline": timeline.contract_dump(), "cacheHit": cache_hit},
                timings={"simulationAndTimelineSeconds": elapsed},
            )
        except Exception as error:
            self._fail(job_id, error)
        finally:
            self._forget(job_id)

    def _run_export(self, job_id: str, request: ExportRequest) -> None:
        cancel = self._cancellations[job_id]
        started = time.perf_counter()
        try:
            if cancel.is_set():
                raise MotionForgeError(ErrorCode.CANCELLED, "Video export was cancelled.")
            timeline = request.timeline or self._timeline_from_job(request.simulation_job_id or "")
            self.store.update(job_id, status=JobStatus.RUNNING, stage=JobStage.EXPORTING, progress=0.01)
            export_key = cache_key(
                "export",
                {"sourceSceneHash": timeline.source_scene_hash, "timeline": timeline, "options": request.options},
                extra_versions={"renderer": RENDERER_VERSION},
            )
            cached = self.cache.get("exports", export_key)
            if cached:
                candidate = ExportResult.model_validate(cached)
                if Path(candidate.output_path).is_file():
                    self.store.update(
                        job_id,
                        status=JobStatus.COMPLETED,
                        stage=JobStage.COMPLETED,
                        progress=1,
                        result={"export": candidate.contract_dump(), "cacheHit": True},
                        timings={"exportSeconds": time.perf_counter() - started},
                    )
                    return
            acquired = False
            while not acquired:
                if cancel.wait(0.1):
                    raise MotionForgeError(ErrorCode.CANCELLED, "Video export was cancelled.")
                acquired = self.export_slots.acquire(timeout=0.1)
            try:
                result = self._isolated_export(job_id, timeline, request, cancel)
            finally:
                self.export_slots.release()
            self.cache.put("exports", export_key, result)
            self.store.update(
                job_id,
                status=JobStatus.COMPLETED,
                stage=JobStage.COMPLETED,
                progress=1,
                result={"export": result.contract_dump(), "cacheHit": False},
                timings={"exportSeconds": time.perf_counter() - started},
            )
        except Exception as error:
            self._fail(job_id, error)
        finally:
            self._forget(job_id)

    def _isolated_export(self, job_id: str, timeline: Timeline, request: ExportRequest, cancel: Event) -> ExportResult:
        job_directory = (self.paths.jobs / job_id).resolve()
        if self.paths.jobs.resolve() not in job_directory.parents:
            raise MotionForgeError(ErrorCode.EXPORT_FAILED, "Invalid export job path.")
        job_directory.mkdir(parents=True, exist_ok=True)
        timeline_path = job_directory / "timeline.json"
        options_path = job_directory / "options.json"
        result_path = job_directory / "result.json"
        error_path = job_directory / "error.json"
        output_path = (self.paths.exports / job_id / "animation.mp4").resolve()
        if self.paths.exports.resolve() not in output_path.parents:
            raise MotionForgeError(ErrorCode.EXPORT_FAILED, "Invalid export output path.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(timeline_path, timeline.contract_dump())
        _atomic_json(options_path, request.options.contract_dump())

        if getattr(sys, "frozen", False):
            command = [sys.executable, "_export-worker"]
        else:
            command = [sys.executable, "-m", "motionforge", "_export-worker"]
        command.extend([str(timeline_path), str(options_path), str(output_path), str(result_path), str(error_path)])
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=job_directory,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            start_new_session=sys.platform != "win32",
        )
        with self._lock:
            self._processes[job_id] = process
        deadline = time.monotonic() + request.options.timeout_seconds
        try:
            while process.poll() is None:
                if cancel.wait(0.2):
                    _terminate_process_tree(process)
                    raise MotionForgeError(ErrorCode.CANCELLED, "Video export was cancelled.")
                if time.monotonic() > deadline:
                    _terminate_process_tree(process)
                    raise MotionForgeError(ErrorCode.TIMEOUT, "Video export exceeded its time limit.")
            stdout, stderr = process.communicate(timeout=5)
            if process.returncode != 0:
                details: Any = stderr[-2_000:] or stdout[-2_000:]
                if error_path.is_file():
                    try:
                        payload = json.loads(error_path.read_text(encoding="utf-8"))
                        raise MotionForgeError(
                            ErrorCode(payload.get("code", ErrorCode.EXPORT_FAILED.value)),
                            payload.get("message", "Video export failed."),
                            details=payload.get("details", details),
                        )
                    except (ValueError, json.JSONDecodeError):
                        pass
                raise MotionForgeError(ErrorCode.EXPORT_FAILED, "Video export worker failed.", details=details)
            if not result_path.is_file():
                raise MotionForgeError(ErrorCode.EXPORT_FAILED, "Video export worker returned no result.")
            return ExportResult.model_validate_json(result_path.read_text(encoding="utf-8"))
        finally:
            with self._lock:
                self._processes.pop(job_id, None)

    def _scene_from_job(self, job_id: str) -> SceneSpec:
        job = self.store.get(job_id)
        if not job or job.kind != "compile" or job.status != JobStatus.COMPLETED or not job.result:
            raise MotionForgeError(ErrorCode.NOT_FOUND, "The compile job is not ready or does not exist.")
        return SceneSpec.model_validate(job.result["scene"])

    def _timeline_from_job(self, job_id: str) -> Timeline:
        job = self.store.get(job_id)
        if not job or job.kind != "simulation" or job.status != JobStatus.COMPLETED or not job.result:
            raise MotionForgeError(ErrorCode.NOT_FOUND, "The simulation job is not ready or does not exist.")
        return Timeline.model_validate(job.result["timeline"])

    def get(self, job_id: str) -> JobResponse | None:
        return self.store.get(job_id)

    def cancel(self, job_id: str) -> JobResponse | None:
        job = self.store.get(job_id)
        if job is None:
            return None
        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            return job
        with self._lock:
            cancellation = self._cancellations.get(job_id)
            process = self._processes.get(job_id)
        if cancellation:
            cancellation.set()
        if process:
            _terminate_process_tree(process)
        return self.store.update(
            job_id,
            status=JobStatus.CANCELLED,
            stage=JobStage.CANCELLED,
            progress=job.progress,
            error=JobError(code=ErrorCode.CANCELLED.value, message="The job was cancelled."),
        )

    def _fail(self, job_id: str, error: Exception) -> None:
        self.logger.error(
            "job=%s failed: %s",
            job_id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        current = self.store.get(job_id)
        if current and current.status == JobStatus.CANCELLED:
            return
        if isinstance(error, MotionForgeError):
            job_error = JobError(**error.as_dict())
            status = JobStatus.CANCELLED if error.code == ErrorCode.CANCELLED else JobStatus.FAILED
            stage = JobStage.CANCELLED if status == JobStatus.CANCELLED else JobStage.FAILED
        elif isinstance(error, ValidationError):
            job_error = JobError(code=ErrorCode.INVALID_REQUEST.value, message="The job input is invalid.", details=validation_diagnostics(error))
            status, stage = JobStatus.FAILED, JobStage.FAILED
        else:
            job_error = JobError(code=ErrorCode.INTERNAL_ERROR.value, message="MotionForge could not complete the job.", details=str(error))
            status, stage = JobStatus.FAILED, JobStage.FAILED
        self.store.update(job_id, status=status, stage=stage, error=job_error)

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._cancellations.pop(job_id, None)

    def close(self) -> None:
        with self._lock:
            cancellations = list(self._cancellations.values())
            processes = list(self._processes.values())
        for cancellation in cancellations:
            cancellation.set()
        for process in processes:
            _terminate_process_tree(process)
        self.executor.shutdown(wait=False, cancel_futures=True)
        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        process.kill()
