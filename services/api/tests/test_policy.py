"""Upstream locomotion policy (velstand.onnx) driver tests.

The open-loop scripted gait cannot walk the free-standing MicroDuck (see
memory: gait probes all faceplanted). The harness instead runs the official
upstream velocity/stand policy as the low-level walking controller:
61-D observation -> 14-D position-target residual added to STAND.
"""

from pathlib import Path

import numpy as np
import pytest

mujoco = pytest.importorskip("mujoco")

from app.simulator.microduck.model import (
    EXPECTED_GROUND_GEOMS,
    FALL_TRUNK_Z,
    OFFICIAL_CONTROL_HZ,
    OFFICIAL_KEYFRAME,
    OFFICIAL_TIMESTEP,
    compose_scene_xml,
    stage_assets,
    stand_joint_targets,
)
from app.simulator.microduck.policy import (
    ACTUATOR_ORDER,
    ACT_DIM,
    OBS_DIM,
    ORT_THREADS,
    POLICY_HZ,
    VelStandPolicy,
    build_observation,
    policy_path,
    projected_gravity,
    rollout,
    targets_from_action,
)
from app.simulator.mujoco_engine import MujocoEngine
from app.simulator.robot_description import parse_mjcf
from app.simulator.scenario import MujocoScenario

STAND = np.array([angle for _, angle in stand_joint_targets()], dtype=float)


def test_policy_file_is_vendored():
    assert policy_path().name == "velstand.onnx"
    assert policy_path().stat().st_size > 100_000


def test_projected_gravity_matches_frame_convention():
    assert np.allclose(projected_gravity((1.0, 0.0, 0.0, 0.0)), (0, 0, -1))
    # 90-degree pitch about Y: body +X now points along world -Z, so
    # gravity in the body frame becomes (+1, 0, 0).
    half = np.sqrt(0.5)
    assert np.allclose(
        projected_gravity((half, 0.0, half, 0.0)), (1, 0, 0), atol=1e-6
    )


def test_observation_layout_is_documented_order():
    ang_vel = (0.1, 0.2, 0.3)
    pos = STAND + 0.05
    vel = np.full(14, 0.01)
    last = np.full(14, -0.02)
    obs = build_observation(
        ang_vel=ang_vel,
        quat=(1.0, 0.0, 0.0, 0.0),
        joint_pos=pos,
        joint_vel=vel,
        last_action=last,
        twist=(0.2, 0.0, 0.0),
        head=(0.0, 0.0, 0.0, 0.0),
        body=(0.0,) * 6,
    )
    assert obs.shape == (OBS_DIM,) == (61,)
    assert obs.dtype == np.float32
    assert np.allclose(obs[0:3], ang_vel)
    assert np.allclose(obs[3:6], (0, 0, -1))
    assert np.allclose(obs[6:20], 0.05)
    assert np.allclose(obs[20:34], 0.01)
    assert np.allclose(obs[34:48], -0.02)
    assert np.allclose(obs[48:51], (0.2, 0.0, 0.0))
    assert np.allclose(obs[51:61], 0.0)


def test_action_maps_to_stand_plus_residual():
    action = np.zeros(ACT_DIM)
    assert np.allclose(targets_from_action(action, STAND), STAND)
    action = np.full(ACT_DIM, 0.1, dtype=float)
    assert np.allclose(targets_from_action(action, STAND), STAND + 0.1)


def test_engine_joint_order_matches_policy_actuator_order(tmp_path: Path):
    engine = _engine(tmp_path)
    assert engine.joint_names == ACTUATOR_ORDER


def test_policy_runs_one_step_from_stand(tmp_path: Path):
    engine = _engine(tmp_path)
    policy = VelStandPolicy()
    targets = policy.step(_policy_inputs(engine))
    assert targets.shape == (ACT_DIM,)
    assert np.all(np.isfinite(targets))
    # A standing-still observation must produce near-STAND targets, not a
    # wild swing: the duck starts at STAND with zero velocity.
    assert np.all(np.abs(targets - STAND) < 0.6)


def test_policy_session_is_single_threaded():
    """The runner has one CPU: ORT must not size its pool to the host cores.

    Default ORT threads busy-wait/spin and get cgroup-throttled inside the
    one-CPU run container, which stretched the 20 ms control tick to ~33 ms
    and made every run lag realtime. See ``ORT_THREADS``.
    """
    import onnxruntime as ort

    policy = VelStandPolicy()
    options = policy.session.get_session_options()
    assert ORT_THREADS == 1
    assert options.intra_op_num_threads == 1
    assert options.inter_op_num_threads == 1
    assert options.execution_mode == ort.ExecutionMode.ORT_SEQUENTIAL


def test_policy_walks_forward_without_falling(tmp_path: Path):
    engine = _engine(tmp_path)
    policy = VelStandPolicy()
    result = rollout(
        engine, policy, seconds=6.0, twist=(0.2, 0.0, 0.0),
    )
    assert result["fallen"] is False, f"trunk_z={result['min_trunk_z']:.3f}"
    assert result["forward_m"] > 0.15, result
    assert result["policy_steps"] == round(6.0 * POLICY_HZ)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _engine(tmp_path: Path, **overrides) -> MujocoEngine:
    workspace = tmp_path / f"ws_{len(list(tmp_path.glob('ws_*')))}"
    workspace.mkdir()
    (workspace / "robot.xml").write_text(compose_scene_xml())
    stage_assets(workspace)
    world = {
        "dt": OFFICIAL_TIMESTEP,
        "control_hz": OFFICIAL_CONTROL_HZ,
        "keyframe": OFFICIAL_KEYFRAME,
        "integrator": "Euler",
        "expected_contact_geoms": list(EXPECTED_GROUND_GEOMS),
        "fall_trunk_z": FALL_TRUNK_Z,
        "fix_base": False,
    }
    world.update(overrides)
    scenario = MujocoScenario.from_dict(world)
    info = parse_mjcf((workspace / "robot.xml").read_text(), "robot.xml")
    return MujocoEngine(workspace / "robot.xml", info, scenario=scenario)


def _policy_inputs(engine: MujocoEngine) -> dict:
    readings = engine.sensor_readings()
    return {
        "ang_vel": readings["imu_ang_vel"],
        "quat": engine.root_orientation(),
        "joint_pos": engine.joint_positions(),
        "joint_vel": engine.joint_velocities(),
        "twist": (0.0, 0.0, 0.0),
        "head": (0.0, 0.0, 0.0, 0.0),
        "body": (0.0,) * 6,
    }
