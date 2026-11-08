"""Image loading, segmentation helpers, and mask geometry.

Segmentation is intentionally a provider interface: a real Meta SAM 3.1
endpoint can be configured, and a deterministic foreground/semantic-region
fallback keeps the Foundry usable offline. The fallback is labelled as such
everywhere it is stored or displayed -- it is never presented as SAM output.
"""

from __future__ import annotations

import base64
import io
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from PIL import Image, ImageFilter, ImageOps

try:  # Pillow >= 9
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover - older Pillow
    RESAMPLE = Image.LANCZOS


@dataclass
class Detection:
    """One component candidate, in original-image pixel coordinates."""

    label: str
    component_type: str
    confidence: float
    box: tuple[int, int, int, int]  # x, y, w, h
    mask: np.ndarray  # bool, full image size
    method: str = "heuristic"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "component_type": self.component_type,
            "confidence": round(float(self.confidence), 3),
            "box": list(self.box),
            "method": self.method,
            "metadata": dict(self.metadata),
        }


def load_rgb(path: str) -> tuple[np.ndarray, tuple[int, int]]:
    with Image.open(path) as image:
        rgb = ImageOps.exif_transpose(image).convert("RGB")
        return np.asarray(rgb, dtype=np.uint8), rgb.size


def image_info(data: bytes) -> tuple[int, int, str]:
    """Validate image bytes and return (width, height, format)."""
    with Image.open(io.BytesIO(data)) as image:
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        return image.width, image.height, (image.format or "PNG").upper()


def mask_png_bytes(mask: np.ndarray) -> bytes:
    """Encode a boolean mask as an 8-bit black/white PNG."""
    array = (mask.astype(np.uint8) * 255)
    image = Image.fromarray(array, mode="L")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def save_mask(mask: np.ndarray, path: str) -> None:
    with open(path, "wb") as handle:
        handle.write(mask_png_bytes(mask))


def _crop_with_mask(
    rgb: np.ndarray, mask: np.ndarray, box: tuple[int, int, int, int], pad: int = 2
) -> Image.Image:
    height, width = rgb.shape[:2]
    x, y, w, h = box
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(width, x + w + pad)
    y1 = min(height, y + h + pad)
    crop = rgb[y0:y1, x0:x1]
    crop_mask = mask[y0:y1, x0:x1]
    rgba = np.zeros((crop.shape[0], crop.shape[1], 4), dtype=np.uint8)
    rgba[..., :3] = crop
    rgba[..., 3] = np.where(crop_mask, 255, 0).astype(np.uint8)
    return Image.fromarray(rgba, mode="RGBA")


def save_component_source(
    rgb: np.ndarray, mask: np.ndarray, box: tuple[int, int, int, int], path: str
) -> None:
    image = _crop_with_mask(rgb, mask, box)
    image.save(path, format="PNG", optimize=True)


def save_thumbnail(rgb: np.ndarray, path: str, max_size: int = 420) -> None:
    image = Image.fromarray(rgb, mode="RGB")
    image.thumbnail((max_size, max_size), RESAMPLE)
    image.save(path, format="PNG", optimize=True)


