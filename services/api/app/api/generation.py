"""Controller generation and correction endpoints (AGENTS.md section 6)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..db import Store
from ..models.schemas import (
    FixRequest,
    FixResult,
    GenerateRequest,
    GenerationResult,
    ModelRequestOut,
)
from ..services import generation as generation_service
from ..services.experiments import FixBudgetExhaustedError
from ..services.files import (
    FilePathError,
    FileTooLargeError,
    TooManyFilesError,
)
from ..services.generation import ProjectNotFoundError, RunNotReadyError
from ..services.meta_model import MetaModelNotConfiguredError, MetaModelError

router = APIRouter(prefix="/api/projects", tags=["generation"])


def _error_detail(
    exc: BaseException,
    *,
    message: str | None = None,
    hint: str | None = None,
) -> dict:
    """Structured 502 body so the UI can explain what happened and why."""
    return {
        "message": message or str(exc),
        "hint": hint or getattr(exc, "hint", None),
        "trace": getattr(exc, "trace", None) or [],
    }


@router.post("/{project_id}/generate", response_model=GenerationResult)
async def generate_controller(
    project_id: str, body: GenerateRequest, request: Request
) -> GenerationResult:
    store: Store = request.app.state.store
    try:
        return await generation_service.handle_generate(
            store, request.app.state.settings, project_id, body.task_description
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    except MetaModelNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MetaModelError as exc:
        raise HTTPException(status_code=502, detail=_error_detail(exc)) from exc
    except (FilePathError, FileTooLargeError, TooManyFilesError) as exc:
        raise HTTPException(
            status_code=502,
            detail=_error_detail(
                exc,
                message=f"Model returned an unusable file: {exc}",
                hint=(
                    "No files were written — the project is unchanged. "
                    "Retry; the model sometimes emits bad paths."
                ),
            ),
        ) from exc


@router.get("/{project_id}/model-requests", response_model=list[ModelRequestOut])
def list_model_requests(project_id: str, request: Request) -> list[ModelRequestOut]:
    store: Store = request.app.state.store
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return [ModelRequestOut(**row) for row in store.list_model_requests(project_id)]


@router.post("/{project_id}/fix", response_model=FixResult)
async def request_fix(
    project_id: str, body: FixRequest, request: Request
) -> FixResult:
    store: Store = request.app.state.store
    try:
        return await generation_service.handle_fix(
            store, request.app.state.settings, project_id, body.run_id
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Run not found.") from exc
    except RunNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FixBudgetExhaustedError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except MetaModelNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except MetaModelError as exc:
        raise HTTPException(status_code=502, detail=_error_detail(exc)) from exc
    except (FilePathError, FileTooLargeError, TooManyFilesError) as exc:
        raise HTTPException(
            status_code=502,
            detail=_error_detail(
                exc,
                message=f"Model returned an unusable file: {exc}",
                hint=(
                    "No files were written — the project is unchanged. "
                    "Retry; the model sometimes emits bad paths."
                ),
            ),
        ) from exc
