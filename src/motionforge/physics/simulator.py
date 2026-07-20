from __future__ import annotations

import math
import time
from dataclasses import dataclass
from threading import Event
from typing import TypedDict

import pymunk

from motionforge.errors import ErrorCode, MotionForgeError
from motionforge.models import PhysicsObject, PhysicsSpec, SimulationOptions, TimelineEvent


class ObjectFrame(TypedDict):
    x: float
    y: float
    angle: float
    vx: float
    vy: float
    angular_velocity: float
    ax: float
    ay: float
    force_x: float
    force_y: float
    kinetic_energy: float
    potential_energy: float
    momentum_x: float
    momentum_y: float


class FrameState(TypedDict):
    t: float
    objects: dict[str, ObjectFrame]


@dataclass(slots=True)
class SimulationResult:
    frames: list[FrameState]
    events: list[TimelineEvent]
    elapsed_seconds: float


def _make_shape(body: pymunk.Body, obj: PhysicsObject) -> pymunk.Shape:
    if obj.shape == "circle":
        shape = pymunk.Circle(body, obj.radius or 1)
    elif obj.shape == "box":
        shape = pymunk.Poly.create_box(body, size=(obj.width or 1, obj.height or 1))
    elif obj.shape == "polygon":
        shape = pymunk.Poly(body, obj.vertices or [])
    elif obj.shape == "segment":
        shape = pymunk.Segment(body, obj.point_a or (0, 0), obj.point_b or (1, 0), obj.segment_radius)
    else:  # pragma: no cover - protected by the schema
        raise ValueError(f"unsupported shape '{obj.shape}'")
    shape.friction = obj.friction
    # Chipmunk multiplies the two colliding shapes' elasticities. Storing the
    # square root makes equal SceneSpec restitution values combine to the
    # coefficient learners expect (e.g. 0.8, rather than 0.8²).
    shape.elasticity = math.sqrt(obj.restitution)
    if obj.collision_group:
        shape.filter = pymunk.ShapeFilter(group=obj.collision_group)
    return shape


def _moment_for(obj: PhysicsObject) -> float:
    if obj.shape == "circle":
        return pymunk.moment_for_circle(obj.mass, 0, obj.radius or 1)
    if obj.shape == "box":
        return pymunk.moment_for_box(obj.mass, (obj.width or 1, obj.height or 1))
    if obj.shape == "polygon":
        return pymunk.moment_for_poly(obj.mass, obj.vertices or [])
    return float("inf")


def _add_object(space: pymunk.Space, obj: PhysicsObject) -> tuple[pymunk.Body, pymunk.Shape]:
    if obj.is_static:
        body = pymunk.Body(body_type=pymunk.Body.STATIC)
    else:
        body = pymunk.Body(obj.mass, _moment_for(obj))
    body.position = obj.position
    body.angle = obj.angle
    if not obj.is_static:
        body.velocity = obj.velocity
        body.angular_velocity = obj.angular_velocity
    shape = _make_shape(body, obj)
    space.add(body, shape)
    return body, shape


def _add_constraints(space: pymunk.Space, spec: PhysicsSpec, bodies: dict[str, pymunk.Body]) -> None:
    for item in spec.constraints:
        body_a = bodies[item.object_a]
        body_b = bodies[item.object_b]
        if item.type == "pin":
            constraint = pymunk.PinJoint(body_a, body_b, item.anchor_a, item.anchor_b)
            if item.distance is not None:
                constraint.distance = item.distance
        else:
            constraint = pymunk.DampedSpring(
                body_a,
                body_b,
                item.anchor_a,
                item.anchor_b,
                item.rest_length or 1,
                item.stiffness or 1,
                item.damping or 0,
            )
        space.add(constraint)


def _external_forces(
    spec: PhysicsSpec,
    objects: dict[str, PhysicsObject],
    field_forces: dict[str, tuple[float, float]] | None = None,
) -> dict[str, tuple[float, float]]:
    result: dict[str, tuple[float, float]] = {}
    for obj_id, obj in objects.items():
        if obj.is_static:
            result[obj_id] = (0.0, 0.0)
        else:
            result[obj_id] = (obj.mass * spec.gravity[0], obj.mass * spec.gravity[1])
    for force in spec.forces:
        for obj_id in force.applies_to:
            old_x, old_y = result[obj_id]
            result[obj_id] = (old_x + force.vector[0], old_y + force.vector[1])
    for obj_id, (field_x, field_y) in (field_forces or {}).items():
        old_x, old_y = result[obj_id]
        result[obj_id] = (old_x + field_x, old_y + field_y)
    return result


