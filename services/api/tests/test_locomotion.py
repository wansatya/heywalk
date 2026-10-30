"""Locomotion runs: walk to the flag with the upstream policy.

Covers the scenario fields, the Twist steering contract, the
LocomotionRunner evidence gathering, the evaluator verdicts, and one
end-to-end harness run with a Twist-publishing controller.
"""

import json
from pathlib import Path

import pytest

mujoco = pytest.importorskip("mujoco")

from app.runner import topics
from app.runner.mujoco_loop import MujocoLoop
from app.simulator.locomotion import (
    KICK_MIN_DISPLACEMENT,
    LOCOMOTION_TEST_ID,
    LocomotionRunner,
)
from app.simulator.microduck.model import (
    EXPECTED_GROUND_GEOMS,
    FALL_TRUNK_Z,
    FLAG_BODY,
    OFFICIAL_CONTROL_HZ,
    OFFICIAL_KEYFRAME,
    OFFICIAL_TIMESTEP,
    ball_geom_names,
    compose_scene_xml,
    flag_geom_names,
    stage_assets,
)
from app.simulator.mujoco_engine import MujocoEngine, MujocoEngineError
from app.simulator.mujoco_tests import evaluate_mujoco
from app.simulator.robot_description import parse_mjcf
from app.simulator.scenario import (
    MujocoScenario,
    ScenarioError,
    parse_objectives,
)

WALK_CONTROLLER = '''\
"""Test controller: steer toward the flag at 0.2 m/s."""

from geometry_msgs.msg import Twist
from rclpy.node import Node


class WalkNode(Node):
    def __init__(self):
        super().__init__("walk")
        self._pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self.create_timer(0.1, self._tick)

    def _tick(self):
        command = Twist()
        command.linear.x = 0.2
        command.angular.z = 0.0
        self._pub.publish(command)
'''

PHASE_WALK_CONTROLLER = '''\
"""Test controller: walk forward and log every /objective_state phase."""

from geometry_msgs.msg import Twist
from rclpy.node import Node
from std_msgs.msg import String


class PhaseWalkNode(Node):
    def __init__(self):
        super().__init__("phase_walk")
        self._phase = None
        self._pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self.create_subscription(String, "/objective_state", self._on_phase, 10)
        self.create_timer(0.1, self._tick)

    def _on_phase(self, message):
        if message.data == self._phase:
            return
        self._phase = message.data
        self.get_logger().info(f"objective phase: {message.data}")

    def _tick(self):
        command = Twist()
        command.linear.x = 0.2
        command.angular.z = 0.0
        self._pub.publish(command)
'''


def _world(**overrides):
    world = {
        "backend": "mujoco",
        "dt": OFFICIAL_TIMESTEP,
        "control_hz": OFFICIAL_CONTROL_HZ,
        "keyframe": OFFICIAL_KEYFRAME,
        "integrator": "Euler",
        "expected_contact_geoms": list(EXPECTED_GROUND_GEOMS),
        "fall_trunk_z": FALL_TRUNK_Z,
        "include_body_poses": False,
        "fix_base": False,
        "locomotion": True,
        "locomotion_duration_s": 12.0,
        "goal_geoms": list(flag_geom_names()),
    }
    world.update(overrides)
    return world


def _workspace(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "robot.xml").write_text(compose_scene_xml())
    stage_assets(path)
    return path


def _loop(tmp_path: Path, world: dict, name: str = "ws") -> MujocoLoop:
    workspace = _workspace(tmp_path / name / "workspace")
    out_dir = tmp_path / name / "out"
    scenario = MujocoScenario.from_dict(world)
    return MujocoLoop(workspace, out_dir, scenario)


def _run_until_finished(loop: MujocoLoop, cap: int = 2000) -> None:
    for _ in range(cap):
        loop.tick()
        if loop.finished:
            return
    raise AssertionError("locomotion run did not finish")


# ---------------------------------------------------------------------------
# scenario
# ---------------------------------------------------------------------------


def test_locomotion_fields_round_trip():
    scenario = MujocoScenario.from_dict(
        _world(locomotion_twist=[0.3, 0.0, -0.1])
    )
    assert scenario.locomotion is True
    assert scenario.locomotion_twist == (0.3, 0.0, -0.1)
    assert scenario.locomotion_duration_s == 12.0
    assert scenario.locomotion_goal_radius == 0.3
    assert set(scenario.goal_geoms) == set(flag_geom_names())
    again = MujocoScenario.from_dict(scenario.to_dict())
    assert again == scenario


def test_locomotion_defaults_keep_joint_tests_unchanged():
    scenario = MujocoScenario.from_dict({})
    assert scenario.locomotion is False
    assert scenario.goal_geoms == ()
    assert scenario.locomotion_duration_s == 20.0


def test_locomotion_rejects_bad_inputs():
    with pytest.raises(ScenarioError, match="triple"):
        MujocoScenario.from_dict(_world(locomotion_twist=[0.2, 0.0]))
    with pytest.raises(ScenarioError, match="within"):
        MujocoScenario.from_dict(
            _world(locomotion_twist=[2.0, 0.0, 0.0])
        )
    with pytest.raises(ScenarioError, match="positive"):
        MujocoScenario.from_dict(_world(locomotion_duration_s=0.0))
    with pytest.raises(ScenarioError, match="array"):
        MujocoScenario.from_dict(_world(goal_geoms="env_flag_base"))


# ---------------------------------------------------------------------------
# Twist contract
# ---------------------------------------------------------------------------


