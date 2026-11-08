"""Backend .env credential loading.

The backend must always refer to the repo .env for credentials: an explicit
environment variable wins, .env fills the gaps, and unknown keys are ignored.
Values are merged inside load_settings without mutating os.environ.
"""

from __future__ import annotations

import os

from app.config import load_settings


def test_load_settings_reads_dotenv_for_creds(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "MODEL_API_KEY=test-key-from-dotenv\n"
        "SAM31_API_KEY=sam-key-from-dotenv\n"
        "MUSE_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    for key in ("MODEL_API_KEY", "SAM31_API_KEY", "MUSE_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HEYWALK_DOTENV", str(dotenv))
    monkeypatch.delenv("HEYWALK_NO_DOTENV", raising=False)
    settings = load_settings()
    assert settings.model_api_key == "test-key-from-dotenv"
    assert settings.sam_api_key == "sam-key-from-dotenv"
    assert settings.muse_model == "dotenv-model"
    assert settings.dotenv_path == str(dotenv)


def test_explicit_environment_wins_over_dotenv(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("MODEL_API_KEY=dotenv-key\n", encoding="utf-8")
    monkeypatch.setenv("HEYWALK_DOTENV", str(dotenv))
    monkeypatch.setenv("MODEL_API_KEY", "explicit-key")
    monkeypatch.delenv("HEYWALK_NO_DOTENV", raising=False)
    assert load_settings().model_api_key == "explicit-key"


def test_dotenv_ignores_unknown_keys_and_strips_quotes(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "UNRELATED_SECRET=should-not-import\n"
        'MODEL_API_KEY="quoted key"\n'
        "export SAM31_MODEL='sam-quoted'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HEYWALK_DOTENV", str(dotenv))
    monkeypatch.delenv("HEYWALK_NO_DOTENV", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("SAM31_MODEL", raising=False)
    settings = load_settings()
    assert settings.model_api_key == "quoted key"
    assert settings.sam_model == "sam-quoted"
    assert "UNRELATED_SECRET" not in os.environ


def test_dotenv_opt_out_uses_environment_only(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("MODEL_API_KEY=dotenv-key\n", encoding="utf-8")
    monkeypatch.setenv("HEYWALK_DOTENV", str(dotenv))
    monkeypatch.setenv("HEYWALK_NO_DOTENV", "1")
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    settings = load_settings()
    assert settings.model_api_key == ""
    assert settings.dotenv_path == ""
