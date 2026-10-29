"""Pydantic request/response schemas (AGENTS.md sections 10-11)."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


RunStatus = Literal[
    "queued",
    "preparing",
    "running",
    "evaluating",
    "succeeded",
    "failed",
    "timeout",
    "cancelled",
    "error",
]

TERMINAL_RUN_STATUSES: tuple[str, ...] = (
    "succeeded",
    "failed",
    "timeout",
    "cancelled",
    "error",
)


# ---- projects ----


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    task_description: str = Field(default="", max_length=20000)


class ProjectOut(BaseModel):
    id: str
    name: str
    task_description: str
    created_at: str
    updated_at: str
    #: Grading phases for the current controller (None = flag default).
    objectives: list[RunObjectiveOut] | None = None


class ProjectFileEntry(BaseModel):
    path: str
    version: int
    updated_at: str | None = None


class ProjectFileContent(BaseModel):
    path: str
    version: int
    updated_at: str | None = None
    content: str


class ProjectBrowseEntry(BaseModel):
    type: str  # "dir" or "file"
    path: str
    size: int | None = None
    version: int | None = None


class ProjectFileSave(BaseModel):
    content: str = Field(max_length=300000)


# ---- generation (AGENTS.md section 6 output contract) ----


class GeneratedFile(BaseModel):
    path: str = Field(min_length=1, max_length=256)
    content: str = Field(max_length=300000)


class GenerateRequest(BaseModel):
    task_description: str | None = Field(default=None, max_length=20000)


class RobotJointOut(BaseModel):
    """One actuated joint of the generated robot description."""

    name: str
    type: str
    actuator: str = ""
    lower: float | None = None
    upper: float | None = None


class PlannedTestOut(BaseModel):
    """One automated joint test the platform will run on this robot."""

    id: str
    joint: str
    target: float
    tolerance: float


class RobotPackageOut(BaseModel):
    """Validated robot package returned with a generation/correction."""

    model_path: str
    joints: list[RobotJointOut] = []
    tests: list[PlannedTestOut] = []
    notes: list[str] = []


class StarterRobotOut(BaseModel):
    """A bundled starter robot the platform can install into a project."""

    id: str
    name: str
    summary: str
    attribution: str
    license: str
    source_url: str = ""
    model_path: str
    joints: list[RobotJointOut] = []
    tests: list[PlannedTestOut] = []


class StarterRobotInstall(BaseModel):
    """Request body for installing a starter robot."""

    robot_id: str
    # False installs the robot with an EMPTY controller.py (fresh
    # experiments); the sample stays available via the sample endpoint.
    include_controller: bool = True


class TraceStep(BaseModel):
    """One recorded stage of a model operation (for under-the-hood display)."""

    stage: str
    status: str = "ok"
    ms: int = 0
    detail: str = ""


class RunObjectiveOut(BaseModel):
    """Task objective declared by the model (drives runner + evaluator)."""

    type: str = "go_to"
    target: list[float] | None = None
    #: Composed prop body to reach (e.g. "env_box_2"), resolved live.
    target_body: str | None = None


class GenerationResult(BaseModel):
    summary: str
    assumptions: list[str] = []
    files: list[GeneratedFile]
    expected_behavior: str = ""
    safety_considerations: str = ""
    #: Ordered objective phases (1+, no cap); None only from test fakes.
    objectives: list[RunObjectiveOut] | None = None
    robot: RobotPackageOut | None = None
    trace: list[TraceStep] = []
    model_name: str = ""
    latency_ms: int | None = None


class FixRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=128)


class FixResult(BaseModel):
    failure_hypothesis: str
    evidence: list[str] = []
    proposed_change: str
    updated_files: list[GeneratedFile]
    expected_improvement: str = ""
    robot: RobotPackageOut | None = None
    trace: list[TraceStep] = []
    model_name: str = ""
    latency_ms: int | None = None


class ModelRequestOut(BaseModel):
    id: int
    project_id: str
    run_id: str | None = None
    purpose: str
    model_name: str
    prompt_version: str
    status: str
    latency_ms: int | None = None
    error_summary: str | None = None
    created_at: str | None = None


class TaskInterpretation(BaseModel):
    """Structured reading of a natural-language task (AGENTS.md section 6)."""

    goal: str
    safety_requirements: list[str] = []
    assumptions: list[str] = []
    needs_clarification: bool = False
    clarification_question: str = ""
    suggested_task: str = ""


# ---- runs ----


class RunCreate(BaseModel):
    world_config: dict[str, Any] | None = None
    seed: int = Field(default=0, ge=0, le=2**31 - 1)
    parent_run_id: str | None = Field(default=None, max_length=128)


class RunMetricsOut(BaseModel):
    success: bool
    collision_count: int
    goal_distance: float
    completion_time: float
    path_length: float
    emergency_stop_latency: float | None
    evaluator_version: str
    # MuJoCo automated-test metrics (AGENTS.md §8); 0 for 2D runs.
    tests_total: int = 0
    tests_passed: int = 0
    max_joint_error: float = 0.0
    total_contacts: int = 0


class EvaluationReportOut(BaseModel):
    success: bool
    reasons: list[str] = []
    evaluator_version: str


class JointStateOut(BaseModel):
    """One actuated joint as reported by the MuJoCo state interface."""

    name: str
    position: float
    velocity: float
    effort: float = 0.0
    target: float = 0.0


class TestResultOut(BaseModel):
    """Objective evidence for one automated joint test (AGENTS.md §8)."""

    id: str
    kind: str = "joint_move"
    joint: str
    target: float
    tolerance: float
    final_position: float
    final_error: float
    max_error: float
    max_velocity: float
    max_effort: float
    settling_time: float | None = None
    duration: float
    contacts: int = 0
    max_contact_force: float = 0.0
    passed: bool
    reason: str = ""
    estop_latency: float | None = None


class RunOut(BaseModel):
    id: str
    project_id: str
    parent_run_id: str | None
    controller_version: int
    status: str
    backend: str = "differential_drive"
    world_config: dict[str, Any] | None
    seed: int
    started_at: str | None
    finished_at: str | None
    exit_code: int | None
    failure_summary: str | None
    metrics: RunMetricsOut | None = None
    evaluation: EvaluationReportOut | None = None
    tests: list[TestResultOut] = []
    paused: bool = False


class BodyPoseOut(BaseModel):
    body: str
    pos: list[float]
    quat: list[float]


class TelemetrySample(BaseModel):
    t: float
    x: float
    y: float
    theta: float
    v: float
    omega: float
    collision: bool = False
    goal_reached: bool = False
    estop_active: bool = False
    min_range: float | None = None
    # MuJoCo joint telemetry (absent for 2D runs).
    joints: list[JointStateOut] | None = None
    contacts: int | None = None
    max_contact_force: float | None = None
    test_id: str | None = None
    # Official-model telemetry (absent unless the scenario enables it).
    fallen: bool | None = None
    trunk_z: float | None = None
    expected_contacts: int | None = None
    sensors: dict[str, list[float]] | None = None
    bodies: list[BodyPoseOut] | None = None


class ComparisonRunSummary(BaseModel):
    run_id: str
    status: str
    controller_version: int
    failure_summary: str | None
    metrics: RunMetricsOut | None
    backend: str = "differential_drive"
    tests_total: int = 0
    tests_passed: int = 0


class RunComparison(BaseModel):
    baseline: ComparisonRunSummary | None
    current: ComparisonRunSummary
