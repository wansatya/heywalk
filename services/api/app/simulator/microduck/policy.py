"""Upstream locomotion policy driver (velstand.onnx).

A scripted open-loop gait cannot walk the free-standing MicroDuck: without
feedback every step pattern faceplants within a second. The harness therefore
runs the official upstream velocity/stand policy as its low-level walking
controller. The policy is used exactly as trained:

- input: 61-D observation, raw physical units, no normalization
- output: 14-D position-target residual added to the STAND keyframe
- rate: 50 Hz, matching the official control timestep

Observation layout (see pollen-robotics/microduck-policies + mjlab
``microduck_velocity_env_cfg.py``)::

    [0:3]    trunk angular velocity, trunk frame (imu_ang_vel, rad/s)
    [3:6]    projected gravity: world -Z expressed in the trunk frame
    [6:20]   joint positions minus STAND (rad, actuator order)
    [20:34]  joint velocities (rad/s, actuator order)
    [34:48]  previous raw network output (zeros on the first step)
    [48:51]  twist command: forward/strafe/yaw velocity (m/s, m/s, rad/s)
    [51:55]  head command: 4-D deltas from HOME (zeros = look ahead)
    [55:61]  body command: 6-D torso offset deltas (zeros = nominal posture)

Joint order is the MJCF actuator order (left leg, neck/head, right leg).
``rollout()`` refuses to run when the engine reports any other order.
"""

from __future__ import annotations

from pathlib import Path
from functools import lru_cache
from typing import Any

import numpy as np

try:
    import onnxruntime as ort
except ImportError:  # pragma: no cover - exercised via missing-dep path
    ort = None  # type: ignore[assignment]

from .model import stand_joint_targets

POLICY_FILENAME = "velstand.onnx"
OBS_DIM = 61
ACT_DIM = 14
POLICY_HZ = 50.0

#: MJCF actuator order; obs/action slots follow this order.
ACTUATOR_ORDER: tuple[str, ...] = (
    "left_hip_yaw",
    "left_hip_roll",
    "left_hip_pitch",
    "left_knee",
    "left_ankle",
    "neck_pitch",
    "head_pitch",
    "head_yaw",
    "head_roll",
    "right_hip_yaw",
    "right_hip_roll",
    "right_hip_pitch",
    "right_knee",
    "right_ankle",
)


class PolicyError(RuntimeError):
    """The locomotion policy cannot run in this environment."""


def policy_path() -> Path:
    """Filesystem path of the vendored ``velstand.onnx`` file."""
    return Path(__file__).resolve().parent / POLICY_FILENAME


@lru_cache(maxsize=1)
def _stand_pose_template() -> np.ndarray:
    pose = np.array([angle for _, angle in stand_joint_targets()], dtype=float)
    pose.flags.writeable = False
    return pose


def stand_pose() -> np.ndarray:
    """STAND joint angles in actuator order (the policy's zero point).

    Cached: resolving the angles parses the vendored MJCF files, which
    measured 1.3 ms per call — 6% of a 20 ms control period, and this is
    called once per tick from ``build_observation``. Returns a fresh copy so
    callers may modify it.
    """
    return _stand_pose_template().copy()


def projected_gravity(quat: Any) -> np.ndarray:
    """World -Z expressed in the body frame of quaternion ``quat`` (w,x,y,z).

    This is mjlab's ``projected_gravity`` observation: the policy reads the
    trunk tilt from it instead of from raw accelerometer values.
    """
    w, x, y, z = (float(v) for v in quat)
    norm = np.sqrt(w * w + x * x + y * y + z * z)
    if norm <= 0.0:
        raise PolicyError("Cannot project gravity through a zero quaternion.")
    w, x, y, z = w / norm, x / norm, y / norm, z / norm
    # Third column of R(body->world) transposed times (0,0,-1).
    return np.array(
        [
            2.0 * (x * z - w * y),
            2.0 * (y * z + w * x),
            w * w - x * x - y * y + z * z,
        ],
        dtype=float,
    ) * -1.0


def build_observation(
    *,
    ang_vel: Any,
    quat: Any,
    joint_pos: Any,
    joint_vel: Any,
    last_action: Any,
    twist: Any,
    head: Any,
    body: Any,
) -> np.ndarray:
    """Assemble the 61-D policy input from engine state (float32)."""
    obs = np.zeros(OBS_DIM, dtype=np.float32)
    obs[0:3] = np.asarray(ang_vel, dtype=float).reshape(3)
    obs[3:6] = projected_gravity(quat)
    pos = np.asarray(joint_pos, dtype=float).reshape(ACT_DIM)
    obs[6:20] = pos - stand_pose()
    obs[20:34] = np.asarray(joint_vel, dtype=float).reshape(ACT_DIM)
    obs[34:48] = np.asarray(last_action, dtype=float).reshape(ACT_DIM)
    obs[48:51] = np.asarray(twist, dtype=float).reshape(3)
    obs[51:55] = np.asarray(head, dtype=float).reshape(4)
    obs[55:61] = np.asarray(body, dtype=float).reshape(6)
    return obs


def targets_from_action(action: Any, stand: Any) -> np.ndarray:
    """Position targets = STAND + raw network output (scale 1.0, unclipped)."""
    return np.asarray(stand, dtype=float).reshape(
        ACT_DIM
    ) + np.asarray(action, dtype=float).reshape(ACT_DIM)


