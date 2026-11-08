"""Mechanical assembly stage.

Turns segmented/reconstructed components into a reviewable articulated
hierarchy: parent/child links, body offsets, joint anchors, joint types,
axes, limits, and actuator defaults. Nothing here is claimed to be the
robot's real internal mechanism: the hierarchy is inferred from visible
layout and is explicitly marked as an estimate.

The output is plain data (dicts) so the MJCF generator and the UI share the
same model, and a future AI assembly stage can replace this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .physics import (
    DEFAULT_FILL_FACTOR,
    DEFAULT_FRICTION,
    actuator_defaults,
    estimate_box_physics,
)
from .store import slug
from .template_microduck import ROLE_RANGES

#: Per-category height prior, metres. Must match reconstruction.py.
CATEGORY_HEIGHT_M = {
    # Product default: a MicroDuck-style biped (~0.30 m tall).
    "biped": 0.30,
    "humanoid": 1.55,
    "wheeled": 0.65,
    "vehicle": 0.65,
    "rover": 0.65,
    "arm": 1.0,
    "manipulator": 1.0,
}


@dataclass
class Placement:
    component_id: str
    name: str
    label: str
    component_type: str
    parent_id: str | None
    body_pos: tuple[float, float, float]
    joint_pos: tuple[float, float, float]
    dimensions: tuple[float, float, float]
    mass: float
    joint_type: str
    axis: tuple[float, float, float]
    limits: tuple[float, float] | None
    actuator: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _category_scale(category: str, foreground_height_px: float) -> float:
    height = CATEGORY_HEIGHT_M.get((category or "biped").lower(), 1.4)
    return height / max(1.0, foreground_height_px)


def _box(component: dict[str, Any]) -> tuple[int, int, int, int]:
    raw = (component.get("metadata") or {}).get("box")
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            return tuple(int(round(float(value))) for value in raw)  # type: ignore[return-value]
        except (TypeError, ValueError):
            pass
    return (0, 0, 100, 100)


def _dims(component: dict[str, Any], scale: float) -> tuple[float, float, float]:
    raw = (component.get("metadata") or {}).get("dimensions")
    if isinstance(raw, (list, tuple)) and len(raw) == 3:
        try:
            dims = tuple(max(0.005, float(value)) for value in raw)
            if all(math.isfinite(value) for value in dims):
                return dims  # type: ignore[return-value]
        except (TypeError, ValueError):
            pass
    x, y, w, h = _box(component)
    width = max(0.01, w * scale)
    height = max(0.01, h * scale)
    return (width, max(0.01, min(width, height) * 0.55), height)


def _root_component(components: list[dict[str, Any]]) -> dict[str, Any]:
    def score(component: dict[str, Any]) -> tuple[int, float]:
        kind = str(component.get("component_type") or "").lower()
        rank = 3 if kind in ("torso", "base") else 1
        _x, _y, w, h = _box(component)
        return rank, float(w * h)

    return max(components, key=score)


def _parent_for(
    component: dict[str, Any],
    root: dict[str, Any],
    components: list[dict[str, Any]],
    category: str,
) -> str | None:
    if component["id"] == root["id"]:
        return None
    label = str(component.get("label") or component.get("name") or "").lower()
    by_label = {
        str(item.get("label") or item.get("name") or "").lower(): item
        for item in components
        if item["id"] != component["id"]
    }
    # Simple manipulator chain: upper arm -> fore arm -> gripper.
    if "gripper" in label and "fore_arm" in by_label:
        return by_label["fore_arm"]["id"]
    if "fore_arm" in label and "upper_arm" in by_label:
        return by_label["upper_arm"]["id"]
    if "upper_arm" in label and "base" in by_label:
        return by_label["base"]["id"]
    return root["id"]


def _joint_spec(
    component: dict[str, Any], root_id: str
) -> tuple[str, tuple[float, float, float], tuple[float, float] | None]:
    if component["id"] == root_id:
        return "free", (0.0, 0.0, 0.0), None
    kind = str(component.get("component_type") or "").lower()
    label = str(component.get("label") or component.get("name") or "").lower()
    if kind == "wheel":
        return "hinge", (1.0, 0.0, 0.0), (-math.pi, math.pi)
    if kind == "head" or "head" in label:
        # Yaw hinge like the duck's head_yaw; range copied from the template.
        return "hinge", (0.0, 0.0, 1.0), ROLE_RANGES["head_yaw"]
    if kind == "arm" or "arm" in label:
        return "hinge", (1.0, 0.0, 0.0), ROLE_RANGES["pitch"]
    if kind == "leg" or "leg" in label:
        return "hinge", (1.0, 0.0, 0.0), ROLE_RANGES["pitch"]
    if kind in ("torso", "base"):
        return "fixed", (0.0, 0.0, 0.0), None
    return "fixed", (0.0, 0.0, 0.0), None


def _mass_and_inertia(
    dimensions: tuple[float, float, float], fill: float
) -> tuple[float, tuple[float, float, float]]:
    estimate = estimate_box_physics(dimensions, fill=fill)
    return estimate.mass, estimate.inertia


def build_assembly(
    components: list[dict[str, Any]],
    *,
    category: str = "biped",
    image_size: tuple[int, int] | None = None,
    foreground_box: tuple[int, int, int, int] | None = None,
) -> list[Placement]:
    """Build parent links, body offsets, joints, and physics estimates."""
    if not components:
        return []
    if foreground_box is None:
        boxes = [_box(component) for component in components]
        left = min(box[0] for box in boxes)
        top = min(box[1] for box in boxes)
        right = max(box[0] + box[2] for box in boxes)
        bottom = max(box[1] + box[3] for box in boxes)
        foreground_box = (left, top, max(1, right - left), max(1, bottom - top))
    if image_size is None:
        width = max(1, foreground_box[0] + foreground_box[2])
        height = max(1, foreground_box[1] + foreground_box[3])
        image_size = (width, height)
    scale = _category_scale(category, foreground_box[3])
    union_cx = foreground_box[0] + foreground_box[2] / 2.0
    union_bottom = foreground_box[1] + foreground_box[3]

    root = _root_component(components)
    placements: list[Placement] = []
    centers: dict[str, tuple[float, float, float]] = {}
    dims_by_id: dict[str, tuple[float, float, float]] = {}
    for component in components:
        dims = _dims(component, scale)
        dims_by_id[component["id"]] = dims
        x, y, w, h = _box(component)
        cx = x + w / 2.0
        cy = y + h / 2.0
        centers[component["id"]] = (
            (cx - union_cx) * scale,
            0.0,
            (union_bottom - cy) * scale,
        )

    for component in components:
        component_id = component["id"]
        name = component.get("name") or slug(component.get("label") or "part")
        parent_id = _parent_for(component, root, components, category)
        dims = dims_by_id[component_id]
        center = centers[component_id]
        parent_center = centers.get(parent_id or "", (0.0, 0.0, 0.0))
        body_pos = (
            center[0] - parent_center[0],
            center[1] - parent_center[1],
            center[2] - parent_center[2],
        )
        joint_type, axis, limits = _joint_spec(component, root["id"])
        # A hinge anchors at the child's top edge, so the visible part hangs
        # below the joint and rotates like a limb. Wheels spin about center.
        if joint_type == "free":
            joint_pos = (0.0, 0.0, 0.0)
            body_pos = center
        elif joint_type == "hinge" and component.get("component_type") == "wheel":
            joint_pos = (0.0, 0.0, 0.0)
        elif joint_type == "hinge":
            joint_pos = (0.0, 0.0, dims[2] / 2.0)
        else:
            joint_pos = (0.0, 0.0, 0.0)

        mass, inertia = _mass_and_inertia(dims, DEFAULT_FILL_FACTOR)
        actuator = None
        if joint_type in ("hinge", "slide"):
            actuator = {
                "name": f"{name}_motor",
                "actuator_type": "position",
                "configuration": actuator_defaults(mass, limits),
            }

        placements.append(
            Placement(
                component_id=component_id,
                name=name,
                label=str(component.get("label") or name),
                component_type=str(component.get("component_type") or "other"),
                parent_id=parent_id,
                body_pos=body_pos,
                joint_pos=joint_pos,
                dimensions=dims,
                mass=mass,
                joint_type=joint_type,
                axis=axis,
                limits=limits,
                actuator=actuator,
                metadata={
                    "placement": {
                        "body_pos": [round(value, 6) for value in body_pos],
                        "joint_pos": [round(value, 6) for value in joint_pos],
                        "world_center": [round(value, 6) for value in center],
                    },
                    "physics": {
                        "mass_kg": round(mass, 6),
                        "inertia": [round(value, 8) for value in inertia],
                        "friction": DEFAULT_FRICTION,
                        "estimated": True,
                        "note": (
                            "Mass/inertia are estimated from visible geometry"
                            " and an assumed density; they are not measured."
                        ),
                    },
                    "assembly": {
                        "inferred": True,
                        "note": (
                            "Hierarchy and joints are inferred from the visible"
                            " layout, not from the robot's real mechanism."
                        ),
                    },
                },
            )
        )
    return placements


def placements_to_rows(
    placements: list[Placement], *, existing: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert placements to component updates + joint/actuator insert rows."""
    by_id = {component["id"]: component for component in existing}
    joint_rows: list[dict[str, Any]] = []
    actuator_rows: list[dict[str, Any]] = []
    for placement in placements:
        component = by_id.get(placement.component_id)
        if component is None:
            continue
        component["parent_id"] = placement.parent_id
        metadata = dict(component.get("metadata") or {})
        metadata.update(placement.metadata)
        component["metadata"] = metadata
        if placement.joint_type == "free":
            continue
        joint_id = f"{placement.component_id}_joint"
        joint_rows.append(
            {
                "id": joint_id,
                "component_id": placement.component_id,
                "parent_component_id": placement.parent_id,
                "name": f"{placement.name}_joint",
                "joint_type": placement.joint_type,
                "axis": list(placement.axis),
                "position": list(placement.joint_pos),
                "limits": (
                    list(placement.limits)
                    if placement.limits is not None
                    else None
                ),
                "metadata": {
                    "estimated": True,
                    "body_pos": list(placement.body_pos),
                },
            }
        )
        if placement.actuator is not None:
            actuator_rows.append(
                {
                    "id": f"{placement.component_id}_actuator",
                    "joint_id": joint_id,
                    "name": placement.actuator["name"],
                    "actuator_type": placement.actuator["actuator_type"],
                    "configuration": placement.actuator["configuration"],
                }
            )
    return list(by_id.values()), joint_rows, actuator_rows


__all__ = [
    "Placement",
    "build_assembly",
    "placements_to_rows",
]
