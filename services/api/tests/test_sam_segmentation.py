"""SAM 3.1 provider tests.

The wire format is Meta's Responses API special-token segmentation output
(https://dev.meta.ai/docs/sam/overview), not a JSON component schema. These
tests pin the request shape and exercise the real mask decoder.
"""

from __future__ import annotations

import io

import httpx
import numpy as np
from PIL import Image

from app.foundry.segmentation import (
    SegmentationSettings,
    parse_sam_output,
    sam_segment,
    segment_image,
)

# Frame 0, one object: box 211,228..270,254 and a 27x60 lossless mask.
SAMPLE_LINE = (
    "<0f>0<|box;x1=211;y1=228;x2=270;y2=254;w=320;h=334|>"
    "<|mask;x=0;y=0;data=27,60,~!!!!M!0c[0o91w?q1!pIH4pPRVp2B3"
    "'7`e.ioeAf6-k/#Xd8%dX9x(|>"
)


def _png(width: int = 320, height: int = 334) -> bytes:
    image = Image.new("RGB", (width, height), (245, 245, 245))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_parse_sam_output_decodes_box_and_mask():
    detections = parse_sam_output(
        SAMPLE_LINE,
        (320, 334),
        prompt="robot head",
        label="head",
        component_type="head",
    )
    assert len(detections) == 1
    detection = detections[0]
    assert detection.label == "head"
    assert detection.component_type == "head"
    assert detection.box == (211, 228, 60, 27)
    assert detection.mask.shape == (334, 320)
    assert detection.mask.any()
    assert detection.method == "sam_3.1"
    assert detection.metadata["mask_encoding"] == "lossless"
    assert detection.metadata["prompt"] == "robot head"


def test_parse_sam_output_numbers_multiple_instances():
    second = (
        ",1<|box;x1=155;y1=228;x2=202;y2=254;w=320;h=334|>"
        "<|mask;x=0;y=0;data=27,48,~!!!!J!0c[q=Pj_zs=*4C4(/./x#:/`S_GnD`"
        "=o3?X{emCgO$y@|>"
    )
    detections = parse_sam_output(
        SAMPLE_LINE + second,
        (320, 334),
        prompt="robot leg",
        label="leg",
        component_type="leg",
    )
    assert [item.label for item in detections] == ["leg_1", "leg_2"]
    assert [item.metadata["provider_component_id"] for item in detections] == [
        "0",
        "1",
    ]


def test_sam_segment_uses_meta_responses_shape(monkeypatch):
    calls: list[dict] = []

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": SAMPLE_LINE}],
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, json=None, headers=None):
            calls.append({"url": url, "json": json, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    settings = SegmentationSettings(
        endpoint="https://api.meta.ai/v1/responses",
        api_key="secret",
        timeout_s=5.0,
    )
    detections, metadata = sam_segment(_png(), "biped", settings)

    assert len(calls) == 3
    assert {call["json"]["input"][0]["content"][0]["text"] for call in calls} == {
        "robot head",
        "robot torso",
        "robot leg",
    }
    for call in calls:
        body = call["json"]
        assert call["url"] == "https://api.meta.ai/v1/responses"
        assert call["headers"]["Authorization"] == "Bearer secret"
        assert body["model"] == "sam-3.1"
        assert body["stream"] is False
        assert body["metadata"] == {
            "mask_encoding": "one_bit",
            "include_confidence": "true",
        }
        image_part = body["input"][0]["content"][1]
        assert image_part["type"] == "input_image"
        assert image_part["image_url"].startswith("data:image/png;base64,")

    assert len(detections) == 3
    assert {item.component_type for item in detections} == {
        "head",
        "torso",
        "leg",
    }
    assert all(item.mask.shape == (334, 320) for item in detections)
    assert metadata["method"] == "sam_3.1"
    assert metadata["provider"] == "meta_sam_3.1_responses"


def test_segment_image_labels_provider_failure_as_heuristic(tmp_path, monkeypatch):
    image_path = tmp_path / "robot.png"
    image_path.write_bytes(_png(200, 300))

    class BrokenClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, *args, **kwargs):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "Client", BrokenClient)
    detections, metadata = segment_image(
        str(image_path),
        category="biped",
        settings=SegmentationSettings(
            endpoint="https://api.meta.ai/v1/responses", api_key="secret"
        ),
    )

    assert detections
    assert metadata["method"] == "heuristic_v1"
    assert metadata["provider_configured"] is True
    assert "offline" in metadata["provider_error"]
    assert "not SAM output" in metadata["note"]
    assert np.count_nonzero(detections[0].mask) > 0
