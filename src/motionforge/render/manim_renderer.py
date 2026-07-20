from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from threading import Event
from typing import Any

from motionforge.errors import ErrorCode, MotionForgeError
from motionforge.constants import MAX_EXPORT_BYTES
from motionforge.models import ExportOptions, ExportResult, Timeline
from motionforge.timeline.converter import sample_timeline

BASE_SCALE = 1 / 60


def output_frame_times(duration: float, fps: int) -> list[float]:
    """Return one display timestamp per encoded frame.

    A 3 second export contains exactly 72, 90, or 180 frames at 24, 30, or
    60 FPS. The final displayed timestamp is therefore duration - 1/fps.
    """

    count = max(1, round(duration * fps))
    return [min(duration, index / fps) for index in range(count)]


def find_ffmpeg() -> str | None:
    configured = os.environ.get("MOTIONFORGE_FFMPEG")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    executable_root = Path(sys.executable).resolve().parent
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    candidates.extend(
        [
            bundle_root / "resources" / "ffmpeg" / name,
            bundle_root / "resources" / "ffmpeg" / "bin" / name,
            executable_root / "resources" / "ffmpeg" / name,
            executable_root / "resources" / "ffmpeg" / "bin" / name,
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("ffmpeg")


def renderer_health() -> dict[str, Any]:
    import importlib.util

    ffmpeg = find_ffmpeg()
    return {
        "manim": importlib.util.find_spec("manim") is not None,
        "ffmpeg": bool(ffmpeg),
        "ffmpegPath": ffmpeg,
        "codecs": ["h264"] if ffmpeg else [],
        "pixelFormats": ["yuv420p"] if ffmpeg else [],
        "fonts": {"strategy": "bundled-or-system-sans-serif", "latexRequired": False},
    }


def render_video(
    timeline: Timeline,
    output_path: str | Path,
    options: ExportOptions | None = None,
    *,
    cancel_event: Event | None = None,
) -> ExportResult:
    settings = options or ExportOptions()
    width, height, fps = settings.resolved()
    destination = Path(output_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.lower() != ".mp4":
        destination = destination.with_suffix(".mp4")
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise MotionForgeError(ErrorCode.EXPORT_FAILED, "FFmpeg is unavailable; the playable timeline is still available.")
    cancelled = cancel_event or Event()
    started = time.perf_counter()
    try:
        raw_path = _render_manim(timeline, width, height, fps, cancelled)
        if cancelled.is_set():
            raise MotionForgeError(ErrorCode.CANCELLED, "Video export was cancelled.")
        temporary = destination.with_name(f".{destination.stem}.{os.getpid()}.tmp.mp4")
        command = [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(raw_path),
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(temporary),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=settings.timeout_seconds, check=False)
        if completed.returncode != 0:
            raise MotionForgeError(ErrorCode.EXPORT_FAILED, "FFmpeg could not encode the video.", details=completed.stderr[-2_000:])
        os.replace(temporary, destination)
        if destination.stat().st_size > MAX_EXPORT_BYTES:
            destination.unlink(missing_ok=True)
            raise MotionForgeError(ErrorCode.EXPORT_FAILED, "The encoded video exceeded the output-size limit.")
    except subprocess.TimeoutExpired as error:
        raise MotionForgeError(ErrorCode.TIMEOUT, "Video export exceeded its time limit.") from error
    except OSError as error:
        if getattr(error, "winerror", None) == 112 or getattr(error, "errno", None) == 28:
            raise MotionForgeError(ErrorCode.DISK_FULL, "There is not enough disk space to export the video.") from error
        raise MotionForgeError(ErrorCode.EXPORT_FAILED, "Video export failed.", details=str(error)) from error
    finally:
        try:
            if "raw_path" in locals():
                workspace = next(
                    (parent for parent in raw_path.parents if parent.name.startswith("motionforge-export-")),
                    raw_path.parent,
                )
                shutil.rmtree(workspace, ignore_errors=True)
        except OSError:
            pass
    return ExportResult(
        output_path=str(destination),
        duration=timeline.duration,
        width=width,
        height=height,
        fps=fps,
        size_bytes=destination.stat().st_size,
        render_seconds=time.perf_counter() - started,
    )


def _render_manim(timeline: Timeline, width: int, height: int, fps: int, cancelled: Event) -> Path:
    # Importing Manim is deliberately isolated from sidecar startup and preview work.
    from manim import BLACK, WHITE, Circle, Line, ManimColor, Polygon, Rectangle, Scene, Text, UpdateFromAlphaFunc, VGroup, VMobject, linear, tempconfig

    workspace = Path(tempfile.mkdtemp(prefix="motionforge-export-"))

    class TimelineScene(Scene):
        def construct(self) -> None:
            background = ManimColor(timeline.scene.background)
            self.camera.background_color = background
            zoom = timeline.scene.camera.zoom
            centre = timeline.scene.camera.center
            scale = BASE_SCALE * zoom

            def point(x: float, y: float):
                return ((x - centre[0]) * scale, (y - centre[1]) * scale, 0)

            def segment_point(local: tuple[float, float], state: dict[str, float]):
                cosine, sine = math.cos(state["angle"]), math.sin(state["angle"])
                world_x = state["x"] + local[0] * cosine - local[1] * sine
                world_y = state["y"] + local[0] * sine + local[1] * cosine
                return point(world_x, world_y)

            label_color = WHITE if _is_dark(timeline.scene.background) else BLACK
            shapes: dict[str, Any] = {}
            labels: dict[str, Any] = {}
            trails: dict[str, Any] = {}
            trail_points: dict[str, deque] = {}
            initial = sample_timeline(timeline, 0)
            for obj_id, definition in timeline.scene.objects.items():
                state = initial[obj_id]
                color = ManimColor(definition.color)
                if definition.shape == "circle":
                    shape = Circle(radius=max((definition.radius or 1) * scale, 0.025), color=color, fill_color=color, fill_opacity=definition.opacity, stroke_width=definition.stroke_width)
                elif definition.shape == "box":
                    shape = Rectangle(width=(definition.width or 1) * scale, height=(definition.height or 1) * scale, color=color, fill_color=color, fill_opacity=definition.opacity, stroke_width=definition.stroke_width)
                elif definition.shape == "polygon":
                    shape = Polygon(*[(x * scale, y * scale, 0) for x, y in definition.vertices or []], color=color, fill_color=color, fill_opacity=definition.opacity, stroke_width=definition.stroke_width)
                else:
                    pa = segment_point(definition.point_a or (0, 0), state)
                    pb = segment_point(definition.point_b or (1, 0), state)
                    shape = Line(pa, pb, color=color, stroke_width=max(1, (definition.segment_radius or 2) * scale * 8))
                if definition.shape != "segment":
                    shape.rotate(state["angle"])
                    shape.move_to(point(state["x"], state["y"]))
                shapes[obj_id] = shape
                self.add(shape)
                if definition.show_label and definition.label:
                    label = Text(definition.label, font_size=20, color=label_color)
                    label.next_to(shape, direction=(0, 1, 0), buff=0.08)
                    labels[obj_id] = label
                    self.add(label)
                if timeline.scene.trail.enabled and not definition.is_static:
                    trail = VMobject(color=color, stroke_width=2)
                    trails[obj_id] = trail
                    trail_points[obj_id] = deque(maxlen=timeline.scene.trail.max_points)
                    self.add(trail)

            if timeline.scene.title:
                title = Text(timeline.scene.title, font_size=30, color=label_color)
                title.to_edge((0, 1, 0))
                self.add(title)

            vector_overlays: dict[str, tuple[Any, Any | None, Any]] = {}
            constraint_overlays: dict[str, tuple[Any, Any]] = {}
            highlight_overlays: dict[str, tuple[Any, str]] = {}
            graph_overlays: dict[str, dict[str, Any]] = {}
            bottom_slot = 0
            for overlay_id, overlay_track in timeline.overlay_tracks.items():
                overlay = overlay_track.overlay
                if not overlay.visible:
                    continue
                color = ManimColor(overlay.color)
                if overlay.kind == "vector" and overlay.target_id in initial:
                    origin = point(initial[overlay.target_id]["x"], initial[overlay.target_id]["y"])
                    line = Line(origin, (origin[0] + 0.001, origin[1], 0), color=color, stroke_width=4)
                    vector_label = Text(overlay.label, font_size=16, color=color) if overlay.label else None
                    self.add(line)
                    if vector_label is not None:
                        self.add(vector_label)
                    vector_overlays[overlay_id] = (line, vector_label, overlay)
                elif overlay.kind == "path" and isinstance(overlay.data.get("constraint"), str):
                    constraint = next((item for item in timeline.scene.constraints if item.id == overlay.data["constraint"]), None)
                    if constraint is not None:
                        line = Line(
                            point(initial[constraint.object_a]["x"], initial[constraint.object_a]["y"]),
                            point(initial[constraint.object_b]["x"], initial[constraint.object_b]["y"]),
                            color=color,
                            stroke_width=3,
                        )
                        self.add(line)
                        constraint_overlays[overlay_id] = (line, constraint)
                elif overlay.kind == "equation" and overlay.label:
                    equation = Text(overlay.label, font_size=18, color=color)
                    equation.to_edge((0, -1, 0)).shift((0, bottom_slot * 0.35, 0))
                    bottom_slot += 1
                    self.add(equation)
                elif overlay.kind == "highlight" and overlay.target_id in shapes:
                    highlight = Circle(radius=max(shapes[overlay.target_id].width, shapes[overlay.target_id].height) * 0.7, color=color, stroke_width=3)
                    highlight.move_to(shapes[overlay.target_id])
                    self.add(highlight)
                    highlight_overlays[overlay_id] = (highlight, overlay.target_id)
                elif overlay.kind == "graph" and overlay.target_id in timeline.tracks:
                    panel = Rectangle(width=3.2, height=1.6, color=color, stroke_width=1)
                    panel.move_to((4.6, -2.35 + len(graph_overlays) * 1.8, 0))
                    graph_label = Text(overlay.label or "graph", font_size=13, color=color)
                    graph_label.next_to(panel, direction=(0, 1, 0), buff=0.05)
                    requested_series = overlay.data.get("series", ["x"])
                    allowed_series = {"x", "y", "angle", "vx", "vy", "ax", "ay", "kinetic_energy", "potential_energy", "momentum_x", "momentum_y"}
                    series_names = [str(item) for item in requested_series if str(item) in allowed_series][:3] if isinstance(requested_series, list) else ["x"]
                    if not series_names:
                        series_names = ["x"]
                    curves = [VMobject(color=ManimColor(value), stroke_width=2) for value in ("#378ADD", "#D85A30", "#639922")[: len(series_names)]]
                    self.add(panel, graph_label, *curves)
                    graph_overlays[overlay_id] = {
                        "panel": panel,
                        "series": series_names,
                        "curves": curves,
                        "track": timeline.tracks[overlay.target_id],
                    }

            previous_angles = {obj_id: initial[obj_id]["angle"] for obj_id in initial}
            last_trail_frame = [-1]
            frame_count = len(output_frame_times(timeline.duration, fps))

            def update_scene(timestamp: float) -> None:
                if cancelled.is_set():
                    raise MotionForgeError(ErrorCode.CANCELLED, "Video export was cancelled.")
                timestamp = min(timeline.duration, max(0.0, timestamp))
                frame_index = min(frame_count - 1, int(timestamp * fps + 1e-9))
                states = sample_timeline(timeline, timestamp)
                for obj_id, state in states.items():
                    definition = timeline.scene.objects[obj_id]
                    if definition.is_static:
                        continue
                    shape = shapes[obj_id]
                    shape.move_to(point(state["x"], state["y"]))
                    delta = (state["angle"] - previous_angles[obj_id] + math.pi) % (2 * math.pi) - math.pi
                    shape.rotate(delta)
                    previous_angles[obj_id] = state["angle"]
                    if obj_id in labels:
                        labels[obj_id].next_to(shape, direction=(0, 1, 0), buff=0.08)
                    if (
                        obj_id in trails
                        and frame_index != last_trail_frame[0]
                        and frame_index % timeline.scene.trail.sample_every == 0
                    ):
                        trail_points[obj_id].append(point(state["x"], state["y"]))
                        if len(trail_points[obj_id]) >= 2:
                            trails[obj_id].set_points_as_corners(list(trail_points[obj_id]))
                for line, vector_label, overlay in vector_overlays.values():
                    target = states[overlay.target_id]
                    vx, vy = _overlay_vector(overlay.data.get("source"), target, timeline, overlay.target_id)
                    vector_scale = float(overlay.data.get("scale", 0.1))
                    start = point(target["x"], target["y"])
                    end = point(target["x"] + vx * vector_scale, target["y"] + vy * vector_scale)
                    if math.hypot(end[0] - start[0], end[1] - start[1]) < 0.001:
                        end = (start[0] + 0.001, start[1], 0)
                    line.put_start_and_end_on(start, end)
                    if vector_label is not None:
                        vector_label.next_to(line, direction=(0, 1, 0), buff=0.03)
                for line, constraint in constraint_overlays.values():
                    start_state = states[constraint.object_a]
                    end_state = states[constraint.object_b]
                    line.put_start_and_end_on(point(start_state["x"], start_state["y"]), point(end_state["x"], end_state["y"]))
                for highlight, target_id in highlight_overlays.values():
                    highlight.move_to(shapes[target_id])
                for graph in graph_overlays.values():
                    _update_graph(graph, timestamp, timeline.duration)
                last_trail_frame[0] = frame_index

            # Animating a ValueTracker suspends that tracker's updaters in Manim.
            # An invisible standalone driver also lets Cairo cache all visible
            # objects as a static background. Make every mutable visual part of
            # the animated group so the renderer redraws timeline changes.
            dynamic_mobjects = [*shapes.values(), *labels.values(), *trails.values()]
            for line, vector_label, _overlay in vector_overlays.values():
                dynamic_mobjects.append(line)
                if vector_label is not None:
                    dynamic_mobjects.append(vector_label)
            dynamic_mobjects.extend(line for line, _constraint in constraint_overlays.values())
            dynamic_mobjects.extend(highlight for highlight, _target_id in highlight_overlays.values())
            for graph in graph_overlays.values():
                dynamic_mobjects.extend(graph["curves"])
            self.remove(*dynamic_mobjects)
            driver = VGroup(*dynamic_mobjects)
            self.add(driver)
            self.play(
                UpdateFromAlphaFunc(driver, lambda _driver, alpha: update_scene(alpha * timeline.duration)),
                run_time=timeline.duration,
                rate_func=linear,
            )

    config = {
        "media_dir": str(workspace),
        "output_file": "motionforge-raw",
        "pixel_width": width,
        "pixel_height": height,
        "frame_rate": fps,
        "format": "mp4",
        "write_to_movie": True,
        "save_last_frame": False,
        "disable_caching": False,
        "verbosity": "WARNING",
    }
    try:
        with tempconfig(config):
            scene = TimelineScene()
            scene.render()
            movie = Path(scene.renderer.file_writer.movie_file_path)
        if not movie.is_file():
            raise MotionForgeError(ErrorCode.EXPORT_FAILED, "Manim did not create an output video.")
        return movie
    except MotionForgeError:
        raise
    except Exception as error:
        shutil.rmtree(workspace, ignore_errors=True)
        raise MotionForgeError(ErrorCode.EXPORT_FAILED, "Manim could not render the timeline.", details=str(error)) from error


def _is_dark(color: str) -> bool:
    red, green, blue = (int(color[index : index + 2], 16) for index in (1, 3, 5))
    return (0.2126 * red + 0.7152 * green + 0.0722 * blue) < 128


def _overlay_vector(source: Any, state: dict[str, float], timeline: Timeline, target_id: str) -> tuple[float, float]:
    if source == "velocity":
        return state.get("vx", 0), state.get("vy", 0)
    if source == "acceleration":
        return state.get("ax", 0), state.get("ay", 0)
    if source == "momentum":
        return state.get("momentum_x", 0), state.get("momentum_y", 0)
    if source == "gravity":
        return timeline.scene.gravity
    if source == "force":
        return state.get("force_x", 0), state.get("force_y", 0)
    if source in {"normal", "friction", "constraint"}:
        mass = timeline.scene.objects[target_id].mass
        return (
            mass * (state.get("ax", 0) - timeline.scene.gravity[0]),
            mass * (state.get("ay", 0) - timeline.scene.gravity[1]),
        )
    return 0.0, 0.0


def _update_graph(graph: dict[str, Any], timestamp: float, duration: float) -> None:
    track = graph["track"]
    panel = graph["panel"]
    centre = panel.get_center()
    selected = [index for index, sample_time in enumerate(track.times) if sample_time <= timestamp + 1e-9]
    if len(selected) < 2:
        return
    for series, curve in zip(graph["series"], graph["curves"], strict=False):
        values = getattr(track, series, [])
        if not values:
            continue
        minimum, maximum = min(values), max(values)
        span = maximum - minimum or 1.0
        points = [
            (
                centre[0] - 1.5 + 3.0 * track.times[index] / duration,
                centre[1] - 0.7 + 1.4 * (values[index] - minimum) / span,
                0,
            )
            for index in selected
        ]
        if len(points) >= 2:
            curve.set_points_as_corners(points)
