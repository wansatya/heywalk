"""MuJoCo backend tests: scenario, robot package, engine, tests, evaluator.

Covers the primary simulation backend (AGENTS.md sections 7 and 8): the
scenario/test-suite contract, MJCF/URDF/ros2_control validation and synthesis,
the physics engine's position-command interface, the platform-owned automated
joint tests, and the objective evaluator. Nothing here executes model output
inside the API process: the harness runs as a child process.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.config import Settings
from app.db import Store
from app.models.schemas import RunOut
from app.runner import RunnerResult
from app.runner.local_runner import LocalSubprocessRunner
from app.services import meta_model
from app.services.execution import ExecutionManager, RunEventBus, is_terminal
from app.services.generation import install_starter_robot
from app.simulator.robot_catalog import starter_robot_summary, starter_robots
from app.simulator.mujoco_engine import MujocoEngine, MujocoEngineError
from app.simulator.mujoco_tests import (
    EVALUATOR_VERSION,
    JointTestRunner,
    evaluate_mujoco,
)
from app.simulator.robot_description import (
    RobotPackageError,
    build_robot_package,
    find_model_file,
    parse_mjcf,
    synthesize_ros2_control,
    synthesize_urdf,
    validate_ros2_control_text,
)
from app.simulator.scenario import (
    BACKEND_DIFF_DRIVE,
    BACKEND_MUJOCO,
    MUJOCO_DEFAULTS,
    JointTest,
    MujocoScenario,
    ScenarioError,
    default_joint_target,
    default_tests_for_joints,
    infer_backend,
    normalize_backend,
    parse_tests,
    safe_relative_path,
)

ROBOT_XML = meta_model.DEV_MOCK_ROBOT_XML
CONTROLLER = meta_model.DEV_MOCK_CONTROLLER


# ---------------------------------------------------------------------------
# Scenario / backend selection
# ---------------------------------------------------------------------------


def test_normalize_backend_aliases():
    assert normalize_backend(None) == BACKEND_MUJOCO
    assert normalize_backend("auto") == BACKEND_MUJOCO
    assert normalize_backend("MuJoCo") == BACKEND_MUJOCO
    assert normalize_backend("2d") == BACKEND_DIFF_DRIVE
    assert normalize_backend("diff-drive") == BACKEND_DIFF_DRIVE
    with pytest.raises(ScenarioError):
        normalize_backend("gazebo")


def test_infer_backend_prefers_mujoco_for_new_runs():
    assert infer_backend(None) == BACKEND_MUJOCO
    assert infer_backend({}) == BACKEND_MUJOCO
    assert infer_backend({"time_limit": 5.0}) == BACKEND_MUJOCO


def test_infer_backend_keeps_legacy_arena_worlds():
    legacy = {"goal": {"x": 1.0, "y": 2.0}, "obstacles": []}
    assert infer_backend(legacy) == BACKEND_DIFF_DRIVE
    # An explicit backend always wins over the legacy markers.
    assert infer_backend({**legacy, "backend": "mujoco"}) == BACKEND_MUJOCO
    assert infer_backend({"backend": "2d"}) == BACKEND_DIFF_DRIVE


def test_scenario_round_trip_stores_hidden_defaults():
    scenario = MujocoScenario.from_dict({"seed": 4})
    stored = scenario.to_dict()
    assert stored["backend"] == BACKEND_MUJOCO
    assert stored["dt"] == MUJOCO_DEFAULTS["dt"]
    assert stored["seed"] == 4
    assert stored["tests"] == []


def test_scenario_rejects_out_of_range_control_rate():
    with pytest.raises(ScenarioError, match="physics rate"):
        MujocoScenario.from_dict({"dt": 0.01, "control_hz": 500.0})


def test_scenario_layout_round_trip():
    scenario = MujocoScenario.from_dict({
        "spawn_xy": [0.5, -0.25],
        "spawn_yaw": 1.5707,
        "env_layout": {"env_ball": [0.1, 0.2]},
    })
    assert scenario.spawn_xy == (0.5, -0.25)
    assert scenario.spawn_yaw == pytest.approx(1.5707)
    assert scenario.env_layout == (("env_ball", 0.1, 0.2),)
    stored = scenario.to_dict()
    assert stored["spawn_xy"] == [0.5, -0.25]
    assert stored["env_layout"] == [{"body": "env_ball", "x": 0.1, "y": 0.2}]
    again = MujocoScenario.from_dict(stored)
    assert again.spawn_xy == scenario.spawn_xy
    assert again.env_layout == scenario.env_layout


def test_scenario_layout_defaults_to_composed_scene():
    scenario = MujocoScenario.from_dict({})
    assert scenario.spawn_xy is None
    assert scenario.spawn_yaw is None
    assert scenario.env_layout == ()


def test_scenario_rejects_bad_layout():
    with pytest.raises(ScenarioError, match="within"):
        MujocoScenario.from_dict({"spawn_xy": [5.0, 0.0]})
    with pytest.raises(ScenarioError, match="pair"):
        MujocoScenario.from_dict({"spawn_xy": [1.0]})
    with pytest.raises(ScenarioError, match="finite"):
        MujocoScenario.from_dict({"spawn_yaw": float("inf")})
    with pytest.raises(ScenarioError, match="within"):
        MujocoScenario.from_dict({"env_layout": {"env_ball": [0.0, 9.0]}})
    with pytest.raises(ScenarioError, match="pair"):
        MujocoScenario.from_dict({"env_layout": {"env_ball": [0.0]}})


def test_scenario_rejects_path_traversal_in_model_path():
    with pytest.raises(ScenarioError, match="illegal path segment"):
        MujocoScenario.from_dict({"model_path": "../robot.xml"})
    with pytest.raises(ScenarioError, match="relative"):
        safe_relative_path("/etc/passwd", "model_path")


def test_joint_test_validation_guards():
    with pytest.raises(ScenarioError, match="must name a joint"):
        JointTest(id="a", joint=" ", target=0.1)
    with pytest.raises(ScenarioError, match="unsupported kind"):
        JointTest(id="a", joint="j", target=0.1, kind="teleport")
    with pytest.raises(ScenarioError, match="guard"):
        JointTest(id="a", joint="j", target=1000.0)
    with pytest.raises(ScenarioError, match="tolerance"):
        JointTest(id="a", joint="j", target=0.1, tolerance=0.0)


def test_parse_tests_rejects_duplicate_ids():
    tests = parse_tests([{"id": "a", "joint": "j", "target": 0.1}])
    assert tests[0].target == 0.1
    with pytest.raises(ScenarioError, match="Duplicate"):
        parse_tests(
            [
                {"id": "a", "joint": "j", "target": 0.1},
                {"id": "a", "joint": "j", "target": 0.2},
            ]
        )
    with pytest.raises(ScenarioError, match="JSON array"):
        parse_tests("nope")


def test_default_joint_target_stays_inside_range():
    home, target = default_joint_target(-1.0, 1.0)
    assert home == 0.0 and 0.0 < target <= 1.0
    home, target = default_joint_target(0.2, 1.0)
    assert home == 0.2 and target > home
    home, target = default_joint_target(-1.0, -0.2)
    assert home == -0.2 and target < home


def test_default_tests_are_derived_per_actuated_joint():
    tests = default_tests_for_joints([("a", -1.0, 1.0), ("b", -1.0, 1.0)])
    assert [test.joint for test in tests] == ["a", "b"]
    with pytest.raises(ScenarioError, match="no actuated joints"):
        default_tests_for_joints([])


# ---------------------------------------------------------------------------
# Robot package: MJCF / URDF / ros2_control
# ---------------------------------------------------------------------------


def test_parse_mjcf_reads_named_position_actuated_joints():
    info = parse_mjcf(ROBOT_XML, "robot.xml")
    assert info.joint_names == ("shoulder", "elbow")
    assert [joint.type for joint in info.joints] == ["hinge", "hinge"]
    assert info.joints[0].lower == pytest.approx(-1.2)
    assert info.joints[0].upper == pytest.approx(1.2)
    assert info.body_count == 4


def test_parse_mjcf_converts_degree_ranges():
    xml = """
    <mujoco>
      <worldbody>
        <body name="b">
          <joint name="j" type="hinge" range="-90 90"/>
        </body>
      </worldbody>
      <actuator><position name="p" joint="j"/></actuator>
    </mujoco>
    """
    info = parse_mjcf(xml)
    assert info.joints[0].lower == pytest.approx(-1.5707963, rel=1e-6)
    assert any("radians" in note for note in info.notes)


@pytest.mark.parametrize(
    "xml, message",
    [
        ("<robot/>", "<mujoco> root"),
        (
            "<mujoco><worldbody><body><joint name='j' type='ball'/></body>"
            "</worldbody><actuator><position joint='j'/></actuator></mujoco>",
            "unsupported type",
        ),
        (
            "<mujoco><worldbody><body><joint name='j'/></body></worldbody>"
            "<actuator><motor joint='j'/></actuator></mujoco>",
            "require <position> actuators",
        ),
        (
            "<mujoco><worldbody><body><joint name='j'/></body></worldbody>"
            "</mujoco>",
            "no <actuator>",
        ),
        (
            "<mujoco><worldbody><body><joint name='j'/></body></worldbody>"
            "<actuator><position joint='missing'/></actuator></mujoco>",
            "unknown joint",
        ),
        (
            "<mujoco><worldbody><body><joint/></body></worldbody>"
            "<actuator><position joint='j'/></actuator></mujoco>",
            "needs a name",
        ),
    ],
)
def test_parse_mjcf_reports_actionable_errors(xml, message):
    with pytest.raises(RobotPackageError, match=message):
        parse_mjcf(xml)


def test_parse_mjcf_rejects_doctype():
    with pytest.raises(RobotPackageError, match="DOCTYPE"):
        parse_mjcf('<!DOCTYPE foo [<!ENTITY a "b">]><mujoco/>')


def test_parse_mjcf_rejects_duplicate_and_double_actuation():
    duplicate = """
    <mujoco><worldbody><body>
      <joint name="j"/><joint name="j"/>
    </body></worldbody>
    <actuator><position joint="j"/></actuator></mujoco>
    """
    with pytest.raises(RobotPackageError, match="Duplicate"):
        parse_mjcf(duplicate)
    twice = """
    <mujoco><worldbody><body><joint name="j"/></body></worldbody>
    <actuator><position joint="j"/><position joint="j"/></actuator></mujoco>
    """
    with pytest.raises(RobotPackageError, match="more than one actuator"):
        parse_mjcf(twice)


def test_build_robot_package_synthesizes_missing_companions():
    package = build_robot_package({"robot.xml": ROBOT_XML})
    assert package.model_path == "robot.xml"
    assert set(package.files) == {"robot.xml", "robot.urdf", "ros2_control.yaml"}
    assert "shoulder" in package.files["robot.urdf"]
    assert "elbow" in package.files["ros2_control.yaml"]
    assert any("synthesized" in note for note in package.notes)


def test_build_robot_package_keeps_author_files_when_consistent():
    urdf = synthesize_urdf(parse_mjcf(ROBOT_XML, "robot.xml"))
    control = synthesize_ros2_control(parse_mjcf(ROBOT_XML, "robot.xml"))
    package = build_robot_package(
        {"robot.xml": ROBOT_XML, "robot.urdf": urdf, "ros2_control.yaml": control}
    )
    assert package.files["robot.urdf"] == urdf
    assert package.files["ros2_control.yaml"] == control
    assert not any("synthesized" in note for note in package.notes)


def test_build_robot_package_rejects_joint_mismatch():
    info = parse_mjcf(ROBOT_XML, "robot.xml")
    urdf = synthesize_urdf(info).replace('name="shoulder"', 'name="other"')
    with pytest.raises(RobotPackageError, match="missing joint"):
        build_robot_package({"robot.xml": ROBOT_XML, "robot.urdf": urdf})


def test_ros2_control_validation_requires_every_joint():
    with pytest.raises(RobotPackageError, match="does not mention joint"):
        validate_ros2_control_text(
            "controller_manager: {}", "ros2_control.yaml", ("shoulder", "elbow")
        )
    with pytest.raises(RobotPackageError, match="non-empty YAML mapping"):
        validate_ros2_control_text("", "ros2_control.yaml", ())
    notes = validate_ros2_control_text(
        "shoulder: {}\n", "ros2_control.yaml", ("shoulder",)
    )
    assert any("controller_manager" in note for note in notes)


def test_find_model_file_falls_back_to_any_mjcf():
    assert find_model_file({"robot.xml": ROBOT_XML}) == "robot.xml"
    assert find_model_file({"models/arm.xml": ROBOT_XML}) == "models/arm.xml"
    with pytest.raises(RobotPackageError, match="No MJCF robot model"):
        find_model_file({"controller.py": "#"})
    with pytest.raises(RobotPackageError, match="No MJCF robot model"):
        find_model_file({"notes.xml": "<robot/>"})


# ---------------------------------------------------------------------------
# Physics engine
# ---------------------------------------------------------------------------


def _engine(tmp_path, *, xml=ROBOT_XML, **scenario_overrides):
    path = tmp_path / "robot.xml"
    path.write_text(xml)
    info = parse_mjcf(xml, "robot.xml")
    scenario = MujocoScenario.from_dict(scenario_overrides)
    return MujocoEngine(path, info, scenario=scenario)


def test_engine_binds_position_actuators(tmp_path):
    engine = _engine(tmp_path)
    assert engine.joint_count == 2
    assert [binding.name for binding in engine.bindings] == ["shoulder", "elbow"]
    assert engine.joint_positions() == pytest.approx([0.0, 0.0])


def test_engine_rejects_bad_command_length(tmp_path):
    engine = _engine(tmp_path)
    assert engine.set_commands([0.1, 0.2]) is True
    assert engine.set_commands([0.1]) is False
    assert engine.set_commands([0.1, 0.2, 0.3]) is False


def test_engine_clamps_commands_to_joint_range(tmp_path):
    engine = _engine(tmp_path)
    engine.set_commands([10.0, -10.0])
    assert engine.commands[0] == pytest.approx(1.2)
    assert engine.commands[1] == pytest.approx(-1.2)


def test_engine_steps_settle_on_target(tmp_path):
    engine = _engine(tmp_path)
    engine.set_commands([0.4, -0.3])
    engine.advance(MujocoScenario.from_dict({}).physics_steps_per_control * 100)
    positions = engine.joint_positions()
    assert positions[0] == pytest.approx(0.4, abs=0.05)
    assert positions[1] == pytest.approx(-0.3, abs=0.05)
    snapshot = engine.snapshot()
    assert [joint["name"] for joint in snapshot["joints"]] == ["shoulder", "elbow"]
    assert snapshot["joints"][0]["target"] == pytest.approx(0.4)


def test_engine_hold_current_freezes_motion(tmp_path):
    engine = _engine(tmp_path)
    engine.set_commands([0.5, 0.5])
    engine.advance(200)
    engine.hold_current()
    engine.advance(200)
    engine.hold_current()
    before = engine.joint_positions()
    engine.advance(200)
    after = engine.joint_positions()
    assert after[0] == pytest.approx(before[0], abs=0.02)


def test_engine_reset_returns_to_home(tmp_path):
    engine = _engine(tmp_path)
    engine.set_commands([0.6, 0.6])
    engine.advance(300)
    assert abs(engine.joint_positions()[0]) > 0.1
    engine.reset()
    assert engine.joint_positions() == pytest.approx([0.0, 0.0], abs=1e-6)
    assert engine.commands == pytest.approx(engine.home)


def test_engine_uses_implicit_integrator_by_default(tmp_path):
    import mujoco

    engine = _engine(tmp_path)
    assert engine.model.opt.integrator == int(
        mujoco.mjtIntegrator.mjINT_IMPLICITFAST
    )


def test_engine_honours_explicit_integrator(tmp_path):
    import mujoco

    xml = ROBOT_XML.replace(
        '<option timestep="0.002"/>', '<option integrator="Euler"/>'
    )
    engine = _engine(tmp_path, xml=xml)
    assert engine.model.opt.integrator == int(mujoco.mjtIntegrator.mjINT_EULER)


def test_engine_rejects_include_and_escaping_assets(tmp_path):
    include = ROBOT_XML.replace(
        "<worldbody>", '<include file="/etc/passwd"/><worldbody>'
    )
    with pytest.raises(MujocoEngineError, match="include"):
        _engine(tmp_path, xml=include)
    escape = ROBOT_XML.replace(
        "<worldbody>", '<worldbody><geom type="mesh" mesh="../../x"/>'
    )
    # The mesh attribute is not an asset path; use a real asset section.
    del escape
    asset = ROBOT_XML.replace(
        "</mujoco>",
        '<asset><mesh name="m" file="../../etc/shadow"/></asset></mujoco>',
    )
    with pytest.raises(MujocoEngineError, match="workspace"):
        _engine(tmp_path, xml=asset)


# ---------------------------------------------------------------------------
# Automated joint tests + evaluator
# ---------------------------------------------------------------------------


def _run_tests(tmp_path, *, drive, scenario_overrides=None):
    """Run the test suite with a fixed 'controller' policy."""
    path = tmp_path / "robot.xml"
    path.write_text(ROBOT_XML)
    scenario = MujocoScenario.from_dict(scenario_overrides or {})
    engine = MujocoEngine(path, parse_mjcf(ROBOT_XML, "robot.xml"), scenario=scenario)
    runner = JointTestRunner(engine, scenario)
    sim_time = 0.0
    for _ in range(int(scenario.time_limit / scenario.control_period) + 10):
        if runner.finished:
            break
        commands = drive(engine, runner)
        if commands is not None:
            engine.set_commands(commands)
        if runner.estop_active:
            engine.hold_current()
        engine.advance(scenario.physics_steps_per_control)
        sim_time += scenario.control_period
        runner.tick(sim_time)
    return runner, scenario


def test_joint_tests_pass_with_a_tracking_controller(tmp_path):
    runner, scenario = _run_tests(
        tmp_path, drive=lambda engine, runner: runner.target_payload()
    )
    assert runner.finished
    assert runner.all_tests_passed
    assert [result["id"] for result in runner.results] == [
        "shoulder_to_0.50rad",
        "elbow_to_0.50rad",
    ]
    assert runner.events[0]["type"] == "test_started"


def test_joint_tests_fail_when_controller_never_moves(tmp_path):
    runner, scenario = _run_tests(tmp_path, drive=lambda engine, runner: [0.0, 0.0])
    assert runner.finished
    assert not runner.all_tests_passed
    first = runner.results[0]
    assert first["passed"] is False
    assert "did not reach" in first["reason"]
    assert first["final_error"] == pytest.approx(0.5, abs=0.02)


def test_joint_tests_fail_when_controller_sends_wrong_length(tmp_path):
    runner, _ = _run_tests(tmp_path, drive=lambda engine, runner: [0.0])
    assert not runner.all_tests_passed


def test_estop_test_measures_stop_latency(tmp_path):
    runner, _ = _run_tests(
        tmp_path,
        drive=lambda engine, runner: runner.target_payload(),
        scenario_overrides={"estop_after_s": 0.5, "time_limit": 40.0},
    )
    estop = next(r for r in runner.results if r["kind"] == "estop")
    assert estop["passed"] is True
    assert estop["estop_latency"] is not None
    assert estop["estop_latency"] <= 0.5


def test_suite_names_unknown_joint_is_rejected(tmp_path):
    path = tmp_path / "robot.xml"
    path.write_text(ROBOT_XML)
    scenario = MujocoScenario.from_dict(
        {"tests": [{"id": "t", "joint": "wrist", "target": 0.1}]}
    )
    engine = MujocoEngine(path, parse_mjcf(ROBOT_XML, "robot.xml"), scenario=scenario)
    with pytest.raises(ValueError, match="unknown joint"):
        JointTestRunner(engine, scenario)


def _samples(positions, targets, t_step=0.05):
    samples = []
    for index, position in enumerate(positions):
        samples.append(
            {
                "t": index * t_step,
                "x": 0.0,
                "y": 0.0,
                "theta": 0.0,
                "v": 0.0,
                "omega": 0.0,
                "collision": False,
                "joints": [
                    {"name": "shoulder", "position": position,
                     "velocity": 0.0, "target": targets[index]}
                ],
            }
        )
    return samples


def test_evaluate_mujoco_success_requires_all_tests():
    scenario = MujocoScenario.from_dict({})
    results = [
        {"id": "a", "kind": "joint_move", "joint": "shoulder", "passed": True,
         "final_error": 0.01, "tolerance": 0.05, "contacts": 0},
    ]
    report, metrics = evaluate_mujoco(
        results, _samples([0.0, 0.5], [0.5, 0.5]), scenario=scenario
    )
    assert report["success"] is True
    assert report["run_status"] == "success"
    assert metrics["tests_total"] == 1
    assert metrics["tests_passed"] == 1
    assert metrics["completion_time"] == pytest.approx(0.05)


def test_evaluate_mujoco_reports_failed_test_evidence():
    scenario = MujocoScenario.from_dict({})
    results = [
        {"id": "a", "kind": "joint_move", "joint": "shoulder", "passed": False,
         "final_error": 0.4, "tolerance": 0.05, "contacts": 0,
         "reason": "Joint 'shoulder' did not reach +0.500 rad within 5s."},
    ]
    report, _ = evaluate_mujoco(
        results, _samples([0.0, 0.1], [0.5, 0.5]), scenario=scenario
    )
    assert report["success"] is False
    assert report["run_status"] == "failed"
    assert any("did not reach" in reason for reason in report["reasons"])


def test_evaluate_mujoco_ignores_unpassed_timeout_and_collisions():
    scenario = MujocoScenario.from_dict({})
    collided = _samples([0.0, 0.5], [0.5, 0.5])
    collided[1]["collision"] = True
    report, metrics = evaluate_mujoco(
        [{"id": "a", "kind": "joint_move", "joint": "shoulder", "passed": True,
          "final_error": 0.0, "tolerance": 0.05, "contacts": 1}],
        collided,
        scenario=scenario,
    )
    assert report["success"] is False
    assert metrics["collision_count"] == 1
    assert any("Collision" in reason for reason in report["reasons"])

    report, _ = evaluate_mujoco(
        [], [],
        scenario=scenario,
        timed_out=True,
        exit_code=None,
    )
    assert report["run_status"] == "timeout"
    assert report["success"] is False


def test_evaluate_mujoco_maps_controller_crash_to_error():
    scenario = MujocoScenario.from_dict({})
    report, _ = evaluate_mujoco(
        [{"id": "a", "kind": "joint_move", "joint": "shoulder", "passed": True,
          "final_error": 0.0, "tolerance": 0.05, "contacts": 0}],
        _samples([0.0, 0.5], [0.5, 0.5]),
        scenario=scenario,
        exit_code=2,
        failure_summary="controller crashed",
    )
    assert report["run_status"] == "error"
    assert report["evaluator_version"] == EVALUATOR_VERSION
    assert any("exited with code 2" in reason for reason in report["reasons"])


# ---------------------------------------------------------------------------
# Database + API surface
# ---------------------------------------------------------------------------


def test_run_test_results_round_trip(tmp_path):
    store = Store(tmp_path / "t.db")
    project = store.create_project("p", "task")
    run = store.create_run(
        project["id"], parent_run_id=None, controller_version=0,
        status="running", world_config=None, seed=0, backend=BACKEND_MUJOCO,
    )
    assert store.list_test_results(run["id"]) == []
    store.save_test_results(
        run["id"],
        [
            {
                "id": "shoulder_to_0.50rad", "kind": "joint_move",
                "joint": "shoulder", "target": 0.5, "tolerance": 0.05,
                "final_position": 0.49, "final_error": 0.01, "max_error": 0.4,
                "max_velocity": 1.2, "max_effort": 2.0, "settling_time": 0.3,
                "duration": 0.6, "contacts": 0, "max_contact_force": 0.0,
                "passed": True, "reason": "ok",
            }
        ],
    )
    stored = store.list_test_results(run["id"])
    assert len(stored) == 1
    assert stored[0]["passed"] is True
    assert stored[0]["id"] == "shoulder_to_0.50rad"
    # Replacing the evidence replaces the rows rather than appending.
    store.save_test_results(run["id"], [])
    assert store.list_test_results(run["id"]) == []


def test_run_out_accepts_mujoco_payload():
    run = RunOut(
        id="run_1",
        project_id="proj",
        parent_run_id=None,
        controller_version=1,
        status="succeeded",
        backend=BACKEND_MUJOCO,
        world_config={"backend": BACKEND_MUJOCO},
        seed=0,
        started_at=None,
        finished_at=None,
        exit_code=0,
        failure_summary=None,
        tests=[
            {
                "id": "estop", "kind": "estop", "joint": "shoulder",
                "target": 0.25, "tolerance": 0.05, "final_position": 0.2,
                "final_error": 0.0, "max_error": 0.0, "max_velocity": 0.1,
                "max_effort": 0.0, "duration": 0.02, "contacts": 0,
                "max_contact_force": 0.0, "passed": True, "reason": "stopped",
                "estop_latency": 0.02,
            }
        ],
    )
    assert run.tests[0].estop_latency == pytest.approx(0.02)
    assert run.backend == BACKEND_MUJOCO


def test_run_out_defaults_keep_legacy_payloads_valid():
    run = RunOut(
        id="run_1", project_id="proj", parent_run_id=None,
        controller_version=1, status="succeeded", world_config=None, seed=0,
        started_at=None, finished_at=None, exit_code=0, failure_summary=None,
    )
    assert run.backend == BACKEND_DIFF_DRIVE
    assert run.tests == []


def test_tests_endpoint_returns_objective_evidence(client, project_id):
    store = Store(client.app.state.store.db_path)
    project = store.get_project(project_id)
    run = store.create_run(
        project["id"], parent_run_id=None, controller_version=0,
        status="failed", world_config=None, seed=0, backend=BACKEND_MUJOCO,
    )
    store.save_test_results(
        run["id"],
        [{
            "id": "shoulder_to_0.50rad", "kind": "joint_move", "joint": "shoulder",
            "target": 0.5, "tolerance": 0.05, "final_position": 0.1,
            "final_error": 0.4, "max_error": 0.4, "max_velocity": 0.0,
            "max_effort": 0.0, "duration": 5.0, "contacts": 0,
            "max_contact_force": 0.0, "passed": False,
            "reason": "did not reach",
        }],
    )
    response = client.get(f"/api/runs/{run['id']}/tests")
    assert response.status_code == 200
    body = response.json()
    assert body[0]["passed"] is False
    assert body[0]["joint"] == "shoulder"
    assert client.get(f"/api/runs/{run['id']}").json()["tests"][0]["id"] == (
        "shoulder_to_0.50rad"
    )
    assert client.get("/api/runs/missing/tests").status_code == 404


def test_comparison_reports_backend_and_test_counts(client, project_id):
    store = Store(client.app.state.store.db_path)
    baseline = store.create_run(
        project_id, parent_run_id=None, controller_version=0,
        status="failed", world_config=None, seed=0, backend=BACKEND_MUJOCO,
    )
    store.save_test_results(
        baseline["id"],
        [{"id": "a", "kind": "joint_move", "joint": "shoulder", "target": 0.5,
          "tolerance": 0.05, "final_position": 0.0, "final_error": 0.5,
          "max_error": 0.5, "max_velocity": 0.0, "max_effort": 0.0,
          "duration": 5.0, "contacts": 0, "max_contact_force": 0.0,
          "passed": False, "reason": "nope"}],
    )
    current = store.create_run(
        project_id, parent_run_id=baseline["id"], controller_version=1,
        status="succeeded", world_config=None, seed=0, backend=BACKEND_MUJOCO,
    )
    body = client.get(f"/api/runs/{current['id']}/comparison").json()
    assert body["baseline"]["backend"] == BACKEND_MUJOCO
    assert body["baseline"]["tests_total"] == 1
    assert body["baseline"]["tests_passed"] == 0


# ---------------------------------------------------------------------------
# End-to-end: harness + execution manager
# ---------------------------------------------------------------------------


def _settings(tmp_path, **overrides):
    fields = {
        "db_path": str(tmp_path / "t.db"),
        "model_api_key": "",
        "meta_api_base_url": "https://api.meta.ai/v1",
        "muse_model": "muse-spark-1.3",
        "model_timeout_s": 5.0,
        "cors_origins": ("*",),
    }
    fields.update(overrides)
    return Settings(**fields)


def _drive_until_terminal(manager, run_id, attempts=800):
    async def poll():
        for _ in range(attempts):
            current = manager.get_run(run_id)
            if is_terminal(current["status"]):
                return current
            await asyncio.sleep(0.05)
        raise AssertionError("run did not reach a terminal state")

    return poll()


def _run_mujoco_project(tmp_path, controller_source, world=None):
    async def scenario():
        settings = _settings(tmp_path)
        store = Store(settings.db_path)
        project = store.create_project("p", "move the joints")
        store.save_file(project["id"], "controller.py", controller_source)
        store.save_file(project["id"], "robot.xml", ROBOT_XML)
        manager = ExecutionManager(
            store, bus=RunEventBus(), runner=LocalSubprocessRunner(settings)
        )
        run = await manager.start_run(
            project["id"], world_config=world or {"estop_after_s": 0.5}, seed=0
        )
        final = await _drive_until_terminal(manager, run["id"])
        await manager.shutdown()
        return store, final

    return asyncio.run(scenario())


def test_start_run_stores_the_resolved_joint_test_suite(tmp_path):
    """The stored scenario carries the derived suite so the UI can show the
    plan up front and a rerun repeats exactly the same tests (AGENTS.md §9)."""

    async def scenario():
        settings = _settings(tmp_path)
        store = Store(settings.db_path)
        project = store.create_project("p", "move the joints")
        store.save_file(project["id"], "controller.py", CONTROLLER)
        store.save_file(project["id"], "robot.xml", ROBOT_XML)
        manager = ExecutionManager(store, bus=RunEventBus(), runner=LocalSubprocessRunner(settings))
        run = await manager.start_run(project["id"], world_config={}, seed=0)
        rerun = await manager.rerun_run(run["id"])
        await manager.shutdown()
        return run, rerun

    run, rerun = asyncio.run(scenario())
    suite = run["world_config"]["tests"]
    assert [test["joint"] for test in suite] == ["shoulder", "elbow"]
    assert all(test["kind"] == "joint_move" for test in suite)
    assert rerun["world_config"]["tests"] == suite


def test_start_run_without_a_robot_model_is_rejected_with_a_fix_path(tmp_path):
    """A missing robot description is a precondition, not a run failure.

    Starting the run would only fail inside the harness with a terse log line;
    the manager refuses up front and points at the two ways forward (generate,
    or install a starter robot) plus the legacy backend.
    """

    async def scenario():
        settings = _settings(tmp_path)
        store = Store(settings.db_path)
        project = store.create_project("p", "task")
        store.save_file(project["id"], "controller.py", CONTROLLER)
        manager = ExecutionManager(store, bus=RunEventBus(), runner=LocalSubprocessRunner(settings))
        try:
            await manager.start_run(project["id"], world_config={}, seed=0)
        finally:
            await manager.shutdown()

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(scenario())
    message = str(excinfo.value)
    assert "no simulatable robot description" in message
    assert "GET /api/robots" in message
    assert "2D differential-drive" in message


def test_installing_a_starter_robot_makes_a_project_runnable(tmp_path):
    """The recovery path: install a bundled robot, then the run just works."""

    async def scenario():
        settings = _settings(tmp_path)
        store = Store(settings.db_path)
        project = store.create_project("p", "task")
        store.save_file(project["id"], "controller.py", CONTROLLER)
        manager = ExecutionManager(
            store, bus=RunEventBus(), runner=LocalSubprocessRunner(settings)
        )
        package = install_starter_robot(store, project["id"], "arm")
        run = await manager.start_run(project["id"], world_config=None, seed=0)
        final = await _drive_until_terminal(manager, run["id"])
        await manager.shutdown()
        return package, run, final

    package, run, final = asyncio.run(scenario())
    assert [joint.name for joint in package.joints] == ["shoulder", "elbow"]
    assert len(package.tests) == 2
    assert run["backend"] == "mujoco"
    assert final["status"] == "succeeded", final["failure_summary"]
    assert final["metrics"]["tests_total"] == 2
    assert final["metrics"]["tests_passed"] == 2


def test_microduck_starter_robot_reports_honest_fall_evidence(tmp_path):
    """Free-standing runs (fix_base=false) still report honest fall physics.

    Joint tests default to the welded test stand so tracking is gradeable;
    this pins the opt-out: single-joint excursions topple a real
    free-standing biped, so the run fails with structured fall evidence
    instead of passing every test. Foot-sole touches stay expected (never
    disqualifying contacts); the verdict comes from real physics.
    """

    async def scenario():
        settings = _settings(tmp_path)
        store = Store(settings.db_path)
        project = store.create_project("p", "calibrate the legs")
        install_starter_robot(store, project["id"], "microduck")
        manager = ExecutionManager(
            store, bus=RunEventBus(), runner=LocalSubprocessRunner(settings)
        )
        run = await manager.start_run(
            project["id"], world_config={"fix_base": False}, seed=0
        )
        final = await _drive_until_terminal(manager, run["id"])
        tests = store.list_test_results(run["id"])
        await manager.shutdown()
        return store, final, tests

    store, final, tests = asyncio.run(scenario())
    assert final["status"] == "failed", final["failure_summary"]
    assert len(tests) == 14
    assert {test["joint"] for test in tests} == {
        "left_hip_yaw", "left_hip_roll", "left_hip_pitch", "left_knee",
        "left_ankle", "right_hip_yaw", "right_hip_roll", "right_hip_pitch",
        "right_knee", "right_ankle", "neck_pitch", "head_yaw",
        "head_pitch", "head_roll",
    }
    assert any("fell" in test["reason"] for test in tests)
    telemetry = store.list_telemetry(final["id"])
    from app.simulator.microduck import ENVIRONMENT

    assert telemetry and len(telemetry[-1]["bodies"]) == 15 + len(ENVIRONMENT)
    assert "imu_accel" in telemetry[-1]["sensors"]
    # Foot-sole touches are recorded as expected ground contact (falling body
    # parts may still count as disqualifying contacts — that is the physics).
    assert any(sample.get("expected_contacts", 0) > 0 for sample in telemetry)
    # A 14-case suite must not be cut short by the default 30 s budget.
    assert final["world_config"]["time_limit"] >= 30.0
    assert final["world_config"]["time_limit"] >= 6.0 * len(tests)


def test_every_starter_robot_is_a_validated_robot_package():
    for robot in starter_robots():
        summary = starter_robot_summary(robot)
        assert summary["joints"], robot.id
        assert summary["tests"], robot.id
        assert summary["license"], robot.id
        for joint in summary["joints"]:
            assert joint["actuator"], f"{robot.id}/{joint['name']}"
            assert joint["lower"] is not None and joint["upper"] is not None


def test_mujoco_run_succeeds_end_to_end(tmp_path):
    store, final = _run_mujoco_project(tmp_path, CONTROLLER)
    assert final["status"] == "succeeded", final["failure_summary"]
    assert final["backend"] == BACKEND_MUJOCO
    assert final["metrics"]["success"] is True
    assert final["metrics"]["tests_total"] == 3
    assert final["metrics"]["tests_passed"] == 3
    assert final["evaluation"]["evaluator_version"] == EVALUATOR_VERSION
    assert len(final["tests"]) == 3
    assert all(test["passed"] for test in final["tests"])
    assert len(store.list_telemetry(final["id"])) > 5
    joints = store.list_telemetry(final["id"])[-1]["joints"]
    assert [joint["name"] for joint in joints] == ["shoulder", "elbow"]


BROKEN_CONTROLLER = '''\
"""Publishes valid-length commands but never tracks the target."""

from rclpy.node import Node
from std_msgs.msg import Float64MultiArray


class BrokenController(Node):
    def __init__(self) -> None:
        super().__init__("broken_joint_controller")
        self._commands = self.create_publisher(
            Float64MultiArray, "/joint_commands", 10
        )
        self.create_timer(0.02, self._tick)

    def _tick(self) -> None:
        message = Float64MultiArray()
        message.data = [0.0, 0.0]
        self._commands.publish(message)
'''


def test_mujoco_run_reports_honest_failure_with_evidence(tmp_path):
    store, final = _run_mujoco_project(
        tmp_path, BROKEN_CONTROLLER, world={"time_limit": 12.0}
    )
    assert final["status"] == "failed"
    assert final["metrics"]["success"] is False
    assert final["metrics"]["tests_passed"] < final["metrics"]["tests_total"]
    assert final["tests"]
    failed = [test for test in final["tests"] if not test["passed"]]
    assert failed and "did not reach" in failed[0]["reason"]
    assert any("failed" in reason for reason in final["evaluation"]["reasons"])
    assert final["failure_summary"]


def test_execution_manager_records_tests_when_result_carries_them(tmp_path):
    """The manager stores runner test evidence even for non-MuJoCo verdicts."""

    class _FakeRunner:
        name = "fake"

        async def execute(self, ctx):
            return RunnerResult(
                exit_code=0,
                tests=[
                    {"id": "a", "kind": "joint_move", "joint": "shoulder",
                     "target": 0.5, "tolerance": 0.05, "final_position": 0.5,
                     "final_error": 0.0, "max_error": 0.4, "max_velocity": 0.0,
                     "max_effort": 0.0, "duration": 0.5, "contacts": 0,
                     "max_contact_force": 0.0, "passed": True, "reason": "ok"}
                ],
                suite_size=1,
            )

    async def scenario():
        store = Store(tmp_path / "fake.db")
        project = store.create_project("p", "task")
        store.save_file(project["id"], "controller.py", CONTROLLER)
        store.save_file(project["id"], "robot.xml", ROBOT_XML)
        manager = ExecutionManager(store, bus=RunEventBus(), runner=_FakeRunner())
        run = await manager.start_run(
            project["id"], world_config={"backend": BACKEND_MUJOCO}, seed=0
        )
        final = await _drive_until_terminal(manager, run["id"])
        await manager.shutdown()
        return final

    final = asyncio.run(scenario())
    # Test evidence is persisted for every outcome, whatever the verdict.
    assert final["tests"][0]["id"] == "a"
    assert len(final["tests"]) == 1


# ---------------------------------------------------------------------------
# Generation / correction integration
# ---------------------------------------------------------------------------


def test_handle_generate_writes_and_synthesizes_robot_package(tmp_path):
    from app.services import generation as generation_service

    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    project = store.create_project("p", "move the joints")
    result = asyncio.run(
        generation_service.handle_generate(
            store, settings, project["id"], None,
            client=meta_model.DevMockMetaModelClient(),
        )
    )
    paths = {entry["path"] for entry in store.list_files(project["id"])}
    assert {"controller.py", "robot.xml", "robot.urdf", "ros2_control.yaml"} <= paths
    assert result.robot is not None
    assert [joint.name for joint in result.robot.joints] == ["shoulder", "elbow"]
    assert [test.joint for test in result.robot.tests] == ["shoulder", "elbow"]
    assert any("synthesized" in note for note in result.robot.notes)


def test_handle_generate_rejects_unusable_robot_model(tmp_path):
    from app.models.schemas import GeneratedFile, GenerationResult
    from app.services import generation as generation_service
    from app.services.meta_model import MetaModelResponseError

    class _BadRobotClient:
        model_name = "test-bad-robot"

        async def generate_controller(self, context):
            return GenerationResult(
                summary="s", assumptions=[],
                files=[
                    GeneratedFile(path="controller.py", content="# ok"),
                    GeneratedFile(
                        path="robot.xml",
                        content="<mujoco><worldbody><body><joint name='j'"
                        " type='ball'/></body></worldbody></mujoco>",
                    ),
                ],
                expected_behavior="", safety_considerations="",
            )

    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    project = store.create_project("p", "task")
    with pytest.raises(MetaModelResponseError, match="not simulatable"):
        asyncio.run(
            generation_service.handle_generate(
                store, settings, project["id"], None,
                client=_BadRobotClient(),
            )
        )
    # Nothing is written when the robot package is rejected.
    assert store.list_files(project["id"]) == []


def test_handle_generate_persists_the_declared_objective(tmp_path):
    """The objective travels with the controller for later Starts."""
    from app.models.schemas import (
        GeneratedFile,
        GenerationResult,
        RunObjectiveOut,
    )
    from app.services import generation as generation_service

    class _KickClient:
        model_name = "test-kick"

        async def generate_controller(self, context):
            return GenerationResult(
                summary="s", assumptions=[],
                files=[GeneratedFile(path="controller.py", content="# kick")],
                expected_behavior="", safety_considerations="",
                objectives=[RunObjectiveOut(type="kick")],
            )

    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    project = store.create_project("p", "Kick the ball away from you, then stop.")
    install_starter_robot(store, project["id"], "arm")
    result = asyncio.run(
        generation_service.handle_generate(
            store, settings, project["id"], None, client=_KickClient()
        )
    )
    assert result.objectives is not None
    assert [o.type for o in result.objectives] == ["kick"]
    assert store.get_project_objectives(project["id"]) == [
        {"type": "kick", "target": None, "target_body": None},
    ]


def test_import_sample_controller_clears_a_declared_objective(tmp_path):
    from app.services import generation as generation_service

    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    project = store.create_project("p", "task")
    store.set_project_objectives(
        project["id"], [{"type": "kick", "target": None, "target_body": None}]
    )
    saved = generation_service.import_sample_controller(
        store, project["id"], "microduck"
    )
    assert saved["path"] == "controller.py"
    assert "starter" in saved["content"].lower()
    assert store.get_project_objectives(project["id"]) is None


def test_start_run_grades_the_project_objective_without_an_override(tmp_path):
    """Start with no explicit objective uses the controller's own."""

    async def scenario():
        settings = _settings(tmp_path)
        store = Store(settings.db_path)
        project = store.create_project("p", "Kick the ball.")
        store.save_file(project["id"], "controller.py", "# kick")
        store.save_file(project["id"], "robot.xml", ROBOT_XML)
        store.set_project_objectives(
            project["id"], [{"type": "kick", "target": None, "target_body": None}]
        )
        manager = ExecutionManager(
            store, bus=RunEventBus(), runner=LocalSubprocessRunner(settings)
        )
        run = await manager.start_run(project["id"], world_config={}, seed=0)
        explicit = await manager.start_run(
            project["id"],
            world_config={"objective": {"type": "wander"}},
            seed=1,
        )
        await manager.shutdown()
        return run, explicit

    run, explicit = asyncio.run(scenario())
    assert run["world_config"]["objectives"] == [
        {"type": "kick", "target": None, "target_body": None},
    ]
    assert run["world_config"]["objective"]["type"] == "kick"
    assert explicit["world_config"]["objective"]["type"] == "wander"


