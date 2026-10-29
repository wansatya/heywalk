"""ROS 2 topic contract between controller and simulator (AGENTS.md section 7).

The generated controller and the simulator bridge communicate only through
these topics. Both the real rclpy transport (inside the Docker runner) and
the in-process loopback transport (local dev/test runner) implement exactly
this contract, so controller code runs unmodified under either one.

| Topic            | Message type                    | Direction           |
|------------------|---------------------------------|---------------------|
| /cmd_vel         | geometry_msgs/msg/Twist         | controller -> sim   |
| /odom            | nav_msgs/msg/Odometry           | sim -> controller   |
| /scan            | sensor_msgs/msg/LaserScan       | sim -> controller   |
| /emergency_stop  | std_msgs/msg/Bool               | harness -> both     |
| /goal_pose       | geometry_msgs/msg/PoseStamped   | sim -> controller   |
| /objective_state | std_msgs/msg/String             | sim -> controller   |
"""

from __future__ import annotations

import math
from typing import Any

CMD_VEL_TOPIC = "/cmd_vel"
ODOM_TOPIC = "/odom"
SCAN_TOPIC = "/scan"
EMERGENCY_STOP_TOPIC = "/emergency_stop"
GOAL_POSE_TOPIC = "/goal_pose"
#: Current locomotion phase type (go_to/touch/wander/kick), every tick.
OBJECTIVE_STATE_TOPIC = "/objective_state"

# MuJoCo joint-test contract (ros2_control position interface emulation).
JOINT_STATES_TOPIC = "/joint_states"
JOINT_COMMANDS_TOPIC = "/joint_commands"
JOINT_TARGETS_TOPIC = "/joint_targets"

TOPIC_MESSAGE_TYPES: dict[str, str] = {
    CMD_VEL_TOPIC: "geometry_msgs/msg/Twist",
    ODOM_TOPIC: "nav_msgs/msg/Odometry",
    SCAN_TOPIC: "sensor_msgs/msg/LaserScan",
    EMERGENCY_STOP_TOPIC: "std_msgs/msg/Bool",
    GOAL_POSE_TOPIC: "geometry_msgs/msg/PoseStamped",
    OBJECTIVE_STATE_TOPIC: "std_msgs/msg/String",
    JOINT_STATES_TOPIC: "sensor_msgs/msg/JointState",
    JOINT_COMMANDS_TOPIC: "std_msgs/msg/Float64MultiArray",
    JOINT_TARGETS_TOPIC: "std_msgs/msg/Float64MultiArray",
}

REQUIRED_TOPICS: tuple[str, ...] = (
    CMD_VEL_TOPIC,
    ODOM_TOPIC,
    SCAN_TOPIC,
    EMERGENCY_STOP_TOPIC,
    GOAL_POSE_TOPIC,
    OBJECTIVE_STATE_TOPIC,
)

#: Phase type the 2D arena publishes on /objective_state: its single
#: objective is to reach the goal region, so the phase never changes.
ARENA_OBJECTIVE_TYPE = "go_to"

#: Topics the MuJoCo joint-test harness provides to a generated controller.
#: Locomotion runs (``locomotion=True``) additionally publish the steering
#: contract: /cmd_vel, /odom, /goal_pose, and /objective_state.
MUJOCO_TOPICS: tuple[str, ...] = (
    JOINT_STATES_TOPIC,
    JOINT_TARGETS_TOPIC,
    JOINT_COMMANDS_TOPIC,
    EMERGENCY_STOP_TOPIC,
)

# Telemetry sample contract shared by the harness, the runners, and the
# run_telemetry store (mirrors the simulator engine's recorded samples).
TELEMETRY_FIELDS: tuple[str, ...] = (
    "t",
    "x",
    "y",
    "theta",
    "v",
    "omega",
    "collision",
    "goal_reached",
    "estop_active",
    "min_range",
)


def twist_to_command(linear_x: Any, angular_z: Any) -> tuple[float, float]:
    """Convert Twist components to a (v, omega) simulator command.

    Non-finite values are coerced to zero; the simulator counts them as
    invalid commands and the evaluator reports them as failure evidence.
    """
    try:
        v = float(linear_x)
    except (TypeError, ValueError):
        v = 0.0
    try:
        omega = float(angular_z)
    except (TypeError, ValueError):
        omega = 0.0
    if not math.isfinite(v):
        v = 0.0
    if not math.isfinite(omega):
        omega = 0.0
    return v, omega


