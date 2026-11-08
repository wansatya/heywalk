"""Official MicroDuck robot model (the ``RobotModel`` role).

This module is the single entry point for the vendored Pollen Robotics
MicroDuck MuJoCo model. It exposes model metadata (joints, actuators,
sensors, physics parameters) discovered from the official files, composes a
stand-alone single-file scene for simulation, and stages mesh assets into run
workspaces.

Official sources (verified, see ATTRIBUTION.md in this directory):

- model/scenes: https://github.com/pollen-robotics/microduck_rl
  (``src/mjlab_microduck/robot/microduck/``)
- ``robot_groundcontact.xml`` is the model the official default scene
  (``scene.xml``, loaded by ``sim/body_server.py``) uses.
- physics: ``TIMESTEP = 0.005``, 50 Hz control, MuJoCo default integrator
  (the official MJCF ships no ``<option>`` and the official runtime sets no
  integrator).

Licenses: MJCF XML is Apache-2.0; ``assets/*.stl`` is CC BY-NC-SA 4.0
(see LICENSE-MODELS.md). No hardware/manufacturing rights are included.
"""

from __future__ import annotations

import math
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from functools import lru_cache
from typing import Any

# Shared simulator scene (floor, arena, props): imported here so the
# long-standing microduck.* import paths keep working unchanged.
from ..scene import (
    BALL_BODY,
    ENV_BODY_PREFIX,
    ENVIRONMENT,
    FLAG_BODY,
    PROP_LABELS,
    WALL_BODIES,
    EnvBody,
    EnvGeom,
    SceneError,
    ball_geom_names,
    body_geom_names,
    compose_robot_scene,
    environment_payload,
    flag_geom_names,
    prop_body_names,
    prop_catalog,
    wall_geom_names,
)

MODEL_DIR = Path(__file__).resolve().parent
ROBOT_XML = "robot_groundcontact.xml"
SCENE_XML = "scene.xml"
ASSETS_DIR = "assets"

SOURCE_URL = "https://github.com/pollen-robotics/microduck_rl"
SOURCE_MODEL_PATH = "src/mjlab_microduck/robot/microduck/robot_groundcontact.xml"
SOURCE_COMMIT = "bae39f5900b318f7e87fc485c30a31213cfa14eb"
MODEL_LICENSE = "Apache-2.0 (MJCF); CC BY-NC-SA 4.0 (meshes)"

#: Official runtime physics (microduck_rl ``sim/body_server.py``).
OFFICIAL_TIMESTEP = 0.005
OFFICIAL_CONTROL_HZ = 50.0
OFFICIAL_KEYFRAME = "STAND"

#: Geoms the official export config marks for ground contact that are
#: expected to touch the floor while standing (from
#: ``config_mjcf_walk.json`` post-import commands).
EXPECTED_GROUND_GEOMS: tuple[str, ...] = (
    "left_foot_collision",
    "right_foot_collision",
)

#: Trunk height (m) below which the robot is considered fallen. Standing is
#: ~0.12 m; a collapse onto the ground settles near ~0.04 m (measured).
FALL_TRUNK_Z = 0.08

TRUNK_BODY = "trunk_base"

#: Marker comment stamped on composed scenes so installers, prompts, and
#: tests can tell an official-model composition from generated MJCF.
COMPOSED_MARKER = "HeyWalk official-MicroDuck composition"

class MicroduckModelError(RuntimeError):
    """Raised when the vendored MicroDuck model cannot be loaded."""


