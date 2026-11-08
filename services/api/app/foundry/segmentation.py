"""Segmentation stage: Meta SAM 3.1 via the Responses API + offline fallback.

Reference: https://dev.meta.ai/docs/sam/overview

SAM 3.1 is a segmentation/detection model. It returns boxes and masks; it does
not produce 3D geometry. This module therefore only turns an image into
:class:`~app.foundry.imaging.Detection` objects. Reconstruction is a separate
stage (``reconstruction.py``) so the provider can be replaced independently.

The real provider is the Meta Model API Responses endpoint:

``POST {META_API_BASE_URL}/responses`` with ``model="sam-3.1"``. The prompt is
a short noun phrase in an ``input_text`` part and the image is an
``input_image`` data URL. Masks arrive as SAM's special-token text, not JSON;
``meta-sam-parser`` decodes the base85 + range-coded payload into a binary
raster. One concept is sent per request, as the SAM API requires.

Any provider failure falls back to the deterministic offline segmentation and
records the failure in the analysis metadata; fallback output is always
labelled ``heuristic_v1`` and never presented as SAM.
"""

from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass
from typing import Any

import numpy as np

from .imaging import (
    Detection,
    heuristic_segment,
    load_rgb,
)

try:  # Official zero-dependency decoder for SAM's mask payloads.
    from meta_sam_parser import SegmentationMask, decode_mask_to_raster
except ImportError:  # pragma: no cover - dependency is declared in pyproject
    SegmentationMask = None  # type: ignore[assignment]
    decode_mask_to_raster = None  # type: ignore[assignment]

SAM_METHOD = "sam_3.1"
RESPONSES_PATH = "/responses"

#: One concept per request, as required by SAM 3.1. The tuple is
#: ``(prompt, label, component_type)``. A prompt may match more than one
#: instance (for example both legs); labels are numbered afterwards.
_PROMPT_SPECS: dict[str, tuple[tuple[str, str, str], ...]] = {
    # The product default is a MicroDuck-style biped: a head, a torso, and
    # two legs. No arms.
    "biped": (
        ("robot head", "head", "head"),
        ("robot torso", "torso", "torso"),
        ("robot leg", "leg", "leg"),
    ),
    "humanoid": (
        ("robot head", "head", "head"),
        ("robot torso", "torso", "torso"),
        ("robot arm", "arm", "arm"),
        ("robot leg", "leg", "leg"),
    ),
    "wheeled": (
        ("robot body", "torso", "torso"),
        ("wheel", "wheel", "wheel"),
    ),
    "vehicle": (
        ("robot body", "torso", "torso"),
        ("wheel", "wheel", "wheel"),
    ),
    "rover": (
        ("robot body", "torso", "torso"),
        ("wheel", "wheel", "wheel"),
    ),
    "arm": (
        ("robot arm", "arm", "arm"),
        ("robot base", "base", "base"),
        ("gripper", "gripper", "other"),
    ),
    "manipulator": (
        ("robot arm", "arm", "arm"),
        ("robot base", "base", "base"),
        ("gripper", "gripper", "other"),
    ),
}

_BOX_RE = re.compile(r"<\|box;([^|]*)\|>")
_MASK_RE = re.compile(r"<\|mask;([^|]*)\|>")
_FRAME_RE = re.compile(r"<(\d+)f[^>]*>")
_OBJECT_ID_RE = re.compile(r"\s*(\d+)")


@dataclass(frozen=True)
class SegmentationSettings:
    """Resolved provider configuration (secrets stay server-side)."""

    endpoint: str = ""
    api_key: str = ""
    timeout_s: float = 60.0
    model: str = "sam-3.1"

    @property
    def configured(self) -> bool:
        return bool(self.endpoint.strip())


def resolve_sam_endpoint(value: str) -> str:
    """Resolve a base URL or full SAM endpoint to the Responses URL.

    ``SAM31_ENDPOINT`` may be either the full ``.../responses`` URL or the
    Meta API base (``https://api.meta.ai/v1``). A custom proxy URL is left
    untouched so it can implement its own path.
    """
    endpoint = (value or "").strip().rstrip("/")
    if not endpoint:
        return ""
    if endpoint.endswith("/responses"):
        return endpoint
    if endpoint.endswith("/v1"):
        return endpoint + RESPONSES_PATH
    return endpoint


def _prompt_specs(category: str) -> tuple[tuple[str, str, str], ...]:
    return _PROMPT_SPECS.get((category or "biped").lower(), _PROMPT_SPECS["biped"])


