from __future__ import annotations

import copy
import hashlib
import json
import re
from threading import Event
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from motionforge.compiler.templates import classify_template, compile_template
from motionforge.constants import MAX_DURATION_SECONDS, MAX_TIMESTEP_SECONDS, MIN_TIMESTEP_SECONDS, SCHEMA_VERSION
from motionforge.errors import ErrorCode, MotionForgeError, validation_diagnostics
from motionforge.models import CompileRequest, CompilerMetadata, SceneSpec
from motionforge.providers import Provider, build_provider

MAX_MODEL_ATTEMPTS = 2

SYSTEM_PROMPT = """You are MotionForge's physics scene compiler. Turn the learner's physics question into
one short, physically meaningful visualization and return one JSON object with no prose.
Use only the exact fields and enum values in the supplied JSON Schema. In particular:
- Physics objects use `id`, `shape`, and numeric `[x, y]` arrays for position and velocity.
- Object color and labels belong in `visual.objectStyles`, never in physics objects.
- Constraints are only `pin` or `dampedSpring` and use `objectA` and `objectB` IDs.
- A requested graph must be a `graph` overlay targeting a simulated object, not a chain of bodies.
- Prefer a clear primitive analogy when the concept is abstract; never invent a new object or constraint type.
Coordinates are Cartesian with +y upward, normally within an 800 by 500 scene. Keep scenes bounded,
short, physically plausible, and educational. Never emit code, URLs, LaTeX commands, asset paths,
or keys outside the supplied JSON Schema. Every style, overlay, force, and constraint reference must
name a real object.

A minimal valid shape is:
{"schemaVersion":1,"physics":{"duration":3,"dt":0.0166666667,"gravity":[0,-981],"objects":[{"id":"body","shape":"circle","radius":20,"position":[0,100]}]},"visual":{"objectStyles":{"body":{"color":"#378ADD"}}}}"""

COLOR_NAMES = {
    "white": "#FFFFFF",
    "black": "#000000",
    "blue": "#378ADD",
    "red": "#D85A30",
    "green": "#639922",
    "purple": "#7F77DD",
    "gray": "#888780",
    "grey": "#888780",
}


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"\s*```$", "", stripped)
    data = json.loads(stripped)
    if not isinstance(data, dict):
        raise ValueError("model output must be a JSON object")
    return data


