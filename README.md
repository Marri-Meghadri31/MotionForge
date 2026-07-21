# MotionForge

MotionForge is a renderer-independent physics visualization engine for the Velo tutor. It plans a prompt into a bounded, composable `SceneSpec`, simulates it with Pymunk, returns a compact interactive `Timeline` immediately, and performs optional Manim/FFmpeg MP4 export in an isolated worker.

All application code lives in `src/motionforge`. The root `main.py` is only a compatibility launcher for older Velo builds.

## Development with GPT-5.6 and Codex

GPT-5.6 was used as an AI development assistant throughout MotionForge's development. It helped plan the renderer-independent architecture, explore the structured scene compiler and physics-animation pipeline, strengthen schema validation and deterministic repair, and reason through build, performance, and prompt-to-video playback issues.

Codex was used alongside GPT-5.6 as a repository-aware coding agent. It inspected and modified the Python codebase, connected MotionForge to Velo, implemented and refined compiler, simulation, rendering, sidecar, and packaging behavior, updated documentation, ran regression tests, and verified representative scenes such as ramps, collisions, and orbital motion. AI-generated suggestions and code were reviewed and validated as part of the normal development workflow.

GPT-5.6 and Codex were development aids, not runtime dependencies. MotionForge's runtime scene generation remains independently configurable through its supported model providers and currently defaults to Ollama; neither development tool is bundled with the application or required by end users.

## Quick start

```powershell
uv sync
uv run motionforge compile "show projectile motion" -o scene.json
uv run motionforge simulate scene.json -o timeline.json
uv run motionforge export timeline.json -o projectile.mp4
```

General scene planning is the default and uses Ollama unless another provider is selected:

```powershell
uv run motionforge compile "a red ball drops onto a 20 degree incline with friction" --provider ollama --model llama3.1
```

Deterministic templates are opt-in fast paths for offline or latency-sensitive use:

```powershell
uv run motionforge compile "show projectile motion" --prefer-template -o scene.json
uv run motionforge compile "lamp shadow" --template lamp-shadow -o scene.json
```

The legacy prompt-to-video command remains available:

```powershell
uv run prompt-animator "a ball drops and bounces" --quality low --output bounce
```

This command shares the sidecar's versioned scene and timeline caches. Repeating an unchanged prompt and model skips model planning and simulation, and its stage output reports compile, simulation, render, and total timings.

## Persistent sidecar

Start the authenticated localhost service once:

```powershell
uv run motionforge serve --port 8765 --secret "a-long-random-launch-secret"
```

When `--secret` is omitted, MotionForge generates one and prints a single JSON `ready` event containing its port, secret, PID, and contract version. The service binds only to `127.0.0.1`.

Contract v1 routes:

```text
POST   /v1/visualizations
GET    /v1/visualizations/:id
GET    /v1/visualizations/:id/timeline
POST   /v1/visualizations/:id/parameters
POST   /v1/visualizations/:id/exports
DELETE /v1/visualizations/:id
GET    /v1/visualizations/:id/events
POST   /v1/scenes/compile
POST   /v1/simulations
POST   /v1/exports
GET    /v1/jobs/:id
GET    /v1/jobs/:id/events
DELETE /v1/jobs/:id
GET    /v1/health
```

Use either `Authorization: Bearer <secret>` or `X-MotionForge-Secret`. The visualization routes provide the UI-oriented compile-to-timeline lifecycle under one stable visualization ID. The lower-level compile, simulation, and export routes remain available as independent durable jobs. `GET .../events` is an SSE stream; polling the visualization or job resource is also supported.

See [Velo integration](docs/velo-integration.md) for the request sequence and Canvas coordinate contract.

## Architecture

```text
prompt/parameters
  -> structured scene planner (optional explicit template fast path)
  -> validated SceneSpec
  -> fixed-step Pymunk simulation + declarative force fields
  -> compact renderer-neutral Timeline
       -> Velo Canvas playback
       -> isolated Manim worker -> FFmpeg H.264 MP4
```

Key modules:

