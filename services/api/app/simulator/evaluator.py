"""Objective, model-independent run evaluation (AGENTS.md section 8).

The evaluator derives success and metrics from simulator state and telemetry
only. A run counts as successful solely when the evaluator has evidence for
it -- never because the model claims completion.
"""

from __future__ import annotations

import math
from typing import Any

EVALUATOR_VERSION = "heywalk-eval-0.2"

STOP_LINEAR_THRESHOLD = 0.02
STOP_ANGULAR_THRESHOLD = 0.05


def _dist(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


def _is_stopped(sample: dict[str, Any]) -> bool:
    return (
        abs(float(sample.get("v", 0.0))) < STOP_LINEAR_THRESHOLD
        and abs(float(sample.get("omega", 0.0))) < STOP_ANGULAR_THRESHOLD
    )


def count_collision_events(samples: list[dict[str, Any]]) -> int:
    """Count distinct collisions (rising edges of the collision flag)."""
    count = 0
    previously_colliding = False
    for sample in samples:
        colliding = bool(sample.get("collision"))
        if colliding and not previously_colliding:
            count += 1
        previously_colliding = colliding
    return count


def collision_times(samples: list[dict[str, Any]]) -> list[float]:
    times: list[float] = []
    previously_colliding = False
    for sample in samples:
        colliding = bool(sample.get("collision"))
        if colliding and not previously_colliding:
            times.append(float(sample.get("t", 0.0)))
        previously_colliding = colliding
    return times


def compute_estop_latency(samples: list[dict[str, Any]]) -> float | None:
    """Simulated delay from e-stop activation to motion cessation.

    Returns None when no e-stop was demonstrated or when the robot never
    stopped moving after the stop was activated.
    """
    estop_start: float | None = None
    for sample in samples:
        if sample.get("estop_active"):
            estop_start = float(sample.get("t", 0.0))
            break
    if estop_start is None:
        return None
    for sample in samples:
        if float(sample.get("t", 0.0)) >= estop_start and _is_stopped(sample):
            return max(0.0, float(sample.get("t", 0.0)) - estop_start)
    return None


def goal_distance_of(samples: list[dict[str, Any]], goal: dict[str, Any] | None) -> float:
    if goal is None or not samples:
        return math.inf
    last = samples[-1]
    center = _dist(float(last["x"]), float(last["y"]), float(goal["x"]), float(goal["y"]))
    return max(0.0, center - float(goal.get("radius", 0.0)))


def path_length_of(samples: list[dict[str, Any]]) -> float:
    length = 0.0
    for prev, cur in zip(samples, samples[1:]):
        length += _dist(
            float(prev["x"]), float(prev["y"]), float(cur["x"]), float(cur["y"])
        )
    return length


def invalid_command_count(samples: list[dict[str, Any]]) -> int:
    return sum(1 for s in samples if s.get("commands_valid") is False)


def evaluate(
    samples: list[dict[str, Any]],
    *,
    goal: dict[str, Any] | None,
    time_limit: float | None = None,
    estop_test_required: bool = False,
    estop_latency: float | None = None,
    estop_latency_limit: float = 1.0,
    exit_code: int | None = None,
    failure_summary: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compute (evaluation_report, metrics) from recorded telemetry.

    Success requires: goal reached, no collision, clean exit, termination
    within the time limit, and (when the scenario requires it) the
    emergency-stop latency within its limit.
    """
    reasons: list[str] = []
    collision_count = count_collision_events(samples)
    first_collision = collision_times(samples)[:1]
    goal_reached = any(bool(s.get("goal_reached")) for s in samples)
    completion_time = float(samples[-1]["t"]) if samples else 0.0
    goal_distance = goal_distance_of(samples, goal)
    path_length = path_length_of(samples)
    invalid_commands = invalid_command_count(samples)

    if estop_latency is None:
        estop_latency = compute_estop_latency(samples)
    estop_demonstrated = any(bool(s.get("estop_active")) for s in samples)

    timed_out = (
        time_limit is not None and completion_time >= time_limit and not goal_reached
    )

    if not samples:
        reasons.append("No telemetry was recorded for this run.")
        goal_reached = False
    if goal is None:
        reasons.append("No goal was configured for this run.")
        goal_reached = False
    if collision_count:
        when = f" at t={first_collision[0]:.2f}s" if first_collision else ""
        reasons.append(
            f"Collision detected{when} ({collision_count} event(s))."
        )
    if samples and goal is not None and not goal_reached:
        if timed_out:
            assert time_limit is not None
            reasons.append(
                "Goal not reached before timeout"
                f" ({time_limit:.2f}s limit, final distance"
                f" {goal_distance:.2f}m)."
            )
        else:
            reasons.append("Goal not reached before the run ended.")
    if invalid_commands and not goal_reached:
        reasons.append(
            "Controller published invalid (non-finite) velocity commands"
            f" ({invalid_commands} sample(s))."
        )
    if samples and goal is not None and not goal_reached and not any(
        abs(float(s.get("v", 0.0))) > 0.0 or abs(float(s.get("omega", 0.0))) > 0.0
        for s in samples
    ):
        reasons.append(
            "Controller failed to publish valid motion commands"
            " (robot never moved)."
        )
    if exit_code not in (None, 0):
        reasons.append(f"Controller process exited with code {exit_code}.")
    if estop_test_required:
        if not estop_demonstrated and estop_latency is None:
            reasons.append("Emergency-stop behavior was not demonstrated.")
        elif estop_latency is None:
            reasons.append(
                "Emergency stop did not halt motion within"
                f" {estop_latency_limit:.2f}s (robot still moving when the run ended)."
            )
        elif estop_latency > estop_latency_limit:
            reasons.append(
                "Emergency stop did not halt motion within"
                f" {estop_latency_limit:.2f}s (observed {estop_latency:.2f}s)."
            )
    if failure_summary and not reasons:
        reasons.append(failure_summary)

    success = (
        bool(samples)
        and goal is not None
        and goal_reached
        and collision_count == 0
        and exit_code in (None, 0)
        and not timed_out
        and (
            not estop_test_required
            or (estop_latency is not None and estop_latency <= estop_latency_limit)
        )
    )
    if success:
        reasons = ["Goal reached with no disqualifying safety violation."]

    if success:
        run_status = "success"
    elif exit_code not in (None, 0):
        run_status = "error"
    elif timed_out:
        run_status = "timeout"
    else:
        run_status = "failed"

    report = {
        "success": success,
        "reasons": reasons,
        "evaluator_version": EVALUATOR_VERSION,
        "run_status": run_status,
    }
    metrics = {
        "success": success,
        "collision_count": collision_count,
        "goal_distance": goal_distance,
        "completion_time": completion_time,
        "path_length": path_length,
        "emergency_stop_latency": estop_latency,
        "evaluator_version": EVALUATOR_VERSION,
    }
    return report, metrics


def evaluate_from_telemetry(
    samples: list[dict[str, Any]],
    *,
    goal: dict[str, Any] | None,
    time_limit: float | None = None,
    estop_test_required: bool = False,
    estop_latency: float | None = None,
    estop_latency_limit: float = 1.0,
    exit_code: int | None = None,
    failure_summary: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compatibility entry point used by the run orchestration layer."""
    return evaluate(
        samples,
        goal=goal,
        time_limit=time_limit,
        estop_test_required=estop_test_required,
        estop_latency=estop_latency,
        estop_latency_limit=estop_latency_limit,
        exit_code=exit_code,
        failure_summary=failure_summary,
    )


def failure_report(
    reasons: list[str], *, run_status: str = "error"
) -> dict[str, Any]:
    """Build an explicit evaluation report for runs that never ran cleanly."""
    return {
        "success": False,
        "reasons": reasons or ["Run did not complete."],
        "evaluator_version": EVALUATOR_VERSION,
        "run_status": run_status,
    }
