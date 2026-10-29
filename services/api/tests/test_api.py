"""REST API integration tests (AGENTS.md section 10)."""

from __future__ import annotations

import io
import time
import zipfile


def _install_robot(client, project_id, robot_id="arm"):
    """Give the project a validated starter robot so MuJoCo runs can start."""
    response = client.post(
        f"/api/projects/{project_id}/robot", json={"robot_id": robot_id}
    )
    assert response.status_code == 200, response.text
    return response.json()



def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_is_also_served_at_the_root(client):
    """/health is the probe path docker-compose and proxies use."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "heywalk-api"


def test_project_crud(client):
    created = client.post(
        "/api/projects", json={"name": "nav", "task_description": "go"}
    )
    assert created.status_code == 201
    project = created.json()
    assert project["name"] == "nav"

    listed = client.get("/api/projects")
    assert listed.status_code == 200
    assert any(p["id"] == project["id"] for p in listed.json())

    fetched = client.get(f"/api/projects/{project['id']}")
    assert fetched.status_code == 200
    assert client.get("/api/projects/missing").status_code == 404


def test_project_delete_removes_project_files_and_runs(client):
    created = client.post(
        "/api/projects", json={"name": "doomed", "task_description": "x"}
    )
    project_id = created.json()["id"]
    _install_robot(client, project_id)
    put = client.put(
        f"/api/projects/{project_id}/files/notes.txt",
        json={"content": "hello"},
    )
    assert put.status_code == 200

    deleted = client.delete(f"/api/projects/{project_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"id": project_id, "deleted": True}

    assert client.get(f"/api/projects/{project_id}").status_code == 404
    assert all(
        p["id"] != project_id for p in client.get("/api/projects").json()
    )
    assert client.get(f"/api/projects/{project_id}/files").status_code == 404
    assert client.get(f"/api/projects/{project_id}/runs").status_code == 404
    assert client.delete("/api/projects/missing").status_code == 404


def test_project_delete_refuses_active_run(client, project_id, settings):
    from app.db import Store

    store = Store(settings.db_path)
    store.create_run(
        project_id,
        parent_run_id=None,
        controller_version=1,
        status="running",
        world_config="{}",
        seed=0,
    )
    refused = client.delete(f"/api/projects/{project_id}")
    assert refused.status_code == 409
    assert "still active" in refused.text
    # The project survives the refused delete.
    assert client.get(f"/api/projects/{project_id}").status_code == 200


def test_file_crud_and_listing(client, project_id):
    put = client.put(
        f"/api/projects/{project_id}/files/controller.py",
        json={"content": "# v1"},
    )
    assert put.status_code == 200
    assert put.json()["version"] == 1

    put2 = client.put(
        f"/api/projects/{project_id}/files/controller.py",
        json={"content": "# v2"},
    )
    assert put2.json()["version"] == 2

    listed = client.get(f"/api/projects/{project_id}/files")
    assert listed.json() == [
        {"path": "controller.py", "version": 2, "updated_at": put2.json()["updated_at"]}
    ]

    read = client.get(f"/api/projects/{project_id}/files/controller.py")
    assert read.json()["content"] == "# v2"
    assert client.get(f"/api/projects/{project_id}/files/nope.py").status_code == 404


def test_file_too_large_rejected(client, project_id):
    response = client.put(
        f"/api/projects/{project_id}/files/big.py",
        json={"content": "x" * (300 * 1024)},
    )
    assert response.status_code in (413, 422)


def test_generate_without_api_key_is_explicit_503(client, project_id):
    response = client.post(f"/api/projects/{project_id}/generate", json={})
    assert response.status_code == 503
    assert "MODEL_API_KEY" in response.json()["detail"]


def test_generate_unknown_project_is_404(client):
    response = client.post("/api/projects/missing/generate", json={})
    assert response.status_code == 404


def _wait_for_terminal(client, run_id, timeout_s=10.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in ("succeeded", "failed", "timeout", "cancelled", "error"):
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach a terminal state")


def test_run_lifecycle_honest_error_without_runner(client, project_id):
    _install_robot(client, project_id)
    created = client.post(
        f"/api/projects/{project_id}/runs", json={"seed": 7}
    )
    assert created.status_code == 201
    run_id = created.json()["id"]
    assert created.json()["status"] == "queued"

    run = _wait_for_terminal(client, run_id)
    # No isolated runner is configured, so the honest outcome is an explicit
    # error -- never fabricated telemetry or success.
    assert run["status"] == "error"
    assert run["failure_summary"]
    assert "NOT executed" in run["failure_summary"]
    assert run["finished_at"] is not None
    assert run["evaluation"] is not None
    assert run["evaluation"]["success"] is False
    assert run["metrics"] is None

    logs = client.get(f"/api/runs/{run_id}/logs").json()
    assert any("Runner error" in line for line in logs)
    assert client.get(f"/api/runs/{run_id}/telemetry").json() == []


def test_run_list_newest_first_and_detail(client, project_id):
    _install_robot(client, project_id)
    first = client.post(f"/api/projects/{project_id}/runs", json={}).json()
    second = client.post(f"/api/projects/{project_id}/runs", json={}).json()
    runs = client.get(f"/api/projects/{project_id}/runs").json()
    assert [r["id"] for r in runs] == [second["id"], first["id"]]
    detail = client.get(f"/api/runs/{first['id']}").json()
    assert detail["project_id"] == project_id
    assert client.get("/api/runs/missing").status_code == 404


def test_cancel_terminal_run_is_noop(client, project_id):
    _install_robot(client, project_id)
    run_id = client.post(f"/api/projects/{project_id}/runs", json={}).json()["id"]
    run = _wait_for_terminal(client, run_id)
    cancelled = client.post(f"/api/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == run["status"]


def test_comparison_uses_parent_or_previous(client, project_id):
    _install_robot(client, project_id)
    first = client.post(f"/api/projects/{project_id}/runs", json={}).json()
    second = client.post(
        f"/api/projects/{project_id}/runs", json={"parent_run_id": first["id"]}
    ).json()
    comparison = client.get(f"/api/runs/{second['id']}/comparison").json()
    assert comparison["current"]["run_id"] == second["id"]
    assert comparison["baseline"]["run_id"] == first["id"]

    lone = client.get(f"/api/runs/{first['id']}/comparison").json()
    assert lone["baseline"] is None
    assert client.get("/api/runs/missing/comparison").status_code == 404


def test_comparison_accepts_explicit_baseline(client, project_id):
    _install_robot(client, project_id)
    first = client.post(f"/api/projects/{project_id}/runs", json={}).json()
    second = client.post(f"/api/projects/{project_id}/runs", json={}).json()
    third = client.post(
        f"/api/projects/{project_id}/runs", json={"parent_run_id": second["id"]}
    ).json()
    # Explicit selection wins over the parent-run default.
    override = client.get(
        f"/api/runs/{third['id']}/comparison",
        params={"baseline_run_id": first["id"]},
    ).json()
    assert override["current"]["run_id"] == third["id"]
    assert override["baseline"]["run_id"] == first["id"]
    # Unknown or foreign baselines fall back to parent-or-previous.
    fallback = client.get(
        f"/api/runs/{third['id']}/comparison",
        params={"baseline_run_id": "missing"},
    ).json()
    assert fallback["baseline"]["run_id"] == second["id"]
    self_baseline = client.get(
        f"/api/runs/{third['id']}/comparison",
        params={"baseline_run_id": third["id"]},
    ).json()
    assert self_baseline["baseline"]["run_id"] == second["id"]


def test_fix_requires_failed_run_and_reports_503(client, project_id):
    _install_robot(client, project_id)
    run_id = client.post(f"/api/projects/{project_id}/runs", json={}).json()["id"]
    run = _wait_for_terminal(client, run_id)
    assert run["status"] == "error"
    response = client.post(
        f"/api/projects/{project_id}/fix", json={"run_id": run_id}
    )
    assert response.status_code == 503
    assert "MODEL_API_KEY" in response.json()["detail"]


def test_fix_rejects_live_run(client, project_id):
    _install_robot(client, project_id)
    # A fresh run is live (or finishing); requesting a fix must not fail with
    # a model error -- the run evidence gate applies first when still live.
    run_id = client.post(f"/api/projects/{project_id}/runs", json={}).json()["id"]
    response = client.post(
        f"/api/projects/{project_id}/fix", json={"run_id": run_id}
    )
    assert response.status_code in (409, 503)


def test_export_contains_files_and_runs_without_secrets(client, project_id):
    _install_robot(client, project_id)
    client.put(
        f"/api/projects/{project_id}/files/controller.py",
        json={"content": "# controller"},
    )
    run_id = client.post(f"/api/projects/{project_id}/runs", json={}).json()["id"]
    _wait_for_terminal(client, run_id)

    response = client.get(f"/api/projects/{project_id}/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert "controller.py" in names
        assert "runs.json" in names
        assert "README.txt" in names
        blob = b"".join(archive.read(n) for n in names)
    assert b"MODEL_API_KEY" not in blob
    assert b"# controller" in blob
    assert run_id.encode() in blob


def test_run_without_a_robot_description_is_a_clear_400(client, project_id):
    """The failure the user hit must arrive as an actionable message."""
    # A controller is present so the run reaches the robot precondition
    # (an empty controller is rejected earlier with its own message).
    client.put(
        f"/api/projects/{project_id}/files/controller.py",
        json={"content": "# controller"},
    )
    response = client.post(f"/api/projects/{project_id}/runs", json={})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "no simulatable robot description" in detail
    assert "GET /api/robots" in detail
    assert "2D differential-drive" in detail
    # The legacy backend still runs on the same project.
    legacy = client.post(
        f"/api/projects/{project_id}/runs",
        json={"world_config": {"backend": "differential_drive"}},
    )
    assert legacy.status_code == 201
    assert legacy.json()["backend"] == "differential_drive"


def test_starter_robot_catalog_is_validated(client):
    response = client.get("/api/robots")
    assert response.status_code == 200
    robots = response.json()
    ids = [robot["id"] for robot in robots]
    assert "microduck" in ids and "arm" in ids
    for robot in robots:
        assert robot["name"] and robot["summary"] and robot["license"]
        assert robot["model_path"] == "robot.xml"
        assert robot["joints"] and robot["tests"]
        assert {test["joint"] for test in robot["tests"]} == {
            joint["name"] for joint in robot["joints"]
        }


def test_install_starter_robot_writes_a_validated_package(client, project_id):
    response = client.post(
        f"/api/projects/{project_id}/robot", json={"robot_id": "microduck"}
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["joints"]) == 14
    assert len(body["tests"]) == 14
    assert body["model_path"] == "robot.xml"

    files = {
        entry["path"]: entry["version"]
        for entry in client.get(f"/api/projects/{project_id}/files").json()
    }
    assert set(files) == {
        "controller.py",
        "robot.xml",
        "robot.urdf",
        "ros2_control.yaml",
    }
    urdf = client.get(f"/api/projects/{project_id}/files/robot.urdf").json()["content"]
    assert "left_hip_yaw" in urdf

    # Reinstalling replaces files with a new version instead of losing them.
    again = client.post(
        f"/api/projects/{project_id}/robot", json={"robot_id": "arm"}
    )
    assert again.status_code == 200
    assert len(again.json()["joints"]) == 2
    versions = {
        entry["path"]: entry["version"]
        for entry in client.get(f"/api/projects/{project_id}/files").json()
    }
    assert versions["controller.py"] == 2
    assert versions["robot.xml"] == 2


def test_install_unknown_starter_robot_is_404(client, project_id):
    response = client.post(
        f"/api/projects/{project_id}/robot", json={"robot_id": "nope"}
    )
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]
    missing = client.post("/api/projects/missing/robot", json={"robot_id": "arm"})
    assert missing.status_code == 404


def test_install_without_controller_leaves_an_empty_controller(client, project_id):
    """Fresh experiments install the robot with a blank controller.py."""
    response = client.post(
        f"/api/projects/{project_id}/robot",
        json={"robot_id": "microduck", "include_controller": False},
    )
    assert response.status_code == 200
    assert len(response.json()["joints"]) == 14
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
    content = client.get(f"/api/projects/{project_id}/files/controller.py").json()[
        "content"
    ]
    assert content == ""

    # Reinstalling without the controller never wipes user code.
    client.put(
        f"/api/projects/{project_id}/files/controller.py",
        json={"content": "# mine"},
    )
    again = client.post(
        f"/api/projects/{project_id}/robot",
        json={"robot_id": "microduck", "include_controller": False},
    )
    assert again.status_code == 200
    kept = client.get(f"/api/projects/{project_id}/files/controller.py").json()[
        "content"
    ]
    assert kept == "# mine"


def test_import_sample_controller_writes_and_resets_objective(client, project_id):
    """Importing the sample writes controller.py and restores flag grading."""
    _install_robot(client, project_id, robot_id="microduck")
    response = client.post(
        f"/api/projects/{project_id}/controller/sample",
        json={"robot_id": "microduck"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["path"] == "controller.py"
    assert "starter" in body["content"].lower()
    project = client.get(f"/api/projects/{project_id}").json()
    assert project["objectives"] is None
    read = client.get(f"/api/projects/{project_id}/files/controller.py").json()
    assert read["content"] == body["content"]
    missing = client.post(
        f"/api/projects/{project_id}/controller/sample", json={"robot_id": "nope"}
    )
    assert missing.status_code == 404


def test_mujoco_run_with_an_empty_controller_is_rejected(client, project_id):
    """Start fails fast with a fix path instead of dying in the runner."""
    installed = client.post(
        f"/api/projects/{project_id}/robot",
        json={"robot_id": "arm", "include_controller": False},
    )
    assert installed.status_code == 200
    created = client.post(f"/api/projects/{project_id}/runs", json={"seed": 1})
    assert created.status_code == 400
    assert "empty" in created.json()["detail"]

    client.put(
        f"/api/projects/{project_id}/files/controller.py",
        json={"content": "# controller"},
    )
    created = client.post(f"/api/projects/{project_id}/runs", json={"seed": 1})
    assert created.status_code == 201


def test_run_world_config_merges_defaults(client, project_id):
    # The 2D arena backend is the legacy second backend; ask for it explicitly
    # because new runs default to MuJoCo.
    created = client.post(
        f"/api/projects/{project_id}/runs",
        json={
            "seed": 7,
            "world_config": {"time_limit": None, "backend": "differential_drive"},
        },
    )
    assert created.status_code == 201
    world = created.json()["world_config"]
    assert world["time_limit"] is None
    assert world["width"] == 10.0
    assert world["goal"]["x"] == 8.5
    assert len(world["obstacles"]) == 2


def test_new_runs_default_to_the_mujoco_backend(client, project_id):
    _install_robot(client, project_id)
    created = client.post(
        f"/api/projects/{project_id}/runs", json={"seed": 7}
    )
    assert created.status_code == 201
    body = created.json()
    assert body["backend"] == "mujoco"
    world = body["world_config"]
    assert world["backend"] == "mujoco"
    assert world["model_path"] == "robot.xml"
    # The legacy 2D arena defaults stay out of a MuJoCo scenario.
    assert "goal" not in world and "obstacles" not in world


def test_generate_502_carries_structured_diagnosis(client, project_id, monkeypatch):
    from app.services import meta_model as meta_module
    from app.services.meta_model import MetaModelResponseError

    class _BoomClient:
        model_name = "test-boom"

        async def generate_controller(self, context):
            raise MetaModelResponseError(
                "Model API returned an empty assistant message."
            )

        async def propose_fix(self, evidence):
            raise MetaModelResponseError("unused")

    monkeypatch.setattr(
        meta_module, "build_client", lambda settings: _BoomClient()
    )
    response = client.post(f"/api/projects/{project_id}/generate", json={})
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["message"] == "Model API returned an empty assistant message."
    assert "transient" in detail["hint"]
    stages = {step["stage"]: step["status"] for step in detail["trace"]}
    assert stages == {"prepare_prompt": "ok", "model_request": "error"}
    history = client.get(f"/api/projects/{project_id}/model-requests").json()
    assert len(history) == 1
    assert history[0]["status"] == "error"
    assert history[0]["model_name"] == "test-boom"


def test_model_requests_unknown_project_is_404(client):
    assert client.get("/api/projects/missing/model-requests").status_code == 404


def test_microduck_mesh_assets_are_cacheable(client):
    # The 3D view downloads ~21 MB of STLs; without cache headers every
    # simulator load re-fetches them and crowds API calls off the
    # browser's per-origin connections on slow links.
    response = client.get("/api/robots/microduck/assets/sole_left.stl")
    assert response.status_code == 200
    assert response.headers["content-type"] == "model/stl"
    assert response.headers["cache-control"] == "public, max-age=86400"
    assert (
        client.get("/api/robots/microduck/assets/no_such_mesh.stl").status_code
        == 404
    )
