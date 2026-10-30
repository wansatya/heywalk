"""Official MicroDuck model tests: loading, physics, control, evidence.

Covers the vendored Pollen Robotics model (see
``app/simulator/microduck/ATTRIBUTION.md``): asset resolution, joint /
actuator / sensor discovery, MuJoCo stepping with gravity and ground
contact, command effects, STAND reset, invalid-command rejection, fall
detection, and renderer/telemetry synchronization. Nothing here executes
model output inside the API process: the harness runs as a child process.

No test claims walking or balancing works: the suite proves a static
position hold topples (fall evidence), which is the honest starting point.
"""

from __future__ import annotations

import asyncio
import math
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Store
from app.runner import topics
from app.runner.local_runner import LocalSubprocessRunner
from app.runner.staging import prepare_workspace
from app.services.execution import ExecutionManager, RunEventBus, is_terminal
from app.services.generation import install_starter_robot
from app.services.meta_model import target_robot_brief
from app.simulator import microduck
from app.simulator.microduck import (
    EXPECTED_GROUND_GEOMS,
    FALL_TRUNK_Z,
    OFFICIAL_CONTROL_HZ,
    OFFICIAL_KEYFRAME,
    OFFICIAL_TIMESTEP,
    MicroduckModelError,
    body_hierarchy,
    check_assets,
    compose_scene_xml,
    is_composed_scene,
    load_metadata,
    missing_assets,
    stage_assets,
)
from app.simulator.mujoco_engine import MujocoEngine, MujocoEngineError
from app.simulator.mujoco_tests import JointTestRunner
from app.simulator.robot_adapter import check_adapter
from app.simulator.robot_catalog import (
    MICRODUCK_SCENARIO_DEFAULTS,
    get_starter_robot,
    starter_robot_summary,
)
from app.simulator.robot_description import parse_mjcf
from app.simulator.scenario import MujocoScenario

EXPECTED_JOINTS = (
    "left_hip_yaw",
    "left_hip_roll",
    "left_hip_pitch",
    "left_knee",
    "left_ankle",
    "neck_pitch",
    "head_pitch",
    "head_yaw",
    "head_roll",
    "right_hip_yaw",
    "right_hip_roll",
    "right_hip_pitch",
    "right_knee",
    "right_ankle",
)

EXPECTED_SENSORS = (
    "orientation",
    "angular-velocity",
    "imu_ang_vel",
    "imu_lin_vel",
    "imu_accel",
    "root_angmom",
)

MICRODUCK_WORLD = {
    "dt": OFFICIAL_TIMESTEP,
    "control_hz": OFFICIAL_CONTROL_HZ,
    "keyframe": OFFICIAL_KEYFRAME,
    "integrator": "Euler",
    "expected_contact_geoms": list(EXPECTED_GROUND_GEOMS),
    "fall_trunk_z": FALL_TRUNK_Z,
    "include_body_poses": True,
}


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / f"ws_{len(list(tmp_path.glob('ws_*')))}"
    workspace.mkdir()
    (workspace / "robot.xml").write_text(compose_scene_xml())
    stage_assets(workspace)
    return workspace


def _engine(tmp_path: Path, **overrides) -> MujocoEngine:
    workspace = _workspace(tmp_path)
    world = dict(MICRODUCK_WORLD)
    world.update(overrides)
    scenario = MujocoScenario.from_dict(world)
    info = parse_mjcf((workspace / "robot.xml").read_text(), "robot.xml")
    return MujocoEngine(workspace / "robot.xml", info, scenario=scenario)


# ---------------------------------------------------------------------------
# Model loading + asset resolution + metadata discovery
# ---------------------------------------------------------------------------


def test_official_metadata_is_discovered_from_the_model():
    metadata = load_metadata()
    assert metadata.joints == EXPECTED_JOINTS
    assert metadata.actuators == EXPECTED_JOINTS
    assert metadata.sensors == EXPECTED_SENSORS
    assert len(metadata.mesh_files) == 38
    assert set(metadata.keyframes) >= {"INIT", "STAND"}
    assert metadata.timestep == OFFICIAL_TIMESTEP
    assert metadata.control_hz == OFFICIAL_CONTROL_HZ
    assert "microduck_rl" in metadata.source_url
    # Ranges come from the file, not from a hardcoded table.
    assert metadata.joint_ranges["left_hip_yaw"] == (
        pytest.approx(-0.43633231),
        pytest.approx(0.52359878),
    )