def test_fix_evidence_includes_test_results(tmp_path):
    from app.services import generation as generation_service
    from app.services.meta_model import FixEvidence

    captured = {}

    class _CapturingClient:
        model_name = "test-capture"

        async def propose_fix(self, evidence):
            captured["evidence"] = evidence
            from app.models.schemas import FixResult, GeneratedFile

            return FixResult(
                failure_hypothesis="h", evidence=[], proposed_change="c",
                updated_files=[GeneratedFile(path="controller.py", content="# new")],
                expected_improvement="",
            )

    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    project = store.create_project("p", "task")
    store.save_file(project["id"], "controller.py", CONTROLLER)
    store.save_file(project["id"], "robot.xml", ROBOT_XML)
    run = store.create_run(
        project["id"], parent_run_id=None, controller_version=1,
        status="failed", world_config=None, seed=0, backend=BACKEND_MUJOCO,
    )
    store.save_test_results(
        run["id"],
        [{"id": "a", "kind": "joint_move", "joint": "shoulder", "target": 0.5,
          "tolerance": 0.05, "final_position": 0.0, "final_error": 0.5,
          "max_error": 0.5, "max_velocity": 0.0, "max_effort": 0.0,
          "duration": 5.0, "contacts": 0, "max_contact_force": 0.0,
          "passed": False, "reason": "did not reach"}],
    )
    asyncio.run(
        generation_service.handle_fix(
            store, settings, project["id"], run["id"],
            client=_CapturingClient(),
        )
    )
    evidence = captured["evidence"]
    assert isinstance(evidence, FixEvidence)
    assert evidence.backend == BACKEND_MUJOCO
    assert evidence.test_results and evidence.test_results[0]["joint"] == "shoulder"
    prompt = meta_model.build_fix_messages(evidence)[1]["content"]
    assert "automated_test_results" in prompt
    assert "did not reach" in prompt
    # The correction is re-validated as a complete robot package.
    assert store.get_file(project["id"], "controller.py")["content"] == "# new"
    assert store.get_file(project["id"], "robot.urdf") is not None


