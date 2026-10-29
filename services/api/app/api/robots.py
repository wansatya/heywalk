"""Bundled starter robot endpoints (AGENTS.md sections 5 and 7).

A project needs a simulatable robot description before a MuJoCo run can do
anything. These endpoints let the user install a platform-owned robot without
asking the model for one, which is also the recovery path when generation
never produced a usable MJCF.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ..db import Store
from ..models.schemas import (
    ProjectFileContent,
    RobotPackageOut,
    StarterRobotInstall,
    StarterRobotOut,
)
from ..services import generation as generation_service
from ..services.files import FilePathError, FileTooLargeError, TooManyFilesError
from ..services.generation import ProjectNotFoundError
from ..simulator.robot_catalog import (
    UnknownStarterRobotError,
    starter_robot_summary,
    starter_robots,
)
from ..simulator.robot_description import RobotPackageError

router = APIRouter(tags=["robots"])


@router.get("/api/robots", response_model=list[StarterRobotOut])
def list_starter_robots() -> list[StarterRobotOut]:
    return [
        StarterRobotOut(**starter_robot_summary(robot)) for robot in starter_robots()
    ]


@router.get("/api/robots/microduck/model")
def microduck_model() -> dict:
    """Official MicroDuck model metadata + load status for the UI.

    Returns joints, actuators, sensors, body hierarchy, physics parameters,
    visual mesh placements, the STAND preview pose, the composed
    environment (obstacles + goal), and whether the vendored meshes
    resolve. The 3D view poses meshes from live telemetry (or the STAND
    preview before a run streams); the robot panel uses the status for its
    loading indicator.
    """
    from ..simulator.microduck import (
        MicroduckModelError,
        body_hierarchy,
        environment_payload,
        load_metadata,
        missing_assets,
        stand_poses,
        visual_geoms,
    )

    try:
        metadata = load_metadata()
        hierarchy = body_hierarchy()
        visuals = visual_geoms()
        preview = stand_poses()
        environment = environment_payload()
    except MicroduckModelError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    missing = missing_assets()
    payload = metadata.to_dict()
    payload["hierarchy"] = hierarchy
    payload["visual_geoms"] = visuals
    payload["default_poses"] = preview
    payload["environment"] = environment
    payload["assets_ok"] = not missing
    payload["missing_assets"] = list(missing)
    if missing:
        payload["error"] = (
            f"{len(missing)} mesh asset(s) are missing from the bundle;"
            " reinstall the application."
        )
    return payload


@router.get("/api/robots/microduck/assets/{filename}")
def microduck_asset(filename: str) -> FileResponse:
    """Serve one vendored MicroDuck mesh (STL) to the 3D view.

    Only mesh files the official model references are served; anything
    else is a 404, so this endpoint can never leak arbitrary host files.
    """
    from ..simulator.microduck import MicroduckModelError, resolve_asset

    try:
        path = resolve_asset(filename)
    except MicroduckModelError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # Vendored meshes (~21 MB over ~38 files) are stable per release, so let
    # browsers cache them: without this every simulator load re-downloads all
    # meshes, saturating the browser's per-origin connection pool and
    # starving API calls behind the bulk download on slow links.
    return FileResponse(
        path,
        media_type="model/stl",
        filename=filename,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.post(
    "/api/projects/{project_id}/controller/sample",
    response_model=ProjectFileContent,
)
def import_sample_controller(
    project_id: str, body: StarterRobotInstall, request: Request
) -> ProjectFileContent:
    """Write the bundled sample controller and reset to default grading."""
    store: Store = request.app.state.store
    try:
        saved = generation_service.import_sample_controller(
            store, project_id, body.robot_id
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    except UnknownStarterRobotError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ProjectFileContent(**saved)


@router.post(
    "/api/projects/{project_id}/robot", response_model=RobotPackageOut
)
def install_starter_robot(
    project_id: str, body: StarterRobotInstall, request: Request
) -> RobotPackageOut:
    store: Store = request.app.state.store
    try:
        return generation_service.install_starter_robot(
            store, project_id, body.robot_id,
            include_controller=body.include_controller,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found.") from exc
    except UnknownStarterRobotError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RobotPackageError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Bundled starter robot is not simulatable: {exc}",
        ) from exc
    except (FilePathError, FileTooLargeError, TooManyFilesError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
