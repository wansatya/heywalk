"""Robot description parsing, validation, and ros2_control synthesis.

The generated robot package is untrusted model output, so it is parsed as
data only -- never imported or executed. This module:

- reads the MJCF (``robot.xml``) that MuJoCo will load, extracting the
  actuated joint list, joint types/ranges, and position actuators;
- validates the companion URDF and ``ros2_control.yaml`` against those joints;
- synthesizes missing ``robot.urdf`` / ``ros2_control.yaml`` from the MJCF so
  the platform always has a complete robot package and a documented control
  interface (AGENTS.md section 5/7).

Safety: XML parsing rejects DOCTYPE/ENTITY declarations (no entity expansion
or external entities) and the MJCF loader in the runner rejects absolute or
parent-traversing asset paths.
"""

from __future__ import annotations

import math
import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import yaml

from .scenario import (
    DEFAULT_MODEL_PATH,
    DEFAULT_ROS2_CONTROL_PATH,
    DEFAULT_URDF_PATH,
)

MAX_ROBOT_XML_BYTES = 256 * 1024
MAX_JOINTS = 64
MAX_BODIES = 128
MAX_GEOMS = 256
MAX_ACTUATORS = 64

_DOCTYPE_PATTERN = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)
_ANGLE_DEGREE_UNITS = {"degree", "degrees", "deg"}


class RobotPackageError(ValueError):
    """Raised when a generated robot package cannot be used for simulation."""


@dataclass(frozen=True)
class RobotJoint:
    """One actuated joint as declared by the MJCF model."""

    name: str
    type: str  # "hinge" | "slide"
    actuator: str
    lower: float | None
    upper: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "actuator": self.actuator,
            "lower": self.lower,
            "upper": self.upper,
        }


@dataclass(frozen=True)
class RobotInfo:
    """Validated kinematic/actuation summary of a generated MJCF robot."""

    model_path: str
    joints: tuple[RobotJoint, ...]
    body_count: int
    geom_count: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def joint_names(self) -> tuple[str, ...]:
        return tuple(joint.name for joint in self.joints)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_path": self.model_path,
            "joints": [joint.to_dict() for joint in self.joints],
            "body_count": self.body_count,
            "geom_count": self.geom_count,
            "notes": list(self.notes),
        }


def parse_xml(text: str, label: str) -> ET.Element:
    """Parse untrusted XML with DOCTYPE/ENTITY rejection and size limits."""
    if not isinstance(text, str):
        raise RobotPackageError(f"{label} must be text.")
    if len(text.encode("utf-8")) > MAX_ROBOT_XML_BYTES:
        raise RobotPackageError(
            f"{label} exceeds the {MAX_ROBOT_XML_BYTES // 1024} KiB limit."
        )
    if _DOCTYPE_PATTERN.search(text):
        raise RobotPackageError(
            f"{label} contains a DOCTYPE/ENTITY declaration, which is not"
            " allowed in generated robot descriptions."
        )
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise RobotPackageError(f"{label} is not valid XML: {exc}.") from None


def _float_attr(element: ET.Element, name: str) -> float | None:
    raw = element.get(name)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise RobotPackageError(
            f"Attribute {name}={raw!r} on <{element.tag}> is not a number."
        ) from None
    if not math.isfinite(value):
        raise RobotPackageError(
            f"Attribute {name}={raw!r} on <{element.tag}> must be finite."
        )
    return value


def _range_of(
    element: ET.Element, *, radians: bool
) -> tuple[float | None, float | None]:
    raw = element.get("range")
    if not raw:
        return None, None
    parts = raw.split()
    if len(parts) != 2:
        raise RobotPackageError(
            f"Joint {element.get('name')!r} range must hold two numbers."
        )
    try:
        lower, upper = float(parts[0]), float(parts[1])
    except ValueError:
        raise RobotPackageError(
            f"Joint {element.get('name')!r} range is not numeric."
        ) from None
    if not (math.isfinite(lower) and math.isfinite(upper)):
        raise RobotPackageError(
            f"Joint {element.get('name')!r} range must be finite."
        )
    if upper <= lower:
        raise RobotPackageError(
            f"Joint {element.get('name')!r} range must be increasing"
            f" (got {lower}..{upper})."
        )
    if radians:
        lower, upper = math.radians(lower), math.radians(upper)
    return lower, upper


