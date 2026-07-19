# MotionForge engine plan

## Purpose

This plan covers the `MotionForge` repository: scene compilation, schema validation, physics simulation, timeline generation, preview data, optional video export, performance, and sidecar packaging.

The Velo learner interface, Explain and Guide workflows, TTS, settings, and desktop shell are covered in `G:\Git_repo\Velo\planUI.md`.

## Engine goal

MotionForge should become a renderer-independent physics visualization service:

```text
prompt or template parameters
  -> validated SceneSpec
  -> Pymunk simulation
  -> compact Timeline
       |-> live Canvas playback in Velo
       `-> optional Manim/FFmpeg export
```

The normal learning flow must not wait for MP4 generation. Scene compilation and simulation should return a playable timeline first; high-quality export should be a separate background operation.

## Current findings

1. `main.py` performs compile, simulate, timeline conversion, and render as one synchronous operation.
2. Only `storyboard[0]` is used, so the storyboard abstraction is not yet implemented end to end.
3. Each Velo request starts a fresh PyInstaller executable and imports Manim again.
4. Ollama uses `format: "json"`, a fixed 120-second timeout, and no schema, keep-alive, output limit, or cancellation.
5. Invalid model output can trigger up to three additional model calls.
6. Scene bounds, duration, timestep, object count, coordinates, and polygon complexity are not sufficiently capped.
7. The simulator defaults to 60 Hz while low-quality Manim output is 30 FPS.
8. The renderer calls `wait(dt)` for every simulation sample. A 1/60-second wait at 30 FPS can consume a full 1/30-second frame and produce incorrect video duration.
9. Static object and style data are copied into every timeline keyframe.
10. Trails add a new `Dot` on every frame and can continuously increase scene complexity.
11. Camera zoom exists in the timeline but is not applied by the current Manim renderer.
12. Progress is unstructured console text.
13. The PyInstaller specification builds a one-file Windows executable and does not yet demonstrate clean-machine FFmpeg/font compatibility.

## Public engine contract

Split the current `run()` function into independently testable operations:

```python
compile_scene(request: CompileRequest) -> SceneSpec
simulate_scene(scene: SceneSpec, options: SimulationOptions) -> Timeline
export_video(timeline: Timeline, options: ExportOptions) -> ExportResult
```

Provide a versioned sidecar API:

```text
POST   /v1/scenes/compile
POST   /v1/simulations
POST   /v1/exports
GET    /v1/jobs/:id
GET    /v1/jobs/:id/events
DELETE /v1/jobs/:id
GET    /v1/health
```

Every job response should contain:

```json
{
  "contractVersion": 1,
  "jobId": "...",
  "status": "running",
  "stage": "simulating",
  "progress": 0.65,
  "error": null,
  "result": null
}
```

Use stable error codes such as `MODEL_UNAVAILABLE`, `INVALID_SCENE`, `SIMULATION_FAILED`, `EXPORT_FAILED`, `CANCELLED`, `TIMEOUT`, `DISK_FULL`, and `CONTRACT_MISMATCH`.

For desktop IPC, use localhost HTTP with a per-launch secret, named pipes, or JSON Lines over stdin/stdout. Bind HTTP only to `127.0.0.1`. Keep the CLI as a development and automation client of the same core API.

## Scene compiler

### Schema improvements

- Add an explicit schema version.
- Add limits for duration, timestep, object count, polygon vertices, force magnitude, coordinates, velocity, angular velocity, mass, labels, and trail settings.
- Reject non-finite values and invalid colors.
- Validate force target IDs and all visual references.
- Validate polygon geometry and winding before passing it to Pymunk.
- Define scene dimensions and units instead of relying on prompt-only pixel conventions.
- Separate static object definitions from animation tracks.
- Add optional educational overlays: vectors, paths, equations, graph axes/series, event markers, and inspectable values.
- Add declared parameters with type, range, unit, default, and whether local re-simulation is safe.

### Model generation improvements

- Pass Ollama an exact JSON Schema in `format` when supported.
- Detect provider capabilities before selecting structured-output behavior.
- Use low temperature and a bounded output-token limit for compilation.
- Configure provider URL, connect/read timeout, model, keep-alive, and retry policy.
- Set Ollama `keep_alive` so local models remain loaded between requests.
- Add request cancellation.
- Auto-repair safe issues such as a missing style, clamped duration, normalized color, or camera bounds before making another model call.
- Retry only recoverable failures and include the exact validation errors in repair requests.
- Return a concise diagnostic that identifies the invalid schema path.

### Deterministic templates

Bypass the LLM for common concepts when the prompt can be classified reliably:

- falling and bouncing bodies;
- projectile motion;
- ramps and friction;
- pendulums;
- collisions and momentum;
- circular motion and simple orbit demonstrations;
- force/vector diagrams;
- springs and simple harmonic motion;
- simple position, velocity, and acceleration graphs.

Templates should accept validated parameters and produce a SceneSpec directly. Record whether a scene came from a template or model so cache behavior and quality can be measured.

## Physics simulation

- Keep a fixed physics timestep independent from display or export FPS.
- Include the initial state at `t=0` and a final sample at the requested duration.
- Define exact duration behavior when duration is not divisible by `dt`.
- Produce events for collisions, apex, rest, crossings, and other educational moments where practical.
- Optionally record velocity, angular velocity, acceleration, force, kinetic energy, potential energy, and momentum for inspectable objects.
- Use collision filtering and sleeping where appropriate.
- Reject pathological scenes before allocating large timelines.
- Make cancellation checks during long simulations.
- Ensure deterministic output for the same normalized SceneSpec and engine version.

### Simulation correctness tests

- Free fall against analytic position and velocity.
- Bounce height against restitution within tolerance.
- Sliding/friction deceleration.
- Projectile range and apex.
- Conservation behavior for controlled collision and energy examples.
- Static segment/box/polygon collision fixtures.
- Constant forces applied only to valid dynamic bodies.
- Exact start/end timestamps for multiple durations and timesteps.

## Timeline redesign

The current timeline duplicates geometry and style in every frame. Replace it with a compact renderer-neutral format:

```json
{
  "contractVersion": 1,
  "duration": 3.0,
  "simulationFps": 60,
  "recommendedPlaybackFps": 30,
  "scene": {
    "size": [800, 500],
    "background": "#FFFFFF",
    "objects": {
      "ball": { "shape": "circle", "radius": 20, "color": "#378ADD" }
    }
  },
  "tracks": {
    "ball": {
      "times": [0, 0.016667],
      "x": [0, 0],
      "y": [300, 299.73],
      "angle": [0, 0]
    }
  },
  "events": []
}
```

- Store static geometry and styles once.
- Store dynamic tracks per object.
- Use consistent coordinate and angle conventions.
- Document interpolation rules.
- Keep overlays as separate tracks so Guide can change a highlight without re-simulating.
- Include camera tracks only when camera movement is required.
- Start with JSON. Consider MessagePack or compressed JSON only after profiling transfer and parsing costs.
- Add a compatibility reader for the previous timeline during migration if needed.

## Renderer and export fixes

### Correctness fixes

- Resample the physics timeline at the exact output frame timestamps.
- For 30 FPS output from a 60 Hz simulation, render 30 frames per second by sampling/interpolating rather than calling `wait(1/60)` 60 times.
- Add regression tests asserting that a 3-second timeline creates a 3-second video at 24, 30, and 60 FPS.
- Apply camera zoom or remove it from the supported contract until implemented.
- Ensure labels follow dynamic objects correctly without unintentionally changing the object's rotation center.
- Validate colors and fonts before constructing Manim objects.
- Encode MP4 as H.264 with `yuv420p` and fast-start metadata.

### Performance fixes

- Keep MP4 export off the live-preview critical path.
- Add a `preview` preset such as 640x360 at 24 or 30 FPS.
- Keep 1080p/60 as an explicit high-quality export.
- Replace per-frame trail dots with a bounded path object or lower-rate trail samples.
- Reuse static Manim objects and precomputed geometry.
- Avoid recreating labels and other unchanged visual elements.
- Disable or enable Manim caching based on measured behavior rather than unconditionally.
- Record separate timings for module/process startup, compilation, validation, simulation, timeline creation, Manim frame rendering, and FFmpeg encoding.

### Export isolation

- Run export work in a worker process so cancellation or a renderer crash does not stop the sidecar.
- Default to one concurrent export per machine.
- Allow lightweight compilation and simulation while export runs, subject to memory limits.
- Terminate the entire export process tree on cancellation.
- Write to a temporary file and atomically rename after successful completion.
- Preserve the playable timeline when an export fails.

## Performance targets and caching

### Main targets

- Template compile plus simulation: under 1 second for normal scenes.
- Cached timeline lookup: under 100 ms locally.
- Simulation: substantially faster than real time for bounded primitive scenes.
- Sidecar health response: under 200 ms after startup.
- Exact p50 and p95 targets for model compilation and MP4 export should be set after instrumentation.

### Cache design

Cache SceneSpec and Timeline separately using a hash of:

- normalized prompt or template parameters;
- compiler and template version;
- provider and model identifier;
- schema version;
- simulation engine version and options;
- renderer-independent visual options.

MP4 exports need an additional key containing renderer version, resolution, FPS, codec, and quality settings. Use a disk-budget and age policy, with validated job-owned paths only.

## Job management

- Use structured progress events rather than console parsing.
- Persist job metadata in SQLite or a small durable job store.
- Recover completed files after restart.
- Mark interrupted jobs explicitly instead of leaving them `running`.
- Support compile, simulation, and export timeouts independently.
- Add cancellation tokens to provider calls, simulation loops, and exporters.
- Keep detailed local diagnostics while returning user-safe errors to Velo.
- Redact prompts or sensitive provider data according to the selected privacy setting.

## Sidecar and distribution compatibility

### Build approach

- Prefer a PyInstaller `onedir` build. It usually starts faster and is easier to inspect than a one-file executable that extracts dependencies on each launch.
- Build separately for Windows x64, Windows ARM64 if required, macOS Apple Silicon/x64, and Linux x64.
- Do not treat the Windows `.exe` as cross-platform.
- Sign and notarize applicable binaries.
- Resolve resources relative to the installed executable, never the development repository.
- Write logs, jobs, cache, and exports under OS application-data directories.

### Runtime dependencies

- Explicitly bundle or locate FFmpeg and required native libraries.
- Bundle known fonts or define tested fallback fonts.
- Avoid LaTeX as a runtime dependency unless it is fully bundled and tested.
- Report available renderers, codecs, fonts, provider connectivity, build target, engine version, and schema version from `/v1/health`.
- Fail a renderer health check early rather than after scene generation.

### Clean-machine cases

Test with no preinstalled Python, Manim, FFmpeg, Ollama, development fonts, compiler, or repository checkout. Also test:

- paths containing spaces and non-ASCII characters;
- read-only installation directories;
- low disk space;
- no network or unavailable cloud provider;
- local Ollama without the selected model;
- abrupt parent-process termination;
- application upgrade with old cached timelines;
- cancellation during compilation and export.

## Provider abstraction

Define a common provider interface:

```python
health()
list_models()
generate_text()
generate_structured(schema)
cancel(request_id)
```

- Keep provider-specific payloads out of the scene compiler.
- Support capability flags such as structured output, streaming, cancellation, keep-alive, and local/cloud.
- Never include API keys in command-line arguments, URLs, logs, job metadata, or returned contracts.
- Let Velo retrieve credentials from the OS vault and pass them through a protected process environment or authenticated local channel.
- Use smaller structured-output models as the default when quality tests pass; reserve the largest model for a Best quality option.

## Security and resource limits

- Bind the service only to localhost and require a random per-launch secret from Velo.
- Apply request-size, duration, object-count, frame-count, polygon, label, and output-size limits.
- Treat model-generated data as untrusted input.
- Do not execute generated Python, Manim code, shell commands, LaTeX, URLs, or arbitrary asset paths.
- Use generated job IDs and fixed output names.
- Validate every path before read, write, move, or cleanup.
- Apply separate CPU-time and wall-time limits where the platform permits.

## Testing plan

### Unit tests

- Schema boundaries and every supported shape.
- JSON extraction, deterministic repair, provider timeout, and cancellation.
- Template classification and generated SceneSpecs.
- Physics fixtures and exact timestamps.
- Timeline compaction, interpolation, and serialization.
- Render-frame sampling at 24, 30, and 60 FPS.
- Cache keys and invalidation.

### Integration tests

- Compile -> simulate -> timeline.
- Compile -> simulate -> MP4.
- Template -> simulate without a model.
- Structured progress, cancellation, timeout, and restart recovery.
- Missing model, provider, font, codec, or FFmpeg.
- Contract compatibility with the Velo client.

### Quality suite

Maintain a curated set of prompts covering:

- projectile motion;
- falling and bouncing bodies;
- ramps and friction;
- elastic and inelastic collisions;
- energy and momentum;
- pendulums and springs;
- circular/orbital motion;
- graphs and vector diagrams.

For each case, check schema validity, framing, labels, physical plausibility, timeline duration, and a representative preview frame. Track model/template success rate and latency across releases.

## Delivery phases

### Phase MF-0: correct and bound the current pipeline

- [ ] Fix 60 Hz simulation to 30 FPS render sampling and exact video duration.
- [ ] Bound duration, timestep, objects, polygons, coordinates, forces, labels, and trails.
- [ ] Add stable errors and separate stage timeouts.
- [ ] Replace unbounded trail dots.
- [ ] Add physics and render-duration regression tests.

**Exit condition:** the existing MP4 pipeline is physically and temporally correct, bounded, and testable.

### Phase MF-1: modular engine and contracts

- [ ] Split compile, simulate, and export into independent APIs.
- [ ] Version SceneSpec, Timeline, job, progress, and error schemas.
- [ ] Compact timeline data and separate static data from tracks.
- [ ] Add exact JSON-schema generation and deterministic repairs.
- [ ] Add structured progress and cancellation.

**Exit condition:** every stage can run and fail independently through a stable contract.

### Phase MF-2: persistent sidecar and fast preview

- [ ] Implement the authenticated localhost or JSON-Lines sidecar.
- [ ] Keep provider connections/models warm where supported.
- [ ] Return the timeline before optional export.
- [ ] Add queueing, persistence, restart recovery, and safe cleanup.
- [ ] Integrate contract tests with Velo's Canvas player.

**Exit condition:** MotionForge starts once and returns live-preview data without launching Manim.

### Phase MF-3: optimization

- [ ] Add deterministic templates for common physics concepts.
- [ ] Add SceneSpec, Timeline, and export caches.
- [ ] Optimize trails, static objects, labels, and render sampling.
- [ ] Instrument each stage and publish p50/p95 measurements.
- [ ] Tune model size, structured output, keep-alive, and retry policy.

**Exit condition:** template/cached scenes meet the preview latency targets and uncached latency is measured by stage.

### Phase MF-4: multi-platform packaging

- [ ] Produce per-platform `onedir` builds.
- [ ] Bundle/test FFmpeg, fonts, and native libraries.
- [ ] Implement detailed health and capability reporting.
- [ ] Sign binaries and test them in clean virtual machines.
- [ ] Add contract/cache migration rules for upgrades.

**Exit condition:** the sidecar operates on every supported clean target without a development environment.

## Recommended implementation order

1. Fix timeline sampling/video duration and add regression tests.
2. Add schema resource limits and stable stage errors.
3. Define versioned SceneSpec, compact Timeline, progress, job, and error contracts.
4. Split compile, simulate, and export, then expose them through a persistent sidecar.
5. Return live timeline data first and make Manim/MP4 optional.
6. Add templates, caching, instrumentation, and cross-platform `onedir` packaging.