def _force_field_forces(
    spec: PhysicsSpec,
    objects: dict[str, PhysicsObject],
    bodies: dict[str, pymunk.Body],
) -> dict[str, tuple[float, float]]:
    """Evaluate position-dependent fields without coupling them to rendering."""

    result = {obj_id: (0.0, 0.0) for obj_id in objects}
    for field in spec.force_fields:
        seen_mutual_pairs: set[tuple[str, str]] = set()
        for source_id in dict.fromkeys(field.sources):
            for target_id in dict.fromkeys(field.targets):
                if source_id == target_id:
                    continue
                if field.mutual:
                    pair = tuple(sorted((source_id, target_id)))
                    if pair in seen_mutual_pairs:
                        continue
                    seen_mutual_pairs.add(pair)
                source_body = bodies[source_id]
                target_body = bodies[target_id]
                delta_x = source_body.position.x - target_body.position.x
                delta_y = source_body.position.y - target_body.position.y
                distance_squared = delta_x * delta_x + delta_y * delta_y
                if distance_squared < 1e-18:
                    continue
                softened_squared = distance_squared + field.softening * field.softening
                magnitude = (
                    field.strength
                    * objects[source_id].mass
                    * objects[target_id].mass
                    / softened_squared
                )
                magnitude = min(field.max_force, magnitude)
                if field.direction == "repel":
                    magnitude = -magnitude
                inverse_distance = 1 / math.sqrt(distance_squared)
                force_x = magnitude * delta_x * inverse_distance
                force_y = magnitude * delta_y * inverse_distance
                if not objects[target_id].is_static:
                    old_x, old_y = result[target_id]
                    result[target_id] = (old_x + force_x, old_y + force_y)
                if field.mutual and not objects[source_id].is_static:
                    old_x, old_y = result[source_id]
                    result[source_id] = (old_x - force_x, old_y - force_y)
    return result


def _frame(
    timestamp: float,
    spec: PhysicsSpec,
    definitions: dict[str, PhysicsObject],
    bodies: dict[str, pymunk.Body],
    external_forces: dict[str, tuple[float, float]],
    previous_velocities: dict[str, tuple[float, float]] | None = None,
    step_dt: float | None = None,
) -> FrameState:
    objects: dict[str, ObjectFrame] = {}
    gx, gy = spec.gravity
    for obj_id, body in bodies.items():
        definition = definitions[obj_id]
        if previous_velocities is not None and step_dt:
            prior_vx, prior_vy = previous_velocities[obj_id]
            ax = (body.velocity.x - prior_vx) / step_dt
            ay = (body.velocity.y - prior_vy) / step_dt
        elif definition.is_static:
            ax = ay = 0.0
        else:
            force_x, force_y = external_forces[obj_id]
            ax, ay = force_x / definition.mass, force_y / definition.mass
        speed_squared = body.velocity.x**2 + body.velocity.y**2
        kinetic = 0.0 if definition.is_static else 0.5 * definition.mass * speed_squared + 0.5 * body.moment * body.angular_velocity**2
        potential = 0.0 if definition.is_static else -definition.mass * (gx * body.position.x + gy * body.position.y)
        force_x, force_y = external_forces[obj_id]
        objects[obj_id] = {
            "x": float(body.position.x),
            "y": float(body.position.y),
            "angle": float(body.angle),
            "vx": float(body.velocity.x),
            "vy": float(body.velocity.y),
            "angular_velocity": float(body.angular_velocity),
            "ax": float(ax),
            "ay": float(ay),
            "force_x": float(force_x),
            "force_y": float(force_y),
            "kinetic_energy": float(kinetic),
            "potential_energy": float(potential),
            "momentum_x": float(0 if definition.is_static else definition.mass * body.velocity.x),
            "momentum_y": float(0 if definition.is_static else definition.mass * body.velocity.y),
        }
    return {"t": float(timestamp), "objects": objects}


