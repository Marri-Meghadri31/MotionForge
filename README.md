# MotionForge

MotionForge is a renderer-independent physics visualization engine for the Velo tutor. It compiles a prompt or deterministic template into a bounded `SceneSpec`, simulates it with Pymunk, returns a compact interactive `Timeline` immediately, and performs optional Manim/FFmpeg MP4 export in an isolated worker.

All application code lives in `src/motionforge`. The root `main.py` is only a compatibility launcher for older Velo builds.

## Quick start

```powershell
uv sync
uv run motionforge compile "show projectile motion" -o scene.json
uv run motionforge simulate scene.json -o timeline.json
uv run motionforge export timeline.json -o projectile.mp4
```

Common concepts use deterministic templates and do not require a model. Model-backed prompts default to Ollama and can use Anthropic explicitly:

```powershell
uv run motionforge compile "a niche mechanics demonstration" --no-template --provider ollama --model llama3.1
```

The legacy prompt-to-video command remains available:

```powershell
uv run prompt-animator "a ball drops and bounces" --quality low --output bounce
```

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
  -> compiler (template or structured provider)
  -> validated SceneSpec
  -> fixed-step Pymunk simulation
  -> compact renderer-neutral Timeline
       -> Velo Canvas playback
       -> isolated Manim worker -> FFmpeg H.264 MP4
```

Key modules:

```text
src/motionforge/
├── api/          authenticated localhost HTTP + SSE
├── compiler/     template classification, structured generation, safe repair
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

## Deterministic templates

The compiler recognizes falling/bouncing bodies, projectile motion, ramps and friction, pendulums, collisions and momentum, circular motion/orbits, force diagrams, springs/SHM, and position/velocity/acceleration graphs. Template parameters are validated and declared in the returned scene so Velo can safely re-simulate changes without another model call.

## Build an executable

The supported PyInstaller build is `onedir` for fast startup and inspectable native dependencies:

```powershell
.\scripts\build.ps1
```

Output:

```text
dist\prompt-animator\prompt-animator.exe
```

The build script bundles the FFmpeg selected by `-FfmpegPath`, `MOTIONFORGE_FFMPEG`, or the current `PATH`. At runtime MotionForge checks the bundled resource, the executable directory, `MOTIONFORGE_FFMPEG`, then `PATH`. Build separately on each target OS/architecture; PyInstaller artifacts are not cross-platform.

```powershell
.\scripts\build.ps1 -FfmpegPath C:\ffmpeg\bin\ffmpeg.exe
```

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
