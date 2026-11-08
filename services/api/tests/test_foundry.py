"""Robot Foundry API integration tests.

The Foundry must produce a real, MuJoCo-compilable package from an uploaded
image: segmented components, mesh files, an MJCF, validation evidence, and a
sequence the existing simulator can open. These tests exercise the public API
and the actual subprocess validator (no mocked MuJoCo result).
"""

from __future__ import annotations

import io
import time

from PIL import Image, ImageDraw


def _robot_png() -> bytes:
    image = Image.new("RGB", (400, 600), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    draw.rectangle([150, 180, 250, 360], fill=(70, 110, 170))  # torso
    draw.ellipse([170, 100, 230, 170], fill=(80, 120, 180))  # head
    draw.rectangle([100, 190, 145, 340], fill=(60, 100, 160))  # left arm
    draw.rectangle([255, 190, 300, 340], fill=(60, 100, 160))  # right arm
    draw.rectangle([160, 370, 195, 540], fill=(50, 90, 150))  # left leg
    draw.rectangle([205, 370, 240, 540], fill=(50, 90, 150))  # right leg
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _create(client, name="Foundry Bot", category="humanoid"):
    response = client.post(
        "/api/foundry/projects",
        json={"name": name, "description": "from a photo", "category": category},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _upload(client, foundry_id):
    response = client.post(
        f"/api/foundry/projects/{foundry_id}/images",
        files={"files": ("robot.png", _robot_png(), "image/png")},
        data={"image_type": "reference"},
    )
    assert response.status_code == 201, response.text
    return response.json()[0]


def _wait(client, job_id, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/foundry/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"Foundry job {job_id} did not finish in time.")


def _start(client, foundry_id, stage):
    response = client.post(f"/api/foundry/projects/{foundry_id}/{stage}")
    assert response.status_code == 202, response.text
    return _wait(client, response.json()["job"]["id"])


def test_foundry_project_creates_a_simulator_robot_folder(client):
    """Creating a robot must not create a sequence project.

    The robot is a package folder in the simulator robot library; sequences
    are only created when the user explicitly opens it in the simulator.
    """
    before = {project["id"] for project in client.get("/api/projects").json()}
    foundry = _create(client)
    after = {project["id"] for project in client.get("/api/projects").json()}
    assert after == before
    assert foundry["project_id"] == ""

    root = client.app.state.foundry.project_dir(foundry["id"])
    assert root.is_dir()
    assert root.parent == client.app.state.foundry.root

    listed = client.get("/api/foundry/projects")
    assert any(item["id"] == foundry["id"] for item in listed.json())


def test_foundry_defaults_to_a_biped_robot(client):
    response = client.post(
        "/api/foundry/projects",
        json={"name": "Default Biped", "description": "from a photo"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["category"] == "biped"


def test_foundry_full_pipeline_generates_a_compilable_robot(client):
    foundry = _create(client)
    foundry_id = foundry["id"]
    image = _upload(client, foundry_id)
    assert image["asset_url"].endswith(".png")

    job = _start(client, foundry_id, "generate")
    assert job["status"] == "succeeded", job
    generation = job["result"]["generation"]
    assert generation["status"] in ("ok", "warning")
    assert generation["checks"]["mujoco_compilation"] is True
    assert generation["checks"]["mesh_files_valid"] is True
    assert generation["joints"]

    detail = client.get(f"/api/foundry/projects/{foundry_id}").json()
    assert detail["project"]["status"] == "READY"
    assert detail["project"]["component_count"] >= 4
    assert detail["project"]["joint_count"] >= 1
    assert detail["components"]
    for component in detail["components"]:
        assert component["visual_asset_url"]
        assert component["collision_asset_url"]
        # The reported preview must actually exist.
        preview = client.get(component["preview_asset_url"])
        assert preview.status_code == 200, component["preview_asset_url"]
        visual = client.get(component["visual_asset_url"])
        assert visual.status_code == 200

    model = client.get(f"/api/foundry/projects/{foundry_id}/model")
    assert model.status_code == 200
    assert "<mujoco" in model.text
    assert "<freejoint" in model.text
    # Adjacent collision meshes overlap at the joints; without excludes the
    # solver jams the joint with huge contact forces and the joint test fails.
    assert "<contact>" in model.text
    assert model.text.count("<exclude") >= len(detail["components"]) - 1

    versions = client.get(f"/api/foundry/projects/{foundry_id}/versions").json()
    assert versions and versions[0]["validation"]["status"] in ("ok", "warning")

    # Load the real generated model in MuJoCo and verify an arm joint can
    # actually track a target without self-collision contacts.
    from app.simulator.mujoco_engine import MujocoEngine
    from app.simulator.robot_description import parse_mjcf
    from app.simulator.scenario import MujocoScenario

    model_path = client.app.state.foundry.project_dir(foundry_id) / "robot.xml"
    info = parse_mjcf(model_path.read_text(encoding="utf-8"), model_path.name)
    engine = MujocoEngine(
        model_path, info, scenario=MujocoScenario(fix_base=True, time_limit=5.0)
    )
    try:
        engine.reset()
        index = [joint.name for joint in info.joints].index("left_arm_joint")
        target = list(engine.home)
        target[index] = 0.45
        engine.set_commands(target)
        engine.advance(1000)  # 2 s at the default 0.002 s timestep
        assert abs(engine.joint_positions()[index] - 0.45) <= 0.05
        assert engine.contacts == 0
    finally:
        engine.close()


def test_foundry_simulate_stages_meshes_into_the_sequence(client):
    foundry = _create(client, name="Sim Bot")
    foundry_id = foundry["id"]
    _upload(client, foundry_id)
    assert _start(client, foundry_id, "generate")["status"] == "succeeded"

    # The robot lives in the simulator library before any sequence exists.
    robot_dir = client.app.state.foundry.project_dir(foundry_id)
    assert (robot_dir / "robot.xml").is_file()
    assert (robot_dir / "assets" / "visual").is_dir()
    assert (robot_dir / "assets" / "collision").is_dir()

    response = client.post(f"/api/foundry/projects/{foundry_id}/simulate")
    assert response.status_code == 200, response.text
    result = response.json()
    sequence_id = result["project_id"]
    assert sequence_id and sequence_id != foundry_id
    assert "robot.xml" in result["text_files"]
    assert any(name.endswith(".obj") for name in result["binary_files"])
    assert any(name.endswith(".stl") for name in result["binary_files"])

    files = client.get(f"/api/projects/{sequence_id}/files").json()
    paths = {entry["path"] for entry in files}
    assert "robot.xml" in paths
    assert "controller.py" in paths

    sequence_dir = client.app.state.store.project_dir(sequence_id)
    assert (sequence_dir / "assets" / "visual").is_dir()
    assert list((sequence_dir / "assets" / "visual").glob("*.obj"))
    assert list((sequence_dir / "assets" / "collision").glob("*.stl"))
    assert (
        client.get(f"/api/projects/{sequence_id}/robot").json()["robot_id"]
        == f"foundry:{foundry_id}"
    )


def test_foundry_edits_flow_into_the_regenerated_model(client):
    foundry = _create(client, name="Edit Bot")
    foundry_id = foundry["id"]
    _upload(client, foundry_id)
    assert _start(client, foundry_id, "generate")["status"] == "succeeded"

    detail = client.get(f"/api/foundry/projects/{foundry_id}").json()
    component = detail["components"][0]
    response = client.patch(
        f"/api/foundry/components/{component['id']}",
        json={
            "label": "Reviewed torso",
            "mass_kg": 3.5,
            "friction": 0.42,
        },
    )
    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["label"] == "Reviewed torso"
    assert edited["metadata"]["physics"]["mass_kg"] == 3.5
    assert edited["metadata"]["physics"]["friction"] == 0.42
    assert edited["metadata"]["physics"]["estimated"] is False

    joint = detail["joints"][0]
    response = client.patch(
        f"/api/foundry/joints/{joint['id']}",
        json={"axis": [0.0, 1.0, 0.0], "limits": [-0.2, 0.2]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["axis"] == [0.0, 1.0, 0.0]

    actuator = detail["actuators"][0]
    response = client.patch(
        f"/api/foundry/actuators/{actuator['id']}", json={"kp": 123.0}
    )
    assert response.status_code == 200, response.text
    assert response.json()["configuration"]["kp"] == 123.0

    before = len(detail["versions"])
    job = _start(client, foundry_id, "generate-mjcf")
    assert job["status"] == "succeeded", job
    model = client.get(f"/api/foundry/projects/{foundry_id}/model").text
    assert 'mass="3.5"' in model
    assert 'axis="0 1 0"' in model
    assert 'kp="123"' in model
    versions = client.get(f"/api/foundry/projects/{foundry_id}/versions").json()
    assert len(versions) == before + 1


def test_foundry_analysis_requires_an_image(client):
    foundry = _create(client)
    job = _start(client, foundry["id"], "analyze")
    assert job["status"] == "failed"
    assert "Upload at least one reference image" in (job["error_message"] or "")


def test_foundry_generation_requires_reconstruction(client):
    foundry = _create(client)
    foundry_id = foundry["id"]
    _upload(client, foundry_id)
    assert _start(client, foundry_id, "analyze")["status"] == "succeeded"

    job = _start(client, foundry_id, "generate-mjcf")
    assert job["status"] == "failed"
    assert "reconstruction" in (job["error_message"] or "").lower()


def test_foundry_asset_paths_cannot_escape_the_project(client):
    foundry = _create(client)
    response = client.get(
        f"/api/foundry/projects/{foundry['id']}/assets/../heywalk.db"
    )
    assert response.status_code in (400, 404)


def test_foundry_robot_is_a_switchable_catalog_robot(client):
    """A generated robot must behave like a bundled catalog robot.

    It appears in the switch-robot catalog, serves model/assets, installs into
    an unrelated sequence with binary meshes, is detected from its MJCF
    marker, and carries joint-test run defaults.
    """
    foundry = _create(client, name="Catalog Bot")
    foundry_id = foundry["id"]
    _upload(client, foundry_id)
    assert _start(client, foundry_id, "generate")["status"] == "succeeded"
    robot_id = f"foundry:{foundry_id}"

    catalog = client.get("/api/robots")
    assert catalog.status_code == 200
    entry = next(
        (item for item in catalog.json() if item["id"] == robot_id), None
    )
    assert entry is not None, "Foundry robot missing from the robot catalog"
    assert entry["source"] == "foundry"
    assert entry["foundry_id"] == foundry_id
    assert entry["joints"]

    model_response = client.get(f"/api/robots/{robot_id}/model")
    assert model_response.status_code == 200, model_response.text
    model = model_response.json()
    assert model["robot_id"] == robot_id
    assert model["visual_geoms"]
    assert model["default_poses"]
    assert model["assets_ok"] is True
    assert model["missing_assets"] == []

    bundle = client.get(f"/api/robots/{robot_id}/assets/bundle")
    assert bundle.status_code == 200
    assert len(bundle.content) > 0
    first_asset = model["visual_geoms"][0]["file"]
    assert client.get(f"/api/robots/{robot_id}/assets/{first_asset}").status_code == 200

    target = client.post(
        "/api/projects", json={"name": "Installed elsewhere", "task_description": ""}
    ).json()
    installed = client.post(
        f"/api/projects/{target['id']}/robot",
        json={"robot_id": robot_id, "include_controller": False},
    )
    assert installed.status_code == 200, installed.text
    assert installed.json()["joints"]

    files = {
        item["path"]
        for item in client.get(f"/api/projects/{target['id']}/files").json()
    }
    assert "robot.xml" in files
    assert "controller.py" in files
    assert client.get(f"/api/projects/{target['id']}/robot").json()["robot_id"] == robot_id

    store = client.app.state.store
    sequence_dir = store.project_dir(target["id"])
    assert any(sequence_dir.glob("assets/visual/*.obj"))
    assert any(sequence_dir.glob("assets/collision/*.stl"))

    # The copied package must load in real MuJoCo from the target sequence;
    # this proves the binary mesh staging is complete, not just the file list.
    from app.simulator.mujoco_engine import MujocoEngine
    from app.simulator.robot_description import parse_mjcf
    from app.simulator.scenario import MujocoScenario

    model_path = sequence_dir / "robot.xml"
    info = parse_mjcf(model_path.read_text(encoding="utf-8"), "robot.xml")
    engine = MujocoEngine(
        model_path, info, scenario=MujocoScenario(fix_base=True, time_limit=5.0)
    )
    try:
        engine.reset()
        engine.advance(10)
    finally:
        engine.close()

    sample = client.post(
        f"/api/projects/{target['id']}/controller/sample",
        json={"robot_id": robot_id, "include_controller": True},
    )
    assert sample.status_code == 200, sample.text
    assert sample.json()["content"].strip()

    from app.services.execution import _bundled_robot_defaults, _detect_project_robot

    detected = _detect_project_robot(store, target["id"])
    assert detected is not None
    assert detected.id == robot_id
    defaults = _bundled_robot_defaults(store, target["id"])
    assert defaults["fix_base"] is True
    assert defaults["locomotion"] is False
    assert defaults["include_body_poses"] is True
    assert defaults["keyframe"] == "STAND"
    assert defaults["integrator"] == "implicitfast"


def test_foundry_project_status_tracks_stage_lifecycle(client):
    """Failed stages mark the project FAILED; success clears it to review."""
    from app.foundry.jobs import JOB_RUNNING_STATUS

    assert JOB_RUNNING_STATUS["analyze"] == "ANALYZING"
    assert JOB_RUNNING_STATUS["reconstruct"] == "RECONSTRUCTING"
    assert JOB_RUNNING_STATUS["assemble"] == "ASSEMBLING"
    assert JOB_RUNNING_STATUS["generate_mjcf"] == "GENERATING"
    assert JOB_RUNNING_STATUS["generate"] == "GENERATING"
    assert JOB_RUNNING_STATUS["validate"] == "VALIDATING"

    foundry = _create(client, name="Lifecycle Bot")
    foundry_id = foundry["id"]
    job = _start(client, foundry_id, "analyze")
    assert job["status"] == "failed"
    detail = client.get(f"/api/foundry/projects/{foundry_id}").json()
    assert detail["project"]["status"] == "FAILED"
    assert "reference image" in (detail["project"]["error_message"] or "")

    _upload(client, foundry_id)
    assert _start(client, foundry_id, "analyze")["status"] == "succeeded"
    detail = client.get(f"/api/foundry/projects/{foundry_id}").json()
    assert detail["project"]["status"] == "NEEDS_REVIEW"
    assert detail["project"]["error_message"] is None


def test_foundry_joint_rejects_unsupported_types(client):
    """Ball/free joints are rejected at edit time, not at generation."""
    foundry = _create(client, name="Joint Type Bot")
    foundry_id = foundry["id"]
    _upload(client, foundry_id)
    assert _start(client, foundry_id, "generate")["status"] == "succeeded"
    detail = client.get(f"/api/foundry/projects/{foundry_id}").json()
    joint_id = detail["joints"][0]["id"]
    for joint_type in ("ball", "free"):
        response = client.patch(
            f"/api/foundry/joints/{joint_id}", json={"joint_type": joint_type}
        )
        assert response.status_code == 422, response.text
    response = client.patch(
        f"/api/foundry/joints/{joint_id}", json={"joint_type": "hinge"}
    )
    assert response.status_code == 200, response.text


def test_foundry_mjcf_copies_microduck_contract(client):
    """Generated MJCF carries the copied MicroDuck simulation contract."""
    import json
    import xml.etree.ElementTree as ET

    foundry = _create(client, name="Contract Bot")
    foundry_id = foundry["id"]
    _upload(client, foundry_id)
    job = _start(client, foundry_id, "generate")
    assert job["status"] == "succeeded", job
    assert job["result"]["generation"]["checks"]["mujoco_compilation"] is True

    model = client.get(f"/api/foundry/projects/{foundry_id}/model").text
    assert 'class="foundry_actuator"' in model
    assert 'name="STAND"' in model
    assert 'name="imu"' in model
    assert 'name="imu_accel"' in model
    assert 'name="root_angmom"' in model
    assert 'timestep="0.005"' in model
    assert "pollen-robotics/microduck_rl" in model

    # STAND qpos must match the model: free root (7) + one slot per joint.
    detail = client.get(f"/api/foundry/projects/{foundry_id}").json()
    actuated = sum(
        1 for joint in detail["joints"] if joint["joint_type"] in ("hinge", "slide")
    )
    key = ET.fromstring(model).find("./keyframe/key[@name='STAND']")
    assert key is not None
    assert len(key.get("qpos", "").split()) == 7 + actuated

    manifest = json.loads(
        (client.app.state.foundry.project_dir(foundry_id) / "manifest.json")
        .read_text(encoding="utf-8")
    )
    assert manifest["simulation_contract"] == "microduck_contract_v1"


def test_foundry_rejects_non_image_upload(client):
    foundry = _create(client)
    response = client.post(
        f"/api/foundry/projects/{foundry['id']}/images",
        files={"files": ("notes.txt", b"not an image", "text/plain")},
    )
    assert response.status_code == 415