@dataclass(frozen=True)
class MicroduckMetadata:
    """Model facts discovered from the official files (never hardcoded)."""

    joints: tuple[str, ...]
    actuators: tuple[str, ...]
    sensors: tuple[str, ...]
    bodies: tuple[str, ...]
    joint_ranges: dict[str, tuple[float | None, float | None]]
    keyframes: tuple[str, ...]
    mesh_files: tuple[str, ...]
    timestep: float = OFFICIAL_TIMESTEP
    control_hz: float = OFFICIAL_CONTROL_HZ
    source_url: str = SOURCE_URL
    source_commit: str = SOURCE_COMMIT
    license: str = MODEL_LICENSE

    def to_dict(self) -> dict[str, Any]:
        return {
            "joints": list(self.joints),
            "actuators": list(self.actuators),
            "sensors": list(self.sensors),
            "bodies": list(self.bodies),
            "joint_ranges": {
                name: {"lower": lo, "upper": hi}
                for name, (lo, hi) in self.joint_ranges.items()
            },
            "keyframes": list(self.keyframes),
            "mesh_files": list(self.mesh_files),
            "timestep": self.timestep,
            "control_hz": self.control_hz,
            "source_url": self.source_url,
            "source_commit": self.source_commit,
            "license": self.license,
        }


def _parse_official(path: Path) -> ET.Element:
    try:
        return ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise MicroduckModelError(
            f"Vendored MicroDuck file {path.name!r} is not valid XML: {exc}."
        ) from None
    except OSError as exc:
        raise MicroduckModelError(
            f"Vendored MicroDuck file {path.name!r} is unreadable: {exc}."
        ) from None


def _joint_range(element: ET.Element) -> tuple[float | None, float | None]:
    raw = (element.get("range") or "").strip()
    if not raw:
        return None, None
    parts = raw.split()
    if len(parts) != 2:
        raise MicroduckModelError(
            f"Joint {element.get('name')!r} has a malformed range."
        )
    return float(parts[0]), float(parts[1])


def load_metadata() -> MicroduckMetadata:
    """Read joints, actuators, sensors, and meshes from the official files."""
    robot_path = MODEL_DIR / ROBOT_XML
    if not robot_path.is_file():
        raise MicroduckModelError(
            f"Vendored MicroDuck model {ROBOT_XML!r} is missing from"
            f" {MODEL_DIR}. Reinstall the application."
        )
    root = _parse_official(robot_path)
    joints: list[str] = []
    ranges: dict[str, tuple[float | None, float | None]] = {}
    for joint in root.iter("joint"):
        name = (joint.get("name") or "").strip()
        if not name or joint.get("type", "hinge") != "hinge":
            continue
        joints.append(name)
        ranges[name] = _joint_range(joint)
    actuator_root = root.find("actuator")
    actuators = tuple(
        (actuator.get("name") or "").strip()
        for actuator in (
            actuator_root if actuator_root is not None else []
        )
    )
    sensor_root = root.find("sensor")
    sensors = tuple(
        (sensor.get("name") or "").strip()
        for sensor in (sensor_root if sensor_root is not None else [])
        if (sensor.get("name") or "").strip()
    )
    bodies = tuple(
        (body.get("name") or "").strip()
        for body in root.iter("body")
        if (body.get("name") or "").strip()
    )
    meshes = tuple(
        sorted(
            {
                element.get("file", "").strip()
                for element in root.iter("mesh")
                if (element.get("file") or "").strip()
            }
        )
    )
    scene_root = _parse_official(MODEL_DIR / SCENE_XML)
    keyframe_root = scene_root.find("keyframe")
    keyframes = tuple(
        (key.get("name") or "").strip()
        for key in (keyframe_root if keyframe_root is not None else [])
        if (key.get("name") or "").strip()
    )
    return MicroduckMetadata(
        joints=tuple(joints),
        actuators=actuators,
        sensors=sensors,
        bodies=bodies,
        joint_ranges=ranges,
        keyframes=keyframes,
        mesh_files=meshes,
    )


