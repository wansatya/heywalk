"""Shared simulator scene: floor, arena, and props (the ``Scene`` role).

Every bundled robot runs in the SAME world: an infinite ground plane, a
4 m arena bounded by four walls, two obstacle crates, a kickable ball,
and a goal flag. This module is the single source of truth for that
scene — robot compositions (see ``microduck/model.py`` and
``ed209/model.py``) stage it around their robot subtree, and the physics
engine, prompts, and 3D view all read prop names and placements from
here, so physics and rendering cannot disagree and a new robot reuses
the arena instead of duplicating it.

Environment bodies share the ``env_`` prefix so the engine can exclude
them from the *robot* body set (a resting ball never counts as a robot
collision) while genuine robot-obstacle touches still do. Static props
take no qpos slots; the ball is the one dynamic prop (free joint).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

#: Name prefix for simulator-environment bodies (obstacles + goal). Every
#: bundled robot uses this same prefix.
ENV_BODY_PREFIX = "env_"


@dataclass(frozen=True)
class EnvGeom:
    """One primitive geom of a composed-environment body."""

    name: str
    type: str  # "box" | "sphere" | "cylinder"
    size: tuple[float, ...]
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    quat: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)
    rgba: tuple[float, float, float, float] = (0.8, 0.8, 0.8, 1.0)
    #: Presentation role for the 3D view (soccer texture, edge lines, ...).
    role: str = "prop"

    def to_xml(self) -> ET.Element:
        element = ET.Element("geom")
        element.set("name", self.name)
        element.set("type", self.type)
        element.set("size", " ".join(f"{v:g}" for v in self.size))
        element.set("pos", " ".join(f"{v:g}" for v in self.pos))
        element.set("quat", " ".join(f"{v:g}" for v in self.quat))
        element.set("rgba", " ".join(f"{v:g}" for v in self.rgba))
        return element

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "size": list(self.size),
            "pos": list(self.pos),
            "quat": list(self.quat),
            "rgba": list(self.rgba),
            "role": self.role,
        }


@dataclass(frozen=True)
class EnvBody:
    """One environment body added to the composed scene.

    Static props (crates, flag, walls) carry no joint, so they take no qpos
    slots and the robot's keyframes keep working unchanged. A ``free``
    prop (the ball) gets a free joint instead: it is simulated, so a robot
    that walks into it actually kicks it, and the run's evidence can measure
    how far it rolled. Free props stay excluded from robot-body contact
    accounting (see ``MujocoEngine``), and the composed keyframes carry
    their start pose.
    """

    name: str
    pos: tuple[float, float, float]
    geoms: tuple[EnvGeom, ...]
    free: bool = False

    @property
    def free_joint_name(self) -> str:
        return f"{self.name}_free"

    def start_qpos(self) -> tuple[float, ...]:
        """Keyframe qpos slots of a free prop (position + identity quat)."""
        return (*self.pos, 1.0, 0.0, 0.0, 0.0)

    def to_xml(self) -> ET.Element:
        element = ET.Element("body")
        element.set("name", self.name)
        element.set("pos", " ".join(f"{v:g}" for v in self.pos))
        if self.free:
            joint = ET.SubElement(element, "freejoint")
            joint.set("name", self.free_joint_name)
        for geom in self.geoms:
            element.append(geom.to_xml())
        return element

    def to_dict(self) -> dict[str, Any]:
        return {
            "body": self.name,
            "pos": list(self.pos),
            "geoms": [geom.to_dict() for geom in self.geoms],
            "free": self.free,
            "role": self.geoms[0].role if self.geoms else "prop",
        }


#: Arena and wall geometry (metres). The inner faces sit exactly on the
#: arrange bounds. The +x/-x walls butt into the inner faces of the +y/-y
#: walls, while the +y/-y walls run all the way to the outer corners. That
#: gives exact 90° inner and outer corners with no protruding ends (the old
#: overlapping boxes left excess at every corner) and no coplanar faces to
#: z-fight in the 3D view.
ARENA_HALF_EXTENT = 2.0
WALL_HALF_THICKNESS = 0.05
WALL_HALF_HEIGHT = 0.15
WALL_CENTER = ARENA_HALF_EXTENT + WALL_HALF_THICKNESS
#: The +y/-y walls span to the outer corner (centre + half thickness).
WALL_HALF_LENGTH = ARENA_HALF_EXTENT + 2 * WALL_HALF_THICKNESS
#: Dark yellow / golden ochre wall color matching the arena barriers.
WALL_RGBA: tuple[float, float, float, float] = (0.78, 0.57, 0.16, 1.0)

#: Simulator environment composed around every bundled robot: two obstacle
#: crates, a ball, a goal flag, and four arena walls. Crates, the flag and
#: the walls are STATIC (no joints): they take no qpos slots, so the
#: robot STAND/INIT keyframes keep working unchanged. The ball is the one
#: DYNAMIC prop — it has a free joint, so a robot that walks into it kicks
#: it and the run can measure how far it rolled. This is the single source
#: of truth — the composed MJCF and the 3D view payload are both generated
#: from it, so physics and rendering cannot disagree. Walls are FIXED arena
#: bounds (see WALL_BODIES): the arrange UI never drags them and layout
#: overrides skip them, while wall touches count as disqualifying contacts.
ENVIRONMENT: tuple[EnvBody, ...] = (
    EnvBody(
        name="env_box_1",
        pos=(0.45, 0.30, 0.05),
        geoms=(
            EnvGeom(
                name="env_box_1_geom",
                type="box",
                size=(0.05, 0.05, 0.05),
                rgba=(0.85, 0.42, 0.10, 1.0),
                role="crate",
            ),
        ),
    ),
    EnvBody(
        name="env_box_2",
        pos=(-0.35, 0.35, 0.04),
        geoms=(
            EnvGeom(
                name="env_box_2_geom",
                type="box",
                size=(0.04, 0.04, 0.04),
                rgba=(0.85, 0.42, 0.10, 1.0),
                role="crate",
            ),
        ),
    ),
    EnvBody(
        name="env_ball",
        pos=(0.35, -0.30, 0.035),
        geoms=(
            EnvGeom(
                name="env_ball_geom",
                type="sphere",
                size=(0.035,),
                rgba=(0.95, 0.95, 0.95, 1.0),
                role="ball",
            ),
        ),
        free=True,
    ),
    EnvBody(
        name="env_flag",
        pos=(0.80, 0.0, 0.0),
        geoms=(
            EnvGeom(
                name="env_flag_base",
                type="cylinder",
                size=(0.03, 0.01),
                pos=(0.0, 0.0, 0.01),
                rgba=(0.30, 0.32, 0.36, 1.0),
                role="base",
            ),
            EnvGeom(
                name="env_flag_pole",
                type="cylinder",
                size=(0.008, 0.15),
                pos=(0.0, 0.0, 0.17),
                rgba=(0.55, 0.58, 0.62, 1.0),
                role="pole",
            ),
            EnvGeom(
                name="env_flag_cloth",
                type="box",
                size=(0.05, 0.002, 0.03),
                pos=(0.055, 0.0, 0.29),
                rgba=(0.20, 0.83, 0.45, 1.0),
                role="flag",
            ),
        ),
    ),
    # Arena walls: inner faces exactly on the ±2 m arrange bounds, 0.3 m
    # tall, 0.1 m thick. The x walls butt into the y walls; the y walls span
    # the corners, so every corner is a clean 90° joint with no excess.
    EnvBody(
        name="env_wall_px",
        pos=(WALL_CENTER, 0.0, WALL_HALF_HEIGHT),
        geoms=(
            EnvGeom(
                name="env_wall_px_geom",
                type="box",
                size=(WALL_HALF_THICKNESS, ARENA_HALF_EXTENT, WALL_HALF_HEIGHT),
                rgba=WALL_RGBA,
                role="wall",
            ),
        ),
    ),
    EnvBody(
        name="env_wall_nx",
        pos=(-WALL_CENTER, 0.0, WALL_HALF_HEIGHT),
        geoms=(
            EnvGeom(
                name="env_wall_nx_geom",
                type="box",
                size=(WALL_HALF_THICKNESS, ARENA_HALF_EXTENT, WALL_HALF_HEIGHT),
                rgba=WALL_RGBA,
                role="wall",
            ),
        ),
    ),
    EnvBody(
        name="env_wall_py",
        pos=(0.0, WALL_CENTER, WALL_HALF_HEIGHT),
        geoms=(
            EnvGeom(
                name="env_wall_py_geom",
                type="box",
                size=(WALL_HALF_LENGTH, WALL_HALF_THICKNESS, WALL_HALF_HEIGHT),
                rgba=WALL_RGBA,
                role="wall",
            ),
        ),
    ),
    EnvBody(
        name="env_wall_ny",
        pos=(0.0, -WALL_CENTER, WALL_HALF_HEIGHT),
        geoms=(
            EnvGeom(
                name="env_wall_ny_geom",
                type="box",
                size=(WALL_HALF_LENGTH, WALL_HALF_THICKNESS, WALL_HALF_HEIGHT),
                rgba=WALL_RGBA,
                role="wall",
            ),
        ),
    ),
)


#: Fixed arena-bound bodies: never draggable, never layout-movable.
WALL_BODIES: tuple[str, ...] = (
    "env_wall_px",
    "env_wall_nx",
    "env_wall_py",
    "env_wall_ny",
)


#: Shared ground plane staged by every robot composition.
FLOOR_XML = '<geom name="floor" type="plane" size="0 0 0.05" pos="0 0 0"/>'


class SceneError(RuntimeError):
    """Raised when the shared scene cannot be composed around a robot."""


def compose_robot_scene(
    robot_root: ET.Element,
    *,
    keyframe: ET.Element | None = None,
    attach_keyframe: bool = False,
    header: str = "",
) -> str:
    """Stage the shared scene around a robot subtree; return one MJCF file.

    The robot subtree is preserved element-for-element; the only additions
    are the ground plane (unless the robot file already stages one), the
    simulator environment from :data:`ENVIRONMENT`, and — when the caller
    passes the robot's ``<keyframe>`` — free-prop start qpos appended to
    every key (MuJoCo keys must cover every degree of freedom). Set
    ``attach_keyframe`` when the keyframe lives in another tree and must
    be appended; an inline keyframe is extended in place. The result is a
    single file so untrusted-input rules (no ``<include>``) keep applying
    to everything the engine loads.
    """
    worldbody = robot_root.find("worldbody")
    if worldbody is None:
        raise SceneError("Robot MJCF has no <worldbody>; nothing to stage.")
    if worldbody.find("./geom[@type='plane']") is None:
        floor = ET.fromstring(FLOOR_XML)
        floor.tail = "\n    "
        worldbody.insert(0, floor)
    for env_body in ENVIRONMENT:
        element = env_body.to_xml()
        element.tail = "\n    "
        worldbody.append(element)
    if keyframe is not None:
        # Free props add qpos slots after the robot's trunk+hinge values;
        # a key whose qpos does not cover them fails to compile.
        free_qpos = " ".join(
            f"{value:g}"
            for body in ENVIRONMENT
            if body.free
            for value in body.start_qpos()
        )
        if free_qpos:
            for key in keyframe.iter("key"):
                raw = (key.get("qpos") or "").strip()
                if not raw:
                    continue
                key.set("qpos", f"{raw} {free_qpos}")
        if attach_keyframe:
            robot_root.append(keyframe)
    xml = ET.tostring(robot_root, encoding="unicode")
    return header + xml


def environment_payload() -> list[dict[str, Any]]:
    """Composed-environment bodies for the 3D view (see :data:`ENVIRONMENT`)."""
    return [body.to_dict() for body in ENVIRONMENT]


#: Goal body for locomotion runs: the robot walks to this flag. Derived
#: from ENVIRONMENT so the name cannot drift from the composed scene.
FLAG_BODY = "env_flag"

#: The kickable prop: a free (dynamic) body, so walking into it really
#: kicks it and the run measures how far it rolled.
BALL_BODY = "env_ball"

#: Human labels for the composed props. Used by the code-generation brief so
#: the model can map "the box in front of you" onto a body name instead of
#: inventing coordinates, and by the run UI to name the objective. Front/behind
#: are stated relative to the spawn frame (robot at the origin facing +x), which
#: is what the task text says; the exact offsets live in the coordinates.
PROP_LABELS: dict[str, str] = {
    "env_box_1": "orange crate in front of the start",
    "env_box_2": "orange crate behind the start",
    "env_ball": "white soccer ball (rolls when pushed)",
    "env_flag": "green goal flag",
}


def body_geom_names(body: str) -> tuple[str, ...]:
    """Collision-geom names of one composed environment body."""
    for env_body in ENVIRONMENT:
        if env_body.name == body:
            return tuple(geom.name for geom in env_body.geoms)
    return ()


def flag_geom_names() -> tuple[str, ...]:
    """Collision-geom names of the goal flag (never disqualifying)."""
    return body_geom_names(FLAG_BODY)


def ball_geom_names() -> tuple[str, ...]:
    """Collision-geom names of the ball (the aim of a kick run)."""
    return body_geom_names(BALL_BODY)


def prop_body_names() -> tuple[str, ...]:
    """Movable/aimable prop bodies, in composed-scene order."""
    return tuple(body.name for body in ENVIRONMENT if body.name not in WALL_BODIES)


def prop_catalog() -> list[dict[str, Any]]:
    """Prompt/UI-friendly description of the composed props."""
    return [
        {
            "body": body.name,
            "label": PROP_LABELS.get(body.name, body.name),
            "kind": body.geoms[0].role if body.geoms else "prop",
            "x": body.pos[0],
            "y": body.pos[1],
            "free": body.free,
            "geoms": [geom.name for geom in body.geoms],
        }
        for body in ENVIRONMENT
        if body.name not in WALL_BODIES
    ]


def wall_geom_names() -> tuple[str, ...]:
    """Collision-geom names of the arena walls (touch runs aim for them)."""
    names: list[str] = []
    for body in ENVIRONMENT:
        if body.name in WALL_BODIES:
            names.extend(geom.name for geom in body.geoms)
    return tuple(names)


__all__ = [
    "ARENA_HALF_EXTENT",
    "BALL_BODY",
    "ENV_BODY_PREFIX",
    "ENVIRONMENT",
    "FLAG_BODY",
    "FLOOR_XML",
    "PROP_LABELS",
    "WALL_BODIES",
    "WALL_CENTER",
    "WALL_HALF_HEIGHT",
    "WALL_HALF_LENGTH",
    "WALL_HALF_THICKNESS",
    "WALL_RGBA",
    "EnvBody",
    "EnvGeom",
    "SceneError",
    "ball_geom_names",
    "body_geom_names",
    "compose_robot_scene",
    "environment_payload",
    "flag_geom_names",
    "prop_body_names",
    "prop_catalog",
    "wall_geom_names",
]