def _component_type(value: Any, label: str) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "limb": "leg",
        "arm": "arm",
        "leg": "leg",
        "head": "head",
        "torso": "torso",
        "body": "torso",
        "wheel": "wheel",
        "base": "base",
        "gripper": "other",
        "other": "other",
    }
    if raw in aliases:
        return aliases[raw]
    lowered = label.lower()
    for token in ("head", "arm", "leg", "wheel", "torso", "base"):
        if token in lowered:
            return token
    return "other"


def _fields(body: str) -> dict[str, str]:
    """Parse ``key=value;key=value`` token fields.

    The mask payload is opaque base85 and may contain ``=``; ``partition``
    keeps it in the value.
    """
    parsed: dict[str, str] = {}
    for part in body.split(";"):
        part = part.strip()
        if not part:
            continue
        key, separator, value = part.partition("=")
        if not separator:
            continue
        parsed[key.strip()] = value.strip()
    return parsed


def _integer(
    fields: dict[str, str], key: str, *, minimum: int | None = None
) -> int:
    raw = fields.get(key)
    if raw is None:
        raise ValueError(f"SAM token is missing {key!r}.")
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"SAM token {key!r} is not an integer.") from None
    if minimum is not None and value < minimum:
        raise ValueError(f"SAM token {key!r} is below {minimum}.")
    return value


def _confidence(box: dict[str, str], mask: dict[str, str]) -> float:
    for fields in (box, mask):
        raw = fields.get("c")
        if raw is None:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if np.isfinite(value):
            return max(0.0, min(1.0, value))
    return 0.5


def _scaled_box(
    box: dict[str, str], frame_size: tuple[int, int]
) -> tuple[int, int, int, int, int, int]:
    """Return ``(left, top, right, bottom, source_w, source_h)``."""
    x1 = _integer(box, "x1")
    y1 = _integer(box, "y1")
    x2 = _integer(box, "x2")
    y2 = _integer(box, "y2")
    source_w = _integer(box, "w", minimum=1)
    source_h = _integer(box, "h", minimum=1)
    if x2 < x1 or y2 < y1:
        raise ValueError("SAM box has inverted corners.")
    frame_w, frame_h = frame_size
    scale_x = frame_w / source_w
    scale_y = frame_h / source_h
    left = max(0, min(frame_w - 1, int(round(x1 * scale_x))))
    top = max(0, min(frame_h - 1, int(round(y1 * scale_y))))
    right = max(left + 1, min(frame_w, int(round((x2 + 1) * scale_x))))
    bottom = max(top + 1, min(frame_h, int(round((y2 + 1) * scale_y))))
    return left, top, right, bottom, source_w, source_h


def _decode_mask(mask: dict[str, str]) -> tuple[np.ndarray, str]:
    if SegmentationMask is None or decode_mask_to_raster is None:
        raise RuntimeError(
            "meta-sam-parser is not installed; cannot decode SAM 3.1 masks."
        )
    data = mask.get("data")
    if not data:
        raise ValueError("SAM mask token is missing 'data'.")
    parts = data.split(",", 2)
    if len(parts) != 3:
        raise ValueError("SAM mask data must be height,width,payload.")
    try:
        mask_h = int(parts[0])
        mask_w = int(parts[1])
    except ValueError:
        raise ValueError("SAM mask dimensions are not integers.") from None
    if mask_h <= 0 or mask_w <= 0:
        raise ValueError("SAM mask dimensions must be positive.")
    payload = parts[2]
    if not payload or payload[0] not in "~!":
        raise ValueError("SAM mask payload is missing its encoding marker.")
    encoding = "one_bit" if payload[0] == "!" else "lossless"
    raster = decode_mask_to_raster(
        SegmentationMask(
            encoding=encoding,  # type: ignore[arg-type]
            payload=payload,
            width=mask_w,
            height=mask_h,
        )
    )
    mask_array = np.frombuffer(raster, dtype=np.uint8).reshape(mask_h, mask_w)
    return mask_array.astype(bool), encoding


def _place_mask(
    mask: np.ndarray, box: tuple[int, int, int, int], frame_size: tuple[int, int]
) -> np.ndarray:
    from PIL import Image

    left, top, right, bottom, _source_w, _source_h = box
    box_w = right - left
    box_h = bottom - top
    if mask.shape != (box_h, box_w):
        image = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
        image = image.resize((box_w, box_h), Image.Resampling.NEAREST)
        mask = np.asarray(image, dtype=np.uint8) > 127
    frame_w, frame_h = frame_size
    full = np.zeros((frame_h, frame_w), dtype=bool)
    full[top:bottom, left:right] = mask
    return full