def test_all_mesh_assets_resolve():
    assert missing_assets() == ()
    check_assets()  # raises with an actionable message when files are missing


def test_missing_assets_raise_an_actionable_error(monkeypatch):
    monkeypatch.setattr(microduck.model, "MODEL_DIR", Path("/nonexistent"))
    with pytest.raises(MicroduckModelError, match="missing|unreadable"):
        check_assets()


def test_composed_scene_preserves_the_official_robot_subtree():
    """The composition adds only floor + environment + keyframes."""
    official = ET.parse(microduck.model.MODEL_DIR / "robot_groundcontact.xml")
    composed = ET.fromstring(compose_scene_xml())

    def canonical(element: ET.Element) -> str:
        for node in element.iter():
            node.tail = None
            node.text = (node.text or "").strip() or None
        return ET.tostring(element, encoding="unicode")

    official_bodies = canonical(official.getroot().find("worldbody"))
    composed_world = composed.find("worldbody")
    assert composed_world is not None
    # Strip the added floor geom and environment bodies, then the robot
    # subtrees must match exactly.
    for child in list(composed_world):
        if child.tag == "geom" and child.get("name") == "floor":
            composed_world.remove(child)
        if (
            child.tag == "body"
            and (child.get("name") or "").startswith(
                microduck.model.ENV_BODY_PREFIX
            )
        ):
            composed_world.remove(child)
    assert canonical(composed_world) == official_bodies
    for section in ("actuator", "sensor", "asset"):
        assert canonical(composed.find(section)) == canonical(
            official.getroot().find(section)
        )
    assert composed.find("keyframe") is not None
    assert is_composed_scene(compose_scene_xml())
    assert not is_composed_scene("<mujoco><worldbody/></mujoco>")


def test_composed_scene_walls_bound_the_arena():
    """Four fixed walls with inner faces exactly on the ±2 m bounds."""
    composed = ET.fromstring(compose_scene_xml())
    world = composed.find("worldbody")
    assert world is not None
    bodies = {
        child.get("name"): child
        for child in list(world)
        if child.tag == "body"
    }
    for name in microduck.model.WALL_BODIES:
        body = bodies[name]
        assert body.findall("joint") == []
        pos = [float(v) for v in body.get("pos", "").split()]
        geom = body.find("geom")
        assert geom is not None
        size = [float(v) for v in geom.get("size", "").split()]
        if name.endswith("_px"):
            assert pos[0] - size[0] == pytest.approx(2.0)
            # Butt into the y wall's inner face: no overlap or excess.
            assert size[1] == pytest.approx(2.0)
        elif name.endswith("_nx"):
            assert pos[0] + size[0] == pytest.approx(-2.0)
            assert size[1] == pytest.approx(2.0)
        elif name.endswith("_py"):
            assert pos[1] - size[1] == pytest.approx(2.0)
            # Span the corner out to the perpendicular wall's outer face.
            assert size[0] == pytest.approx(2.0 + 2 * size[1])
        elif name.endswith("_ny"):
            assert pos[1] + size[1] == pytest.approx(-2.0)
            assert size[0] == pytest.approx(2.0 + 2 * size[1])


def test_composed_scene_carries_the_simulator_environment():
    """Crates, a ball, and a goal flag — plain env_* bodies, one free ball."""
    composed = ET.fromstring(compose_scene_xml())
    world = composed.find("worldbody")
    assert world is not None
    env = [
        child
        for child in list(world)
        if child.tag == "body"
        and (child.get("name") or "").startswith(
            microduck.model.ENV_BODY_PREFIX
        )
    ]
    assert [body.get("name") for body in env] == [
        body.name for body in microduck.model.ENVIRONMENT
    ]
    dynamic = []
    for body in env:
        # Hinges stay off the props: the official keyframes describe the
        # robot only. The ball is the ONE free (dynamic) prop, so a kick
        # really rolls it; everything else is static.
        assert body.findall("joint") == []
        free = body.findall("freejoint")
        assert len(free) <= 1
        if free:
            dynamic.append(body.get("name"))
        assert len(body.findall("geom")) >= 1
    assert dynamic == [microduck.model.BALL_BODY]