def _normalize_color(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    lowered = value.strip().lower()
    if lowered in COLOR_NAMES:
        return COLOR_NAMES[lowered]
    if re.fullmatch(r"#[0-9a-fA-F]{3}", lowered):
        return "#" + "".join(character * 2 for character in lowered[1:]).upper()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", lowered):
        return lowered.upper()
    return fallback


def repair_scene_data(raw: dict[str, Any]) -> dict[str, Any]:
    """Apply only deterministic, semantics-preserving repairs before validation."""

    data = copy.deepcopy(raw)
    data.setdefault("schemaVersion", data.pop("schema_version", SCHEMA_VERSION))
    physics = data.setdefault("physics", {})
    if isinstance(physics, dict):
        try:
            physics["duration"] = min(MAX_DURATION_SECONDS, max(0.05, float(physics.get("duration", 3))))
        except (TypeError, ValueError):
            pass
        try:
            physics["dt"] = min(MAX_TIMESTEP_SECONDS, max(MIN_TIMESTEP_SECONDS, float(physics.get("dt", 1 / 60))))
        except (TypeError, ValueError):
            pass
        objects = physics.get("objects", [])
        if isinstance(objects, list):
            for body in objects:
                if not isinstance(body, dict):
                    continue
                if body.get("shape") == "segment":
                    body.setdefault("isStatic", body.pop("is_static", True))

    visual = data.setdefault("visual", {})
    if isinstance(visual, dict):
        legacy_trails = visual.pop("show_trails", visual.pop("showTrails", None))
        if "trail" not in visual and legacy_trails is not None:
            visual["trail"] = {"enabled": bool(legacy_trails)}
        visual["backgroundColor"] = _normalize_color(
            visual.get("backgroundColor", visual.pop("background_color", "#FFFFFF")), "#FFFFFF"
        )
        styles = visual.setdefault("objectStyles", visual.pop("object_styles", {}))
        objects = physics.get("objects", []) if isinstance(physics, dict) else []
        if not isinstance(styles, dict):
            styles = {}
            visual["objectStyles"] = styles
        for body in objects if isinstance(objects, list) else []:
            if not isinstance(body, dict) or not isinstance(body.get("id"), str):
                continue
            style = styles.setdefault(body["id"], {})
            if not isinstance(style, dict):
                style = {}
                styles[body["id"]] = style
            style["color"] = _normalize_color(style.get("color"), "#378ADD")
        camera = visual.setdefault("camera", {})
        if isinstance(camera, dict):
            try:
                camera["zoom"] = min(10, max(0.1, float(camera.get("zoom", 1))))
            except (TypeError, ValueError):
                pass
            center = camera.get("center", [0, 0])
            if isinstance(center, (list, tuple)) and len(center) == 2:
                try:
                    camera["center"] = [min(10_000, max(-10_000, float(value))) for value in center]
                except (TypeError, ValueError):
                    pass
    return data


class SceneCompiler:
    def __init__(self, provider: Provider | None = None) -> None:
        self.provider = provider

    def compile(self, request: CompileRequest, *, cancel_event: Event | None = None) -> SceneSpec:
        cancel = cancel_event or Event()
        if cancel.is_set():
            raise MotionForgeError(ErrorCode.CANCELLED, "Compilation was cancelled.")
        if request.scene is not None:
            scene = request.scene.model_copy(update={"metadata": CompilerMetadata(origin="provided")})
            return _apply_privacy(scene, request)

        prompt = request.prompt.strip() if request.prompt else ""
        template_id = request.template or (classify_template(prompt) if request.prefer_template else None)
        if template_id:
            try:
                scene = compile_template(template_id, prompt, request.parameters)
            except (ValueError, ValidationError) as error:
                raise MotionForgeError(
                    ErrorCode.INVALID_SCENE,
                    "Template parameters are invalid.",
                    details=validation_diagnostics(error),
                ) from error
            scene = scene.model_copy(
                update={
                    "metadata": scene.metadata.model_copy(
                        update={"normalized_prompt_hash": _prompt_hash(prompt)}
                    )
                }
            )
            return _apply_privacy(scene, request)

        provider = self.provider or build_provider(request.provider, request.model, timeout=request.timeout_seconds)
        schema = SceneSpec.model_json_schema(by_alias=True)
        messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]
        request_id = uuid4().hex
        last_error: Exception | None = None
        for attempt in range(MAX_MODEL_ATTEMPTS):
            if cancel.is_set():
                provider.cancel(request_id)
                raise MotionForgeError(ErrorCode.CANCELLED, "Compilation was cancelled.")
            raw = provider.generate_structured(
                SYSTEM_PROMPT,
                messages,
                schema,
                request_id=request_id,
                cancel_event=cancel,
            )
            try:
                scene = SceneSpec.model_validate(repair_scene_data(_extract_json(raw)))
                metadata = CompilerMetadata(
                    origin="model",
                    provider=provider.name,
                    model=provider.model,
                    normalized_prompt_hash=_prompt_hash(prompt),
                )
                return _apply_privacy(scene.model_copy(update={"metadata": metadata}), request)
            except (json.JSONDecodeError, ValueError, ValidationError) as error:
                last_error = error
                if attempt + 1 >= MAX_MODEL_ATTEMPTS:
                    break
                diagnostics = validation_diagnostics(error)
                messages.extend(
                    [
                        {"role": "assistant", "content": raw},
                        {
                            "role": "user",
                            "content": (
                                "The response does not match MotionForge's schema. Rebuild the complete JSON object from scratch, "
                                "using only the exact schema field names and enum values. Do not preserve unsupported object or "
                                "constraint types. Correct these validation errors: " + json.dumps(diagnostics)
                            ),
                        },
                    ]
                )
        raise MotionForgeError(
            ErrorCode.INVALID_SCENE,
            "The model could not produce a valid physics scene.",
            details=validation_diagnostics(last_error or ValueError("invalid model response")),
        )


def _prompt_hash(prompt: str) -> str:
    normalized = " ".join(prompt.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _apply_privacy(scene: SceneSpec, request: CompileRequest) -> SceneSpec:
    if request.privacy == "redact":
        return scene.model_copy(update={"description": None})
    return scene
