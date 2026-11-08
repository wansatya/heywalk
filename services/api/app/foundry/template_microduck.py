"""MicroDuck simulation contract, copied for Foundry robots.

The official MicroDuck model is the proven reference: its joint tuning,
sensor set, STAND keyframe, and contact handling are what make it "work
perfect" in the simulator. Foundry robots copy those *conventions* so a
generated morphology gets the same stable contract.

What is copied (MJCF patterns and values, Apache-2.0 — see sources)::

- the ``chosen_actuator`` joint tuning (damping/frictionloss/armature),
- the position-actuator key set (kp/kv/gear/forcerange/ctrlrange),
- the IMU sensor set on a trunk ``imu`` site,
- the STAND keyframe (all-zero joints, root at assembly height),
- hinge range conventions per joint role.

What is deliberately NOT copied:

- MicroDuck meshes (CC BY-NC-SA 4.0) — Foundry robots only ever carry
  meshes generated from the user's reference images.
- The soft-servo integration (Euler) and tiny gains (kp 0.55): Foundry
  gains are mass-scaled an order of magnitude stiffer to meet the 0.05
  tracking tolerance, so Foundry runs keep the validated ``implicitfast``
  integration — the same adaptation ED-209 already makes.
- The 14-joint duck skeleton and its ONNX walking policy, which only fit
  the duck morphology. Multi-joint limb chains are future work; Foundry
  keeps one hinge per detected limb for now.

Sources (verified, see ``simulator/microduck/ATTRIBUTION.md``):

- https://github.com/pollen-robotics/microduck_rl
- ``src/mjlab_microduck/robot/microduck/robot_groundcontact.xml``
"""

from __future__ import annotations

import math
from typing import Any

SOURCE_URL = "https://github.com/pollen-robotics/microduck_rl"
SOURCE_MODEL_PATH = (
    "src/mjlab_microduck/robot/microduck/robot_groundcontact.xml"
)
SOURCE_COMMIT = "bae39f5900b318f7e87fc485c30a31213cfa14eb"
SOURCE_LICENSE = "Apache-2.0 (MJCF patterns and values; no meshes copied)"

#: Template version stamped into Foundry manifests.
TEMPLATE_VERSION = "microduck_contract_v1"

#: Official runtime physics (microduck_rl ``sim/body_server.py``).
TIMESTEP = 0.005
CONTROL_HZ = 50.0
KEYFRAME_STAND = "STAND"

#: Default-class name applied to Foundry joints and position actuators,
#: mirroring the official ``chosen_actuator`` class.
ACTUATOR_CLASS = "foundry_actuator"

#: Joint tuning copied from the official ``chosen_actuator`` class.
JOINT_TUNING: dict[str, float] = {
    "damping": 0.053,
    "frictionloss": 0.0048,
    "armature": 0.0018,
}

#: Fallback position gains when an actuator has no estimated kp/kv.
POSITION_FALLBACK: dict[str, float] = {"kp": 80.0, "kv": 2.0}

#: Hinge ranges per joint role, copied from the official joints
#: (hip_pitch ±90°, neck_pitch -90°/+60°, head_yaw ±170°).
ROLE_RANGES: dict[str, tuple[float, float]] = {
    "pitch": (-math.pi / 2.0, math.pi / 2.0),
    "neck_pitch": (-math.pi / 2.0, math.pi / 3.0),
    "head_yaw": (-2.9670597283903613, 2.9670597283903595),
}

#: IMU sensor set copied from the official ``<sensor>`` block (noise
#: omitted for deterministic tests). ``subtreeangmom`` binds to the root
#: body at generation time.
IMU_SENSORS: tuple[dict[str, Any], ...] = (
    {"tag": "framequat", "name": "orientation", "objtype": "site", "objname": "imu"},
    {"tag": "gyro", "name": "angular-velocity", "site": "imu"},
    {"tag": "gyro", "name": "imu_ang_vel", "site": "imu"},
    {"tag": "velocimeter", "name": "imu_lin_vel", "site": "imu"},
    {"tag": "accelerometer", "name": "imu_accel", "site": "imu"},
    {"tag": "subtreeangmom", "name": "root_angmom", "body": ""},
)

IMU_SITE_NAME = "imu"


def stand_qpos(
    root_pos: tuple[float, float, float], n_joints: int
) -> list[float]:
    """STAND keyframe qpos: root frame pose plus all-zero joints."""
    return [root_pos[0], root_pos[1], root_pos[2], 1.0, 0.0, 0.0, 0.0] + [
        0.0
    ] * max(0, n_joints)


__all__ = [
    "ACTUATOR_CLASS",
    "CONTROL_HZ",
    "IMU_SENSORS",
    "IMU_SITE_NAME",
    "JOINT_TUNING",
    "KEYFRAME_STAND",
    "POSITION_FALLBACK",
    "ROLE_RANGES",
    "SOURCE_COMMIT",
    "SOURCE_LICENSE",
    "SOURCE_MODEL_PATH",
    "SOURCE_URL",
    "TEMPLATE_VERSION",
    "TIMESTEP",
    "stand_qpos",
]
