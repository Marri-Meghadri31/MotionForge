"""
(list[FrameState], VisualSpec) -> list[Keyframe]

This is the merge point: physics knows nothing about color or labels,
visual knows nothing about position or velocity. A Keyframe is the only
thing the renderer needs to look at -- it never has to reach back into
PhysicsSpec or ask "what color is this object".

Keeping this as its own module (rather than inlining the merge into the
renderer) means the renderer stays engine-agnostic, and physics stays
presentation-agnostic. Swapping Manim for another renderer later only
means writing a new consumer of Keyframe, nothing upstream changes.
"""

from __future__ import annotations

from typing import TypedDict

from engines.engine_2d import FrameState
from schema.scene_spec import PhysicsObject, VisualSpec


class RenderObjectState(TypedDict):
    x: float
    y: float
    angle: float
    color: str
    label: str | None
    show_label: bool
    shape: str
    radius: float | None
    width: float | None
    height: float | None
    vertices: list[tuple[float, float]] | None
    point_a: tuple[float, float] | None
    point_b: tuple[float, float] | None
    is_static: bool


class Keyframe(TypedDict):
    t: float
    dt: float
    objects: dict[str, RenderObjectState]
    background_color: str
    title: str | None
    camera_zoom: float
    camera_center: tuple[float, float]
    show_trails: bool


def build_timeline(
    frames: list[FrameState],
    physics_objects: list[PhysicsObject],
    visual: VisualSpec,
) -> list[Keyframe]:
    keyframes: list[Keyframe] = []
    shape_lookup = {obj.id: obj for obj in physics_objects}

    for frame in frames:
        objects: dict[str, RenderObjectState] = {}
        for obj_id, state in frame["objects"].items():
            style = visual.object_styles.get(obj_id)
            shape_obj = shape_lookup[obj_id]
            objects[obj_id] = {
                "x": state["x"],
                "y": state["y"],
                "angle": state["angle"],
                "color": style.color if style else "#378ADD",
                "label": style.label if style else None,
                "show_label": style.show_label if style else False,
                "shape": shape_obj.shape,
                "radius": shape_obj.radius,
                "width": shape_obj.width,
                "height": shape_obj.height,
                "vertices": shape_obj.vertices,
                "point_a": shape_obj.point_a,
                "point_b": shape_obj.point_b,
                "is_static": shape_obj.is_static or shape_obj.shape == "segment",
            }

        keyframes.append(
            {
                "t": frame["t"],
                "dt": frame["dt"],
                "objects": objects,
                "background_color": visual.background_color,
                "title": visual.title,
                "camera_zoom": visual.camera.zoom,
                "camera_center": visual.camera.center,
                "show_trails": visual.show_trails,
            }
        )

    return keyframes
