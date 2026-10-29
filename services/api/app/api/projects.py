"""Project, file, and export endpoints."""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone
from typing import Any
from fastapi import APIRouter, HTTPException, Request, Response

from ..db import Store
from ..models.schemas import (
    ProjectBrowseEntry,
    ProjectCreate,
    ProjectFileContent,
    ProjectFileEntry,
    ProjectFileSave,
    ProjectOut,
)
from ..services import files as file_service
from ..services.execution import is_terminal
from ..services.files import (
    FilePathError,
    FileTooLargeError,
    TooManyFilesError,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])

MAX_EXPORT_BYTES = 32 * 1024 * 1024

# Defense in depth: secrets are never stored in the database, but the export
# still redacts credential-shaped keys and any configured secret values.
SECRET_KEY_PATTERN = re.compile(
    r"(?i)\b(model_api_key|api[_-]?key|secret|token|password)\b"
)
REDACTED = "[REDACTED]"


def _scrub(value: Any, secret_values: list[str]) -> Any:
    if isinstance(value, str):
        scrubbed = value
        for secret in secret_values:
            if secret and secret in scrubbed:
                scrubbed = scrubbed.replace(secret, REDACTED)
        return scrubbed
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if SECRET_KEY_PATTERN.search(str(key))
                else _scrub(item, secret_values)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item, secret_values) for item in value]
    return value


def _store(request: Request) -> Store:
    return request.app.state.store


def _project_out(project: dict[str, Any]) -> ProjectOut:
    """ProjectOut with the stored phases parsed (None = default).

    A legacy single dict reads as a one-phase sequence.
    """
    raw = project.get("objective")
    objectives = None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            objectives = [parsed]
        elif isinstance(parsed, list):
            objectives = parsed
    elif isinstance(raw, dict):
        objectives = [raw]
    elif isinstance(raw, list):
        objectives = raw
    return ProjectOut(**{**project, "objectives": objectives})


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, request: Request) -> ProjectOut:
    project = _store(request).create_project(body.name.strip(), body.task_description)
    return _project_out(project)


@router.get("", response_model=list[ProjectOut])
def list_projects(request: Request) -> list[ProjectOut]:
    return [_project_out(p) for p in _store(request).list_projects()]


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, request: Request) -> ProjectOut:
    project = _store(request).get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return _project_out(project)


@router.delete("/{project_id}")
def delete_project(project_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    for run in store.list_runs(project_id):
        if not is_terminal(run["status"]):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Project has a run that is still active;"
                    " stop it before deleting."
                ),
            )
    store.delete_project(project_id)
    return {"id": project_id, "deleted": True}


@router.get("/{project_id}/files", response_model=list[ProjectFileEntry])
def list_files(project_id: str, request: Request) -> list[ProjectFileEntry]:
    store = _store(request)
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return [ProjectFileEntry(**f) for f in store.list_files(project_id)]


@router.get("/{project_id}/browse", response_model=list[ProjectBrowseEntry])
def browse_project(project_id: str, request: Request) -> list[ProjectBrowseEntry]:
    store = _store(request)
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        entries = store.browse_files(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [ProjectBrowseEntry(**entry) for entry in entries]


@router.get("/{project_id}/files/{file_path:path}", response_model=ProjectFileContent)
def read_file(project_id: str, file_path: str, request: Request) -> ProjectFileContent:
    store = _store(request)
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        stored = file_service.read_project_file(store, project_id, file_path)
    except FilePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if stored is None:
        raise HTTPException(status_code=404, detail="File not found.")
    return ProjectFileContent(**stored)


@router.put("/{project_id}/files/{file_path:path}", response_model=ProjectFileContent)
def save_file(
    project_id: str, file_path: str, body: ProjectFileSave, request: Request
) -> ProjectFileContent:
    store = _store(request)
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        stored = file_service.save_project_file(
            store, project_id, file_path, body.content
        )
    except FilePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except TooManyFilesError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ProjectFileContent(**stored)


@router.get("/{project_id}/export")
def export_project(project_id: str, request: Request) -> Response:
    store = _store(request)
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    settings = getattr(request.app.state, "settings", None)
    secret_values = [settings.model_api_key] if settings and settings.model_api_key else []

    # The export IS the project folder: heal any disk gaps from the DB
    # index first, then zip the folder contents at the archive root so the
    # download extracts as a usable program directory.
    for entry in store.list_files(project_id):
        store.get_file(project_id, entry["path"])
    try:
        folder = store.project_dir(project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    versions = {
        entry["path"]: entry["version"]
        for entry in store.list_files(project_id)
    }

    buffer = io.BytesIO()
    total_bytes = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        readme = (
            f"HeyWalk project export: {project['name']}\n"
            f"Task: {project['task_description']}\n"
            "Simulation only -- no hardware validation.\n"
        )
        archive.writestr("README.txt", _scrub(readme, secret_values))
        file_versions = {}
        disk_files = sorted(
            (p for p in folder.rglob("*") if p.is_file() and not p.is_symlink()),
            key=lambda p: p.relative_to(folder).as_posix(),
        )
        for disk in disk_files:
            rel = disk.relative_to(folder).as_posix()
            try:
                content = _scrub(
                    disk.read_text(encoding="utf-8"), secret_values
                )
            except (OSError, UnicodeDecodeError):
                continue
            total_bytes += len(content.encode("utf-8"))
            if total_bytes > MAX_EXPORT_BYTES:
                raise HTTPException(
                    status_code=413, detail="Project export exceeds the size limit."
                )
            archive.writestr(rel, content)
            if rel in versions:
                file_versions[rel] = versions[rel]
        config = _scrub(
            {
                "project_id": project_id,
                "name": project["name"],
                "task_description": project["task_description"],
                "model": settings.muse_model if settings else "unknown",
                "file_versions": file_versions,
                "exported_at": datetime.now(timezone.utc).isoformat(),
            },
            secret_values,
        )
        archive.writestr("config.json", json.dumps(config, indent=2))
        summary = []
        for run in store.list_runs(project_id):
            try:
                world_config = (
                    json.loads(run["world_config"]) if run["world_config"] else None
                )
            except (ValueError, TypeError):
                world_config = None
            summary.append(
                {
                    "id": run["id"],
                    "parent_run_id": run["parent_run_id"],
                    "status": run["status"],
                    "controller_version": run["controller_version"],
                    "seed": run["seed"],
                    "world_config": world_config,
                    "started_at": run["started_at"],
                    "finished_at": run["finished_at"],
                    "exit_code": run["exit_code"],
                    "failure_summary": run["failure_summary"],
                    "metrics": store.get_metrics(run["id"]),
                    "evaluation": store.latest_evaluation(run["id"]),
                }
            )
        archive.writestr(
            "runs.json", json.dumps(_scrub(summary, secret_values), indent=2)
        )
    data = buffer.getvalue()
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="heywalk-{project_id}.zip"'
        },
    )
