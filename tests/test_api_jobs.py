from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest

from motionforge.api.server import SidecarServer
from motionforge.jobs.store import JobStore
from motionforge.models import JobResponse, JobStage, JobStatus, utc_now
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

    def test_health_is_authenticated_and_versioned(self) -> None:
        status, body = self.request("GET", "/v1/health", authorized=False)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")
        started = time.perf_counter()
        status, body = self.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["contractVersion"], 1)
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
