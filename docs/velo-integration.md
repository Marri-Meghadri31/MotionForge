# Velo integration contract

MotionForge contract v1 matches the responsibility boundary in `G:\Git_repo\Velo\planUI.md`: Velo owns the learner UI, Canvas player, file serving, and sidecar lifecycle; MotionForge owns compilation, validation, simulation, timelines, and optional export.

## Lifecycle

1. Spawn `prompt-animator.exe serve --port 0` once with a hidden window.
2. Read the first JSON line from stdout. It contains `event: "ready"`, the selected port, a random launch secret, PID, and `contractVersion: 1`.
3. Call authenticated `GET /v1/health` and disable Visualize if the contract version is unsupported. Timeline playback remains available when the exporter is unhealthy.
4. Stop the sidecar when the desktop shell exits. MotionForge marks unfinished persisted jobs as interrupted on the next launch.

Do not put provider keys or the launch secret in URLs, command-line provider arguments, logs, or job metadata. Pass provider keys through the protected process environment or a future authenticated credential channel.

## Visualization flow

Create and poll a compile job:

```http
POST /v1/scenes/compile
Authorization: Bearer <launch-secret>
Content-Type: application/json

{
  "contractVersion": 1,
  "prompt": "Launch a projectile at 45 degrees",
  "preferTemplate": true,
  "provider": "ollama",
  "model": "llama3.1"
}
```

When the compile job is `completed`, start simulation by reference:

```json
{
  "contractVersion": 1,
  "compileJobId": "<compile-job-id>",
  "options": {
    "recommendedPlaybackFps": 30,
    "recordInspectables": true,
    "detectEvents": true
  }
}
```

The completed simulation job contains `result.timeline`. Store that timeline immediately and render it in Canvas. MP4 is never on the preview critical path.

Optional export uses the simulation job reference:

```json
{
  "contractVersion": 1,
  "simulationJobId": "<simulation-job-id>",
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

Use `GET /v1/jobs/:id/events` for SSE and send its `id` as `Last-Event-ID` when reconnecting. Polling is a compatible fallback. Cancel with `DELETE /v1/jobs/:id`; the simulator checks its token between steps and export cancellation terminates the worker process tree.

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

The current Velo backend still invokes the compatible legacy prompt-to-MP4 CLI. Its migration target is the lifecycle and job sequence above; no MotionForge UI is duplicated in this repository.