def test_body_hierarchy_covers_every_body_once():
    links = body_hierarchy()
    names = [link["body"] for link in links]
    assert len(names) == len(set(names)) == 15
    assert "trunk_base" in names
    parents = {link["body"]: link["parent"] for link in links}
    assert parents["trunk_base"] is None


# ---------------------------------------------------------------------------
# Physics engine on the official model
# ---------------------------------------------------------------------------


def test_engine_binds_all_official_actuators_in_order(tmp_path):
    import mujoco

    engine = _engine(tmp_path)
    check_adapter(engine)
    assert engine.joint_count == 14
    assert engine.joint_names == EXPECTED_JOINTS
    assert engine.model.opt.integrator == int(mujoco.mjtIntegrator.mjINT_EULER)
    assert engine.model.opt.timestep == pytest.approx(OFFICIAL_TIMESTEP)


def test_engine_rejects_unknown_keyframe_geom_and_integrator(tmp_path):
    with pytest.raises(MujocoEngineError, match="keyframe"):
        _engine(tmp_path, keyframe="FLY")
    with pytest.raises(MujocoEngineError, match="geom"):
        _engine(tmp_path, expected_contact_geoms=["no_such_geom"])
    with pytest.raises(MujocoEngineError, match="integrator"):
        _engine(tmp_path, integrator="warp-drive")


def test_reset_restores_the_stand_keyframe(tmp_path):
    engine = _engine(tmp_path)
    stand = engine.joint_positions()
    assert stand[2] == pytest.approx(-0.457924, abs=1e-4)
    assert engine.snapshot()["trunk_z"] == pytest.approx(0.12, abs=1e-6)
    engine.set_commands([0.0] * 14)
    engine.advance(200)
    assert engine.joint_positions() != pytest.approx(stand)
    engine.reset()
    assert engine.joint_positions() == pytest.approx(stand, abs=1e-9)
    assert engine.snapshot()["fallen"] is False
    assert engine.snapshot()["trunk_z"] == pytest.approx(0.12, abs=1e-6)


def test_engine_applies_scene_layout(tmp_path):
    """Arrange-mode spawn + prop spots become the physics start state."""
    engine = _engine(
        tmp_path,
        spawn_xy=[0.4, -0.2],
        spawn_yaw=1.5707963,
        env_layout={"env_ball": [0.0, 0.5], "env_box_1": [-0.5, 0.0]},
    )
    snapshot = engine.snapshot()
    assert snapshot["x"] == pytest.approx(0.4)
    assert snapshot["y"] == pytest.approx(-0.2)
    assert snapshot["theta"] == pytest.approx(1.5707963, abs=1e-6)
    assert snapshot["trunk_z"] == pytest.approx(0.12, abs=1e-6)
    poses = {pose["body"]: pose for pose in snapshot["bodies"]}
    assert poses["env_ball"]["pos"][0] == pytest.approx(0.0)
    assert poses["env_ball"]["pos"][1] == pytest.approx(0.5)
    assert poses["env_ball"]["pos"][2] == pytest.approx(0.035)
    assert poses["env_box_1"]["pos"][:2] == pytest.approx([-0.5, 0.0])
    assert engine.layout_skipped == ()
    # Joints still start at STAND; only the base moved.
    assert engine.joint_positions()[2] == pytest.approx(-0.457924, abs=1e-4)


def test_microduck_defaults_weld_the_base():
    assert MICRODUCK_SCENARIO_DEFAULTS["fix_base"] is True


def test_welded_stand_holds_trunk_and_tracks_joints(tmp_path):
    """On the stand the trunk never drops and hinges follow commands."""
    engine = _engine(tmp_path, fix_base=True)
    assert engine.base_fixed is True
    # 14 hinges + the ball's 7 free-joint slots: the prop stays dynamic.
    assert engine.model.nq == 21
    home = engine.joint_positions()
    target = list(home)
    target[3] = home[3] + 0.2  # left_knee excursion
    assert engine.set_commands(target) is True
    engine.advance(engine.scenario.physics_steps_per_control * 100)
    snapshot = engine.snapshot()
    assert snapshot["trunk_z"] == pytest.approx(0.12, abs=1e-6)
    assert snapshot["fallen"] is False
    assert snapshot["contacts"] == 0
    assert engine.joint_positions()[3] == pytest.approx(
        target[3], abs=0.05
    )


