from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from threading import Event
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from motionforge.compiler.templates import classify_template, compile_template
from motionforge.constants import (
    MAX_COORDINATE,
    MAX_DURATION_SECONDS,
    MAX_FORCE_MAGNITUDE,
    MAX_MASS,
    MAX_TIMESTEP_SECONDS,
    MIN_TIMESTEP_SECONDS,
    SCHEMA_VERSION,
)
from motionforge.errors import ErrorCode, MotionForgeError, validation_diagnostics
from motionforge.models import CompileRequest, CompilerMetadata, SceneSpec
from motionforge.providers import Provider, build_provider

MAX_MODEL_ATTEMPTS = 2

SYSTEM_PROMPT = """You are MotionForge's general physics scene planner. Convert the learner's request into
one compact, physically meaningful SceneSpec JSON object with no prose. Compose the scene from the
schema's reusable bodies, contacts, constraints, force fields, styles, and overlays. Do not choose a
predefined scenario or erase details merely because a familiar textbook category appears.

Planning rules:
- Preserve every material detail that affects the requested visual: object kind, color, count, angle,
  friction, restitution, mass, dimensions, initial position/velocity, and named reference frame.
- Physics objects use `id`, `shape`, and numeric `[x, y]` position/velocity arrays. A ball is a `circle`;
  an inclined plane is a static `segment`; their friction values belong on both colliding objects.
- Coordinates are Cartesian with +y upward. Convert stated angles to segment endpoints and body angles.
- Use `gravity` for a uniform field and `forces` for constant applied forces. Use an `inverseSquare`
  `forceFields` entry for orbital/electrostatic-style radial interactions. Its acceleration is generated
  from strength × sourceMass / softenedDistance²; use a static massive source for a fixed focus or
  `mutual:true` for interacting dynamic bodies. Choose stable dt, softening, and bounded initial states.
- Constraints are `pin` or `dampedSpring`. Do not fake a force law with a constraint when a force field
  expresses the requested physics.
- Object appearance belongs in `visual.objectStyles`. Use `renderAs:"ball"` for a visibly rotating ball.
- Use trails for trajectories. Use vector overlays sourced from velocity, acceleration, force, gravity,
  momentum, friction, normal, or constraint. Use declarative line/measurement point references for
  object anchors, intersections, and dimensions. Equations are plain Unicode text, not LaTeX.
- A graph overlay targets one real simulated object. Every style, overlay, force, field, and constraint
  reference must name an existing object; overlay dependency references must point backward.
- Keep the scene legible in an approximately 800 × 500 logical viewport and normally 2–12 seconds.
  Select simulation units consistently and expose meaningful adjustable parameters.
- Never emit code, URLs, asset paths, unsupported keys, or enum values absent from the supplied schema.

Minimal valid scene:
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


def _has_valid_segment_endpoints(body: dict[str, Any]) -> bool:
    """Return whether a planner supplied two usable, distinct segment endpoints."""

    def valid_point(value: Any) -> bool:
        return (
            isinstance(value, (list, tuple))
            and len(value) == 2
            and all(
                isinstance(component, (int, float))
                and not isinstance(component, bool)
                and math.isfinite(float(component))
                and abs(float(component)) <= MAX_COORDINATE
                for component in value
            )
        )

    point_a = body.get("pointA", body.get("point_a"))
    point_b = body.get("pointB", body.get("point_b"))
    return valid_point(point_a) and valid_point(point_b) and tuple(point_a) != tuple(point_b)


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
                    # Some planners describe a segment with endpoints and also
                    # repeat its derived length. PhysicsObject accepts the
                    # endpoints; once both are valid, dropping the redundant
                    # value is deterministic and preserves the geometry.
                    if _has_valid_segment_endpoints(body):
                        body.pop("length", None)
        object_ids = [
            body["id"]
            for body in objects
            if isinstance(body, dict) and isinstance(body.get("id"), str)
        ] if isinstance(objects, list) else []
        static_ids = {
            body["id"]
            for body in objects
            if isinstance(body, dict) and isinstance(body.get("id"), str) and body.get("isStatic", False)
        } if isinstance(objects, list) else set()
        fields = physics.setdefault("forceFields", physics.pop("force_fields", []))
        if isinstance(fields, list):
            for index, field in enumerate(fields):
                if not isinstance(field, dict):
                    continue
                field.setdefault("id", f"forceField{index + 1}")
                source_id = field.pop("sourceId", field.pop("source", None))
                if "sources" not in field and isinstance(source_id, str):
                    field["sources"] = [source_id]
                target_id = field.pop("targetId", field.pop("target", None))
                if "targets" not in field:
                    if isinstance(target_id, str):
                        field["targets"] = [target_id]
                    else:
                        sources = set(field.get("sources", []))
                        dynamic_targets = [item for item in object_ids if item not in sources and item not in static_ids]
                        if dynamic_targets:
                            field["targets"] = dynamic_targets
                # Source mass belongs to the referenced PhysicsObject. Some
                # planners repeat it on the field even when the body already
                # carries the same value; removing the duplicate preserves the
                # force law used by the simulator.
                field.pop("sourceMass", None)
        masses = [
            float(body["mass"])
            for body in objects
            if isinstance(body, dict)
            and isinstance(body.get("mass"), (int, float))
            and float(body["mass"]) > 0
        ] if isinstance(objects, list) else []
        if masses and max(masses) > MAX_MASS:
            mass_scale = MAX_MASS / max(masses)
            for body in objects:
                if isinstance(body, dict) and isinstance(body.get("mass"), (int, float)):
                    body["mass"] = float(body["mass"]) * mass_scale
            forces = physics.get("forces", [])
            if isinstance(forces, list):
                for force in forces:
                    if not isinstance(force, dict) or not isinstance(force.get("vector"), (list, tuple)):
                        continue
                    force["vector"] = [float(component) * mass_scale for component in force["vector"]]
            if isinstance(fields, list):
                for field in fields:
                    if not isinstance(field, dict) or not isinstance(field.get("strength"), (int, float)):
                        continue
                    field["strength"] = min(MAX_FORCE_MAGNITUDE, float(field["strength"]) / mass_scale)
                    if isinstance(field.get("maxForce"), (int, float)):
                        field["maxForce"] = max(1e-12, float(field["maxForce"]) * mass_scale)
            constraints = physics.get("constraints", [])
            if isinstance(constraints, list):
                for constraint in constraints:
                    if not isinstance(constraint, dict):
                        continue
                    for key in ("stiffness", "damping"):
                        if isinstance(constraint.get(key), (int, float)):
                            constraint[key] = float(constraint[key]) * mass_scale

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
        legacy_trail_map = visual.pop("trails", None)
        if "trail" not in visual and isinstance(legacy_trail_map, dict) and legacy_trail_map:
            maximums = [
                value.get("maxLength")
                for value in legacy_trail_map.values()
                if isinstance(value, dict) and isinstance(value.get("maxLength"), (int, float))
            ]
            visual["trail"] = {
                "enabled": True,
                **({"maxPoints": int(max(maximums))} if maximums else {}),
            }
        overlays = visual.get("overlays", [])
        repaired_overlays: list[dict[str, Any]] = []
        if isinstance(overlays, list):
            used_overlay_ids: set[str] = set()
            for index, overlay in enumerate(overlays):
                if not isinstance(overlay, dict):
                    continue
                legacy_type = overlay.pop("type", None)
                if "kind" not in overlay and isinstance(legacy_type, str):
                    overlay["kind"] = legacy_type
                target_id = overlay.pop("target", overlay.pop("sourceId", None))
                if "targetId" not in overlay and isinstance(target_id, str):
                    overlay["targetId"] = target_id
                if overlay.get("kind") == "label" and isinstance(overlay.get("targetId"), str):
                    target_style = styles.setdefault(overlay["targetId"], {})
                    if isinstance(target_style, dict):
                        label = overlay.pop("text", overlay.get("label"))
                        if isinstance(label, str):
                            target_style["label"] = label
                            target_style["showLabel"] = True
                    continue
                overlay_id = overlay.get("id")
                if not isinstance(overlay_id, str) or not overlay_id or overlay_id in used_overlay_ids:
                    overlay_id = f"overlay{index + 1}"
                    overlay["id"] = overlay_id
                used_overlay_ids.add(overlay_id)
                if "label" not in overlay and isinstance(overlay.get("text"), str):
                    overlay["label"] = overlay.pop("text")
                data_payload = overlay.get("data")
                overlay_data = dict(data_payload) if isinstance(data_payload, dict) else {}
                if str(overlay.get("kind", "")).casefold() in {"trail", "trajectory"}:
                    overlay["kind"] = "path"
                    overlay_data.setdefault("style", "trail")
                source = overlay.pop("vectorType", overlay.pop("subtype", None))
                if source is None and overlay.get("kind") == "vector":
                    source = overlay_data.pop("property", None)
                if source is not None and "source" not in overlay_data:
                    overlay_data["source"] = source
                for key in ("scale", "offset", "fontSize", "arrowHead", "maxScreenLength", "hideWhenZero"):
                    if key in overlay:
                        overlay_data[key] = overlay.pop(key)
                canonical_overlay_keys = {
                    "id",
                    "kind",
                    "targetId",
                    "label",
                    "color",
                    "visible",
                    "start",
                    "end",
                    "operation",
                    "data",
                }
                for key in list(overlay):
                    if key not in canonical_overlay_keys:
                        overlay_data[key] = overlay.pop(key)
                overlay["data"] = overlay_data
                target_id = overlay.get("targetId")
                label = overlay.get("label")
                normalized_target = (
                    re.sub(r"[^a-z0-9]", "", target_id.casefold()) if isinstance(target_id, str) else ""
                )
                normalized_label = re.sub(r"[^a-z0-9]", "", label.casefold()) if isinstance(label, str) else ""
                if (
                    overlay.get("kind") == "equation"
                    and len(normalized_target) >= 3
                    and (
                        normalized_label == normalized_target
                        or normalized_label.startswith(normalized_target)
                        or normalized_target.startswith(normalized_label)
                    )
                ):
                    target_style = styles.setdefault(target_id, {})
                    if isinstance(target_style, dict):
                        target_style["label"] = label
                        target_style["showLabel"] = True
                    continue
                if overlay.get("kind") == "path" and str(overlay_data.get("style", "")).casefold() in {
                    "trail",
                    "trajectory",
                }:
                    trail = visual.setdefault("trail", {})
                    if isinstance(trail, dict):
                        trail["enabled"] = True
                        if isinstance(overlay_data.get("maxLength"), (int, float)):
                            trail["maxPoints"] = int(overlay_data["maxLength"])
                    continue
                repaired_overlays.append(overlay)
            visual["overlays"] = repaired_overlays
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