def _iter_bodies(worldbody: ET.Element):
    stack = list(worldbody.findall("body"))
    while stack:
        body = stack.pop()
        yield body
        stack.extend(body.findall("body"))


def parse_mjcf(text: str, model_path: str = DEFAULT_MODEL_PATH) -> RobotInfo:
    """Validate an MJCF robot and extract its actuated joints.

    Requirements for the automated joint-test workflow:

    - root element ``<mujoco>``;
    - at least one ``hinge``/``slide`` joint driven by a ``<position>``
      actuator (the ros2_control position command interface);
    - supported joint types only, unique names, bounded complexity.
    """
    root = parse_xml(text, f"MJCF model {model_path!r}")
    if root.tag != "mujoco":
        raise RobotPackageError(
            f"{model_path!r} must be a MuJoCo MJCF model (<mujoco> root);"
            f" found <{root.tag}>."
        )
    notes: list[str] = []

    compiler = root.find("compiler")
    angle = (compiler.get("angle") if compiler is not None else "degree") or "degree"
    hinge_radians = angle.strip().lower() in _ANGLE_DEGREE_UNITS
    if hinge_radians:
        notes.append("MJCF hinge ranges converted from degrees to radians.")

    worldbody = root.find("worldbody")
    if worldbody is None:
        raise RobotPackageError(
            f"{model_path!r} has no <worldbody>; nothing to simulate."
        )
    bodies = list(_iter_bodies(worldbody))
    if len(bodies) > MAX_BODIES:
        raise RobotPackageError(
            f"{model_path!r} has {len(bodies)} bodies (limit {MAX_BODIES})."
        )

    joints_by_name: dict[str, tuple[str, float | None, float | None]] = {}
    body_count = 0
    geom_count = 0
    for body in bodies:
        body_count += 1
        geom_count += len(body.findall("geom"))
        for joint in body.findall("joint"):
            name = (joint.get("name") or "").strip()
            if not name:
                raise RobotPackageError(
                    "Every MJCF joint needs a name so it can appear on"
                    " /joint_states and in ros2_control."
                )
            joint_type = (joint.get("type") or "hinge").strip().lower()
            if joint_type not in ("hinge", "slide"):
                raise RobotPackageError(
                    f"Joint {name!r} has unsupported type {joint_type!r};"
                    " only hinge and slide joints are supported by the"
                    " automated joint tests."
                )
            if name in joints_by_name:
                raise RobotPackageError(f"Duplicate MJCF joint name {name!r}.")
            radians = hinge_radians and joint_type == "hinge"
            lower, upper = _range_of(joint, radians=radians)
            joints_by_name[name] = (joint_type, lower, upper)
    if geom_count > MAX_GEOMS:
        raise RobotPackageError(
            f"{model_path!r} has {geom_count} geoms (limit {MAX_GEOMS})."
        )

    actuator_root = root.find("actuator")
    if actuator_root is None:
        raise RobotPackageError(
            f"{model_path!r} has no <actuator> section; add <position>"
            " actuators so ros2_control position commands can drive the"
            " joints."
        )
    actuators = list(actuator_root)
    if len(actuators) > MAX_ACTUATORS:
        raise RobotPackageError(
            f"{model_path!r} has {len(actuators)} actuators"
            f" (limit {MAX_ACTUATORS})."
        )
    joints: list[RobotJoint] = []
    seen_joint_actuators: set[str] = set()
    for index, actuator in enumerate(actuators):
        actuator_name = (actuator.get("name") or f"actuator_{index + 1}").strip()
        if actuator.tag != "position":
            raise RobotPackageError(
                f"Actuator {actuator_name!r} uses <{actuator.tag}>; the"
                " automated joint tests require <position> actuators"
                " (position command interface)."
            )
        joint_name = (actuator.get("joint") or "").strip()
        if not joint_name:
            raise RobotPackageError(
                f"Position actuator {actuator_name!r} must target a joint."
            )
        if joint_name in seen_joint_actuators:
            raise RobotPackageError(
                f"Joint {joint_name!r} is driven by more than one actuator;"
                " keep exactly one position actuator per joint."
            )
        if joint_name not in joints_by_name:
            raise RobotPackageError(
                f"Actuator {actuator_name!r} targets unknown joint"
                f" {joint_name!r}."
            )
        joint_type, lower, upper = joints_by_name[joint_name]
        seen_joint_actuators.add(joint_name)
        joints.append(
            RobotJoint(
                name=joint_name,
                type=joint_type,
                actuator=actuator_name,
                lower=lower,
                upper=upper,
            )
        )
    if not joints:
        raise RobotPackageError(
            f"{model_path!r} has no position-actuated joints; the automated"
            " tests need at least one."
        )
    if len(joints) > MAX_JOINTS:
        raise RobotPackageError(
            f"{model_path!r} has {len(joints)} actuated joints"
            f" (limit {MAX_JOINTS})."
        )
    return RobotInfo(
        model_path=model_path,
        joints=tuple(joints),
        body_count=body_count,
        geom_count=geom_count,
        notes=tuple(notes),
    )


