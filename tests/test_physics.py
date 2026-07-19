from __future__ import annotations

import math
import unittest
from threading import Event

from motionforge.errors import ErrorCode, MotionForgeError
from motionforge.models import PhysicsObject, PhysicsSpec, SimulationOptions
from motionforge.physics.simulator import simulate


def state(result, object_id: str, index: int = -1):
    return result.frames[index]["objects"][object_id]


class PhysicsCorrectnessTests(unittest.TestCase):
    def test_exact_initial_and_final_timestamps_with_partial_last_step(self) -> None:
        spec = PhysicsSpec(
            duration=0.1,
            dt=0.03,
            gravity=(0, 0),
            objects=[PhysicsObject(id="ball", shape="circle", radius=1)],
        )
        result = simulate(spec)
        self.assertEqual([round(frame["t"], 8) for frame in result.frames], [0, 0.03, 0.06, 0.09, 0.1])

    def test_free_fall_matches_analytic_velocity_and_position(self) -> None:
        spec = PhysicsSpec(
            duration=1,
            dt=1 / 240,
            gravity=(0, -9.81),
            objects=[PhysicsObject(id="ball", shape="circle", radius=0.1, position=(0, 10))],
        )
        result = simulate(spec)
        final = state(result, "ball")
        self.assertAlmostEqual(final["vy"], -9.81, delta=0.02)
        self.assertAlmostEqual(final["y"], 10 - 0.5 * 9.81, delta=0.04)

    def test_bounce_generates_collision_and_reaches_expected_fractional_height(self) -> None:
        spec = PhysicsSpec(
            duration=4,
            dt=1 / 120,
            gravity=(0, -100),
            objects=[
                PhysicsObject(id="floor", shape="segment", point_a=(-200, 0), point_b=(200, 0), is_static=True, restitution=0.8),
                PhysicsObject(id="ball", shape="circle", radius=10, position=(0, 100), restitution=0.8),
            ],
        )
        result = simulate(spec)
        collision = next(event for event in result.events if event.type == "collision")
        after_collision = [frame["objects"]["ball"]["y"] for frame in result.frames if frame["t"] > collision.time + 0.2]
        self.assertGreater(max(after_collision), 55)

    def test_sliding_friction_decelerates(self) -> None:
        spec = PhysicsSpec(
            duration=2,
            dt=1 / 120,
            gravity=(0, -100),
            objects=[
                PhysicsObject(id="floor", shape="segment", point_a=(-500, 0), point_b=(500, 0), is_static=True, friction=1),
                PhysicsObject(id="box", shape="box", width=20, height=20, position=(0, 11), velocity=(100, 0), friction=1, restitution=0),
            ],
        )
        result = simulate(spec)
        self.assertLess(abs(state(result, "box")["vx"]), 15)

    def test_projectile_apex_time(self) -> None:
        spec = PhysicsSpec(
            duration=1.5,
            dt=1 / 120,
            gravity=(0, -100),
            objects=[PhysicsObject(id="ball", shape="circle", radius=1, velocity=(50, 70))],
        )
        result = simulate(spec)
        apex = next(event for event in result.events if event.type == "apex")
        self.assertAlmostEqual(apex.time, 0.7, delta=0.02)
        self.assertAlmostEqual(state(result, "ball")["x"], 75, delta=0.2)

    def test_elastic_collision_conserves_momentum(self) -> None:
        spec = PhysicsSpec(
            duration=3,
            dt=1 / 120,
            gravity=(0, 0),
            objects=[
                PhysicsObject(id="a", shape="circle", radius=10, position=(-40, 0), velocity=(20, 0), mass=1, restitution=1, friction=0),
                PhysicsObject(id="b", shape="circle", radius=10, position=(40, 0), velocity=(-10, 0), mass=2, restitution=1, friction=0),
            ],
        )
        result = simulate(spec)
        initial = sum(state(result, name, 0)["momentum_x"] for name in ("a", "b"))
        final = sum(state(result, name)["momentum_x"] for name in ("a", "b"))
        self.assertAlmostEqual(initial, final, delta=0.05)
        self.assertTrue(any(event.type == "collision" for event in result.events))

    def test_constant_force_applies_to_dynamic_target(self) -> None:
        spec = PhysicsSpec(
            duration=1,
            dt=1 / 120,
            gravity=(0, 0),
            objects=[PhysicsObject(id="box", shape="box", width=2, height=2, mass=2)],
            forces=[{"appliesTo": ["box"], "vector": [10, 0]}],
        )
        result = simulate(spec)
        self.assertAlmostEqual(state(result, "box")["vx"], 5, delta=0.05)

    def test_cancellation_is_checked_before_simulation_work(self) -> None:
        cancellation = Event()
        cancellation.set()
        spec = PhysicsSpec(
            duration=1,
            objects=[PhysicsObject(id="ball", shape="circle", radius=1)],
        )
        with self.assertRaises(MotionForgeError) as raised:
            simulate(spec, cancel_event=cancellation)
        self.assertEqual(raised.exception.code, ErrorCode.CANCELLED)


if __name__ == "__main__":
    unittest.main()