```text
src/motionforge/
├── api/          authenticated localhost HTTP + SSE
├── compiler/     structured scene planning, optional templates, safe repair
├── jobs/         SQLite persistence, progress, cancellation, export queue
├── physics/      headless Pymunk simulation and educational events
├── providers/    Ollama and Anthropic capability abstraction
├── render/       optional Manim renderer and FFmpeg encoder
├── timeline/     compact tracks, legacy reader, interpolation
├── cache.py      versioned atomic scene/timeline/export caches
├── core.py       compile_scene, simulate_scene, export_video
└── models.py     bounded versioned contracts
```

Runtime data is written to the OS application-data directory, never the install directory. Override it with `MOTIONFORGE_DATA_DIR` for development or tests.

## General scene composition

The model planner composes scenes from bodies, geometry, contacts, material properties, constant forces, inverse-square force fields, springs, pins, measurements, and educational overlays. This means new prompts do not require a new Python scene function. For example, an elliptical orbit uses the same Pymunk body contract as a collision, with a declarative position-dependent force evaluated at every simulation step.

Two non-template examples are included and can be rendered without a model or web application:

```powershell
uv run motionforge simulate examples/elliptical_orbit.scene.json -o orbit-timeline.json
uv run motionforge export orbit-timeline.json -o elliptical-orbit.mp4 --quality high

uv run motionforge simulate examples/red_ball_ramp.scene.json -o ramp-timeline.json
uv run motionforge export ramp-timeline.json -o red-ball-ramp.mp4 --quality high
```

## Optional deterministic templates

When `--prefer-template`, `preferTemplate:true`, or an explicit template ID is supplied, the compiler can use deterministic implementations for falling/bouncing bodies, projectile motion, ramps and friction, pendulums, collisions and momentum, circular motion, force diagrams, springs/SHM, motion graphs, and lamp-shadow problems. Template parameters are validated and declared in the returned scene so a caller can safely re-simulate changes without another model call.

Geometry-heavy questions use the same pipeline as rigid-body scenes. Pymunk produces the authoritative object tracks; the timeline layer resolves object anchors, projected rays, line intersections, and measurements; Manim only renders those sampled results. This keeps shadows, optics rays, and relative-distance annotations synchronized with the physics.

Render the lamp-post example directly, without starting the sidecar or a web app:

```powershell
uv run motionforge compile "A 1.6 m person walks away from a 4 m lamp post at 60 cm/s; show the shadow speed relative to the person" --template lamp-shadow -o lamp-shadow.json
uv run motionforge simulate lamp-shadow.json -o lamp-shadow-timeline.json
uv run motionforge export lamp-shadow-timeline.json -o lamp-shadow.mp4 --quality high
```

The similar-triangle relation is `s/x = 1.6/(4 - 1.6) = 2/3`, so the shadow tip moves at **40 cm/s relative to the person** (and 100 cm/s relative to the ground).

## Build an executable

The supported PyInstaller build is `onedir` for fast startup and inspectable native dependencies:

```powershell
.\scripts\build.ps1
```

Output:

```text
dist\prompt-animator\prompt-animator.exe
```

The build does not bundle FFmpeg. Interactive timeline visualization works without it. MP4 export requires FFmpeg to be supplied separately through `MOTIONFORGE_FFMPEG`, an adjacent `resources/ffmpeg` directory, or the system `PATH`. Build separately on each target OS/architecture; PyInstaller artifacts are not cross-platform.

Point the current Velo legacy backend at the onedir executable while it migrates to the sidecar contract:

```powershell
$env:MOTIONFORGE_EXECUTABLE = "G:\Git_repo\MotionForge\dist\prompt-animator\prompt-animator.exe"
```

## Verification

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Native duration/codec tests render real videos at 24, 30, and 60 FPS and are opt-in:

```powershell
$env:MOTIONFORGE_RUN_EXPORT_TESTS = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_export_integration -v
```

Benchmark the nine-template quality suite and report p50/p95 stage latency:

```powershell
.\.venv\Scripts\python.exe scripts\benchmark.py --iterations 10
```

The latest checked Windows x64 measurement is in [`benchmarks/windows-x64.json`](benchmarks/windows-x64.json); it records a 6.373 ms p95 template preview and 5.853 ms p95 validated cache lookup on this machine.

`/v1/health` reports build target, contract/schema/timeline versions, provider reachability and capabilities, Manim, FFmpeg, codec/pixel-format support, and the no-LaTeX font strategy. Signing, notarization, and clean-VM qualification remain release-pipeline responsibilities for each platform.
