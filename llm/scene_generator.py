"""
prompt (str) -> Storyboard (list[SceneSpec])

Single LLM call returns one JSON object per scene with two top-level keys,
"physics" and "visual", so the two stay consistent (visual references real
object ids, camera framing matches the actual scene bounds, etc).

Phase 1 always returns a storyboard of length 1 -- the storyboard concept
exists in the schema/prompt already so multi-scene support is a later
extension, not a rewrite.
"""

from __future__ import annotations

import json
import os

from pydantic import ValidationError

from llm.providers.base import LLMProvider
from schema.scene_spec import SceneSpec, Storyboard

MAX_RETRIES = 3

SYSTEM_PROMPT = """You are a physics scene compiler. You convert a natural language \
description of a physical scenario into a strict JSON object describing a 2D physics \
scene, using ONLY primitive shapes.

Respond with ONLY a JSON object. No prose, no markdown fences, no explanation.

The JSON object has exactly two top-level keys: "physics" and "visual".

"physics" schema:
{
  "duration": float (seconds, how long the scene should run),
  "dt": float (optional, default 0.01666 = 1/60s),
  "gravity": [x, y] (optional, default [0, -981], pixel-scale units, negative y = down),
  "objects": [
    {
      "id": string (unique),
      "shape": "circle" | "box" | "polygon" | "segment",
      // circle: "radius": float
      // box: "width": float, "height": float
      // polygon: "vertices": [[x,y], ...] local coordinates around origin
      // segment (for floors/walls/ramps): "point_a": [x,y], "point_b": [x,y], "segment_radius": float
      "position": [x, y],
      "angle": float (radians, optional, default 0),
      "velocity": [x, y] (optional, default [0,0]),
      "angular_velocity": float (optional, default 0),
      "mass": float (optional, default 1.0, ignored if is_static),
      "is_static": bool (optional, default false -- true for floors/ramps/walls),
      "friction": float 0-1 (optional, default 0.5),
      "restitution": float 0-1 (optional, default 0.5, bounciness)
    }
  ],
  "forces": [
    {"applies_to": ["object_id", ...], "vector": [x, y]}
  ] (optional, default [])
}

"visual" schema:
{
  "object_styles": {
    "object_id": {"color": "#RRGGBB", "label": string or null, "show_label": bool}
  },
  "background_color": "#RRGGBB" (optional, default white),
  "title": string or null (optional scene title, shown at top),
  "camera": {"zoom": float (default 1.0), "center": [x, y] (default [0,0])},
  "show_trails": bool (optional, default false)
}

Rules:
- Use a coordinate system where +y is up, in pixel-like units (a scene is
  roughly 800 wide x 500 tall, objects a few tens of units in size, floor
  near y=0).
- ALWAYS include a static floor/ground segment unless the prompt clearly
  describes objects in free space (e.g. orbits).
- Every object referenced in visual.object_styles MUST exist in physics.objects.
- Prefer simple, physically plausible values. Do not invent extra top-level keys.
- Approximate complex real-world objects (cars, ramps, seesaws) using the
  available primitives -- e.g. a ramp is a static angled box or segment.
"""

FEW_SHOT_EXAMPLE = {
    "role": "user",
    "content": "A ball drops from height and bounces on the floor a couple of times.",
}

FEW_SHOT_RESPONSE = {
    "role": "assistant",
    "content": json.dumps(
        {
            "physics": {
                "duration": 3.0,
                "dt": 0.01667,
                "gravity": [0, -981],
                "objects": [
                    {
                        "id": "floor",
                        "shape": "segment",
                        "point_a": [-400, 0],
                        "point_b": [400, 0],
                        "segment_radius": 4,
                        "is_static": True,
                        "friction": 0.6,
                        "restitution": 0.7,
                    },
                    {
                        "id": "ball",
                        "shape": "circle",
                        "radius": 20,
                        "position": [0, 300],
                        "mass": 1.0,
                        "friction": 0.4,
                        "restitution": 0.75,
                    },
                ],
                "forces": [],
            },
            "visual": {
                "object_styles": {
                    "floor": {"color": "#5F5E5A", "show_label": False},
                    "ball": {"color": "#D85A30", "label": "ball", "show_label": True},
                },
                "background_color": "#FFFFFF",
                "title": "Bouncing ball",
                "camera": {"zoom": 1.0, "center": [0, 150]},
                "show_trails": True,
            },
        }
    ),
}


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def generate_scene(prompt: str,  provider: LLMProvider) -> Storyboard:
    """Call the LLM, validate the result against SceneSpec, retry on failure.

    Returns a Storyboard (list[SceneSpec]) of length 1 for Phase 1.
    """

    messages = [
        FEW_SHOT_EXAMPLE,
        FEW_SHOT_RESPONSE,
        {"role": "user", "content": prompt},
    ]

    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES):
        raw_text = provider.generate(SYSTEM_PROMPT, messages)
        try:
            data = _extract_json(raw_text)
            scene = SceneSpec.model_validate(data)
            return [scene]
        except (json.JSONDecodeError, ValidationError) as e:
            last_error = e
            # Feed the error back to the model and ask it to fix its output
            messages.append({"role": "assistant", "content": raw_text})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"That JSON was invalid: {e}\n"
                        "Return ONLY a corrected JSON object, no prose."
                    ),
                }
            )

    raise RuntimeError(
        f"Failed to get a valid scene spec after {MAX_RETRIES} attempts. "
        f"Last error: {last_error}"
    )
