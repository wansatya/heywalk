"""Runtime AI-model settings (AGENTS.md section 6).

Any OpenAI-compatible Chat Completions endpoint works: the model client posts
``{base_url}/chat/completions`` with a JSON-schema response_format. Updates
take effect immediately for the next generate/fix call (handlers build the
client per request from ``app.state.settings``) and are persisted to the
repo .env so a restart keeps them.

The API key is write-only: responses prove a key is set via ``configured``
and ``key_hint`` but never return the key itself.
"""

from __future__ import annotations

import dataclasses

from fastapi import APIRouter, HTTPException, Request

from ..config import (
    Settings,
    default_persist_path,
    model_env_locks,
    persist_dotenv_values,
)
from ..models.schemas import ModelSettingsOut, ModelSettingsUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _redact_key(api_key: str) -> tuple[bool, str]:
    if not api_key:
        return False, ""
    return True, f"••••{api_key[-4:]}" if len(api_key) > 4 else "••••"


def _out(
    settings: Settings, *, persisted: bool = True, persist_path: str = ""
) -> ModelSettingsOut:
    configured, key_hint = _redact_key(settings.model_api_key)
    locks = model_env_locks()
    return ModelSettingsOut(
        base_url=settings.meta_api_base_url,
        model=settings.muse_model,
        configured=configured,
        key_hint=key_hint,
        api_key_from_env=locks["model_api_key"],
        base_url_from_env=locks["meta_api_base_url"],
        model_from_env=locks["muse_model"],
        persist_path=persist_path or settings.dotenv_path,
        persisted=persisted,
    )


def _validated_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    if not value or not value.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=422,
            detail="base_url must be an http(s) URL, "
            "e.g. https://api.example.com/v1.",
        )
    return value


def _validated_model(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise HTTPException(
            status_code=422, detail="model must be a non-empty model name."
        )
    return value


@router.get("/model", response_model=ModelSettingsOut)
def get_model_settings(request: Request) -> ModelSettingsOut:
    return _out(request.app.state.settings)


@router.put("/model", response_model=ModelSettingsOut)
def update_model_settings(
    body: ModelSettingsUpdate, request: Request
) -> ModelSettingsOut:
    current: Settings = request.app.state.settings
    # Validate everything before touching state: a rejected update changes
    # nothing.
    base_url = (
        _validated_base_url(body.base_url)
        if body.base_url is not None
        else current.meta_api_base_url
    )
    model = (
        _validated_model(body.model)
        if body.model is not None
        else current.muse_model
    )
    api_key = body.api_key.strip() if body.api_key is not None else current.model_api_key

    target = default_persist_path(current.dotenv_path)
    persisted = True
    try:
        persist_dotenv_values(
            target,
            {
                "MODEL_API_KEY": api_key,
                "META_API_BASE_URL": base_url,
                "MUSE_MODEL": model,
            },
        )
    except OSError:
        persisted = False

    request.app.state.settings = dataclasses.replace(
        current,
        model_api_key=api_key,
        meta_api_base_url=base_url,
        muse_model=model,
        dotenv_path=str(target),
    )
    return _out(
        request.app.state.settings, persisted=persisted, persist_path=str(target)
    )
