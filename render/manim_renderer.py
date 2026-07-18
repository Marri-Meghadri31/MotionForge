"""
list[Keyframe] -> .mp4

Builds one Manim Mobject per physics object (shape decided purely from
keyframe["shape"], never anything physics-specific), then walks the
keyframes moving/rotating each Mobject to match. Absolute angle is tracked
per object since Manim's `.rotate()` is a relative operation.
"""

from __future__ import annotations

from manim import (
    BLACK,
    Circle,
    Dot,
    Line,
    ManimColor,
    Polygon,
    Rectangle,
    Scene,
    Text,
    VGroup,
    config,
)

from timeline.converter import Keyframe

# Manim's coordinate system is centered at the origin already, which lines
# up with the pixel-ish coordinates the LLM is instructed to use, scaled
# down to Manim units (Manim units are roughly "meters" on a ~14-wide frame).
SCALE = 1 / 60


def _to_manim_point(x: float, y: float, camera_center: tuple[float, float]):
    return (
        (x - camera_center[0]) * SCALE,
        (y - camera_center[1]) * SCALE,
        0,
    )


def _make_mobject(obj_id: str, state: dict, camera_center: tuple[float, float]) -> VGroup:
    color = ManimColor(state["color"])
    shape = state["shape"]

    if shape == "circle":
        mob = Circle(radius=max(state["radius"] * SCALE, 0.05), color=color, fill_opacity=0.9)
    elif shape == "box":
        mob = Rectangle(
            width=state["width"] * SCALE,
            height=state["height"] * SCALE,
            color=color,
            fill_opacity=0.9,
        )
    elif shape == "polygon":
        pts = [(vx * SCALE, vy * SCALE, 0) for vx, vy in state["vertices"]]
        mob = Polygon(*pts, color=color, fill_opacity=0.9)
    elif shape == "segment":
        # Segments are static (floors/ramps/walls) in this schema, so
        # point_a/point_b are treated as absolute world coordinates and the
        # Line is built directly -- no move_to needed afterwards.
        pa = _to_manim_point(*state["point_a"], camera_center)
        pb = _to_manim_point(*state["point_b"], camera_center)
        mob = Line(pa, pb, color=color, stroke_width=6)
        group = VGroup(mob)
        if state.get("show_label") and state.get("label"):
            label = Text(state["label"], font_size=20, color=BLACK)
            label.next_to(mob, direction=[0, 1, 0], buff=0.1)
            group.add(label)
        return group
    else:
        raise ValueError(f"Unsupported shape for rendering: {shape}")

    group = VGroup(mob)
    if state.get("show_label") and state.get("label"):
        label = Text(state["label"], font_size=20, color=BLACK)
        label.next_to(mob, direction=[0, 1, 0], buff=0.1)
        group.add(label)

    group.move_to(_to_manim_point(state["x"], state["y"], camera_center))
    return group


class PhysicsScene(Scene):
    """Instantiate with keyframes + title bound as class attributes before
    calling .render() -- see render_video() below."""

    keyframes: list[Keyframe] = []

    def construct(self):
        if not self.keyframes:
            return

        first = self.keyframes[0]
        camera_center = first["camera_center"]
        self.camera.background_color = ManimColor(first["background_color"])

        mobjects: dict[str, VGroup] = {}
        prev_angle: dict[str, float] = {}

        if first.get("title"):
            title = Text(first["title"], font_size=32, color=BLACK)
            title.to_edge(edge=[0, 1, 0])
            self.add(title)

        trails: dict[str, VGroup] = {}

        for obj_id, state in first["objects"].items():
            mob = _make_mobject(obj_id, state, camera_center)
            mobjects[obj_id] = mob
            prev_angle[obj_id] = state["angle"]
            self.add(mob)
            if first["show_trails"]:
                trails[obj_id] = VGroup()
                self.add(trails[obj_id])

        for frame in self.keyframes[1:]:
            for obj_id, state in frame["objects"].items():
                if state["is_static"]:
                    continue
                mob = mobjects.get(obj_id)
                if mob is None:
                    continue
                target = _to_manim_point(state["x"], state["y"], camera_center)
                mob.move_to(target)

                delta_angle = state["angle"] - prev_angle[obj_id]
                if delta_angle != 0:
                    mob.rotate(delta_angle)
                prev_angle[obj_id] = state["angle"]

                if frame["show_trails"] and obj_id in trails:
                    dot = Dot(point=target, radius=0.02, color=mob[0].color)
                    trails[obj_id].add(dot)

            self.wait(frame["dt"])


def render_video(keyframes: list[Keyframe], output_path: str, quality: str = "low") -> str:
    """quality: 'low' (fast, for iteration) or 'high' (final output)."""
    quality_map = {
        "low": {"pixel_height": 480, "pixel_width": 854, "frame_rate": 30},
        "high": {"pixel_height": 1080, "pixel_width": 1920, "frame_rate": 60},
    }
    settings = quality_map.get(quality, quality_map["low"])

    config.pixel_height = settings["pixel_height"]
    config.pixel_width = settings["pixel_width"]
    config.frame_rate = settings["frame_rate"]
    config.output_file = output_path
    config.disable_caching = True

    PhysicsScene.keyframes = keyframes
    scene = PhysicsScene()
    scene.render()
    return output_path
