from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from motionforge.api.server import SidecarServer
from motionforge.errors import ErrorCode, MotionForgeError
from motionforge.jobs.store import JobStore
from motionforge.models import ExportResult, JobResponse, JobStage, JobStatus, utc_now
from motionforge.paths import app_paths


class SidecarIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.server = SidecarServer(("127.0.0.1", 0), "test-secret", app_paths(self.temporary.name))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, method: str, path: str, payload=None, authorized: bool = True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"content-type": "application/json"}
        if authorized:
            headers["authorization"] = "Bearer test-secret"
        body = json.dumps(payload).encode() if payload is not None else None
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def wait_for_job(self, job_id: str):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, job = self.request("GET", f"/v1/jobs/{job_id}")
            self.assertEqual(status, 200)
            if job["status"] in {"completed", "failed", "cancelled"}:
                return job
            time.sleep(0.02)
        self.fail("job did not complete")

    def wait_for_visualization(self, visualization_id: str):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, visualization = self.request("GET", f"/v1/visualizations/{visualization_id}")
            self.assertEqual(status, 200)
            if visualization["status"] in {"completed", "failed", "cancelled"}:
                return visualization
            time.sleep(0.02)
        self.fail("visualization did not complete")

    def test_health_is_authenticated_and_versioned(self) -> None:
        status, body = self.request("GET", "/v1/health", authorized=False)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")
        started = time.perf_counter()
        status, body = self.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["contractVersion"], 1)
        self.assertTrue(body["capabilities"]["visualizations"])
        self.assertLess(time.perf_counter() - started, 0.2)

    def test_compile_then_simulate_via_job_references(self) -> None:
        status, compile_job = self.request("POST", "/v1/scenes/compile", {"contractVersion": 1, "prompt": "projectile motion"})
        self.assertEqual(status, 202)
        compiled = self.wait_for_job(compile_job["jobId"])
        self.assertEqual(compiled["status"], "completed", compiled.get("error"))
        self.assertEqual(compiled["result"]["scene"]["metadata"]["origin"], "template")

        status, simulation_job = self.request(
            "POST",
            "/v1/simulations",
            {"contractVersion": 1, "compileJobId": compile_job["jobId"]},
        )
        self.assertEqual(status, 202)
        simulated = self.wait_for_job(simulation_job["jobId"])
        self.assertEqual(simulated["status"], "completed", simulated.get("error"))
        timeline = simulated["result"]["timeline"]
        self.assertEqual(timeline["duration"], 3)
        self.assertIn("projectile", timeline["tracks"])

    def test_contract_mismatch_has_stable_error(self) -> None:
        status, body = self.request("POST", "/v1/scenes/compile", {"contractVersion": 2, "prompt": "falling ball"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "CONTRACT_MISMATCH")
        status, body = self.request("POST", "/v1/visualizations", {"contractVersion": 2, "prompt": "falling ball"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "CONTRACT_MISMATCH")

    def test_completed_job_events_are_available_as_sse(self) -> None:
        status, started = self.request("POST", "/v1/scenes/compile", {"prompt": "falling ball"})
        self.assertEqual(status, 202)
        completed = self.wait_for_job(started["jobId"])
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(
            "GET",
            f"/v1/jobs/{completed['jobId']}/events",
            headers={"authorization": "Bearer test-secret"},
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn("event: job", body)
        self.assertIn('"status":"completed"', body)

    def test_visualization_create_get_timeline_and_parameter_update(self) -> None:
        status, created = self.request(
            "POST",
            "/v1/visualizations",
            {"contractVersion": 1, "prompt": "projectile motion"},
        )
        self.assertEqual(status, 202)
        visualization_id = created["visualizationId"]
        first_job_id = created["jobId"]
        completed = self.wait_for_visualization(visualization_id)
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        self.assertNotIn("timeline", completed)
        self.assertEqual(completed["scene"]["metadata"]["templateId"], "projectile-motion")

        status, timeline_payload = self.request("GET", f"/v1/visualizations/{visualization_id}/timeline")
        self.assertEqual(status, 200)
        first_timeline = timeline_payload["timeline"]

        status, updating = self.request(
            "POST",
            f"/v1/visualizations/{visualization_id}/parameters",
            {"contractVersion": 1, "parameters": {"speed": 500}},
        )
        self.assertEqual(status, 202)
        self.assertEqual(updating["visualizationId"], visualization_id)
        self.assertNotEqual(updating["jobId"], first_job_id)
        updated = self.wait_for_visualization(visualization_id)
        self.assertEqual(updated["status"], "completed", updated.get("error"))
        self.assertEqual(updated["parameterValues"]["speed"], 500)

        status, updated_timeline_payload = self.request("GET", f"/v1/visualizations/{visualization_id}/timeline")
        self.assertEqual(status, 200)
        self.assertNotEqual(first_timeline["sourceSceneHash"], updated_timeline_payload["timeline"]["sourceSceneHash"])

    def test_visualization_export_and_sse_routes(self) -> None:
        status, created = self.request("POST", "/v1/visualizations", {"prompt": "falling ball"})
        self.assertEqual(status, 202)
        visualization_id = created["visualizationId"]
        completed = self.wait_for_visualization(visualization_id)
        self.assertEqual(completed["status"], "completed", completed.get("error"))

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(
            "GET",
            f"/v1/visualizations/{visualization_id}/events",
            headers={"authorization": "Bearer test-secret"},
        )
        response = connection.getresponse()
        events = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn("event: visualization", events)
        self.assertIn(f'"visualizationId":"{visualization_id}"', events)

        def fake_export(job_id, timeline, request, cancel):
            output = self.server.paths.exports / job_id / "animation.mp4"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"fake-mp4")
            width, height, fps = request.options.resolved()
            return ExportResult(
                output_path=str(output),
                duration=timeline.duration,
                width=width,
                height=height,
                fps=fps,
                size_bytes=output.stat().st_size,
                render_seconds=0.01,
            )

        with patch.object(self.server.manager, "_isolated_export", side_effect=fake_export):
            status, export_job = self.request(
                "POST",
                f"/v1/visualizations/{visualization_id}/exports",
                {"contractVersion": 1, "options": {"preset": "preview"}},
            )
            self.assertEqual(status, 202)
            self.assertEqual(export_job["visualizationId"], visualization_id)
            exported = self.wait_for_job(export_job["jobId"])
        self.assertEqual(exported["status"], "completed", exported.get("error"))
        self.assertTrue(Path(exported["result"]["export"]["outputPath"]).is_file())

    def test_visualization_cancel_stops_active_work(self) -> None:
        entered = threading.Event()

        def slow_compile(request, *, cancel_event, progress):
            entered.set()
            cancel_event.wait(2)
            raise MotionForgeError(ErrorCode.CANCELLED, "Visualization was cancelled.")

        with patch("motionforge.jobs.manager.compile_scene", side_effect=slow_compile):
            status, created = self.request("POST", "/v1/visualizations", {"prompt": "falling ball"})
            self.assertEqual(status, 202)
            self.assertTrue(entered.wait(1))
            visualization_id = created["visualizationId"]
            status, cancelled = self.request("DELETE", f"/v1/visualizations/{visualization_id}")
            self.assertEqual(status, 200)
            self.assertIn(created["jobId"], cancelled["cancelledJobIds"])
            finished = self.wait_for_visualization(visualization_id)
        self.assertEqual(finished["status"], "cancelled")

    def test_visualization_cancel_stops_linked_export(self) -> None:
        status, created = self.request("POST", "/v1/visualizations", {"prompt": "falling ball"})
        self.assertEqual(status, 202)
        visualization_id = created["visualizationId"]
        completed = self.wait_for_visualization(visualization_id)
        self.assertEqual(completed["status"], "completed", completed.get("error"))
        entered = threading.Event()

        def slow_export(job_id, timeline, request, cancel):
            entered.set()
            cancel.wait(2)
            raise MotionForgeError(ErrorCode.CANCELLED, "Video export was cancelled.")

        with patch.object(self.server.manager, "_isolated_export", side_effect=slow_export):
            status, export_job = self.request(
                "POST",
                f"/v1/visualizations/{visualization_id}/exports",
                {"options": {"preset": "preview"}},
            )
            self.assertEqual(status, 202)
            self.assertTrue(entered.wait(1))
            status, cancelled = self.request("DELETE", f"/v1/visualizations/{visualization_id}")
            self.assertEqual(status, 200)
            self.assertIn(export_job["jobId"], cancelled["cancelledJobIds"])
            finished = self.wait_for_job(export_job["jobId"])
        self.assertEqual(finished["status"], "cancelled")


class JobRecoveryTests(unittest.TestCase):
    def test_restart_marks_running_jobs_as_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = app_paths(temporary).database
            store = JobStore(path)
            now = utc_now()
            store.create(
                JobResponse(
                    job_id="interrupted",
                    kind="simulation",
                    status=JobStatus.RUNNING,
                    stage=JobStage.SIMULATING,
                    progress=0.5,
                    created_at=now,
                    updated_at=now,
                )
            )
            recovered = JobStore(path).get("interrupted")
            self.assertEqual(recovered.status, JobStatus.FAILED)
            self.assertIn("interrupted", recovered.error.message.lower())


if __name__ == "__main__":
    unittest.main()
