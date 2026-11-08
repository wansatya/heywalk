"""MJCF generation stage.

Builds a complete MuJoCo XML model from the assembly data. The output uses
only features the existing simulator supports: a free root (``<freejoint/>``),
hinge/slide child joints, and one ``<position>`` actuator per actuated joint.
Visual OBJ meshes and separate collision STL meshes are referenced through
relative ``assets/...`` paths so the model is self-contained.

The simulation contract (actuator-class tuning, IMU sensors, STAND
keyframe) is copied from the official MicroDuck model — see
:mod:`app.foundry.template_microduck`.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any
from xml.dom import minidom

from .store import slug
from .template_microduck import (
    ACTUATOR_CLASS,
    IMU_SENSORS,
    IMU_SITE_NAME,
    JOINT_TUNING,
    KEYFRAME_STAND,
    POSITION_FALLBACK,
    SOURCE_COMMIT,
    SOURCE_URL,
    TIMESTEP,
    stand_qpos,
)

DEFAULT_TIMESTEP = TIMESTEP
DEFAULT_CONTROL_HZ = 50.0


class MjcfError(ValueError):
    """Raised when the assembly cannot be turned into a valid MJCF model."""


@dataclass(frozen=True)
class MjcfModel:
    xml: str
    mesh_files: tuple[str, ...]
    joint_names: tuple[str, ...]
    body_names: tuple[str, ...]


def _fmt(value: float) -> str:
    text = f"{float(value):.9g}"
    return "0" if text in ("-0", "") else text


def _vec(values: Any, fallback: tuple[float, ...]) -> str:
    if isinstance(values, (list, tuple)) and len(values) == len(fallback):
        try:
            return " ".join(_fmt(float(value)) for value in values)
        except (TypeError, ValueError):
            pass
    return " ".join(_fmt(value) for value in fallback)


def _inertial(component: dict[str, Any]) -> ET.Element:
    physics = (component.get("metadata") or {}).get("physics") or {}
    mass = float(physics.get("mass_kg") or 1.0)
    inertia = physics.get("inertia")
    if not (isinstance(inertia, (list, tuple)) and len(inertia) == 3):
        inertia = [max(1e-5, mass / 12.0) for _ in range(3)]
    com = physics.get("center_of_mass") or [0.0, 0.0, 0.0]
    element = ET.Element(
        "inertial",
        {
            "pos": _vec(com, (0.0, 0.0, 0.0)),
            "mass": _fmt(max(0.01, mass)),
            "diaginertia": _vec(
                [max(1e-6, float(value)) for value in inertia],
                (1e-4, 1e-4, 1e-4),
            ),
        },
    )
    return element


def _geom(
    name: str,
    mesh_name: str,
    *,
    collision: bool,
    friction: float,
) -> ET.Element:
    if collision:
        return ET.Element(
            "geom",
            {
                "name": name,
                "type": "mesh",
                "mesh": mesh_name,
                "contype": "1",
                "conaffinity": "1",
                "group": "3",
                "rgba": "0 0 0 0",
                "friction": f"{_fmt(friction)} 0.02 0.001",
                "condim": "3",
            },
        )
    return ET.Element(
        "geom",
        {
            "name": name,
            "type": "mesh",
            "mesh": mesh_name,
            "contype": "0",
            "conaffinity": "0",
            "group": "1",
            "rgba": "0.72 0.74 0.78 1",
        },
    )


def build_mjcf(
    *,
    components: list[dict[str, Any]],
    joints: list[dict[str, Any]],
    actuators: list[dict[str, Any]],
    name: str = "foundry_robot",
    timestep: float = DEFAULT_TIMESTEP,
    control_hz: float = DEFAULT_CONTROL_HZ,
) -> MjcfModel:
    """Generate the MJCF text and its referenced mesh files."""
    if not components:
        raise MjcfError("No components to generate an MJCF from.")
    by_id = {component["id"]: component for component in components}
    children: dict[str | None, list[dict[str, Any]]] = {}
    for component in components:
        children.setdefault(component.get("parent_id"), []).append(component)
    roots = children.get(None) or []
    if not roots:
        raise MjcfError(
            "The assembly has no root component; every component has a parent."
        )
    if len(roots) > 1:
        raise MjcfError(
            "The assembly has more than one root component; connect them or"
            " remove the extra roots."
        )

    joints_by_component = {
        joint["component_id"]: joint for joint in joints if joint.get("component_id")
    }
    actuators_by_joint = {
        actuator["joint_id"]: actuator for actuator in actuators
    }

    root = ET.Element("mujoco", {"model": slug(name, "foundry_robot")})
    root.append(
        ET.Comment(
            " Simulation contract copied from the official MicroDuck model"
            f" ({SOURCE_URL}, {SOURCE_COMMIT}; Apache-2.0 MJCF)."
            " Meshes are generated from the user's reference images."
        )
    )
    ET.SubElement(
        root,
        "compiler",
        {
            "angle": "radian",
            "coordinate": "local",
            "meshdir": ".",
            "inertiafromgeom": "false",
            "autolimits": "true",
        },
    )
    ET.SubElement(
        root,
        "option",
        {
            "timestep": _fmt(timestep),
            "integrator": "implicitfast",
            "cone": "elliptic",
            "impratio": "10",
            "gravity": "0 0 -9.81",
        },
    )
    ET.SubElement(
        root,
        "size",
        {"njmax": "500", "nconmax": "200"},
    )

    assets = ET.SubElement(root, "asset")
    mesh_files: list[str] = []
    mesh_names: dict[str, str] = {}
    for component in components:
        body_name = slug(component.get("name") or component["id"], "part")
        visual = component.get("mesh_url")
        collision = component.get("collision_mesh_url")
        if visual:
            mesh_name = f"{body_name}_visual"
            ET.SubElement(
                assets,
                "mesh",
                {"name": mesh_name, "file": str(visual).replace("\\", "/")},
            )
            mesh_names[f"{component['id']}:visual"] = mesh_name
            mesh_files.append(str(visual))
        if collision:
            mesh_name = f"{body_name}_collision"
            ET.SubElement(
                assets,
                "mesh",
                {"name": mesh_name, "file": str(collision).replace("\\", "/")},
            )
            mesh_names[f"{component['id']}:collision"] = mesh_name
            mesh_files.append(str(collision))

    visual_default = ET.SubElement(root, "default")
    ET.SubElement(
        visual_default,
        "geom",
        {"friction": "0.8 0.02 0.001", "condim": "3"},
    )
    # Actuator class copied from the official ``chosen_actuator`` class:
    # per-joint tuning lives here, per-actuator gains stay explicit.
    actuator_default = ET.SubElement(
        visual_default, "default", {"class": ACTUATOR_CLASS}
    )
    ET.SubElement(
        actuator_default,
        "joint",
        {
            "damping": _fmt(JOINT_TUNING["damping"]),
            "frictionloss": _fmt(JOINT_TUNING["frictionloss"]),
            "armature": _fmt(JOINT_TUNING["armature"]),
            "limited": "true",
        },
    )
    ET.SubElement(
        actuator_default,
        "position",
        {
            "kp": _fmt(POSITION_FALLBACK["kp"]),
            "kv": _fmt(POSITION_FALLBACK["kv"]),
        },
    )

    worldbody = ET.SubElement(root, "worldbody")
    ET.SubElement(
        worldbody,
        "geom",
        {
            "name": "floor",
            "type": "plane",
            "size": "4 4 0.1",
            "rgba": "0.78 0.76 0.72 1",
            "friction": "0.9 0.02 0.001",
        },
    )
    ET.SubElement(
        worldbody,
        "light",
        {
            "name": "key_light",
            "pos": "0 0 3",
            "dir": "0 0 -1",
            "directional": "true",
            "diffuse": "0.8 0.8 0.8",
        },
    )

    body_names: list[str] = []
    joint_names: list[str] = []
    root_name = slug(roots[0].get("name") or roots[0]["id"], "part")
    root_pos: list[float] = [0.0, 0.0, 0.0]
    # Adjacent collision meshes overlap at the joint by construction (each
    # part is extruded from its own silhouette). Without an exclude pair the
    # solver sees constant interpenetration, generates enormous contact
    # forces, and the joint cannot move. Excluding only directly connected
    # bodies keeps unrelated self-collisions (e.g. arm vs. torso) enabled.
    contact_pairs: list[tuple[str, str]] = []

    def add_body(component: dict[str, Any], parent: ET.Element, is_root: bool) -> None:
        metadata = component.get("metadata") or {}
        placement = metadata.get("placement") or {}
        body_name = slug(component.get("name") or component["id"], "part")
        if is_root:
            pos = placement.get("world_center") or [0.0, 0.0, 0.0]
        else:
            pos = placement.get("body_pos") or [0.0, 0.0, 0.0]
        body = ET.SubElement(
            parent,
            "body",
            {"name": body_name, "pos": _vec(pos, (0.0, 0.0, 0.0))},
        )
        body_names.append(body_name)
        body.append(_inertial(component))

        joint = joints_by_component.get(component["id"])
        if is_root:
            ET.SubElement(body, "freejoint", {"name": f"{body_name}_free"})
            # IMU site on the trunk, like the official model.
            ET.SubElement(body, "site", {"name": IMU_SITE_NAME, "pos": "0 0 0"})
            try:
                parsed = [float(value) for value in pos]
                root_pos[:] = [
                    parsed[i] if i < len(parsed) else 0.0 for i in range(3)
                ]
            except (TypeError, ValueError):
                root_pos[:] = [0.0, 0.0, 0.0]
        elif joint is not None:
            joint_type = str(joint.get("joint_type") or "fixed").lower()
            if joint_type in ("hinge", "slide"):
                joint_name = slug(joint.get("name") or f"{body_name}_joint", "joint")
                attrs = {
                    "name": joint_name,
                    "type": joint_type,
                    "axis": _vec(joint.get("axis"), (0.0, 0.0, 1.0)),
                    "pos": _vec(joint.get("position"), (0.0, 0.0, 0.0)),
                    "class": ACTUATOR_CLASS,
                }
                limits = joint.get("limits")
                if isinstance(limits, (list, tuple)) and len(limits) == 2:
                    lower, upper = float(limits[0]), float(limits[1])
                    if upper > lower:
                        attrs["range"] = f"{_fmt(lower)} {_fmt(upper)}"
                        attrs["limited"] = "true"
                ET.SubElement(body, "joint", attrs)
                joint_names.append(joint_name)
            elif joint_type != "fixed":
                raise MjcfError(
                    f"Joint {joint.get('name')!r} has unsupported type"
                    f" {joint_type!r}; use fixed, hinge, or slide."
                )

        physics = metadata.get("physics") or {}
        friction = float(physics.get("friction") or 0.8)
        visual_name = mesh_names.get(f"{component['id']}:visual")
        collision_name = mesh_names.get(f"{component['id']}:collision")
        if visual_name:
            body.append(
                _geom(
                    f"{body_name}_visual_geom",
                    visual_name,
                    collision=False,
                    friction=friction,
                )
            )
        if collision_name:
            body.append(
                _geom(
                    f"{body_name}_collision_geom",
                    collision_name,
                    collision=True,
                    friction=friction,
                )
            )

        for child in children.get(component["id"], []):
            child_name = slug(child.get("name") or child["id"], "part")
            contact_pairs.append((body_name, child_name))
            add_body(child, body, False)

    add_body(roots[0], worldbody, True)

    if contact_pairs:
        contact_root = ET.SubElement(root, "contact")
        for parent_name, child_name in contact_pairs:
            ET.SubElement(
                contact_root,
                "exclude",
                {"body1": parent_name, "body2": child_name},
            )

    actuator_root = ET.SubElement(root, "actuator")
    for joint in joints:
        actuator = actuators_by_joint.get(joint.get("id"))
        if actuator is None:
            continue
        if str(actuator.get("actuator_type") or "position") != "position":
            raise MjcfError(
                f"Actuator {actuator.get('name')!r} is not a position actuator;"
                " the simulator's joint tests require position control."
            )
        joint_name = slug(joint.get("name") or "", "joint")
        if joint_name not in joint_names:
            continue
        config = actuator.get("configuration") or {}
        attrs = {
            "name": slug(actuator.get("name") or f"{joint_name}_motor", "motor"),
            "joint": joint_name,
            "class": ACTUATOR_CLASS,
        }
        if "kp" in config:
            attrs["kp"] = _fmt(float(config["kp"]))
        if "kv" in config:
            attrs["kv"] = _fmt(float(config["kv"]))
        if "gear" in config:
            attrs["gear"] = _fmt(float(config["gear"]))
        for key in ("ctrlrange", "forcerange"):
            value = config.get(key)
            if isinstance(value, (list, tuple)) and len(value) == 2:
                attrs[key] = f"{_fmt(value[0])} {_fmt(value[1])}"
        ET.SubElement(actuator_root, "position", attrs)

    if not joint_names:
        raise MjcfError(
            "The assembly has no hinge/slide joints; the simulator needs at"
            " least one position-actuated joint."
        )

    sensor_root = ET.SubElement(root, "sensor")
    for joint_name in joint_names:
        ET.SubElement(
            sensor_root,
            "jointpos",
            {"joint": joint_name, "name": f"{joint_name}_pos"},
        )
    for spec in IMU_SENSORS:
        tag = str(spec["tag"])
        attrs = {key: str(value) for key, value in spec.items() if key != "tag"}
        if tag == "subtreeangmom":
            attrs["body"] = root_name
        ET.SubElement(sensor_root, tag, attrs)

    keyframe_root = ET.SubElement(root, "keyframe")
    ET.SubElement(
        keyframe_root,
        "key",
        {
            "name": KEYFRAME_STAND,
            "qpos": " ".join(
                _fmt(value)
                for value in stand_qpos(
                    (root_pos[0], root_pos[1], root_pos[2]), len(joint_names)
                )
            ),
        },
    )

    xml = _pretty(root)
    return MjcfModel(
        xml=xml,
        mesh_files=tuple(dict.fromkeys(mesh_files)),
        joint_names=tuple(joint_names),
        body_names=tuple(body_names),
    )


def _pretty(root: ET.Element) -> str:
    raw = ET.tostring(root, encoding="utf-8")
    parsed = minidom.parseString(raw)
    return parsed.toprettyxml(indent="  ")


__all__ = ["MjcfError", "MjcfModel", "build_mjcf"]
