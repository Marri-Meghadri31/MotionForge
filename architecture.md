If you're aiming to build an **AI-powered physics video generation platform** (not just a script), I would design it as a modular pipeline where **Pymunk is the simulation engine** and **Manim is the cinematic renderer**.

# High-Level Architecture

!scene_planner_pipeline.svg

```
                +----------------------+
                |     User Prompt      |
                | "Explain projectile  |
                | motion at 30°"       |
                +----------+-----------+
                           |
                           v
                +----------------------+
                |  LLM Scene Planner   |
                | Break into scenes    |
                | Generate storyboard  |
                +----------+-----------+
                           |
         +-----------------+------------------+
         |                                    |
         v                                    v
+--------------------+              +----------------------+
| Physics Generator  |              | Visual Generator     |
| Equations          |              | Labels               |
| Constraints        |              | Camera              |
| Initial Conditions |              | Colors              |
+----------+---------+              +----------+-----------+
           |                                   |
           v                                   |
     +-------------+                           |
     |   Pymunk    |                           |
     | Simulation  |                           |
     +------+------+                           |
            |                                  |
            | position, velocity, rotation     |
            +-------------------------+--------+
                                      |
                                      v
                          +----------------------+
                          | Timeline Converter   |
                          | Physics → Keyframes  |
                          +----------+-----------+
                                     |
                                     v
                            +-------------------+
                            | Manim Renderer    |
                            | Objects           |
                            | Graphs            |
                            | Vectors           |
                            | Camera            |
                            +---------+---------+
                                      |
                                      v
                             MP4 Scene Output
                                      |
                                      v
                      +-----------------------------+
                      | Narration + Audio + FFmpeg |
                      +-----------------------------+
                                      |
                                      v
                              Final Video
```

---

# Layer 1 — Scene Planning

The LLM never writes one huge Manim file.

Instead, it creates a storyboard.

Example:

```yaml
Scene 1:
  Title: Introduction

Scene 2:
  Show cannon

Scene 3:
  Launch projectile

Scene 4:
  Draw velocity vector

Scene 5:
  Plot trajectory

Scene 6:
  Show equations

Scene 7:
  Conclusion
```

Each scene is independent.

---

# Layer 2 — Physics Engine

Pymunk should only calculate physics.

Example state:

```python
state = {
    "time":0.3,
    "x":3.45,
    "y":1.92,
    "vx":7.8,
    "vy":4.2,
    "omega":0.1,
    "force":[0,-9.81]
}
```

Every frame:

```
t=0

↓

simulate

↓

t=0.016

↓

simulate

↓

...

↓

t=10s
```

Produces

```
trajectory.json
```

Example

```json
[
 {
   "t":0.0,
   "x":0,
   "y":0
 },
 {
   "t":0.016,
   "x":0.1,
   "y":0.08
 }
]
```

No rendering occurs here.

---

# Layer 3 — Timeline Converter

This layer converts simulation output into animation-friendly data.

Instead of

```
Physics State
```

Manim receives

```python
frame = {

ball_position,

velocity_arrow,

acceleration_arrow,

trail,

camera_position,

labels

}
```

Everything is synchronized.

Derived geometry is also resolved here, never inside a renderer. A declarative
point reference can target a physics object's centre, edge, top/bottom anchor,
segment endpoint, literal world point, or the start/end of an earlier overlay.
The converter samples line projections, ground intersections, and measurements
against every Pymunk frame and stores them as overlay tracks. Manim and any
future renderer therefore replay identical geometry and numeric results.

---

# Layer 4 — Manim Renderer

Now Manim focuses purely on visuals.

Example responsibilities:

- draw object
- move object
- draw vectors
- update graphs
- fade equations
- zoom camera
- animate labels
- highlight forces

No physics calculations.

---

# Folder Structure

```
physics-ai/

├── prompts/
│
├── planner/
│   planner.py
│
├── physics/
│   projectile.py
│   pendulum.py
│   collision.py
│   springs.py
│
├── simulations/
│   projectile.json
│
├── renderer/
│   manim_renderer.py
│
├── scenes/
│   intro.py
│   projectile_scene.py
│
├── assets/
│
├── narration/
│
├── export/
│
└── main.py
```

---

# Data Flow

```
Prompt

↓

LLM

↓

Scene JSON

↓

Physics JSON

↓

Animation JSON

↓

Manim

↓

MP4
```

Notice that every stage communicates through structured JSON rather than direct function calls, making each component replaceable and easier to debug.

---

# Scene Object

```json
{
  "scene_id":3,
  "duration":8,
  "physics":"projectile",
  "camera":"follow",
  "objects":[
      "ball",
      "ground"
  ]
}
```

---

# Physics Output

```json
{
    "frame":1,
    "time":0.016,
    "ball":{
        "x":2.3,
        "y":1.2,
        "rotation":12
    }
}
```

---

# Animation Layer

This enriches the simulation with educational overlays:

```json
{
  "vector":"velocity",
  "length":3.4,

  "equation":"v = u + at",

  "highlight":"green",

  "camera_zoom":1.3
}
```

---

# Why Separate Physics and Rendering?

Keeping Pymunk and Manim independent has several advantages:

- You can swap Pymunk for another physics engine (e.g., Box2D or Taichi) without changing the renderer.
- You can replay the same simulation with different visual styles.
- Physics can be tested independently of animations.
- Rendering bugs never affect simulation accuracy.
- Multiple renderers (Manim, Blender, web-based) can reuse the same simulation data.

---

# Extending Beyond Basic Mechanics

As the platform grows, you can plug in additional simulation engines based on the topic:

| Topic | Simulation Engine | Renderer |
| --- | --- | --- |
| Projectile motion | Pymunk | Manim |
| Pendulums & springs | Pymunk | Manim |
| Rigid body collisions | Pymunk | Manim |
| Fluid dynamics | Taichi | Manim |
| Electromagnetism | Custom numerical solver | Manim |
| Planetary motion | SciPy / N-body solver | Manim |
| Cloth & soft bodies | Taichi | Blender or Manim overlays |

---

# Long-Term Vision

Think of the platform as a compiler:

```
Natural Language
        │
        ▼
Educational Storyboard
        │
        ▼
Physics Specification
        │
        ▼
Simulation Engine
        │
        ▼
Animation Timeline
        │
        ▼
Manim Renderer
        │
        ▼
Narration + Audio
        │
        ▼
YouTube-Quality Educational Video
```

This separation of concerns makes the system scalable, testable, and capable of supporting many branches of physics without redesigning the entire pipeline. It also enables AI agents to work on different stages (storyboarding, simulation, rendering, narration) independently while producing a coherent final video.