def _parse_record(
    segment: str,
    frame_size: tuple[int, int],
    *,
    prompt: str,
    label: str,
    component_type: str,
) -> Detection | None:
    object_match = _OBJECT_ID_RE.match(segment)
    box_match = _BOX_RE.search(segment)
    mask_match = _MASK_RE.search(segment)
    if object_match is None or box_match is None or mask_match is None:
        raise ValueError("SAM record needs an object id, a box, and a mask.")
    box_fields = _fields(box_match.group(1))
    mask_fields = _fields(mask_match.group(1))
    left, top, right, bottom, source_w, source_h = _scaled_box(
        box_fields, frame_size
    )
    mask, encoding = _decode_mask(mask_fields)
    full_mask = _place_mask(
        mask, (left, top, right, bottom, source_w, source_h), frame_size
    )
    if not full_mask.any():
        return None
    return Detection(
        label=label,
        component_type=component_type,
        confidence=_confidence(box_fields, mask_fields),
        box=(left, top, right - left, bottom - top),
        mask=full_mask,
        method=SAM_METHOD,
        metadata={
            "provider": "meta_sam_3.1_responses",
            "provider_component_id": object_match.group(1),
            "prompt": prompt,
            "mask_encoding": encoding,
            "source_box": [
                _integer(box_fields, "x1"),
                _integer(box_fields, "y1"),
                _integer(box_fields, "x2"),
                _integer(box_fields, "y2"),
            ],
            "source_size": [source_w, source_h],
            "note": (
                "SAM 3.1 mask; 2D pixels only. It does not imply depth,"
                " hidden geometry, or a real mechanical hierarchy."
            ),
        },
    )


def parse_sam_output(
    text: str,
    frame_size: tuple[int, int],
    *,
    prompt: str = "robot",
    label: str = "component",
    component_type: str = "other",
) -> list[Detection]:
    """Parse one SAM 3.1 ``output_text`` lane into detections.

    Empty text is a valid zero-match result. Malformed non-empty records raise
    ``ValueError`` so the caller can fall back honestly instead of inventing
    components.
    """
    if not text or not text.strip():
        return []
    frame_w, frame_h = frame_size
    detections: list[Detection] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        frame = _FRAME_RE.match(line)
        if frame is None:
            raise ValueError(
                "SAM output line does not start with a frame marker."
            )
        rest = line[frame.end() :].strip()
        if not rest:
            continue
        # Commas also appear inside ``data=H,W,...``; only a comma followed
        # by the next object id and token starts a new record.
        for segment in re.split(r",(?=\s*\d+\s*<\|)", rest):
            segment = segment.strip()
            if not segment:
                continue
            detection = _parse_record(
                segment,
                (frame_w, frame_h),
                prompt=prompt,
                label=label,
                component_type=component_type,
            )
            if detection is not None:
                detections.append(detection)
    if len(detections) > 1:
        for index, detection in enumerate(detections, start=1):
            detection.label = f"{label}_{index}"
    return detections


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    left = max(ax, bx)
    top = max(ay, by)
    right = min(ax + aw, bx + bw)
    bottom = min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    union = aw * ah + bw * bh - intersection
    return intersection / union if union else 0.0


def _deduplicate(detections: list[Detection]) -> list[Detection]:
    """Drop near-identical masks from overlapping concept prompts."""
    kept: list[Detection] = []
    for detection in sorted(detections, key=lambda item: item.confidence, reverse=True):
        duplicate = any(
            other.component_type == detection.component_type
            and _iou(other.box, detection.box) > 0.8
            for other in kept
        )
        if not duplicate:
            kept.append(detection)
    return kept


def _extract_output_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("SAM response must be a JSON object.")
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("code") or "unknown error"
        raise ValueError(f"SAM API error: {message}")
    status = str(payload.get("status") or "")
    if status in ("failed", "cancelled"):
        detail = payload.get("error") or payload.get("incomplete_details") or status
        raise ValueError(f"SAM response {status}: {detail}")
    top = payload.get("output_text")
    if isinstance(top, str):
        return top
    texts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "output_text" and isinstance(item.get("text"), str):
            texts.append(item["text"])
        for part in item.get("content") or []:
            if (
                isinstance(part, dict)
                and part.get("type") in ("output_text", "text")
                and isinstance(part.get("text"), str)
            ):
                texts.append(part["text"])
    return "".join(texts)


