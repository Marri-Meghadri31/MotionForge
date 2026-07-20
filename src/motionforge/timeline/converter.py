from __future__ import annotations

import bisect
import hashlib
import json
import math
from typing import Any

from motionforge.models import (
    ObjectStyle,
    ObjectTrack,
    OverlaySpec,
    OverlayTrack,
    SceneSpec,
    SimulationOptions,
    Timeline,
    TimelineObject,
    TimelineScene,
    TrailSpec,
)
from motionforge.physics.simulator import SimulationResult


def scene_hash(scene: SceneSpec) -> str:
    payload = scene.contract_dump(exclude={"metadata"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_timeline(scene: SceneSpec, result: SimulationResult, options: SimulationOptions | None = None) -> Timeline:
    settings = options or SimulationOptions()
    static_scene_objects: dict[str, TimelineObject] = {}
    tracks: dict[str, ObjectTrack] = {}
    for definition in scene.physics.objects:
        style = scene.visual.object_styles.get(definition.id, ObjectStyle())
        static_scene_objects[definition.id] = TimelineObject(
            shape=definition.shape,
            is_static=definition.is_static,
            mass=definition.mass,
            radius=definition.radius,
            width=definition.width,
            height=definition.height,
            vertices=definition.vertices,
            point_a=definition.point_a,
            point_b=definition.point_b,
            segment_radius=definition.segment_radius,
            color=style.color,
            label=style.label,
            show_label=style.show_label,
            opacity=style.opacity,
            stroke_width=style.stroke_width,
        )
        selected_frames = result.frames[:1] if definition.is_static else result.frames
        states = [frame["objects"][definition.id] for frame in selected_frames]
        inspect = settings.record_inspectables and definition.inspectable
        tracks[definition.id] = ObjectTrack(
            times=[frame["t"] for frame in selected_frames],
            x=[state["x"] for state in states],
            y=[state["y"] for state in states],
            angle=[state["angle"] for state in states],
            vx=[state["vx"] for state in states] if inspect else [],
            vy=[state["vy"] for state in states] if inspect else [],
            angular_velocity=[state["angular_velocity"] for state in states] if inspect else [],
            ax=[state["ax"] for state in states] if inspect else [],
            ay=[state["ay"] for state in states] if inspect else [],
            force_x=[state["force_x"] for state in states] if inspect else [],
            force_y=[state["force_y"] for state in states] if inspect else [],
            kinetic_energy=[state["kinetic_energy"] for state in states] if inspect else [],
            potential_energy=[state["potential_energy"] for state in states] if inspect else [],
            momentum_x=[state["momentum_x"] for state in states] if inspect else [],
            momentum_y=[state["momentum_y"] for state in states] if inspect else [],
        )
    overlay_tracks = {
        overlay.id: OverlayTrack(overlay=overlay)
        for overlay in scene.visual.overlays
    }
    return Timeline(
        duration=scene.physics.duration,
        simulation_fps=1 / scene.physics.dt,
        recommended_playback_fps=settings.recommended_playback_fps,
        scene=TimelineScene(
            size=scene.visual.scene_size,
            units=scene.visual.units,
            coordinate_system=scene.visual.coordinate_system,
            background=scene.visual.background_color,
            title=scene.visual.title,
            gravity=scene.physics.gravity,
            objects=static_scene_objects,
            constraints=scene.physics.constraints,
            camera=scene.visual.camera,
            trail=scene.visual.trail,
        ),
        tracks=tracks,
        overlay_tracks=overlay_tracks,
        events=result.events,
        parameters=scene.parameters,
        source_scene_hash=scene_hash(scene),
    )


def _interpolate(values: list[float], left: int, right: int, fraction: float, *, angle: bool = False) -> float:
    start, end = values[left], values[right]
    delta = (end - start + math.pi) % (2 * math.pi) - math.pi if angle else end - start
    return start + delta * fraction


def sample_track(track: ObjectTrack, timestamp: float) -> dict[str, float]:
    if len(track.times) == 1 or timestamp <= track.times[0]:
        left = right = 0
        fraction = 0.0
    elif timestamp >= track.times[-1]:
        left = right = len(track.times) - 1
        fraction = 0.0
    else:
        right = bisect.bisect_right(track.times, timestamp)
        left = right - 1
        fraction = (timestamp - track.times[left]) / (track.times[right] - track.times[left])
    result = {
        "x": _interpolate(track.x, left, right, fraction),
        "y": _interpolate(track.y, left, right, fraction),
        "angle": _interpolate(track.angle, left, right, fraction, angle=True),
    }
    for name in (
        "vx",
        "vy",
        "angular_velocity",
        "ax",
        "ay",
        "force_x",
        "force_y",
        "kinetic_energy",
        "potential_energy",
        "momentum_x",
        "momentum_y",
    ):
        values = getattr(track, name)
        if values:
            result[name] = _interpolate(values, left, right, fraction)
    return result


def sample_timeline(timeline: Timeline, timestamp: float) -> dict[str, dict[str, float]]:
    bounded = min(timeline.duration, max(0.0, timestamp))
    return {obj_id: sample_track(track, bounded) for obj_id, track in timeline.tracks.items()}


def from_legacy_keyframes(keyframes: list[dict[str, Any]]) -> Timeline:
    """Read the original repeated-keyframe format during Velo migration."""

    if not keyframes:
        raise ValueError("legacy timeline cannot be empty")
    first = keyframes[0]
    scene_objects: dict[str, TimelineObject] = {}
    tracks: dict[str, ObjectTrack] = {}
    duration = max(float(frame.get("t", 0)) for frame in keyframes)
    if duration <= 0:
        duration = sum(float(frame.get("dt", 0)) for frame in keyframes)
    for obj_id, legacy in first["objects"].items():
        scene_objects[obj_id] = TimelineObject(
            shape=legacy["shape"],
            is_static=bool(legacy.get("is_static", False)),
            radius=legacy.get("radius"),
            width=legacy.get("width"),
            height=legacy.get("height"),
            vertices=legacy.get("vertices"),
            point_a=legacy.get("point_a"),
            point_b=legacy.get("point_b"),
            color=legacy.get("color", "#378ADD"),
            label=legacy.get("label"),
            show_label=legacy.get("show_label", False),
        )
        source = keyframes[:1] if legacy.get("is_static") else keyframes
        tracks[obj_id] = ObjectTrack(
            times=[float(frame.get("t", index * frame.get("dt", 0))) for index, frame in enumerate(source)],
            x=[float(frame["objects"][obj_id]["x"]) for frame in source],
            y=[float(frame["objects"][obj_id]["y"]) for frame in source],
            angle=[float(frame["objects"][obj_id].get("angle", 0)) for frame in source],
        )
    return Timeline(
        duration=duration,
        simulation_fps=round(1 / float(first.get("dt", 1 / 60)), 6),
        scene=TimelineScene(
            background=first.get("background_color", "#FFFFFF"),
            title=first.get("title"),
            objects=scene_objects,
            camera={"zoom": first.get("camera_zoom", 1), "center": first.get("camera_center", [0, 0])},
            trail=TrailSpec(enabled=first.get("show_trails", False)),
        ),
        tracks=tracks,
        source_scene_hash="legacy",
    )
