# Velo integration contract

MotionForge contract v1 matches the responsibility boundary in `G:\Git_repo\Velo\planUI.md`: Velo owns the learner UI, Canvas player, file serving, and sidecar lifecycle; MotionForge owns compilation, validation, simulation, timelines, and optional export.

## Lifecycle

1. Spawn `prompt-animator.exe serve --port 0` once with a hidden window.
2. Read the first JSON line from stdout. It contains `event: "ready"`, the selected port, a random launch secret, PID, and `contractVersion: 1`.
3. Call authenticated `GET /v1/health` and disable Visualize if the contract version is unsupported. Timeline playback remains available when the exporter is unhealthy.
4. Stop the sidecar when the desktop shell exits. MotionForge marks unfinished persisted jobs as interrupted on the next launch.

Do not put provider keys or the launch secret in URLs, command-line provider arguments, logs, or job metadata. Pass provider keys through the protected process environment or a future authenticated credential channel.

## Visualization flow

The UI-oriented API compiles and simulates under one durable visualization ID:

```http
POST /v1/visualizations
Authorization: Bearer <launch-secret>
Content-Type: application/json

{
  "contractVersion": 1,
  "prompt": "Launch a projectile at 45 degrees",
  "preferTemplate": true,
  "provider": "ollama",
  "model": "llama3.1",
  "simulationOptions": {
    "recommendedPlaybackFps": 30,
    "recordInspectables": true,
    "detectEvents": true
  }
}
```

Poll `GET /v1/visualizations/:id` or subscribe to `GET /v1/visualizations/:id/events`. Once its stage is `ready`, fetch the compact timeline separately:

```http
GET /v1/visualizations/:id/timeline
```

Store that timeline immediately and render it in Canvas. MP4 is never on the preview critical path. The lower-level `/v1/scenes/compile` and `/v1/simulations` job APIs remain available for clients that need stage-by-stage orchestration.

Apply a validated declared parameter and rebuild the timeline while retaining the visualization ID:

```http
POST /v1/visualizations/:id/parameters
Content-Type: application/json

{
  "contractVersion": 1,
  "parameters": { "speed": 500 }
}
```

Parameter updates reject unknown, unsafe, incorrectly typed, and out-of-range values. Contract v1 supports parameter updates for deterministic template visualizations.

Optional export uses the visualization timeline:

```http
POST /v1/visualizations/:id/exports
Content-Type: application/json
```

```json
{
  "contractVersion": 1,
  "options": { "preset": "preview" }
}
```

Only one export runs at a time by default. Compilation and simulation can continue while an export worker runs. If export fails, retain the timeline and show the stable export error separately.

## Progress and cancellation

Every job has this stable shape:

```json
{
  "contractVersion": 1,
  "jobId": "...",
  "kind": "simulation",
  "status": "running",
  "stage": "simulating",
  "progress": 0.65,
  "error": null,
  "result": null,
  "createdAt": "...",
  "updatedAt": "...",
  "timings": {}
}
```

Use `GET /v1/visualizations/:id/events` for SSE and send its `id` as `Last-Event-ID` when reconnecting. Polling `GET /v1/visualizations/:id` is a compatible fallback. Cancel active visualization and linked export work with `DELETE /v1/visualizations/:id`; the simulator checks its token between steps and export cancellation terminates the worker process tree. The lower-level job event and cancellation routes remain supported.

## Canvas playback

- Scene coordinates are Cartesian `y`-up and independent of the viewport.
- `scene.size` is the authored world viewport, normally `[800, 500]`.
- Static geometry and styles live once in `scene.objects`.
- `tracks[id]` contains parallel arrays: `times`, `x`, `y`, `angle`, plus optional inspectable arrays.
- Interpolate position and scalar values linearly. Interpolate angle along the shortest wrapped path.
- A static object's track contains one sample at `t=0`.
- Draw trails with `scene.trail.maxPoints` and `sampleEvery`; never retain an unbounded point list.
- Overlay tracks are independent of physics tracks so Guide can change visibility or highlights without re-simulation.
- Values available for inspection include velocity, angular velocity, acceleration, applied force, kinetic/potential energy, and momentum.

The current Velo backend still invokes the compatible legacy prompt-to-MP4 CLI. Its migration target is the lifecycle above;