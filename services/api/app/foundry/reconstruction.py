"""3D reconstruction stage.

This stage turns a segmented 2D mask into an actual mesh file. It is a
separate module from segmentation and assembly so a learned image-to-3D
model can replace the default outline extrusion without touching the rest of
the pipeline.

Default method (``outline_extrusion_v1``): the mask outline is scaled to
metres and extruded to a reviewable solid. A single image cannot reveal
depth or hidden surfaces, so the depth is an estimate and every component's
metadata records that fact. When ``IMAGE_TO_3D_ENDPOINT`` is configured the
provider is called first and its mesh is validated before use.
"""

from __future__ import annotations

import base64
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .imaging import (
    Detection,
    extrude_polygon_mesh,
    mask_outline_points,
    mask_png_bytes,
    save_component_source,
)

DEFAULT_DENSITY_KG_M3 = 420.0
#: Fraction of the bounding box a robot part is assumed to fill. Hollow
#: frames, joints and electronics mean a solid box overestimates mass.
DEFAULT_FILL_FACTOR = 0.38
#: Minimum depth so a very thin visible part still becomes a solid mesh.
MIN_DEPTH_M = 0.02
MAX_DEPTH_M = 0.6


@dataclass(frozen=True)
class ReconstructionSettings:
    """Optional learned image-to-3D provider configuration."""

    endpoint: str = ""
    api_key: str = ""
    timeout_s: float = 120.0

    @property
    def configured(self) -> bool:
        return bool(self.endpoint.strip())


@dataclass
class ComponentMesh:
    """Generated geometry for one component."""

    name: str
    visual_obj: Path
    collision_stl: Path
    preview_glb: Path
    dimensions: tuple[float, float, float]
    method: str
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, root: Path) -> dict[str, Any]:
        return {
            "visual_url": self.visual_obj.relative_to(root).as_posix(),
            "collision_url": self.collision_stl.relative_to(root).as_posix(),
            "preview_url": self.preview_glb.relative_to(root).as_posix(),
            "dimensions": [round(value, 6) for value in self.dimensions],
            "method": self.method,
            "confidence": round(float(self.confidence), 3),
            "metadata": dict(self.metadata),
        }


def _scale_for_category(category: str, foreground_height_px: float) -> float:
    """Metres per pixel from a per-category height prior."""
    category = (category or "biped").lower()
    target_height = {
        # Product default: a MicroDuck-style biped (~0.30 m tall).
        "biped": 0.30,
        "humanoid": 1.55,
        "wheeled": 0.65,
        "vehicle": 0.65,
        "rover": 0.65,
        "arm": 1.0,
        "manipulator": 1.0,
    }.get(category, 1.4)
    return target_height / max(1.0, foreground_height_px)


def _estimated_depth(width_m: float, height_m: float) -> float:
    depth = min(width_m, height_m) * 0.55
    return max(MIN_DEPTH_M, min(MAX_DEPTH_M, depth))


def _component_points(
    detection: Detection, scale: float, box: tuple[int, int, int, int]
) -> np.ndarray:
    """Mask outline in metres, centred on the component, y-up image -> z-up."""
    outline = mask_outline_points(detection.mask)
    if len(outline) < 3:
        x, y, w, h = box
        outline = np.array(
            [
                [x, y],
                [x + w, y],
                [x + w, y + h],
                [x, y + h],
            ],
            dtype=float,
        )
    cx = box[0] + box[2] / 2.0
    cy = box[1] + box[3] / 2.0
    points = np.column_stack(
        ((outline[:, 0] - cx) * scale, (cy - outline[:, 1]) * scale)
    )
    return points


