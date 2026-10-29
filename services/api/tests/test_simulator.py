"""Unit tests for the deterministic 2D simulator and evaluator.

Covers AGENTS.md section 14 simulator cases: motion integration, collision
detection, laser scans, goal detection, emergency-stop behavior, and metric
calculations, plus determinism, world configuration, and the narrow passage.
"""

from __future__ import annotations

import math

import pytest

from app.services import evaluation as service_evaluator
from app.simulator import (
    DEFAULT_WORLD,
    EVALUATOR_VERSION,
    Goal,
    Pose,
    Rect,
    Simulator,
    WorldConfig,
    circle_hits_rect,
    compute_estop_latency,
    count_collision_events,
    evaluate_from_telemetry,
    failure_report,
    make_narrow_passage_obstacles,
    normalize_angle,
    pose_in_collision,
)
from app.simulator import evaluate_from_telemetry as sim_evaluate_from_telemetry


@pytest.fixture()
def open_world() -> WorldConfig:
    return WorldConfig(
        width=10.0,
        height=10.0,
        start=Pose(1.0, 1.0, 0.0),
        goal=Goal(9.0, 9.0, 0.5),
        obstacles=(),
        dt=0.1,
        robot_radius=0.2,
        max_linear_speed=1.0,
        max_angular_speed=2.0,
        time_limit=30.0,
        num_rays=4,
    )


@pytest.fixture()
def goal_dict() -> dict:
    return {"x": 9.0, "y": 9.0, "radius": 0.5}


# ---- world configuration ----


