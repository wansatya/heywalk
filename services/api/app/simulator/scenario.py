"""Scenario + automated test-suite configuration for simulation backends.

MuJoCo is the first-class backend (AGENTS.md sections 7 and 8): a run loads a
generated robot description into MuJoCo, emulates the ros2_control position
command interfaces, and executes a deterministic suite of joint tests. The
legacy deterministic 2D differential-drive arena remains available as a
second backend so existing experiments keep working.

A run's scenario is stored in the run's ``world_config`` JSON. For MuJoCo it
carries the physics/control rates, the robot description file names, the
automated test cases, and the safety settings. Test cases are platform-owned
(not model-authored) so the verdict stays objective; the agent only writes
the controller and the robot description.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

BACKEND_MUJOCO = "mujoco"
BACKEND_DIFF_DRIVE = "differential_drive"
SUPPORTED_BACKENDS: tuple[str, ...] = (BACKEND_MUJOCO, BACKEND_DIFF_DRIVE)

# Keys that only a differential-drive world uses. A run config carrying them
# without an explicit backend is treated as a legacy 2D run, so existing
# experiments stay reproducible.
_DIFF_DRIVE_MARKERS = frozenset(
    {
        "goal",
        "start",
        "obstacles",
        "max_linear_speed",
        "max_angular_speed",
        "robot_radius",
        "narrow_passage",
    }
)

DEFAULT_MODEL_PATH = "robot.xml"
DEFAULT_URDF_PATH = "robot.urdf"
DEFAULT_ROS2_CONTROL_PATH = "ros2_control.yaml"

MAX_TESTS = 32
MAX_JOINT_TARGET = 100.0

#: MuJoCo defaults used when a run config omits them.
MUJOCO_DEFAULTS: dict[str, Any] = {
    "dt": 0.002,
    "control_hz": 50.0,
    "telemetry_hz": 20.0,
    "time_limit": 30.0,
    "collision_force_threshold": 0.05,
    "ignore_root_contacts": True,
    "joint_velocity_tolerance": 0.05,
    "estop_after_s": None,
    "estop_latency_limit": 0.5,
    "model_path": DEFAULT_MODEL_PATH,
    "urdf_path": DEFAULT_URDF_PATH,
    "ros2_control_path": DEFAULT_ROS2_CONTROL_PATH,
    # Official-model extensions; all default off so legacy runs are unchanged.
    "keyframe": None,
    "expected_contact_geoms": [],
    "fall_trunk_z": None,
    "include_body_poses": False,
    "integrator": None,
    # Scene layout (Arrange mode): duck spawn + prop positions on the floor
    # plane. Null/empty means the composed defaults; z always rests.
    "spawn_xy": None,
    "spawn_yaw": None,
    "env_layout": {},
    # Weld the trunk to a test stand (lock free joints) so joint tracking
    # is gradeable; a free-standing biped topples under position control
    # before any test can measure anything. Fall physics stays available
    # with fix_base=false.
    "fix_base": False,
    # Locomotion runs: the harness drives the vendored upstream walking
    # policy (50 Hz) while the generated controller steers it with /cmd_vel
    # (Twist: forward m/s, yaw rad/s). Default off so joint-test runs are
    # unchanged. locomotion_twist is the fallback until the controller
    # publishes its first Twist; goal_geoms (e.g. the flag) never count as
    # collisions because touching the goal is the objective.
    "locomotion": False,
    "locomotion_twist": [0.2, 0.0, 0.0],
    "locomotion_duration_s": 20.0,
    "locomotion_goal_radius": 0.3,
    "goal_geoms": [],
    # Task objectives (see RunObjective): ordered phases the behavior must
    # achieve in sequence (1+, no cap). Null/single means go_to the flag —
    # every run before sequences existed.
    "objective": None,
    "objectives": None,
}

#: Floor-plane bounds for scene-layout coordinates (matches the 4 m grid).
LAYOUT_XY_LIMIT = 2.0


class ScenarioError(ValueError):
    """Raised when a scenario or test suite is malformed."""


def normalize_backend(name: str | None) -> str:
    value = (name or "").strip().lower().replace("-", "_")
    if value in ("", "auto"):
        return BACKEND_MUJOCO
    if value in ("2d", "diff_drive", "differential"):
        return BACKEND_DIFF_DRIVE
    if value == BACKEND_MUJOCO:
        return BACKEND_MUJOCO
    if value == BACKEND_DIFF_DRIVE:
        return BACKEND_DIFF_DRIVE
    raise ScenarioError(
        f"Unknown simulation backend {name!r}; expected one of"
        f" {', '.join(SUPPORTED_BACKENDS)}."
    )


def infer_backend(
    world_config: dict[str, Any] | None,
    project_backend: str | None = None,
) -> str:
    """Resolve the backend for a run config.

    Explicit ``backend`` wins. A legacy world config (arena keys, no backend)
    keeps running on the 2D backend. Everything else follows the project
    backend (MuJoCo by default), so new experiments use MuJoCo first.
    """
    world = world_config or {}
    explicit = world.get("backend")
    if explicit:
        return normalize_backend(str(explicit))
    if any(key in world for key in _DIFF_DRIVE_MARKERS):
        return BACKEND_DIFF_DRIVE
    return normalize_backend(project_backend or BACKEND_MUJOCO)


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ScenarioError(f"{name} must be a number.") from None
    if not math.isfinite(number):
        raise ScenarioError(f"{name} must be finite.")
    return number


def _twist_triple(value: Any) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ScenarioError("locomotion_twist must be a [vx, vy, yaw] triple.")
    return (
        _finite(value[0], "locomotion_twist[0]"),
        _finite(value[1], "locomotion_twist[1]"),
        _finite(value[2], "locomotion_twist[2]"),
    )


def _name_tuple(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ScenarioError(f"{name} must be a JSON array.")
    return tuple(str(entry) for entry in value)


def safe_relative_path(value: str, name: str) -> str:
    """Validate a workspace-relative file path in a scenario."""
    path = (value or "").strip()
    if not path:
        raise ScenarioError(f"{name} must not be empty.")
    if path.startswith(("/", "~")) or "\\" in path:
        raise ScenarioError(f"{name} must be a relative POSIX path.")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ScenarioError(f"{name} contains an illegal path segment.")
    return path


def _positive(value: Any, name: str) -> float:
    number = _finite(value, name)
    if number <= 0:
        raise ScenarioError(f"{name} must be positive.")
    return number


def _layout_coord(value: Any, name: str) -> float:
    number = _finite(value, name)
    if abs(number) > LAYOUT_XY_LIMIT:
        raise ScenarioError(
            f"{name} must be within ±{LAYOUT_XY_LIMIT:g} m."
        )
    return number


#: Locomotion objective types. go_to: reach a point and stop (the flag by
#: default, or a named prop via target_body). touch: make contact with an
#: arena wall, then stop — wall touches are the aim, other contacts still
#: fail. wander: keep moving without a destination until the window ends; the
#: verdict is survival (no fall, no disqualifying contact) plus distance
#: covered. kick: walk into the (dynamic) ball and push it away; the verdict
#: is ball contact plus measured ball displacement.
OBJECTIVE_TYPES: tuple[str, ...] = ("go_to", "touch", "wander", "kick")

#: Objective target bodies are composed-prop names, never arbitrary model
#: bodies: the runner resolves the live position, the prompt can name them,
#: and a hallucinated name fails validation instead of the run.
GOAL_BODY_PATTERN = re.compile(r"^env_[a-z0-9_]{1,48}$")


@dataclass(frozen=True)
class RunObjective:
    """What a locomotion run must achieve (declared from the task text)."""

    type: str = "go_to"
    target: tuple[float, float] | None = None
    #: Named prop to reach (e.g. "env_box_2"), resolved live from the engine.
    target_body: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "target": list(self.target) if self.target is not None else None,
            "target_body": self.target_body,
        }

    @classmethod
    def from_data(cls, data: Any) -> RunObjective:
        """Validate raw JSON into an objective (null/omitted = flag)."""
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ScenarioError("objective must be an object or null.")
        raw_type = data.get("type", "go_to")
        if raw_type not in OBJECTIVE_TYPES:
            raise ScenarioError(
                f"objective.type must be one of"
                f" {', '.join(OBJECTIVE_TYPES)}."
            )
        raw_target = data.get("target")
        target: tuple[float, float] | None = None
        if raw_target is not None:
            if raw_type in ("wander", "kick"):
                raise ScenarioError(
                    f"objective.target must be null for {raw_type} runs."
                )
            if not isinstance(raw_target, list) or len(raw_target) != 2:
                raise ScenarioError(
                    "objective.target must be an [x, y] pair or null."
                )
            target = (
                _layout_coord(raw_target[0], "objective.target[0]"),
                _layout_coord(raw_target[1], "objective.target[1]"),
            )
        raw_body = data.get("target_body")
        target_body: str | None = None
        if raw_body is not None:
            body = str(raw_body).strip()
            if raw_type != "go_to":
                raise ScenarioError(
                    "objective.target_body is only valid for go_to runs."
                )
            if not GOAL_BODY_PATTERN.match(body):
                raise ScenarioError(
                    "objective.target_body must be a composed prop name such"
                    " as 'env_box_1'."
                )
            target_body = body
        return cls(
            type=str(raw_type), target=target, target_body=target_body
        )


def parse_objectives(data: Any) -> tuple[RunObjective, ...]:
    """Validate raw JSON into an ordered objective sequence (1+, no cap).

    A single object (or null/omitted) is one phase, so every run stored
    before sequences existed keeps working unchanged.
    """
    if data is None:
        return (RunObjective(),)
    if isinstance(data, dict):
        items: list[Any] = [data]
    elif isinstance(data, list):
        items = data
    else:
        raise ScenarioError(
            "objectives must be an object, a list of objects, or null."
        )
    if not items:
        raise ScenarioError("objectives must declare at least one objective.")
    phases = tuple(RunObjective.from_data(item) for item in items)
    for phase in phases[:-1]:
        if phase.type == "wander":
            raise ScenarioError(
                "wander completes only at the window end, so it must be"
                " the last objective in the sequence."
            )
    return phases


@dataclass(frozen=True)
class JointTest:
    """One automated joint-move test executed by the MuJoCo harness."""

    id: str
    joint: str
    target: float
    kind: str = "joint_move"
    tolerance: float = 0.05
    hold_s: float = 0.25
    timeout_s: float = 5.0
    max_velocity: float | None = None
    max_effort: float | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ScenarioError("Test id must not be empty.")
        if not self.joint.strip():
            raise ScenarioError(f"Test {self.id!r} must name a joint.")
        if self.kind not in ("joint_move", "hold_position"):
            raise ScenarioError(
                f"Test {self.id!r} has unsupported kind {self.kind!r};"
                " expected 'joint_move' or 'hold_position'."
            )
        if not math.isfinite(self.target):
            raise ScenarioError(f"Test {self.id!r} target must be finite.")
        if abs(self.target) > MAX_JOINT_TARGET:
            raise ScenarioError(
                f"Test {self.id!r} target {self.target} exceeds the"
                f" ±{MAX_JOINT_TARGET:g} guard."
            )
        _positive(self.tolerance, f"Test {self.id!r} tolerance")
        _positive(self.timeout_s, f"Test {self.id!r} timeout_s")
        if self.hold_s < 0:
            raise ScenarioError(f"Test {self.id!r} hold_s must be >= 0.")
        if self.max_velocity is not None and self.max_velocity < 0:
            raise ScenarioError(
                f"Test {self.id!r} max_velocity must be non-negative."
            )
        if self.max_effort is not None and self.max_effort < 0:
            raise ScenarioError(
                f"Test {self.id!r} max_effort must be non-negative."
            )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "joint": self.joint,
            "target": self.target,
            "tolerance": self.tolerance,
            "hold_s": self.hold_s,
            "timeout_s": self.timeout_s,
        }
        if self.max_velocity is not None:
            data["max_velocity"] = self.max_velocity
        if self.max_effort is not None:
            data["max_effort"] = self.max_effort
        return data

    @classmethod
    def from_dict(cls, data: Any, index: int) -> JointTest:
        if not isinstance(data, dict):
            raise ScenarioError(f"Test #{index} must be a JSON object.")
        kind = str(data.get("kind", "joint_move"))
        joint = str(data.get("joint", "")).strip()
        if not joint:
            raise ScenarioError(f"Test #{index} must name a joint.")
        test_id = str(data.get("id") or f"{joint}_{index + 1}").strip()
        target = data.get("target", 0.0)
        max_velocity = data.get("max_velocity")
        max_effort = data.get("max_effort")
        return cls(
            id=test_id,
            joint=joint,
            target=_finite(target, f"Test {test_id!r} target"),
            kind=kind,
            tolerance=_finite(
                data.get("tolerance", 0.05), f"Test {test_id!r} tolerance"
            ),
            hold_s=_finite(data.get("hold_s", 0.25), f"Test {test_id!r} hold_s"),
            timeout_s=_finite(
                data.get("timeout_s", 5.0), f"Test {test_id!r} timeout_s"
            ),
            max_velocity=(
                None
                if max_velocity is None
                else _finite(max_velocity, f"Test {test_id!r} max_velocity")
            ),
            max_effort=(
                None
                if max_effort is None
                else _finite(max_effort, f"Test {test_id!r} max_effort")
            ),
        )


def parse_tests(raw: Any) -> tuple[JointTest, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ScenarioError("tests must be a JSON array.")
    if len(raw) > MAX_TESTS:
        raise ScenarioError(f"tests may contain at most {MAX_TESTS} cases.")
    tests = tuple(JointTest.from_dict(item, i) for i, item in enumerate(raw))
    seen: set[str] = set()
    for test in tests:
        if test.id in seen:
            raise ScenarioError(f"Duplicate test id {test.id!r}.")
        seen.add(test.id)
    return tests


@dataclass(frozen=True)
class MujocoScenario:
    """Deterministic MuJoCo scenario + automated test suite."""

    dt: float = 0.002
    control_hz: float = 50.0
    telemetry_hz: float = 20.0
    time_limit: float | None = 30.0
    tests: tuple[JointTest, ...] = ()
    seed: int = 0
    estop_after_s: float | None = None
    estop_latency_limit: float = 0.5
    collision_force_threshold: float = 0.05
    ignore_root_contacts: bool = True
    joint_velocity_tolerance: float = 0.05
    model_path: str = DEFAULT_MODEL_PATH
    urdf_path: str = DEFAULT_URDF_PATH
    ros2_control_path: str = DEFAULT_ROS2_CONTROL_PATH
    # Official-model extensions (all default off for generated rigs):
    # keyframe: reset physics to this MuJoCo key (e.g. MicroDuck "STAND").
    # expected_contact_geoms: geoms allowed to touch (e.g. foot soles).
    # fall_trunk_z: trunk height below which the run records a fall.
    # include_body_poses: stream per-body pos/quat for the 3D renderer.
    # integrator: explicit MuJoCo integrator, else the engine default.
    # spawn_xy/spawn_yaw: duck trunk floor position + facing; keyframe wins
    #   when null. env_layout: (body, x, y) floor overrides for static
    #   environment bodies; z always rests at the composed height.
    keyframe: str | None = None
    expected_contact_geoms: tuple[str, ...] = ()
    fall_trunk_z: float | None = None
    include_body_poses: bool = False
    integrator: str | None = None
    spawn_xy: tuple[float, float] | None = None
    spawn_yaw: float | None = None
    env_layout: tuple[tuple[str, float, float], ...] = ()
    fix_base: bool = False
    locomotion: bool = False
    locomotion_twist: tuple[float, float, float] = (0.2, 0.0, 0.0)
    locomotion_duration_s: float = 20.0
    locomotion_goal_radius: float = 0.3
    goal_geoms: tuple[str, ...] = ()
    objective: RunObjective = field(default_factory=RunObjective)
    #: Ordered objective sequence (1+, no cap). from_dict always sets it;
    #: direct constructions with only ``objective`` fall back to one phase.
    objectives: tuple[RunObjective, ...] = ()

    @property
    def phases(self) -> tuple[RunObjective, ...]:
        """Objective sequence the runner executes in order."""
        return self.objectives if self.objectives else (self.objective,)

    def __post_init__(self) -> None:
        _positive(self.dt, "dt")
        _positive(self.control_hz, "control_hz")
        _positive(self.telemetry_hz, "telemetry_hz")
        if self.control_hz > 1.0 / self.dt + 1e-9:
            raise ScenarioError("control_hz cannot exceed the physics rate.")
        if self.time_limit is not None:
            _positive(self.time_limit, "time_limit")
        if self.estop_after_s is not None and self.estop_after_s < 0:
            raise ScenarioError("estop_after_s must be non-negative or null.")
        _positive(self.estop_latency_limit, "estop_latency_limit")
        if self.collision_force_threshold < 0:
            raise ScenarioError("collision_force_threshold must be non-negative.")
        if self.joint_velocity_tolerance < 0:
            raise ScenarioError("joint_velocity_tolerance must be non-negative.")
        if self.seed < 0:
            raise ScenarioError("seed must be non-negative.")
        if len(self.tests) > MAX_TESTS:
            raise ScenarioError(f"tests may contain at most {MAX_TESTS} cases.")
        if self.keyframe is not None and not self.keyframe.strip():
            raise ScenarioError("keyframe must be non-empty or null.")
        if self.fall_trunk_z is not None and self.fall_trunk_z < 0:
            raise ScenarioError("fall_trunk_z must be non-negative or null.")
        if self.integrator is not None and not self.integrator.strip():
            raise ScenarioError("integrator must be non-empty or null.")
        for name in self.expected_contact_geoms:
            if not str(name).strip():
                raise ScenarioError("expected_contact_geoms must hold names.")
        for name in self.goal_geoms:
            if not str(name).strip():
                raise ScenarioError("goal_geoms must hold names.")
        if len(self.locomotion_twist) != 3:
            raise ScenarioError("locomotion_twist must hold [vx, vy, yaw].")
        for value in self.locomotion_twist:
            _finite(value, "locomotion_twist")
        if abs(self.locomotion_twist[0]) > 1.0 or abs(
            self.locomotion_twist[1]
        ) > 1.0:
            raise ScenarioError(
                "locomotion_twist linear components must stay within ±1 m/s."
            )
        if abs(self.locomotion_twist[2]) > 1.0:
            raise ScenarioError(
                "locomotion_twist yaw must stay within ±1 rad/s."
            )
        _positive(self.locomotion_duration_s, "locomotion_duration_s")
        _positive(self.locomotion_goal_radius, "locomotion_goal_radius")
        if not isinstance(self.objective, RunObjective):
            raise ScenarioError("objective must be a RunObjective.")
        for index, phase in enumerate(self.phases):
            label = f"objectives[{index}]"
            if not isinstance(phase, RunObjective):
                raise ScenarioError(f"{label} must be a RunObjective.")
            if phase.type not in OBJECTIVE_TYPES:
                raise ScenarioError(
                    f"{label}.type must be one of"
                    f" {', '.join(OBJECTIVE_TYPES)}."
                )
            if phase.target is not None:
                for value in phase.target:
                    _layout_coord(value, f"{label}.target")
            if phase.target_body is not None:
                if phase.type != "go_to":
                    raise ScenarioError(
                        f"{label}.target_body is only valid for go_to runs."
                    )
                if not GOAL_BODY_PATTERN.match(phase.target_body):
                    raise ScenarioError(
                        f"{label}.target_body must be a composed prop name"
                        " such as 'env_box_1'."
                    )
            if phase.type == "wander" and index < len(self.phases) - 1:
                raise ScenarioError(
                    "wander completes only at the window end, so it must be"
                    " the last objective in the sequence."
                )
        if self.spawn_xy is not None:
            for value in self.spawn_xy:
                _layout_coord(value, "spawn_xy")
        if self.spawn_yaw is not None:
            _finite(self.spawn_yaw, "spawn_yaw")
        seen_layout: set[str] = set()
        for name, x, y in self.env_layout:
            if not str(name).strip():
                raise ScenarioError("env_layout must name bodies.")
            if name in seen_layout:
                raise ScenarioError(f"Duplicate env_layout body {name!r}.")
            seen_layout.add(name)
            _layout_coord(x, f"env_layout[{name}].x")
            _layout_coord(y, f"env_layout[{name}].y")

    @property
    def physics_steps_per_control(self) -> int:
        return max(1, int(round(1.0 / (self.dt * self.control_hz))))

    @property
    def control_period(self) -> float:
        return 1.0 / self.control_hz

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": BACKEND_MUJOCO,
            "dt": self.dt,
            "control_hz": self.control_hz,
            "telemetry_hz": self.telemetry_hz,
            "time_limit": self.time_limit,
            "seed": self.seed,
            "tests": [test.to_dict() for test in self.tests],
            "estop_after_s": self.estop_after_s,
            "estop_latency_limit": self.estop_latency_limit,
            "collision_force_threshold": self.collision_force_threshold,
            "ignore_root_contacts": self.ignore_root_contacts,
            "joint_velocity_tolerance": self.joint_velocity_tolerance,
            "model_path": self.model_path,
            "urdf_path": self.urdf_path,
            "ros2_control_path": self.ros2_control_path,
            "keyframe": self.keyframe,
            "expected_contact_geoms": list(self.expected_contact_geoms),
            "fall_trunk_z": self.fall_trunk_z,
            "include_body_poses": self.include_body_poses,
            "integrator": self.integrator,
            "spawn_xy": list(self.spawn_xy) if self.spawn_xy else None,
            "spawn_yaw": self.spawn_yaw,
            "env_layout": [
                {"body": name, "x": x, "y": y}
                for name, x, y in self.env_layout
            ],
            "fix_base": self.fix_base,
            "locomotion": self.locomotion,
            "locomotion_twist": list(self.locomotion_twist),
            "locomotion_duration_s": self.locomotion_duration_s,
            "locomotion_goal_radius": self.locomotion_goal_radius,
            "goal_geoms": list(self.goal_geoms),
            "objective": self.objective.to_dict(),
            "objectives": [phase.to_dict() for phase in self.phases],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MujocoScenario:
        if not isinstance(data, dict):
            raise ScenarioError("MuJoCo scenario must be a JSON object.")
        raw_limit = data.get("time_limit", MUJOCO_DEFAULTS["time_limit"])
        raw_estop = data.get("estop_after_s", MUJOCO_DEFAULTS["estop_after_s"])
        raw_keyframe = data.get("keyframe", MUJOCO_DEFAULTS["keyframe"])
        raw_fall = data.get("fall_trunk_z", MUJOCO_DEFAULTS["fall_trunk_z"])
        raw_integrator = data.get("integrator", MUJOCO_DEFAULTS["integrator"])
        raw_expected = data.get(
            "expected_contact_geoms", MUJOCO_DEFAULTS["expected_contact_geoms"]
        )
        if raw_expected is None:
            expected: tuple[str, ...] = ()
        elif isinstance(raw_expected, list):
            expected = tuple(str(name) for name in raw_expected)
        else:
            raise ScenarioError("expected_contact_geoms must be a JSON array.")
        raw_spawn = data.get("spawn_xy", MUJOCO_DEFAULTS["spawn_xy"])
        if raw_spawn is None:
            spawn_xy: tuple[float, float] | None = None
        elif isinstance(raw_spawn, list) and len(raw_spawn) == 2:
            spawn_xy = (
                _layout_coord(raw_spawn[0], "spawn_xy[0]"),
                _layout_coord(raw_spawn[1], "spawn_xy[1]"),
            )
        else:
            raise ScenarioError("spawn_xy must be an [x, y] pair or null.")
        raw_yaw = data.get("spawn_yaw", MUJOCO_DEFAULTS["spawn_yaw"])
        spawn_yaw = None if raw_yaw is None else _finite(raw_yaw, "spawn_yaw")
        raw_layout = data.get("env_layout", MUJOCO_DEFAULTS["env_layout"])
        layout: list[tuple[str, float, float]] = []
        if raw_layout is None:
            pass
        elif isinstance(raw_layout, dict):
            for name, pair in raw_layout.items():
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ScenarioError(
                        f"env_layout[{name!r}] must be an [x, y] pair."
                    )
                layout.append((
                    str(name),
                    _layout_coord(pair[0], f"env_layout[{name}].x"),
                    _layout_coord(pair[1], f"env_layout[{name}].y"),
                ))
        elif isinstance(raw_layout, list):
            for index, entry in enumerate(raw_layout):
                if not isinstance(entry, dict):
                    raise ScenarioError(
                        f"env_layout[{index}] must be an object."
                    )
                layout.append((
                    str(entry.get("body", "")),
                    _layout_coord(entry.get("x"), f"env_layout[{index}].x"),
                    _layout_coord(entry.get("y"), f"env_layout[{index}].y"),
                ))
        else:
            raise ScenarioError(
                "env_layout must be an object or an array, or null."
            )
        return cls(
            dt=_finite(data.get("dt", MUJOCO_DEFAULTS["dt"]), "dt"),
            control_hz=_finite(
                data.get("control_hz", MUJOCO_DEFAULTS["control_hz"]),
                "control_hz",
            ),
            telemetry_hz=_finite(
                data.get("telemetry_hz", MUJOCO_DEFAULTS["telemetry_hz"]),
                "telemetry_hz",
            ),
            time_limit=None if raw_limit is None else _finite(raw_limit, "time_limit"),
            tests=parse_tests(data.get("tests")),
            seed=int(data.get("seed", 0)),
            estop_after_s=(
                None if raw_estop is None else _finite(raw_estop, "estop_after_s")
            ),
            estop_latency_limit=_finite(
                data.get(
                    "estop_latency_limit", MUJOCO_DEFAULTS["estop_latency_limit"]
                ),
                "estop_latency_limit",
            ),
            collision_force_threshold=_finite(
                data.get(
                    "collision_force_threshold",
                    MUJOCO_DEFAULTS["collision_force_threshold"],
                ),
                "collision_force_threshold",
            ),
            ignore_root_contacts=bool(
                data.get(
                    "ignore_root_contacts",
                    MUJOCO_DEFAULTS["ignore_root_contacts"],
                )
            ),
            joint_velocity_tolerance=_finite(
                data.get(
                    "joint_velocity_tolerance",
                    MUJOCO_DEFAULTS["joint_velocity_tolerance"],
                ),
                "joint_velocity_tolerance",
            ),
            model_path=safe_relative_path(
                str(data.get("model_path") or DEFAULT_MODEL_PATH), "model_path"
            ),
            urdf_path=safe_relative_path(
                str(data.get("urdf_path") or DEFAULT_URDF_PATH), "urdf_path"
            ),
            ros2_control_path=safe_relative_path(
                str(data.get("ros2_control_path") or DEFAULT_ROS2_CONTROL_PATH),
                "ros2_control_path",
            ),
            keyframe=(
                None if raw_keyframe is None else str(raw_keyframe).strip()
            ),
            expected_contact_geoms=expected,
            fall_trunk_z=(
                None if raw_fall is None else _finite(raw_fall, "fall_trunk_z")
            ),
            include_body_poses=bool(
                data.get(
                    "include_body_poses",
                    MUJOCO_DEFAULTS["include_body_poses"],
                )
            ),
            integrator=(
                None if raw_integrator is None else str(raw_integrator).strip()
            ),
            spawn_xy=spawn_xy,
            spawn_yaw=spawn_yaw,
            env_layout=tuple(layout),
            fix_base=bool(
                data.get("fix_base", MUJOCO_DEFAULTS["fix_base"])
            ),
            locomotion=bool(
                data.get("locomotion", MUJOCO_DEFAULTS["locomotion"])
            ),
            locomotion_twist=_twist_triple(
                data.get(
                    "locomotion_twist", MUJOCO_DEFAULTS["locomotion_twist"]
                )
            ),
            locomotion_duration_s=_finite(
                data.get(
                    "locomotion_duration_s",
                    MUJOCO_DEFAULTS["locomotion_duration_s"],
                ),
                "locomotion_duration_s",
            ),
            locomotion_goal_radius=_finite(
                data.get(
                    "locomotion_goal_radius",
                    MUJOCO_DEFAULTS["locomotion_goal_radius"],
                ),
                "locomotion_goal_radius",
            ),
            goal_geoms=_name_tuple(data.get("goal_geoms"), "goal_geoms"),
            **_objectives_from_data(data),
        )


def _objectives_from_data(data: dict[str, Any]) -> dict[str, Any]:
    """Parse the objective sequence (list wins, single is one phase)."""
    raw = data.get("objectives", None)
    if raw is None:
        raw = data.get("objective", MUJOCO_DEFAULTS["objective"])
    phases = parse_objectives(raw)
    return {"objective": phases[0], "objectives": phases}


def default_joint_target(
    lower: float | None, upper: float | None
) -> tuple[float, float]:
    """Derive (home, target) for an auto-generated joint test.

    Home is where the joint rests (0 when inside the range, else the nearest
    bound); the target is a conservative excursion taken in the direction with
    more travel, so it never reaches a joint limit and an honest controller can
    satisfy it.
    """
    if lower is None or upper is None:
        return 0.0, 0.5
    lo, hi = float(lower), float(upper)
    if hi <= lo:
        raise ScenarioError("Joint range must be increasing.")
    home = 0.0 if lo <= 0.0 <= hi else (lo if abs(lo) < abs(hi) else hi)
    up_room = hi - home
    down_room = home - lo
    if up_room >= down_room:
        excursion = min(0.5, max(0.0, up_room * 0.5))
        target = home + excursion
    else:
        excursion = min(0.5, max(0.0, down_room * 0.5))
        target = home - excursion
    return home, target


def default_tests_for_joints(
    joints: list[tuple[str, float | None, float | None]],
) -> tuple[JointTest, ...]:
    """Build the default automated suite from the robot's actuated joints."""
    if not joints:
        raise ScenarioError(
            "The robot has no actuated joints; a MuJoCo test suite needs at"
            " least one position actuator."
        )
    tests: list[JointTest] = []
    for name, lower, upper in joints:
        home, target = default_joint_target(lower, upper)
        if abs(target - home) < 1e-6:
            continue
        tests.append(
            JointTest(
                id=f"{name}_to_{target:+.2f}rad".replace("+", ""),
                joint=name,
                target=round(target, 6),
            )
        )
    if not tests:
        raise ScenarioError(
            "Could not derive a safe joint excursion from the robot ranges;"
            " supply explicit tests."
        )
    return tuple(tests)
