"""Fast deterministic SceneSpec templates for common teaching scenarios."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

from motionforge.models import (
    CameraSpec,
    CompilerMetadata,
    ConstraintSpec,
    ConstantForce,
    ObjectStyle,
    OverlaySpec,
    ParameterSpec,
    PhysicsObject,
    PhysicsSpec,
    SceneSpec,
    TrailSpec,
    VisualSpec,
)

Template = Callable[[str, dict[str, Any]], SceneSpec]


def _number(parameters: dict[str, Any], name: str, default: float, minimum: float, maximum: float) -> float:
    value = parameters.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"parameter '{name}' must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"parameter '{name}' must be a number") from error
    if not minimum <= result <= maximum:
        raise ValueError(f"parameter '{name}' must be between {minimum:g} and {maximum:g}")
    return result


def _scene(
    template_id: str,
    prompt: str,
    physics: PhysicsSpec,
    styles: dict[str, ObjectStyle],
    *,
    title: str,
    camera: tuple[float, float] = (0, 150),
    trail: bool = False,
    overlays: list[OverlaySpec] | None = None,
    parameters: list[ParameterSpec] | None = None,
) -> SceneSpec:
    return SceneSpec(
        scene_id=template_id,
        description=prompt[:1_000],
        physics=physics,
        visual=VisualSpec(
            object_styles=styles,
            title=title,
            camera=CameraSpec(center=camera),
            trail=TrailSpec(enabled=trail, max_points=300, sample_every=2),
            overlays=overlays or [],
        ),
        parameters=parameters or [],
        metadata=CompilerMetadata(origin="template", template_id=template_id),
    )


def falling_body(prompt: str, values: dict[str, Any]) -> SceneSpec:
    height = _number(values, "height", 300, 50, 800)
    restitution = _number(values, "restitution", 0.72, 0, 1)
    duration = _number(values, "duration", 3, 0.25, 10)
    radius = _number(values, "radius", 20, 4, 80)
    return _scene(
        "falling-body",
        prompt,
        PhysicsSpec(
            duration=duration,
            objects=[
                PhysicsObject(id="ground", shape="segment", point_a=(-400, 0), point_b=(400, 0), is_static=True, restitution=restitution, friction=0.7),
                PhysicsObject(id="ball", shape="circle", radius=radius, position=(0, height), restitution=restitution, friction=0.45),
            ],
        ),
        {"ground": ObjectStyle(color="#5F5E5A"), "ball": ObjectStyle(color="#D85A30", label="ball", show_label=True)},
        title="Falling and bouncing body",
        trail=True,
        overlays=[OverlaySpec(id="velocity", kind="vector", target_id="ball", label="velocity", color="#378ADD", data={"source": "velocity", "scale": 0.2})],
        parameters=[
            ParameterSpec(id="height", path="physics.objects[ball].position.y", default=height, minimum=50, maximum=800, unit="px"),
            ParameterSpec(id="restitution", path="physics.objects[ball].restitution", default=restitution, minimum=0, maximum=1),
        ],
    )


def projectile(prompt: str, values: dict[str, Any]) -> SceneSpec:
    speed = _number(values, "speed", 260, 20, 1_000)
    angle_degrees = _number(values, "angle", 45, 1, 89)
    duration = _number(values, "duration", 3, 0.25, 10)
    radians = math.radians(angle_degrees)
    velocity = (speed * math.cos(radians), speed * math.sin(radians))
    return _scene(
        "projectile-motion",
        prompt,
        PhysicsSpec(
            duration=duration,
            objects=[
                PhysicsObject(id="ground", shape="segment", point_a=(-400, 0), point_b=(400, 0), is_static=True, restitution=0.45),
                PhysicsObject(id="projectile", shape="circle", radius=12, position=(-280, 20), velocity=velocity, restitution=0.45, friction=0.4),
            ],
        ),
        {"ground": ObjectStyle(color="#5F5E5A"), "projectile": ObjectStyle(color="#D85A30", label="projectile", show_label=True)},
        title="Projectile motion",
        camera=(0, 120),
        trail=True,
        overlays=[
            OverlaySpec(id="velocity", kind="vector", target_id="projectile", label="v", color="#378ADD", data={"source": "velocity", "scale": 0.25}),
            OverlaySpec(id="acceleration", kind="vector", target_id="projectile", label="g", color="#639922", data={"source": "acceleration", "scale": 0.12}),
            OverlaySpec(id="equation", kind="equation", label="y = y₀ + v₀t − ½gt²", color="#7F77DD"),
        ],
        parameters=[
            ParameterSpec(id="speed", path="physics.objects[projectile].velocity.magnitude", default=speed, minimum=20, maximum=1_000, unit="px/s"),
            ParameterSpec(id="angle", path="physics.objects[projectile].velocity.angle", default=angle_degrees, minimum=1, maximum=89, unit="degrees"),
        ],
    )


def ramp(prompt: str, values: dict[str, Any]) -> SceneSpec:
    friction = _number(values, "friction", 0.35, 0, 2)
    angle_degrees = _number(values, "angle", 24, 5, 60)
    length = 450.0
    radians = math.radians(angle_degrees)
    start = (-250.0, 40.0)
    end = (start[0] + length * math.cos(radians), start[1] + length * math.sin(radians))
    return _scene(
        "ramp-friction",
        prompt,
        PhysicsSpec(
            duration=_number(values, "duration", 4, 0.25, 10),
            objects=[
                PhysicsObject(id="ground", shape="segment", point_a=(-400, 0), point_b=(400, 0), is_static=True, friction=friction),
                PhysicsObject(id="ramp", shape="segment", point_a=start, point_b=end, is_static=True, friction=friction),
                PhysicsObject(id="block", shape="box", width=42, height=28, position=(end[0] - 30, end[1] + 28), angle=radians, friction=friction, restitution=0.05),
            ],
        ),
        {"ground": ObjectStyle(color="#5F5E5A"), "ramp": ObjectStyle(color="#888780"), "block": ObjectStyle(color="#378ADD", label="block", show_label=True)},
        title="Ramp and friction",
        camera=(0, 130),
        overlays=[
            OverlaySpec(id="gravity", kind="vector", target_id="block", label="weight", color="#D85A30", data={"source": "gravity"}),
            OverlaySpec(id="friction", kind="vector", target_id="block", label="friction", color="#639922", data={"source": "friction"}),
        ],
        parameters=[ParameterSpec(id="friction", path="physics.objects[*].friction", default=friction, minimum=0, maximum=2)],
    )


def pendulum(prompt: str, values: dict[str, Any]) -> SceneSpec:
    length = _number(values, "length", 180, 40, 350)
    angle_degrees = _number(values, "angle", 35, 2, 80)
    anchor = (0.0, 220.0)
    radians = math.radians(angle_degrees)
    bob = (anchor[0] + length * math.sin(radians), anchor[1] - length * math.cos(radians))
    return _scene(
        "pendulum",
        prompt,
        PhysicsSpec(
            duration=_number(values, "duration", 5, 0.25, 12),
            objects=[
                PhysicsObject(id="pivot", shape="circle", radius=6, position=anchor, is_static=True, inspectable=False),
                PhysicsObject(id="bob", shape="circle", radius=20, position=bob, friction=0.2, restitution=0.1),
            ],
            constraints=[ConstraintSpec(id="rod", type="pin", object_a="pivot", object_b="bob", distance=length)],
        ),
        {"pivot": ObjectStyle(color="#5F5E5A"), "bob": ObjectStyle(color="#D85A30", label="bob", show_label=True)},
        title="Simple pendulum",
        camera=(0, 100),
        trail=True,
        overlays=[OverlaySpec(id="rod-path", kind="path", target_id="bob", color="#888780", data={"constraint": "rod"})],
        parameters=[
            ParameterSpec(id="length", path="physics.constraints[rod].distance", default=length, minimum=40, maximum=350, unit="px"),
            ParameterSpec(id="angle", path="physics.objects[bob].position", default=angle_degrees, minimum=2, maximum=80, unit="degrees"),
        ],
    )


def collision(prompt: str, values: dict[str, Any]) -> SceneSpec:
    speed = _number(values, "speed", 180, 10, 600)
    restitution = _number(values, "restitution", 0.9, 0, 1)
    return _scene(
        "collision-momentum",
        prompt,
        PhysicsSpec(
            gravity=(0, 0),
            duration=_number(values, "duration", 3, 0.25, 8),
            objects=[
                PhysicsObject(id="left", shape="circle", radius=24, position=(-180, 0), velocity=(speed, 0), mass=1, restitution=restitution, friction=0),
                PhysicsObject(id="right", shape="circle", radius=30, position=(130, 0), velocity=(-speed * 0.4, 0), mass=2, restitution=restitution, friction=0),
            ],
        ),
        {"left": ObjectStyle(color="#378ADD", label="m₁", show_label=True), "right": ObjectStyle(color="#D85A30", label="m₂", show_label=True)},
        title="Collision and momentum",
        camera=(0, 0),
        trail=True,
        overlays=[OverlaySpec(id="momentum", kind="vector", target_id="left", label="p = mv", color="#639922", data={"source": "momentum"})],
        parameters=[ParameterSpec(id="restitution", path="physics.objects[*].restitution", default=restitution, minimum=0, maximum=1)],
    )


def circular_motion(prompt: str, values: dict[str, Any]) -> SceneSpec:
    radius = _number(values, "radius", 150, 50, 350)
    speed = _number(values, "speed", 180, 20, 600)
    return _scene(
        "circular-motion",
        prompt,
        PhysicsSpec(
            gravity=(0, 0),
            duration=_number(values, "duration", 5, 0.25, 12),
            objects=[
                PhysicsObject(id="centre", shape="circle", radius=10, position=(0, 0), is_static=True, inspectable=False),
                PhysicsObject(id="orbiter", shape="circle", radius=18, position=(radius, 0), velocity=(0, speed), friction=0, restitution=0),
            ],
            constraints=[ConstraintSpec(id="radius", type="pin", object_a="centre", object_b="orbiter", distance=radius)],
        ),
        {"centre": ObjectStyle(color="#5F5E5A", label="centre", show_label=True), "orbiter": ObjectStyle(color="#378ADD", label="object", show_label=True)},
        title="Circular motion",
        camera=(0, 0),
        trail=True,
        overlays=[
            OverlaySpec(id="velocity", kind="vector", target_id="orbiter", label="velocity", color="#378ADD", data={"source": "velocity"}),
            OverlaySpec(id="centripetal", kind="vector", target_id="orbiter", label="centripetal", color="#D85A30", data={"source": "constraint"}),
        ],
        parameters=[ParameterSpec(id="radius", path="physics.constraints[radius].distance", default=radius, minimum=50, maximum=350, unit="px")],
    )


def force_diagram(prompt: str, values: dict[str, Any]) -> SceneSpec:
    applied = _number(values, "force", 300, -5_000, 5_000)
    return _scene(
        "force-diagram",
        prompt,
        PhysicsSpec(
            duration=_number(values, "duration", 2.5, 0.25, 8),
            objects=[
                PhysicsObject(id="ground", shape="segment", point_a=(-400, 0), point_b=(400, 0), is_static=True, friction=0.7),
                PhysicsObject(id="block", shape="box", width=70, height=50, position=(-160, 28), mass=2, friction=0.7),
            ],
            forces=[ConstantForce(applies_to=["block"], vector=(applied, 0))],
        ),
        {"ground": ObjectStyle(color="#5F5E5A"), "block": ObjectStyle(color="#378ADD", label="block", show_label=True)},
        title="Force vector diagram",
        camera=(0, 90),
        overlays=[
            OverlaySpec(id="weight", kind="vector", target_id="block", label="weight", color="#D85A30", data={"source": "gravity"}),
            OverlaySpec(id="normal", kind="vector", target_id="block", label="normal", color="#639922", data={"source": "normal"}),
            OverlaySpec(id="applied", kind="vector", target_id="block", label="applied force", color="#378ADD", data={"source": "force"}),
        ],
        parameters=[ParameterSpec(id="force", path="physics.forces[0].vector.x", default=applied, minimum=-5_000, maximum=5_000, unit="N")],
    )


def spring(prompt: str, values: dict[str, Any]) -> SceneSpec:
    stiffness = _number(values, "stiffness", 90, 5, 2_000)
    rest_length = _number(values, "length", 180, 40, 350)
    displacement = _number(values, "displacement", 80, -150, 150)
    return _scene(
        "spring-shm",
        prompt,
        PhysicsSpec(
            gravity=(0, 0),
            duration=_number(values, "duration", 6, 0.25, 12),
            objects=[
                PhysicsObject(id="anchor", shape="circle", radius=7, position=(-220, 0), is_static=True, inspectable=False),
                PhysicsObject(id="mass", shape="box", width=48, height=48, position=(-220 + rest_length + displacement, 0), mass=2, friction=0),
            ],
            constraints=[ConstraintSpec(id="spring", type="dampedSpring", object_a="anchor", object_b="mass", rest_length=rest_length, stiffness=stiffness, damping=2.5)],
        ),
        {"anchor": ObjectStyle(color="#5F5E5A"), "mass": ObjectStyle(color="#7F77DD", label="mass", show_label=True)},
        title="Spring and simple harmonic motion",
        camera=(0, 0),
        trail=True,
        overlays=[OverlaySpec(id="spring-line", kind="path", target_id="mass", color="#5F5E5A", data={"constraint": "spring"})],
        parameters=[
            ParameterSpec(id="stiffness", path="physics.constraints[spring].stiffness", default=stiffness, minimum=5, maximum=2_000, unit="N/m"),
            ParameterSpec(id="displacement", path="physics.objects[mass].position.x", default=displacement, minimum=-150, maximum=150, unit="px"),
        ],
    )


def motion_graph(prompt: str, values: dict[str, Any]) -> SceneSpec:
    velocity = _number(values, "velocity", 90, -500, 500)
    acceleration = _number(values, "acceleration", 35, -500, 500)
    mass = _number(values, "mass", 1, 0.1, 100)
    return _scene(
        "motion-graphs",
        prompt,
        PhysicsSpec(
            gravity=(0, 0),
            duration=_number(values, "duration", 5, 0.25, 12),
            objects=[PhysicsObject(id="marker", shape="circle", radius=12, position=(-260, 80), velocity=(velocity, 0), mass=mass)],
            forces=[ConstantForce(applies_to=["marker"], vector=(mass * acceleration, 0))],
        ),
        {"marker": ObjectStyle(color="#378ADD", label="object", show_label=True)},
        title="Position, velocity, and acceleration",
        camera=(0, 80),
        trail=True,
        overlays=[OverlaySpec(id="graphs", kind="graph", target_id="marker", label="x(t), v(t), a(t)", color="#7F77DD", data={"series": ["x", "vx", "ax"]})],
        parameters=[
            ParameterSpec(id="velocity", path="physics.objects[marker].velocity.x", default=velocity, minimum=-500, maximum=500, unit="px/s"),
            ParameterSpec(id="acceleration", path="physics.forces[0].vector.x", default=acceleration, minimum=-500, maximum=500, unit="px/s²"),
        ],
    )


TEMPLATES: dict[str, Template] = {
    "falling-body": falling_body,
    "projectile-motion": projectile,
    "ramp-friction": ramp,
    "pendulum": pendulum,
    "collision-momentum": collision,
    "circular-motion": circular_motion,
    "force-diagram": force_diagram,
    "spring-shm": spring,
    "motion-graphs": motion_graph,
}

CLASSIFIERS: list[tuple[str, re.Pattern[str]]] = [
    ("motion-graphs", re.compile(r"\b(position|velocity|acceleration)\b.*\b(graph|plot)\b|\b(graph|plot)\b.*\b(motion|position|velocity|acceleration)\b", re.I)),
    ("spring-shm", re.compile(r"\b(spring|simple harmonic|shm|oscillat)\w*\b", re.I)),
    ("pendulum", re.compile(r"\bpendulum\b", re.I)),
    ("collision-momentum", re.compile(r"\b(collid|collision|momentum|impact)\w*\b", re.I)),
    ("circular-motion", re.compile(r"\b(circular|centripetal|orbit|orbital)\w*\b", re.I)),
    ("ramp-friction", re.compile(r"\b(ramp|incline|friction|sliding)\w*\b", re.I)),
    ("projectile-motion", re.compile(r"\b(projectile|trajectory|launch|thrown?|cannon)\w*\b", re.I)),
    ("falling-body", re.compile(r"\b(fall|falling|drop|dropped|bounce|bouncing|gravity)\w*\b", re.I)),
    ("force-diagram", re.compile(r"\b(force|free.body|vector diagram|newton)\w*\b", re.I)),
]


def classify_template(prompt: str) -> str | None:
    for template_id, pattern in CLASSIFIERS:
        if pattern.search(prompt):
            return template_id
    return None


def compile_template(template_id: str, prompt: str, parameters: dict[str, Any]) -> SceneSpec:
    try:
        template = TEMPLATES[template_id]
    except KeyError as error:
        raise ValueError(f"unknown template '{template_id}'") from error
    return template(prompt, parameters)
