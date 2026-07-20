from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from motionforge.cache import JsonCache, cache_key
from motionforge.compiler.templates import compile_template
from motionforge.core import simulate_scene
from motionforge.models import ObjectTrack
from motionforge.render.manim_renderer import output_frame_times
from motionforge.timeline.converter import from_legacy_keyframes, sample_overlay_track, sample_track


class TimelineTests(unittest.TestCase):
    def test_static_data_is_stored_once_and_dynamic_track_is_complete(self) -> None:
        timeline = simulate_scene(compile_template("falling-body", "drop", {}))
        self.assertEqual(timeline.tracks["ground"].times, [0])
        self.assertEqual(len(timeline.tracks["ball"].times), 181)
        payload = timeline.contract_dump()
        self.assertIn("objects", payload["scene"])
        self.assertNotIn("objects", payload["tracks"]["ball"])

    def test_linear_and_shortest_angle_interpolation(self) -> None:
        track = ObjectTrack(times=[0, 1], x=[0, 10], y=[10, 20], angle=[math_radians(170), math_radians(-170)])
        sample = sample_track(track, 0.5)
        self.assertAlmostEqual(sample["x"], 5)
        self.assertAlmostEqual(abs(sample["angle"]), math_radians(180), delta=1e-8)

    def test_legacy_reader(self) -> None:
        legacy = [
            {
                "t": 0,
                "dt": 0.5,
                "objects": {"ball": {"x": 0, "y": 0, "angle": 0, "shape": "circle", "radius": 2, "is_static": False}},
            },
            {
                "t": 0.5,
                "dt": 0.5,
                "objects": {"ball": {"x": 1, "y": 2, "angle": 0, "shape": "circle", "radius": 2, "is_static": False}},
            },
        ]
        timeline = from_legacy_keyframes(legacy)
        self.assertEqual(timeline.tracks["ball"].x, [0, 1])

    def test_export_sampling_has_exact_frame_count(self) -> None:
        for fps in (24, 30, 60):
            times = output_frame_times(3, fps)
            self.assertEqual(len(times), 3 * fps)
            self.assertAlmostEqual(times[-1], 3 - 1 / fps)

    def test_geometry_tracks_follow_physics_and_measure_relative_shadow_speed(self) -> None:
        timeline = simulate_scene(compile_template("lamp-shadow", "lamp post shadow", {}))
        ray = timeline.overlay_tracks["light-ray"]
        shadow = timeline.overlay_tracks["shadow-length"]
        self.assertAlmostEqual(ray.end_x[0], 200)
        self.assertAlmostEqual(ray.end_x[-1], 800)
        self.assertAlmostEqual(shadow.value[0], 80)
        self.assertAlmostEqual(shadow.value[-1], 320)
        self.assertAlmostEqual((shadow.value[-1] - shadow.value[0]) / timeline.duration, 40)
        halfway = sample_overlay_track(shadow, timeline.duration / 2)
        self.assertAlmostEqual(float(halfway["value"]), 200)


class CacheTests(unittest.TestCase):
    def test_cache_keys_change_with_options_and_round_trip_atomically(self) -> None:
        first = cache_key("timeline", {"fps": 30})
        second = cache_key("timeline", {"fps": 60})
        self.assertNotEqual(first, second)
        with tempfile.TemporaryDirectory() as temporary:
            cache = JsonCache(Path(temporary))
            cache.put("timelines", first, {"value": 42})
            self.assertEqual(cache.get("timelines", first), {"value": 42})


def math_radians(value: float) -> float:
    import math

    return math.radians(value)


if __name__ == "__main__":
    unittest.main()