def odom_from_state(
    x: float, y: float, theta: float, v: float, omega: float
) -> dict[str, Any]:
    """Build the logical odometry payload published on /odom."""
    half = theta / 2.0
    return {
        "position": {"x": float(x), "y": float(y), "z": 0.0},
        "orientation": {
            "x": 0.0,
            "y": 0.0,
            "z": math.sin(half),
            "w": math.cos(half),
        },
        "linear_velocity": float(v),
        "angular_velocity": float(omega),
    }


def yaw_from_quaternion(z: float, w: float) -> float:
    """Recover planar heading from a z/w-only quaternion."""
    return math.atan2(2.0 * w * z, 1.0 - 2.0 * z * z)


def scan_from_ranges(
    ranges: list[float],
    *,
    angle_min: float,
    angle_max: float,
    range_min: float,
    range_max: float,
) -> dict[str, Any]:
    """Build the logical laser-scan payload published on /scan."""
    count = len(ranges)
    increment = (angle_max - angle_min) / max(count - 1, 1) if count else 0.0
    return {
        "angle_min": float(angle_min),
        "angle_max": float(angle_max),
        "angle_increment": float(increment),
        "range_min": float(range_min),
        "range_max": float(range_max),
        "ranges": [float(r) for r in ranges],
    }


def goal_pose_from_goal(goal_x: float, goal_y: float) -> dict[str, Any]:
    """Build the logical goal payload published on /goal_pose."""
    return {"position": {"x": float(goal_x), "y": float(goal_y), "z": 0.0}}


def fill_odometry_message(message: Any, payload: dict[str, Any]) -> Any:
    """Fill an Odometry message (real or shim) from an odom payload."""
    message.pose.pose.position.x = payload["position"]["x"]
    message.pose.pose.position.y = payload["position"]["y"]
    message.pose.pose.orientation.z = payload["orientation"]["z"]
    message.pose.pose.orientation.w = payload["orientation"]["w"]
    message.twist.twist.linear.x = payload["linear_velocity"]
    message.twist.twist.angular.z = payload["angular_velocity"]
    return message


def fill_laser_scan_message(message: Any, payload: dict[str, Any]) -> Any:
    """Fill a LaserScan message (real or shim) from a scan payload."""
    message.angle_min = payload["angle_min"]
    message.angle_max = payload["angle_max"]
    message.angle_increment = payload["angle_increment"]
    message.range_min = payload["range_min"]
    message.range_max = payload["range_max"]
    message.ranges = list(payload["ranges"])
    return message


def fill_goal_message(message: Any, payload: dict[str, Any]) -> Any:
    """Fill a PoseStamped message (real or shim) from a goal payload."""
    message.pose.position.x = payload["position"]["x"]
    message.pose.position.y = payload["position"]["y"]
    return message


def joint_state_from_values(
    names: list[str],
    positions: list[float],
    velocities: list[float],
    efforts: list[float],
) -> dict[str, Any]:
    """Build the logical /joint_states payload (ros2_control state interface)."""
    return {
        "name": list(names),
        "position": [float(value) for value in positions],
        "velocity": [float(value) for value in velocities],
        "effort": [float(value) for value in efforts],
    }


def fill_joint_state_message(message: Any, payload: dict[str, Any]) -> Any:
    """Fill a JointState message (real or shim) from a joint-state payload."""
    message.name = list(payload["name"])
    message.position = list(payload["position"])
    message.velocity = list(payload["velocity"])
    message.effort = list(payload["effort"])
    return message


def fill_float64_array_message(message: Any, values: list[float]) -> Any:
    """Fill a Float64MultiArray command/target message."""
    message.data = [float(value) for value in values]
    return message