def test_twist_from_message_accepts_shapes():
    assert topics.twist_from_message((0.2, 0.0, 0.1)) == (0.2, 0.0, 0.1)
    assert topics.twist_from_message(
        {"linear": {"x": 0.2, "y": 0.0}, "angular": {"z": 0.1}}
    ) == (0.2, 0.0, 0.1)


def test_twist_from_message_rejects_malformed():
    assert topics.twist_from_message(object()) is None
    assert topics.twist_from_message((0.2, 0.0)) is None
    assert topics.twist_from_message((0.2, float("nan"), 0.0)) is None
    assert topics.twist_from_message({"linear": {"x": 1.0}}) is None


def test_set_twist_clamps_to_training_range(tmp_path: Path):
    loop = _loop(tmp_path, _world())
    assert isinstance(loop.runner, LocomotionRunner)
    loop.runner.set_twist(5.0, -5.0, 2.0)
    assert loop.runner.twist == (1.0, -1.0, 1.0)


def test_cmd_vel_outside_locomotion_is_invalid(tmp_path: Path):
    world = _world(locomotion=False, fix_base=True)
    world["tests"] = [
        {
            "id": "t0",
            "joint": "left_hip_yaw",
            "target": 0.1,
            "tolerance": 0.5,
            "timeout_s": 1.0,
        }
    ]
    loop = _loop(tmp_path, world)
    loop.on_cmd_vel((0.2, 0.0, 0.0))
    assert loop.invalid_commands == 1


# ---------------------------------------------------------------------------
# engine support
# ---------------------------------------------------------------------------


def test_engine_resolves_flag_body_and_goal_geoms(tmp_path: Path):
    loop = _loop(tmp_path, _world())
    pos = loop.engine.body_position(FLAG_BODY)
    assert pos is not None
    assert pos[0] == pytest.approx(0.80)
    assert pos[1] == pytest.approx(0.0)
    assert loop.engine.body_position("no_such_body") is None


def test_engine_rejects_unknown_goal_geom(tmp_path: Path):
    workspace = _workspace(tmp_path / "ws" / "workspace")
    scenario = MujocoScenario.from_dict(
        _world(goal_geoms=["no_such_geom"])
    )
    info = parse_mjcf((workspace / "robot.xml").read_text(), "robot.xml")
    with pytest.raises(MujocoEngineError, match="Goal geom"):
        MujocoEngine(workspace / "robot.xml", info, scenario=scenario)


# ---------------------------------------------------------------------------
# runner preconditions
# ---------------------------------------------------------------------------


def test_locomotion_refuses_welded_base(tmp_path: Path):
    with pytest.raises(MujocoEngineError, match="fix_base=false"):
        _loop(tmp_path, _world(fix_base=True))


def test_locomotion_requires_policy_rate(tmp_path: Path):
    with pytest.raises(MujocoEngineError, match="control_hz=50"):
        _loop(tmp_path, _world(control_hz=25.0))


# ---------------------------------------------------------------------------
# walking evidence
# ---------------------------------------------------------------------------


def test_walk_reaches_the_flag(tmp_path: Path):
    loop = _loop(tmp_path, _world())
    assert isinstance(loop.runner, LocomotionRunner)
    assert loop.runner.current_test_id == LOCOMOTION_TEST_ID
    _run_until_finished(loop)
    payload = loop.result_payload("tests_complete")
    assert payload["tests_total"] == 1
    assert payload["tests_passed"] == 1
    result = payload["tests"][0]
    assert result["kind"] == "locomotion"
    assert result["passed"] is True
    assert result["distance_m"] > 0.3
    assert result["min_flag_distance_m"] <= 0.3
    assert result["fallen"] is False
    assert result["contacts"] == 0
    assert loop.sample_count > 10
    loop.close()


def test_goal_settle_stands_before_verdict(tmp_path: Path):
    import numpy as np

    from app.simulator.microduck.policy import stand_pose

    loop = _loop(
        tmp_path,
        _world(
            env_layout={"env_flag": [0.1, 0.0]},
            locomotion_duration_s=8.0,
        ),
    )
    _run_until_finished(loop)
    # Reached on the first tick, then 4 s of creep+stand settle.
    assert loop.sim_time >= 4.0
    payload = loop.result_payload("tests_complete")
    result = payload["tests"][0]
    assert result["outcome"] == "goal_reached"
    assert result["passed"] is True
    assert result["fallen"] is False
    assert result["min_trunk_z"] > 0.08
    # Balanced at STAND, not frozen mid-stride (exact-frozen STAND topples,
    # so the bar is a close balanced stand, not bitwise equality).
    deviation = np.abs(np.array(loop.engine.joint_positions()) - stand_pose())
    assert float(deviation.max()) < 0.2
    loop.close()


def test_goal_settle_stops_near_flag_in_stand(tmp_path: Path):
    """A pass stops NEAR the flag (not at the 0.3 m edge) in STAND."""
    import numpy as np

    from app.simulator.microduck.policy import stand_pose

    loop = _loop(tmp_path, _world())
    _run_until_finished(loop)
    payload = loop.result_payload("tests_complete")
    result = payload["tests"][0]
    assert result["passed"] is True
    assert result["outcome"] == "goal_reached"
    # Final stop is near the flag center, not halfway at the pass edge.
    assert result["final_flag_distance_m"] <= 0.15
    assert result["min_flag_distance_m"] <= 0.3
    # Balanced STAND when the verdict is recorded.
    deviation = np.abs(np.array(loop.engine.joint_positions()) - stand_pose())
    assert float(deviation.max()) < 0.2
    loop.close()