def test_fix_rejects_correction_that_breaks_the_robot_package(tmp_path):
    from app.models.schemas import FixResult, GeneratedFile
    from app.services import generation as generation_service
    from app.services.meta_model import MetaModelResponseError

    class _BreakingClient:
        model_name = "test-breaking"

        async def propose_fix(self, evidence):
            return FixResult(
                failure_hypothesis="h", evidence=[], proposed_change="c",
                updated_files=[
                    GeneratedFile(
                        path="robot.xml",
                        content="<mujoco><worldbody><body><joint name='j'"
                        " type='free'/></body></worldbody></mujoco>",
                    )
                ],
                expected_improvement="",
            )

    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    project = store.create_project("p", "task")
    store.save_file(project["id"], "controller.py", CONTROLLER)
    store.save_file(project["id"], "robot.xml", ROBOT_XML)
    run = store.create_run(
        project["id"], parent_run_id=None, controller_version=1,
        status="failed", world_config=None, seed=0, backend=BACKEND_MUJOCO,
    )
    with pytest.raises(MetaModelResponseError, match="not simulatable"):
        asyncio.run(
            generation_service.handle_fix(
                store, settings, project["id"], run["id"],
                client=_BreakingClient(),
            )
        )
    # The previous robot description survives a rejected correction.
    assert store.get_file(project["id"], "robot.xml")["content"] == ROBOT_XML