def simulate(
    spec: PhysicsSpec,
    options: SimulationOptions | None = None,
    *,
    cancel_event: Event | None = None,
    progress: callable | None = None,
) -> SimulationResult:
    """Simulate at a fixed physics step, including exact t=0 and duration samples.

    When duration is not divisible by dt, every full dt step is retained and one
    final short Pymunk step lands exactly on the requested duration.
    """

    settings = options or SimulationOptions()
    cancelled = cancel_event or Event()
    started = time.perf_counter()
    deadline = started + settings.timeout_seconds
    space = pymunk.Space(threaded=False)
    space.gravity = spec.gravity
    space.sleep_time_threshold = 0.5
    space.collision_slop = 0.1

    definitions = {obj.id: obj for obj in spec.objects}
    bodies: dict[str, pymunk.Body] = {}
    shape_ids: dict[pymunk.Shape, str] = {}
    for obj in spec.objects:
        body, shape = _add_object(space, obj)
        bodies[obj.id] = body
        shape_ids[shape] = obj.id
    _add_constraints(space, spec, bodies)
    field_forces = _force_field_forces(spec, definitions, bodies)
    forces = _external_forces(spec, definitions, field_forces)

    events: list[TimelineEvent] = []
    current_time = [0.0]
    collision_index = [0]

    def collision_begin(arbiter: pymunk.Arbiter, _space: pymunk.Space, _data: dict) -> bool:
        ids = sorted(shape_ids.get(shape, "unknown") for shape in arbiter.shapes)
        collision_index[0] += 1
        events.append(
            TimelineEvent(
                id=f"collision-{collision_index[0]}",
                type="collision",
                time=current_time[0],
                object_ids=ids,
            )
        )
        return True

    if settings.detect_events:
        if hasattr(space, "on_collision"):
            space.on_collision(None, None, begin=collision_begin)
        else:  # pragma: no cover - compatibility with older Pymunk
            handler = space.add_default_collision_handler()
            handler.begin = collision_begin

    frames: list[FrameState] = [_frame(0.0, spec, definitions, bodies, forces)]
    rest_counts = {obj.id: 0 for obj in spec.objects if not obj.is_static}
    rest_recorded: set[str] = set()
    t = 0.0
    step_number = 0
    try:
        while t < spec.duration - 1e-12:
            if cancelled.is_set():
                raise MotionForgeError(ErrorCode.CANCELLED, "Simulation was cancelled.")
            if time.perf_counter() > deadline:
                raise MotionForgeError(ErrorCode.TIMEOUT, "Simulation exceeded its time limit.")
            step_dt = min(spec.dt, spec.duration - t)
            previous = frames[-1]
            prior_velocities = {
                obj_id: (body.velocity.x, body.velocity.y) for obj_id, body in bodies.items()
            }
            for force in spec.forces:
                for obj_id in force.applies_to:
                    bodies[obj_id].apply_force_at_world_point(force.vector, bodies[obj_id].position)
            field_forces = _force_field_forces(spec, definitions, bodies)
            for obj_id, vector in field_forces.items():
                if not definitions[obj_id].is_static:
                    bodies[obj_id].apply_force_at_world_point(vector, bodies[obj_id].position)
            next_t = spec.duration if spec.duration - (t + step_dt) < 1e-12 else t + step_dt
            current_time[0] = float(next_t)
            space.step(step_dt)
            t = next_t
            reported_forces = _external_forces(
                spec,
                definitions,
                _force_field_forces(spec, definitions, bodies),
            )
            frame = _frame(t, spec, definitions, bodies, reported_forces, prior_velocities, step_dt)
            frames.append(frame)
            step_number += 1
            if settings.detect_events:
                _detect_frame_events(previous, frame, definitions, events, rest_counts, rest_recorded)
            if progress and (step_number % 10 == 0 or t >= spec.duration):
                progress(min(1.0, t / spec.duration))
    except MotionForgeError:
        raise
    except Exception as error:
        raise MotionForgeError(ErrorCode.SIMULATION_FAILED, "Physics simulation failed.", details=str(error)) from error
    return SimulationResult(frames=frames, events=events, elapsed_seconds=time.perf_counter() - started)


def _detect_frame_events(
    previous: FrameState,
    current: FrameState,
    definitions: dict[str, PhysicsObject],
    events: list[TimelineEvent],
    rest_counts: dict[str, int],
    rest_recorded: set[str],
) -> None:
    for obj_id, definition in definitions.items():
        if definition.is_static:
            continue
        before = previous["objects"][obj_id]
        after = current["objects"][obj_id]
        if before["vy"] > 1.0 >= after["vy"]:
            denominator = before["vy"] - after["vy"]
            fraction = before["vy"] / denominator if denominator else 1.0
            event_time = previous["t"] + fraction * (current["t"] - previous["t"])
            events.append(TimelineEvent(id=f"apex-{obj_id}-{len(events)}", type="apex", time=event_time, object_ids=[obj_id]))
        if before["y"] * after["y"] < 0:
            events.append(TimelineEvent(id=f"crossing-{obj_id}-{len(events)}", type="crossing", time=current["t"], object_ids=[obj_id], data={"axis": "y"}))
        speed = math.hypot(after["vx"], after["vy"])
        if speed < 0.5 and abs(after["angular_velocity"]) < 0.05:
            rest_counts[obj_id] += 1
            if rest_counts[obj_id] >= 10 and obj_id not in rest_recorded:
                rest_recorded.add(obj_id)
                events.append(TimelineEvent(id=f"rest-{obj_id}", type="rest", time=current["t"], object_ids=[obj_id]))
        else:
            rest_counts[obj_id] = 0
