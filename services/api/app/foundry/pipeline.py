"""Foundry generation orchestrator.

Stages are kept separate and callable independently so a failed or
unsatisfying stage can be re-run without restarting the pipeline:

``analyze -> reconstruct -> assemble -> generate_mjcf -> validate -> simulate``

Each public method is synchronous and reports progress through a callback;
the API runs them on :class:`~app.foundry.jobs.FoundryJobManager`.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image

from ..config import Settings
from ..db import Store, new_id, utcnow_iso
from ..simulator.foundry_robot import stamp_foundry_marker
from . import physics
from .assembly import CATEGORY_HEIGHT_M, build_assembly, placements_to_rows
from .imaging import Detection, load_rgb, mask_png_bytes, save_component_source
from .mjcf import MjcfError, build_mjcf
from .package import (
    build_package_files,
    install_foundry_robot,
    write_artifacts,
)
from .reconstruction import (
    ReconstructionSettings,
    reconstruct_components,
)
from .segmentation import (
    SegmentationSettings,
    resolve_sam_endpoint,
    segment_image,
)
from .store import FoundryStore, slug
from .template_microduck import TIMESTEP as TEMPLATE_TIMESTEP
from .validation import validate_mjcf

Progress = Callable[[int, str], None]


class FoundryPipelineError(ValueError):
    """A stage cannot run with the project's current artifacts."""


@dataclass
class FoundryRuntime:
    foundry: FoundryStore
    sequences: Store
    settings: Settings

    @property
    def segmentation_settings(self) -> SegmentationSettings:
        api_key = (
            self.settings.sam_api_key.strip()
            or self.settings.model_api_key.strip()
        )
        explicit = self.settings.sam_endpoint.strip()
        if explicit:
            endpoint = resolve_sam_endpoint(explicit)
        elif api_key:
            endpoint = resolve_sam_endpoint(self.settings.meta_api_base_url)
        else:
            endpoint = ""
        return SegmentationSettings(
            endpoint=endpoint,
            api_key=api_key,
            timeout_s=self.settings.sam_timeout_s,
            model=self.settings.sam_model,
        )

    @property
    def reconstruction_settings(self) -> ReconstructionSettings:
        return ReconstructionSettings(
            endpoint=self.settings.image3d_endpoint,
            api_key=self.settings.image3d_api_key,
            timeout_s=self.settings.image3d_timeout_s,
        )


def _notify(progress: Progress | None, value: int, message: str) -> None:
    if progress is not None:
        progress(value, message)


def _project_or_error(runtime: FoundryRuntime, foundry_id: str) -> dict[str, Any]:
    project = runtime.foundry.get_project(foundry_id)
    if project is None:
        raise FoundryPipelineError(f"Foundry project {foundry_id!r} not found.")
    return project


def _analysis_metadata(project: dict[str, Any]) -> dict[str, Any]:
    metadata = project.get("metadata") or {}
    analysis = metadata.get("analysis")
    return analysis if isinstance(analysis, dict) else {}


def _reference_image(runtime: FoundryRuntime, foundry_id: str) -> dict[str, Any]:
    images = runtime.foundry.list_images(foundry_id)
    if not images:
        raise FoundryPipelineError(
            "Upload at least one reference image before analyzing the robot."
        )
    # Prefer an explicit reference, otherwise the first upload.
    for image in images:
        if image.get("image_type") == "reference":
            return image
    return images[0]


def _image_path(runtime: FoundryRuntime, foundry_id: str, image: dict[str, Any]) -> Path:
    return runtime.foundry.resolve_asset(foundry_id, image["file_url"])


def _foreground_box(detections: list[Detection]) -> tuple[int, int, int, int]:
    boxes = [detection.box for detection in detections]
    if not boxes:
        return (0, 0, 1, 1)
    left = min(box[0] for box in boxes)
    top = min(box[1] for box in boxes)
    right = max(box[0] + box[2] for box in boxes)
    bottom = max(box[1] + box[3] for box in boxes)
    return (left, top, max(1, right - left), max(1, bottom - top))


def _unique_name(base: str, used: set[str]) -> str:
    candidate = slug(base, "part")
    if candidate not in used:
        used.add(candidate)
        return candidate
    index = 2
    while f"{candidate}_{index}" in used:
        index += 1
    name = f"{candidate}_{index}"
    used.add(name)
    return name


