from __future__ import annotations

import math
import unittest

from pydantic import ValidationError

from motionforge.models import OverlaySpec, PhysicsObject, PhysicsSpec, PointReference, SceneSpec, VisualSpec


class SchemaTests(unittest.TestCase):
    def test_rejects_non_finite_and_out_of_bounds_values(self) -> None:
        with self.assertRaises(ValidationError):
            PhysicsSpec(duration=float("nan"), objects=[PhysicsObject(id="ball", shape="circle", radius=10)])
        with self.assertRaises(ValidationError):
            PhysicsObject(id="ball", shape="circle", radius=10, velocity=(20_001, 0))
        with self.assertRaises(ValidationError):
            PhysicsSpec(duration=31, objects=[PhysicsObject(id="ball", shape="circle", radius=10)])

    def test_rejects_invalid_colors_and_references(self) -> None:
        with self.assertRaises(ValidationError):
            SceneSpec(
                physics=PhysicsSpec(duration=1, objects=[PhysicsObject(id="ball", shape="circle", radius=10)]),
                visual={"backgroundColor": "white"},
            )
        with self.assertRaises(ValidationError):
            PhysicsSpec(
                duration=1,
                objects=[PhysicsObject(id="ball", shape="circle", radius=10)],
                forces=[{"appliesTo": ["missing"], "vector": [1, 0]}],
            )

    def test_polygon_winding_is_normalized_and_concavity_rejected(self) -> None:
        clockwise = PhysicsObject(
            id="poly",
            shape="polygon",
            vertices=[(-10, -10), (-10, 10), (10, 10), (10, -10)],
        )
        signed_area = sum(
            x1 * y2 - x2 * y1
            for (x1, y1), (x2, y2) in zip(clockwise.vertices, clockwise.vertices[1:] + clockwise.vertices[:1], strict=True)
        )
        self.assertGreater(signed_area, 0)
        with self.assertRaises(ValidationError):
            PhysicsObject(
                id="concave",
                shape="polygon",
                vertices=[(0, 0), (20, 0), (10, 5), (20, 20), (0, 20)],
            )

    def test_segment_must_be_static(self) -> None:
        with self.assertRaises(ValidationError):
            PhysicsObject(id="floor", shape="segment", point_a=(-10, 0), point_b=(10, 0))

    def test_derived_geometry_rejects_forward_overlay_references(self) -> None:
        with self.assertRaises(ValidationError):
            SceneSpec(
                physics=PhysicsSpec(
                    gravity=(0, 0),
                    duration=1,
                    objects=[PhysicsObject(id="walker", shape="box", width=1, height=2)],
                ),
                visual=VisualSpec(
                    overlays=[
                        OverlaySpec(
                            id="measurement",
                            kind="measurement",
                            start=PointReference(object_id="walker", anchor="bottom"),
                            end=PointReference(overlay_id="future", anchor="end"),
                            operation="deltaX",
                        ),
                        OverlaySpec(
                            id="future",
                            kind="line",
                            start=PointReference(point=(0, 0)),
                            end=PointReference(point=(1, 0)),
                        ),
                    ]
                ),
            )

    def test_camel_case_contract_serialization(self) -> None:
        scene = SceneSpec(
            physics=PhysicsSpec(duration=1, objects=[PhysicsObject(id="ball", shape="circle", radius=10)]),
            visual=VisualSpec(),
        )
        payload = scene.contract_dump()
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertIn("sceneSize", payload["visual"])
        self.assertNotIn("scene_size", payload["visual"])


if __name__ == "__main__":
    unittest.main()