def _export_mesh(mesh: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(str(path))


def _provider_mesh(
    image_bytes: bytes,
    detection: Detection,
    settings: ReconstructionSettings,
    size: tuple[int, int],
) -> Any:
    """Fetch an image-to-3D mesh from the configured provider.

    The provider contract is deliberately simple: it returns OBJ or GLB bytes
    (base64) or a ``mesh`` URL. The mesh is imported and re-exported by
    trimesh so unsupported provider encodings are rejected here, not by
    MuJoCo later.
    """
    import httpx
    import trimesh

    width, height = size
    x, y, w, h = detection.box
    payload = {
        "image": base64.b64encode(image_bytes).decode("ascii"),
        "mask": base64.b64encode(
            np.packbits(detection.mask, axis=None).tobytes()
        ).decode("ascii"),
        "box": [x, y, w, h],
        "image_size": [width, height],
        "label": detection.label,
        "format": "obj",
    }
    headers = {"Content-Type": "application/json"}
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    with httpx.Client(timeout=settings.timeout_s) as client:
        response = client.post(settings.endpoint, json=payload, headers=headers)
    response.raise_for_status()
    body = response.json()
    raw = body.get("mesh") or body.get("mesh_base64")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Image-to-3D response has no mesh payload.")
    encoded = raw.split(",", 1)[-1]
    data = base64.b64decode(encoded)
    suffix = ".glb" if data[:4] == b"glTF" else ".obj"
    loaded = trimesh.load(io_bytes(data, suffix), force="mesh")
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise ValueError("Image-to-3D payload is not a usable triangle mesh.")
    return loaded


def io_bytes(data: bytes, suffix: str):
    """Small ``trimesh`` file object helper (kept import-light)."""
    import io

    stream = io.BytesIO(data)
    stream.name = f"provider{suffix}"
    return stream


def reconstruct_component(
    *,
    root: Path,
    component_id: str,
    name: str,
    detection: Detection,
    rgb: np.ndarray,
    image_size: tuple[int, int],
    scale: float,
    category: str,
    settings: ReconstructionSettings | None = None,
    image_bytes: bytes | None = None,
) -> ComponentMesh:
    """Build visual/collision/preview files for one component."""
    box = detection.box
    width_m = max(0.01, box[2] * scale)
    height_m = max(0.01, box[3] * scale)
    depth_m = _estimated_depth(width_m, height_m)
    method = "outline_extrusion_v1"
    confidence = min(0.72, max(0.25, float(detection.confidence) * 0.82))
    notes = [
        "Depth is estimated from the visible mask; hidden surfaces are not"
        " reconstructed.",
    ]

    mesh = None
    resolved = settings or ReconstructionSettings()
    if resolved.configured and image_bytes:
        try:
            mesh = _provider_mesh(image_bytes, detection, resolved, image_size)
            bounds = mesh.extents
            depth_m = max(MIN_DEPTH_M, min(MAX_DEPTH_M, float(bounds[1])))
            width_m = max(0.01, float(bounds[0]))
            height_m = max(0.01, float(bounds[2]))
            method = "image_to_3d"
            confidence = min(0.9, max(0.4, float(detection.confidence)))
            notes = [
                "Mesh came from the configured image-to-3D provider; scale is"
                " derived from the visible component box and still needs"
                " review.",
            ]
        except Exception as exc:
            notes.append(f"Image-to-3D provider failed; used outline extrusion: {exc}")

    if mesh is None:
        points = _component_points(detection, scale, box)
        mesh = extrude_polygon_mesh(points, depth_m)

    component_dir = root / "components" / name
    component_dir.mkdir(parents=True, exist_ok=True)
    visual_obj = component_dir / "visual.obj"
    collision_stl = component_dir / "collision.stl"
    preview_glb = component_dir / "preview.glb"
    # Package copies: the MJCF lives at the artifact root, so assets are
    # referenced from assets/visual and assets/collision.
    package_visual = root / "assets" / "visual" / f"{name}.obj"
    package_collision = root / "assets" / "collision" / f"{name}.stl"
    package_preview = root / "assets" / "preview" / f"{name}.glb"

    _export_mesh(mesh, visual_obj)
    _export_mesh(mesh, package_visual)
    _export_mesh(mesh, collision_stl)
    _export_mesh(mesh, package_collision)
    try:
        _export_mesh(mesh, preview_glb)
        _export_mesh(mesh, package_preview)
        preview_path = preview_glb
    except Exception:
        # GLB is a preview convenience; OBJ is authoritative.
        preview_path = visual_obj

    save_component_source(rgb, detection.mask, box, str(component_dir / "source.png"))
    (component_dir / "mask.png").write_bytes(mask_png_bytes(detection.mask))
    metadata = {
        "component_id": component_id,
        "name": name,
        "dimensions": [round(width_m, 6), round(depth_m, 6), round(height_m, 6)],
        "unit": "meter",
        "origin": [0, 0, 0],
        "reconstruction_method": method,
        "confidence": round(confidence, 3),
        "estimated": True,
        "source_box": list(box),
        "notes": notes,
    }
    (component_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    return ComponentMesh(
        name=name,
        visual_obj=package_visual,
        collision_stl=package_collision,
        preview_glb=preview_path,
        dimensions=(width_m, depth_m, height_m),
        method=method,
        confidence=confidence,
        metadata=metadata,
    )


def reconstruct_components(
    *,
    root: Path,
    components: list[dict[str, Any]],
    detections: dict[str, Detection],
    rgb: np.ndarray,
    image_size: tuple[int, int],
    foreground_box: tuple[int, int, int, int],
    category: str,
    settings: ReconstructionSettings | None = None,
    image_bytes: bytes | None = None,
    progress: Any = None,
) -> dict[str, ComponentMesh]:
    """Reconstruct every component, returning meshes keyed by component id."""
    foreground_height = max(1, foreground_box[3])
    scale = _scale_for_category(category, foreground_height)
    meshes: dict[str, ComponentMesh] = {}
    total = max(1, len(components))
    for index, component in enumerate(components):
        detection = detections.get(component["id"])
        if detection is None:
            continue
        mesh = reconstruct_component(
            root=root,
            component_id=component["id"],
            name=component["name"],
            detection=detection,
            rgb=rgb,
            image_size=image_size,
            scale=scale,
            category=category,
            settings=settings,
            image_bytes=image_bytes,
        )
        meshes[component["id"]] = mesh
        if callable(progress):
            progress(
                int(round((index + 1) / total * 100)),
                f"Reconstructed {index + 1}/{total} components",
            )
    return meshes


__all__ = [
    "ComponentMesh",
    "ReconstructionSettings",
    "reconstruct_component",
    "reconstruct_components",
]
