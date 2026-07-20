"""Versioned contracts for compilation, simulation, playback, export, and jobs."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from motionforge.constants import (
    CONTRACT_VERSION,
    MAX_ANGULAR_VELOCITY,
    MAX_COORDINATE,
    MAX_DURATION_SECONDS,
    MAX_EXPORT_FPS,
    MAX_EXPORT_HEIGHT,
    MAX_EXPORT_WIDTH,
    MAX_FORCE_MAGNITUDE,
    MAX_FORCES,
    MAX_LABEL_LENGTH,
    MAX_MASS,
    MAX_OBJECTS,
    MAX_POLYGON_VERTICES,
    MAX_PROMPT_LENGTH,
    MAX_TIMESTEP_SECONDS,
    MAX_TITLE_LENGTH,
    MAX_TRAIL_POINTS,
    MAX_VELOCITY,
    MIN_TIMESTEP_SECONDS,
    SCHEMA_VERSION,
    TIMELINE_VERSION,
)

Color = str
Point = tuple[float, float]
ShapeKind = Literal["circle", "box", "polygon", "segment"]
OverlayKind = Literal["vector", "path", "equation", "graph", "eventMarker", "highlight"]


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        extra="forbid",
        allow_inf_nan=False,
        validate_assignment=True,
    )

    def contract_dump(self, **kwargs: Any) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True, **kwargs)


def _point_within_limit(point: Point, field_name: str) -> Point:
    if any(abs(value) > MAX_COORDINATE for value in point):
        raise ValueError(f"{field_name} coordinates must be within ±{MAX_COORDINATE:g}")
    return point


HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def validate_color(value: str) -> str:
    if not HEX_COLOR.fullmatch(value):
        raise ValueError("color must use #RRGGBB notation")
    return value.upper()


class PhysicsObject(ContractModel):
    id: str
    shape: ShapeKind
    radius: float | None = Field(default=None, gt=0, le=MAX_COORDINATE)
    width: float | None = Field(default=None, gt=0, le=MAX_COORDINATE)
    height: float | None = Field(default=None, gt=0, le=MAX_COORDINATE)
    vertices: list[Point] | None = None
    point_a: Point | None = None
    point_b: Point | None = None
    segment_radius: float = Field(default=2.0, gt=0, le=100)
    position: Point = (0.0, 0.0)
    angle: float = Field(default=0.0, ge=-10_000 * math.pi, le=10_000 * math.pi)
    velocity: Point = (0.0, 0.0)
    angular_velocity: float = Field(default=0.0, ge=-MAX_ANGULAR_VELOCITY, le=MAX_ANGULAR_VELOCITY)
    mass: float = Field(default=1.0, gt=0, le=MAX_MASS)
    is_static: bool = False
    friction: float = Field(default=0.5, ge=0, le=2)
    restitution: float = Field(default=0.5, ge=0, le=1)
    inspectable: bool = True
    collision_group: int = Field(default=0, ge=0, le=65_535)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not IDENTIFIER.fullmatch(value):
            raise ValueError("id must be 1-64 safe identifier characters")
        return value

    @field_validator("position", "point_a", "point_b")
    @classmethod
    def validate_coordinates(cls, value: Point | None, info: Any) -> Point | None:
        return None if value is None else _point_within_limit(value, info.field_name)

    @field_validator("velocity")
    @classmethod
    def validate_velocity(cls, value: Point) -> Point:
        if any(abs(component) > MAX_VELOCITY for component in value):
            raise ValueError(f"velocity components must be within ±{MAX_VELOCITY:g}")
        return value

    @model_validator(mode="after")
    def validate_geometry(self) -> "PhysicsObject":
        if self.shape == "circle" and self.radius is None:
            raise ValueError("circle requires radius")
        if self.shape == "box" and (self.width is None or self.height is None):
            raise ValueError("box requires width and height")
        if self.shape == "segment":
            if self.point_a is None or self.point_b is None:
                raise ValueError("segment requires pointA and pointB")
            if self.point_a == self.point_b:
                raise ValueError("segment endpoints must differ")
            if not self.is_static:
                raise ValueError("segments must be static")
        if self.shape == "polygon":
            if not self.vertices or not 3 <= len(self.vertices) <= MAX_POLYGON_VERTICES:
                raise ValueError(f"polygon requires 3-{MAX_POLYGON_VERTICES} vertices")
            for vertex in self.vertices:
                _point_within_limit(vertex, "vertices")
            object.__setattr__(self, "vertices", _validated_convex_vertices(self.vertices))
        return self


def _validated_convex_vertices(vertices: list[Point]) -> list[Point]:
    """Validate a simple convex polygon and normalize winding counter-clockwise."""

    if len(set(vertices)) != len(vertices):
        raise ValueError("polygon vertices must be unique")
    signed_area = sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(vertices, vertices[1:] + vertices[:1], strict=True)
    )
    if abs(signed_area) < 1e-8:
        raise ValueError("polygon area must be non-zero")
    normalized = list(reversed(vertices)) if signed_area < 0 else list(vertices)
    signs: set[bool] = set()
    for index in range(len(normalized)):
        a = normalized[index - 2]
        b = normalized[index - 1]
        c = normalized[index]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) < 1e-8:
            raise ValueError("polygon cannot contain collinear adjacent vertices")
        signs.add(cross > 0)
    if len(signs) != 1:
        raise ValueError("polygon must be convex and consistently wound")
    return normalized


class ConstantForce(ContractModel):
    applies_to: list[str] = Field(min_length=1, max_length=MAX_OBJECTS)
    vector: Point

    @field_validator("vector")
    @classmethod
    def validate_magnitude(cls, value: Point) -> Point:
        if math.hypot(*value) > MAX_FORCE_MAGNITUDE:
            raise ValueError(f"force magnitude cannot exceed {MAX_FORCE_MAGNITUDE:g}")
        return value


class ConstraintSpec(ContractModel):
    id: str
    type: Literal["pin", "dampedSpring"]
    object_a: str
    object_b: str
    anchor_a: Point = (0.0, 0.0)
    anchor_b: Point = (0.0, 0.0)
    distance: float | None = Field(default=None, gt=0, le=MAX_COORDINATE * 2)
    rest_length: float | None = Field(default=None, gt=0, le=MAX_COORDINATE * 2)
    stiffness: float | None = Field(default=None, gt=0, le=MAX_FORCE_MAGNITUDE)
    damping: float | None = Field(default=None, ge=0, le=MAX_FORCE_MAGNITUDE)

    @model_validator(mode="after")
    def validate_constraint_fields(self) -> "ConstraintSpec":
        if self.type == "dampedSpring" and (
            self.rest_length is None or self.stiffness is None or self.damping is None
        ):
            raise ValueError("dampedSpring requires restLength, stiffness, and damping")
        return self


class PhysicsSpec(ContractModel):
    gravity: Point = (0.0, -981.0)
    duration: float = Field(gt=0, le=MAX_DURATION_SECONDS)
    dt: float = Field(default=1 / 60, ge=MIN_TIMESTEP_SECONDS, le=MAX_TIMESTEP_SECONDS)
    objects: list[PhysicsObject] = Field(min_length=1, max_length=MAX_OBJECTS)
    forces: list[ConstantForce] = Field(default_factory=list, max_length=MAX_FORCES)
    constraints: list[ConstraintSpec] = Field(default_factory=list, max_length=MAX_FORCES)

    @field_validator("gravity")
    @classmethod
    def validate_gravity(cls, value: Point) -> Point:
        if math.hypot(*value) > MAX_FORCE_MAGNITUDE:
            raise ValueError("gravity magnitude is too large")
        return value

    @model_validator(mode="after")
    def validate_references(self) -> "PhysicsSpec":
        ids = [obj.id for obj in self.objects]
        if len(ids) != len(set(ids)):
            raise ValueError("object ids must be unique")
        known = set(ids)
        dynamic = {obj.id for obj in self.objects if not obj.is_static}
        for force in self.forces:
            missing = set(force.applies_to) - known
            if missing:
                raise ValueError(f"force references unknown objects: {', '.join(sorted(missing))}")
            static_targets = set(force.applies_to) - dynamic
            if static_targets:
                raise ValueError(f"forces cannot target static objects: {', '.join(sorted(static_targets))}")
        constraint_ids = [constraint.id for constraint in self.constraints]
        if len(constraint_ids) != len(set(constraint_ids)):
            raise ValueError("constraint ids must be unique")
        for constraint in self.constraints:
            missing = {constraint.object_a, constraint.object_b} - known
            if missing:
                raise ValueError(f"constraint references unknown objects: {', '.join(sorted(missing))}")
            if constraint.object_a == constraint.object_b:
                raise ValueError("constraint endpoints must be different objects")
        frame_count = math.ceil(self.duration / self.dt) + 1
        if frame_count > 7_201:
            raise ValueError("duration and dt would allocate too many simulation frames")
        return self


class ObjectStyle(ContractModel):
    color: Color = "#378ADD"
    label: str | None = Field(default=None, max_length=MAX_LABEL_LENGTH)
    show_label: bool = False
    opacity: float = Field(default=0.9, ge=0, le=1)
    stroke_width: float = Field(default=2.0, ge=0, le=20)

    _color = field_validator("color")(validate_color)


class CameraSpec(ContractModel):
    zoom: float = Field(default=1.0, ge=0.1, le=10)
    center: Point = (0.0, 0.0)

    @field_validator("center")
    @classmethod
    def validate_center(cls, value: Point) -> Point:
        return _point_within_limit(value, "center")


class TrailSpec(ContractModel):
    enabled: bool = False
    max_points: int = Field(default=240, ge=2, le=MAX_TRAIL_POINTS)
    sample_every: int = Field(default=2, ge=1, le=60)
    fade: bool = True


class OverlaySpec(ContractModel):
    id: str
    kind: OverlayKind
    target_id: str | None = None
    label: str | None = Field(default=None, max_length=MAX_LABEL_LENGTH)
    color: Color = "#378ADD"
    visible: bool = True
    data: dict[str, Any] = Field(default_factory=dict)

    _color = field_validator("color")(validate_color)


class ParameterSpec(ContractModel):
    id: str
    path: str
    type: Literal["number", "integer", "boolean", "choice"] = "number"
    default: float | int | bool | str
    minimum: float | None = None
    maximum: float | None = None
    choices: list[str] | None = None
    unit: str | None = Field(default=None, max_length=32)
    local_resimulation_safe: bool = True

    @model_validator(mode="after")
    def validate_bounds(self) -> "ParameterSpec":
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum cannot exceed maximum")
        if self.type == "choice" and not self.choices:
            raise ValueError("choice parameter requires choices")
        return self


class VisualSpec(ContractModel):
    scene_size: tuple[int, int] = (800, 500)
    units: Literal["pixels", "metres"] = "pixels"
    coordinate_system: Literal["cartesian-y-up"] = "cartesian-y-up"
    object_styles: dict[str, ObjectStyle] = Field(default_factory=dict)
    background_color: Color = "#FFFFFF"
    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    camera: CameraSpec = Field(default_factory=CameraSpec)
    trail: TrailSpec = Field(default_factory=TrailSpec)
    overlays: list[OverlaySpec] = Field(default_factory=list, max_length=128)

    _background = field_validator("background_color")(validate_color)

    @field_validator("scene_size")
    @classmethod
    def validate_scene_size(cls, value: tuple[int, int]) -> tuple[int, int]:
        if not 320 <= value[0] <= MAX_EXPORT_WIDTH or not 180 <= value[1] <= MAX_EXPORT_HEIGHT:
            raise ValueError("sceneSize must be between 320x180 and 3840x2160")
        return value


class CompilerMetadata(ContractModel):
    origin: Literal["template", "model", "provided"]
    template_id: str | None = None
    provider: str | None = None
    model: str | None = None
    compiler_version: str = "1"
    normalized_prompt_hash: str | None = None


class SceneSpec(ContractModel):
    schema_version: int = Field(default=SCHEMA_VERSION, ge=SCHEMA_VERSION, le=SCHEMA_VERSION)
    scene_id: str = "scene_1"
    description: str | None = Field(default=None, max_length=1_000)
    physics: PhysicsSpec
    visual: VisualSpec = Field(default_factory=VisualSpec)
    parameters: list[ParameterSpec] = Field(default_factory=list, max_length=64)
    metadata: CompilerMetadata = Field(default_factory=lambda: CompilerMetadata(origin="provided"))

    @model_validator(mode="after")
    def validate_visual_references(self) -> "SceneSpec":
        ids = {obj.id for obj in self.physics.objects}
        missing_styles = set(self.visual.object_styles) - ids
        if missing_styles:
            raise ValueError(f"styles reference unknown objects: {', '.join(sorted(missing_styles))}")
        for overlay in self.visual.overlays:
            if overlay.target_id is not None and overlay.target_id not in ids:
                raise ValueError(f"overlay '{overlay.id}' references unknown object '{overlay.target_id}'")
        parameter_ids = [parameter.id for parameter in self.parameters]
        if len(parameter_ids) != len(set(parameter_ids)):
            raise ValueError("parameter ids must be unique")
        return self


class CompileRequest(ContractModel):
    contract_version: int = Field(default=CONTRACT_VERSION, ge=CONTRACT_VERSION, le=CONTRACT_VERSION)
    prompt: str | None = Field(default=None, max_length=MAX_PROMPT_LENGTH)
    scene: SceneSpec | None = None
    parameters: dict[str, float | int | bool | str] = Field(default_factory=dict, max_length=64)
    template: str | None = None
    provider: Literal["ollama", "anthropic"] = "ollama"
    model: str | None = Field(default=None, max_length=200)
    prefer_template: bool = True
    timeout_seconds: float = Field(default=90.0, gt=0, le=300)
    privacy: Literal["standard", "redact"] = "standard"

    @model_validator(mode="after")
    def require_input(self) -> "CompileRequest":
        if self.scene is None and not (self.prompt and self.prompt.strip()):
            raise ValueError("prompt or scene is required")
        return self


class SimulationOptions(ContractModel):
    contract_version: int = Field(default=CONTRACT_VERSION, ge=CONTRACT_VERSION, le=CONTRACT_VERSION)
    recommended_playback_fps: int = Field(default=30, ge=1, le=MAX_EXPORT_FPS)
    record_inspectables: bool = True
    detect_events: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)


class SimulationRequest(ContractModel):
    contract_version: int = Field(default=CONTRACT_VERSION, ge=CONTRACT_VERSION, le=CONTRACT_VERSION)
    scene: SceneSpec | None = None
    compile_job_id: str | None = None
    options: SimulationOptions = Field(default_factory=SimulationOptions)

    @model_validator(mode="after")
    def require_scene_source(self) -> "SimulationRequest":
        if (self.scene is None) == (self.compile_job_id is None):
            raise ValueError("provide exactly one of scene or compileJobId")
        return self


class VisualizationRequest(CompileRequest):
    """Create a playable visualization in one compile-and-simulate operation."""

    simulation_options: SimulationOptions = Field(default_factory=SimulationOptions)

    def compile_request(self) -> CompileRequest:
        return CompileRequest.model_validate(
            self.contract_dump(exclude={"simulation_options"})
        )


class ParameterUpdateRequest(ContractModel):
    contract_version: int = Field(default=CONTRACT_VERSION, ge=CONTRACT_VERSION, le=CONTRACT_VERSION)
    parameters: dict[str, float | int | bool | str] = Field(min_length=1, max_length=64)
    simulation_options: SimulationOptions | None = None


class VisualizationExportRequest(ContractModel):
    contract_version: int = Field(default=CONTRACT_VERSION, ge=CONTRACT_VERSION, le=CONTRACT_VERSION)
    options: "ExportOptions" = Field(default_factory=lambda: ExportOptions())


class TimelineObject(ContractModel):
    shape: ShapeKind
    is_static: bool
    mass: float = 1.0
    radius: float | None = None
    width: float | None = None
    height: float | None = None
    vertices: list[Point] | None = None
    point_a: Point | None = None
    point_b: Point | None = None
    segment_radius: float | None = None
    color: Color = "#378ADD"
    label: str | None = None
    show_label: bool = False
    opacity: float = 0.9
    stroke_width: float = 2.0


class TimelineScene(ContractModel):
    size: tuple[int, int] = (800, 500)
    units: str = "pixels"
    coordinate_system: str = "cartesian-y-up"
    background: Color = "#FFFFFF"
    title: str | None = None
    gravity: Point = (0.0, -981.0)
    objects: dict[str, TimelineObject]
    constraints: list[ConstraintSpec] = Field(default_factory=list)
    camera: CameraSpec = Field(default_factory=CameraSpec)
    trail: TrailSpec = Field(default_factory=TrailSpec)


class ObjectTrack(ContractModel):
    times: list[float]
    x: list[float]
    y: list[float]
    angle: list[float]
    vx: list[float] = Field(default_factory=list)
    vy: list[float] = Field(default_factory=list)
    angular_velocity: list[float] = Field(default_factory=list)
    ax: list[float] = Field(default_factory=list)
    ay: list[float] = Field(default_factory=list)
    force_x: list[float] = Field(default_factory=list)
    force_y: list[float] = Field(default_factory=list)
    kinetic_energy: list[float] = Field(default_factory=list)
    potential_energy: list[float] = Field(default_factory=list)
    momentum_x: list[float] = Field(default_factory=list)
    momentum_y: list[float] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_lengths(self) -> "ObjectTrack":
        length = len(self.times)
        if length == 0:
            raise ValueError("track requires at least one sample")
        if any(b <= a for a, b in zip(self.times, self.times[1:])):
            raise ValueError("track times must be strictly increasing")
        required = (self.x, self.y, self.angle)
        optional = (
            self.vx,
            self.vy,
            self.angular_velocity,
            self.ax,
            self.ay,
            self.force_x,
            self.force_y,
            self.kinetic_energy,
            self.potential_energy,
            self.momentum_x,
            self.momentum_y,
        )
        if any(len(values) != length for values in required):
            raise ValueError("position and angle arrays must match times")
        if any(values and len(values) != length for values in optional):
            raise ValueError("inspectable arrays must be empty or match times")
        return self


class TimelineEvent(ContractModel):
    id: str
    type: Literal["collision", "apex", "rest", "crossing", "custom"]
    time: float = Field(ge=0)
    object_ids: list[str] = Field(default_factory=list)
    data: dict[str, float | int | str | bool] = Field(default_factory=dict)


class OverlayTrack(ContractModel):
    overlay: OverlaySpec
    times: list[float] = Field(default_factory=list)
    visible: list[bool] = Field(default_factory=list)


class Timeline(ContractModel):
    contract_version: int = Field(default=CONTRACT_VERSION, ge=CONTRACT_VERSION, le=CONTRACT_VERSION)
    timeline_version: int = Field(default=TIMELINE_VERSION, ge=TIMELINE_VERSION, le=TIMELINE_VERSION)
    duration: float = Field(gt=0, le=MAX_DURATION_SECONDS)
    simulation_fps: float = Field(gt=0, le=240)
    recommended_playback_fps: int = Field(default=30, ge=1, le=MAX_EXPORT_FPS)
    interpolation: Literal["linear", "shortest-angle-linear"] = "shortest-angle-linear"
    scene: TimelineScene
    tracks: dict[str, ObjectTrack]
    overlay_tracks: dict[str, OverlayTrack] = Field(default_factory=dict)
    events: list[TimelineEvent] = Field(default_factory=list)
    parameters: list[ParameterSpec] = Field(default_factory=list)
    source_scene_hash: str

    @model_validator(mode="after")
    def validate_timeline(self) -> "Timeline":
        if set(self.tracks) != set(self.scene.objects):
            raise ValueError("timeline tracks must match scene objects")
        for event in self.events:
            if event.time > self.duration + 1e-9:
                raise ValueError("event time cannot exceed duration")
        return self


class ExportPreset(StrEnum):
    PREVIEW = "preview"
    HIGH = "high"
    CUSTOM = "custom"


class ExportOptions(ContractModel):
    contract_version: int = Field(default=CONTRACT_VERSION, ge=CONTRACT_VERSION, le=CONTRACT_VERSION)
    preset: ExportPreset = ExportPreset.PREVIEW
    width: int | None = Field(default=None, ge=320, le=MAX_EXPORT_WIDTH)
    height: int | None = Field(default=None, ge=180, le=MAX_EXPORT_HEIGHT)
    fps: int | None = Field(default=None, ge=1, le=MAX_EXPORT_FPS)
    codec: Literal["h264"] = "h264"
    timeout_seconds: float = Field(default=600.0, gt=0, le=3_600)

    def resolved(self) -> tuple[int, int, int]:
        if self.preset == ExportPreset.HIGH:
            defaults = (1920, 1080, 60)
        else:
            defaults = (640, 360, 30)
        return self.width or defaults[0], self.height or defaults[1], self.fps or defaults[2]


class ExportRequest(ContractModel):
    contract_version: int = Field(default=CONTRACT_VERSION, ge=CONTRACT_VERSION, le=CONTRACT_VERSION)
    timeline: Timeline | None = None
    simulation_job_id: str | None = None
    options: ExportOptions = Field(default_factory=ExportOptions)

    @model_validator(mode="after")
    def require_timeline_source(self) -> "ExportRequest":
        if (self.timeline is None) == (self.simulation_job_id is None):
            raise ValueError("provide exactly one of timeline or simulationJobId")
        return self


class ExportResult(ContractModel):
    contract_version: int = CONTRACT_VERSION
    output_path: str
    duration: float
    width: int
    height: int
    fps: int
    codec: str = "h264"
    pixel_format: str = "yuv420p"
    fast_start: bool = True
    size_bytes: int
    render_seconds: float


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStage(StrEnum):
    QUEUED = "queued"
    COMPILING = "compiling"
    VALIDATING = "validating"
    SIMULATING = "simulating"
    TIMELINE = "buildingTimeline"
    READY = "ready"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobError(ContractModel):
    code: str
    message: str
    retriable: bool = False
    details: Any = None


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class JobResponse(ContractModel):
    contract_version: int = CONTRACT_VERSION
    job_id: str
    kind: Literal["compile", "simulation", "export", "visualization"]
    status: JobStatus
    stage: JobStage
    progress: float = Field(ge=0, le=1)
    error: JobError | None = None
    result: dict[str, Any] | None = None
    created_at: str
    updated_at: str
    timings: dict[str, float] = Field(default_factory=dict)