def test_generation_prompt_requires_robot_description():
    from app.services.meta_model import (
        GenerationContext,
        build_generate_messages,
    )

    messages = build_generate_messages(
        GenerationContext("move the arm", [{"path": "controller.py", "content": ""}])
    )
    user = messages[1]["content"]
    assert "robot.xml" in user
    assert "robot.urdf" in user
    assert "ros2_control.yaml" in user
    assert "/joint_commands" in user and "/joint_targets" in user
    assert "/emergency_stop" in user
    assert "/cmd_vel" in user  # legacy 2D contract stays documented


def test_scenario_defaults_are_json_serializable():
    scenario = MujocoScenario.from_dict(
        {"tests": [{"id": "t", "joint": "shoulder", "target": 0.2}]}
    )
    payload = json.loads(json.dumps(scenario.to_dict()))
    assert payload["tests"][0]["joint"] == "shoulder"


def test_fix_then_rerun_closes_the_loop(tmp_path):
    """Failed MuJoCo run -> AI correction -> rerun in the same scenario."""
    from app.services import generation as generation_service

    async def scenario():
        settings = _settings(tmp_path)
        store = Store(settings.db_path)
        project = store.create_project("p", "track the joint targets")
        store.save_file(project["id"], "controller.py", BROKEN_CONTROLLER)
        store.save_file(project["id"], "robot.xml", ROBOT_XML)
        manager = ExecutionManager(
            store, bus=RunEventBus(), runner=LocalSubprocessRunner(settings)
        )
        first = await manager.start_run(
            project["id"], world_config={"time_limit": 12.0}, seed=0
        )
        failed = await _drive_until_terminal(manager, first["id"])
        fix = await generation_service.handle_fix(
            store, settings, project["id"], failed["id"],
            client=meta_model.DevMockMetaModelClient(),
        )
        rerun = await manager.rerun_run(failed["id"])
        final = await _drive_until_terminal(manager, rerun["id"])
        await manager.shutdown()
        return store, failed, fix, final

    store, failed, fix, final = asyncio.run(scenario())
    assert failed["status"] == "failed"
    assert failed["metrics"]["tests_passed"] < failed["metrics"]["tests_total"]
    assert fix.robot is not None and len(fix.robot.tests) == 2
    # The rerun reuses the same scenario configuration and records its parent.
    assert final["parent_run_id"] == failed["id"]
    assert final["world_config"]["time_limit"] == 12.0
    assert final["seed"] == failed["seed"]
    assert final["status"] == "succeeded", final["failure_summary"]
    assert final["metrics"]["tests_passed"] == final["metrics"]["tests_total"]
    assert final["controller_version"] > failed["controller_version"]
    # Both runs keep their evidence for comparison.
    assert store.list_test_results(failed["id"])
    assert store.list_test_results(final["id"])


