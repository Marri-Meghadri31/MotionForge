from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from threading import Event
from typing import Any

from pydantic import ValidationError

from motionforge.api import serve
from motionforge.core import compile_scene, export_video, simulate_scene
from motionforge.errors import ErrorCode, MotionForgeError, validation_diagnostics
from motionforge.models import CompileRequest, ExportOptions, SceneSpec, SimulationOptions, Timeline

COMMANDS = {"serve", "compile", "simulate", "export", "run", "_export-worker"}


def _write_json(value: Any, path: str | None = None) -> None:
    payload = value.contract_dump() if hasattr(value, "contract_dump") else value
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, destination)
    else:
        print(text)


def _read_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="motionforge", description="Compile, simulate, preview, and export physics scenes")
    subparsers = parser.add_subparsers(dest="command", required=True)

    server = subparsers.add_parser("serve", help="Start the authenticated persistent localhost sidecar")
    server.add_argument("--port", type=int, default=int(os.environ.get("MOTIONFORGE_PORT", "8765")))
    server.add_argument("--secret", default=os.environ.get("MOTIONFORGE_SECRET"))
    server.add_argument("--data-dir", default=os.environ.get("MOTIONFORGE_DATA_DIR"))

    compile_parser = subparsers.add_parser("compile", help="Compile a prompt or template to a SceneSpec")
    compile_parser.add_argument("prompt")
    _provider_arguments(compile_parser)
    compile_parser.add_argument("--template")
    compile_parser.add_argument("--parameters", help="JSON object of deterministic template parameters")
    compile_parser.add_argument("--no-template", action="store_true")
    compile_parser.add_argument("--output", "-o")

    simulate_parser = subparsers.add_parser("simulate", help="Simulate a SceneSpec into a compact Timeline")
    simulate_parser.add_argument("scene", help="SceneSpec JSON file")
    simulate_parser.add_argument("--playback-fps", type=int, default=30)
    simulate_parser.add_argument("--no-inspectables", action="store_true")
    simulate_parser.add_argument("--output", "-o")

    export_parser = subparsers.add_parser("export", help="Export a Timeline to H.264 MP4")
    export_parser.add_argument("timeline", help="Timeline JSON file")
    export_parser.add_argument("--output", "-o", default="animation.mp4")
    export_parser.add_argument("--quality", choices=["preview", "high"], default="preview")
    export_parser.add_argument("--fps", type=int)
    export_parser.add_argument("--width", type=int)
    export_parser.add_argument("--height", type=int)

    run_parser = subparsers.add_parser("run", help="Legacy prompt-to-video flow using the same core APIs")
    run_parser.add_argument("prompt")
    _provider_arguments(run_parser)
    run_parser.add_argument("--template")
    run_parser.add_argument("--parameters")
    run_parser.add_argument("--no-template", action="store_true")
    run_parser.add_argument("--output", "-o", default="output")
    run_parser.add_argument("--timeline-output")
    run_parser.add_argument("--no-export", action="store_true")
    run_parser.add_argument("--quality", choices=["low", "preview", "high"], default="low")

    worker = subparsers.add_parser("_export-worker", help=argparse.SUPPRESS)
    worker.add_argument("timeline")
    worker.add_argument("options")
    worker.add_argument("output")
    worker.add_argument("result")
    worker.add_argument("error")
    return parser


def _provider_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=["ollama", "anthropic"], default="ollama")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float, default=90)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] not in COMMANDS and not arguments[0].startswith("-"):
        arguments.insert(0, "run")
    parser = build_parser()
    args = parser.parse_args(arguments)
    try:
        if args.command == "serve":
            serve(args.port, args.secret, data_dir=args.data_dir)
            return 0
        if args.command == "compile":
            scene = _compile_from_args(args)
            _write_json(scene, args.output)
            return 0
        if args.command == "simulate":
            scene = SceneSpec.model_validate(_read_json(args.scene))
            timeline = simulate_scene(
                scene,
                SimulationOptions(recommended_playback_fps=args.playback_fps, record_inspectables=not args.no_inspectables),
            )
            _write_json(timeline, args.output)
            return 0
        if args.command == "export":
            timeline = Timeline.model_validate(_read_json(args.timeline))
            result = export_video(
                timeline,
                ExportOptions(preset=args.quality, fps=args.fps, width=args.width, height=args.height),
                output_path=args.output,
            )
            _write_json(result)
            return 0
        if args.command == "run":
            return _run(args)
        if args.command == "_export-worker":
            return _export_worker(args)
    except MotionForgeError as error:
        print(json.dumps({"error": error.as_dict()}, ensure_ascii=False), file=sys.stderr, flush=True)
        return 2
    except ValidationError as error:
        payload = {"code": ErrorCode.INVALID_REQUEST.value, "message": "Input validation failed.", "details": validation_diagnostics(error)}
        print(json.dumps({"error": payload}, ensure_ascii=False), file=sys.stderr, flush=True)
        return 2
    except (OSError, json.JSONDecodeError, ValueError) as error:
        payload = {"code": ErrorCode.INVALID_REQUEST.value, "message": str(error)}
        print(json.dumps({"error": payload}, ensure_ascii=False), file=sys.stderr, flush=True)
        return 2
    return 1


def _compile_from_args(args: argparse.Namespace) -> SceneSpec:
    parameters = json.loads(args.parameters) if args.parameters else {}
    request = CompileRequest(
        prompt=args.prompt,
        parameters=parameters,
        template=args.template,
        provider=args.provider,
        model=args.model,
        prefer_template=not args.no_template,
        timeout_seconds=args.timeout,
    )
    return compile_scene(request)


def _run(args: argparse.Namespace) -> int:
    print("[1/4] Compiling and validating the scene...", flush=True)
    scene = _compile_from_args(args)
    print(f"      -> {len(scene.physics.objects)} objects, {scene.physics.duration:g}s duration ({scene.metadata.origin})", flush=True)
    print("[2/4] Simulating the physics...", flush=True)
    timeline = simulate_scene(scene)
    print(f"      -> {max(len(track.times) for track in timeline.tracks.values())} simulation samples", flush=True)
    print("[3/4] Building the compact timeline...", flush=True)
    if args.timeline_output:
        _write_json(timeline, args.timeline_output)
    if args.no_export:
        if not args.timeline_output:
            _write_json(timeline)
        return 0
    print("[4/4] Rendering the animation...", flush=True)
    preset = "high" if args.quality == "high" else "preview"
    output = Path(args.output)
    if output.suffix.lower() != ".mp4":
        output = output.with_suffix(".mp4")
    result = export_video(timeline, ExportOptions(preset=preset), output_path=output)
    print(f"Done: {result.output_path}", flush=True)
    return 0


def _export_worker(args: argparse.Namespace) -> int:
    result_path = Path(args.result)
    error_path = Path(args.error)
    try:
        timeline = Timeline.model_validate(_read_json(args.timeline))
        options = ExportOptions.model_validate(_read_json(args.options))
        result = export_video(timeline, options, output_path=args.output, cancel_event=Event())
        _write_json(result, str(result_path))
        return 0
    except MotionForgeError as error:
        _write_json(error.as_dict(), str(error_path))
        return 2
    except Exception as error:
        _write_json({"code": ErrorCode.EXPORT_FAILED.value, "message": "Export worker failed.", "details": str(error)}, str(error_path))
        return 2