def _request_body(model: str, prompt: str, data_url: str) -> dict[str, Any]:
    return {
        "model": model,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url},
                ],
            }
        ],
        "stream": False,
        "metadata": {
            "mask_encoding": "one_bit",
            "include_confidence": "true",
        },
    }


def sam_segment(
    image_bytes: bytes,
    category: str,
    settings: SegmentationSettings,
) -> tuple[list[Detection], dict[str, Any]]:
    """Call SAM 3.1 over the Meta Responses API and decode its masks.

    Raises ``RuntimeError`` on any provider failure so the caller can decide
    to fall back and record the honest reason.
    """
    import httpx

    if not settings.endpoint.strip():
        raise RuntimeError("SAM 3.1 endpoint is not configured.")
    if SegmentationMask is None or decode_mask_to_raster is None:
        raise RuntimeError(
            "meta-sam-parser is not installed; cannot decode SAM 3.1 masks."
        )

    png_bytes, _format, width, height = png_bytes_for_upload(image_bytes)
    encoded = base64.b64encode(png_bytes).decode("ascii")
    data_url = f"data:image/png;base64,{encoded}"
    specs = _prompt_specs(category)

    headers = {"Content-Type": "application/json"}
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"

    detections: list[Detection] = []
    warnings: list[str] = []
    with httpx.Client(timeout=settings.timeout_s) as client:
        for prompt, label, component_type in specs:
            body = _request_body(settings.model, prompt, data_url)
            try:
                response = client.post(
                    settings.endpoint, json=body, headers=headers
                )
                if response.status_code != 200:
                    detail = response.text[:300].strip() or "(empty body)"
                    raise RuntimeError(
                        f"HTTP {response.status_code}: {detail}"
                    )
                text = _extract_output_text(response.json())
                detections.extend(
                    parse_sam_output(
                        text,
                        (width, height),
                        prompt=prompt,
                        label=label,
                        component_type=component_type,
                    )
                )
            except Exception as exc:  # network, HTTP, JSON, mask decoding
                warnings.append(f"{prompt}: {exc}")

    detections = _deduplicate(detections)
    if not detections:
        reason = "; ".join(warnings) if warnings else "no matches"
        raise RuntimeError(
            f"SAM 3.1 returned no components for category {category!r}"
            f" ({reason})."
        )
    return detections, {
        "method": SAM_METHOD,
        "provider": "meta_sam_3.1_responses",
        "model": settings.model,
        "image_size": [width, height],
        "prompts": [prompt for prompt, _label, _kind in specs],
        "provider_warnings": warnings,
        "note": (
            "SAM 3.1 concept-prompt segmentation over the Meta Model API"
            " Responses endpoint. Masks are 2D pixel regions; depth and"
            " hidden geometry are reconstructed separately."
        ),
    }


def segment_image(
    path: str,
    *,
    category: str = "biped",
    settings: SegmentationSettings | None = None,
) -> tuple[list[Detection], dict[str, Any]]:
    """Segment one image, using SAM 3.1 when configured and honest fallback.

    Returns ``(detections, metadata)``. ``metadata['method']`` always states
    which method actually produced the result.
    """
    rgb, _size = load_rgb(path)
    resolved = settings or SegmentationSettings()
    if resolved.configured:
        try:
            with open(path, "rb") as handle:
                data = handle.read()
            return sam_segment(data, category, resolved)
        except (RuntimeError, OSError, ValueError) as exc:
            detections, metadata = heuristic_segment(rgb, category)
            metadata = dict(metadata)
            metadata["provider_error"] = str(exc)
            metadata["provider_configured"] = True
            metadata["note"] = (
                "SAM 3.1 was configured but failed; results below are the"
                " offline heuristic fallback, not SAM output. "
                + str(exc)
            )
            return detections, metadata
    detections, metadata = heuristic_segment(rgb, category)
    metadata = dict(metadata)
    metadata["provider_configured"] = False
    return detections, metadata


def png_bytes_for_upload(data: bytes) -> tuple[bytes, str, int, int]:
    """Normalize an uploaded image to PNG bytes (keeps masks consistent).

    EXIF orientation is applied before encoding so SAM's reported source
    dimensions match the pixels the rest of the pipeline sees.
    """
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as image:
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        rgb = ImageOps.exif_transpose(image).convert("RGB")
        width, height = rgb.size
        buffer = io.BytesIO()
        rgb.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue(), "png", width, height


__all__ = [
    "RESPONSES_PATH",
    "SAM_METHOD",
    "SegmentationSettings",
    "parse_sam_output",
    "png_bytes_for_upload",
    "resolve_sam_endpoint",
    "sam_segment",
    "segment_image",
]
