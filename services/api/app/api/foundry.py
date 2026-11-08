"""Robot Foundry REST API.

The API is intentionally thin: uploads and CRUD touch the store directly,
while the generation stages run as background jobs so the UI can poll and
resume after a failure. Generated files are served from the Foundry artifact
folder; nothing outside it is reachable.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from ..db import Store
from ..foundry import pipeline
from ..foundry.jobs import FoundryJobManager
from ..foundry.segmentation import png_bytes_for_upload
from ..foundry.store import FoundryStore, slug
from ..models.foundry import (
    FoundryActuatorOut,
    FoundryActuatorUpdate,
    FoundryComponentCreate,
    FoundryComponentOut,
    FoundryComponentUpdate,
    FoundryImageOut,
    FoundryJobOut,
    FoundryJobStartOut,
    FoundryJointOut,
    FoundryJointUpdate,
    FoundryPreview,
    FoundryProjectCreate,
    FoundryProjectDetail,
    FoundryProjectOut,
    FoundryProjectUpdate,
    FoundrySimulateOut,
    FoundryVersionOut,
)

router = APIRouter(prefix="/api/foundry", tags=["foundry"])

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGES_PER_PROJECT = 8
IMAGE_EXTENSIONS = {"PNG", "JPEG", "WEBP"}


def _foundry(request: Request) -> FoundryStore:
    return request.app.state.foundry


def _sequences(request: Request) -> Store:
    return request.app.state.store


def _jobs(request: Request) -> FoundryJobManager:
    return request.app.state.foundry_jobs


def _runtime(request: Request) -> pipeline.FoundryRuntime:
    return pipeline.FoundryRuntime(
        foundry=_foundry(request),
        sequences=_sequences(request),
        settings=request.app.state.settings,
    )


def _asset_url(foundry_id: str, relative: str | None) -> str | None:
    if not relative:
        return None
    return f"/api/foundry/projects/{foundry_id}/assets/{relative.lstrip('/')}"


def _project_or_404(request: Request, foundry_id: str) -> dict[str, Any]:
    project = _foundry(request).get_project(foundry_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Foundry project not found.")
    return project


def _project_out(store: FoundryStore, project: dict[str, Any]) -> FoundryProjectOut:
    foundry_id = project["id"]
    components = store.list_components(foundry_id)
    joints = store.list_joints(foundry_id)
    latest = store.latest_version(foundry_id)
    validation = (latest or {}).get("validation") or {}
    return FoundryProjectOut(
        **project,
        thumbnail_asset_url=_asset_url(foundry_id, project.get("thumbnail_url")),
        component_count=len(components),
        joint_count=len(joints),
        actuated_joint_count=sum(
            1 for joint in joints if joint.get("joint_type") in ("hinge", "slide")
        ),
        latest_version=(latest or {}).get("version"),
        validation_status=validation.get("status"),
    )


def _image_out(foundry_id: str, image: dict[str, Any]) -> FoundryImageOut:
    return FoundryImageOut(
        **image,
        asset_url=_asset_url(foundry_id, image.get("file_url")),
    )


def _component_out(foundry_id: str, component: dict[str, Any]) -> FoundryComponentOut:
    metadata = component.get("metadata") or {}
    mesh = metadata.get("mesh") if isinstance(metadata.get("mesh"), dict) else {}
    preview = mesh.get("preview_url")
    if not preview:
        name = component.get("name")
        candidate = (
            _foundry_dir_for_component(foundry_id, name) if name else None
        )
        preview = candidate
    return FoundryComponentOut(
        **component,
        visual_asset_url=_asset_url(foundry_id, component.get("mesh_url")),
        collision_asset_url=_asset_url(
            foundry_id, component.get("collision_mesh_url")
        ),
        preview_asset_url=_asset_url(foundry_id, preview),
    )


def _foundry_dir_for_component(foundry_id: str, name: str) -> str | None:
    return f"assets/preview/{name}.glb"


def _joint_out(joint: dict[str, Any]) -> FoundryJointOut:
    return FoundryJointOut(**joint)


def _actuator_out(actuator: dict[str, Any]) -> FoundryActuatorOut:
    return FoundryActuatorOut(**actuator)


def _job_out(job: dict[str, Any]) -> FoundryJobOut:
    return FoundryJobOut(**job)


def _detail(request: Request, foundry_id: str) -> FoundryProjectDetail:
    store = _foundry(request)
    project = _project_or_404(request, foundry_id)
    images = store.list_images(foundry_id)
    return FoundryProjectDetail(
        project=_project_out(store, project),
        images=[_image_out(foundry_id, image) for image in images],
        components=[
            _component_out(foundry_id, component)
            for component in store.list_components(foundry_id)
        ],
        joints=[_joint_out(joint) for joint in store.list_joints(foundry_id)],
        actuators=[
            _actuator_out(actuator)
            for actuator in store.list_actuators(foundry_id)
        ],
        jobs=[_job_out(job) for job in store.list_jobs(foundry_id)],
        versions=[
            FoundryVersionOut(**version)
            for version in store.list_versions(foundry_id)
        ],
        analysis=(project.get("metadata") or {}).get("analysis"),
    )


def _start_job(
    request: Request,
    foundry_id: str,
    job_type: str,
    fn: Callable[[pipeline.Progress], dict[str, Any]],
) -> FoundryJobStartOut:
    try:
        job = _jobs(request).submit(foundry_id, job_type, fn)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FoundryJobStartOut(job=_job_out(job))


# ---- projects -------------------------------------------------------------


@router.post("/projects", response_model=FoundryProjectOut, status_code=201)
def create_project(body: FoundryProjectCreate, request: Request) -> FoundryProjectOut:
    foundry = _foundry(request)
    # A robot build is a simulator robot package folder, not a sequence. The
    # sequence is created later, only when the user opens the robot in the
    # simulator (or installs it through the switch-robot catalog).
    project = foundry.create_project(
        name=body.name.strip(),
        description=body.description.strip(),
        category=body.category.strip() or "biped",
    )
    return _project_out(foundry, project)


@router.get("/projects", response_model=list[FoundryProjectOut])
def list_projects(request: Request) -> list[FoundryProjectOut]:
    store = _foundry(request)
    return [_project_out(store, project) for project in store.list_projects()]


@router.get("/by-sequence/{project_id}", response_model=FoundryProjectOut)
def get_by_sequence(project_id: str, request: Request) -> FoundryProjectOut:
    """Foundry project linked to a normal sequence, if any.

    The IDE uses this to detect that the open sequence is a generated robot
    and to route Start through the Foundry joint-test scenario instead of the
    bundled-robot defaults.
    """
    store = _foundry(request)
    project = store.get_by_sequence(project_id)
    if project is None:
        raise HTTPException(
            status_code=404, detail="No Foundry project is linked to this sequence."
        )
    return _project_out(store, project)


@router.get("/projects/{foundry_id}", response_model=FoundryProjectDetail)
def get_project(foundry_id: str, request: Request) -> FoundryProjectDetail:
    return _detail(request, foundry_id)


@router.patch("/projects/{foundry_id}", response_model=FoundryProjectOut)
def update_project(
    foundry_id: str, body: FoundryProjectUpdate, request: Request
) -> FoundryProjectOut:
    store = _foundry(request)
    _project_or_404(request, foundry_id)
    fields = body.model_dump(exclude_none=True)
    project = store.update_project(foundry_id, **fields)
    assert project is not None
    return _project_out(store, project)


@router.delete("/projects/{foundry_id}")
def delete_project(foundry_id: str, request: Request) -> dict[str, Any]:
    store = _foundry(request)
    _project_or_404(request, foundry_id)
    active = store.active_job(foundry_id)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail="A Foundry job is still running; wait for it to finish.",
        )
    # Deleting a robot removes its simulator package folder. Sequences where
    # it was installed keep their own copies and are not touched.
    store.delete_project(foundry_id)
    return {"id": foundry_id, "deleted": True}


# ---- images ---------------------------------------------------------------


@router.post(
    "/projects/{foundry_id}/images",
    response_model=list[FoundryImageOut],
    status_code=201,
)
async def upload_images(
    foundry_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    image_type: str = Form("reference"),
) -> list[FoundryImageOut]:
    store = _foundry(request)
    project = _project_or_404(request, foundry_id)
    existing = store.list_images(foundry_id)
    if len(existing) + len(files) > MAX_IMAGES_PER_PROJECT:
        raise HTTPException(
            status_code=413,
            detail=f"At most {MAX_IMAGES_PER_PROJECT} reference images per project.",
        )
    root = store.project_dir(foundry_id)
    created: list[dict[str, Any]] = []
    for upload in files:
        data = await upload.read()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"{upload.filename or 'image'} exceeds the 12 MiB limit.",
            )
        try:
            png, _ext, width, height = png_bytes_for_upload(data)
        except Exception as exc:
            raise HTTPException(
                status_code=415,
                detail=f"{upload.filename or 'image'} is not a readable PNG/JPG/WEBP image: {exc}",
            ) from exc
        from ..db import new_id

        image_id = new_id()
        relative = f"images/{image_id}.png"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(png)
        created.append(
            store.add_image(
                foundry_id,
                filename=upload.filename or f"{image_id}.png",
                file_url=relative,
                image_type=image_type if image_type in ("reference", "detail") else "reference",
                width=width,
                height=height,
                analysis_status="pending",
            )
        )
    if project.get("status") in ("DRAFT", "READY", "FAILED"):
        store.update_project(foundry_id, status="DRAFT", error_message=None)
    return [_image_out(foundry_id, image) for image in created]


@router.get("/projects/{foundry_id}/images", response_model=list[FoundryImageOut])
def list_images(foundry_id: str, request: Request) -> list[FoundryImageOut]:
    _project_or_404(request, foundry_id)
    return [
        _image_out(foundry_id, image)
        for image in _foundry(request).list_images(foundry_id)
    ]


# ---- components / joints / actuators -------------------------------------


@router.get(
    "/projects/{foundry_id}/components", response_model=list[FoundryComponentOut]
)
def list_components(foundry_id: str, request: Request) -> list[FoundryComponentOut]:
    _project_or_404(request, foundry_id)
    return [
        _component_out(foundry_id, component)
        for component in _foundry(request).list_components(foundry_id)
    ]


@router.post(
    "/projects/{foundry_id}/components",
    response_model=FoundryComponentOut,
    status_code=201,
)
def add_component(
    foundry_id: str, body: FoundryComponentCreate, request: Request
) -> FoundryComponentOut:
    store = _foundry(request)
    _project_or_404(request, foundry_id)
    components = store.list_components(foundry_id)
    used = {component["name"] for component in components}
    name = slug(body.label, "part")
    if name in used:
        index = 2
        while f"{name}_{index}" in used:
            index += 1
        name = f"{name}_{index}"
    metadata: dict[str, Any] = {}
    if body.box is not None:
        if len(body.box) != 4:
            raise HTTPException(status_code=400, detail="box must hold [x, y, w, h].")
        metadata["box"] = body.box
    if body.dimensions is not None:
        if len(body.dimensions) != 3:
            raise HTTPException(
                status_code=400, detail="dimensions must hold [x, y, z]."
            )
        metadata["dimensions"] = body.dimensions
        metadata["estimated"] = False
    component = store.create_component(
        foundry_id,
        name=name,
        label=body.label,
        component_type=body.component_type,
        parent_id=body.parent_id,
        metadata=metadata,
        confidence=body.confidence,
    )
    store.update_project(foundry_id, status="NEEDS_REVIEW")
    return _component_out(foundry_id, component)


@router.patch("/components/{component_id}", response_model=FoundryComponentOut)
def update_component(
    component_id: str, body: FoundryComponentUpdate, request: Request
) -> FoundryComponentOut:
    store = _foundry(request)
    component = store.get_component(component_id)
    if component is None:
        raise HTTPException(status_code=404, detail="Component not found.")
    fields: dict[str, Any] = {}
    metadata = dict(component.get("metadata") or {})
    if body.metadata is not None:
        metadata.update(body.metadata)
    if body.name is not None:
        fields["name"] = slug(body.name, "part")
    if body.label is not None:
        fields["label"] = body.label
    if body.component_type is not None:
        fields["component_type"] = body.component_type
    if body.parent_id is not None:
        if body.parent_id == component_id:
            raise HTTPException(
                status_code=400, detail="A component cannot be its own parent."
            )
        parent = store.get_component(body.parent_id)
        if parent is None or parent["project_id"] != component["project_id"]:
            raise HTTPException(status_code=400, detail="Parent component not found.")
        # Reject cycles by walking up from the proposed parent.
        seen = {body.parent_id}
        cursor = parent
        while cursor and cursor.get("parent_id"):
            if cursor["parent_id"] == component_id:
                raise HTTPException(
                    status_code=400, detail="That parent would create a cycle."
                )
            if cursor["parent_id"] in seen:
                break
            seen.add(cursor["parent_id"])
            cursor = store.get_component(cursor["parent_id"])
        fields["parent_id"] = body.parent_id
    if body.confidence is not None:
        fields["confidence"] = body.confidence
    if body.dimensions is not None:
        if len(body.dimensions) != 3:
            raise HTTPException(
                status_code=400, detail="dimensions must hold [x, y, z]."
            )
        metadata["dimensions"] = [max(0.001, float(value)) for value in body.dimensions]
        metadata["dimensions_source"] = "user"
    if body.mass_kg is not None or body.friction is not None:
        physics = dict(metadata.get("physics") or {})
        if body.mass_kg is not None:
            physics["mass_kg"] = float(body.mass_kg)
        if body.friction is not None:
            physics["friction"] = float(body.friction)
        physics["estimated"] = False
        physics["user_confirmed"] = True
        metadata["physics"] = physics
    if body.visible is not None:
        metadata["visible"] = bool(body.visible)
    if metadata != (component.get("metadata") or {}):
        fields["metadata_json"] = metadata
    updated = store.update_component(component_id, **fields)
    assert updated is not None
    store.update_project(component["project_id"], status="NEEDS_REVIEW")
    return _component_out(component["project_id"], updated)


@router.delete("/components/{component_id}")
def delete_component(component_id: str, request: Request) -> dict[str, Any]:
    store = _foundry(request)
    component = store.get_component(component_id)
    if component is None:
        raise HTTPException(status_code=404, detail="Component not found.")
    store.delete_component(component_id)
    store.update_project(component["project_id"], status="NEEDS_REVIEW")
    return {"id": component_id, "deleted": True}


@router.get("/projects/{foundry_id}/joints", response_model=list[FoundryJointOut])
def list_joints(foundry_id: str, request: Request) -> list[FoundryJointOut]:
    _project_or_404(request, foundry_id)
    return [_joint_out(joint) for joint in _foundry(request).list_joints(foundry_id)]


@router.patch("/joints/{joint_id}", response_model=FoundryJointOut)
def update_joint(
    joint_id: str, body: FoundryJointUpdate, request: Request
) -> FoundryJointOut:
    store = _foundry(request)
    joint = store.get_joint(joint_id)
    if joint is None:
        raise HTTPException(status_code=404, detail="Joint not found.")
    fields: dict[str, Any] = {}
    if body.name is not None:
        fields["name"] = slug(body.name, "joint")
    if body.joint_type is not None:
        fields["joint_type"] = body.joint_type
    if body.axis is not None:
        if len(body.axis) != 3:
            raise HTTPException(status_code=400, detail="axis must hold [x, y, z].")
        fields["axis"] = [float(value) for value in body.axis]
    if body.position is not None:
        if len(body.position) != 3:
            raise HTTPException(
                status_code=400, detail="position must hold [x, y, z]."
            )
        fields["position"] = [float(value) for value in body.position]
    if body.limits is not None:
        if len(body.limits) != 2 or float(body.limits[1]) <= float(body.limits[0]):
            raise HTTPException(
                status_code=400, detail="limits must be [lower, upper] with upper > lower."
            )
        fields["limits"] = [float(body.limits[0]), float(body.limits[1])]
    if body.parent_component_id is not None:
        fields["parent_component_id"] = body.parent_component_id
    if body.metadata is not None:
        fields["metadata_json"] = body.metadata
    updated = store.update_joint(joint_id, **fields)
    assert updated is not None
    store.update_project(joint["project_id"], status="NEEDS_REVIEW")
    return _joint_out(updated)


@router.get(
    "/projects/{foundry_id}/actuators", response_model=list[FoundryActuatorOut]
)
def list_actuators(foundry_id: str, request: Request) -> list[FoundryActuatorOut]:
    _project_or_404(request, foundry_id)
    return [
        _actuator_out(actuator)
        for actuator in _foundry(request).list_actuators(foundry_id)
    ]


@router.patch("/actuators/{actuator_id}", response_model=FoundryActuatorOut)
def update_actuator(
    actuator_id: str, body: FoundryActuatorUpdate, request: Request
) -> FoundryActuatorOut:
    store = _foundry(request)
    actuator = store.get_actuator(actuator_id)
    if actuator is None:
        raise HTTPException(status_code=404, detail="Actuator not found.")
    configuration = dict(actuator.get("configuration") or {})
    if body.configuration is not None:
        configuration.update(body.configuration)
    for key in ("kp", "kv", "gear"):
        value = getattr(body, key)
        if value is not None:
            configuration[key] = float(value)
    for key in ("ctrlrange", "forcerange"):
        value = getattr(body, key)
        if value is not None:
            if len(value) != 2:
                raise HTTPException(
                    status_code=400, detail=f"{key} must hold two numbers."
                )
            configuration[key] = [float(value[0]), float(value[1])]
    fields: dict[str, Any] = {"configuration_json": configuration}
    if body.name is not None:
        fields["name"] = slug(body.name, "motor")
    if body.actuator_type is not None:
        fields["actuator_type"] = body.actuator_type
    updated = store.update_actuator(actuator_id, **fields)
    assert updated is not None
    store.update_project(actuator["project_id"], status="NEEDS_REVIEW")
    return _actuator_out(updated)


# ---- jobs / stages --------------------------------------------------------


@router.get("/jobs/{job_id}", response_model=FoundryJobOut)
def get_job(job_id: str, request: Request) -> FoundryJobOut:
    job = _foundry(request).get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return _job_out(job)


@router.post(
    "/projects/{foundry_id}/analyze", response_model=FoundryJobStartOut, status_code=202
)
def analyze(foundry_id: str, request: Request) -> FoundryJobStartOut:
    _project_or_404(request, foundry_id)
    runtime = _runtime(request)
    return _start_job(
        request,
        foundry_id,
        "analyze",
        lambda progress: pipeline.analyze(runtime, foundry_id, progress),
    )


@router.post(
    "/projects/{foundry_id}/reconstruct",
    response_model=FoundryJobStartOut,
    status_code=202,
)
def reconstruct(foundry_id: str, request: Request) -> FoundryJobStartOut:
    _project_or_404(request, foundry_id)
    runtime = _runtime(request)
    return _start_job(
        request,
        foundry_id,
        "reconstruct",
        lambda progress: pipeline.reconstruct(runtime, foundry_id, progress),
    )


@router.post(
    "/projects/{foundry_id}/assemble",
    response_model=FoundryJobStartOut,
    status_code=202,
)
def assemble(foundry_id: str, request: Request) -> FoundryJobStartOut:
    _project_or_404(request, foundry_id)
    runtime = _runtime(request)
    return _start_job(
        request,
        foundry_id,
        "assemble",
        lambda progress: pipeline.assemble(runtime, foundry_id, progress),
    )


@router.post(
    "/projects/{foundry_id}/generate-mjcf",
    response_model=FoundryJobStartOut,
    status_code=202,
)
def generate_mjcf(foundry_id: str, request: Request) -> FoundryJobStartOut:
    _project_or_404(request, foundry_id)
    runtime = _runtime(request)
    return _start_job(
        request,
        foundry_id,
        "generate_mjcf",
        lambda progress: pipeline.generate_mjcf(runtime, foundry_id, progress),
    )


@router.post(
    "/projects/{foundry_id}/generate",
    response_model=FoundryJobStartOut,
    status_code=202,
)
def generate_all(foundry_id: str, request: Request) -> FoundryJobStartOut:
    """Run analyze -> reconstruct -> assemble -> generate in one job."""
    _project_or_404(request, foundry_id)
    runtime = _runtime(request)
    return _start_job(
        request,
        foundry_id,
        "generate",
        lambda progress: pipeline.run_full(runtime, foundry_id, progress),
    )


@router.post(
    "/projects/{foundry_id}/validate",
    response_model=FoundryJobStartOut,
    status_code=202,
)
def validate(foundry_id: str, request: Request) -> FoundryJobStartOut:
    _project_or_404(request, foundry_id)
    runtime = _runtime(request)
    return _start_job(
        request,
        foundry_id,
        "validate",
        lambda progress: pipeline.validate(runtime, foundry_id, progress),
    )


@router.post(
    "/projects/{foundry_id}/simulate", response_model=FoundrySimulateOut
)
def simulate(foundry_id: str, request: Request) -> FoundrySimulateOut:
    _project_or_404(request, foundry_id)
    runtime = _runtime(request)
    try:
        result = pipeline.simulate(runtime, foundry_id)
    except pipeline.FoundryPipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Could not prepare the simulator: {exc}"
        ) from exc
    return FoundrySimulateOut(**result)


# ---- preview / assets / export -------------------------------------------


@router.get("/projects/{foundry_id}/preview", response_model=FoundryPreview)
def preview(foundry_id: str, request: Request) -> FoundryPreview:
    store = _foundry(request)
    project = _project_or_404(request, foundry_id)
    return FoundryPreview(
        project_id=project["project_id"],
        foundry_id=foundry_id,
        name=project["name"],
        status=project["status"],
        components=[
            _component_out(foundry_id, component)
            for component in store.list_components(foundry_id)
        ],
        joints=[_joint_out(joint) for joint in store.list_joints(foundry_id)],
        actuators=[
            _actuator_out(actuator)
            for actuator in store.list_actuators(foundry_id)
        ],
        analysis=(project.get("metadata") or {}).get("analysis"),
    )


@router.get("/projects/{foundry_id}/versions", response_model=list[FoundryVersionOut])
def list_versions(foundry_id: str, request: Request) -> list[FoundryVersionOut]:
    _project_or_404(request, foundry_id)
    return [
        FoundryVersionOut(**version)
        for version in _foundry(request).list_versions(foundry_id)
    ]


@router.get("/projects/{foundry_id}/model")
def get_model(foundry_id: str, request: Request) -> Response:
    _project_or_404(request, foundry_id)
    path = _foundry(request).project_dir(foundry_id) / "robot.xml"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No generated MJCF yet.")
    return Response(
        content=path.read_text(encoding="utf-8"),
        media_type="application/xml",
    )


@router.get("/projects/{foundry_id}/export")
def export_package(foundry_id: str, request: Request) -> Response:
    from ..foundry.package import package_name, package_zip

    store = _foundry(request)
    project = _project_or_404(request, foundry_id)
    try:
        data = package_zip(store, foundry_id)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    filename = f"{package_name(project)}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/projects/{foundry_id}/assets/{asset_path:path}")
def get_asset(foundry_id: str, asset_path: str, request: Request) -> FileResponse:
    store = _foundry(request)
    _project_or_404(request, foundry_id)
    try:
        path = store.resolve_asset(foundry_id, asset_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Asset not found.")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        headers={"Cache-Control": "public, max-age=3600"},
    )