def find_model_file(
    files: dict[str, str], model_path: str = DEFAULT_MODEL_PATH
) -> str:
    """Return the path of the MJCF model inside a generated package."""
    if model_path in files:
        return model_path
    for path in sorted(files):
        if path.lower().endswith(".xml"):
            text = files[path]
            if "<mujoco" in text[:2048].lower():
                return path
    raise RobotPackageError(
        f"No MJCF robot model found; add {model_path!r} with a <mujoco>"
        " root, named joints, and <position> actuators."
    )


def parse_urdf_joint_names(text: str, path: str) -> tuple[str, ...]:
    root = parse_xml(text, f"URDF {path!r}")
    if root.tag != "robot":
        raise RobotPackageError(
            f"{path!r} must be a URDF document (<robot> root)."
        )
    names = []
    for joint in root.findall("joint"):
        name = (joint.get("name") or "").strip()
        if name:
            names.append(name)
    return tuple(names)


def validate_ros2_control_text(
    text: str, path: str, joint_names: tuple[str, ...]
) -> list[str]:
    """Validate a ros2_control YAML mapping against the robot's joints."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RobotPackageError(
            f"ros2_control config {path!r} is not valid YAML: {exc}."
        ) from None
    if not isinstance(data, dict) or not data:
        raise RobotPackageError(
            f"ros2_control config {path!r} must be a non-empty YAML mapping."
        )
    missing = [name for name in joint_names if name not in text]
    if missing:
        raise RobotPackageError(
            f"ros2_control config {path!r} does not mention joint(s)"
            f" {', '.join(missing)}; controller and robot description must"
            " agree on joint names."
        )
    notes: list[str] = []
    if "controller_manager" not in data:
        notes.append(
            "ros2_control config has no controller_manager section; the"
            " HeyWalk bridge still emulates the position interface."
        )
    return notes


def synthesize_urdf(info: RobotInfo) -> str:
    """Build a minimal URDF skeleton with the MJCF's joint names/types.

    The skeleton carries the kinematic contract ros2_control needs (link and
    joint names), not collision meshes. A model-authored URDF, when present,
    is kept as-is.
    """
    lines = [
        '<?xml version="1.0"?>',
        "<!-- Generated by HeyWalk from the MJCF model: kinematic skeleton.",
        "     Replace with a model-authored URDF to describe real geometry. -->",
        '<robot name="heywalk_robot">',
        '  <link name="base_link"/>',
    ]
    parent = "base_link"
    for index, joint in enumerate(info.joints, start=1):
        child = f"link_{index}_{re.sub(r'[^A-Za-z0-9_]', '_', joint.name)}"
        urdf_type = "revolute" if joint.type == "hinge" else "prismatic"
        lines.append(f'  <link name="{child}"/>')
        lines.append(f'  <joint name="{joint.name}" type="{urdf_type}">')
        lines.append(f'    <parent link="{parent}"/>')
        lines.append(f'    <child link="{child}"/>')
        lines.append('    <origin xyz="0 0 0" rpy="0 0 0"/>')
        lines.append('    <axis xyz="0 1 0"/>')
        if joint.lower is not None and joint.upper is not None:
            limit_attr = (
                'effort="10" velocity="3"'
                if urdf_type == "revolute"
                else 'effort="10" velocity="1"'
            )
            lines.append(
                f'    <limit lower="{joint.lower:.6g}"'
                f' upper="{joint.upper:.6g}" {limit_attr}/>'
            )
        lines.append("  </joint>")
        parent = child
    lines.append("</robot>")
    return "\n".join(lines) + "\n"


def synthesize_ros2_control(info: RobotInfo, control_hz: float = 50.0) -> str:
    """Build the controller_manager + hardware config for the MJCF joints."""
    joints = list(info.joint_names)
    config = {
        "controller_manager": {
            "ros__parameters": {
                "update_rate": int(round(control_hz)),
            }
        },
        "joint_state_broadcaster": {
            "type": "joint_state_broadcaster/JointStateBroadcaster",
        },
        "joint_position_controller": {
            "type": "forward_command_controller/ForwardCommandController",
            "ros__parameters": {
                "joints": joints,
                "interface_name": "position",
            },
        },
        "heywalk_mujoco_hardware": {
            "ros__parameters": {
                "joints": joints,
                "command_interfaces": ["position"],
                "state_interfaces": ["position", "velocity"],
                "command_topic": "/joint_commands",
                "state_topic": "/joint_states",
            }
        },
    }
    header = (
        "# Generated by HeyWalk from the MJCF model.\n"
        "# The MuJoCo bridge emulates this hardware's position command\n"
        "# interface and publishes /joint_states from MuJoCo state.\n"
    )
    return header + yaml.safe_dump(config, sort_keys=False)


@dataclass(frozen=True)
class RobotPackage:
    """A validated robot package, rebuilt before any model output is written."""

    model_path: str
    info: RobotInfo
    files: dict[str, str]
    notes: tuple[str, ...] = ()

    @property
    def joint_names(self) -> tuple[str, ...]:
        return self.info.joint_names


def build_robot_package(
    files: dict[str, str],
    *,
    model_path: str = DEFAULT_MODEL_PATH,
    urdf_path: str = DEFAULT_URDF_PATH,
    ros2_control_path: str = DEFAULT_ROS2_CONTROL_PATH,
    control_hz: float = 50.0,
) -> RobotPackage:
    """Validate the MJCF and complete/validate the ROS side of the package.

    Missing URDF or ros2_control config is synthesized from the MJCF (the
    platform "configures ros2_control"); present files are validated against
    the MJCF joint names so a mismatch is reported instead of silently
    simulated.
    """
    resolved_model = find_model_file(files, model_path)
    info = parse_mjcf(files[resolved_model], resolved_model)
    notes = list(info.notes)
    output = dict(files)

    urdf_text = files.get(urdf_path)
    if urdf_text is None:
        output[urdf_path] = synthesize_urdf(info)
        notes.append(f"{urdf_path} synthesized from the MJCF joints.")
    else:
        urdf_joints = parse_urdf_joint_names(urdf_text, urdf_path)
        missing = [name for name in info.joint_names if name not in urdf_joints]
        if missing:
            raise RobotPackageError(
                f"URDF {urdf_path!r} is missing joint(s)"
                f" {', '.join(missing)} declared by the MJCF."
            )

    ros2_text = files.get(ros2_control_path)
    if ros2_text is None:
        output[ros2_control_path] = synthesize_ros2_control(info, control_hz)
        notes.append(f"{ros2_control_path} synthesized from the MJCF joints.")
    else:
        notes.extend(
            validate_ros2_control_text(
                ros2_text, ros2_control_path, info.joint_names
            )
        )

    return RobotPackage(
        model_path=resolved_model,
        info=info,
        files=output,
        notes=tuple(notes),
    )


def safe_relative_asset_path(value: str, label: str) -> str:
    """Reject absolute/traversing asset paths in a generated MJCF."""
    if not value or value.startswith(("/", "~")):
        raise RobotPackageError(f"{label} must be a relative path.")
    if "\\" in value:
        raise RobotPackageError(f"{label} must use forward slashes.")
    normalized = posixpath.normpath(value)
    if normalized == ".." or normalized.startswith("../"):
        raise RobotPackageError(
            f"{label} must not traverse outside the project workspace."
        )
    return normalized
