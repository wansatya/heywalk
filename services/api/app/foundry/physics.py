"""Physics configuration stage.

Estimates mass, centre of mass, inertia, friction, and actuator defaults
from each component's reconstructed dimensions. Every value is marked
``estimated: true``; the UI must show that marker and never present these
numbers as measured properties. Manual values are preserved when a component
already carries a non-estimated physics block.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

DEFAULT_DENSITY_KG_M3 = 420.0
DEFAULT_FILL_FACTOR = 0.38
DEFAULT_FRICTION = 0.8
MIN_MASS_KG = 0.05


@dataclass(frozen=True)
class PhysicsEstimate:
    mass: float
    inertia: tuple[float, float, float]
    center_of_mass: tuple[float, float, float]
    friction: float
    estimated: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "mass_kg": round(self.mass, 6),
            "inertia": [round(value, 8) for value in self.inertia],
            "center_of_mass": [round(value, 6) for value in self.center_of_mass],
            "friction": self.friction,
            "estimated": self.estimated,
            "note": (
                "Estimated from visible geometry and an assumed density;"
                " not a measured physical property."
            ),
        }


def estimate_box_physics(
    dimensions: tuple[float, float, float],
    *,
    fill: float = DEFAULT_FILL_FACTOR,
    density: float = DEFAULT_DENSITY_KG_M3,
) -> PhysicsEstimate:
    dx, dy, dz = (max(0.001, float(value)) for value in dimensions)
    volume = dx * dy * dz * max(0.01, min(1.0, fill))
    mass = max(MIN_MASS_KG, volume * max(1.0, density))
    inertia = (
        mass / 12.0 * (dy * dy + dz * dz),
        mass / 12.0 * (dx * dx + dz * dz),
        mass / 12.0 * (dx * dx + dy * dy),
    )
    return PhysicsEstimate(
        mass=mass,
        inertia=tuple(max(1e-6, value) for value in inertia),  # type: ignore[arg-type]
        center_of_mass=(0.0, 0.0, 0.0),
        friction=DEFAULT_FRICTION,
    )


def actuator_defaults(mass: float, limits: tuple[float, float] | None) -> dict[str, Any]:
    ctrl = list(limits or (-1.0, 1.0))
    # The joint tests grade position tracking with a 0.05 rad tolerance. A
    # purely proportional servo holding an arm against gravity settles at
    # ``gravity_torque / kp``; ``mass * 20`` was just low enough for a light
    # arm to sit outside that tolerance. Scale the gain with the part's mass
    # so every estimated joint can hold its target, and leave the value
    # marked estimated so the user can review it.
    force = max(20.0, min(1000.0, float(mass) * 60.0))
    return {
        "kp": round(max(60.0, min(600.0, float(mass) * 40.0)), 3),
        "kv": 2.0,
        "ctrlrange": [round(ctrl[0], 6), round(ctrl[1], 6)],
        "forcerange": [-round(force, 3), round(force, 3)],
        "gear": 1.0,
        "estimated": True,
    }


def apply_physics(components: list[dict[str, Any]]) -> None:
    """Write estimated physics into component metadata in place."""
    for component in components:
        metadata = dict(component.get("metadata") or {})
        existing = metadata.get("physics")
        if isinstance(existing, dict) and existing.get("estimated") is False:
            continue
        raw = metadata.get("dimensions")
        if not (isinstance(raw, (list, tuple)) and len(raw) == 3):
            continue
        try:
            dimensions = tuple(float(value) for value in raw)
        except (TypeError, ValueError):
            continue
        estimate = estimate_box_physics(dimensions)  # type: ignore[arg-type]
        metadata["physics"] = estimate.to_dict()
        component["metadata"] = metadata


def is_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False

__all__ = [
    "PhysicsEstimate",
    "actuator_defaults",
    "apply_physics",
    "estimate_box_physics",
    "is_finite",
]
