"""Runtime AI-model settings: view/update API URL, model, and key via REST.

The API key is write-only: GET responses prove a key is set (configured +
key_hint) but never return the key itself.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def dotenv_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# HeyWalk test env\nMODEL_API_KEY=sk-test-1234\n", encoding="utf-8"
    )
    return path


@pytest.fixture()
def settings_client(tmp_path, dotenv_file):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        model_api_key="sk-test-1234",
        meta_api_base_url="https://api.meta.ai/v1",
        muse_model="muse-spark-1.3",
        model_timeout_s=5.0,
        cors_origins=("*",),
        foundry_root=str(tmp_path / "foundry_robots"),
        dotenv_path=str(dotenv_file),
    )
    app = create_app(db_path=settings.db_path, settings=settings)
    with TestClient(app) as test_client:
        yield test_client


def test_get_model_settings_redacts_key(settings_client):
    response = settings_client.get("/api/settings/model")
    assert response.status_code == 200
    body = response.json()
    assert body["base_url"] == "https://api.meta.ai/v1"
    assert body["model"] == "muse-spark-1.3"
    assert body["configured"] is True
    assert body["key_hint"].endswith("1234")
    assert "sk-test-1234" not in response.text


def test_get_model_settings_unconfigured(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        model_api_key="",
        meta_api_base_url="https://api.meta.ai/v1",
        muse_model="muse-spark-1.3",
        model_timeout_s=5.0,
        cors_origins=("*",),
    )
    app = create_app(db_path=settings.db_path, settings=settings)
    with TestClient(app) as test_client:
        response = test_client.get("/api/settings/model")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["key_hint"] == ""


def test_put_model_settings_updates_runtime_and_persists(
    settings_client, dotenv_file
):
    response = settings_client.put(
        "/api/settings/model",
        json={
            "base_url": "https://api.example.com/v1/",
            "model": "custom-model-2",
            "api_key": "sk-new-9999",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["base_url"] == "https://api.example.com/v1"
    assert body["model"] == "custom-model-2"
    assert body["configured"] is True
    assert body["key_hint"].endswith("9999")
    assert "sk-new-9999" not in response.text

    # Effective without restart: a generate now attempts a live call (502
    # from the unreachable test endpoint) instead of 503 not-configured.
    project = settings_client.post(
        "/api/projects", json={"name": "demo", "task_description": "go"}
    )
    assert project.status_code == 201
    generate = settings_client.post(
        f"/api/projects/{project.json()['id']}/generate", json={}
    )
    assert generate.status_code == 502

    # Persisted to .env with comments preserved.
    saved = dotenv_file.read_text(encoding="utf-8")
    assert "# HeyWalk test env" in saved
    assert "MODEL_API_KEY=sk-new-9999" in saved
    assert "META_API_BASE_URL=https://api.example.com/v1" in saved
    assert "MUSE_MODEL=custom-model-2" in saved


def test_put_model_settings_partial_update_keeps_key(settings_client):
    response = settings_client.put(
        "/api/settings/model", json={"model": "only-model-changed"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "only-model-changed"
    assert body["configured"] is True
    assert body["key_hint"].endswith("1234")
    assert body["base_url"] == "https://api.meta.ai/v1"


def test_put_model_settings_empty_key_clears(settings_client):
    response = settings_client.put("/api/settings/model", json={"api_key": ""})
    assert response.status_code == 200
    assert response.json()["configured"] is False

    project = settings_client.post(
        "/api/projects", json={"name": "demo", "task_description": "go"}
    )
    generate = settings_client.post(
        f"/api/projects/{project.json()['id']}/generate", json={}
    )
    assert generate.status_code == 503


def test_put_model_settings_rejects_bad_values(settings_client):
    assert (
        settings_client.put(
            "/api/settings/model", json={"base_url": "ftp://x/y"}
        ).status_code
        == 422
    )
    assert (
        settings_client.put(
            "/api/settings/model", json={"base_url": ""}
        ).status_code
        == 422
    )
    assert (
        settings_client.put(
            "/api/settings/model", json={"model": "  "}
        ).status_code
        == 422
    )
    # Rejected updates change nothing.
    body = settings_client.get("/api/settings/model").json()
    assert body["base_url"] == "https://api.meta.ai/v1"
    assert body["model"] == "muse-spark-1.3"


def test_get_model_settings_reports_env_overrides(
    settings_client, monkeypatch
):
    monkeypatch.setenv("MODEL_API_KEY", "sk-from-env")
    body = settings_client.get("/api/settings/model").json()
    assert body["api_key_from_env"] is True
    assert body["base_url_from_env"] is False
    assert body["model_from_env"] is False