def compose_scene_xml() -> str:
    """Build a stand-alone single-file scene from the official model.

    The official robot subtree is preserved element-for-element; the only
    additions are the shared simulator scene (ground plane plus the
    environment from :mod:`app.simulator.scene`: static obstacle crates,
    a kickable ball, and a goal flag) and the official keyframes (from
    ``scene.xml``). Free props extend each keyframe with their start qpos,
    because MuJoCo keys must cover every degree of freedom. The result is
    a single file so untrusted-input rules (no ``<include>``) keep
    applying to everything the engine loads.
    """
    robot_root = _parse_official(MODEL_DIR / ROBOT_XML)
    scene_root = _parse_official(MODEL_DIR / SCENE_XML)
    header = (
        f"<!-- {COMPOSED_MARKER}: official {ROBOT_XML} ({SOURCE_URL},"
        f" commit {SOURCE_COMMIT[:12]}) + ground plane + {len(ENVIRONMENT)}"
        " environment bodies + official keyframes. Robot subtree is"
        " unchanged; see ATTRIBUTION.md. -->\n"
    )
    try:
        return compose_robot_scene(
            robot_root,
            keyframe=scene_root.find("keyframe"),
            attach_keyframe=True,
            header=header,
        )
    except SceneError as exc:
        raise MicroduckModelError(
            f"Vendored {ROBOT_XML!r} has no <worldbody>."
        ) from exc


def is_composed_scene(xml_text: str) -> bool:
    """True when an MJCF text is this module's official-model composition."""
    return COMPOSED_MARKER in xml_text[:4096]


def missing_assets() -> tuple[str, ...]:
    """Mesh files the official model references but the bundle lacks."""
    metadata = load_metadata()
    missing = [
        name
        for name in metadata.mesh_files
        if not (MODEL_DIR / ASSETS_DIR / name).is_file()
    ]
    return tuple(missing)


def check_assets() -> None:
    """Raise an actionable error when vendored meshes are missing."""
    missing = missing_assets()
    if missing:
        preview = ", ".join(missing[:5])
        raise MicroduckModelError(
            "MicroDuck mesh assets are missing from the bundle"
            f" ({preview}{'...' if len(missing) > 5 else ''}). Reinstall the"
            " application; the model cannot load without its meshes."
        )


def stage_assets(workspace: Path) -> int:
    """Copy bundled meshes into a run workspace; return files written."""
    check_assets()
    target_dir = workspace / ASSETS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for name in load_metadata().mesh_files:
        target = target_dir / name
        if target.is_file():
            continue
        shutil.copyfile(MODEL_DIR / ASSETS_DIR / name, target)
        count += 1
    return count


def body_hierarchy() -> list[dict[str, str | None]]:
    """Parent/child body links for the skeleton renderer (official order)."""
    root = _parse_official(MODEL_DIR / ROBOT_XML)
    worldbody = root.find("worldbody")
    if worldbody is None:
        raise MicroduckModelError(
            f"Vendored {ROBOT_XML!r} has no <worldbody>."
        )
    links: list[dict[str, str | None]] = []

    def visit(element: ET.Element, parent: str | None) -> None:
        for body in element.findall("body"):
            name = (body.get("name") or "").strip() or None
            links.append({"body": name, "parent": parent})
            visit(body, name)

    visit(worldbody, None)
    return links


def _vec(text: str | None, size: int, default: tuple[float, ...]) -> tuple[float, ...]:
    if not (text or "").strip():
        return default
    parts = (text or "").split()
    if len(parts) != size:
        raise MicroduckModelError(
            f"Expected {size} numbers in {text!r}."
        )
    try:
        return tuple(float(part) for part in parts)
    except ValueError:
        raise MicroduckModelError(
            f"Malformed numbers in {text!r}."
        ) from None


