from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from motionforge.cache import JsonCache, cache_key
from motionforge.core import compile_scene, simulate_scene
from motionforge.models import CompileRequest, SceneSpec

QUALITY_PROMPTS = [
    "a falling ball bounces on the floor",
    "launch a projectile at 45 degrees",
    "a block slides down a ramp with friction",
    "show a simple pendulum",
    "two bodies collide and exchange momentum",
    "demonstrate circular motion",
    "draw the forces on a moving block",
    "a mass oscillates on a spring",
    "plot position velocity and acceleration",
]


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(fraction * len(ordered) + 0.999999) - 1))
    return ordered[index]


def summary(values: list[float]) -> dict[str, float]:
    return {
        "samples": len(values),
        "p50Ms": round(statistics.median(values) * 1_000, 3),
        "p95Ms": round(percentile(values, 0.95) * 1_000, 3),
        "maxMs": round(max(values) * 1_000, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark deterministic MotionForge preview stages")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("iterations must be positive")

    compile_times: list[float] = []
    simulation_times: list[float] = []
    total_times: list[float] = []
    cache_times: list[float] = []
    with tempfile.TemporaryDirectory() as temporary:
        cache = JsonCache(Path(temporary))
        for _ in range(args.iterations):
            for prompt in QUALITY_PROMPTS:
                total_started = time.perf_counter()
                compile_started = time.perf_counter()
                scene = compile_scene(CompileRequest(prompt=prompt))
                compile_times.append(time.perf_counter() - compile_started)
                simulation_started = time.perf_counter()
                simulate_scene(scene)
                simulation_times.append(time.perf_counter() - simulation_started)
                total_times.append(time.perf_counter() - total_started)
                key = cache_key("benchmark-scene", scene)
                cache.put("scenes", key, scene)
                cache_started = time.perf_counter()
                SceneSpec.model_validate(cache.get("scenes", key))
                cache_times.append(time.perf_counter() - cache_started)

    result = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "qualityPrompts": len(QUALITY_PROMPTS),
        "iterations": args.iterations,
        "templateCompile": summary(compile_times),
        "simulationAndTimeline": summary(simulation_times),
        "templatePreviewTotal": summary(total_times),
        "cachedSceneLookupAndValidation": summary(cache_times),
    }
    output = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
