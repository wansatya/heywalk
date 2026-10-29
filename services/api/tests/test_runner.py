"""Isolated runner, ROS 2 bridge, rerun, fix-budget, and export tests.

Covers AGENTS.md sections 5, 7, 9, and 12: the topic contract, the
deterministic loopback transport, the isolated-run harness, staging and
telemetry collection, the local subprocess fallback, the Docker command's
isolation profile, fresh reruns, correction validation and attempt caps,
and secret-free export.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Store
from app.main import create_app
from app.models.schemas import GeneratedFile
from app.runner import (
    NotConfiguredRunner,
    RunnerContext,
    RunnerError,
    build_runner,
)
from app.runner import loopback as loopback_mod
from app.runner import staging as staging_mod
from app.runner import topics
from app.runner.docker_runner import (
    DockerRunner,
    build_docker_command,
    container_user,
)
from app.runner.local_runner import LocalSubprocessRunner
from app.services import experiments
from app.services.execution import ExecutionManager, RunEventBus, is_terminal
from app.services.meta_model import MetaModelResponseError
from app.simulator.world import WorldConfig


def _install_robot(client, project_id, robot_id="arm"):
    """Give the project a validated starter robot so MuJoCo runs can start."""
    response = client.post(
        f"/api/projects/{project_id}/robot", json={"robot_id": robot_id}
    )
    assert response.status_code == 200, response.text
    return response.json()


STRAIGHT_CONTROLLER = '''\
"""Test controller: drive straight ahead at 0.4 m/s."""

import rclpy
from geometry_msgs.msg import Twist
from rclpy.node import Node


class StraightNode(Node):
    def __init__(self):
        super().__init__("straight")
        self._pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self.create_timer(0.1, self._tick)

    def _tick(self):
        command = Twist()
        command.linear.x = 0.4
        command.angular.z = 0.0
        self._pub.publish(command)
'''

SLOW_CONTROLLER = '''\
"""Test controller: crawl straight ahead at 0.1 m/s."""

from geometry_msgs.msg import Twist
from rclpy.node import Node


class SlowNode(Node):
    def __init__(self):
        super().__init__("slow")
        self._pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self.create_timer(0.1, self._tick)

    def _tick(self):
        command = Twist()
        command.linear.x = 0.1
        command.angular.z = 0.0
        self._pub.publish(command)
'''

CRASH_CONTROLLER = '''\
"""Test controller: raise inside the timer callback."""

from rclpy.node import Node


class CrashNode(Node):
    def __init__(self):
        super().__init__("crash")
        self.create_timer(0.1, self._tick)

    def _tick(self):
        raise RuntimeError("boom")
'''

SLEEPY_CONTROLLER = '''\
"""Test controller: slow callbacks so timeout/cancel can trigger."""

import time

from geometry_msgs.msg import Twist
from rclpy.node import Node


class SleepyNode(Node):
    def __init__(self):
        super().__init__("sleepy")
        self._pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self.create_timer(0.05, self._tick)

    def _tick(self):
        time.sleep(0.05)
        self._pub.publish(Twist())
'''

NOT_A_NODE = '''\
"""Test module with no controller node."""

VALUE = 42
'''

PHASE_CONTROLLER = '''\
"""Test controller: log every /objective_state phase it receives."""

from geometry_msgs.msg import Twist
from rclpy.node import Node
from std_msgs.msg import String


class PhaseNode(Node):
    def __init__(self):
        super().__init__("phase_probe")
        self._phase = None
        self._pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self.create_subscription(String, "/objective_state", self._on_phase, 10)
        self.create_timer(0.1, self._tick)

    def _on_phase(self, message):
        if message.data == self._phase:
            return
        self._phase = message.data
        self.get_logger().info(f"objective phase: {message.data}")

    def _tick(self):
        command = Twist()
        command.linear.x = 0.4
        self._pub.publish(command)
'''


def _open_world(**overrides):
    world = {
        "width": 10.0,
        "height": 8.0,
        "start": {"x": 1.0, "y": 1.0, "theta": 0.0},
        "goal": {"x": 3.0, "y": 1.0, "radius": 0.4},
        "obstacles": [],
        "dt": 0.05,
        "time_limit": 10.0,
    }
    world.update(overrides)
    return world


def _settings(tmp_path, **overrides):
    fields = {
        "db_path": str(tmp_path / "t.db"),
        "model_api_key": "",
        "meta_api_base_url": "https://api.meta.ai/v1",
        "muse_model": "muse-spark-1.3",
        "model_timeout_s": 5.0,
        "cors_origins": ("*",),
    }
    fields.update(overrides)
    return Settings(**fields)


def _stage(tmp_path, controller_source, world, name="ws"):
    workspace = tmp_path / name / "workspace"
    out_dir = tmp_path / name / "out"
    workspace.mkdir(parents=True)
    out_dir.mkdir(parents=True)
    (workspace / "world.json").write_text(json.dumps(world))
    (workspace / "controller.py").write_text(controller_source)
    return workspace, out_dir


def _wait_for_terminal(client, run_id, timeout_s=15.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if is_terminal(run["status"]):
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach a terminal state")


# ---- topic contract ----


def test_required_topics_and_message_types():
    assert set(topics.REQUIRED_TOPICS) == {
        "/cmd_vel",
        "/odom",
        "/scan",
        "/emergency_stop",
        "/goal_pose",
        "/objective_state",
    }
    assert topics.TOPIC_MESSAGE_TYPES["/cmd_vel"] == "geometry_msgs/msg/Twist"
    assert topics.TOPIC_MESSAGE_TYPES["/odom"] == "nav_msgs/msg/Odometry"
    assert topics.TOPIC_MESSAGE_TYPES["/scan"] == "sensor_msgs/msg/LaserScan"
    assert topics.TOPIC_MESSAGE_TYPES["/emergency_stop"] == "std_msgs/msg/Bool"
    assert topics.TOPIC_MESSAGE_TYPES["/goal_pose"] == (
        "geometry_msgs/msg/PoseStamped"
    )
    assert topics.TOPIC_MESSAGE_TYPES["/objective_state"] == "std_msgs/msg/String"


def test_objective_state_message_round_trip():
    """The phase channel builds under the shim exactly like Bool does."""
    import sys
    from pathlib import Path

    from app.runner.shim.std_msgs.msg import String

    sys.path.insert(0, str(Path("app/runner/shim").resolve()))
    try:
        assert topics.string_message("kick").data == "kick"
        assert String().data == ""
        assert topics.string_message("go_to", String()).data == "go_to"
    finally:
        sys.path.remove(str(Path("app/runner/shim").resolve()))


def test_twist_to_command_coerces_non_finite():
    assert topics.twist_to_command(0.3, -1.0) == (0.3, -1.0)
    assert topics.twist_to_command(float("nan"), 0.5) == (0.0, 0.5)
    assert topics.twist_to_command(0.1, float("inf")) == (0.1, 0.0)
    assert topics.twist_to_command("bad", None) == (0.0, 0.0)


def test_normalize_telemetry_sample_round_trip():
    raw = {
        "t": 1.5, "x": 2.0, "y": 3.0, "theta": 0.1, "v": 0.4,
        "omega": 0.0, "collision": 0, "goal_reached": 1,
        "estop_active": False, "min_range": 2.5,
    }
    sample = topics.normalize_telemetry_sample(raw)
    assert sample["t"] == 1.5
    assert sample["collision"] is False
    assert sample["goal_reached"] is True
    for field in topics.TELEMETRY_FIELDS:
        assert field in sample


def test_normalize_telemetry_sample_rejects_malformed():
    with pytest.raises(ValueError):
        topics.normalize_telemetry_sample({"t": 0.0})  # missing fields
    with pytest.raises(ValueError):
        topics.normalize_telemetry_sample({
            "t": 0, "x": 0, "y": 0, "theta": 0, "v": float("nan"),
            "omega": 0, "min_range": None,
        })
    with pytest.raises(ValueError):
        topics.normalize_telemetry_sample("not-a-dict")  # type: ignore[arg-type]


def test_message_fillers_use_shim_messages():
    from app.runner.harness import _ensure_importable

    _ensure_importable()
    from geometry_msgs.msg import PoseStamped  # noqa: E402
    from nav_msgs.msg import Odometry  # noqa: E402
    from sensor_msgs.msg import LaserScan  # noqa: E402

    odom = topics.fill_odometry_message(
        Odometry(),
        topics.odom_from_state(1.0, 2.0, 0.5, 0.3, 0.1),
    )
    assert odom.pose.pose.position.x == 1.0
    assert abs(topics.yaw_from_quaternion(
        odom.pose.pose.orientation.z, odom.pose.pose.orientation.w
    ) - 0.5) < 1e-9
    scan = topics.fill_laser_scan_message(
        LaserScan(),
        topics.scan_from_ranges(
            [1.0, 2.0], angle_min=-1.0, angle_max=1.0,
            range_min=0.02, range_max=12.0,
        ),
    )
    assert scan.ranges == [1.0, 2.0]
    goal = topics.fill_goal_message(
        PoseStamped(), topics.goal_pose_from_goal(8.5, 6.5)
    )
    assert goal.pose.position.x == 8.5


def test_bool_message_is_built_without_constructor_arguments():
    """rclpy message classes take no constructor arguments.

    ``Bool(True)`` raises ``TypeError`` on the real ROS 2 stack, so the shim
    has to be just as strict: otherwise the loopback path passes locally while
    the ROS 2 runner fails inside the harness.
    """
    from app.runner.harness import _ensure_importable

    _ensure_importable()
    from std_msgs.msg import Bool  # noqa: E402

    with pytest.raises(TypeError):
        Bool(True)  # type: ignore[call-arg]

    message = topics.bool_message(True)
    assert isinstance(message, Bool)
    assert message.data is True
    assert topics.bool_message(False).data is False


# ---- loopback transport + closed loop ----


def test_loopback_dispatch_order_and_timers():
    transport = loopback_mod.LoopbackTransport()
    seen: list[str] = []
    transport.add_subscription("ch", lambda m: seen.append(f"first:{m}"))
    transport.add_subscription("ch", lambda m: seen.append(f"second:{m}"))
    transport.publish("ch", "a")
    transport.publish("ch", "b")
    transport.dispatch()
    assert seen == ["first:a", "second:a", "first:b", "second:b"]

    ticks: list[float] = []
    transport2 = loopback_mod.LoopbackTransport()
    loopback_mod.install_transport(transport2)
    try:
        from app.runner.harness import _ensure_importable

        _ensure_importable()
        from rclpy.node import Node

        node = Node("probe")
        node.create_timer(0.1, lambda: ticks.append(transport2.time))
        for _ in range(4):
            transport2.advance(0.05)
    finally:
        loopback_mod.install_transport(None)
    assert len(ticks) == 2  # fires at t=0.1 and t=0.2, deterministically


def test_closed_loop_records_telemetry_and_is_deterministic(tmp_path):
    world = WorldConfig.from_dict(_open_world())
    first, second = [], []
    for index, collected in ((0, first), (1, second)):
        out = tmp_path / f"loop-{index}"
        loop = loopback_mod.ClosedLoop(world, seed=11, out_dir=out)
        loop.on_cmd_vel(0.4, 0.0)
        for _ in range(20):
            collected.append(loop.tick())
        loop.close()
    assert first == second
    assert len(first) == 20
    lines = (tmp_path / "loop-0" / "telemetry.jsonl").read_text().splitlines()
    assert len(lines) == 21  # initial sample + 20 ticks


# ---- harness (loopback) ----


def test_closed_loop_reports_the_arena_phase(tmp_path):
    """The 2D arena has one phase and reports it on /objective_state."""
    from app.runner.loopback import ClosedLoop

    loop = ClosedLoop(
        WorldConfig.from_dict(_open_world()), seed=0, out_dir=tmp_path / "out"
    )
    try:
        assert topics.ARENA_OBJECTIVE_TYPE == "go_to"
        assert loop.objective_payload() == "go_to"
    finally:
        loop.close()


def test_harness_publishes_objective_state(tmp_path, capsys):
    """A 2D controller receives the phase type while the loop runs."""
    from app.runner import harness

    workspace, out_dir = _stage(tmp_path, PHASE_CONTROLLER, _open_world())
    result = harness.run_loopback(
        workspace, out_dir, seed=3, entry="controller.py",
        estop_file=None, max_samples=200_000,
    )
    assert result["exit_code"] == 0
    assert "objective phase: go_to" in capsys.readouterr().out


def test_harness_reaches_goal_straight_ahead(tmp_path):
    from app.runner import harness

    workspace, out_dir = _stage(tmp_path, STRAIGHT_CONTROLLER, _open_world())
    result = harness.run_loopback(
        workspace, out_dir, seed=3, entry="controller.py",
        estop_file=None, max_samples=200_000,
    )
    assert result["exit_code"] == 0
    assert result["reason"] == "goal"
    samples = [
        json.loads(line)
        for line in (out_dir / "telemetry.jsonl").read_text().splitlines()
    ]
    assert samples[-1]["goal_reached"] is True
    assert all(s["collision"] is False for s in samples)


def test_harness_unlimited_reaches_late_goal(tmp_path):
    from app.runner import harness

    world = _open_world(
        goal={"x": 8.5, "y": 1.0, "radius": 0.4}, time_limit=None
    )
    workspace, out_dir = _stage(tmp_path, SLOW_CONTROLLER, world)
    result = harness.run_loopback(
        workspace, out_dir, seed=3, entry="controller.py",
        estop_file=None, max_samples=200_000,
    )
    assert result["exit_code"] == 0
    assert result["reason"] == "goal"
    samples = [
        json.loads(line)
        for line in (out_dir / "telemetry.jsonl").read_text().splitlines()
    ]
    # 7.5 m at 0.1 m/s: must run past the old 60 s budget to succeed.
    assert samples[-1]["t"] > 60.0
    assert samples[-1]["goal_reached"] is True


def test_harness_is_deterministic_for_same_seed(tmp_path):
    from app.runner import harness

    first_ws, first_out = _stage(
        tmp_path, STRAIGHT_CONTROLLER, _open_world(), name="a"
    )
    second_ws, second_out = _stage(
        tmp_path, STRAIGHT_CONTROLLER, _open_world(), name="b"
    )
    first = harness.run_loopback(
        first_ws, first_out, seed=9, entry="controller.py",
        estop_file=None, max_samples=200_000,
    )
    second = harness.run_loopback(
        second_ws, second_out, seed=9, entry="controller.py",
        estop_file=None, max_samples=200_000,
    )
    assert first == second
    assert (first_out / "telemetry.jsonl").read_bytes() == (
        second_out / "telemetry.jsonl"
    ).read_bytes()


def test_harness_reports_collision_and_stops(tmp_path):
    from app.runner import harness

    world = _open_world(goal={"x": 9.5, "y": 7.0, "radius": 0.4},
                        time_limit=30.0)
    workspace, out_dir = _stage(tmp_path, STRAIGHT_CONTROLLER, world)
    result = harness.run_loopback(
        workspace, out_dir, seed=0, entry="controller.py",
        estop_file=None, max_samples=200_000,
    )
    assert result["exit_code"] == 0
    assert result["reason"] == "collision"


def test_harness_honors_estop_file(tmp_path):
    from app.runner import harness

    workspace, out_dir = _stage(tmp_path, STRAIGHT_CONTROLLER, _open_world())
    estop_file = out_dir / "estop"
    estop_file.write_text("estop\n")
    result = harness.run_loopback(
        workspace, out_dir, seed=0, entry="controller.py",
        estop_file=estop_file, max_samples=200_000,
    )
    assert result["exit_code"] == 0
    assert result["reason"] == "estop"
    samples = [
        json.loads(line)
        for line in (out_dir / "telemetry.jsonl").read_text().splitlines()
    ]
    assert any(s["estop_active"] for s in samples)
    assert samples[-1]["v"] == 0.0 and samples[-1]["omega"] == 0.0


def test_harness_cli_reports_controller_crash(tmp_path):
    from app.runner import harness

    workspace, out_dir = _stage(tmp_path, CRASH_CONTROLLER, _open_world())
    code = harness.main([
        "--workspace", str(workspace), "--out", str(out_dir),
        "--seed", "0", "--transport", "loopback", "--entry", "controller.py",
    ])
    assert code == 2
    result = json.loads((out_dir / "result.json").read_text())
    assert result["reason"] == "harness_error"
    assert "boom" in (result["failure_summary"] or "")


def test_harness_cli_reports_missing_mujoco_actionably(tmp_path, monkeypatch):
    """Without the physics engine the harness names the fix (project .venv)."""
    import sys

    from app.runner import harness

    workspace, out_dir = _stage(
        tmp_path, STRAIGHT_CONTROLLER, {"backend": "mujoco"}
    )

    class _BlockMujoco:
        def find_spec(self, name, path=None, target=None):
            if name == "mujoco" or name.startswith("mujoco."):
                raise ModuleNotFoundError(
                    f"No module named {name!r}", name=name
                )
            return None

    monkeypatch.setattr(sys, "meta_path", [_BlockMujoco(), *sys.meta_path])
    # Force the mujoco import chain to re-execute under the block. The
    # cached physics modules must go too: an already-imported mujoco would
    # satisfy `import mujoco` without consulting the meta path (this test
    # shares the session with suites that really import it).
    for module in (
        "mujoco",
        *[m for m in sys.modules if m.startswith("mujoco.")],
        "app.runner.mujoco_loop",
        "app.simulator.mujoco_engine",
    ):
        monkeypatch.delitem(sys.modules, module, raising=False)
    code = harness.main([
        "--workspace", str(workspace), "--out", str(out_dir),
        "--seed", "0", "--transport", "loopback", "--entry", "controller.py",
    ])
    assert code == 2
    result = json.loads((out_dir / "result.json").read_text())
    assert result["reason"] == "harness_error"
    summary = result["failure_summary"] or ""
    assert "project environment" in summary
    assert ".venv/bin/pip install" in summary


def test_stall_warning_flags_blocked_ticks():
    from app.runner.harness import stall_warning

    assert stall_warning(0.02, 0.02, 0) is None
    assert stall_warning(0.10, 0.02, 0) is None
    note = stall_warning(0.11, 0.02, 0)
    assert note is not None and "blocking" in note
    assert stall_warning(10.0, 0.02, 3) is None
    assert stall_warning(10.0, 0.0, 0) is None


def test_stall_warning_splits_harness_vs_controller_time():
    from app.runner.harness import stall_warning

    # Fast harness body proves the controller blocked the remainder.
    note = stall_warning(0.48, 0.02, 0, 0.006)
    assert note is not None
    assert "harness body 0.006s" in note
    assert "the other ~0.47s" in note
    assert "controller is blocking" in note
    # CPU starvation (cgroup throttling) is the other measured cause.
    assert "CPU-starved" in note
    # A slow harness body blames physics/policy instead.
    note = stall_warning(0.48, 0.02, 0, 0.45)
    assert note is not None and "tick body itself" in note
    # Healthy pacing stays silent with or without a body measurement.
    assert stall_warning(0.02, 0.02, 0, 0.006) is None


def test_find_blocking_calls_flags_sleep_lines():
    from app.runner.harness import find_blocking_calls

    source = (
        "import time\n"
        "from time import sleep\n"
        "\n"
        "def tick():\n"
        "    time.sleep(0.5)  # line 5\n"
        "    sleep(0.1)  # line 6\n"
        "    # a comment mentioning sleep( is fine\n"
    )
    assert find_blocking_calls(source) == [5, 6]
    assert find_blocking_calls("x = 1\n") == []
    assert find_blocking_calls("def broken(:\n") == []


def test_harness_cli_rejects_module_without_node(tmp_path):
    from app.runner import harness

    workspace, out_dir = _stage(tmp_path, NOT_A_NODE, _open_world())
    code = harness.main([
        "--workspace", str(workspace), "--out", str(out_dir),
        "--seed", "0", "--transport", "loopback", "--entry", "controller.py",
    ])
    assert code == 2
    result = json.loads((out_dir / "result.json").read_text())
    assert "Node subclass" in (result["failure_summary"] or "")


def test_harness_cli_rejects_missing_entry(tmp_path):
    from app.runner import harness

    workspace = tmp_path / "ws-risk"
    out_dir = tmp_path / "out-risk"
    workspace.mkdir(parents=True)
    out_dir.mkdir(parents=True)
    (workspace / "world.json").write_text(json.dumps(_open_world()))
    code = harness.main([
        "--workspace", str(workspace), "--out", str(out_dir),
        "--seed", "0", "--transport", "loopback", "--entry", "missing.py",
    ])
    assert code == 2


# ---- staging + telemetry collection ----


def test_prepare_workspace_selects_controller_entry(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    workspace, out_dir, entry = staging_mod.prepare_workspace(
        staging,
        [
            {"path": "helper.py", "content": "# helper"},
            {"path": "controller.py", "content": "# main"},
        ],
        _open_world(),
    )
    assert entry == "controller.py"
    assert (workspace / "world.json").is_file()
    assert (workspace / "controller.py").read_text() == "# main"
    assert out_dir.is_dir()


def test_prepare_workspace_falls_back_to_first_module(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    _, _, entry = staging_mod.prepare_workspace(
        staging, [{"path": "custom.py", "content": "# c"}], _open_world()
    )
    assert entry == "custom.py"


def test_prepare_workspace_rejects_traversal_and_empty(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(ValueError):
        staging_mod.prepare_workspace(
            staging, [{"path": "../escape.py", "content": "x"}], _open_world()
        )
    with pytest.raises(ValueError, match="No Python controller"):
        staging_mod.prepare_workspace(
            staging, [{"path": "notes.txt", "content": "x"}], _open_world()
        )


def test_scrub_env_drops_credentials():
    env = {
        "PATH": "/bin",
        "MODEL_API_KEY": "sk-live",
        "MY_TOKEN_VALUE": "t",
        "DB_PASSWORD": "p",
        "APP_SECRET": "s",
        "RUNNER_BACKEND": "local",
    }
    scrubbed = staging_mod.scrub_env(env)
    assert scrubbed == {"PATH": "/bin", "RUNNER_BACKEND": "local"}


def test_telemetry_tailer_is_incremental_and_counts_malformed(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    tailer = staging_mod.TelemetryTailer(out_dir)
    assert tailer.poll() == []  # missing file: no samples yet
    good = {
        "t": 0.0, "x": 1.0, "y": 1.0, "theta": 0.0, "v": 0.0,
        "omega": 0.0, "collision": False, "goal_reached": False,
        "estop_active": False, "min_range": 5.0,
    }
    with open(out_dir / "telemetry.jsonl", "w") as handle:
        handle.write(json.dumps(good) + "\n")
        handle.write("{broken\n")
    assert len(tailer.poll()) == 1
    assert tailer.malformed_lines == 1
    assert tailer.poll() == []  # offset persisted: no duplicates


def test_read_result_handles_missing_and_corrupt(tmp_path):
    missing = staging_mod.read_result(tmp_path / "nope")
    assert missing["exit_code"] == 1
    assert missing["reason"] == "missing_result"
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    (bad_dir / "result.json").write_text("{oops")
    bad = staging_mod.read_result(bad_dir)
    assert bad["reason"] == "bad_result"


# ---- local subprocess runner ----


def _local_context(files, world, logs, samples, cancel, estop):
    async def emit_log(line):
        logs.append(line)

    async def emit_telemetry(sample):
        samples.append(sample)

    return RunnerContext(
        run_id="run-local-1",
        project_id="proj-local-1",
        controller_files=files,
        world_config=world,
        seed=3,
        emit_log=emit_log,
        emit_telemetry=emit_telemetry,
        cancel_event=cancel,
        estop_event=estop,
    )


def test_local_runner_executes_controller_out_of_process(tmp_path):
    async def scenario():
        logs: list[str] = []
        samples: list[dict] = []
        ctx = _local_context(
            [{"path": "controller.py", "content": STRAIGHT_CONTROLLER}],
            _open_world(),
            logs,
            samples,
            asyncio.Event(),
            asyncio.Event(),
        )
        runner = LocalSubprocessRunner(_settings(tmp_path))
        return await runner.execute(ctx), logs, samples

    result, logs, samples = asyncio.run(scenario())
    assert result.exit_code == 0
    assert result.failure_summary is None
    assert samples, "expected streamed telemetry"
    assert samples[-1]["goal_reached"] is True
    assert any("goal reached" in line for line in logs)


def test_local_runner_reports_harness_errors(tmp_path):
    async def scenario():
        logs: list[str] = []
        samples: list[dict] = []
        ctx = _local_context(
            [{"path": "controller.py", "content": NOT_A_NODE}],
            _open_world(),
            logs,
            samples,
            asyncio.Event(),
            asyncio.Event(),
        )
        runner = LocalSubprocessRunner(_settings(tmp_path))
        return await runner.execute(ctx)

    result = asyncio.run(scenario())
    assert result.exit_code == 2
    assert result.failure_summary and "Node subclass" in result.failure_summary


def test_local_runner_timeout_kills_child(tmp_path):
    async def scenario():
        logs: list[str] = []
        samples: list[dict] = []
        ctx = _local_context(
            [{"path": "controller.py", "content": SLEEPY_CONTROLLER}],
            _open_world(time_limit=60.0),
            logs,
            samples,
            asyncio.Event(),
            asyncio.Event(),
        )
        runner = LocalSubprocessRunner(
            _settings(tmp_path, runner_timeout_s=2.0)
        )
        started = time.monotonic()
        result = await runner.execute(ctx)
        return result, time.monotonic() - started

    result, elapsed = asyncio.run(scenario())
    assert result.timed_out is True
    assert elapsed < 20.0, "timed-out child must be killed, not awaited"


def test_local_runner_cancel_kills_child(tmp_path):
    async def scenario():
        logs: list[str] = []
        samples: list[dict] = []
        cancel = asyncio.Event()
        ctx = _local_context(
            [{"path": "controller.py", "content": SLEEPY_CONTROLLER}],
            _open_world(time_limit=60.0),
            logs,
            samples,
            cancel,
            asyncio.Event(),
        )
        asyncio.get_running_loop().call_later(0.5, cancel.set)
        runner = LocalSubprocessRunner(_settings(tmp_path))
        started = time.monotonic()
        result = await runner.execute(ctx)
        return result, time.monotonic() - started

    result, elapsed = asyncio.run(scenario())
    assert result.cancelled is True
    assert elapsed < 20.0, "cancelled child must be killed promptly"


# ---- docker runner command + factory ----


def test_docker_command_carries_isolation_profile(tmp_path):
    from app.runner.docker_runner import DockerLimits

    command = build_docker_command(
        container_name="heywalk-run-abc",
        workspace=tmp_path / "workspace",
        out_dir=tmp_path / "out",
        estop_file=tmp_path / "out" / "estop",
        seed=7,
        entry="controller.py",
        limits=DockerLimits(
            image="heywalk-runner:jazzy", cpus=1.0, memory="512m",
            pids_limit=128, network="none", timeout_s=180.0,
        ),
        run_user="65534:65534",
    )
    assert command[:3] == ["docker", "run", "--rm"]
    assert "--user" in command and "0:0" not in command
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert "--cap-drop" in command and "ALL" in command
    assert "no-new-privileges" in " ".join(command)
    assert command[command.index("--memory") + 1] == "512m"
    assert command[command.index("--memory-swap") + 1] == "512m"
    assert command[command.index("--pids-limit") + 1] == "128"
    assert command[command.index("--cpus") + 1] == "1"
    joined = " ".join(command)
    assert "/workspace:ro" in joined
    assert "docker.sock" not in joined
    assert "-e" not in command and "--env" not in command
    assert "MODEL_API_KEY" not in joined
    harness_at = command.index("app.runner.harness")
    assert command[harness_at - 2:harness_at + 9] == [
        "python3", "-m", "app.runner.harness",
        "--workspace", "/workspace", "--out", "/out",
        "--seed", "7", "--transport", "rclpy",
    ]
    assert command[-2:] == ["--estop-file", "/out/estop"]


def test_container_user_is_never_root():
    user, group = container_user().split(":")
    assert user.isdigit() and group.isdigit()
    assert user != "0", "container must never run as root"


def test_docker_runner_without_cli_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(RunnerError, match="Docker CLI not found"):
        DockerRunner(_settings(tmp_path, runner_backend="docker"))


def test_default_runner_memory_leaves_headroom(tmp_path, monkeypatch):
    """The MuJoCo + ONNX + rclpy process measures ~500 MiB, so 512m had none.

    Measured in the runner image at --cpus 1 with a live generated two-phase
    run: cgroup anon 491 MiB, peak 504 MiB, and one run peaked at exactly the
    512 MiB cap. ``--memory-swap`` is pinned to the same value, so crossing
    the cap OOM-kills the controller and the run reports a crash instead of a
    verdict. The default is therefore 1024m (about 2x the working set).
    """
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")
    runner = DockerRunner(_settings(tmp_path, runner_backend="docker"))
    assert runner.limits.memory == "1024m"
    default = _settings(tmp_path)
    assert default.runner_memory == "1024m"


def test_build_runner_selection(tmp_path, monkeypatch):
    assert isinstance(
        build_runner(_settings(tmp_path, runner_backend="none")),
        NotConfiguredRunner,
    )
    assert isinstance(
        build_runner(_settings(tmp_path, runner_backend="bogus")),
        NotConfiguredRunner,
    )
    assert isinstance(
        build_runner(_settings(tmp_path, runner_backend="local")),
        LocalSubprocessRunner,
    )
    monkeypatch.setattr(shutil, "which", lambda _: None)
    # Missing CLI degrades honestly to not-configured instead of failing boot.
    assert isinstance(
        build_runner(_settings(tmp_path, runner_backend="docker")),
        NotConfiguredRunner,
    )


@pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker CLI not available"
)
def test_docker_runner_live_container_when_image_present(tmp_path):
    import subprocess

    probe = subprocess.run(
        ["docker", "image", "inspect", "heywalk-runner:jazzy"],
        capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip("runner image heywalk-runner:jazzy not built")

    async def scenario():
        logs: list[str] = []
        samples: list[dict] = []
        ctx = _local_context(
            [{"path": "controller.py", "content": STRAIGHT_CONTROLLER}],
            _open_world(),
            logs,
            samples,
            asyncio.Event(),
            asyncio.Event(),
        )
        runner = DockerRunner(
            _settings(tmp_path, runner_backend="docker",
                      runner_timeout_s=120.0)
        )
        return await runner.execute(ctx), samples

    result, samples = asyncio.run(scenario())
    assert result.exit_code == 0
    assert samples and samples[-1]["goal_reached"] is True


# ---- correction validation + attempt budget ----


def test_fix_budget_allows_up_to_cap_then_stops(tmp_path):
    store = Store(tmp_path / "b.db")
    project = store.create_project("p", "task")
    run = store.create_run(
        project["id"], parent_run_id=None, controller_version=0,
        status="failed", world_config=None, seed=0,
    )
    assert experiments.check_fix_budget(store, project["id"], run["id"]) == 0
    for _ in range(2):
        store.record_model_request(
            project["id"], "fix", "m", "pv", "done", run_id=run["id"]
        )
    assert experiments.count_fix_attempts(store, project["id"], run["id"]) == 2
    with pytest.raises(experiments.FixBudgetExhaustedError, match="2 of 2"):
        experiments.check_fix_budget(store, project["id"], run["id"])
    # The cap is configurable, not hardcoded.
    assert experiments.check_fix_budget(
        store, project["id"], run["id"], max_attempts=3
    ) == 2


def test_fix_budget_default_is_two():
    assert experiments.DEFAULT_MAX_FIX_ATTEMPTS == 2


def test_validate_correction_files_approves_existing_and_entry(tmp_path):
    store = Store(tmp_path / "c.db")
    project = store.create_project("p", "task")
    store.save_file(project["id"], "helper.py", "# old")
    validated = experiments.validate_correction_files(
        store,
        project["id"],
        [
            GeneratedFile(path="helper.py", content="# new"),
            GeneratedFile(path="controller.py", content="# node"),
        ],
    )
    assert validated == [("helper.py", "# new"), ("controller.py", "# node")]


def test_validate_correction_files_rejects_surprises(tmp_path):
    store = Store(tmp_path / "c.db")
    project = store.create_project("p", "task")
    with pytest.raises(MetaModelResponseError, match="no files"):
        experiments.validate_correction_files(store, project["id"], [])
    with pytest.raises(MetaModelResponseError, match="unexpected file"):
        experiments.validate_correction_files(
            store, project["id"],
            [GeneratedFile(path="surprise.py", content="x")],
        )
    with pytest.raises(ValueError, match="[Tt]raversal"):
        experiments.validate_correction_files(
            store, project["id"],
            [GeneratedFile(path="../escape.py", content="x")],
        )


def test_snapshot_and_restore_round_trip(tmp_path):
    store = Store(tmp_path / "s.db")
    project = store.create_project("p", "task")
    store.save_file(project["id"], "controller.py", "# v1")
    snapshot = experiments.snapshot_files(
        store, project["id"], ["controller.py", "fresh.py"]
    )
    assert snapshot == {"controller.py": "# v1", "fresh.py": None}
    store.save_file(project["id"], "controller.py", "# broken")
    store.save_file(project["id"], "fresh.py", "# partial")
    experiments.restore_files(store, project["id"], snapshot)
    assert store.get_file(project["id"], "controller.py")["content"] == "# v1"
    assert store.get_file(project["id"], "fresh.py") is None


# ---- end-to-end: local backend through the execution manager ----


def _drive_until_terminal(manager, run_id):
    async def poll():
        for _ in range(600):
            current = manager.get_run(run_id)
            if is_terminal(current["status"]):
                return current
            await asyncio.sleep(0.05)
        raise AssertionError("run did not reach a terminal state")

    return poll()


def test_local_backend_run_succeeds_end_to_end(tmp_path):
    async def scenario():
        store = Store(tmp_path / "e2e.db")
        project = store.create_project("p", "reach the goal")
        store.save_file(project["id"], "controller.py", STRAIGHT_CONTROLLER)
        manager = ExecutionManager(
            store,
            bus=RunEventBus(),
            runner=LocalSubprocessRunner(_settings(tmp_path)),
        )
        run = await manager.start_run(
            project["id"], world_config=_open_world(), seed=3
        )
        final = await _drive_until_terminal(manager, run["id"])
        await manager.shutdown()
        return store, final

    store, final = asyncio.run(scenario())
    assert final["status"] == "succeeded"
    assert final["metrics"] is not None
    assert final["metrics"]["success"] is True
    assert final["metrics"]["collision_count"] == 0
    assert final["evaluation"]["success"] is True
    assert len(store.list_telemetry(final["id"])) > 10


def test_local_backend_collision_is_honest_failure(tmp_path):
    async def scenario():
        store = Store(tmp_path / "e2e.db")
        project = store.create_project("p", "task")
        store.save_file(project["id"], "controller.py", STRAIGHT_CONTROLLER)
        manager = ExecutionManager(
            store,
            bus=RunEventBus(),
            runner=LocalSubprocessRunner(_settings(tmp_path)),
        )
        world = _open_world(
            goal={"x": 9.5, "y": 7.0, "radius": 0.4}, time_limit=30.0
        )
        run = await manager.start_run(
            project["id"], world_config=world, seed=0
        )
        final = await _drive_until_terminal(manager, run["id"])
        await manager.shutdown()
        return final

    final = asyncio.run(scenario())
    assert final["status"] == "failed"
    assert final["metrics"]["collision_count"] >= 1
    assert any(
        "Collision" in reason for reason in final["evaluation"]["reasons"]
    )


def test_wall_clock_timeout_surfaces_as_timeout_status(tmp_path):
    async def scenario():
        store = Store(tmp_path / "e2e.db")
        project = store.create_project("p", "task")
        store.save_file(project["id"], "controller.py", SLEEPY_CONTROLLER)
        manager = ExecutionManager(
            store,
            bus=RunEventBus(),
            runner=LocalSubprocessRunner(
                _settings(tmp_path, runner_timeout_s=2.0)
            ),
        )
        run = await manager.start_run(
            project["id"],
            world_config=_open_world(time_limit=60.0),
            seed=0,
        )
        final = await _drive_until_terminal(manager, run["id"])
        await manager.shutdown()
        return final

    final = asyncio.run(scenario())
    assert final["status"] == "timeout"
    assert "wall-clock" in (final["failure_summary"] or "")


# ---- rerun API ----


def test_rerun_starts_fresh_run_with_same_scenario(client, project_id):
    _install_robot(client, project_id)
    run_id = client.post(
        f"/api/projects/{project_id}/runs", json={"seed": 11}
    ).json()["id"]
    original = _wait_for_terminal(client, run_id)
    rerun = client.post(f"/api/runs/{run_id}/rerun")
    assert rerun.status_code == 201
    body = rerun.json()
    assert body["id"] != run_id
    assert body["parent_run_id"] == run_id
    assert body["seed"] == original["seed"] == 11
    assert body["world_config"] == original["world_config"]
    assert body["status"] == "queued"
    rerun_final = _wait_for_terminal(client, body["id"])
    assert is_terminal(rerun_final["status"])
    comparison = client.get(f"/api/runs/{body['id']}/comparison").json()
    assert comparison["baseline"]["run_id"] == run_id


def test_rerun_missing_run_is_404(client):
    assert client.post("/api/runs/missing/rerun").status_code == 404


# ---- export: config + run summary without secrets ----


def test_export_carries_config_and_scrubs_secrets(tmp_path):
    secret = "sk-test-secret-abc-123"
    settings = _settings(tmp_path, model_api_key=secret)
    app = create_app(db_path=settings.db_path, settings=settings)
    with TestClient(app) as client:
        project_id = client.post(
            "/api/projects", json={"name": "s", "task_description": "go"}
        ).json()["id"]
        client.put(
            f"/api/projects/{project_id}/files/controller.py",
            json={"content": f"# key leak? {secret}\n"},
        )
        run_id = client.post(
            f"/api/projects/{project_id}/runs",
            json={"seed": 1, "world_config": {"backend": "differential_drive"}},
        ).json()["id"]
        _wait_for_terminal(client, run_id)
        response = client.get(f"/api/projects/{project_id}/export")
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = set(archive.namelist())
            assert {"README.txt", "config.json", "runs.json"} <= names
            assert "controller.py" in names
            blob = b"".join(archive.read(n) for n in names)
            runs = json.loads(archive.read("runs.json"))
            config = json.loads(archive.read("config.json"))
    assert secret.encode() not in blob
    assert b"[REDACTED]" in blob
    assert b"MODEL_API_KEY" not in blob
    assert runs[0]["id"] == run_id
    assert runs[0]["world_config"]["goal"]["x"] == 8.5
    assert runs[0]["seed"] == 1
    assert config["model"] == "muse-spark-1.3"
    assert config["file_versions"] == {"controller.py": 1}


# ---- fix budget API ----


def test_fix_returns_429_when_budget_exhausted(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(db_path=settings.db_path, settings=settings)
    store: Store = app.state.store
    project = store.create_project("p", "task")
    run = store.create_run(
        project["id"], parent_run_id=None, controller_version=0,
        status="failed", world_config=None, seed=0,
    )
    for _ in range(2):
        store.record_model_request(
            project["id"], "fix", "m", "pv", "done", run_id=run["id"]
        )
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project['id']}/fix", json={"run_id": run["id"]}
        )
    assert response.status_code == 429
    assert "correction" in response.json()["detail"]


def test_fix_with_remaining_budget_reaches_model_check(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(db_path=settings.db_path, settings=settings)
    store: Store = app.state.store
    project = store.create_project("p", "task")
    run = store.create_run(
        project["id"], parent_run_id=None, controller_version=0,
        status="failed", world_config=None, seed=0,
    )
    store.record_model_request(
        project["id"], "fix", "m", "pv", "done", run_id=run["id"]
    )
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project['id']}/fix", json={"run_id": run["id"]}
        )
    # One attempt used of two: the request proceeds to the (unconfigured)
    # model client instead of the budget gate.
    assert response.status_code == 503
