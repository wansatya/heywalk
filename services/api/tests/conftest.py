"""Shared pytest fixtures: isolated SQLite DB + TestClient per test."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        db_path=str(tmp_path / "test.db"),
        model_api_key="",
        meta_api_base_url="https://api.meta.ai/v1",
        muse_model="muse-spark-1.3",
        model_timeout_s=5.0,
        cors_origins=("*",),
    )


@pytest.fixture()
def client(settings):
    app = create_app(db_path=settings.db_path, settings=settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def project_id(client):
    response = client.post(
        "/api/projects", json={"name": "demo", "task_description": "reach goal"}
    )
    assert response.status_code == 201
    return response.json()["id"]
