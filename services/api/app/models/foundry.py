"""Pydantic models for the Robot Foundry API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class FoundryProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    category: str = Field(default="biped", max_length=40)


class FoundryProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    category: str | None = Field(default=None, max_length=40)


class FoundryProjectOut(BaseModel):
    id: str
    project_id: str
    name: str
    description: str
    category: str
    status: str
    thumbnail_url: str | None = None
    thumbnail_asset_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    created_at: str
    updated_at: str
    component_count: int = 0
    joint_count: int = 0
    actuated_joint_count: int = 0
    latest_version: int | None = None
    validation_status: str | None = None


class FoundryImageOut(BaseModel):
    id: str
    project_id: str
    filename: str
    file_url: str
    asset_url: str
    image_type: str
    analysis_status: str
    width: int | None = None
    height: int | None = None
    created_at: str


class FoundryComponentCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    component_type: str = Field(default="other", max_length=40)
    parent_id: str | None = None
    box: list[int] | None = None
    dimensions: list[float] | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class FoundryComponentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    label: str | None = Field(default=None, min_length=1, max_length=80)
    component_type: str | None = Field(default=None, max_length=40)
    parent_id: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    dimensions: list[float] | None = None
    mass_kg: float | None = Field(default=None, gt=0)
    friction: float | None = Field(default=None, ge=0.0, le=2.0)
    visible: bool | None = None
    metadata: dict[str, Any] | None = None


class FoundryComponentOut(BaseModel):
    id: str
    project_id: str
    parent_id: str | None = None
    name: str
    label: str
    component_type: str
    mesh_url: str | None = None
    collision_mesh_url: str | None = None
    visual_asset_url: str | None = None
    collision_asset_url: str | None = None
    preview_asset_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    confidence: float
    created_at: str
    updated_at: str


class FoundryJointUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    # Hinge/slide only: the simulator run contract (position actuators, 1-DoF
    # joint tests) cannot drive ball/free joints, so they are rejected here
    # instead of failing later at MJCF generation.
    joint_type: Literal["fixed", "hinge", "slide"] | None = None
    axis: list[float] | None = None
    position: list[float] | None = None
    limits: list[float] | None = None
    parent_component_id: str | None = None
    metadata: dict[str, Any] | None = None


class FoundryJointOut(BaseModel):
    id: str
    project_id: str
    component_id: str
    parent_component_id: str | None = None
    name: str
    joint_type: str
    axis: list[float]
    position: list[float]
    limits: list[float] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FoundryActuatorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    actuator_type: Literal["position", "velocity", "motor"] | None = None
    kp: float | None = None
    kv: float | None = None
    gear: float | None = None
    ctrlrange: list[float] | None = None
    forcerange: list[float] | None = None
    configuration: dict[str, Any] | None = None


class FoundryActuatorOut(BaseModel):
    id: str
    project_id: str
    joint_id: str
    name: str
    actuator_type: str
    configuration: dict[str, Any] = Field(default_factory=dict)


class FoundryJobOut(BaseModel):
    id: str
    project_id: str
    job_type: str
    status: str
    progress: int
    message: str
    error_message: str | None = None
    result: dict[str, Any] | None = None
    created_at: str
    completed_at: str | None = None


class FoundryVersionOut(BaseModel):
    id: str
    project_id: str
    version: int
    mjcf_url: str | None = None
    manifest_url: str | None = None
    validation: dict[str, Any] | None = None
    created_at: str


class FoundryProjectDetail(BaseModel):
    project: FoundryProjectOut
    images: list[FoundryImageOut]
    components: list[FoundryComponentOut]
    joints: list[FoundryJointOut]
    actuators: list[FoundryActuatorOut]
    jobs: list[FoundryJobOut]
    versions: list[FoundryVersionOut]
    analysis: dict[str, Any] | None = None


class FoundryPreview(BaseModel):
    project_id: str
    foundry_id: str
    name: str
    status: str
    components: list[FoundryComponentOut]
    joints: list[FoundryJointOut]
    actuators: list[FoundryActuatorOut]
    analysis: dict[str, Any] | None = None


class FoundryJobStartOut(BaseModel):
    job: FoundryJobOut


class FoundrySimulateOut(BaseModel):
    foundry_id: str
    project_id: str
    version: int | None = None
    validation_status: str | None = None
    text_files: list[str] = Field(default_factory=list)
    binary_files: list[str] = Field(default_factory=list)


__all__ = [
    "FoundryActuatorOut",
    "FoundryActuatorUpdate",
    "FoundryComponentCreate",
    "FoundryComponentOut",
    "FoundryComponentUpdate",
    "FoundryImageOut",
    "FoundryJobOut",
    "FoundryJobStartOut",
    "FoundryJointOut",
    "FoundryJointUpdate",
    "FoundryPreview",
    "FoundryProjectCreate",
    "FoundryProjectDetail",
    "FoundryProjectOut",
    "FoundryProjectUpdate",
    "FoundrySimulateOut",
    "FoundryVersionOut",
]