# ---- blocking-call guards ----


def test_reject_blocking_calls_flags_sleep_variants():
    from app.services.experiments import (
        BlockingControllerError,
        reject_blocking_calls,
    )

    with pytest.raises(BlockingControllerError, match="controller.py:2"):
        reject_blocking_calls([
            ("controller.py", "import time\ntime.sleep(0.1)\n"),
        ])
    with pytest.raises(BlockingControllerError, match="sleep"):
        reject_blocking_calls([
            ("controller.py", "from time import sleep\nsleep(1)\n"),
        ])
    with pytest.raises(BlockingControllerError, match="sleep"):
        reject_blocking_calls([
            ("controller.py", "import asyncio\nasyncio.sleep(0)\n"),
        ])
    # Mentions in comments/docstrings are not calls; other files are out
    # of scope; broken files fail later with their own precise error.
    reject_blocking_calls([
        ("controller.py", "# never call sleep( here\nVALUE = 1\n"),
        ("controller.py", '"""Use the timer, not sleep(), for pacing."""\n'),
        ("notes.txt", "time.sleep(1)\n"),
        ("controller.py", "def broken(:\n"),
    ])


def test_handle_generate_repairs_sleepy_controller(tmp_path):
    import asyncio

    from app.models.schemas import GeneratedFile, GenerationResult
    from app.services import generation as generation_service

    calls = []

    class _SleepyThenCleanClient:
        model_name = "test-sleepy"

        async def generate_controller(self, context):
            calls.append(context.task_description)
            sleepy = "REJECTED" not in context.task_description
            return GenerationResult(
                summary="s", assumptions=[],
                files=[
                    GeneratedFile(
                        path="controller.py",
                        content=(
                            "import time\ntime.sleep(0.5)\n"
                            if sleepy else "# clean controller\n"
                        ),
                    ),
                    GeneratedFile(path="robot.xml", content=ROBOT_XML),
                ],
                expected_behavior="", safety_considerations="",
            )

    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    project = store.create_project("p", "task")
    result = asyncio.run(
        generation_service.handle_generate(
            store, settings, project["id"], None,
            client=_SleepyThenCleanClient(),
        )
    )
    assert len(calls) == 2
    assert "REJECTED" in calls[1]
    assert any("repair_blocking_calls" in step.stage for step in result.trace)
    stored = store.get_file(project["id"], "controller.py")
    assert stored is not None and "sleep" not in stored["content"]