def test_welded_model_loads_from_a_read_only_workspace(tmp_path):
    """Isolated runners mount /workspace read-only; welding writes nothing."""
    workspace = _workspace(tmp_path)
    os.chmod(workspace, 0o555)
    try:
        world = dict(MICRODUCK_WORLD)
        world["fix_base"] = True
        scenario = MujocoScenario.from_dict(world)
        info = parse_mjcf((workspace / "robot.xml").read_text(), "robot.xml")
        engine = MujocoEngine(workspace / "robot.xml", info, scenario=scenario)
    finally:
        os.chmod(workspace, 0o755)
    assert engine.base_fixed is True
    assert not (workspace / "robot.welded.xml").exists()
    assert engine.snapshot()["trunk_z"] == pytest.approx(0.12, abs=1e-6)


def test_welded_reset_keeps_stand_keyframe(tmp_path):
    engine = _engine(tmp_path, fix_base=True)
    engine.reset()
    assert engine.joint_positions()[2] == pytest.approx(-0.457924, abs=1e-4)
    assert engine.snapshot()["trunk_z"] == pytest.approx(0.12, abs=1e-6)


def test_welded_spawn_moves_the_stand(tmp_path):
    engine = _engine(
        tmp_path, fix_base=True, spawn_xy=[0.5, -0.5], spawn_yaw=1.0
    )
    snapshot = engine.snapshot()
    assert snapshot["x"] == pytest.approx(0.5, abs=1e-6)
    assert snapshot["y"] == pytest.approx(-0.5, abs=1e-6)
    assert snapshot["theta"] == pytest.approx(1.0, abs=1e-6)
    assert snapshot["trunk_z"] == pytest.approx(0.12, abs=1e-6)


def test_weld_free_joints_welds_only_the_robot_trunk():
    from app.simulator.mujoco_engine import _weld_free_joints

    official = compose_scene_xml()
    welded, changed = _weld_free_joints(official)
    assert changed is True
    root = ET.fromstring(welded)
    world = root.find("worldbody")
    assert world is not None
    free_bodies = [
        body.get("name")
        for body in world.iter("body")
        if body.find("freejoint") is not None
    ]
    # The trunk welds to the stand; the kickable prop keeps its free joint.
    assert free_bodies == [microduck.model.BALL_BODY]
    keys = root.find("keyframe")
    assert keys is not None
    for key in keys.findall("key"):
        # 14 hinges + the ball's 7 free-joint values.
        assert len(key.get("qpos", "").split()) == 21
    # No free joint: untouched.
    plain = "<mujoco><worldbody><body><joint name='j'/></body></worldbody></mujoco>"
    assert _weld_free_joints(plain) == (plain, False)
    # A free joint behind another joint is not the robot trunk: refuse loudly.
    trailing = (
        "<mujoco><worldbody><body name='a'><joint name='j' type='hinge'/>"
        "</body><body name='b'><freejoint name='f'/></body></worldbody>"
        "</mujoco>"
    )
    with pytest.raises(MujocoEngineError, match="free joint to come first"):
        _weld_free_joints(trailing)


def test_engine_layout_skips_unknown_bodies_and_protects_robot(tmp_path):
    engine = _engine(
        tmp_path,
        env_layout={"env_ball": [0.0, 0.0], "no_such_body": [0.0, 0.0]},
    )
    assert engine.layout_skipped == ("no_such_body",)
    with pytest.raises(MujocoEngineError, match="robot body"):
        _engine(tmp_path, env_layout={"trunk_base": [0.0, 0.0]})


def test_physics_steps_and_gravity_pulls_a_raised_trunk_down(tmp_path):
    import mujoco

    engine = _engine(tmp_path)
    assert engine.time == pytest.approx(0.0)
    engine.data.qpos[2] += 0.1  # lift the whole robot; gravity does the rest
    mujoco.mj_forward(engine.model, engine.data)
    z_before = float(engine.data.xpos[engine._root_body][2])
    assert z_before == pytest.approx(0.22, abs=1e-6)
    engine.advance(100)
    z_after = float(engine.data.xpos[engine._root_body][2])
    assert engine.time > 0.0
    assert z_after < z_before - 0.02


