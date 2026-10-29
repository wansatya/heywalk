"""Deterministic 2D differential-drive simulator (AGENTS.md section 7).

This package owns the simulation engine, world configuration, and objective
evaluator. The default demo world below is the reproducible scenario used
when a run does not supply its own configuration.
"""

from __future__ import annotations

from typing import Any

from .engine import (
    MIN_RANGE,
    Simulator,
    cast_ray,
    circle_hits_rect,
    compute_scan,
    integrate_diff_drive,
    normalize_angle,
    pose_in_collision,
)
from .evaluator import (
    EVALUATOR_VERSION,
    collision_times,
    compute_estop_latency,
    count_collision_events,
    evaluate,
    evaluate_from_telemetry,
    failure_report,
    goal_distance_of,
    path_length_of,
)
from .world import (
    Goal,
    Pose,
    Rect,
    WorldConfig,
    make_narrow_passage_obstacles,
)

DEFAULT_WORLD: dict[str, Any] = {
    "width": 10.0,
    "height": 8.0,
    "start": {"x": 1.0, "y": 1.0, "theta": 0.0},
    "goal": {"x": 8.5, "y": 6.5, "radius": 0.4},
    "obstacles": [
        {"x": 4.0, "y": 1.0, "w": 0.6, "h": 4.0},
        {"x": 6.0, "y": 4.0, "w": 0.6, "h": 3.0},
    ],
    "dt": 0.05,
    "max_linear_speed": 0.6,
    "max_angular_speed": 1.5,
    "robot_radius": 0.22,
    "time_limit": 60.0,
}

__all__ = [
    "DEFAULT_WORLD",
    "EVALUATOR_VERSION",
    "MIN_RANGE",
    "Goal",
    "Pose",
    "Rect",
    "Simulator",
    "WorldConfig",
    "cast_ray",
    "circle_hits_rect",
    "collision_times",
    "compute_estop_latency",
    "compute_scan",
    "count_collision_events",
    "evaluate",
    "evaluate_from_telemetry",
    "failure_report",
    "goal_distance_of",
    "integrate_diff_drive",
    "make_narrow_passage_obstacles",
    "normalize_angle",
    "path_length_of",
    "pose_in_collision",
]
