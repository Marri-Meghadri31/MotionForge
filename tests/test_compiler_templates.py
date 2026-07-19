from __future__ import annotations

import json
import unittest
from threading import Event

from motionforge.compiler.scene_compiler import SceneCompiler, repair_scene_data
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
        self.assertEqual(len(TEMPLATES), 9)
        for template_id in TEMPLATES:
            with self.subTest(template=template_id):
                scene = compile_template(template_id, template_id, {})
                self.assertEqual(scene.metadata.origin, "template")
                self.assertEqual(scene.metadata.template_id, template_id)

    def test_classifier_uses_high_confidence_concepts(self) -> None:
        cases = {
            "plot velocity against time": "motion-graphs",
            "a mass on a spring oscillates": "spring-shm",
            "two carts collide and exchange momentum": "collision-momentum",
            "launch a projectile at 45 degrees": "projectile-motion",
            "free body force vector diagram": "force-diagram",
        }
        for prompt, expected in cases.items():
            self.assertEqual(classify_template(prompt), expected)
        self.assertIsNone(classify_template("explain quantum tunnelling"))

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