def _component_rows(
    *,
    foundry_id: str,
    detections: list[Detection],
    analysis: dict[str, Any],
    image: dict[str, Any],
    root: Path,
    rgb: np.ndarray,
) -> list[dict[str, Any]]:
    used: set[str] = set()
    rows: list[dict[str, Any]] = []
    for detection in detections:
        name = _unique_name(detection.label, used)
        component_id = new_id()
        component_dir = root / "components" / name
        component_dir.mkdir(parents=True, exist_ok=True)
        mask_path = f"components/{name}/mask.png"
        source_path = f"components/{name}/source.png"
        (root / mask_path).write_bytes(mask_png_bytes(detection.mask))
        save_component_source(
            rgb, detection.mask, detection.box, str(root / source_path)
        )
        rows.append(
            {
                "id": component_id,
                "parent_id": None,
                "name": name,
                "label": detection.label,
                "component_type": detection.component_type,
                "mesh_url": None,
                "collision_mesh_url": None,
                "confidence": detection.confidence,
                "metadata": {
                    "box": list(detection.box),
                    "image_size": list(analysis.get("image_size") or rgb.shape[1::-1]),
                    "foreground_box": list(analysis.get("foreground_box") or []),
                    "scale_m_per_px": analysis.get("scale_m_per_px"),
                    "source_image_id": image["id"],
                    "source_image": image["file_url"],
                    "mask_path": mask_path,
                    "source_path": source_path,
                    "segmentation_method": detection.method,
                    "segmentation": detection.to_dict(),
                },
            }
        )
    return rows


