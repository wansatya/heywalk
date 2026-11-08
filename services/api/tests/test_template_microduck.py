"""MicroDuck contract copy: the Foundry template must match the official model.

The template pins the values Foundry generation copies (actuator tuning,
sensor set, joint ranges, STAND keyframe). These tests parse the vendored
official MJCF, so a vendor update forces a conscious template update rather
than silent drift.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

from app.foundry import template_microduck as template
from app.simulator.microduck import model as microduck_model

OFFICIAL_XML = Path(microduck_model.MODEL_DIR) / microduck_model.ROBOT_XML


def _official_root() -> ET.Element:
    return ET.parse(OFFICIAL_XML).getroot()


def test_template_matches_official_actuator_tuning():
    root = _official_root()
    chosen = None
    for default in root.iter("default"):
        if default.get("class") == "chosen_actuator":
            chosen = default
            break
    assert chosen is not None, "official chosen_actuator class not found"
    joint = chosen.find("joint")
    assert joint is not None
    for key in ("damping", "frictionloss", "armature"):
        assert float(joint.get(key)) == template.JOINT_TUNING[key]


def test_template_matches_official_sensor_set():
    root = _official_root()
    sensor = root.find("sensor")
    assert sensor is not None
    official = {(child.tag, child.get("name")) for child in sensor}
    copied = {(spec["tag"], spec["name"]) for spec in template.IMU_SENSORS}
    assert copied == official


def test_template_matches_official_joint_ranges():
    root = _official_root()
    ranges: dict[str, tuple[float, float]] = {}
    for joint in root.iter("joint"):
        name = joint.get("name") or ""
        raw = (joint.get("range") or "").split()
        if len(raw) == 2:
            ranges[name] = (float(raw[0]), float(raw[1]))
    pitch = template.ROLE_RANGES["pitch"]
    assert ranges["left_hip_pitch"] == (pitch[0], pitch[1]) or (
        math.isclose(ranges["left_hip_pitch"][0], pitch[0], abs_tol=1e-9)
        and math.isclose(ranges["left_hip_pitch"][1], pitch[1], abs_tol=1e-9)
    )
    assert math.isclose(ranges["left_knee"][0], pitch[0], abs_tol=1e-9)
    assert math.isclose(ranges["left_knee"][1], pitch[1], abs_tol=1e-9)
    neck = template.ROLE_RANGES["neck_pitch"]
    assert math.isclose(ranges["neck_pitch"][0], neck[0], abs_tol=1e-9)
    assert math.isclose(ranges["neck_pitch"][1], neck[1], abs_tol=1e-9)
    yaw = template.ROLE_RANGES["head_yaw"]
    assert ranges["head_yaw"] == (yaw[0], yaw[1])


def test_template_stand_matches_official_keyframe():
    assert template.KEYFRAME_STAND == microduck_model.OFFICIAL_KEYFRAME
    assert template.KEYFRAME_STAND in microduck_model.load_metadata().keyframes
    assert template.TIMESTEP == microduck_model.OFFICIAL_TIMESTEP
    assert template.CONTROL_HZ == microduck_model.OFFICIAL_CONTROL_HZ


def test_stand_qpos_layout():
    qpos = template.stand_qpos((0.1, 0.2, 0.3), 4)
    assert qpos == [0.1, 0.2, 0.3, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