def test_foot_contacts_are_expected_not_collisions(tmp_path):
    engine = _engine(tmp_path)
    engine.set_commands(list(engine.home))
    engine.advance(engine.scenario.physics_steps_per_control * 10)
    snapshot = engine.snapshot()
    # Feet rest on the floor: expected touches are recorded separately and
    # never counted as disqualifying contacts.
    assert snapshot["expected_contacts"] > 0
    assert snapshot["contacts"] == 0
    assert snapshot["collision"] is False


def test_static_hold_topples_and_latches_the_fall_flag(tmp_path):
    """Holding STAND with position servos alone does not balance.

    This is measured physics, not a failure of the harness: without feedback
    control the biped falls within about a second, and the run must say so.
    """
    engine = _engine(tmp_path)
    engine.set_commands(list(engine.home))
    for _ in range(10):
        engine.advance(engine.scenario.physics_steps_per_control * 10)
        if engine.fallen:
            break
    assert engine.fallen is True
    snapshot = engine.snapshot()
    assert snapshot["collision"] is True
    assert snapshot["trunk_z"] < FALL_TRUNK_Z


def test_commands_move_the_intended_actuator(tmp_path):
    engine = _engine(tmp_path)
    head_index = EXPECTED_JOINTS.index("head_yaw")
    target = list(engine.home)
    target[head_index] = 0.3
    engine.set_commands(target)
    engine.advance(engine.scenario.physics_steps_per_control * 10)
    positions = engine.joint_positions()
    assert positions[head_index] == pytest.approx(0.3, abs=0.15)
    # Other joints have not been commanded away from STAND.
    for index, home in enumerate(engine.home):
        if index == head_index:
            continue
        assert positions[index] == pytest.approx(home, abs=0.15)


def test_invalid_commands_are_rejected_safely(tmp_path):
    engine = _engine(tmp_path)
    before = engine.joint_positions()
    assert engine.set_commands([0.0] * 13) is False
    assert engine.set_commands([0.0] * 15) is False
    assert engine.joint_positions() == pytest.approx(before)
    # Non-finite values can never reach the actuators.
    engine.set_commands([float("nan")] * 14)
    assert all(math.isfinite(v) for v in engine.commands)


def test_sensors_report_live_finite_readings(tmp_path):
    engine = _engine(tmp_path)
    engine.advance(50)
    readings = engine.sensor_readings()
    assert tuple(readings) == EXPECTED_SENSORS
    for values in readings.values():
        assert values and all(math.isfinite(v) for v in values)
    # The accelerometer observes gravity (~9.8 m/s^2 magnitude at rest).
    accel = readings["imu_accel"]
    assert math.dist(accel, (0.0, 0.0, 0.0)) == pytest.approx(9.8, abs=1.5)


def test_snapshot_bodies_match_engine_state_for_the_renderer(tmp_path):
    """What the 3D view draws is exactly what MuJoCo reports."""
    engine = _engine(tmp_path)
    engine.advance(20)
    snapshot = engine.snapshot()
    expected = 15 + len(microduck.model.ENVIRONMENT)
    assert len(snapshot["bodies"]) == expected
    live = {pose["body"]: pose for pose in engine.body_poses()}
    for pose in snapshot["bodies"]:
        assert pose["pos"] == pytest.approx(live[pose["body"]]["pos"])
        assert pose["quat"] == pytest.approx(live[pose["body"]]["quat"])
    # Environment bodies stream too, so the 3D view can draw them.
    streamed = {pose["body"] for pose in snapshot["bodies"]}
    for env_body in microduck.model.ENVIRONMENT:
        assert env_body.name in streamed
    # And the telemetry normalizer carries bodies + sensors through intact.
    normalized = topics.normalize_telemetry_sample({**snapshot, "t": 0.0})
    assert len(normalized["bodies"]) == expected
    assert set(normalized["sensors"]) == set(EXPECTED_SENSORS)


