"""
PhysicsSpec -> list[FrameState]

Headless Pymunk simulation. No rendering happens here -- just numeric
integration. Each FrameState records every object's (x, y, angle) at a
tick, plus the elapsed dt, so the timeline converter downstream can merge
this with visual info without touching physics at all.
"""

from __future__ import annotations

from typing import TypedDict

import pymunk

from schema.scene_spec import PhysicsObject, PhysicsSpec


class ObjectFrame(TypedDict):
    x: float
    y: float
    angle: float


class FrameState(TypedDict):
    t: float          # elapsed simulation time in seconds
    dt: float
    objects: dict[str, ObjectFrame]


def _make_shape(body: pymunk.Body, obj: PhysicsObject) -> pymunk.Shape:
    if obj.shape == "circle":
        shape = pymunk.Circle(body, obj.radius)
    elif obj.shape == "box":
        shape = pymunk.Poly.create_box(body, size=(obj.width, obj.height))
    elif obj.shape == "polygon":
        shape = pymunk.Poly(body, obj.vertices)
    elif obj.shape == "segment":
        shape = pymunk.Segment(body, obj.point_a, obj.point_b, obj.segment_radius)
    else:
        raise ValueError(f"Unsupported shape kind: {obj.shape}")

    shape.friction = obj.friction
    shape.elasticity = obj.restitution
    return shape


def _moment_for(obj: PhysicsObject) -> float:
    if obj.shape == "circle":
        return pymunk.moment_for_circle(obj.mass, 0, obj.radius)
    if obj.shape == "box":
        return pymunk.moment_for_box(obj.mass, (obj.width, obj.height))
    if obj.shape == "polygon":
        return pymunk.moment_for_poly(obj.mass, obj.vertices)
    # segments are always static in this schema, moment is irrelevant
    return float("inf")


def _add_object(space: pymunk.Space, obj: PhysicsObject) -> pymunk.Body:
    if obj.is_static or obj.shape == "segment":
        body = pymunk.Body(body_type=pymunk.Body.STATIC)
    else:
        moment = _moment_for(obj)
        body = pymunk.Body(obj.mass, moment)

    body.position = obj.position
    body.angle = obj.angle
    if not (obj.is_static or obj.shape == "segment"):
        body.velocity = obj.velocity
        body.angular_velocity = obj.angular_velocity

    shape = _make_shape(body, obj)
    space.add(body, shape)
    return body


def simulate(spec: PhysicsSpec) -> list[FrameState]:
    space = pymunk.Space()
    space.gravity = spec.gravity

    bodies: dict[str, pymunk.Body] = {}
    for obj in spec.objects:
        bodies[obj.id] = _add_object(space, obj)

    # Precompute which dynamic bodies each constant force applies to
    force_map: dict[str, list[tuple[float, float]]] = {}
    for force in spec.forces:
        for obj_id in force.applies_to:
            force_map.setdefault(obj_id, []).append(force.vector)

    n_steps = int(spec.duration / spec.dt)
    frames: list[FrameState] = []

    for step in range(n_steps):
        for obj_id, vectors in force_map.items():
            body = bodies.get(obj_id)
            if body is not None and body.body_type == pymunk.Body.DYNAMIC:
                for vx, vy in vectors:
                    body.apply_force_at_local_point((vx, vy), (0, 0))

        space.step(spec.dt)

        frame: FrameState = {
            "t": step * spec.dt,
            "dt": spec.dt,
            "objects": {
                obj_id: {"x": body.position.x, "y": body.position.y, "angle": body.angle}
                for obj_id, body in bodies.items()
            },
        }
        frames.append(frame)

    return frames