def test_telemetry_records_commanded_twist(tmp_path: Path):
    workspace = tmp_path / "ws" / "workspace"
    out_dir = tmp_path / "ws" / "out"
    workspace.mkdir(parents=True)
    (workspace / "robot.xml").write_text(compose_scene_xml())
    stage_assets(workspace)
    scenario = MujocoScenario.from_dict(
        _world(env_layout={"env_flag": [0.1, 0.0]})
    )
    loop = MujocoLoop(workspace, out_dir, scenario)
    _run_until_finished(loop)
    loop.close()
    lines = (out_dir / "telemetry.jsonl").read_text().strip().split("\n")
    first = json.loads(lines[0])
    last = json.loads(lines[-1])
    assert first["twist"] == [0.2, 0.0, 0.0]
    assert last["twist"] == [0.0, 0.0, 0.0]


def test_standing_still_fails_with_shortfall_evidence(tmp_path: Path):
    loop = _loop(
        tmp_path,
        _world(locomotion_twist=[0.0, 0.0, 0.0], locomotion_duration_s=2.0),
    )
    _run_until_finished(loop)
    payload = loop.result_payload("tests_complete")
    result = payload["tests"][0]
    assert result["passed"] is False
    assert result["fallen"] is False
    assert "not reached" in result["reason"]
    assert result["final_error"] > 0.0
    loop.close()


def test_planned_estop_commands_a_safe_stop(tmp_path: Path):
    loop = _loop(tmp_path, _world(estop_after_s=2.0))
    _run_until_finished(loop)
    assert loop.estop_active is True
    payload = loop.result_payload("tests_complete")
    result = payload["tests"][0]
    assert result["fallen"] is False
    assert result["estop_latency"] is not None
    assert result["estop_latency"] < 2.0
    loop.close()


# ---------------------------------------------------------------------------
# evaluator verdicts
# ---------------------------------------------------------------------------


def _samples():
    return [
        {"t": 0.0, "collision": False},
        {"t": 8.0, "collision": False},
    ]


def test_evaluate_locomotion_success(tmp_path: Path):
    # Flag arranged one step ahead so a real runner pass stays fast.
    loop = _loop(
        tmp_path,
        _world(
            env_layout={"env_flag": [0.25, 0.0]},
            locomotion_duration_s=6.0,
        ),
    )
    _run_until_finished(loop)
    payload = loop.result_payload("tests_complete")
    loop.close()
    assert payload["tests"][0]["passed"] is True
    report, metrics = evaluate_mujoco(
        payload["tests"], _samples(), scenario=MujocoScenario.from_dict({})
    )
    assert report["success"] is True
    assert report["run_status"] == "success"
    assert "flag" in report["reasons"][0]
    assert metrics["success"] is True


def test_evaluate_locomotion_success_names_every_phase():
    """A multi-phase walk is not described as a joint-test suite."""
    report, _ = evaluate_mujoco(
        [
            {
                "id": "phase_1",
                "kind": "locomotion",
                "passed": True,
                "reason": "Kicked the ball 2.30 m.",
                "phase": 1,
                "phases": 2,
            },
            {
                "id": "phase_2",
                "kind": "locomotion",
                "passed": True,
                "reason": "Reached the target.",
                "phase": 2,
                "phases": 2,
            },
        ],
        _samples(),
        scenario=MujocoScenario.from_dict({}),
    )
    assert report["success"] is True
    assert report["reasons"] == [
        "All 2 locomotion phase(s) passed with no fall and no"
        " disqualifying contact."
    ]