def test_stand_preview_poses_match_mujoco_forward_kinematics(tmp_path):
    """The API's engine-free STAND pose equals MuJoCo's own FK."""
    import mujoco

    from app.simulator.microduck import stand_poses

    engine = _engine(tmp_path)
    engine.reset()
    preview = {
        pose["body"]: pose for pose in stand_poses()
    }
    assert len(preview) == 15
    for body_id in range(1, engine.model.nbody):
        name = mujoco.mj_id2name(
            engine.model, mujoco.mjtObj.mjOBJ_BODY, body_id
        )
        if name not in preview:
            continue
        assert preview[name]["pos"] == pytest.approx(
            [float(v) for v in engine.data.xpos[body_id]], abs=1e-6
        )
        live_quat = [float(v) for v in engine.data.xquat[body_id]]
        expected = preview[name]["quat"]
        # Quaternions double-cover rotations; accept either sign.
        assert min(
            math.dist(live_quat, expected),
            math.dist(live_quat, [-v for v in expected]),
        ) == pytest.approx(0.0, abs=1e-6)


def test_visual_geoms_resolve_to_bundled_mesh_files():
    """Every visual placement names a real body, file, and color."""
    from app.simulator.microduck import load_metadata, visual_geoms

    bodies = set(load_metadata().bodies)
    placements = visual_geoms()
    assert len(placements) == 70
    seen_files: set[str] = set()
    for placement in placements:
        assert placement["body"] in bodies
        path = microduck.model.MODEL_DIR / "assets" / placement["file"]
        assert path.is_file()
        seen_files.add(placement["file"])
        assert len(placement["pos"]) == 3
        assert len(placement["quat"]) == 4
        assert len(placement["rgba"]) == 4
    assert len(seen_files) == 38