def visual_geoms() -> list[dict[str, Any]]:
    """Visual mesh placements for the 3D view, discovered from the MJCF.

    One entry per ``class=\"visual\"`` mesh geom: the body it is attached
    to, the mesh file, the geom-local pos/quat offset, and the resolved
    material color. The renderer attaches each mesh to its body group with
    the offset, then poses the group from live MuJoCo body poses.
    """
    root = _parse_official(MODEL_DIR / ROBOT_XML)
    asset = root.find("asset")
    mesh_files: dict[str, str] = {}
    if asset is not None:
        for mesh in asset.iter("mesh"):
            file_attr = (mesh.get("file") or "").strip()
            if not file_attr:
                continue
            base = file_attr.rsplit("/", 1)[-1]
            stem = base.rsplit(".", 1)[0] if "." in base else base
            mesh_files[base] = base
            mesh_files[stem] = base
            name = (mesh.get("name") or "").strip()
            if name:
                mesh_files[name] = base
    materials: dict[str, tuple[float, ...]] = {}
    if asset is not None:
        for material in asset.iter("material"):
            name = (material.get("name") or "").strip()
            rgba = (material.get("rgba") or "").strip()
            if name and rgba:
                materials[name] = _vec(rgba, 4, (1.0, 1.0, 1.0, 1.0))
    worldbody = root.find("worldbody")
    if worldbody is None:
        raise MicroduckModelError(
            f"Vendored {ROBOT_XML!r} has no <worldbody>."
        )
    placements: list[dict[str, Any]] = []

    def visit(element: ET.Element) -> None:
        for body in element.findall("body"):
            body_name = (body.get("name") or "").strip()
            for geom in body.findall("geom"):
                if geom.get("class") != "visual":
                    continue
                mesh_ref = (geom.get("mesh") or "").strip()
                if not mesh_ref or mesh_ref not in mesh_files:
                    raise MicroduckModelError(
                        f"Visual geom on body {body_name!r} references"
                        f" unknown mesh {mesh_ref!r}."
                    )
                material = (geom.get("material") or "").strip()
                placements.append(
                    {
                        "body": body_name,
                        "mesh": mesh_ref,
                        "file": mesh_files[mesh_ref],
                        "pos": list(
                            _vec(geom.get("pos"), 3, (0.0, 0.0, 0.0))
                        ),
                        "quat": list(
                            _vec(
                                geom.get("quat"),
                                4,
                                (1.0, 0.0, 0.0, 0.0),
                            )
                        ),
                        "rgba": list(
                            materials.get(
                                material, (1.0, 1.0, 1.0, 1.0)
                            )
                        ),
                    }
                )
            visit(body)

    visit(worldbody)
    return placements


