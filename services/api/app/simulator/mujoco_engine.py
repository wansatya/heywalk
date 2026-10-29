"""MuJoCo physics engine for generated robots (first simulation backend).

The engine loads a validated MJCF model, exposes the ros2_control position
command interface (``set_commands``), steps physics at a fixed timestep, and
reports joint state plus collision/contact evidence. It owns no evaluation
logic: the joint-test runner and the evaluator judge outcomes from the state
this engine reports.

Untrusted-input safety: the MJCF is parsed before loading to reject
DOCTYPE/ENTITY declarations, ``<include>`` elements, and absolute or
parent-traversing asset paths. MuJoCo then loads the file from inside the
run workspace only.
"""

from __future__ import annotations

import contextlib
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mujoco
import numpy as np

from .robot_description import (
    RobotInfo,
    RobotPackageError,
    parse_xml,
    safe_relative_asset_path,
)
from .scenario import MAX_JOINT_TARGET, MujocoScenario


class MujocoEngineError(RuntimeError):
    """Raised when the robot cannot be simulated in MuJoCo."""


#: Integrator used unless the generated model names one explicitly.
#: MuJoCo's explicit Euler integrator cannot carry the stiff position-servo
#: damping of a typical generated arm at small timesteps; ``implicitfast``
#: integrates joint damping/actuator velocity feedback implicitly, which keeps
#: the joint tests representative instead of numerically divergent.
#: Official models may override this per scenario (MicroDuck uses Euler at
#: dt=0.005, matching the official runtime, which sets no integrator).
DEFAULT_INTEGRATOR = "implicitfast"

#: Scenario ``integrator`` names mapped to MuJoCo constants.
INTEGRATORS = {
    "euler": "mjINT_EULER",
    "implicit": "mjINT_IMPLICIT",
    "implicitfast": "mjINT_IMPLICITFAST",
    "rk4": "mjINT_RK4",
}


@dataclass(frozen=True)
class JointBinding:
    """Maps a declared joint to its MuJoCo addresses and actuator."""

    name: str
    type: str
    actuator_index: int
    qpos_address: int
    dof_address: int
    lower: float | None
    upper: float | None

    def clamp(self, value: float) -> float:
        if not math.isfinite(value):
            return 0.0
        if self.lower is not None and self.upper is not None:
            return float(min(max(value, self.lower), self.upper))
        return float(min(max(value, -MAX_JOINT_TARGET), MAX_JOINT_TARGET))


def validate_model_paths(xml_text: str, model_path: str) -> None:
    """Reject includes and escaping asset paths before MuJoCo loads the file."""
    root = parse_xml(xml_text, f"MJCF model {model_path!r}")
    if root.tag != "mujoco":
        raise MujocoEngineError(
            f"{model_path!r} must be an MJCF model (<mujoco> root)."
        )
    if root.findall("include"):
        raise MujocoEngineError(
            "MJCF <include> elements are not allowed in generated robot"
            " descriptions; inline the model into one file."
        )
    for element in root.iter():
        for attr in ("meshdir", "texturedir", "assetdir"):
            value = element.get(attr)
            if value:
                try:
                    safe_relative_asset_path(value, f"{attr} in {model_path!r}")
                except RobotPackageError as exc:
                    raise MujocoEngineError(str(exc)) from None
        file_attr = element.get("file")
        if file_attr:
            try:
                safe_relative_asset_path(
                    file_attr, f"file attribute in {model_path!r}"
                )
            except RobotPackageError as exc:
                raise MujocoEngineError(str(exc)) from None


def _declares_integrator(xml_text: str) -> bool:
    """True when the MJCF explicitly chooses an integrator."""
    root = parse_xml(xml_text, "MJCF model")
    option = root.find("option")
    return option is not None and bool((option.get("integrator") or "").strip())