def _border_color(rgb: np.ndarray) -> np.ndarray:
    height, width = rgb.shape[:2]
    band = max(1, min(height, width) // 24)
    border = np.concatenate(
        [
            rgb[:band, :, :].reshape(-1, 3),
            rgb[-band:, :, :].reshape(-1, 3),
            rgb[:, :band, :].reshape(-1, 3),
            rgb[:, -band:, :].reshape(-1, 3),
        ],
        axis=0,
    )
    return np.median(border, axis=0)


def foreground_mask(rgb: np.ndarray) -> np.ndarray:
    """Estimate the robot silhouette from contrast against the border colour."""
    height, width = rgb.shape[:2]
    background = _border_color(rgb)
    difference = np.linalg.norm(rgb.astype(np.float32) - background, axis=2)
    # A border-coloured background is common; a photographic backdrop is not.
    threshold = max(28.0, float(np.percentile(difference, 72)) * 0.55)
    mask = difference > threshold
    # Keep the largest central-ish region: drop tiny noise near the borders.
    coverage = float(mask.mean())
    if coverage < 0.01 or coverage > 0.92:
        # Fall back to a generous central rectangle rather than an empty mask.
        mask = np.zeros((height, width), dtype=bool)
        mask[int(height * 0.03) : int(height * 0.97), int(width * 0.05) : int(width * 0.95)] = True
    image = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
    image = image.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    return np.asarray(image, dtype=np.uint8) > 127


def _bbox_of(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        height, width = mask.shape
        return (0, 0, width, height)
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    return (x0, y0, max(1, x1 - x0 + 1), max(1, y1 - y0 + 1))


def _semantic_regions(
    bbox: tuple[int, int, int, int], category: str
) -> list[dict[str, Any]]:
    """Biped/humanoid semantic regions relative to the foreground box.

    A single image cannot reveal hidden geometry or a real mechanical
    hierarchy. These regions are a reviewable starting point, not a claim
    about the robot's actual construction.
    """
    x, y, w, h = bbox
    # The product default is a MicroDuck-style biped: head, torso, two legs,
    # no arms.
    biped = [
        ("head", "head", 0.30, 0.00, 0.40, 0.22),
        ("main_body", "torso", 0.18, 0.20, 0.64, 0.42),
        ("left_leg", "leg", 0.26, 0.58, 0.20, 0.42),
        ("right_leg", "leg", 0.54, 0.58, 0.20, 0.42),
    ]
    humanoid = [
        ("head", "head", 0.28, 0.00, 0.44, 0.22),
        ("main_body", "torso", 0.16, 0.17, 0.68, 0.46),
        ("left_arm", "arm", 0.00, 0.19, 0.22, 0.43),
        ("right_arm", "arm", 0.78, 0.19, 0.22, 0.43),
        ("left_leg", "leg", 0.23, 0.56, 0.27, 0.44),
        ("right_leg", "leg", 0.50, 0.56, 0.27, 0.44),
    ]
    wheeled = [
        ("main_body", "torso", 0.10, 0.10, 0.80, 0.55),
        ("left_wheel", "wheel", 0.03, 0.58, 0.28, 0.38),
        ("right_wheel", "wheel", 0.69, 0.58, 0.28, 0.38),
        ("head", "head", 0.32, 0.00, 0.36, 0.20),
        ("left_arm", "arm", 0.00, 0.18, 0.14, 0.38),
        ("right_arm", "arm", 0.86, 0.18, 0.14, 0.38),
    ]
    arm_like = [
        ("base", "base", 0.35, 0.55, 0.30, 0.35),
        ("upper_arm", "arm", 0.25, 0.20, 0.25, 0.40),
        ("fore_arm", "arm", 0.52, 0.12, 0.34, 0.32),
        ("gripper", "other", 0.80, 0.05, 0.20, 0.22),
    ]
    category = (category or "biped").lower()
    if category in ("wheeled", "vehicle", "rover"):
        rows = wheeled
    elif category in ("arm", "manipulator"):
        rows = arm_like
    elif category == "humanoid":
        rows = humanoid
    else:
        rows = biped
    return [
        {
            "label": label,
            "component_type": kind,
            "x": x + int(round(w * fx)),
            "y": y + int(round(h * fy)),
            "w": max(2, int(round(w * fw))),
            "h": max(2, int(round(h * fh))),
        }
        for label, kind, fx, fy, fw, fh in rows
    ]


def heuristic_segment(
    rgb: np.ndarray, category: str = "biped"
) -> tuple[list[Detection], dict[str, Any]]:
    """Deterministic offline segmentation (clearly labelled heuristic)."""
    height, width = rgb.shape[:2]
    foreground = foreground_mask(rgb)
    fg_box = _bbox_of(foreground)
    regions = _semantic_regions(fg_box, category)
    detections: list[Detection] = []
    for region in regions:
        x, y, w, h = region["x"], region["y"], region["w"], region["h"]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(width, x + w), min(height, y + h)
        if x1 <= x0 or y1 <= y0:
            continue
        region_mask = np.zeros((height, width), dtype=bool)
        region_mask[y0:y1, x0:x1] = True
        component_mask = foreground & region_mask
        pixels = int(component_mask.sum())
        if pixels < max(12, int((x1 - x0) * (y1 - y0) * 0.01)):
            # No silhouette here: keep a reviewable rectangle, but flag it.
            component_mask = region_mask
            confidence = 0.34
        else:
            confidence = min(0.68, 0.42 + math.log10(1 + pixels) * 0.06)
        box = _bbox_of(component_mask)
        detections.append(
            Detection(
                label=region["label"],
                component_type=region["component_type"],
                confidence=confidence,
                box=box,
                mask=component_mask,
                method="heuristic_v1",
                metadata={
                    "note": (
                        "Heuristic silhouette region; review before trusting"
                        " the component boundary."
                    )
                },
            )
        )
    return detections, {
        "method": "heuristic_v1",
        "foreground_box": list(fg_box),
        "image_size": [width, height],
        "note": (
            "Offline heuristic segmentation. Set MODEL_API_KEY or"
            " SAM31_ENDPOINT to use Meta SAM 3.1 for object detection and"
            " masks."
        ),
    }


# ---- 2D geometry ----------------------------------------------------------


def convex_hull(points: np.ndarray) -> np.ndarray:
    """Monotonic-chain convex hull (CCW) for an Nx2 float array."""
    if points.size == 0:
        return np.zeros((0, 2), dtype=float)
    unique = np.unique(points.astype(float), axis=0)
    if len(unique) <= 2:
        return unique
    ordered = unique[np.lexsort((unique[:, 1], unique[:, 0]))]

    def cross(o: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
        return float(
            (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
        )

    lower: list[np.ndarray] = []
    for point in ordered:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper: list[np.ndarray] = []
    for point in ordered[::-1]:
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    hull = np.array(lower[:-1] + upper[:-1], dtype=float)
    return hull if len(hull) >= 3 else unique


def mask_outline_points(mask: np.ndarray, max_points: int = 160) -> np.ndarray:
    """Return a manageable outline point set from a boolean mask."""
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return np.zeros((0, 2), dtype=float)
    points = np.column_stack((xs, ys)).astype(float)
    if len(points) > max_points:
        step = max(1, len(points) // max_points)
        points = points[::step]
    return convex_hull(points)


def extrude_polygon_mesh(points2d: np.ndarray, depth: float):
    """Build a watertight extruded mesh from a 2D polygon (x, z).

    Returns a ``trimesh.Trimesh``. Avoids optional scipy/shapely deps by
    constructing front/back fans and side quads directly.
    """
    import trimesh

    depth = max(0.005, float(depth))
    hull = convex_hull(points2d)
    if len(hull) < 3:
        return trimesh.creation.box(extents=[0.1, depth, 0.1])
    # Remove collinear-ish duplicate points that break fans.
    cleaned: list[tuple[float, float]] = []
    for x, z in hull:
        if not cleaned or math.hypot(x - cleaned[-1][0], z - cleaned[-1][1]) > 1e-6:
            cleaned.append((float(x), float(z)))
    if len(cleaned) < 3:
        return trimesh.creation.box(extents=[0.1, depth, 0.1])
    n = len(cleaned)
    half = depth / 2.0
    vertices: list[list[float]] = []
    for x, z in cleaned:
        vertices.append([x, -half, z])
    for x, z in cleaned:
        vertices.append([x, half, z])
    faces: list[list[int]] = []
    for i in range(1, n - 1):
        faces.append([0, i + 1, i])  # front (-y), outward normal
        faces.append([n, n + i, n + i + 1])  # back (+y)
    for i in range(n):
        j = (i + 1) % n
        faces.append([i, j, n + j])
        faces.append([i, n + j, n + i])
    mesh = trimesh.Trimesh(
        vertices=np.asarray(vertices, dtype=float),
        faces=np.asarray(faces, dtype=int),
        process=True,
    )
    mesh.remove_unreferenced_vertices()
    # Face winding above is already outward for a CCW convex polygon.
    # ``fix_normals`` would pull in scipy for connected-component analysis,
    # which is an optional dependency we deliberately avoid here.
    return mesh


def decode_mask_base64(value: str, size: tuple[int, int]) -> np.ndarray:
    """Decode a provider mask (PNG base64) into a full-size boolean array."""
    raw = value.split(",", 1)[-1]
    data = base64.b64decode(raw)
    with Image.open(io.BytesIO(data)) as image:
        mask = image.convert("L")
        if mask.size != size:
            mask = mask.resize(size, Image.Resampling.NEAREST)
        return np.asarray(mask, dtype=np.uint8) > 127


__all__ = [
    "Detection",
    "convex_hull",
    "decode_mask_base64",
    "extrude_polygon_mesh",
    "foreground_mask",
    "heuristic_segment",
    "image_info",
    "load_rgb",
    "mask_outline_points",
    "mask_png_bytes",
    "save_component_source",
    "save_mask",
    "save_thumbnail",
]