def _quat_mul(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    wa, xa, ya, za = a
    wb, xb, yb, zb = b
    return (
        wa * wb - xa * xb - ya * yb - za * zb,
        wa * xb + xa * wb + ya * zb - za * yb,
        wa * yb - xa * zb + ya * wb + za * xb,
        wa * zb + xa * yb - ya * xb + za * wb,
    )


def _quat_rotate(
    q: tuple[float, float, float, float],
    v: tuple[float, float, float],
) -> tuple[float, float, float]:
    w, x, y, z = q
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    if norm <= 0.0:
        raise MicroduckModelError("Zero-length quaternion in model.")
    inv = 1.0 / norm
    # q * (0, v) * q_conj, expanded; the quaternion is normalized first.
    ux, uy, uz = x * inv, y * inv, z * inv
    s = w * inv
    dot = ux * v[0] + uy * v[1] + uz * v[2]
    cx = uy * v[2] - uz * v[1]
    cy = uz * v[0] - ux * v[2]
    cz = ux * v[1] - uy * v[0]
    k = 2.0 * s
    return (
        (s * s - (ux * ux + uy * uy + uz * uz)) * v[0]
        + 2.0 * dot * ux
        + k * cx,
        (s * s - (ux * ux + uy * uy + uz * uz)) * v[1]
        + 2.0 * dot * uy
        + k * cy,
        (s * s - (ux * ux + uy * uy + uz * uz)) * v[2]
        + 2.0 * dot * uz
        + k * cz,
    )


def _axis_angle(
    axis: tuple[float, float, float], angle: float
) -> tuple[float, float, float, float]:
    norm = math.sqrt(sum(v * v for v in axis))
    if norm <= 0.0:
        raise MicroduckModelError("Zero-length joint axis in model.")
    half = angle / 2.0
    s = math.sin(half) / norm
    return (
        math.cos(half),
        axis[0] * s,
        axis[1] * s,
        axis[2] * s,
    )


def _stand_keyframe() -> tuple[
    tuple[float, float, float],
    tuple[float, float, float, float],
    list[tuple[str, float]],
    ET.Element,
]:
    """Official STAND keyframe: trunk pose, hinge angles, worldbody.

    Hinge angles are in MuJoCo qpos order (document order), matching the
    actuator order. Shared by the 3D preview FK and the agent brief so the
    STAND home values come from one parse of the official files.
    """
    robot_root = _parse_official(MODEL_DIR / ROBOT_XML)
    worldbody = robot_root.find("worldbody")
    if worldbody is None:
        raise MicroduckModelError(
            f"Vendored {ROBOT_XML!r} has no <worldbody>."
        )
    scene_root = _parse_official(MODEL_DIR / SCENE_XML)
    keyframe = scene_root.find("keyframe")
    if keyframe is None:
        raise MicroduckModelError(
            f"Vendored {SCENE_XML!r} defines no keyframes."
        )
    stand = next(
        (
            key
            for key in keyframe.findall("key")
            if (key.get("name") or "").strip() == OFFICIAL_KEYFRAME
        ),
        None,
    )
    if stand is None:
        raise MicroduckModelError(
            f"Vendored {SCENE_XML!r} defines no {OFFICIAL_KEYFRAME!r} key."
        )
    try:
        qpos = [float(v) for v in (stand.get("qpos") or "").split()]
    except ValueError:
        raise MicroduckModelError(
            f"{OFFICIAL_KEYFRAME!r} keyframe qpos is malformed."
        ) from None
    hinges = [
        joint
        for joint in worldbody.iter("joint")
        if (joint.get("type", "hinge") or "hinge") == "hinge"
    ]
    # This reads the official robot file (no environment props), so its only
    # free joint is the trunk: 7 base values followed by one per hinge.
    if len(qpos) != 7 + len(hinges):
        raise MicroduckModelError(
            f"{OFFICIAL_KEYFRAME!r} keyframe holds {len(qpos)} values for"
            f" {len(hinges)} hinges."
        )
    ordered = [
        ((joint.get("name") or "").strip(), qpos[7 + index])
        for index, joint in enumerate(hinges)
    ]
    trunk_pos = (qpos[0], qpos[1], qpos[2])
    trunk_quat = (qpos[3], qpos[4], qpos[5], qpos[6])
    return trunk_pos, trunk_quat, ordered, worldbody


@lru_cache(maxsize=1)
def _stand_joint_targets_cached() -> tuple[tuple[str, float], ...]:
    _, _, ordered, _ = _stand_keyframe()
    return tuple(ordered)


def stand_joint_targets() -> list[tuple[str, float]]:
    """STAND home values per actuated joint, in actuator order.

    Given to the code-generation agent so a new controller holds a sane
    pose from the first tick instead of zeroing joints it has never seen.
    Must match the bundled example controller's STAND_POSE (tested).
    Cached: resolving them parses the vendored MJCF files (~1.3 ms), which
    the 50 Hz locomotion policy cannot afford per tick.
    """
    return [(name, angle) for name, angle in _stand_joint_targets_cached()]


def stand_poses() -> list[dict[str, Any]]:
    """World body poses of the official STAND keyframe (pure-XML FK).

    Walks the body tree applying each body's local pos/quat plus its
    joint's STAND angle, starting from the keyframe's free-joint pose.
    Needs no physics engine, so the API can serve the 3D preview pose;
    a test verifies every pose against MuJoCo's own forward kinematics.
    """
    trunk_pos, trunk_quat, ordered, worldbody = _stand_keyframe()
    angles = dict(ordered)
    poses: list[dict[str, Any]] = []

    def visit(
        element: ET.Element,
        parent_pos: tuple[float, float, float],
        parent_quat: tuple[float, float, float, float],
        is_trunk_level: bool,
    ) -> None:
        for body in element.findall("body"):
            name = (body.get("name") or "").strip()
            local_pos = _vec(body.get("pos"), 3, (0.0, 0.0, 0.0))
            local_quat = _vec(
                body.get("quat"), 4, (1.0, 0.0, 0.0, 0.0)
            )
            if is_trunk_level and name == TRUNK_BODY:
                world_pos = trunk_pos
                world_quat = trunk_quat
            else:
                world_pos = tuple(
                    p + r
                    for p, r in zip(
                        parent_pos, _quat_rotate(parent_quat, local_pos)
                    )
                )
                world_quat = _quat_mul(parent_quat, local_quat)
                joints = body.findall("joint")
                if len(joints) > 1:
                    raise MicroduckModelError(
                        f"Body {name!r} has several joints; preview FK"
                        " supports one."
                    )
                if joints:
                    joint = joints[0]
                    axis = _vec(joint.get("axis"), 3, (0.0, 0.0, 1.0))
                    angle = angles.get(
                        (joint.get("name") or "").strip(), 0.0
                    )
                    world_quat = _quat_mul(
                        world_quat, _axis_angle(axis, angle)
                    )
            poses.append(
                {
                    "body": name,
                    "pos": [world_pos[0], world_pos[1], world_pos[2]],
                    "quat": [
                        world_quat[0],
                        world_quat[1],
                        world_quat[2],
                        world_quat[3],
                    ],
                }
            )
            visit(body, world_pos, world_quat, False)

    visit(worldbody, (0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0), True)
    return poses


def resolve_asset(filename: str) -> Path:
    """Resolve a vendored mesh filename to its on-disk path.

    The name must be one of the mesh files the official model references;
    anything else raises, so the asset endpoint can never serve arbitrary
    host files.
    """
    allowed = set(load_metadata().mesh_files)
    if filename not in allowed:
        raise MicroduckModelError(
            f"Unknown MicroDuck mesh asset {filename!r}."
        )
    path = (MODEL_DIR / ASSETS_DIR / filename).resolve()
    root = (MODEL_DIR / ASSETS_DIR).resolve()
    if path != root and root not in path.parents:
        raise MicroduckModelError(
            f"Mesh asset path escapes the bundle: {filename!r}."
        )
    if not path.is_file():
        raise MicroduckModelError(
            f"Mesh asset {filename!r} is missing from the bundle."
        )
    return path


__all__ = [
    "ASSETS_DIR",
    "BALL_BODY",
    "COMPOSED_MARKER",
    "ENV_BODY_PREFIX",
    "ENVIRONMENT",
    "EXPECTED_GROUND_GEOMS",
    "FALL_TRUNK_Z",
    "FLAG_BODY",
    "MODEL_DIR",
    "OFFICIAL_CONTROL_HZ",
    "OFFICIAL_KEYFRAME",
    "OFFICIAL_TIMESTEP",
    "PROP_LABELS",
    "ROBOT_XML",
    "SCENE_XML",
    "SOURCE_COMMIT",
    "SOURCE_URL",
    "TRUNK_BODY",
    "WALL_BODIES",
    "EnvBody",
    "EnvGeom",
    "MicroduckMetadata",
    "MicroduckModelError",
    "ball_geom_names",
    "body_geom_names",
    "body_hierarchy",
    "check_assets",
    "compose_scene_xml",
    "environment_payload",
    "flag_geom_names",
    "is_composed_scene",
    "load_metadata",
    "missing_assets",
    "prop_body_names",
    "prop_catalog",
    "resolve_asset",
    "stage_assets",
    "stand_joint_targets",
    "stand_poses",
    "visual_geoms",
    "wall_geom_names",
]
