from __future__ import annotations

import json
import math
import unittest
from pathlib import Path
from threading import Event

from motionforge.compiler.scene_compiler import SYSTEM_PROMPT, SceneCompiler, repair_scene_data
from motionforge.compiler.templates import TEMPLATES, classify_template, compile_template
from motionforge.models import CompileRequest, SceneSpec
from motionforge.errors import ErrorCode, MotionForgeError
from motionforge.providers.base import Provider, ProviderCapabilities
from motionforge.providers.ollama import OllamaProvider


class FakeProvider(Provider):
    name = "fake"
    model = "test"
    capabilities = ProviderCapabilities(True, False, True, False, True)

    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.calls = 0

    def health(self):
        return {"available": True}

    def list_models(self):
        return [self.model]

    def generate_text(self, system, messages, *, request_id, cancel_event):
        self.calls += 1
        return next(self.responses)

    def generate_structured(self, system, messages, schema, *, request_id, cancel_event):
        self.calls += 1
        self.last_schema = schema
        return next(self.responses)

    def cancel(self, request_id):
        return None


class CompilerTests(unittest.TestCase):
    def test_all_documented_template_families_compile(self) -> None:
        self.assertEqual(len(TEMPLATES), 10)
        for template_id in TEMPLATES:
            with self.subTest(template=template_id):
                scene = compile_template(template_id, template_id, {})
                self.assertEqual(scene.metadata.origin, "template")
                self.assertEqual(scene.metadata.template_id, template_id)

    def test_classifier_uses_high_confidence_concepts(self) -> None:
        cases = {
            "plot velocity against time": "motion-graphs",
            "make a linear graph": "motion-graphs",
            "make a linear motion graph": "motion-graphs",
            "a mass on a spring oscillates": "spring-shm",
            "two carts collide and exchange momentum": "collision-momentum",
            "launch a projectile at 45 degrees": "projectile-motion",
            "free body force vector diagram": "force-diagram",
            "a person walks away from a lamp post and casts a shadow": "lamp-shadow",
        }
        for prompt, expected in cases.items():
            self.assertEqual(classify_template(prompt), expected)
        self.assertIsNone(classify_template("explain quantum tunnelling"))

    def test_linear_graph_uses_constant_velocity_defaults(self) -> None:
        scene = compile_template("motion-graphs", "make a linear graph", {})
        self.assertEqual(scene.metadata.template_id, "motion-graphs")
        self.assertEqual(scene.physics.forces[0].vector[0], 0)
        self.assertEqual(scene.visual.title, "Linear motion graphs")
        acceleration = next(parameter for parameter in scene.parameters if parameter.id == "acceleration")
        self.assertEqual(acceleration.default, 0)

    def test_lamp_shadow_template_preserves_question_units_and_answer(self) -> None:
        scene = compile_template(
            "lamp-shadow",
            "A 1.6 m person walks away from a 4 m lamp at 60 cm/s",
            {},
        )
        self.assertEqual(scene.visual.units, "centimetres")
        self.assertEqual(scene.physics.objects[2].velocity, (60, 0))
        self.assertIn("40 cm/s", scene.visual.overlays[-1].label)

    def test_safe_repair_adds_styles_normalizes_colors_and_clamps(self) -> None:
        repaired = repair_scene_data(
            {
                "physics": {
                    "duration": 999,
                    "dt": 1,
                    "objects": [{"id": "floor", "shape": "segment", "pointA": [-1, 0], "pointB": [1, 0]}],
                },
                "visual": {"background_color": "white", "show_trails": True},
            }
        )
        self.assertEqual(repaired["physics"]["duration"], 30)
        self.assertTrue(repaired["physics"]["objects"][0]["isStatic"])
        self.assertEqual(repaired["visual"]["backgroundColor"], "#FFFFFF")
        self.assertIn("floor", repaired["visual"]["objectStyles"])
        self.assertTrue(repaired["visual"]["trail"]["enabled"])

    def test_safe_repair_normalizes_common_general_planner_aliases(self) -> None:
        repaired = repair_scene_data(
            {
                "physics": {
                    "duration": 2.5,
                    "gravity": [0, 0],
                    "objects": [
                        {"id": "earth", "shape": "circle", "radius": 100, "mass": 10_000},
                        {"id": "sat", "shape": "circle", "radius": 5, "position": [150, 0]},
                    ],
                    "forceFields": [
                        {
                            "type": "inverseSquare",
                            "sourceId": "earth",
                            "sourceMass": 10_000,
                            "strength": 1,
                        }
                    ],
                },
                "visual": {
                    "objectStyles": {"earth": {"color": "green"}},
                    "overlays": [
                        {"type": "label", "targetId": "earth", "text": "Earth"},
                        {
                            "type": "vector",
                            "sourceId": "sat",
                            "vectorType": "velocity",
                            "scale": 5,
                            "position": "topRight",
                            "label": "v",
                        },
                        {
                            "id": "legacyVelocity",
                            "kind": "vector",
                            "targetId": "sat",
                            "data": {"property": "velocity"},
                        },
                        {
                            "id": "satelliteName",
                            "kind": "equation",
                            "targetId": "sat",
                            "label": "Satellite",
                            "data": {"offset": [0, 12]},
                        },
                        {
                            "id": "satelliteTrail",
                            "type": "trail",
                            "targetId": "sat",
                            "maxLength": 300,
                        },
                    ],
                    "trails": {"sat": {"maxLength": 200}},
                },
            }
        )
        scene = SceneSpec.model_validate(repaired)
        field = scene.physics.force_fields[0]
        self.assertEqual(field.id, "forceField1")
        self.assertEqual(field.sources, ["earth"])
        self.assertEqual(field.targets, ["sat"])
        self.assertEqual(scene.visual.object_styles["earth"].label, "Earth")
        self.assertTrue(scene.visual.object_styles["earth"].show_label)
        self.assertTrue(scene.visual.trail.enabled)
        self.assertEqual(scene.visual.trail.max_points, 300)
        self.assertEqual(scene.visual.object_styles["sat"].label, "Satellite")
        self.assertTrue(scene.visual.object_styles["sat"].show_label)
        self.assertEqual(scene.visual.overlays[0].kind, "vector")
        self.assertEqual(scene.visual.overlays[0].target_id, "sat")
        self.assertEqual(scene.visual.overlays[0].data["source"], "velocity")
        self.assertEqual(scene.visual.overlays[0].data["position"], "topRight")
        self.assertEqual(scene.visual.overlays[1].data["source"], "velocity")

    def test_safe_repair_rescales_oversized_mass_system_without_changing_motion(self) -> None:
        repaired = repair_scene_data(
            {
                "physics": {
                    "duration": 2,
                    "gravity": [0, 0],
                    "objects": [
                        {"id": "source", "shape": "circle", "radius": 10, "mass": 1_000_000},
                        {"id": "target", "shape": "circle", "radius": 2, "mass": 10},
                    ],
                    "forces": [{"appliesTo": ["target"], "vector": [100, 0]}],
                    "forceFields": [
                        {
                            "id": "orbit",
                            "type": "inverseSquare",
                            "sources": ["source"],
                            "targets": ["target"],
                            "strength": 2,
                        }
                    ],
                }
            }
        )
        scene = SceneSpec.model_validate(repaired)
        self.assertEqual(scene.physics.objects[0].mass, 100_000)
        self.assertEqual(scene.physics.objects[1].mass, 1)
        self.assertEqual(scene.physics.forces[0].vector, (10, 0))
        self.assertEqual(scene.physics.force_fields[0].strength, 20)

    def test_model_compiler_uses_exact_schema_and_one_repair_call(self) -> None:
        valid = compile_template("falling-body", "fallback", {}).contract_dump()
        valid["metadata"] = {"origin": "model"}
        provider = FakeProvider(["not json", json.dumps(valid)])
        scene = SceneCompiler(provider).compile(
            CompileRequest(prompt="a niche scenario", prefer_template=False),
            cancel_event=Event(),
        )
        self.assertEqual(provider.calls, 2)
        self.assertIn("properties", provider.last_schema)
        self.assertEqual(scene.metadata.origin, "model")

    def test_general_model_planner_is_default_and_knows_composable_force_fields(self) -> None:
        valid = compile_template("projectile-motion", "fallback", {}).contract_dump()
        valid["metadata"] = {"origin": "model"}
        provider = FakeProvider([json.dumps(valid)])
        scene = SceneCompiler(provider).compile(CompileRequest(prompt="projectile motion"), cancel_event=Event())
        self.assertEqual(provider.calls, 1)
        self.assertEqual(scene.metadata.origin, "model")
        self.assertIn("forceFields", provider.last_schema["$defs"]["PhysicsSpec"]["properties"])
        self.assertIn("inverseSquare", SYSTEM_PROMPT)

    def test_non_template_model_scene_preserves_ball_color_angle_and_friction(self) -> None:
        example = Path(__file__).parents[1] / "examples" / "red_ball_ramp.scene.json"
        provider = FakeProvider([example.read_text(encoding="utf-8")])
        scene = SceneCompiler(provider).compile(
            CompileRequest(prompt="A red ball drops onto a 20 degree inclined ramp with friction"),
            cancel_event=Event(),
        )
        ramp = next(obj for obj in scene.physics.objects if obj.id == "incline")
        ball = next(obj for obj in scene.physics.objects if obj.id == "ball")
        angle = math.degrees(
            math.atan2(ramp.point_b[1] - ramp.point_a[1], ramp.point_b[0] - ramp.point_a[0])
        )
        self.assertEqual(scene.metadata.origin, "model")
        self.assertAlmostEqual(angle, 20, places=6)
        self.assertEqual(scene.visual.object_styles["ball"].color, "#D92D20")
        self.assertEqual(scene.visual.object_styles["ball"].render_as, "ball")
        self.assertGreater(ball.friction, 0)
        self.assertGreater(ramp.friction, 0)

    def test_ollama_detects_legacy_json_format_capability(self) -> None:
        provider = OllamaProvider()
        formats = []

        def fake_generate(system, messages, output_format, **kwargs):
            formats.append(output_format)
            if len(formats) == 1:
                raise MotionForgeError(ErrorCode.MODEL_UNAVAILABLE, "unsupported", details="400 Client Error")
            return "{}"

        provider._generate = fake_generate
        result = provider.generate_structured("system", [], {"type": "object"}, request_id="request", cancel_event=Event())
        self.assertEqual(result, "{}")
        self.assertIsInstance(formats[0], dict)
        self.assertEqual(formats[1], "json")
        self.assertFalse(provider._schema_format_supported)


if __name__ == "__main__":
    unittest.main()