def test_handle_generate_rejects_persistently_sleepy(tmp_path):
    import asyncio

    from app.models.schemas import GeneratedFile, GenerationResult
    from app.services import generation as generation_service
    from app.services.experiments import BlockingControllerError

    calls = []

    class _AlwaysSleepyClient:
        model_name = "test-always-sleepy"

        async def generate_controller(self, context):
            calls.append(context.task_description)
            return GenerationResult(
                summary="s", assumptions=[],
                files=[
                    GeneratedFile(
                        path="controller.py",
                        content="import time\ntime.sleep(0.5)\n",
                    ),
                    GeneratedFile(path="robot.xml", content=ROBOT_XML),
                ],
                expected_behavior="", safety_considerations="",
            )

    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    project = store.create_project("p", "task")
    with pytest.raises(BlockingControllerError, match="sleep"):
        asyncio.run(
            generation_service.handle_generate(
                store, settings, project["id"], None,
                client=_AlwaysSleepyClient(),
            )
        )
    assert len(calls) == 2
    assert store.list_files(project["id"]) == []


def test_tick_breakdown_names_the_slow_phase_in_stall_warnings(tmp_path):
    """Each tick records per-phase seconds; stalls report the breakdown."""
    from app.runner.harness import stall_warning
    from app.runner.mujoco_loop import MujocoLoop, format_tick_breakdown

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "robot.xml").write_text(ROBOT_XML)
    loop = MujocoLoop(
        workspace, tmp_path / "out", MujocoScenario.from_dict({})
    )
    try:
        loop.tick()
        phases = loop.last_tick_phases
        assert set(phases) == {"policy", "physics", "runner", "sample", "write"}
        assert all(value >= 0.0 for value in phases.values())
        detail = format_tick_breakdown(phases, pub_s=0.001)
        for phase in ("policy", "physics", "runner", "sample", "write", "pub"):
            assert phase in detail
        note = stall_warning(0.61, 0.02, 0, 0.57, breakdown=detail)
        assert note is not None
        assert "tick body itself" in note
        assert detail in note
        # Without a breakdown the legacy guess text stays intact.
        legacy = stall_warning(0.61, 0.02, 0, 0.57)
        assert legacy is not None and "(physics/policy slow?)" in legacy
    finally:
        loop.close()
