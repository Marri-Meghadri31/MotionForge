"""
Entrypoint: prompt -> mp4

    python main.py "a ball drops onto a ramp and rolls off" --output out.mp4
    python main.py "..." --provider ollama --model llama3.1
    python main.py "..." --provider anthropic --model claude-sonnet-5

Phase 1 scope:
- Single scene only (storyboard[0]). Multi-scene stitching is a stub for
  a later phase -- generate_scene() already returns a Storyboard so this
  file is the only place that needs to change when that's added.
- Primitive shapes only (circle, box, polygon, segment). No composites.
"""

from __future__ import annotations

import argparse
import sys

from engines.engine_2d import simulate
from llm.providers.anthropic_provider import AnthropicProvider
from llm.providers.base import LLMProvider
from llm.providers.ollama_provider import OllamaProvider
from llm.scene_generator import generate_scene
from render.manim_renderer import render_video
from timeline.converter import build_timeline


def run(prompt: str, output_path: str, provider: LLMProvider, quality: str = "low") -> str:
    print("[1/4] Generating scene spec from prompt...")
    storyboard = generate_scene(prompt, provider)
    scene = storyboard[0]  # Phase 1: single scene only
    print(f"      -> {len(scene.physics.objects)} objects, "
          f"{scene.physics.duration}s duration")

    print("[2/4] Running physics simulation...")
    frames = simulate(scene.physics)
    print(f"      -> {len(frames)} frames")

    print("[3/4] Building render timeline...")
    keyframes = build_timeline(frames, scene.physics.objects, scene.visual)

    print(f"[4/4] Rendering with Manim ({quality} quality)...")
    result_path = render_video(keyframes, output_path, quality=quality)
    print(f"Done: {result_path}")
    return result_path


def build_provider(provider_name: str, model: str | None) -> LLMProvider:
    if provider_name == "ollama":
        return OllamaProvider(model=model or "llama3.1")
    if provider_name == "anthropic":
        return AnthropicProvider(model=model or "claude-sonnet-5")
    raise ValueError(f"Unknown provider: {provider_name}")


def main():
    parser = argparse.ArgumentParser(description="Prompt-driven physics animator")
    parser.add_argument("prompt", help="Natural language description of the scene")
    parser.add_argument("--output", default="output", help="Output filename (without extension)")
    parser.add_argument("--quality", choices=["low", "high"], default="low")
    parser.add_argument(
        "--provider", choices=["anthropic", "ollama"], default="anthropic",
        help="Which LLM backend to use (default: anthropic)",
    )
    parser.add_argument(
        "--model", default=None,
        help="Model name override (e.g. 'llama3.1' for ollama, 'claude-sonnet-5' for anthropic)",
    )
    args = parser.parse_args()

    try:
        provider = build_provider(args.provider, args.model)
        run(args.prompt, args.output, provider, args.quality)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