def analyze(
    runtime: FoundryRuntime,
    foundry_id: str,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Segment the reference image and store reviewable components."""
    project = _project_or_error(runtime, foundry_id)
    image = _reference_image(runtime, foundry_id)
    _notify(progress, 5, "Loading reference image…")
    path = _image_path(runtime, foundry_id, image)
    rgb, size = load_rgb(str(path))
    category = project.get("category") or "biped"
    _notify(progress, 20, "Running segmentation…")
    detections, metadata = segment_image(
        str(path),
        category=category,
        settings=runtime.segmentation_settings,
    )
    if not detections:
        raise FoundryPipelineError(
            "Segmentation found no components. Try a clearer image with the"
            " whole robot visible."
        )
    foreground = _foreground_box(detections)
    scale = CATEGORY_HEIGHT_M.get(category.lower(), 1.4) / max(1, foreground[3])
    analysis = {
        **metadata,
        "image_size": list(size),
        "foreground_box": list(foreground),
        "scale_m_per_px": scale,
        "source_image_id": image["id"],
        "source_image": image["file_url"],
        "analyzed_at": utcnow_iso(),
    }
    _notify(progress, 45, "Saving component masks…")
    root = runtime.foundry.project_dir(foundry_id)
    rows = _component_rows(
        foundry_id=foundry_id,
        detections=detections,
        analysis=analysis,
        image=image,
        root=root,
        rgb=rgb,
    )
    runtime.foundry.replace_components(foundry_id, rows)
    project_metadata = dict(project.get("metadata") or {})
    project_metadata["analysis"] = analysis
    runtime.foundry.update_project(
        foundry_id,
        metadata_json=project_metadata,
        thumbnail_url=image["file_url"],
        status="NEEDS_REVIEW",
        error_message=None,
    )
    _notify(progress, 100, f"Found {len(rows)} components.")
    return {
        "method": metadata.get("method", "unknown"),
        "provider_configured": metadata.get("provider_configured", False),
        "provider_error": metadata.get("provider_error"),
        "components": len(rows),
        "foreground_box": list(foreground),
        "image_size": list(size),
        "warning": metadata.get("provider_error"),
    }


def _load_detections(
    runtime: FoundryRuntime,
    foundry_id: str,
    project: dict[str, Any],
    components: list[dict[str, Any]],
) -> tuple[np.ndarray, tuple[int, int], tuple[int, int, int, int], dict[str, Detection]]:
    analysis = _analysis_metadata(project)
    image = _reference_image(runtime, foundry_id)
    path = _image_path(runtime, foundry_id, image)
    rgb, size = load_rgb(str(path))
    raw_foreground = analysis.get("foreground_box")
    if isinstance(raw_foreground, (list, tuple)) and len(raw_foreground) == 4:
        foreground = tuple(int(value) for value in raw_foreground)  # type: ignore[assignment]
    else:
        foreground = (0, 0, size[0], size[1])
    root = runtime.foundry.project_dir(foundry_id)
    detections: dict[str, Detection] = {}
    for component in components:
        metadata = component.get("metadata") or {}
        mask_path = metadata.get("mask_path")
        if not mask_path:
            continue
        try:
            with Image.open(root / mask_path) as mask_image:
                mask = np.asarray(mask_image.convert("L"), dtype=np.uint8) > 127
        except OSError:
            continue
        if mask.shape != (size[1], size[0]):
            mask_image = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
            mask = (
                np.asarray(
                    mask_image.resize(size, Image.Resampling.NEAREST), dtype=np.uint8
                )
                > 127
            )
        raw_box = metadata.get("box")
        if isinstance(raw_box, (list, tuple)) and len(raw_box) == 4:
            box = tuple(int(value) for value in raw_box)  # type: ignore[assignment]
        else:
            ys, xs = np.nonzero(mask)
            box = (
                int(xs.min()) if len(xs) else 0,
                int(ys.min()) if len(ys) else 0,
                max(1, int(xs.max() - xs.min() + 1)) if len(xs) else size[0],
                max(1, int(ys.max() - ys.min() + 1)) if len(ys) else size[1],
            )
        detections[component["id"]] = Detection(
            label=component.get("label") or component["name"],
            component_type=component.get("component_type") or "other",
            confidence=float(component.get("confidence") or 0.5),
            box=box,
            mask=mask,
            method=str(metadata.get("segmentation_method") or "heuristic_v1"),
        )
    return rgb, size, foreground, detections


def reconstruct(
    runtime: FoundryRuntime,
    foundry_id: str,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Generate OBJ/STL/GLB files for every component."""
    project = _project_or_error(runtime, foundry_id)
    components = runtime.foundry.list_components(foundry_id)
    if not components:
        raise FoundryPipelineError(
            "Analyze the reference image before reconstructing 3D geometry."
        )
    _notify(progress, 5, "Loading masks…")
    rgb, size, foreground, detections = _load_detections(
        runtime, foundry_id, project, components
    )
    if not detections:
        raise FoundryPipelineError(
            "No component masks were found. Re-run analysis to regenerate them."
        )
    analysis = _analysis_metadata(project)
    category = project.get("category") or "biped"
    root = runtime.foundry.project_dir(foundry_id)
    image = _reference_image(runtime, foundry_id)
    image_bytes = _image_path(runtime, foundry_id, image).read_bytes()
    _notify(progress, 12, "Reconstructing component meshes…")
    meshes = reconstruct_components(
        root=root,
        components=components,
        detections=detections,
        rgb=rgb,
        image_size=size,
        foreground_box=foreground,
        category=category,
        settings=runtime.reconstruction_settings,
        image_bytes=image_bytes,
        progress=lambda value, message: _notify(
            progress, 12 + int(value * 0.78), message
        ),
    )
    _notify(progress, 92, "Recording mesh metadata…")
    for component in components:
        mesh = meshes.get(component["id"])
        if mesh is None:
            continue
        metadata = dict(component.get("metadata") or {})
        metadata.update(mesh.metadata)
        metadata["mesh"] = mesh.to_dict(root)
        runtime.foundry.update_component(
            component["id"],
            mesh_url=mesh.to_dict(root)["visual_url"],
            collision_mesh_url=mesh.to_dict(root)["collision_url"],
            metadata_json=metadata,
        )
    updated = runtime.foundry.list_components(foundry_id)
    physics.apply_physics(updated)
    for component in updated:
        runtime.foundry.update_component(
            component["id"], metadata_json=component.get("metadata") or {}
        )
    runtime.foundry.update_project(foundry_id, status="NEEDS_REVIEW")
    _notify(progress, 100, f"Reconstructed {len(meshes)} components.")
    return {
        "components": len(meshes),
        "methods": sorted({mesh.method for mesh in meshes.values()}),
    }


def assemble(
    runtime: FoundryRuntime,
    foundry_id: str,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Build the hierarchy, joints, actuators, and physics estimates."""
    project = _project_or_error(runtime, foundry_id)
    components = runtime.foundry.list_components(foundry_id)
    if not components:
        raise FoundryPipelineError("Analyze the reference image before assembling.")
    if not all(component.get("mesh_url") for component in components):
        raise FoundryPipelineError(
            "Reconstruct 3D geometry before assembling the robot."
        )
    analysis = _analysis_metadata(project)
    _notify(progress, 15, "Inferring the articulated hierarchy…")
    raw_foreground = analysis.get("foreground_box")
    foreground = None
    if isinstance(raw_foreground, (list, tuple)) and len(raw_foreground) == 4:
        foreground = tuple(int(value) for value in raw_foreground)
    raw_size = analysis.get("image_size")
    image_size = None
    if isinstance(raw_size, (list, tuple)) and len(raw_size) == 2:
        image_size = (int(raw_size[0]), int(raw_size[1]))
    placements = build_assembly(
        components,
        category=project.get("category") or "biped",
        image_size=image_size,
        foreground_box=foreground,
    )
    _notify(progress, 55, "Writing joints and actuator defaults…")
    updated, joints, actuators = placements_to_rows(placements, existing=components)
    physics.apply_physics(updated)
    for component in updated:
        runtime.foundry.update_component(
            component["id"],
            parent_id=component.get("parent_id"),
            metadata_json=component.get("metadata") or {},
        )
    runtime.foundry.replace_joints(foundry_id, joints)
    runtime.foundry.replace_actuators(foundry_id, actuators)
    runtime.foundry.update_project(foundry_id, status="NEEDS_REVIEW")
    _notify(progress, 100, f"Assembled {len(updated)} bodies.")
    return {
        "components": len(updated),
        "joints": len(joints),
        "actuated_joints": sum(
            1 for joint in joints if joint.get("joint_type") in ("hinge", "slide")
        ),
        "actuators": len(actuators),
    }


def _package_inputs(
    runtime: FoundryRuntime, foundry_id: str
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    project = _project_or_error(runtime, foundry_id)
    components = runtime.foundry.list_components(foundry_id)
    joints = runtime.foundry.list_joints(foundry_id)
    actuators = runtime.foundry.list_actuators(foundry_id)
    return project, components, joints, actuators


def generate_mjcf(
    runtime: FoundryRuntime,
    foundry_id: str,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Generate robot.xml, validate it, and write the package artifacts."""
    project, components, joints, actuators = _package_inputs(runtime, foundry_id)
    if not components:
        raise FoundryPipelineError("Analyze and reconstruct the robot first.")
    if not all(component.get("mesh_url") for component in components):
        raise FoundryPipelineError(
            "Some components have no mesh. Re-run reconstruction before"
            " generating the MJCF."
        )
    if not joints:
        _notify(progress, 5, "No assembly yet; assembling first…")
        assemble(runtime, foundry_id, progress=lambda _v, m: _notify(progress, 5, m))
        project, components, joints, actuators = _package_inputs(
            runtime, foundry_id
        )
    _notify(progress, 20, "Generating MJCF…")
    try:
        model = build_mjcf(
            components=components,
            joints=joints,
            actuators=actuators,
            name=project["name"],
            timestep=TEMPLATE_TIMESTEP,
        )
    except MjcfError as exc:
        raise FoundryPipelineError(str(exc)) from exc
    # Mark the model as a Foundry robot so the simulator can recognize it from
    # the installed MJCF alone (robot switcher, detection, run defaults).
    xml = stamp_foundry_marker(model.xml, foundry_id)
    root = runtime.foundry.project_dir(foundry_id)
    (root / "robot.xml").write_text(xml, encoding="utf-8")
    _notify(progress, 45, "Running static checks and MuJoCo compile…")
    validation = validate_mjcf(
        root,
        xml,
        timeout_s=runtime.settings.foundry_validation_timeout_s,
    )
    latest = runtime.foundry.latest_version(foundry_id)
    version = (int(latest["version"]) + 1) if latest else 1
    files = build_package_files(
        foundry_project=project,
        components=components,
        joints=joints,
        actuators=actuators,
        xml=xml,
        validation=validation,
        version=version,
    )
    _notify(progress, 75, "Writing package files…")
    write_artifacts(runtime.foundry, foundry_id, files)
    runtime.foundry.add_version(
        foundry_id,
        mjcf_url="robot.xml",
        manifest_url="manifest.json",
        validation=validation,
    )
    status = "READY" if validation["status"] in ("ok", "warning") else "FAILED"
    error_message = None
    if validation["status"] == "error":
        error_message = "; ".join(validation.get("errors") or [])[:2000]
    runtime.foundry.update_project(
        foundry_id,
        status=status,
        error_message=error_message,
    )
    _notify(progress, 100, "MJCF generated and validated.")
    return {
        "status": validation["status"],
        "checks": validation["checks"],
        "warnings": validation["warnings"],
        "errors": validation["errors"],
        "joints": list(model.joint_names),
        "bodies": list(model.body_names),
        "mesh_files": list(model.mesh_files),
        "version": version,
    }


def validate(
    runtime: FoundryRuntime,
    foundry_id: str,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Re-run validation on the latest generated model."""
    project = _project_or_error(runtime, foundry_id)
    latest = runtime.foundry.latest_version(foundry_id)
    root = runtime.foundry.project_dir(foundry_id)
    model_path = root / (latest["mjcf_url"] if latest and latest.get("mjcf_url") else "robot.xml")
    if not model_path.is_file():
        raise FoundryPipelineError(
            "No generated MJCF found. Generate the model first."
        )
    _notify(progress, 25, "Validating robot.xml…")
    xml = model_path.read_text(encoding="utf-8")
    stamped = stamp_foundry_marker(xml, foundry_id)
    if stamped != xml:
        model_path.write_text(stamped, encoding="utf-8")
        xml = stamped
    validation = validate_mjcf(
        root,
        xml,
        timeout_s=runtime.settings.foundry_validation_timeout_s,
    )
    if latest is not None:
        runtime.foundry.update_latest_version_validation(foundry_id, validation)
    status = "READY" if validation["status"] in ("ok", "warning") else "FAILED"
    runtime.foundry.update_project(
        foundry_id,
        status=status,
        error_message=(
            "; ".join(validation.get("errors") or [])[:2000]
            if validation["status"] == "error"
            else None
        ),
    )
    _notify(progress, 100, "Validation complete.")
    return validation


def simulate(
    runtime: FoundryRuntime,
    foundry_id: str,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Create a sequence and install the generated robot into it.

    The robot itself already lives in the simulator robot library
    (``foundry_root``). "Open in simulator" is the explicit point where a
    normal sequence is created, exactly like installing a bundled robot.
    """
    project = _project_or_error(runtime, foundry_id)
    latest = runtime.foundry.latest_version(foundry_id)
    root = runtime.foundry.project_dir(foundry_id)
    if latest is None or not (root / "robot.xml").is_file():
        _notify(progress, 5, "No package yet; generating it now…")
        generate_mjcf(runtime, foundry_id, progress)
        latest = runtime.foundry.latest_version(foundry_id)
    _notify(progress, 40, "Refreshing the robot package…")
    _project, components, joints, actuators = _package_inputs(runtime, foundry_id)
    xml = (root / "robot.xml").read_text(encoding="utf-8")
    stamped = stamp_foundry_marker(xml, foundry_id)
    if stamped != xml:
        (root / "robot.xml").write_text(stamped, encoding="utf-8")
        xml = stamped
    validation = (latest or {}).get("validation") or {}
    files = build_package_files(
        foundry_project=project,
        components=components,
        joints=joints,
        actuators=actuators,
        xml=xml,
        validation=validation,
        version=(latest or {}).get("version"),
    )
    write_artifacts(runtime.foundry, foundry_id, files)
    _notify(progress, 65, "Creating a sequence for the simulator…")
    sequence = runtime.sequences.create_project(
        f"{project['name']} lab", project.get("description") or ""
    )
    installed = install_foundry_robot(
        runtime.sequences,
        sequence["id"],
        runtime.foundry,
        foundry_id,
        include_controller=True,
        return_copy=True,
    )
    runtime.foundry.update_project(
        foundry_id, status="READY", error_message=None
    )
    _notify(progress, 100, "Robot is ready in the simulator.")
    return {
        "foundry_id": foundry_id,
        "project_id": sequence["id"],
        "version": (latest or {}).get("version"),
        "validation_status": validation.get("status"),
        "text_files": installed.get("text_files", []),
        "binary_files": installed.get("binary_files", []),
    }


def run_full(
    runtime: FoundryRuntime,
    foundry_id: str,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Analyze, reconstruct, assemble, generate, and validate in one job."""
    analyzed = analyze(runtime, foundry_id, lambda v, m: _notify(progress, int(v * 0.2), m))
    reconstructed = reconstruct(
        runtime, foundry_id, lambda v, m: _notify(progress, 20 + int(v * 0.35), m)
    )
    assembled = assemble(
        runtime, foundry_id, lambda v, m: _notify(progress, 55 + int(v * 0.2), m)
    )
    generated = generate_mjcf(
        runtime, foundry_id, lambda v, m: _notify(progress, 75 + int(v * 0.25), m)
    )
    return {
        "analysis": analyzed,
        "reconstruction": reconstructed,
        "assembly": assembled,
        "generation": generated,
    }


__all__ = [
    "FoundryPipelineError",
    "FoundryRuntime",
    "analyze",
    "assemble",
    "generate_mjcf",
    "reconstruct",
    "run_full",
    "simulate",
    "validate",
]
