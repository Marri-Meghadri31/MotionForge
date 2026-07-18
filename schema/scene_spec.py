"""
Schema definitions for the prompt -> physics animation pipeline.

Design notes:
- Objects are PRIMITIVES ONLY (circle, box, polygon, segment). No composite
  objects (car, ragdoll, etc). If the LLM is asked for something complex,
  it should approximate it using primitives.
- A SceneSpec has two top-level sections: `physics` (simulation truth) and
  `visual` (how it should look). These come from a single LLM call so they
  stay consistent with each other.
- `Storyboard` is a list[SceneSpec] so the pipeline is multi-scene-shaped
  from day one, even though Phase 1 only ever produces a storyboard of
  length 1. Scene stitching/concatenation is intentionally NOT implemented
  yet -- that's a later phase.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

ShapeKind = Literal["circle", "box", "polygon", "segment"]


class PhysicsObject(BaseModel):
    """A single primitive body in the simulation."""

    id: str = Field(..., description="Unique id, referenced by VisualSpec.object_styles")
    shape: ShapeKind

    # Shape-specific geometry (only the relevant fields need to be set)
    radius: Optional[float] = None                      # circle
    width: Optional[float] = None                        # box
    height: Optional[float] = None                        # box
    vertices: Optional[list[tuple[float, float]]] = None  # polygon (local coords)
    point_a: Optional[tuple[float, float]] = None          # segment
    point_b: Optional[tuple[float, float]] = None          # segment
    segment_radius: float = 2.0                            # segment "thickness"

    # Placement / motion
    position: tuple[float, float] = (0.0, 0.0)
    angle: float = 0.0                       # radians
    velocity: tuple[float, float] = (0.0, 0.0)
    angular_velocity: float = 0.0

    # Physical properties
    mass: float = 1.0
    is_static: bool = False                  # floors, ramps, walls
    friction: float = 0.5
    restitution: float = 0.5                 # bounciness, 0-1

    @model_validator(mode="after")
    def _check_shape_fields(self):
        if self.shape == "circle" and self.radius is None:
            raise ValueError(f"object '{self.id}': shape=circle requires radius")
        if self.shape == "box" and (self.width is None or self.height is None):
            raise ValueError(f"object '{self.id}': shape=box requires width and height")
        if self.shape == "polygon" and not self.vertices:
            raise ValueError(f"object '{self.id}': shape=polygon requires vertices")
        if self.shape == "segment" and (self.point_a is None or self.point_b is None):
            raise ValueError(f"object '{self.id}': shape=segment requires point_a and point_b")
        return self


class ConstantForce(BaseModel):
    """A simple constant force applied to one or more objects every tick."""

    applies_to: list[str] = Field(..., description="Object ids this force acts on")
    vector: tuple[float, float] = Field(..., description="Force vector (x, y)")


class PhysicsSpec(BaseModel):
    gravity: tuple[float, float] = (0.0, -981.0)  # px/s^2 downward by default
    duration: float = Field(..., gt=0, description="Simulation length in seconds")
    dt: float = Field(default=1 / 60, gt=0, description="Fixed timestep in seconds")
    objects: list[PhysicsObject]
    forces: list[ConstantForce] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_ids_unique(self):
        ids = [o.id for o in self.objects]
        if len(ids) != len(set(ids)):
            raise ValueError("object ids must be unique within a scene")
        return self


class ObjectStyle(BaseModel):
    color: str = "#378ADD"           # hex color, passed straight to Manim
    label: Optional[str] = None       # optional text label rendered near the object
    show_label: bool = False


class CameraSpec(BaseModel):
    """Static camera framing for Phase 1. Per-frame camera keyframes are a
    natural future extension (add `keyframes: list[CameraKeyframe]`)."""

    zoom: float = 1.0
    center: tuple[float, float] = (0.0, 0.0)


class VisualSpec(BaseModel):
    object_styles: dict[str, ObjectStyle] = Field(default_factory=dict)
    background_color: str = "#FFFFFF"
    title: Optional[str] = None
    camera: CameraSpec = Field(default_factory=CameraSpec)
    show_trails: bool = False


class SceneSpec(BaseModel):
    scene_id: str = "scene_1"
    description: Optional[str] = None
    physics: PhysicsSpec
    visual: VisualSpec

    @model_validator(mode="after")
    def _check_visual_ids_exist(self):
        physics_ids = {o.id for o in self.physics.objects}
        for style_id in self.visual.object_styles:
            if style_id not in physics_ids:
                raise ValueError(
                    f"visual.object_styles references unknown object id '{style_id}'"
                )
        return self


# Multi-scene container. Phase 1 always produces a list of length 1.
Storyboard = list[SceneSpec]