#: ONNX Runtime threads for the 50 Hz policy step.
#:
#: The runner container is CPU-limited (``--cpus``, default 1). ONNX Runtime
#: sizes its intra-op pool to the HOST core count, so those threads compete
#: for a one-CPU cgroup quota, the kernel throttles the whole process every
#: 100 ms, and control ticks stretch past their 20 ms period: the run then
#: lags realtime and telemetry arrives in bursts. Measured on the MicroDuck
#: walk inside the runner image (1 CPU, 559 ticks): tick gap 32.8 ms and
#: tick body 12.1 ms by default, 20.8 ms and 8.5 ms with one sequential
#: intra-op thread and spinning disabled. One thread is also all the model
#: needs: the policy is a small MLP and each step runs in ~1 ms.
ORT_THREADS = 1


def session_options(ort_module: Any) -> Any:
    """Single-threaded, non-spinning ONNX Runtime session options.

    Spinning is disabled explicitly: ORT workers busy-wait between steps,
    which burns the container's whole quota even when the model is idle.
    """
    options = ort_module.SessionOptions()
    options.intra_op_num_threads = ORT_THREADS
    options.inter_op_num_threads = 1
    options.execution_mode = ort_module.ExecutionMode.ORT_SEQUENTIAL
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    return options


class VelStandPolicy:
    """50 Hz ONNX locomotion policy with last-action memory."""

    def __init__(self, model_path: str | Path | None = None) -> None:
        if ort is None:
            raise PolicyError(
                "onnxruntime is not installed for this Python. Install the "
                "sim extras: cd services/api && uv pip install "
                "--python .venv/bin/python -e '.[sim]'."
            )
        path = Path(model_path) if model_path else policy_path()
        if not path.exists():
            raise PolicyError(
                f"Locomotion policy file is missing: {path}."
            )
        try:
            self.session = ort.InferenceSession(
                str(path),
                sess_options=session_options(ort),
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:
            raise PolicyError(
                f"Cannot load locomotion policy {path.name!r}: {exc}."
            ) from None
        inputs = self.session.get_inputs()
        outputs = self.session.get_outputs()
        if (
            len(inputs) != 1
            or list(inputs[0].shape) != [1, OBS_DIM]
            or len(outputs) != 1
            or list(outputs[0].shape) != [1, ACT_DIM]
        ):
            raise PolicyError(
                f"Unexpected policy I/O shape in {path.name!r}: "
                f"{[(i.name, i.shape) for i in inputs]} -> "
                f"{[(o.name, o.shape) for o in outputs]}; "
                f"expected [1, {OBS_DIM}] -> [1, {ACT_DIM}]."
            )
        self.input_name = inputs[0].name
        self.output_name = outputs[0].name
        self.stand = stand_pose()
        self.reset()

    def reset(self) -> None:
        """Clear last-action memory (zeros, as in training resets)."""
        self.last_action = np.zeros(ACT_DIM, dtype=np.float32)

    def step(self, inputs: dict[str, Any]) -> np.ndarray:
        """Run one 50 Hz policy step; return 14 position targets."""
        obs = build_observation(
            ang_vel=inputs["ang_vel"],
            quat=inputs["quat"],
            joint_pos=inputs["joint_pos"],
            joint_vel=inputs["joint_vel"],
            last_action=self.last_action,
            twist=inputs.get("twist", (0.0, 0.0, 0.0)),
            head=inputs.get("head", (0.0, 0.0, 0.0, 0.0)),
            body=inputs.get("body", (0.0,) * 6),
        )
        (raw,) = self.session.run(
            [self.output_name], {self.input_name: obs.reshape(1, OBS_DIM)}
        )
        action = np.asarray(raw, dtype=float).reshape(ACT_DIM)
        if not np.all(np.isfinite(action)):
            raise PolicyError("Policy returned a non-finite action.")
        self.last_action = action.astype(np.float32)
        return targets_from_action(action, self.stand)


def rollout(
    engine: Any,
    policy: VelStandPolicy,
    *,
    seconds: float,
    twist: tuple[float, float, float] = (0.0, 0.0, 0.0),
    head: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0),
    body: tuple[float, ...] = (0.0,) * 6,
) -> dict[str, Any]:
    """Run the policy against ``engine``; return locomotion evidence.

    The engine must expose the ``MujocoEngine`` telemetry surface
    (``joint_positions``, ``joint_velocities``, ``sensor_readings``,
    ``root_orientation``, ``set_commands``, ``advance``, ``base_pose``,
    ``fallen``, ``trunk_z``). Physics substeps per policy step derive from
    the scenario timestep so the policy always runs at 50 Hz.
    """
    if tuple(engine.joint_names) != ACTUATOR_ORDER:
        raise PolicyError(
            "Engine joint order does not match the policy actuator order; "
            "refusing to run the locomotion policy."
        )
    dt = float(engine.scenario.dt)
    substeps = max(1, round(1.0 / (POLICY_HZ * dt)))
    steps = max(1, round(seconds * POLICY_HZ))
    start_x, start_y = engine.base_pose()[0:2]
    min_trunk_z = float("inf")
    policy.reset()
    for _ in range(steps):
        readings = engine.sensor_readings()
        targets = policy.step(
            {
                "ang_vel": readings["imu_ang_vel"],
                "quat": engine.root_orientation(),
                "joint_pos": engine.joint_positions(),
                "joint_vel": engine.joint_velocities(),
                "twist": twist,
                "head": head,
                "body": body,
            }
        )
        engine.set_commands(targets)
        engine.advance(substeps)
        min_trunk_z = min(min_trunk_z, float(engine.trunk_z))
        if engine.fallen:
            break
    end_x, end_y = engine.base_pose()[0:2]
    return {
        "policy_steps": steps,
        "forward_m": float(end_x - start_x),
        "lateral_m": float(end_y - start_y),
        "fallen": bool(engine.fallen),
        "min_trunk_z": min_trunk_z,
        "trunk_z": float(engine.trunk_z),
    }