def _quat_to_yaw(quat: np.ndarray) -> float:
    w, x, y, z = (float(quat[0]), float(quat[1]), float(quat[2]), float(quat[3]))
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def _weld_free_joints(xml_text: str) -> tuple[str, bool]:
    """Lock the robot's floating base so the model runs on a test stand.

    Removes the robot's ``<freejoint/>`` (its body welds to the parent at the
    declared frame) and drops the matching leading keyframe qpos slots. Free
    *props* (the composed ball) keep their free joints: they carry physics,
    not the robot's pose, so a joint test still sees a ball that can roll
    after being kicked. The supported shape is exactly one robot free joint,
    first in joint order, followed only by hinge/slide joints and prop free
    joints — anything else raises instead of silently shifting keyframe
    values onto the wrong joints.
    """
    root = parse_xml(xml_text, "MJCF model")
    worldbody = root.find("worldbody")
    if worldbody is None:
        return xml_text, False
    joints = [
        element
        for element in worldbody.iter()
        if element.tag in ("joint", "freejoint")
    ]
    free = [element for element in joints if element.tag == "freejoint"]
    if not free:
        return xml_text, False
    if joints[0].tag != "freejoint":
        raise MujocoEngineError(
            "fix_base needs the robot's free joint to come first; found it"
            " after another joint."
        )
    # Only the first free joint (the trunk) is welded. Free props keep their
    # own joints and keyframe slots, so their qpos stays aligned.
    for parent in worldbody.iter():
        removed = False
        for child in list(parent):
            if child.tag == "freejoint":
                parent.remove(child)
                removed = True
                break
        if removed:
            break
    for key in root.iter("key"):
        raw = (key.get("qpos") or "").strip()
        if not raw:
            continue
        values = raw.split()
        if len(values) < 7:
            raise MujocoEngineError(
                "Keyframe qpos is shorter than its free joint."
            )
        key.set("qpos", " ".join(values[7:]))
    return ET.tostring(root, encoding="unicode"), True


