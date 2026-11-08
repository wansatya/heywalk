"""ED-209 walker tests: registry switching, shared scene, joint runs.

Covers the second bundled robot (AGENTS.md sections 5 and 7): the ED-209
catalog entry validates like every starter robot, detection switches run
defaults/prompts/staging between MicroDuck and ED-209 by composition
marker, both robots share one arena scene, and the ED-209 joint-test
suite passes on the lifted stand with an ideal tracking controller.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app.db import Store
from app.services.execution import (
    _bundled_goal_geoms,
    _bundled_robot_defaults,
    _microduck_defaults,
)
from app.services.generation import install_starter_robot
from app.services.meta_model import (
    ED209_GRADING_BRIEF,
    MICRODUCK_GRADING_BRIEF,
    GenerationContext,
    brief_robot_id,
    build_generate_messages,
    target_robot_brief,
)
from app.simulator import ed209, microduck, scene
from app.simulator.ed209 import (
    Ed209ModelError,
    body_hierarchy,
    compose_scene_xml,
    is_composed_scene,
    load_metadata,
    missing_assets,
    resolve_asset,
    stage_assets,
    stand_joint_targets,
    stand_poses,
    visual_geoms,
)
from app.simulator.mujoco_engine import MujocoEngine
from app.simulator.mujoco_tests import JointTestRunner
from app.simulator.robot_catalog import (
    ED209_SCENARIO_DEFAULTS,
    detect_starter_robot,
    get_starter_robot,
    starter_robot_summary,
)
from app.simulator.robot_description import parse_mjcf
from app.simulator.scenario import (
    MujocoScenario,
    ScenarioError,
    default_tests_for_joints,
)

EXPECTED_JOINTS = (
    "left_hip_pitch",
    "left_knee",
    "left_ankle",
    "right_hip_pitch",
    "right_knee",
    "right_ankle",
)

ED209_WORLD = dict(ED209_SCENARIO_DEFAULTS)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / f"ws_{len(list(tmp_path.glob('ws_*')))}"
    workspace.mkdir()
    (workspace / "robot.xml").write_text(compose_scene_xml())
    stage_assets(workspace)
    return workspace


def _engine(tmp_path: Path, **overrides) -> MujocoEngine:
    workspace = _workspace(tmp_path)
    world = dict(ED209_WORLD)
    world.update(overrides)
    scenario = MujocoScenario.from_dict(world)
    info = parse_mjcf((workspace / "robot.xml").read_text(), "robot.xml")
    return MujocoEngine(workspace / "robot.xml", info, scenario=scenario)


# ---------------------------------------------------------------------------
# Catalog entry + registry detection (robot switching)
# ---------------------------------------------------------------------------


def test_ed209_catalog_entry_is_a_validated_package():
    robot = get_starter_robot("ed209")
    summary = starter_robot_summary(robot)
    assert summary["id"] == "ed209"
    assert summary["model_path"] == "robot.xml"
    assert [joint["name"] for joint in summary["joints"]] == list(
        EXPECTED_JOINTS
    )
    assert {test["joint"] for test in summary["tests"]} == set(
        EXPECTED_JOINTS
    )
    assert robot.marker is not None and robot.models is not None
    assert robot.scenario_defaults["fix_base"] is True


def test_detection_switches_robots_by_composition_marker():
    assert (
        detect_starter_robot(get_starter_robot("ed209").mjcf).id  # type: ignore[union-attr]
        == "ed209"
    )
    assert (
        detect_starter_robot(get_starter_robot("microduck").mjcf).id  # type: ignore[union-attr]
        == "microduck"
    )
    assert detect_starter_robot(get_starter_robot("arm").mjcf) is None
    assert is_composed_scene(get_starter_robot("ed209").mjcf)
    assert not microduck.is_composed_scene(get_starter_robot("ed209").mjcf)
    assert not is_composed_scene(get_starter_robot("microduck").mjcf)


def test_installing_ed209_after_microduck_switches_the_project(tmp_path):
    store = Store(tmp_path / "t.db")
    project = store.create_project("p", "task")
    install_starter_robot(store, project["id"], "microduck")
    assert _bundled_robot_defaults(store, project["id"])["dt"] == pytest.approx(
        0.005
    )
    install_starter_robot(store, project["id"], "ed209")
    defaults = _bundled_robot_defaults(store, project["id"])
    assert defaults["dt"] == pytest.approx(0.002)
    assert defaults["keyframe"] == "STAND"
    assert defaults["fix_base"] is True
    assert defaults["fix_base_lift"] == pytest.approx(0.30)
    assert defaults["fall_trunk_z"] == pytest.approx(1.0)
    # The MicroDuck-only helpers stay silent for ED-209 projects.
    assert _microduck_defaults(store, project["id"]) == {}
    assert (
        _bundled_goal_geoms(
            store, project["id"], {"objectives": [{"type": "go_to"}]}
        )
        == []
    )


# ---------------------------------------------------------------------------
# Shared scene (one arena for every bundled robot)
# ---------------------------------------------------------------------------


def test_both_robots_stage_the_same_shared_scene():
    assert ed209.ENVIRONMENT is scene.ENVIRONMENT
    assert microduck.ENVIRONMENT is scene.ENVIRONMENT
    assert ed209.FLAG_BODY == microduck.model.FLAG_BODY == "env_flag"
    assert ed209.WALL_BODIES is scene.WALL_BODIES

    def env_bodies(xml: str) -> list[str]:
        root = ET.fromstring(xml)
        assert root.tag == "mujoco"
        worldbody = root.find("worldbody")
        assert worldbody is not None
        return sorted(
            body.get("name", "")
            for body in worldbody.findall("body")
            if (body.get("name") or "").startswith("env_")
        )

    duck_env = env_bodies(microduck.compose_scene_xml())
    ed209_env = env_bodies(compose_scene_xml())
    assert duck_env == ed209_env
    assert len(duck_env) == len(scene.ENVIRONMENT)
    # Same shared floor, not per-robot planes.
    for xml in (microduck.compose_scene_xml(), compose_scene_xml()):
        root = ET.fromstring(xml)
        assert root.tag == "mujoco"
        planes = [
            geom
            for geom in root.iter("geom")
            if geom.get("type") == "plane"
        ]
        assert len(planes) == 1
        assert planes[0].get("size") == "0 0 0.05"


def test_ed209_keyframe_covers_hinges_and_the_shared_ball():
    root = ET.fromstring(compose_scene_xml())
    assert root.tag == "mujoco"
    keys = root.find("keyframe")
    assert keys is not None
    stand = next(
        key
        for key in keys.findall("key")
        if key.get("name") == "STAND"
    )
    # Trunk free joint (7) + 6 hinges + the ball's free joint (7).
    assert len(stand.get("qpos", "").split()) == 20


# ---------------------------------------------------------------------------
# Model loading + asset resolution + metadata discovery
# ---------------------------------------------------------------------------


def test_ed209_metadata_is_discovered_from_the_model():
    metadata = load_metadata()
    assert metadata.joints == EXPECTED_JOINTS
    assert metadata.actuators == tuple(f"{name}_position" for name in EXPECTED_JOINTS)
    assert metadata.sensors == ()
    assert len(metadata.mesh_files) == 12
    assert metadata.keyframes == ("STAND",)
    assert metadata.timestep == pytest.approx(0.002)
    assert metadata.control_hz == pytest.approx(50.0)
    assert metadata.joint_ranges["left_knee"] == (
        pytest.approx(-1.5),
        pytest.approx(0.2),
    )


def test_ed209_assets_resolve_and_stage():
    assert missing_assets() == ()
    assert resolve_asset("torso.stl").is_file()
    with pytest.raises(Ed209ModelError):
        resolve_asset("no_such_mesh.stl")
    with pytest.raises(Ed209ModelError):
        resolve_asset("torso.obj")


def test_ed209_staging_copies_meshes_for_runs(tmp_path):
    from app.runner.staging import prepare_workspace

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
    assert (workspace / "assets" / "torso.stl").is_file()
    assert len(list((workspace / "assets").glob("*.stl"))) == 12


def test_ed209_hierarchy_visuals_and_stand_targets():
    links = body_hierarchy()
    assert len(links) == 12
    assert links[0] == {"body": "torso", "parent": None}
    placements = visual_geoms()
    assert len(placements) == 12
    bodies = {link["body"] for link in links}
    for placement in placements:
        assert placement["body"] in bodies
        assert resolve_asset(placement["file"]).is_file()
        assert len(placement["rgba"]) == 4
    assert stand_joint_targets() == [(name, 0.0) for name in EXPECTED_JOINTS]
    poses = {pose["body"]: pose for pose in stand_poses()}
    assert len(poses) == 12
    assert poses["torso"]["pos"] == pytest.approx([0.0, 0.0, 1.49])


def test_ed209_stand_preview_matches_mujoco_forward_kinematics(tmp_path):
    """The API's engine-free STAND pose equals MuJoCo's own FK."""
    import mujoco

    engine = _engine(tmp_path, fix_base_lift=0.0)
    engine.reset()
    preview = {pose["body"]: pose for pose in stand_poses()}
    assert len(preview) == 12
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


# ---------------------------------------------------------------------------
# Engine on the lifted stand + the automated joint suite
# ---------------------------------------------------------------------------


def test_fix_base_lift_is_validated():
    assert MujocoScenario.from_dict({}).fix_base_lift == 0.0
    assert MujocoScenario.from_dict({"fix_base_lift": 0.3}).fix_base_lift == (
        pytest.approx(0.3)
    )
    assert MujocoScenario.from_dict(
        {"fix_base_lift": 0.3}
    ).to_dict()["fix_base_lift"] == pytest.approx(0.3)
    with pytest.raises(ScenarioError, match="fix_base_lift"):
        MujocoScenario.from_dict({"fix_base_lift": -0.1})


def test_engine_resolves_the_ed209_trunk_and_lifted_stand(tmp_path):
    engine = _engine(tmp_path)
    assert engine.base_fixed is True
    assert engine.joint_names == EXPECTED_JOINTS
    assert engine.joint_positions() == pytest.approx([0.0] * 6)
    # STAND trunk (1.49 m) plus the catalog lift (0.30 m).
    assert engine.trunk_z == pytest.approx(1.79, abs=1e-6)
    assert engine.fallen is False


def test_ed209_joint_suite_passes_with_a_tracking_controller(tmp_path):
    workspace = _workspace(tmp_path)
    info = parse_mjcf((workspace / "robot.xml").read_text(), "robot.xml")
    joints = [(joint.name, joint.lower, joint.upper) for joint in info.joints]
    world = dict(ED209_WORLD)
    world["tests"] = [
        test.to_dict() for test in default_tests_for_joints(joints)
    ]
    world["time_limit"] = 60.0
    scenario = MujocoScenario.from_dict(world)
    engine = MujocoEngine(workspace / "robot.xml", info, scenario=scenario)
    runner = JointTestRunner(engine, scenario)
    sim_time = 0.0
    for _ in range(int(scenario.time_limit / scenario.control_period) + 10):
        if runner.finished:
            break
        engine.set_commands(runner.target_payload())
        engine.advance(scenario.physics_steps_per_control)
        sim_time += scenario.control_period
        runner.tick(sim_time)
    assert runner.finished
    assert runner.all_tests_passed
    assert [result["joint"] for result in runner.results] == list(
        EXPECTED_JOINTS
    )
    for result in runner.results:
        assert result["final_error"] < 0.05


def test_locomotion_rejects_ed209_without_a_walking_policy(tmp_path):
    from app.simulator.locomotion import LocomotionRunner
    from app.simulator.microduck.policy import PolicyError

    engine = _engine(tmp_path, fix_base=False)
    scenario = MujocoScenario.from_dict({"control_hz": 50.0})
    with pytest.raises(PolicyError, match="MicroDuck joint layout"):
        LocomotionRunner(engine, scenario)


# ---------------------------------------------------------------------------
# Prompts brief the installed robot
# ---------------------------------------------------------------------------


def test_agent_brief_names_ed209_actuators_props_and_limits():
    robot_id = brief_robot_id(
        [
            {"path": "controller.py", "content": "# c"},
            {"path": "robot.xml", "content": compose_scene_xml()},
        ]
    )
    assert robot_id == "ed209"
    brief = target_robot_brief(
        [
            {"path": "controller.py", "content": "# c"},
            {"path": "robot.xml", "content": compose_scene_xml()},
        ]
    )
    assert "left_hip_pitch" in brief and "right_ankle" in brief
    assert "do NOT rewrite it" in brief
    assert "env_flag" in brief and "env_ball" in brief
    assert "joint tests only" in brief


def test_generate_prompt_grades_ed209_joints_only_not_locomotion():
    ed209_messages = build_generate_messages(
        GenerationContext(
            task_description="Move each joint.",
            existing_files=[
                {"path": "controller.py", "content": "# c"},
                {"path": "robot.xml", "content": compose_scene_xml()},
            ],
        )
    )
    ed209_prompt = ed209_messages[-1]["content"]
    assert ED209_GRADING_BRIEF in ed209_prompt
    assert MICRODUCK_GRADING_BRIEF not in ed209_prompt
    assert "locomotion" not in ed209_prompt.lower().replace(
        "no locomotion mode", ""
    ).replace("no walking policy", "")

    duck_messages = build_generate_messages(
        GenerationContext(
            task_description="Walk to the flag.",
            existing_files=[
                {"path": "controller.py", "content": "# c"},
                {
                    "path": "robot.xml",
                    "content": microduck.compose_scene_xml(),
                },
            ],
        )
    )
    assert MICRODUCK_GRADING_BRIEF in duck_messages[-1]["content"]
    assert ED209_GRADING_BRIEF not in duck_messages[-1]["content"]


# ---------------------------------------------------------------------------
# API installs either robot and serves either model
# ---------------------------------------------------------------------------


def test_install_ed209_writes_a_validated_package(client, project_id):
    response = client.post(
        f"/api/projects/{project_id}/robot", json={"robot_id": "ed209"}
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["joints"]) == 6
    assert len(body["tests"]) == 6
    assert body["model_path"] == "robot.xml"

    files = {
        entry["path"]
        for entry in client.get(f"/api/projects/{project_id}/files").json()
    }
    assert files == {
        "controller.py",
        "robot.xml",
        "robot.urdf",
        "ros2_control.yaml",
    }

    robots = {
        robot["id"]: robot for robot in client.get("/api/robots").json()
    }
    assert set(robots) >= {"microduck", "ed209", "arm"}
    assert len(robots["ed209"]["joints"]) == 6


def test_installed_robot_endpoint_tracks_switches(client, project_id):
    empty = client.get(f"/api/projects/{project_id}/robot")
    assert empty.status_code == 200
    assert empty.json() == {"robot_id": None}

    client.post(f"/api/projects/{project_id}/robot", json={"robot_id": "ed209"})
    assert client.get(f"/api/projects/{project_id}/robot").json() == {
        "robot_id": "ed209"
    }

    client.post(
        f"/api/projects/{project_id}/robot", json={"robot_id": "microduck"}
    )
    assert client.get(f"/api/projects/{project_id}/robot").json() == {
        "robot_id": "microduck"
    }
    assert client.get("/api/projects/missing/robot").status_code == 404


def test_generic_model_endpoint_serves_ed209(client):
    response = client.get("/api/robots/ed209/model")
    assert response.status_code == 200
    payload = response.json()
    assert payload["robot_id"] == "ed209"
    assert payload["joints"] == list(EXPECTED_JOINTS)
    assert payload["assets_ok"] is True
    assert payload["missing_assets"] == []
    assert len(payload["hierarchy"]) == 12
    assert len(payload["visual_geoms"]) == 12
    assert len(payload["default_poses"]) == 12
    assert len(payload["environment"]) == len(scene.ENVIRONMENT)


def test_generic_model_endpoint_matches_duck_shape(client):
    duck = client.get("/api/robots/microduck/model").json()
    generic = client.get("/api/robots/microduck/model").json()
    assert generic["robot_id"] == "microduck"
    assert generic["environment"] == duck["environment"]
    missing = client.get("/api/robots/arm/model")
    assert missing.status_code == 404
    unknown = client.get("/api/robots/nope/model")
    assert unknown.status_code == 404


def test_ed209_mesh_assets_are_cacheable(client):
    response = client.get("/api/robots/ed209/assets/torso.stl")
    assert response.status_code == 200
    assert response.headers["content-type"] == "model/stl"
    assert response.headers["cache-control"] == "public, max-age=86400"
    assert (
        client.get("/api/robots/ed209/assets/no_such_mesh.stl").status_code
        == 404
    )
    # Interchange-only files are never served as simulator meshes.
    assert client.get("/api/robots/ed209/assets/torso.obj").status_code == 404


def test_ed209_mesh_bundle_carries_every_mesh_in_one_response(client):
    import json
    import struct

    response = client.get("/api/robots/ed209/assets/bundle")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=86400"
    body = response.content
    (manifest_length,) = struct.unpack_from("<I", body)
    manifest = json.loads(body[4 : 4 + manifest_length].decode("utf-8"))
    assert [entry["name"] for entry in manifest] == sorted(
        {entry["file"] for entry in visual_geoms()}
    )
    assert len(manifest) == 12
    offset = 4 + manifest_length
    for entry in manifest:
        blob = body[offset : offset + entry["size"]]
        assert blob == resolve_asset(entry["name"]).read_bytes()
        offset += entry["size"]
    assert offset == len(body)
