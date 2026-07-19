"""Independently testable public engine operations."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import Any, Callable

from motionforge.compiler import SceneCompiler
from motionforge.models import CompileRequest, ExportOptions, ExportResult, SceneSpec, SimulationOptions, Timeline
from motionforge.physics import simulate
from motionforge.providers.base import Provider
from motionforge.timeline import build_timeline

ProgressCallback = Callable[[str, float], None]


def compile_scene(
    request: CompileRequest | dict[str, Any],
    *,
    provider: Provider | None = None,
    cancel_event: Event | None = None,
    progress: ProgressCallback | None = None,
) -> SceneSpec:
    parsed = request if isinstance(request, CompileRequest) else CompileRequest.model_validate(request)
    if progress:
        progress("compiling", 0.05)
    result = SceneCompiler(provider).compile(parsed, cancel_event=cancel_event)
    if progress:
        progress("validating", 1.0)
    return result


def simulate_scene(
    scene: SceneSpec | dict[str, Any],
    options: SimulationOptions | dict[str, Any] | None = None,
    *,
    cancel_event: Event | None = None,
    progress: ProgressCallback | None = None,
) -> Timeline:
    parsed_scene = scene if isinstance(scene, SceneSpec) else SceneSpec.model_validate(scene)
    parsed_options = (
        options
        if isinstance(options, SimulationOptions)
        else SimulationOptions.model_validate(options or {})
    )

    def simulation_progress(value: float) -> None:
        if progress:
            progress("simulating", value * 0.9)

    result = simulate(parsed_scene.physics, parsed_options, cancel_event=cancel_event, progress=simulation_progress)
    if progress:
        progress("buildingTimeline", 0.95)
    timeline = build_timeline(parsed_scene, result, parsed_options)
    if progress:
        progress("ready", 1.0)
    return timeline


def export_video(
    timeline: Timeline | dict[str, Any],
    options: ExportOptions | dict[str, Any] | None = None,
    *,
    output_path: str | Path,
    cancel_event: Event | None = None,
) -> ExportResult:
    parsed_timeline = timeline if isinstance(timeline, Timeline) else Timeline.model_validate(timeline)
    parsed_options = options if isinstance(options, ExportOptions) else ExportOptions.model_validate(options or {})
    from motionforge.render.manim_renderer import render_video

    return render_video(parsed_timeline, output_path, parsed_options, cancel_event=cancel_event)
