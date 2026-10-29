"""World configuration for the deterministic 2D simulator (AGENTS.md section 7).

A world is a bounded rectangular arena with a start pose, a circular goal
region, and static axis-aligned-rectangle obstacles, plus an optional narrow
passage for more challenging scenarios.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Pose:
    x: float
    y: float
    theta: float = 0.0


@dataclass(frozen=True)
class Rect:
    """Axis-aligned rectangle; (x, y) is the minimum corner."""

    x: float
    y: float
    w: float
    h: float

    @property
    def x_max(self) -> float:
        return self.x + self.w

    @property
    def y_max(self) -> float:
        return self.y + self.h


@dataclass(frozen=True)
class Goal:
    x: float
    y: float
    radius: float = 0.4


@dataclass(frozen=True)
class WorldConfig:
    width: float = 10.0
    height: float = 8.0
    start: Pose = field(default_factory=lambda: Pose(1.0, 1.0, 0.0))
    goal: Goal = field(default_factory=lambda: Goal(8.5, 6.5, 0.4))
    obstacles: tuple[Rect, ...] = ()
    dt: float = 0.05
    robot_radius: float = 0.22
    max_linear_speed: float = 0.6
    max_angular_speed: float = 1.5
    # Sim-time budget in seconds, or None for unlimited: the run then ends on
    # goal/collision/e-stop/cancel (the runner's wall-clock bound still applies).
    time_limit: float | None = 60.0
    num_rays: int = 36
    scan_fov: float = 2.0 * math.pi
    scan_max_range: float = 12.0
    scan_noise_std: float = 0.0

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Arena width and height must be positive.")
        if self.dt <= 0:
            raise ValueError("Simulation timestep dt must be positive.")
        if self.robot_radius <= 0:
            raise ValueError("Robot radius must be positive.")
        if self.max_linear_speed < 0 or self.max_angular_speed < 0:
            raise ValueError("Speed limits must be non-negative.")
        if self.time_limit is not None and self.time_limit <= 0:
            raise ValueError("Time limit must be positive or None (unlimited).")
        if self.num_rays < 1:
            raise ValueError("num_rays must be at least 1.")
        if self.scan_fov <= 0 or self.scan_fov > 2.0 * math.pi:
            raise ValueError("scan_fov must be in (0, 2*pi].")
        if self.scan_max_range <= 0:
            raise ValueError("scan_max_range must be positive.")
        if self.scan_noise_std < 0:
            raise ValueError("scan_noise_std must be non-negative.")
        if self.goal.radius < 0:
            raise ValueError("Goal radius must be non-negative.")
        for obstacle in self.obstacles:
            if obstacle.w <= 0 or obstacle.h <= 0:
                raise ValueError("Obstacle width and height must be positive.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "start": {"x": self.start.x, "y": self.start.y, "theta": self.start.theta},
            "goal": {"x": self.goal.x, "y": self.goal.y, "radius": self.goal.radius},
            "obstacles": [
                {"x": o.x, "y": o.y, "w": o.w, "h": o.h} for o in self.obstacles
            ],
            "dt": self.dt,
            "robot_radius": self.robot_radius,
            "max_linear_speed": self.max_linear_speed,
            "max_angular_speed": self.max_angular_speed,
            "time_limit": self.time_limit,
            "num_rays": self.num_rays,
            "scan_fov": self.scan_fov,
            "scan_max_range": self.scan_max_range,
            "scan_noise_std": self.scan_noise_std,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorldConfig:
        start = data.get("start", {})
        goal = data.get("goal", {})
        obstacles = tuple(
            Rect(float(o["x"]), float(o["y"]), float(o["w"]), float(o["h"]))
            for o in data.get("obstacles", [])
        )
        if data.get("narrow_passage"):
            obstacles = obstacles + tuple(
                make_narrow_passage_obstacles(
                    float(data.get("width", 10.0)),
                    float(data.get("height", 8.0)),
                )
            )
        raw_limit = data.get("time_limit", 60.0)
        return cls(
            width=float(data.get("width", 10.0)),
            height=float(data.get("height", 8.0)),
            start=Pose(
                float(start.get("x", 1.0)),
                float(start.get("y", 1.0)),
                float(start.get("theta", 0.0)),
            ),
            goal=Goal(
                float(goal.get("x", 8.5)),
                float(goal.get("y", 6.5)),
                float(goal.get("radius", 0.4)),
            ),
            obstacles=obstacles,
            dt=float(data.get("dt", 0.05)),
            robot_radius=float(data.get("robot_radius", 0.22)),
            max_linear_speed=float(data.get("max_linear_speed", 0.6)),
            max_angular_speed=float(data.get("max_angular_speed", 1.5)),
            time_limit=None if raw_limit is None else float(raw_limit),
            num_rays=int(data.get("num_rays", 36)),
            scan_fov=float(data.get("scan_fov", 2.0 * math.pi)),
            scan_max_range=float(data.get("scan_max_range", 12.0)),
            scan_noise_std=float(data.get("scan_noise_std", 0.0)),
        )

    def with_narrow_passage(
        self,
        *,
        gap_center: float | None = None,
        gap_width: float = 1.2,
        wall_thickness: float = 0.3,
    ) -> WorldConfig:
        """Return a copy with a narrow-passage wall added at mid-arena."""
        passage = make_narrow_passage_obstacles(
            self.width,
            self.height,
            gap_center=gap_center,
            gap_width=gap_width,
            wall_thickness=wall_thickness,
        )
        return WorldConfig(
            width=self.width,
            height=self.height,
            start=self.start,
            goal=self.goal,
            obstacles=self.obstacles + tuple(passage),
            dt=self.dt,
            robot_radius=self.robot_radius,
            max_linear_speed=self.max_linear_speed,
            max_angular_speed=self.max_angular_speed,
            time_limit=self.time_limit,
            num_rays=self.num_rays,
            scan_fov=self.scan_fov,
            scan_max_range=self.scan_max_range,
            scan_noise_std=self.scan_noise_std,
        )


def make_narrow_passage_obstacles(
    width: float,
    height: float,
    *,
    gap_center: float | None = None,
    gap_width: float = 1.2,
    wall_thickness: float = 0.3,
) -> list[Rect]:
    """Build a vertical wall with a single gap (narrow passage).

    The wall spans the full arena height except for a gap of `gap_width`
    centered at `gap_center` (default: mid-height).
    """
    if gap_width <= 0:
        raise ValueError("gap_width must be positive.")
    if wall_thickness <= 0:
        raise ValueError("wall_thickness must be positive.")
    center = height / 2.0 if gap_center is None else gap_center
    gap_lo = center - gap_width / 2.0
    gap_hi = center + gap_width / 2.0
    if gap_lo < 0 or gap_hi > height:
        raise ValueError("Passage gap must lie inside the arena.")
    wall_x = width / 2.0 - wall_thickness / 2.0
    segments: list[Rect] = []
    if gap_lo > 0:
        segments.append(Rect(wall_x, 0.0, wall_thickness, gap_lo))
    if gap_hi < height:
        segments.append(Rect(wall_x, gap_hi, wall_thickness, height - gap_hi))
    return segments