def test_evaluate_locomotion_failure_names_the_walk():
    report, metrics = evaluate_mujoco(
        [
            {
                "id": LOCOMOTION_TEST_ID,
                "kind": "locomotion",
                "passed": False,
                "reason": "Flag not reached.",
                "final_error": 0.5,
                "tolerance": 0.3,
                "contacts": 0,
            }
        ],
        _samples(),
        scenario=MujocoScenario.from_dict({}),
    )
    assert report["success"] is False
    assert report["run_status"] == "failed"
    assert "Locomotion 'walk_to_flag' failed" in report["reasons"][0]
    assert metrics["goal_distance"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# harness end to end
# ---------------------------------------------------------------------------


def test_harness_walks_with_twist_controller(tmp_path: Path):
    from app.runner import harness

    workspace = _workspace(tmp_path / "ws" / "workspace")
    out_dir = tmp_path / "ws" / "out"
    out_dir.mkdir(parents=True)
    (workspace / "world.json").write_text(json.dumps(_world()))
    (workspace / "controller.py").write_text(WALK_CONTROLLER)
    code = harness.main([
        "--workspace", str(workspace), "--out", str(out_dir),
        "--seed", "0", "--transport", "loopback", "--entry", "controller.py",
    ])
    assert code == 0
    result = json.loads((out_dir / "result.json").read_text())
    assert result["reason"] == "tests_complete"
    assert result["tests_passed"] == 1
    assert result["tests_total"] == 1
    assert result["suite_size"] == 1
    assert result["tests"][0]["kind"] == "locomotion"
    assert result["tests"][0]["passed"] is True
    telemetry = (out_dir / "telemetry.jsonl").read_text().strip().split("\n")
    assert len(telemetry) > 10
    last = json.loads(telemetry[-1])
    assert last["x"] > 0.3


def test_wall_contact_disqualifies_the_run(tmp_path: Path):
    """Walking into an arena wall fails with contact evidence."""
    loop = _loop(
        tmp_path,
        _world(spawn_xy=[1.6, 0.0], locomotion_duration_s=12.0),
    )
    _run_until_finished(loop)
    payload = loop.result_payload("tests_complete")
    result = payload["tests"][0]
    assert result["passed"] is False
    assert result["contacts"] > 0
    assert "contact" in result["reason"].lower()
    loop.close()


def test_objective_defaults_to_flag_go_to(tmp_path: Path):
    loop = _loop(tmp_path, _world())
    assert loop.runner.objective_type == "go_to"
    assert loop.runner.goal_xy == loop.runner.flag_xy
    loop.close()


def test_objective_validation_rejects_nonsense():
    with pytest.raises(ScenarioError, match="objective.type"):
        MujocoScenario.from_dict({"objective": {"type": "fly"}})
    with pytest.raises(ScenarioError, match="objective.target"):
        MujocoScenario.from_dict(
            {"objective": {"type": "go_to", "target": [1.0]}}
        )
    with pytest.raises(ScenarioError, match="wander"):
        MujocoScenario.from_dict(
            {"objective": {"type": "wander", "target": [0.5, 0.0]}}
        )
    with pytest.raises(ScenarioError, match="within"):
        MujocoScenario.from_dict(
            {"objective": {"type": "go_to", "target": [5.0, 0.0]}}
        )
    parsed = MujocoScenario.from_dict(
        {"objective": {"type": "touch", "target": [1.9, 0.0]}}
    )
    assert parsed.objective.type == "touch"
    assert parsed.objective.target == (1.9, 0.0)
    assert MujocoScenario.from_dict({}).objective.type == "go_to"
    assert MujocoScenario.from_dict({"objective": None}).objective.type == "go_to"


def test_go_to_custom_target(tmp_path: Path):
    """The reference controller steers to /goal_pose, not the flag prop."""
    from app.runner import harness
    from app.simulator.robot_catalog import MICRODUCK_REFERENCE_CONTROLLER

    workspace = _workspace(tmp_path / "ws" / "workspace")
    out_dir = tmp_path / "ws" / "out"
    out_dir.mkdir(parents=True)
    world = _world(
        # Flag off the straight-line path: the run must chase the
        # objective target, ignoring the prop.
        env_layout={"env_flag": [0.1, 0.7]},
        locomotion_duration_s=25.0,
        time_limit=32.0,
        objective={"type": "go_to", "target": [1.5, 0.0]},
    )
    (workspace / "world.json").write_text(json.dumps(world))
    (workspace / "controller.py").write_text(MICRODUCK_REFERENCE_CONTROLLER)
    code = harness.main([
        "--workspace", str(workspace), "--out", str(out_dir),
        "--seed", "0", "--transport", "loopback", "--entry", "controller.py",
    ])
    assert code == 0
    result = json.loads((out_dir / "result.json").read_text())
    assert result["tests"][0]["passed"] is True, result["tests"][0]
    assert result["tests"][0]["outcome"] == "goal_reached"
    assert "1.50, 0.00" in result["tests"][0]["reason"]


def test_touch_requires_wall_contact_not_flag_brush(tmp_path: Path):
    """A flag brush mid-run must not finish a touch run passing."""
    loop = _loop(
        tmp_path,
        _world(objective={"type": "touch", "target": [1.9, 0.0]}),
    )
    loop.engine.goal_touch_geoms = {flag_geom_names()[0]}
    assert loop.runner._touched_wall() is False
    loop.engine.goal_touch_geoms = {"env_wall_px_geom"}
    assert loop.runner._touched_wall() is True
    loop.close()


def test_touch_wall_passes(tmp_path: Path):
    from app.simulator.microduck.model import wall_geom_names

    loop = _loop(
        tmp_path,
        _world(
            spawn_xy=[1.6, 0.0],
            locomotion_duration_s=12.0,
            goal_geoms=list(wall_geom_names()),
            objective={"type": "touch", "target": [1.9, 0.0]},
        ),
    )
    _run_until_finished(loop)
    payload = loop.result_payload("tests_complete")
    result = payload["tests"][0]
    assert result["passed"] is True, result
    assert result["outcome"] == "wall_touched"
    assert "wall" in result["reason"].lower()
    loop.close()


def test_wander_ignores_the_window(tmp_path: Path):
    """Wander has no window: a clean roam outlives it and passes at the limit."""
    loop = _loop(
        tmp_path,
        _world(
            # Flag off the straight-line path: wander ignores it, but the
            # duck must not physically plow through the pole.
            env_layout={"env_flag": [0.1, 0.7]},
            locomotion_duration_s=6.0,
            time_limit=7.0,
            objective={"type": "wander", "target": None},
        ),
    )
    try:
        # Mirror the harness: stop ticking at the time limit, then finalize.
        for _ in range(2000):
            loop.tick()
            if loop.finished or loop.timed_out:
                break
        assert loop.timed_out
        assert loop.runner.finished is False
        payload = loop.result_payload("timeout")
        result = payload["tests"][0]
        assert result["passed"] is True, result
        assert result["outcome"] == "run_ended"
        assert result["duration_s"] > 6.0
        assert result["distance_m"] > 0.5
    finally:
        loop.close()


def test_wander_wall_touch_passes(tmp_path: Path):
    """A wander run ends passing when the duck reaches the arena wall."""
    from app.simulator.microduck.model import wall_geom_names

    loop = _loop(
        tmp_path,
        _world(
            spawn_xy=[1.6, 0.0],
            locomotion_duration_s=6.0,
            goal_geoms=list(flag_geom_names()) + list(wall_geom_names()),
            objective={"type": "wander", "target": None},
        ),
    )
    try:
        _run_until_finished(loop)
        payload = loop.result_payload("tests_complete")
        result = payload["tests"][0]
        assert result["passed"] is True, result
        assert result["outcome"] == "wall_touched"
        assert "wall" in result["reason"].lower()
        assert result["phase_duration_s"] < 6.0
    finally:
        loop.close()


def test_wander_obstacle_contact_fails_immediately(tmp_path: Path):
    """Any disqualifying contact ends a wander run failing, without waiting."""
    loop = _loop(
        tmp_path,
        _world(
            locomotion_duration_s=30.0,
            objective={"type": "wander", "target": None},
        ),
    )
    try:
        for _ in range(10):
            loop.tick()
        assert loop.runner.finished is False
        # A crate brush lands in engine.contacts (only goal geoms are
        # excluded); the roam must end on this tick, not at the window.
        loop.engine.contacts = 1
        loop.runner.tick(loop.sim_time)
        assert loop.runner.finished is True
        result = loop.results()[-1]
        assert result["passed"] is False, result
        assert result["outcome"] == "contact"
        assert "disqualifying contact" in result["reason"].lower()
        assert result["phase_duration_s"] < 30.0
    finally:
        loop.close()


def test_wander_runs_exclude_walls_from_contacts():
    """Production wiring: walls are goal geoms when wander is a phase."""
    from app.services.execution import _microduck_goal_geoms
    from app.simulator.microduck.model import COMPOSED_MARKER, wall_geom_names

    class _Store:
        def list_files(self, project_id):
            return [{"path": "robot.xml"}]

        def get_file(self, project_id, path):
            return {"content": f"<mujoco><!-- {COMPOSED_MARKER} -->"}

    wander = _microduck_goal_geoms(
        _Store(), "p", {"objectives": [{"type": "wander"}]}
    )
    assert set(wall_geom_names()) <= set(wander)
    plain = _microduck_goal_geoms(
        _Store(), "p", {"objectives": [{"type": "go_to"}]}
    )
    assert not (set(wall_geom_names()) & set(plain))


def test_objective_target_body_resolves_the_prop(tmp_path: Path):
    """'Go to the box behind you' names a body; the runner tracks it live."""
    loop = _loop(
        tmp_path,
        _world(
            env_layout={"env_box_2": [-0.6, 0.1]},
            objective={
                "type": "go_to",
                "target": None,
                "target_body": "env_box_2",
            },
        ),
    )
    assert loop.runner.target_body == "env_box_2"
    box = loop.engine.body_position("env_box_2")
    assert box is not None
    assert loop.runner.goal_xy == pytest.approx((box[0], box[1]))
    # Creeping into a crate would be a disqualifying contact: no creep.
    assert loop.runner._creep_to_flag() is None
    loop.close()


def test_objective_target_body_validation():
    with pytest.raises(ScenarioError, match="target_body"):
        MujocoScenario.from_dict(
            {"objective": {"type": "go_to", "target_body": "no_such_body"}}
        )
    with pytest.raises(ScenarioError, match="go_to"):
        MujocoScenario.from_dict(
            {"objective": {"type": "kick", "target_body": "env_ball"}}
        )
    with pytest.raises(ScenarioError, match="kick"):
        MujocoScenario.from_dict(
            {"objective": {"type": "kick", "target": [0.3, 0.0]}}
        )
    parsed = MujocoScenario.from_dict(
        {
            "objective": {
                "type": "go_to",
                "target": None,
                "target_body": "env_box_1",
            }
        }
    )
    assert parsed.objective.to_dict()["target_body"] == "env_box_1"
    assert MujocoScenario.from_dict(parsed.to_dict()) == parsed


def test_target_body_missing_from_the_model_fails_the_run(tmp_path: Path):
    with pytest.raises(MujocoEngineError, match="env_box_9"):
        _loop(
            tmp_path,
            _world(objective={"type": "go_to", "target_body": "env_box_9"}),
        )


def test_kick_requires_the_ball_in_goal_geoms(tmp_path: Path):
    with pytest.raises(MujocoEngineError, match="goal_geoms"):
        _loop(tmp_path, _world(objective={"type": "kick", "target": None}))


def test_kick_run_pushes_the_ball_and_passes(tmp_path: Path):
    """The ball is dynamic: walking into it really rolls it, measurably."""
    loop = _loop(
        tmp_path,
        _world(
            env_layout={"env_ball": [0.5, 0.0]},
            goal_geoms=list(flag_geom_names()) + list(ball_geom_names()),
            locomotion_duration_s=12.0,
            objective={"type": "kick", "target": None},
        ),
    )
    assert loop.runner.objective_type == "kick"
    assert loop.runner.goal_xy == pytest.approx((0.5, 0.0))
    _run_until_finished(loop)
    result = loop.result_payload("tests_complete")["tests"][0]
    assert result["objective"] == "kick"
    assert result["ball_touched"] is True, result
    assert result["ball_displacement_m"] >= KICK_MIN_DISPLACEMENT, result
    assert result["passed"] is True, result
    assert result["outcome"] == "ball_kicked", result
    assert "ball" in result["reason"].lower()
    loop.close()


def test_kick_without_ball_contact_reports_evidence(tmp_path: Path):
    loop = _loop(
        tmp_path,
        _world(
            locomotion_twist=[0.0, 0.0, 0.0],
            locomotion_duration_s=2.0,
            goal_geoms=list(flag_geom_names()) + list(ball_geom_names()),
            objective={"type": "kick", "target": None},
        ),
    )
    _run_until_finished(loop)
    result = loop.result_payload("tests_complete")["tests"][0]
    assert result["passed"] is False
    assert result["ball_touched"] is False
    assert result["ball_displacement_m"] == 0.0
    assert "never touched" in result["reason"].lower()
    loop.close()


def test_harness_kicks_the_ball(tmp_path: Path):
    """End-to-end: a Twist controller walks into the ball and it rolls."""
    from app.runner import harness

    workspace = _workspace(tmp_path / "ws" / "workspace")
    out_dir = tmp_path / "ws" / "out"
    out_dir.mkdir(parents=True)
    world = _world(
        env_layout={"env_ball": [0.5, 0.0]},
        goal_geoms=list(flag_geom_names()) + list(ball_geom_names()),
        locomotion_duration_s=12.0,
        objective={"type": "kick", "target": None},
    )
    (workspace / "world.json").write_text(json.dumps(world))
    (workspace / "controller.py").write_text(WALK_CONTROLLER)
    code = harness.main([
        "--workspace", str(workspace), "--out", str(out_dir),
        "--seed", "0", "--transport", "loopback", "--entry", "controller.py",
    ])
    assert code == 0
    result = json.loads((out_dir / "result.json").read_text())
    kick = result["tests"][0]
    assert kick["objective"] == "kick"
    assert kick["ball_touched"] is True, kick
    assert kick["ball_displacement_m"] >= KICK_MIN_DISPLACEMENT, kick
    assert kick["passed"] is True, kick


def test_harness_publishes_each_locomotion_phase(tmp_path: Path, capsys):
    """The controller sees the CURRENT phase type: kick, then go_to."""
    from app.runner import harness

    workspace = _workspace(tmp_path / "ws" / "workspace")
    out_dir = tmp_path / "ws" / "out"
    out_dir.mkdir(parents=True)
    world = _world(
        env_layout={"env_ball": [0.5, 0.0]},
        goal_geoms=list(flag_geom_names()) + list(ball_geom_names()),
        locomotion_duration_s=20.0,
        objectives=[
            {"type": "kick", "target": None},
            {"type": "go_to", "target": None},
        ],
    )
    (workspace / "world.json").write_text(json.dumps(world))
    (workspace / "controller.py").write_text(PHASE_WALK_CONTROLLER)
    code = harness.main([
        "--workspace", str(workspace), "--out", str(out_dir),
        "--seed", "0", "--transport", "loopback", "--entry", "controller.py",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "objective phase: kick" in out, out
    assert "objective phase: go_to" in out, out
    # The channel reports the live phase, so order follows the sequence.
    assert out.index("objective phase: kick") < out.index("objective phase: go_to")


def test_harness_external_estop_settles_to_stand(tmp_path: Path):
    """External e-stop settles to STAND (not frozen mid-stride)."""
    import numpy as np

    from app.runner import harness
    from app.simulator.microduck.policy import stand_pose

    workspace = _workspace(tmp_path / "ws" / "workspace")
    out_dir = tmp_path / "ws" / "out"
    out_dir.mkdir(parents=True)
    (workspace / "world.json").write_text(json.dumps(_world()))
    (workspace / "controller.py").write_text(WALK_CONTROLLER)
    estop_file = out_dir / "estop"
    estop_file.write_text("estop\n", encoding="utf-8")
    code = harness.main([
        "--workspace", str(workspace), "--out", str(out_dir),
        "--seed", "0", "--transport", "loopback", "--entry", "controller.py",
        "--estop-file", str(estop_file),
    ])
    assert code == 0
    result = json.loads((out_dir / "result.json").read_text())
    # Settled through the policy (1 s stand), not cut after 3 ticks.
    assert result["tests"][0]["outcome"] == "estop_settled"
    assert result["tests"][0]["fallen"] is False
    telemetry = (out_dir / "telemetry.jsonl").read_text().strip().split("\n")
    last = json.loads(telemetry[-1])
    positions = np.array([j["position"] for j in last["joints"]])
    deviation = np.abs(positions - stand_pose())
    assert float(deviation.max()) < 0.2


def test_harness_steers_to_moved_flag(tmp_path: Path):
    """The starter controller closes the loop: odom/goal in, Twist out."""
    from app.runner import harness
    from app.simulator.robot_catalog import MICRODUCK_REFERENCE_CONTROLLER

    workspace = _workspace(tmp_path / "ws" / "workspace")
    out_dir = tmp_path / "ws" / "out"
    out_dir.mkdir(parents=True)
    world = _world(
        env_layout={"env_flag": [0.1, 0.7]},
        locomotion_duration_s=20.0,
        time_limit=30.0,
    )
    (workspace / "world.json").write_text(json.dumps(world))
    (workspace / "controller.py").write_text(MICRODUCK_REFERENCE_CONTROLLER)
    code = harness.main([
        "--workspace", str(workspace), "--out", str(out_dir),
        "--seed", "0", "--transport", "loopback", "--entry", "controller.py",
    ])
    assert code == 0
    result = json.loads((out_dir / "result.json").read_text())
    assert result["tests"][0]["passed"] is True, result["tests"][0]
    assert result["tests"][0]["min_flag_distance_m"] <= 0.3


def test_starter_controller_still_tracks_joints(tmp_path: Path):
    """The same starter file passes joint tests (no odom, tracking only)."""
    from app.runner import harness
    from app.simulator.robot_catalog import MICRODUCK_REFERENCE_CONTROLLER

    workspace = _workspace(tmp_path / "ws" / "workspace")
    out_dir = tmp_path / "ws" / "out"
    out_dir.mkdir(parents=True)
    world = _world(locomotion=False, fix_base=True)
    world.pop("goal_geoms")
    world["tests"] = [
        {
            "id": "t0",
            "joint": "left_hip_yaw",
            "target": 0.2,
            "tolerance": 0.1,
            "timeout_s": 5.0,
        }
    ]
    (workspace / "world.json").write_text(json.dumps(world))
    (workspace / "controller.py").write_text(MICRODUCK_REFERENCE_CONTROLLER)
    code = harness.main([
        "--workspace", str(workspace), "--out", str(out_dir),
        "--seed", "0", "--transport", "loopback", "--entry", "controller.py",
    ])
    assert code == 0
    result = json.loads((out_dir / "result.json").read_text())
    assert result["reason"] == "tests_complete"
    assert result["tests_passed"] == 1


# ---------------------------------------------------------------------------
# run creation
# ---------------------------------------------------------------------------


def test_locomotion_run_stores_free_standing_world(client, project_id):
    created = client.post(
        f"/api/projects/{project_id}/robot", json={"robot_id": "microduck"}
    )
    assert created.status_code == 200, created.text
    response = client.post(
        f"/api/projects/{project_id}/runs",
        json={"world_config": {"locomotion": True}, "seed": 1},
    )
    assert response.status_code == 201, response.text
    detail = client.get(f"/api/runs/{response.json()['id']}").json()
    world = detail["world_config"]
    assert world["locomotion"] is True
    assert world["fix_base"] is False
    assert set(world["goal_geoms"]) == set(flag_geom_names())
    assert world.get("tests") in (None, [])


def test_kick_run_whitelists_the_ball(client, project_id):
    """A kick objective must not grade its own aim as a collision."""
    created = client.post(
        f"/api/projects/{project_id}/robot", json={"robot_id": "microduck"}
    )
    assert created.status_code == 200, created.text
    response = client.post(
        f"/api/projects/{project_id}/runs",
        json={
            "world_config": {
                "locomotion": True,
                "objective": {"type": "kick", "target": None},
            },
            "seed": 1,
        },
    )
    assert response.status_code == 201, response.text
    detail = client.get(f"/api/runs/{response.json()['id']}").json()
    world = detail["world_config"]
    assert set(flag_geom_names()) <= set(world["goal_geoms"])
    assert set(ball_geom_names()) <= set(world["goal_geoms"])


# ---------------------------------------------------------------------------
# Objective sequences
# ---------------------------------------------------------------------------


def _spawn_xy(tmp_path: Path) -> tuple[float, float]:
    """Deterministic spawn pose from an identical composed scene."""
    probe = _loop(tmp_path, _world(), name="probe")
    try:
        x, y, _yaw, _v, _omega = probe.engine.base_pose()
        return (float(x), float(y))
    finally:
        probe.close()


def test_parse_objectives_accepts_single_and_list():
    assert [p.type for p in parse_objectives(None)] == ["go_to"]
    one = parse_objectives({"type": "touch", "target": [1.9, 0.0]})
    assert [p.type for p in one] == ["touch"]
    three = parse_objectives(
        [
            {"type": "kick", "target": None, "target_body": None},
            {"type": "go_to", "target": None, "target_body": None},
            {"type": "wander", "target": None, "target_body": None},
        ]
    )
    assert [p.type for p in three] == ["kick", "go_to", "wander"]
    with pytest.raises(ScenarioError, match="at least one"):
        parse_objectives([])
    with pytest.raises(ScenarioError, match="object, a list"):
        parse_objectives("kick")
    with pytest.raises(ScenarioError, match="must be"):
        parse_objectives(
            [
                {"type": "wander", "target": None, "target_body": None},
                {"type": "go_to", "target": None, "target_body": None},
            ]
        )


def test_scenario_objectives_list_and_legacy_single():
    two = MujocoScenario.from_dict(
        {
            "objectives": [
                {"type": "kick", "target": None, "target_body": None},
                {"type": "go_to", "target": None, "target_body": None},
            ]
        }
    )
    assert [p.type for p in two.phases] == ["kick", "go_to"]
    assert two.objective.type == "kick"
    assert [o["type"] for o in two.to_dict()["objectives"]] == ["kick", "go_to"]
    assert two.to_dict()["objective"]["type"] == "kick"
    one = MujocoScenario.from_dict(
        {"objective": {"type": "touch", "target": [1.9, 0.0]}}
    )
    assert [p.type for p in one.phases] == ["touch"]


def test_sequence_advances_and_records_each_phase(tmp_path: Path):
    """Two go_to phases at spawn complete, switch goal, and both pass."""
    sx, sy = _spawn_xy(tmp_path)
    loop = _loop(
        tmp_path,
        _world(
            objectives=[
                {"type": "go_to", "target": [sx, sy], "target_body": None},
                {"type": "go_to", "target": [sx + 0.1, sy], "target_body": None},
            ]
        ),
        name="seq",
    )
    try:
        loop.tick()
        assert loop.runner.phase_index == 1
        assert loop.runner.objective_type == "go_to"
        assert loop.runner.goal_xy == pytest.approx((sx + 0.1, sy))
        kinds = [e["type"] for e in loop.drain_events()]
        assert "locomotion_phase" in kinds
        _run_until_finished(loop)
        results = loop.results()
        assert [r["outcome"] for r in results] == ["goal_reached", "goal_reached"]
        assert [r["id"] for r in results] == [
            "walk_to_flag_p1_go_to",
            "walk_to_flag_p2_go_to",
        ]
        assert [r["phase"] for r in results] == [1, 2]
        assert all(r["passed"] for r in results)
        assert loop.runner.all_tests_passed
        payload = loop.result_payload("tests_complete")
        assert payload["tests_total"] == 2
        assert payload["tests_passed"] == 2
    finally:
        loop.close()


def test_phase_advance_switches_goal_and_objective_type(tmp_path: Path):
    """After phase 1 the runner aims the phase 2 goal immediately."""
    sx, sy = _spawn_xy(tmp_path)
    loop = _loop(
        tmp_path,
        _world(
            objectives=[
                {"type": "go_to", "target": [sx, sy], "target_body": None},
                {"type": "touch", "target": [1.9, 0.0], "target_body": None},
            ]
        ),
        name="switch",
    )
    try:
        loop.tick()
        assert loop.runner.phase_index == 1
        assert loop.runner.objective_type == "touch"
        assert loop.runner.goal_xy == (1.9, 0.0)
        assert loop.runner.current_test_id == "walk_to_flag_p2_touch"
    finally:
        loop.close()


def test_phase_transition_summarizes_in_run_logs():
    from app.runner.mujoco_loop import summarize_log_events

    lines = summarize_log_events(
        [
            {
                "type": "locomotion_phase",
                "test": "walk_to_flag_p2_go_to",
                "phase": 2,
                "phases": 2,
                "objective": "go_to",
                "goal": [1.0, 0.0],
            }
        ]
    )
    assert "phase 2/2 (go_to)" in lines[0]


def test_each_phase_gets_its_own_time_window(tmp_path: Path):
    """The window restarts per phase instead of being shared by the run.

    A single shared window starved later phases: a kick that used most of
    the budget left the following go_to phase failing with "not reached"
    while the controller was still walking toward the target.
    """
    loop = _loop(
        tmp_path,
        _world(
            locomotion_duration_s=4.0,
            objectives=[
                {"type": "go_to", "target": None, "target_body": None},
                {
                    "type": "go_to",
                    "target": [1.9, 0.0],
                    "target_body": None,
                },
            ],
        ),
        name="windows",
    )
    runner = loop.runner
    try:
        # Two simulated seconds into phase 1, then the walker is judged to
        # have reached its goal (the loopback harness walks a fixed heading,
        # so the goal is forced instead of steered to).
        for _ in range(100):
            loop.tick()
        assert runner.phase_index == 0
        runner.min_flag_m = 0.0
        loop.tick()
        handover = loop.sim_time
        assert runner.phase_index == 1
        # Phase 2's clock starts at the handover, not at the run start.
        assert runner.phase_deadline == pytest.approx(handover + 4.0)
        # Tick past the old shared 4 s window: phase 2 is still graded.
        while loop.sim_time < handover + 3.9:
            loop.tick()
        assert runner.finished is False
        assert [r["phase"] for r in loop.results()] == [1]
        _run_until_finished(loop)
        second = loop.results()[-1]
        assert second["phase"] == 2
        assert second["outcome"] == "duration_elapsed"
        # It used its own 4 s window (the settle is not part of the phase).
        assert second["phase_duration_s"] == pytest.approx(4.0, abs=0.05)
        assert second["phase_budget_s"] == 4.0
        assert "4s window" in second["reason"]
    finally:
        loop.close()


def test_locomotion_run_scales_the_scenario_time_limit(client, project_id):
    """The hard scenario cap must cover every phase's window."""
    created = client.post(
        f"/api/projects/{project_id}/robot", json={"robot_id": "microduck"}
    )
    assert created.status_code == 200, created.text
    response = client.post(
        f"/api/projects/{project_id}/runs",
        json={
            "world_config": {
                "locomotion": True,
                "objectives": [
                    {"type": "kick", "target": None, "target_body": None},
                    {
                        "type": "go_to",
                        "target": None,
                        "target_body": "env_box_2",
                    },
                ],
            },
            "seed": 1,
        },
    )
    assert response.status_code == 201, response.text
    detail = client.get(f"/api/runs/{response.json()['id']}").json()
    world = detail["world_config"]
    assert world["locomotion_duration_s"] == 20.0
    # Two 20 s phase windows plus the settle margin, not the 30 s default.
    assert world["time_limit"] >= 2 * 20.0


def test_unfinished_first_phase_fails_the_run(tmp_path: Path):
    """A phase that never completes fails instead of advancing."""
    loop = _loop(
        tmp_path,
        _world(
            locomotion_duration_s=0.5,
            objectives=[
                {"type": "go_to", "target": [1.9, 0.0], "target_body": None},
                {"type": "go_to", "target": None, "target_body": None},
            ],
        ),
        name="failfast",
    )
    try:
        _run_until_finished(loop)
        results = loop.results()
        assert len(results) == 1
        assert results[0]["outcome"] == "duration_elapsed"
        assert results[0]["passed"] is False
        assert not loop.runner.all_tests_passed
    finally:
        loop.close()