def bool_message(value: bool, message: Any = None) -> Any:
    """Build (or fill) a ``std_msgs/msg/Bool``.

    rclpy message classes take no constructor arguments, so ``Bool(True)``
    raises TypeError on the real ROS 2 stack. Messages are always created
    empty and assigned field by field; this helper keeps that in one place so
    the loopback shim and rclpy behave identically.
    """
    if message is None:
        from std_msgs.msg import Bool

        message = Bool()
    message.data = bool(value)
    return message


def string_message(value: str, message: Any = None) -> Any:
    """Build (or fill) a ``std_msgs/msg/String`` (same empty-then-assign)."""
    if message is None:
        from std_msgs.msg import String

        message = String()
    message.data = str(value)
    return message


def twist_from_message(message: Any) -> tuple[float, float, float] | None:
    """Extract (vx, vy, yaw) from a Twist; None when malformed.

    Accepts real/shim ``geometry_msgs`` messages (``.linear.x`` ...),
    loopback dict payloads (``{\"linear\": {\"x\": ...}}``), and bare
    (vx, vy, yaw) triples. Non-finite components are rejected so a
    hostile controller cannot poison the walking policy.
    """
    triple: Any = None
    linear = getattr(message, "linear", None)
    angular = getattr(message, "angular", None)
    if linear is not None and angular is not None:
        triple = (
            getattr(linear, "x", None),
            getattr(linear, "y", None),
            getattr(angular, "z", None),
        )
    elif isinstance(message, dict):
        linear_map = message.get("linear")
        angular_map = message.get("angular")
        if isinstance(linear_map, dict) and isinstance(angular_map, dict):
            triple = (
                linear_map.get("x"),
                linear_map.get("y"),
                angular_map.get("z"),
            )
    elif isinstance(message, (list, tuple)) and len(message) == 3:
        triple = tuple(message)
    if triple is None:
        return None
    try:
        values = tuple(float(value) for value in triple)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    return values


def commands_from_message(message: Any) -> list[float] | None:
    """Extract a Float64MultiArray payload; None when malformed."""
    data = getattr(message, "data", None)
    if not isinstance(data, (list, tuple)):
        return None
    try:
        values = [float(value) for value in data]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    return values


