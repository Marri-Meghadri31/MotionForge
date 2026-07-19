from __future__ import annotations

import json
import os
import platform
import secrets
import socket
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError

from motionforge.constants import CONTRACT_VERSION, ENGINE_VERSION, MAX_REQUEST_BYTES, SCHEMA_VERSION, TIMELINE_VERSION
from motionforge.errors import ErrorCode, MotionForgeError, validation_diagnostics
from motionforge.jobs import JobManager
from motionforge.models import CompileRequest, ExportRequest, JobResponse, JobStatus, SimulationRequest
from motionforge.paths import AppPaths, app_paths
from motionforge.render.manim_renderer import renderer_health


class SidecarServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], secret: str, paths: AppPaths) -> None:
        self.secret = secret
        self.started = time.perf_counter()
        self.manager = JobManager(paths)
        self.paths = paths
        super().__init__(address, MotionForgeHandler)

    def server_close(self) -> None:
        self.manager.close()
        super().server_close()


class MotionForgeHandler(BaseHTTPRequestHandler):
    server: SidecarServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._common_headers(0)
        self.end_headers()

    def do_GET(self) -> None:
        if not self._authorized():
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/v1/health":
            self._json(HTTPStatus.OK, self._health())
            return
        if path.startswith("/v1/jobs/") and path.endswith("/events"):
            job_id = path.split("/")[3]
            self._events(job_id)
            return
        if path.startswith("/v1/jobs/"):
            job_id = path.split("/")[3]
            job = self.server.manager.get(job_id)
            if job is None:
                self._error(HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND, "Job not found.")
            else:
                self._json(HTTPStatus.OK, job.contract_dump())
            return
        self._error(HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND, "Route not found.")

    def do_POST(self) -> None:
        if not self._authorized():
            return
        path = urlparse(self.path).path.rstrip("/")
        try:
            body = self._read_json()
            if path == "/v1/scenes/compile":
                job = self.server.manager.start_compile(CompileRequest.model_validate(body))
            elif path == "/v1/simulations":
                job = self.server.manager.start_simulation(SimulationRequest.model_validate(body))
            elif path == "/v1/exports":
                job = self.server.manager.start_export(ExportRequest.model_validate(body))
            else:
                self._error(HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND, "Route not found.")
                return
            self._json(HTTPStatus.ACCEPTED, job.contract_dump())
        except ValidationError as error:
            diagnostics = validation_diagnostics(error)
            code = ErrorCode.CONTRACT_MISMATCH if any(item["path"].endswith("contractVersion") for item in diagnostics) else ErrorCode.INVALID_REQUEST
            self._error(HTTPStatus.BAD_REQUEST, code, "Request validation failed.", diagnostics)
        except MotionForgeError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"contractVersion": CONTRACT_VERSION, "error": error.as_dict()})
        except (json.JSONDecodeError, ValueError):
            self._error(HTTPStatus.BAD_REQUEST, ErrorCode.INVALID_REQUEST, "Request body must be valid JSON.")

    def do_DELETE(self) -> None:
        if not self._authorized():
            return
        path = urlparse(self.path).path.rstrip("/")
        if path.startswith("/v1/jobs/"):
            job = self.server.manager.cancel(path.split("/")[3])
            if job is None:
                self._error(HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND, "Job not found.")
            else:
                self._json(HTTPStatus.OK, job.contract_dump())
            return
        self._error(HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND, "Route not found.")

    def _authorized(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else self.headers.get("X-MotionForge-Secret", "")
        if not secrets.compare_digest(supplied, self.server.secret):
            self._error(HTTPStatus.UNAUTHORIZED, ErrorCode.UNAUTHORIZED, "A valid launch secret is required.")
            return False
        return True

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("content length required")
        length = int(raw_length)
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise MotionForgeError(ErrorCode.INVALID_REQUEST, "Request body is too large.")
        payload = self.rfile.read(length)
        data = json.loads(payload.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    def _events(self, job_id: str) -> None:
        job = self.server.manager.get(job_id)
        if job is None:
            self._error(HTTPStatus.NOT_FOUND, ErrorCode.NOT_FOUND, "Job not found.")
            return
        try:
            after = int(self.headers.get("Last-Event-ID", "0"))
        except ValueError:
            after = 0
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        deadline = time.monotonic() + 300
        try:
            while time.monotonic() < deadline:
                events = self.server.manager.store.events(job_id, after)
                for sequence, payload in events:
                    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(f"id: {sequence}\nevent: job\ndata: {encoded}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    after = sequence
                    job = JobResponse.model_validate(payload)
                if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
                    self.close_connection = True
                    break
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _health(self) -> dict[str, Any]:
        return {
            "contractVersion": CONTRACT_VERSION,
            "service": "motionforge",
            "status": "ok",
            "engineVersion": ENGINE_VERSION,
            "schemaVersion": SCHEMA_VERSION,
            "timelineVersion": TIMELINE_VERSION,
            "uptimeSeconds": time.perf_counter() - self.server.started,
            "build": {
                "platform": sys.platform,
                "architecture": platform.machine(),
                "python": platform.python_version(),
                "packaged": bool(getattr(sys, "frozen", False)),
            },
            "renderers": renderer_health(),
            "providers": {
                "ollama": {
                    **_ollama_reachable(),
                    "capabilities": ["structuredOutput", "cancellation", "keepAlive", "local"],
                },
                "anthropic": {
                    "configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
                    "capabilities": ["text", "cloud"],
                },
            },
            "limits": {"requestBytes": MAX_REQUEST_BYTES, "exportConcurrency": 1},
        }

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._common_headers(len(body))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, code: ErrorCode, message: str, details: Any = None) -> None:
        error: dict[str, Any] = {"code": code.value, "message": message, "retriable": False}
        if details is not None:
            error["details"] = details
        self._json(status, {"contractVersion": CONTRACT_VERSION, "error": error})

    def _common_headers(self, length: int) -> None:
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
        self.send_header("Access-Control-Allow-Headers", "authorization, content-type, x-motionforge-secret, last-event-id")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")


def _ollama_reachable() -> dict[str, Any]:
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=0.05):
            return {"reachable": True, "local": True}
    except OSError:
        return {"reachable": False, "local": True}


def serve(port: int = 8765, secret: str | None = None, *, data_dir: str | Path | None = None) -> None:
    launch_secret = secret or secrets.token_urlsafe(32)
    server = SidecarServer(("127.0.0.1", port), launch_secret, app_paths(data_dir))
    startup = {
        "event": "ready",
        "contractVersion": CONTRACT_VERSION,
        "host": "127.0.0.1",
        "port": server.server_address[1],
        "secret": launch_secret,
        "pid": os.getpid(),
    }
    print(json.dumps(startup, separators=(",", ":")), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
