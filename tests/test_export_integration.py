from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from motionforge.core import export_video, simulate_scene
from motionforge.jobs import JobManager
from motionforge.models import (
    ExportOptions,
    ExportRequest,
    JobStatus,
    PhysicsObject,
    PhysicsSpec,
    SceneSpec,
    VisualSpec,
)
from motionforge.paths import app_paths


@unittest.skipUnless(os.environ.get("MOTIONFORGE_RUN_EXPORT_TESTS") == "1", "set MOTIONFORGE_RUN_EXPORT_TESTS=1 for native export tests")
class NativeExportTests(unittest.TestCase):
    def static_timeline(self, duration: float):
        return simulate_scene(
            SceneSpec(
                physics=PhysicsSpec(
                    duration=duration,
                    objects=[PhysicsObject(id="marker", shape="circle", radius=10, is_static=True)],
                ),
                visual=VisualSpec(title="Duration regression"),
            )
        )

    def moving_timeline(self, duration: float):
        return simulate_scene(
            SceneSpec(
                physics=PhysicsSpec(
                    gravity=(0, 0),
                    duration=duration,
                    objects=[
                        PhysicsObject(
                            id="marker",
                            shape="circle",
                            radius=12,
                            position=(-120, 0),
                            velocity=(120, 0),
                        )
                    ],
                ),
                visual=VisualSpec(title="Motion regression"),
            )
        )

    def probe(self, path: Path) -> dict:
        ffprobe = shutil.which("ffprobe")
        self.assertIsNotNone(ffprobe)
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,pix_fmt,nb_frames,duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(completed.stdout)["streams"][0]

    def frame_hashes(self, path: Path) -> list[str]:
        ffmpeg = shutil.which("ffmpeg")
        self.assertIsNotNone(ffmpeg)
        completed = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(path), "-f", "framemd5", "-"],
            capture_output=True,
            text=True,
            check=True,
        )
        return [line.rsplit(",", 1)[-1].strip() for line in completed.stdout.splitlines() if line and not line.startswith("#")]

    def test_three_second_video_duration_at_24_30_and_60_fps(self) -> None:
        timeline = self.static_timeline(3)
        with tempfile.TemporaryDirectory() as temporary:
            for fps in (24, 30, 60):
                with self.subTest(fps=fps):
                    output = Path(temporary) / f"duration-{fps}.mp4"
                    export_video(
                        timeline,
                        ExportOptions(preset="custom", width=320, height=180, fps=fps),
                        output_path=output,
                    )
                    stream = self.probe(output)
                    self.assertEqual(stream["codec_name"], "h264")
                    self.assertEqual(stream["pix_fmt"], "yuv420p")
                    self.assertEqual(int(stream["nb_frames"]), 3 * fps)
                    self.assertAlmostEqual(float(stream["duration"]), 3.0, places=3)

    def test_moving_timeline_changes_encoded_frames(self) -> None:
        timeline = self.moving_timeline(1)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "moving.mp4"
            export_video(
                timeline,
                ExportOptions(preset="custom", width=320, height=180, fps=12),
                output_path=output,
            )
            hashes = self.frame_hashes(output)
            self.assertEqual(len(hashes), 12)
            self.assertGreaterEqual(len(set(hashes)), 8)

    def test_job_manager_uses_isolated_export_worker(self) -> None:
        timeline = self.static_timeline(0.25)
        with tempfile.TemporaryDirectory() as temporary:
            manager = JobManager(app_paths(temporary))
            try:
                created = manager.start_export(
                    ExportRequest(
                        timeline=timeline,
                        options=ExportOptions(preset="custom", width=320, height=180, fps=24),
                    )
                )
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    job = manager.get(created.job_id)
                    if job and job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
                        break
                    time.sleep(0.1)
                self.assertIsNotNone(job)
                self.assertEqual(job.status, JobStatus.COMPLETED, job.error)
                self.assertTrue(Path(job.result["export"]["outputPath"]).is_file())
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