def normalize_telemetry_sample(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one telemetry sample from the harness.

    Raises ValueError when required fields are missing or malformed so a
    corrupt or hostile harness output can never poison stored telemetry.
    """
    if not isinstance(raw, dict):
        raise ValueError("Telemetry sample must be a JSON object.")
    try:
        sample: dict[str, Any] = {
            "t": float(raw["t"]),
            "x": float(raw["x"]),
            "y": float(raw["y"]),
            "theta": float(raw["theta"]),
            "v": float(raw["v"]),
            "omega": float(raw["omega"]),
            "collision": bool(raw.get("collision", False)),
            "goal_reached": bool(raw.get("goal_reached", False)),
            "estop_active": bool(raw.get("estop_active", False)),
            "min_range": raw.get("min_range"),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Malformed telemetry sample: {exc}.") from None
    for key in ("t", "x", "y", "theta", "v", "omega"):
        if not math.isfinite(sample[key]):
            raise ValueError(f"Telemetry field {key!r} is not finite.")
    if sample["min_range"] is not None:
        try:
            sample["min_range"] = float(sample["min_range"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Telemetry field 'min_range' is malformed.") from exc
        if not math.isfinite(sample["min_range"]):
            raise ValueError("Telemetry field 'min_range' is not finite.")
    if raw.get("commands_valid") is False:
        sample["commands_valid"] = False
    if "joints" in raw:
        sample["joints"] = _normalize_joints(raw.get("joints"))
    if raw.get("contacts") is not None:
        try:
            contacts = int(raw["contacts"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Telemetry field 'contacts' is malformed.") from exc
        sample["contacts"] = max(0, contacts)
    if raw.get("max_contact_force") is not None:
        try:
            force = float(raw["max_contact_force"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Telemetry field 'max_contact_force' is malformed."
            ) from exc
        if not math.isfinite(force) or force < 0:
            raise ValueError(
                "Telemetry field 'max_contact_force' must be finite and"
                " non-negative."
            )
        sample["max_contact_force"] = force
    if raw.get("test_id") is not None:
        sample["test_id"] = str(raw["test_id"])[:128]
    if raw.get("fallen") is not None:
        sample["fallen"] = bool(raw.get("fallen", False))
    if raw.get("trunk_z") is not None:
        try:
            trunk_z = float(raw["trunk_z"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Telemetry field 'trunk_z' is malformed.") from exc
        if not math.isfinite(trunk_z):
            raise ValueError("Telemetry field 'trunk_z' is not finite.")
        sample["trunk_z"] = trunk_z
    if raw.get("expected_contacts") is not None:
        try:
            expected = int(raw["expected_contacts"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Telemetry field 'expected_contacts' is malformed."
            ) from exc
        sample["expected_contacts"] = max(0, expected)
    if raw.get("twist") is not None:
        twist = raw["twist"]
        if (
            not isinstance(twist, (list, tuple))
            or len(twist) != 3
        ):
            raise ValueError("Telemetry field 'twist' must be a triple.")
        try:
            floats = [float(value) for value in twist]
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Telemetry field 'twist' is malformed: {exc}."
            ) from None
        if not all(math.isfinite(value) for value in floats):
            raise ValueError("Telemetry field 'twist' must be finite.")
        sample["twist"] = floats
    if "sensors" in raw and raw["sensors"] is not None:
        sample["sensors"] = _normalize_sensors(raw["sensors"])
    if "bodies" in raw and raw["bodies"] is not None:
        sample["bodies"] = _normalize_bodies(raw["bodies"])
    return sample


def _normalize_sensors(raw: Any) -> dict[str, list[float]]:
    """Validate the optional sensor map carried by official-model telemetry."""
    if not isinstance(raw, dict):
        raise ValueError("Telemetry field 'sensors' must be an object.")
    if len(raw) > 64:
        raise ValueError("Telemetry field 'sensors' holds too many sensors.")
    sensors: dict[str, list[float]] = {}
    for name, values in raw.items():
        if not isinstance(values, list) or len(values) > 16:
            raise ValueError(
                f"Telemetry sensor {str(name)[:64]!r} must be a short array."
            )
        try:
            floats = [float(value) for value in values]
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Telemetry sensor {str(name)[:64]!r} is malformed: {exc}."
            ) from None
        if not all(math.isfinite(value) for value in floats):
            raise ValueError("Telemetry sensor values must be finite.")
        sensors[str(name)[:128]] = floats
    return sensors


def _normalize_bodies(raw: Any) -> list[dict[str, Any]]:
    """Validate the optional body-pose array carried by MuJoCo telemetry."""
    if not isinstance(raw, list):
        raise ValueError("Telemetry field 'bodies' must be an array.")
    if len(raw) > 128:
        raise ValueError("Telemetry field 'bodies' holds too many bodies.")
    bodies: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("Each telemetry body must be an object.")
        try:
            name = str(entry["body"])[:128]
            pos = [float(value) for value in entry["pos"]]
            quat = [float(value) for value in entry["quat"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Malformed telemetry body: {exc}.") from None
        if len(pos) != 3 or len(quat) != 4:
            raise ValueError(
                "Telemetry bodies need a 3-vector pos and a 4-vector quat."
            )
        if not all(math.isfinite(value) for value in (*pos, *quat)):
            raise ValueError("Telemetry body values must be finite.")
        bodies.append({"body": name, "pos": pos, "quat": quat})
    return bodies


def _normalize_joints(raw: Any) -> list[dict[str, Any]]:
    """Validate the optional joint-state array carried by MuJoCo telemetry."""
    if not isinstance(raw, list):
        raise ValueError("Telemetry field 'joints' must be an array.")
    if len(raw) > 128:
        raise ValueError("Telemetry field 'joints' holds too many joints.")
    joints: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("Each telemetry joint must be an object.")
        try:
            name = str(entry["name"])[:128]
            position = float(entry["position"])
            velocity = float(entry["velocity"])
            effort = float(entry.get("effort", 0.0))
            target = float(entry.get("target", position))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Malformed telemetry joint: {exc}.") from None
        for value in (position, velocity, effort, target):
            if not math.isfinite(value):
                raise ValueError("Telemetry joint values must be finite.")
        joints.append(
            {
                "name": name,
                "position": position,
                "velocity": velocity,
                "effort": effort,
                "target": target,
            }
        )
    return joints