def test_default_world_matches_orchestration_contract():
    assert DEFAULT_WORLD == {
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


def test_world_round_trip_from_default_dict():
    world = WorldConfig.from_dict(DEFAULT_WORLD)
    assert world.width == 10.0
    assert world.start == Pose(1.0, 1.0, 0.0)
    assert world.goal == Goal(8.5, 6.5, 0.4)
    assert len(world.obstacles) == 2
    assert world.dt == 0.05
    data = world.to_dict()
    for key in ("width", "height", "dt", "robot_radius", "time_limit"):
        assert data[key] == DEFAULT_WORLD[key]


def test_world_rejects_invalid_config():
    with pytest.raises(ValueError):
        WorldConfig(width=0.0)
    with pytest.raises(ValueError):
        WorldConfig(dt=-0.1)
    with pytest.raises(ValueError):
        WorldConfig(robot_radius=0.0)
    with pytest.raises(ValueError):
        WorldConfig(num_rays=0)
    with pytest.raises(ValueError):
        WorldConfig(obstacles=(Rect(1.0, 1.0, 0.0, 1.0),))


def test_narrow_passage_builder_leaves_gap():
    segments = make_narrow_passage_obstacles(10.0, 8.0, gap_width=1.2)
    assert len(segments) == 2
    lower, upper = segments
    assert lower.y == 0.0
    assert math.isclose(upper.y_max, 8.0)
    gap = upper.y - lower.y_max
    assert gap == pytest.approx(1.2)
    with pytest.raises(ValueError):
        make_narrow_passage_obstacles(10.0, 8.0, gap_width=20.0)


def test_narrow_passage_world_gap_passable_wall_blocked():
    world = WorldConfig(
        width=10.0,
        height=8.0,
        start=Pose(1.0, 4.0, 0.0),
        goal=Goal(9.0, 4.0, 0.5),
        dt=0.1,
        robot_radius=0.2,
    ).with_narrow_passage(gap_width=1.2)
    assert len(world.obstacles) == 2
    assert not pose_in_collision(5.0, 4.0, world)
    assert pose_in_collision(5.0, 2.0, world)

    through_gap = Simulator(world, seed=0)
    through_gap.run_commands([(1.0, 0.0)] * 80)
    assert not through_gap.collided

    into_wall = Simulator(
        WorldConfig(
            width=10.0,
            height=8.0,
            start=Pose(1.0, 2.0, 0.0),
            goal=Goal(9.0, 2.0, 0.5),
            obstacles=world.obstacles,
            dt=0.1,
            robot_radius=0.2,
        ),
        seed=0,
    )
    into_wall.run_commands([(1.0, 0.0)] * 80)
    assert into_wall.collided


def test_from_dict_narrow_passage_flag():
    data = dict(DEFAULT_WORLD)
    data["narrow_passage"] = True
    world = WorldConfig.from_dict(data)
    assert len(world.obstacles) == 2 + 2


# ---- motion integration ----


def test_straight_line_motion(open_world):
    sim = Simulator(open_world, seed=0)
    sim.run_commands([(0.5, 0.0)] * 20)
    assert sim.x == pytest.approx(2.0)
    assert sim.y == pytest.approx(1.0)
    assert sim.theta == pytest.approx(0.0)
    assert sim.v == pytest.approx(0.5)


def test_rotation_in_place(open_world):
    sim = Simulator(open_world, seed=0)
    sim.run_commands([(0.0, 1.0)] * 10)
    assert sim.x == pytest.approx(1.0)
    assert sim.y == pytest.approx(1.0)
    assert sim.theta == pytest.approx(1.0)


def test_arc_matches_analytic_solution(open_world):
    sim = Simulator(open_world, seed=0)
    sim.step(0.5, 0.5)
    assert sim.x == pytest.approx(1.0 + math.sin(0.05), abs=1e-12)
    assert sim.y == pytest.approx(1.0 + (1.0 - math.cos(0.05)), abs=1e-12)
    assert sim.theta == pytest.approx(0.05)


def test_velocity_limits_are_enforced(open_world):
    sim = Simulator(open_world, seed=0)
    sample = sim.step(50.0, 0.0)
    assert sample["v"] == pytest.approx(1.0)
    sample = sim.step(-50.0, 0.0)
    assert sample["v"] == pytest.approx(-1.0)
    sample = sim.step(0.0, 50.0)
    assert sample["omega"] == pytest.approx(2.0)
    sample = sim.step(0.0, -50.0)
    assert sample["omega"] == pytest.approx(-2.0)


def test_heading_stays_normalized(open_world):
    sim = Simulator(open_world, seed=0)
    sim.run_commands([(0.0, 2.0)] * 100)
    assert -math.pi <= sim.theta <= math.pi
    assert normalize_angle(3.0 * math.pi) == pytest.approx(math.pi)
    assert normalize_angle(0.0) == pytest.approx(0.0)


def test_fixed_timestep_and_timestamped_trace(open_world):
    sim = Simulator(open_world, seed=0)
    sim.run_commands([(0.5, 0.0)] * 7)
    trace = sim.trace
    assert len(trace) == 8
    assert trace[0]["t"] == pytest.approx(0.0)
    assert [s["t"] for s in trace] == pytest.approx([i * 0.1 for i in range(8)])
    assert sim.elapsed == pytest.approx(0.7)


def test_sample_matches_telemetry_contract(open_world):
    sim = Simulator(open_world, seed=0)
    sample = sim.step(0.5, 0.1)
    for key in ("t", "x", "y", "theta", "v", "omega", "collision",
                "goal_reached", "estop_active", "min_range"):
        assert key in sample


def test_odom_reports_current_state(open_world):
    sim = Simulator(open_world, seed=0)
    sim.step(0.5, 0.0)
    odom = sim.odom()
    assert odom["x"] == pytest.approx(sim.x)
    assert odom["theta"] == pytest.approx(sim.theta)
    assert odom["v"] == pytest.approx(0.5)


def test_invalid_commands_coerced_and_counted(open_world):
    sim = Simulator(open_world, seed=0)
    sample = sim.step(float("nan"), 1.0)
    assert sample["v"] == pytest.approx(0.0)
    assert sample["omega"] == pytest.approx(1.0)
    assert sample["commands_valid"] is False
    assert sim.invalid_command_count == 1


# ---- determinism ----


def _drive_script(sim: Simulator) -> list[dict]:
    return sim.run_commands([(0.6, 0.2)] * 15 + [(0.0, -1.0)] * 10 + [(0.4, 0.0)] * 15)


def test_same_seed_same_trace(open_world):
    first = _drive_script(Simulator(open_world, seed=123))
    second = _drive_script(Simulator(open_world, seed=123))
    assert first == second


def test_scan_noise_is_seeded(open_world):
    noisy = WorldConfig(
        width=open_world.width, height=open_world.height,
        start=open_world.start, goal=open_world.goal,
        dt=open_world.dt, robot_radius=open_world.robot_radius,
        max_linear_speed=1.0, max_angular_speed=2.0,
        scan_noise_std=0.5, num_rays=8,
    )
    first = _drive_script(Simulator(noisy, seed=7))
    second = _drive_script(Simulator(noisy, seed=7))
    third = _drive_script(Simulator(noisy, seed=8))
    assert first == second
    assert [s["min_range"] for s in first] != [s["min_range"] for s in third]


def test_reset_reproduces_initial_state(open_world):
    sim = Simulator(open_world, seed=3)
    sim.run_commands([(1.0, 0.5)] * 10)
    initial = sim.reset()
    assert initial["t"] == pytest.approx(0.0)
    assert (sim.x, sim.y) == pytest.approx((1.0, 1.0))
    assert len(sim.trace) == 1


# ---- collision detection ----


def test_circle_rect_intersection():
    rect = Rect(3.0, 3.0, 2.0, 2.0)
    assert circle_hits_rect(4.0, 4.0, 0.2, rect)
    assert circle_hits_rect(2.9, 4.0, 0.2, rect)
    assert circle_hits_rect(2.75, 4.0, 0.25, rect)  # contact counts
    assert not circle_hits_rect(2.5, 4.0, 0.2, rect)
    assert not circle_hits_rect(0.0, 0.0, 0.5, rect)


def test_wall_collision_blocks_motion(open_world):
    world = WorldConfig(
        width=10.0, height=10.0, start=Pose(1.0, 1.0, 0.0),
        goal=Goal(9.0, 9.0, 0.5), dt=0.25, robot_radius=0.2,
        max_linear_speed=1.0, max_angular_speed=2.0,
    )
    sim = Simulator(world, seed=0)
    last = None
    for _ in range(60):
        last = sim.step(1.0, 0.0)
    assert sim.collided
    assert sim.x == pytest.approx(9.75)
    assert last is not None and last["collision"] is True
    assert last["v"] == pytest.approx(0.0)


def test_obstacle_collision_blocks_motion():
    world = WorldConfig(
        width=10.0, height=10.0, start=Pose(1.0, 1.0, 0.0),
        goal=Goal(9.0, 9.0, 0.5), obstacles=(Rect(3.0, 0.5, 0.5, 1.0),),
        dt=0.1, robot_radius=0.2, max_linear_speed=1.0, max_angular_speed=2.0,
    )
    sim = Simulator(world, seed=0)
    sim.run_commands([(1.0, 0.0)] * 40)
    assert sim.collided
    assert sim.x == pytest.approx(2.7)
    assert sim.trace[-1]["collision"] is True


def test_free_pose_has_no_collision(open_world):
    assert not pose_in_collision(5.0, 5.0, open_world)
    sim = Simulator(open_world, seed=0)
    sim.run_commands([(0.5, 0.0)] * 5)
    assert not sim.collided


# ---- laser scans ----


def test_scan_in_open_arena_matches_walls():
    world = WorldConfig(
        width=10.0, height=10.0, start=Pose(5.0, 5.0, 0.0),
        goal=Goal(9.0, 9.0, 0.5), num_rays=4,
    )
    sim = Simulator(world, seed=0)
    assert sim.scan() == pytest.approx([5.0, 5.0, 5.0, 5.0])


def test_scan_detects_obstacle():
    world = WorldConfig(
        width=10.0, height=10.0, start=Pose(2.0, 5.0, 0.0),
        goal=Goal(9.0, 9.0, 0.5), obstacles=(Rect(4.0, 4.0, 1.0, 2.0),),
        num_rays=1,
    )
    sim = Simulator(world, seed=0)
    assert sim.scan() == pytest.approx([2.0])
    sample = sim.step(0.0, 0.0, include_ranges=True)
    assert sample["ranges"] == pytest.approx([2.0])
    assert sample["min_range"] == pytest.approx(2.0)


def test_scan_clamped_to_max_range():
    world = WorldConfig(
        width=10.0, height=10.0, start=Pose(5.0, 5.0, 0.0),
        goal=Goal(9.0, 9.0, 0.5), num_rays=8, scan_max_range=3.0,
    )
    sim = Simulator(world, seed=0)
    assert all(r <= 3.0 for r in sim.scan())


# ---- goal detection ----


def test_goal_reached_within_tolerance():
    world = WorldConfig(
        width=10.0, height=10.0, start=Pose(1.0, 4.0, 0.0),
        goal=Goal(3.0, 4.0, 0.4), dt=0.1, robot_radius=0.2,
        max_linear_speed=1.0, max_angular_speed=2.0,
    )
    sim = Simulator(world, seed=0)
    assert not sim.goal_reached
    samples = sim.run_commands([(1.0, 0.0)] * 20)
    assert sim.goal_reached
    assert samples[-1]["goal_reached"] is True


def test_goal_boundary_and_miss():
    world = WorldConfig(
        width=10.0, height=10.0, start=Pose(0.6, 4.0, 0.0),
        goal=Goal(1.0, 4.0, 0.4), dt=0.1, robot_radius=0.2,
    )
    sim = Simulator(world, seed=0)
    sim.reset()
    assert sim.goal_reached  # exactly on the tolerance boundary
    far = WorldConfig(
        width=10.0, height=10.0, start=Pose(1.0, 1.0, 0.0),
        goal=Goal(9.0, 9.0, 0.4), dt=0.1, robot_radius=0.2,
    )
    sim_far = Simulator(far, seed=0)
    sim_far.run_commands([(0.5, 0.0)] * 5)
    assert not sim_far.goal_reached


# ---- emergency stop ----


def test_estop_halts_motion_and_resumes(open_world):
    sim = Simulator(open_world, seed=0)
    sim.run_commands([(1.0, 0.0)] * 5)
    stopped = sim.step(1.0, 0.5, estop_active=True)
    assert stopped["v"] == pytest.approx(0.0)
    assert stopped["omega"] == pytest.approx(0.0)
    assert stopped["estop_active"] is True
    pose = (sim.x, sim.y, sim.theta)
    sim.step(1.0, 0.5, estop_active=True)
    assert (sim.x, sim.y, sim.theta) == pytest.approx(pose)
    released = sim.step(1.0, 0.0, estop_active=False)
    assert released["v"] == pytest.approx(1.0)


def test_run_commands_estop_schedule(open_world):
    sim = Simulator(open_world, seed=0)
    samples = sim.run_commands([(1.0, 0.0)] * 6, estop_at=3)
    assert [s["estop_active"] for s in samples] == [
        False, False, False, True, True, True,
    ]
    assert samples[-1]["v"] == pytest.approx(0.0)


# ---- evaluator ----


def _straight_to_goal_trace() -> tuple[list[dict], dict]:
    world = WorldConfig(
        width=10.0, height=10.0, start=Pose(1.0, 4.0, 0.0),
        goal=Goal(9.0, 4.0, 0.5), dt=0.1, robot_radius=0.2,
        max_linear_speed=1.0, max_angular_speed=2.0, time_limit=30.0,
    )
    sim = Simulator(world, seed=0)
    sim.run_commands([(0.5, 0.0)] * 160)
    goal = {"x": 9.0, "y": 4.0, "radius": 0.5}
    return sim.trace, goal


def test_evaluator_success_and_metrics():
    trace, goal = _straight_to_goal_trace()
    report, metrics = evaluate_from_telemetry(trace, goal=goal, time_limit=30.0)
    assert report["success"] is True
    assert report["run_status"] == "success"
    assert report["evaluator_version"] == EVALUATOR_VERSION
    assert metrics["success"] is True
    assert metrics["collision_count"] == 0
    assert metrics["goal_distance"] == pytest.approx(0.0)
    assert metrics["completion_time"] == pytest.approx(16.0)
    assert metrics["path_length"] == pytest.approx(8.0)
    assert metrics["emergency_stop_latency"] is None
    assert metrics["evaluator_version"] == EVALUATOR_VERSION


def test_evaluator_counts_collision_events_with_timestamp(goal_dict):
    samples = [
        {"t": 0.0, "x": 1.0, "y": 1.0, "v": 0.5, "omega": 0.0,
         "collision": False, "goal_reached": False},
        {"t": 0.1, "x": 1.1, "y": 1.0, "v": 0.0, "omega": 0.0,
         "collision": True, "goal_reached": False},
        {"t": 0.2, "x": 1.1, "y": 1.0, "v": 0.0, "omega": 0.0,
         "collision": True, "goal_reached": False},
        {"t": 0.3, "x": 1.2, "y": 1.0, "v": 0.5, "omega": 0.0,
         "collision": False, "goal_reached": False},
        {"t": 0.4, "x": 1.2, "y": 1.0, "v": 0.0, "omega": 0.0,
         "collision": True, "goal_reached": False},
    ]
    assert count_collision_events(samples) == 2
    report, metrics = evaluate_from_telemetry(samples, goal=goal_dict)
    assert report["success"] is False
    assert report["run_status"] == "failed"
    assert metrics["collision_count"] == 2
    assert any("t=0.10s" in reason for reason in report["reasons"])


def test_evaluator_timeout_reason(goal_dict):
    samples = [
        {"t": 0.0, "x": 1.0, "y": 1.0, "v": 0.5, "omega": 0.0,
         "collision": False, "goal_reached": False},
        {"t": 5.0, "x": 2.0, "y": 1.0, "v": 0.5, "omega": 0.0,
         "collision": False, "goal_reached": False},
    ]
    report, _ = evaluate_from_telemetry(samples, goal=goal_dict, time_limit=5.0)
    assert report["run_status"] == "timeout"
    assert any("before timeout" in reason for reason in report["reasons"])


def test_evaluator_goal_distance_and_path_length(goal_dict):
    samples = [
        {"t": 0.0, "x": 0.0, "y": 0.0, "v": 0.0, "omega": 0.0,
         "collision": False, "goal_reached": False},
        {"t": 1.0, "x": 3.0, "y": 4.0, "v": 0.0, "omega": 0.0,
         "collision": False, "goal_reached": False},
    ]
    _, metrics = evaluate_from_telemetry(samples, goal={"x": 6.0, "y": 8.0, "radius": 1.0})
    assert metrics["path_length"] == pytest.approx(5.0)
    assert metrics["goal_distance"] == pytest.approx(4.0)
    assert metrics["completion_time"] == pytest.approx(1.0)


def test_evaluator_empty_telemetry_and_missing_goal(goal_dict):
    report, metrics = evaluate_from_telemetry([], goal=goal_dict)
    assert report["success"] is False
    assert metrics["completion_time"] == pytest.approx(0.0)
    assert math.isinf(metrics["goal_distance"])
    samples = [{"t": 0.0, "x": 1.0, "y": 1.0, "v": 0.0, "omega": 0.0,
                "collision": False, "goal_reached": False}]
    report, _ = evaluate_from_telemetry(samples, goal=None)
    assert report["success"] is False
    assert any("No goal" in reason for reason in report["reasons"])


def test_evaluator_exit_code_error(goal_dict):
    trace, _ = _straight_to_goal_trace()
    report, _ = evaluate_from_telemetry(trace, goal=goal_dict, exit_code=2)
    assert report["success"] is False
    assert report["run_status"] == "error"
    assert any("code 2" in reason for reason in report["reasons"])


def test_evaluator_never_moved_and_invalid_commands(goal_dict):
    still = [
        {"t": 0.0, "x": 1.0, "y": 1.0, "v": 0.0, "omega": 0.0,
         "collision": False, "goal_reached": False},
        {"t": 0.1, "x": 1.0, "y": 1.0, "v": 0.0, "omega": 0.0,
         "collision": False, "goal_reached": False, "commands_valid": False},
    ]
    report, _ = evaluate_from_telemetry(still, goal=goal_dict)
    assert report["success"] is False
    assert any("never moved" in reason for reason in report["reasons"])
    assert any("non-finite" in reason for reason in report["reasons"])


def test_estop_latency_measured_from_trace():
    samples = [
        {"t": 0.0, "x": 1.0, "y": 1.0, "v": 0.5, "omega": 0.0,
         "collision": False, "goal_reached": False, "estop_active": False},
        {"t": 0.1, "x": 1.1, "y": 1.0, "v": 0.5, "omega": 0.0,
         "collision": False, "goal_reached": False, "estop_active": True},
        {"t": 0.2, "x": 1.2, "y": 1.0, "v": 0.0, "omega": 0.0,
         "collision": False, "goal_reached": False, "estop_active": True},
    ]
    assert compute_estop_latency(samples) == pytest.approx(0.1)

    never_stopped = [
        dict(s, v=0.5, estop_active=True) for s in samples
    ]
    assert compute_estop_latency(never_stopped) is None
    assert compute_estop_latency([samples[0]]) is None


def test_estop_latency_zero_when_sim_halts_immediately(open_world):
    sim = Simulator(open_world, seed=0)
    sim.run_commands([(1.0, 0.0)] * 3)
    sim.step(1.0, 0.0, estop_active=True)
    sim.step(1.0, 0.0, estop_active=True)
    assert compute_estop_latency(sim.trace) == pytest.approx(0.0)


def test_estop_requirement_gating(goal_dict):
    trace, goal = _straight_to_goal_trace()
    report, _ = evaluate_from_telemetry(trace, goal=goal, estop_test_required=True)
    assert report["success"] is False
    assert any("not demonstrated" in reason for reason in report["reasons"])

    slow_stop = [
        {"t": 0.0, "x": 1.0, "y": 1.0, "v": 0.5, "omega": 0.0,
         "collision": False, "goal_reached": False, "estop_active": True},
        {"t": 2.0, "x": 2.0, "y": 1.0, "v": 0.0, "omega": 0.0,
         "collision": False, "goal_reached": True, "estop_active": True},
    ]
    report, metrics = evaluate_from_telemetry(
        slow_stop, goal=goal_dict, estop_test_required=True, estop_latency_limit=1.0
    )
    assert report["success"] is False
    assert metrics["emergency_stop_latency"] == pytest.approx(2.0)
    assert any("did not halt motion within" in reason for reason in report["reasons"])


def test_failure_report_shapes():
    report = failure_report(["boom"])
    assert report == {
        "success": False,
        "reasons": ["boom"],
        "evaluator_version": EVALUATOR_VERSION,
        "run_status": "error",
    }
    assert failure_report([], run_status="cancelled")["run_status"] == "cancelled"
    assert failure_report([])["reasons"] == ["Run did not complete."]


def test_service_layer_delegates_to_simulator_evaluator():
    assert service_evaluator.EVALUATOR_VERSION == EVALUATOR_VERSION
    assert service_evaluator.evaluate_from_telemetry is sim_evaluate_from_telemetry


# ---- unlimited sim time ----


def test_world_time_limit_none_roundtrip():
    data = dict(DEFAULT_WORLD)
    data["time_limit"] = None
    world = WorldConfig.from_dict(data)
    assert world.time_limit is None
    assert world.to_dict()["time_limit"] is None


def test_world_rejects_nonpositive_time_limit_but_accepts_none():
    WorldConfig(time_limit=None)
    with pytest.raises(ValueError):
        WorldConfig(time_limit=0.0)
    with pytest.raises(ValueError):
        WorldConfig(time_limit=-5.0)


def test_evaluate_late_goal_without_limit_succeeds(goal_dict):
    trace = [
        {"t": 0.0, "x": 1.0, "y": 1.0, "v": 0.1, "omega": 0.0,
         "collision": False, "goal_reached": False},
        {"t": 75.25, "x": 9.0, "y": 9.0, "v": 0.1, "omega": 0.0,
         "collision": False, "goal_reached": True},
    ]
    report, metrics = evaluate_from_telemetry(
        trace, goal=goal_dict, time_limit=None, exit_code=0
    )
    assert report["success"] is True
    assert report["run_status"] == "success"
    assert metrics["completion_time"] == 75.25