def test_stand_joint_targets_match_example_pose():
    """The agent brief's STAND values equal the example controller's."""
    import importlib.util
    import sys

    from app.simulator.microduck import stand_joint_targets

    sys.path.insert(0, str(Path("app/runner/shim").resolve()))
    try:
        path = Path("app/simulator/microduck/example_controller.py").resolve()
        spec = importlib.util.spec_from_file_location("stand_example", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    targets = stand_joint_targets()
    assert [name for name, _ in targets] == list(EXPECTED_JOINTS)
    assert [value for _, value in targets] == pytest.approx(
        list(module.STAND_POSE)
    )


def test_environment_bodies_are_not_robot_bodies(tmp_path):
    """Resting props never count as collisions; the trunk stays the root."""
    import mujoco

    from app.simulator.microduck import TRUNK_BODY

    engine = _engine(tmp_path)
    names = {
        mujoco.mj_id2name(engine.model, mujoco.mjtObj.mjOBJ_BODY, bid)
        for bid in engine._robot_bodies
    }
    assert TRUNK_BODY in names
    assert not any(
        name.startswith(microduck.model.ENV_BODY_PREFIX) for name in names
    )
    assert len(engine._robot_bodies) == 15
    trunk_id = mujoco.mj_name2id(
        engine.model, mujoco.mjtObj.mjOBJ_BODY, TRUNK_BODY
    )
    assert engine._root_body == trunk_id
    # The ball rests on the floor from the first step; that contact must
    # not appear in the robot's collision accounting.
    engine.advance(engine.scenario.physics_steps_per_control * 10)
    assert engine.snapshot()["contacts"] == 0


def test_resolve_asset_serves_only_referenced_meshes():
    from app.simulator.microduck import resolve_asset

    assert resolve_asset("sole_left.stl").is_file()
    with pytest.raises(MicroduckModelError):
        resolve_asset("robot_groundcontact.xml")
    with pytest.raises(MicroduckModelError):
        resolve_asset("../model.py")
    with pytest.raises(MicroduckModelError):
        resolve_asset("no_such_mesh.stl")


def test_joint_test_runner_records_fall_evidence(tmp_path):
    engine = _engine(tmp_path)
    scenario = MujocoScenario.from_dict(
        {
            **MICRODUCK_WORLD,
            "tests": [
                {
                    "id": "leg_probe",
                    "joint": "left_hip_pitch",
                    "target": 0.2,
                    "timeout_s": 5.0,
                }
            ],
        }
    )
    runner = JointTestRunner(engine, scenario)
    sim_time = 0.0
    for _ in range(500):
        if runner.finished:
            break
        engine.set_commands(runner.target_payload())
        engine.advance(scenario.physics_steps_per_control)
        sim_time += scenario.control_period
        runner.tick(sim_time)
    assert runner.finished
    assert len(runner.results) == 1
    assert runner.results[0]["passed"] is False
    assert "fell" in runner.results[0]["reason"]


# ---------------------------------------------------------------------------
# Catalog / install / staging / agent context
# ---------------------------------------------------------------------------


def test_catalog_microduck_is_the_official_model():
    robot = get_starter_robot("microduck")
    assert "official" in robot.name.lower()
    assert is_composed_scene(robot.mjcf)
    assert "CC BY-NC-SA" in robot.license
    assert "microduck_rl" in robot.source_url
    summary = starter_robot_summary(robot)
    assert [joint["name"] for joint in summary["joints"]] == list(EXPECTED_JOINTS)
    assert len(summary["tests"]) == 14


def test_install_writes_the_official_composition(tmp_path):
    store = Store(tmp_path / "t.db")
    project = store.create_project("p", "task")
    package = install_starter_robot(store, project["id"], "microduck")
    stored = store.get_file(project["id"], "robot.xml")
    assert stored is not None
    assert is_composed_scene(stored["content"])
    assert package.model_path == "robot.xml"
    assert {joint.name for joint in package.joints} == set(EXPECTED_JOINTS)


def test_staging_copies_mesh_assets_for_official_runs(tmp_path):
    staging = tmp_path / "staging"
    workspace, _, entry = prepare_workspace(
        staging,
        [
            {"path": "controller.py", "content": "# controller"},
            {"path": "robot.xml", "content": compose_scene_xml()},
        ],
        {"backend": "mujoco"},
    )
    assert entry == "controller.py"
    assert (workspace / "assets" / "sole_left.stl").is_file()
    assert len(list((workspace / "assets").glob("*.stl"))) == 38


def test_staging_skips_assets_for_generated_rigs(tmp_path):
    from app.services import meta_model

    staging = tmp_path / "staging"
    workspace, _, _ = prepare_workspace(
        staging,
        [
            {"path": "controller.py", "content": "# controller"},
            {"path": "robot.xml", "content": meta_model.DEV_MOCK_ROBOT_XML},
        ],
        {"backend": "mujoco"},
    )
    assert not (workspace / "assets").exists()


def test_agent_brief_names_real_actuators_for_microduck_projects():
    brief = target_robot_brief(
        [
            {"path": "controller.py", "content": "# c"},
            {"path": "robot.xml", "content": compose_scene_xml()},
        ]
    )
    assert "left_hip_yaw" in brief and "right_ankle" in brief
    assert "imu_accel" in brief
    assert "do NOT rewrite it" in brief


def test_agent_brief_is_empty_for_generated_rigs():
    from app.services import meta_model

    assert (
        target_robot_brief(
            [
                {"path": "controller.py", "content": "# c"},
                {"path": "robot.xml", "content": meta_model.DEV_MOCK_ROBOT_XML},
            ]
        )
        == ""
    )


def test_microduck_runs_use_official_physics_by_default(tmp_path):
    from app.services.execution import _microduck_defaults

    store = Store(tmp_path / "t.db")
    project = store.create_project("p", "task")
    install_starter_robot(store, project["id"], "microduck")
    defaults = _microduck_defaults(store, project["id"])
    assert defaults["dt"] == OFFICIAL_TIMESTEP
    assert defaults["keyframe"] == OFFICIAL_KEYFRAME
    assert defaults["integrator"] == "Euler"
    assert defaults["fall_trunk_z"] == FALL_TRUNK_Z


# ---------------------------------------------------------------------------
# Example controller
# ---------------------------------------------------------------------------


def test_example_controller_ramps_to_stand_and_holds(tmp_path):
    """The bundled example publishes bounded STAND-tracking commands."""
    sys.path.insert(0, str(Path("app/runner/shim").resolve()))
    try:
        import importlib.util

        path = (
            Path("app/simulator/microduck/example_controller.py").resolve()
        )
        spec = importlib.util.spec_from_file_location("stand_example", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert len(module.STAND_POSE) == 14
        assert module.STAND_POSE[2] == pytest.approx(-0.457924)
    finally:
        sys.path.pop(0)


# ---------------------------------------------------------------------------
# Pause / resume mechanism
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
def test_local_runner_pause_suspends_and_resumes_a_process():
    """SIGSTOP/SIGCONT round-trip on a real child process group."""
    runner = LocalSubprocessRunner()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    run_id = "pause-probe"
    assert process.pid is not None
    runner._live[run_id] = process.pid
    try:
        asyncio.run(runner.pause(run_id))
        assert runner.is_paused(run_id)
        time.sleep(0.2)
        with open(f"/proc/{process.pid}/stat") as handle:
            state = handle.read().split()[2]
        assert state == "T", f"expected stopped state, got {state!r}"
        asyncio.run(runner.resume(run_id))
        assert not runner.is_paused(run_id)
        time.sleep(0.2)
        with open(f"/proc/{process.pid}/stat") as handle:
            state = handle.read().split()[2]
        assert state != "T"
    finally:
        runner._live.pop(run_id, None)
        runner._paused.discard(run_id)
        process.kill()
        process.wait()


def test_pause_rejects_unknown_and_terminal_runs(tmp_path):
    runner = LocalSubprocessRunner()
    with pytest.raises(Exception, match="no live process"):
        asyncio.run(runner.pause("missing"))

    async def scenario():
        settings = Settings(
            db_path=str(tmp_path / "t.db"),
            model_api_key="",
            meta_api_base_url="https://api.meta.ai/v1",
            muse_model="muse-spark-1.3",
            model_timeout_s=5.0,
            cors_origins=("*",),
        )
        store = Store(settings.db_path)
        project = store.create_project("p", "task")
        run = store.create_run(
            project["id"], parent_run_id=None, controller_version=0,
            status="succeeded", world_config=None, seed=0, backend="mujoco",
        )
        manager = ExecutionManager(store, bus=RunEventBus(), runner=runner)
        try:
            await manager.pause_run(run["id"])
        finally:
            await manager.shutdown()

    with pytest.raises(ValueError, match="already succeeded"):
        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Automated smoke test: official model end to end through the harness
# ---------------------------------------------------------------------------


def _drive_until_terminal(manager, run_id, attempts=1200):
    async def poll():
        for _ in range(attempts):
            current = manager.get_run(run_id)
            if is_terminal(current["status"]):
                return current
            await asyncio.sleep(0.05)
        raise AssertionError("run did not reach a terminal state")

    return poll()


def test_microduck_smoke_run_records_physics_evidence(tmp_path):
    """Full loop on the official model: install, run, telemetry, verdict.

    The reference controller tracks joint targets without balancing, so the
    run is expected to fail with fall evidence — the smoke asserts the loop
    is honest (real physics, real telemetry, structured verdict), not that
    the duck stays upright.
    """
    from app.simulator.robot_catalog import REFERENCE_CONTROLLER

    async def scenario():
        settings = Settings(
            db_path=str(tmp_path / "t.db"),
            model_api_key="",
            meta_api_base_url="https://api.meta.ai/v1",
            muse_model="muse-spark-1.3",
            model_timeout_s=5.0,
            cors_origins=("*",),
        )
        store = Store(settings.db_path)
        project = store.create_project("p", "move the official duck")
        install_starter_robot(store, project["id"], "microduck")
        store.save_file(project["id"], "controller.py", REFERENCE_CONTROLLER)
        manager = ExecutionManager(
            store, bus=RunEventBus(), runner=LocalSubprocessRunner(settings)
        )
        run = await manager.start_run(
            project["id"],
            world_config={
                "tests": [
                    {
                        "id": "head_probe",
                        "joint": "head_yaw",
                        "target": 0.3,
                        "timeout_s": 5.0,
                    }
                ],
                "time_limit": 20.0,
            },
            seed=0,
        )
        final = await _drive_until_terminal(manager, run["id"])
        await manager.shutdown()
        return store, final

    store, final = asyncio.run(scenario())
    assert final["world_config"]["dt"] == OFFICIAL_TIMESTEP
    assert final["world_config"]["keyframe"] == OFFICIAL_KEYFRAME
    telemetry = store.list_telemetry(final["id"])
    assert len(telemetry) > 5
    last = telemetry[-1]
    assert len(last["joints"]) == 14
    assert len(last["bodies"]) == 15 + len(microduck.model.ENVIRONMENT)
    assert set(last["sensors"]) == set(EXPECTED_SENSORS)
    assert final["tests"], "the run must record its test evidence"
    assert final["failure_summary"] or final["metrics"] is not None