class MujocoEngine:
    """Fixed-timestep MuJoCo model with position command interfaces."""

    def __init__(
        self,
        model_path: str | Path,
        info: RobotInfo,
        *,
        scenario: MujocoScenario,
    ) -> None:
        self.path = Path(model_path)
        self.info = info
        self.scenario = scenario
        try:
            xml_text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MujocoEngineError(
                f"Cannot read robot model {self.path.name!r}: {exc}."
            ) from None
        validate_model_paths(xml_text, self.path.name)
        self.base_fixed = False
        if scenario.fix_base:
            xml_text, self.base_fixed = _weld_free_joints(xml_text)
        try:
            if self.base_fixed:
                self.model = self._load_welded_model(xml_text)
            else:
                self.model = mujoco.MjModel.from_xml_path(str(self.path))
        except Exception as exc:  # mujoco raises ValueError with detail
            raise MujocoEngineError(
                f"MuJoCo rejected the robot model: {exc}"
            ) from None
        self.data = mujoco.MjData(self.model)
        try:
            self.model.opt.timestep = scenario.dt
        except Exception:
            pass
        self._apply_integrator(xml_text, scenario)
        self.layout_skipped = self._apply_env_layout()
        self.bindings = self._bind_joints(info)
        self._free_qpos, self._free_dof = self._find_free_joint()
        self._env_free_qpos = self._resolve_env_free_qpos()
        self._robot_bodies = self._resolve_robot_bodies()
        self._root_body = self._resolve_root_body()
        self._keyframe_id = self._resolve_keyframe(scenario)
        self._expected_geoms = self._resolve_expected_geoms(scenario)
        self._goal_geoms = self._resolve_goal_geoms(scenario)
        self.sensors = self._discover_sensors()
        self._body_names = self._cache_body_names()
        # Reused by _update_contacts so the per-tick contact scan allocates
        # nothing (mj_contactForce overwrites it on every call).
        self._contact_force = np.zeros(6, dtype=float)
        self.home = np.array(
            [self._home_of(binding) for binding in self.bindings], dtype=float
        )
        self.commands = self.home.copy()
        self.contacts = 0
        self.expected_contacts = 0
        self.goal_contacts = 0
        self.goal_touch_geoms: set[str] = set()
        self.max_contact_force = 0.0
        self.collision = False
        self.fallen = False
        self.trunk_z = 0.0
        self.reset()

    # ---- construction helpers ----

    def _bind_joints(self, info: RobotInfo) -> tuple[JointBinding, ...]:
        if self.model.nu < len(info.joints):
            raise MujocoEngineError(
                "MuJoCo compiled fewer actuators than the robot declares."
            )
        bindings: list[JointBinding] = []
        for index, joint in enumerate(info.joints):
            joint_id = mujoco.mj_name2id(
                self.model, mujoco.mjtObj.mjOBJ_JOINT, joint.name
            )
            if joint_id < 0:
                raise MujocoEngineError(
                    f"Joint {joint.name!r} is missing from the compiled model."
                )
            jnt_type = int(self.model.jnt_type[joint_id])
            expected = (
                mujoco.mjtJoint.mjJNT_HINGE
                if joint.type == "hinge"
                else mujoco.mjtJoint.mjJNT_SLIDE
            )
            if jnt_type != int(expected):
                raise MujocoEngineError(
                    f"Joint {joint.name!r} compiled as a different type than"
                    f" {joint.type!r}."
                )
            actuator_name = mujoco.mj_id2name(
                self.model, mujoco.mjtObj.mjOBJ_ACTUATOR, index
            )
            if actuator_name and actuator_name != joint.actuator:
                raise MujocoEngineError(
                    f"Actuator ordering mismatch: expected {joint.actuator!r}"
                    f" at index {index}, found {actuator_name!r}."
                )
            actuator_joint_id = int(self.model.actuator_trnid[index][0])
            if actuator_joint_id != joint_id:
                raise MujocoEngineError(
                    f"Actuator {joint.actuator!r} does not drive joint"
                    f" {joint.name!r}."
                )
            bindings.append(
                JointBinding(
                    name=joint.name,
                    type=joint.type,
                    actuator_index=index,
                    qpos_address=int(self.model.jnt_qposadr[joint_id]),
                    dof_address=int(self.model.jnt_dofadr[joint_id]),
                    lower=joint.lower,
                    upper=joint.upper,
                )
            )
        return tuple(bindings)

    def _find_free_joint(self) -> tuple[int | None, int | None]:
        """qpos/dof addresses of the ROBOT's floating base, if any.

        Free props (the ball) also have free joints, so this resolves the
        trunk body's own free joint explicitly. A welded test stand has none
        and reports FK poses instead of a prop's position.
        """
        from .microduck.model import TRUNK_BODY

        trunk = mujoco.mj_name2id(
            self.model, mujoco.mjtObj.mjOBJ_BODY, TRUNK_BODY
        )
        for joint_id in range(self.model.njnt):
            if int(self.model.jnt_type[joint_id]) != int(
                mujoco.mjtJoint.mjJNT_FREE
            ):
                continue
            if trunk >= 0 and int(self.model.jnt_bodyid[joint_id]) != trunk:
                continue
            return (
                int(self.model.jnt_qposadr[joint_id]),
                int(self.model.jnt_dofadr[joint_id]),
            )
        return None, None

    def _resolve_env_free_qpos(self) -> tuple[tuple[int, int], ...]:
        """(body id, qpos address) of free environment props (the ball)."""
        from .microduck.model import ENV_BODY_PREFIX

        found: list[tuple[int, int]] = []
        for joint_id in range(self.model.njnt):
            if int(self.model.jnt_type[joint_id]) != int(
                mujoco.mjtJoint.mjJNT_FREE
            ):
                continue
            body_id = int(self.model.jnt_bodyid[joint_id])
            name = (
                mujoco.mj_id2name(
                    self.model, mujoco.mjtObj.mjOBJ_BODY, body_id
                )
                or ""
            )
            if name.startswith(ENV_BODY_PREFIX):
                found.append(
                    (body_id, int(self.model.jnt_qposadr[joint_id]))
                )
        return tuple(found)

    def _resolve_robot_bodies(self) -> frozenset[int]:
        """Bodies whose contacts count as robot evidence.

        The official-model composition adds static environment bodies
        (obstacle crates, ball, goal flag); those are not the robot, so a
        resting ball must never count as a collision. Robot-obstacle
        touches still count: only one side of the contact needs to be a
        robot body (see ``_update_contacts``).
        """
        from .microduck.model import ENV_BODY_PREFIX

        robot: set[int] = set()
        for body_id in range(1, self.model.nbody):
            name = (
                mujoco.mj_id2name(
                    self.model, mujoco.mjtObj.mjOBJ_BODY, body_id
                )
                or ""
            )
            if not name.startswith(ENV_BODY_PREFIX):
                robot.add(body_id)
        return frozenset(robot)

    def _resolve_root_body(self) -> int:
        """Trunk body id for the fall detector (official trunk by name)."""
        from .microduck.model import TRUNK_BODY

        if self.model.nbody <= 1:
            return 0
        trunk = mujoco.mj_name2id(
            self.model, mujoco.mjtObj.mjOBJ_BODY, TRUNK_BODY
        )
        return int(trunk) if trunk >= 0 else 1

    def _apply_env_layout(self) -> tuple[str, ...]:
        """Move static environment bodies to their arranged floor spots.

        Runs before the first forward pass so physics starts from the
        arranged scene. Entries naming bodies the model does not have are
        skipped (a generated rig has no composed scene); entries naming
        real robot bodies are rejected, since moving them would silently
        break joint/actuator bindings.
        """
        from .microduck.model import ENV_BODY_PREFIX, WALL_BODIES

        skipped: list[str] = []
        for name, x, y in self.scenario.env_layout:
            if name in WALL_BODIES:
                # Arena bounds are fixed; a stale layout naming them must
                # not move (or crash) the run.
                skipped.append(name)
                continue
            body_id = mujoco.mj_name2id(
                self.model, mujoco.mjtObj.mjOBJ_BODY, name
            )
            if body_id < 0:
                skipped.append(name)
                continue
            if not name.startswith(ENV_BODY_PREFIX):
                raise MujocoEngineError(
                    f"Scene layout cannot move robot body {name!r}; only"
                    f" {ENV_BODY_PREFIX}* environment bodies are movable."
                )
            self.model.body_pos[body_id][0] = x
            self.model.body_pos[body_id][1] = y
        return tuple(skipped)

    def _load_welded_model(self, xml_text: str) -> mujoco.MjModel:
        """Compile the welded XML without writing beside the model.

        Isolated runners mount the workspace read-only, so the welded
        variant compiles from a string with the working directory parked
        on the model folder (MuJoCo resolves relative mesh paths from
        the current directory for string loads). Restored afterwards.
        """
        with contextlib.chdir(self.path.parent):
            return mujoco.MjModel.from_xml_string(xml_text)

    def _apply_integrator(self, xml_text: str, scenario: Any) -> None:
        """Apply the scenario integrator, else the engine default."""
        requested = (scenario.integrator or "").strip().lower()
        if requested:
            constant = INTEGRATORS.get(requested)
            if constant is None:
                raise MujocoEngineError(
                    f"Unknown integrator {scenario.integrator!r}; expected one"
                    f" of {', '.join(sorted(INTEGRATORS))}."
                )
            try:
                self.model.opt.integrator = int(
                    getattr(mujoco.mjtIntegrator, constant)
                )
            except Exception as exc:
                raise MujocoEngineError(
                    f"Could not apply integrator {requested!r}: {exc}."
                ) from None
        elif not _declares_integrator(xml_text):
            try:
                self.model.opt.integrator = int(
                    mujoco.mjtIntegrator.mjINT_IMPLICITFAST
                )
            except Exception:
                pass

    def _resolve_keyframe(self, scenario: Any) -> int | None:
        """Resolve the scenario reset keyframe, if any."""
        name = (scenario.keyframe or "").strip()
        if not name:
            return None
        key_id = mujoco.mj_name2id(
            self.model, mujoco.mjtObj.mjOBJ_KEY, name
        )
        if key_id < 0:
            raise MujocoEngineError(
                f"Reset keyframe {name!r} is not defined by the model."
            )
        return int(key_id)

    def _resolve_expected_geoms(self, scenario: Any) -> frozenset[int]:
        """Resolve expected-contact geom names (e.g. foot soles)."""
        names = list(scenario.expected_contact_geoms or ())
        if not names:
            return frozenset()
        resolved: set[int] = set()
        for name in names:
            geom_id = mujoco.mj_name2id(
                self.model, mujoco.mjtObj.mjOBJ_GEOM, str(name)
            )
            if geom_id < 0:
                raise MujocoEngineError(
                    f"Expected-contact geom {name!r} is not in the model."
                )
            resolved.add(int(geom_id))
        return frozenset(resolved)

    def _resolve_goal_geoms(self, scenario: Any) -> dict[int, str]:
        """Resolve goal geom names (e.g. the flag); touching them is the aim.

        Returns geom-id -> name so per-tick touches can name WHAT the
        robot touched (a wall touch passes a touch run; a flag brush on
        the way there must not).
        """
        names = list(scenario.goal_geoms or ())
        if not names:
            return {}
        resolved: dict[int, str] = {}
        for name in names:
            geom_id = mujoco.mj_name2id(
                self.model, mujoco.mjtObj.mjOBJ_GEOM, str(name)
            )
            if geom_id < 0:
                raise MujocoEngineError(
                    f"Goal geom {name!r} is not in the model."
                )
            resolved[int(geom_id)] = str(name)
        return resolved

    def body_position(self, name: str) -> tuple[float, float, float] | None:
        """Live world position of a body (None when the model lacks it)."""
        body_id = mujoco.mj_name2id(
            self.model, mujoco.mjtObj.mjOBJ_BODY, str(name)
        )
        if body_id < 0:
            return None
        pos = self.data.xpos[int(body_id)]
        return float(pos[0]), float(pos[1]), float(pos[2])

    def _discover_sensors(self) -> tuple[dict[str, Any], ...]:
        """Describe the model's sensors (name, type, dimension)."""
        found: list[dict[str, Any]] = []
        for index in range(self.model.nsensor):
            name = (
                mujoco.mj_id2name(
                    self.model, mujoco.mjtObj.mjOBJ_SENSOR, index
                )
                or f"sensor_{index}"
            )
            address = int(self.model.sensor_adr[index])
            dim = int(self.model.sensor_dim[index])
            found.append({"name": name, "dim": dim, "address": address})
        return tuple(found)

    def _home_of(self, binding: JointBinding) -> float:
        if self._keyframe_id is not None:
            value = float(
                self.model.key_qpos[self._keyframe_id][binding.qpos_address]
            )
            return binding.clamp(value)
        lower, upper = binding.lower, binding.upper
        if lower is None or upper is None:
            return 0.0
        if lower <= 0.0 <= upper:
            return 0.0
        return lower if abs(lower) < abs(upper) else upper

    # ---- lifecycle ----

    def reset(self) -> None:
        """Reset physics to the home pose with commands held at home."""
        mujoco.mj_resetData(self.model, self.data)
        if self._keyframe_id is not None:
            self.data.qpos[:] = self.model.key_qpos[self._keyframe_id]
        else:
            self.data.qpos[:] = self.model.qpos0
        for binding, home in zip(self.bindings, self.home):
            self.data.qpos[binding.qpos_address] = home
        self._apply_spawn_pose()
        self._apply_env_free_poses()
        self.commands = self.home.copy()
        self.data.ctrl[:] = self.commands
        mujoco.mj_forward(self.model, self.data)
        self.contacts = 0
        self.expected_contacts = 0
        self.goal_contacts = 0
        self.goal_touch_geoms = set()
        self.max_contact_force = 0.0
        self.collision = False
        self.fallen = False
        self._update_trunk_height()

    def _apply_spawn_pose(self) -> None:
        """Move the base to its arranged spawn (if any).

        Only the floor position and facing change; height and joints keep
        the keyframe values so the duck still starts in STAND. Free bases
        move through the free joint; welded test stands slide the whole
        stand by moving the root frame instead.
        """
        if self._free_qpos is not None:
            address = self._free_qpos
            if self.scenario.spawn_xy is not None:
                self.data.qpos[address] = self.scenario.spawn_xy[0]
                self.data.qpos[address + 1] = self.scenario.spawn_xy[1]
            if self.scenario.spawn_yaw is not None:
                half = self.scenario.spawn_yaw / 2.0
                self.data.qpos[address + 3] = math.cos(half)
                self.data.qpos[address + 4] = 0.0
                self.data.qpos[address + 5] = 0.0
                self.data.qpos[address + 6] = math.sin(half)
            return
        if self.scenario.spawn_xy is not None:
            self.model.body_pos[self._root_body][0] = self.scenario.spawn_xy[0]
            self.model.body_pos[self._root_body][1] = self.scenario.spawn_xy[1]
        if self.scenario.spawn_yaw is not None:
            half = self.scenario.spawn_yaw / 2.0
            self.model.body_quat[self._root_body][0] = math.cos(half)
            self.model.body_quat[self._root_body][1] = 0.0
            self.model.body_quat[self._root_body][2] = 0.0
            self.model.body_quat[self._root_body][3] = math.sin(half)

    def _apply_env_free_poses(self) -> None:
        """Start free props at their composed/arranged floor spot.

        A free prop's pose lives in qpos, so resetting to a keyframe would
        otherwise drop the arranged ball back to the keyframe value. The
        model's ``body_pos`` is the composed position unless the arrange UI
        moved it (``_apply_env_layout``), so it is the authoritative start.
        """
        for body_id, address in self._env_free_qpos:
            self.data.qpos[address : address + 3] = self.model.body_pos[body_id]
            self.data.qpos[address + 3 : address + 7] = (1.0, 0.0, 0.0, 0.0)

    # ---- commands / state ----

    @property
    def joint_count(self) -> int:
        return len(self.bindings)

    @property
    def joint_names(self) -> tuple[str, ...]:
        """Actuated joint names in command order (RobotAdapter contract)."""
        return tuple(binding.name for binding in self.bindings)

    @property
    def time(self) -> float:
        return float(self.data.time)

    def set_commands(self, commands: list[float] | tuple[float, ...]) -> bool:
        """Apply ros2_control position commands; return False on length error."""
        if len(commands) != self.joint_count:
            return False
        for binding, value in zip(self.bindings, commands):
            ctrl = binding.clamp(float(value))
            self.data.ctrl[binding.actuator_index] = ctrl
        self.commands = np.array(
            [binding.clamp(float(v)) for binding, v in zip(self.bindings, commands)]
        )
        return True

    def hold_current(self) -> None:
        """Emergency-stop behaviour: command the current pose, no motion."""
        self.set_commands(self.joint_positions())

    def step(self) -> None:
        mujoco.mj_step(self.model, self.data)
        self._update_contacts()

    def advance(self, steps: int) -> None:
        for _ in range(max(0, steps)):
            mujoco.mj_step(self.model, self.data)
        self._update_contacts()

    def joint_positions(self) -> list[float]:
        return [
            float(self.data.qpos[binding.qpos_address])
            for binding in self.bindings
        ]

    def joint_velocities(self) -> list[float]:
        return [
            float(self.data.qvel[binding.dof_address])
            for binding in self.bindings
        ]

    def joint_efforts(self) -> list[float]:
        return [
            float(self.data.actuator_force[binding.actuator_index])
            for binding in self.bindings
        ]

    def base_pose(self) -> tuple[float, float, float, float, float]:
        """(x, y, yaw, v, omega) of the base; welded stands report FK."""
        if self._free_qpos is None or self._free_dof is None:
            trunk = self.data.xpos[self._root_body]
            yaw = _quat_to_yaw(self.data.xquat[self._root_body])
            return float(trunk[0]), float(trunk[1]), yaw, 0.0, 0.0
        address = self._free_qpos
        x = float(self.data.qpos[address])
        y = float(self.data.qpos[address + 1])
        yaw = _quat_to_yaw(self.data.qpos[address + 3 : address + 7])
        dof = self._free_dof
        v = float(np.linalg.norm(self.data.qvel[dof : dof + 2]))
        omega = float(self.data.qvel[dof + 5])
        return x, y, yaw, v, omega

    def root_orientation(self) -> list[float]:
        """Trunk orientation quaternion (w, x, y, z) for policy observations."""
        return [float(v) for v in self.data.xquat[self._root_body]]

    def sensor_readings(self) -> dict[str, list[float]]:
        """Current sensor values keyed by sensor name (official models)."""
        readings: dict[str, list[float]] = {}
        for sensor in self.sensors:
            address = sensor["address"]
            dim = sensor["dim"]
            readings[sensor["name"]] = [
                float(value)
                for value in self.data.sensordata[address : address + dim]
            ]
        return readings

    def _cache_body_names(self) -> tuple[str, ...]:
        """Body names indexed by body id (index 0 unused: the world body).

        Model names never change after compilation, so the per-tick pose
        snapshot reuses this instead of calling mj_id2name per body.
        """
        return tuple(
            mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_BODY, body_id)
            or f"body_{body_id}"
            for body_id in range(self.model.nbody)
        )

    def body_poses(self) -> list[dict[str, Any]]:
        """Live body poses (name, pos, quat) for the 3D renderer."""
        poses: list[dict[str, Any]] = []
        for body_id in range(1, self.model.nbody):
            poses.append(
                {
                    "body": self._body_names[body_id],
                    "pos": [float(v) for v in self.data.xpos[body_id]],
                    "quat": [float(v) for v in self.data.xquat[body_id]],
                }
            )
        return poses

    # ---- contacts ----

    def _update_trunk_height(self) -> None:
        """Refresh the fall detector from the root-body height."""
        limit = self.scenario.fall_trunk_z
        if limit is None:
            return
        self.trunk_z = float(self.data.xpos[self._root_body][2])
        if self.trunk_z < limit:
            self.fallen = True

    def _update_contacts(self) -> None:
        threshold = self.scenario.collision_force_threshold
        self.contacts = 0
        self.expected_contacts = 0
        self.goal_contacts = 0
        self.goal_touch_geoms = set()
        self.max_contact_force = 0.0
        for index in range(self.data.ncon):
            contact = self.data.contact[index]
            geom1, geom2 = int(contact.geom1), int(contact.geom2)
            body1 = int(self.model.geom_bodyid[geom1])
            body2 = int(self.model.geom_bodyid[geom2])
            if body1 not in self._robot_bodies and body2 not in self._robot_bodies:
                continue
            if geom1 in self._goal_geoms or geom2 in self._goal_geoms:
                # Touching the goal (e.g. the flag, or a wall on touch
                # runs) is the objective of a locomotion run, never a
                # disqualifying collision. Counted separately, with names,
                # so touch objectives can detect the moment of contact.
                self.goal_contacts += 1
                for geom_id in (geom1, geom2):
                    geom_name = self._goal_geoms.get(geom_id)
                    if geom_name is not None:
                        self.goal_touch_geoms.add(geom_name)
                continue
            if (
                self.scenario.ignore_root_contacts
                and {body1, body2} == {0, self._root_body}
            ):
                continue
            force = self._contact_force
            mujoco.mj_contactForce(self.model, self.data, index, force)
            magnitude = float(np.linalg.norm(force[:3]))
            if magnitude < threshold:
                continue
            if self._is_expected_contact(geom1, geom2, body1, body2):
                self.expected_contacts += 1
                continue
            self.contacts += 1
            self.max_contact_force = max(self.max_contact_force, magnitude)
        self._update_trunk_height()
        self.collision = self.contacts > 0 or self.fallen

    def _is_expected_contact(
        self, geom1: int, geom2: int, body1: int, body2: int
    ) -> bool:
        """True for allowed ground touches (e.g. foot soles on the floor)."""
        if not self._expected_geoms:
            return False
        expected_side = (
            geom1 if geom1 in self._expected_geoms else geom2
        )
        if expected_side not in self._expected_geoms:
            return False
        other_body = body2 if expected_side == geom1 else body1
        return other_body == 0

    # ---- telemetry ----

    def snapshot(self) -> dict[str, Any]:
        positions = self.joint_positions()
        velocities = self.joint_velocities()
        efforts = self.joint_efforts()
        x, y, yaw, v, omega = self.base_pose()
        sample: dict[str, Any] = {
            "t": self.time,
            "x": x,
            "y": y,
            "theta": yaw,
            "v": v,
            "omega": omega,
            "joints": [
                {
                    "name": binding.name,
                    "position": position,
                    "velocity": velocity,
                    "effort": effort,
                    "target": float(command),
                }
                for binding, position, velocity, effort, command in zip(
                    self.bindings,
                    positions,
                    velocities,
                    efforts,
                    self.commands,
                )
            ],
            "collision": self.collision,
            "contacts": self.contacts,
            "max_contact_force": self.max_contact_force,
            "fallen": self.fallen,
            "trunk_z": self.trunk_z,
            "expected_contacts": self.expected_contacts,
        }
        if self.sensors:
            sample["sensors"] = self.sensor_readings()
        if self.scenario.include_body_poses:
            sample["bodies"] = self.body_poses()
        return sample

    def close(self) -> None:
        """MuJoCo models are freed by the GC; nothing to release explicitly."""
        self.data = None  # type: ignore[assignment]
