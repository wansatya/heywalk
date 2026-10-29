"""Isolated-run harness: controller + simulator closed loop (AGENTS.md §5, §7).

This module is the entrypoint executed INSIDE the isolated boundary (Docker
container or local subprocess). It never runs in the FastAPI process. It:

1. Loads the world config, seed, and generated controller files.
2. Bridges the controller to the deterministic simulator over the ROS 2
   topic contract (real rclpy when ``--transport rclpy``, otherwise the
   in-process loopback transport with identical topic names).
3. Collects per-step telemetry into ``telemetry.jsonl``.
4. Writes ``result.json`` with the exit code and stop reason.

The harness never judges success: reaching the goal, colliding, or timing
out all exit 0 with a reason string. Only harness/controller crashes exit
nonzero. The objective evaluator owns the success verdict.

CLI: ``python -m app.runner.harness --workspace DIR --out DIR --seed N
[--transport loopback|rclpy] [--entry controller.py]
[--estop-file PATH] [--max-samples N]``
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import math
import sys
import time
import traceback
from pathlib import Path
from typing import Any

WORLD_FILENAME = "world.json"
TELEMETRY_FILENAME = "telemetry.jsonl"
RESULT_FILENAME = "result.json"
DEFAULT_ENTRY = "controller.py"
ESTOP_SETTLE_STEPS = 3

EXIT_OK = 0
EXIT_HARNESS_ERROR = 2


def _ensure_importable() -> None:
    """Make ``app.*`` and the shim importable from any working directory."""
    here = Path(__file__).resolve()
    # .../services/api/app/runner/harness.py -> services/api on sys.path.
    api_root = here.parent.parent.parent
    if str(api_root) not in sys.path:
        sys.path.insert(0, str(api_root))
    shim_root = here.parent / "shim"
    if str(shim_root) not in sys.path:
        # Front of the path so the loopback shim shadows any ambient ROS
        # install only when the loopback transport is selected; the rclpy
        # transport path below refuses to use the shim.
        sys.path.insert(0, str(shim_root))


def load_world(workspace: Path) -> dict[str, Any]:
    path = workspace / WORLD_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"Missing world config: {WORLD_FILENAME}.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("World config must be a JSON object.")
    return data


def load_controller_module(workspace: Path, entry: str) -> Any:
    entry_path = (workspace / entry).resolve()
    workspace_resolved = workspace.resolve()
    if entry_path != workspace_resolved and workspace_resolved not in (
        entry_path.parents
    ):
        raise ValueError(f"Controller entry escapes the workspace: {entry!r}.")
    if not entry_path.is_file():
        raise ValueError(f"Controller entry not found: {entry!r}.")
    if entry_path.suffix != ".py":
        raise ValueError(f"Controller entry must be a Python file: {entry!r}.")
    spec = importlib.util.spec_from_file_location(
        "heywalk_controller_entry", entry_path
    )
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load controller entry: {entry!r}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def find_controller_node(module: Any, node_base: type) -> Any:
    """Resolve the controller node: ``create_node()`` or a Node subclass."""
    factory = getattr(module, "create_node", None)
    if callable(factory):
        node = factory()
        if not isinstance(node, node_base):
            raise TypeError(
                "create_node() must return an rclpy Node instance."
            )
        return node
    candidates = [
        member
        for _, member in inspect.getmembers(module, inspect.isclass)
        if issubclass(member, node_base)
        and member is not node_base
        and member.__module__ == module.__name__
    ]
    if not candidates:
        raise TypeError(
            "Controller must define create_node() or an rclpy Node subclass."
        )
    if len(candidates) > 1:
        names = ", ".join(sorted(c.__name__ for c in candidates))
        raise TypeError(
            f"Controller defines multiple nodes ({names}); define exactly one."
        )
    try:
        return candidates[0]()
    except TypeError as exc:
        raise TypeError(
            f"Controller node {candidates[0].__name__} must be constructible"
            f" with no arguments: {exc}."
        ) from None


def write_result(out_dir: Path, result: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / RESULT_FILENAME).write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )


def _estop_latched(estop_file: Path | None) -> bool:
    return estop_file is not None and estop_file.exists()


def run_loopback(
    workspace: Path,
    out_dir: Path,
    seed: int,
    entry: str,
    estop_file: Path | None,
    max_samples: int,
) -> dict[str, Any]:
    _ensure_importable()
    from app.runner import loopback, topics
    from app.simulator.world import WorldConfig

    world = WorldConfig.from_dict(load_world(workspace))

    # Shim message classes (shim root is already first on sys.path).
    from geometry_msgs.msg import PoseStamped  # noqa: E402
    from nav_msgs.msg import Odometry  # noqa: E402
    from sensor_msgs.msg import LaserScan  # noqa: E402
    from std_msgs.msg import Bool  # noqa: E402
    from rclpy.node import Node as ShimNode  # noqa: E402
    import rclpy  # noqa: E402

    transport = loopback.LoopbackTransport()
    loopback.install_transport(transport)
    loop = loopback.ClosedLoop(world, seed, out_dir, max_samples=max_samples)
    try:
        rclpy.init()
        module = load_controller_module(workspace, entry)
        warn_blocking_calls(workspace / entry, entry)
        node = find_controller_node(module, ShimNode)
        limit_text = (
            "none" if world.time_limit is None else f"{world.time_limit:g}s"
        )
        print(
            f"Harness: controller node '{node.get_name()}' on loopback; "
            f"world dt={world.dt:g}s limit={limit_text} seed={seed}.",
            flush=True,
        )
        transport.add_subscription(
            topics.CMD_VEL_TOPIC, loop.on_twist_message
        )

        def publish_odom_scan() -> None:
            odom_msg = topics.fill_odometry_message(Odometry(), loop.odom_payload())
            scan_msg = topics.fill_laser_scan_message(LaserScan(), loop.scan_payload())
            transport.publish(topics.ODOM_TOPIC, odom_msg)
            transport.publish(topics.SCAN_TOPIC, scan_msg)
            transport.publish(
                topics.OBJECTIVE_STATE_TOPIC,
                topics.string_message(loop.objective_payload()),
            )

        goal_msg = topics.fill_goal_message(PoseStamped(), loop.goal_payload())
        transport.publish(topics.GOAL_POSE_TOPIC, goal_msg)
        publish_odom_scan()
        transport.dispatch()

        budget = (
            None
            if world.time_limit is None
            else max(1, int(math.ceil(world.time_limit / world.dt)))
        )
        reason = "timeout"
        estop_announced = False
        estop_settle = 0
        # The budget is enforced against recorded sim time (sample["t"]), not
        # the iteration count, so the stop point always agrees with what the
        # evaluator sees. A goal reached exactly at the buzzer still wins.
        while True:
            if _estop_latched(estop_file):
                if not estop_announced:
                    print("Harness: emergency stop latched; halting.", flush=True)
                    transport.publish(
                        topics.EMERGENCY_STOP_TOPIC,
                        topics.bool_message(True),
                    )
                    estop_announced = True
                sample = loop.tick(estop_active=True)
                publish_odom_scan()
                transport.dispatch()
                estop_settle += 1
                if estop_settle >= ESTOP_SETTLE_STEPS:
                    reason = "estop"
                    break
                continue
            try:
                transport.advance(world.dt)
            except Exception as exc:
                raise RuntimeError(f"Controller callback failed: {exc}.") from exc
            sample = loop.tick(estop_active=False)
            publish_odom_scan()
            try:
                transport.dispatch()
            except Exception as exc:
                raise RuntimeError(f"Controller callback failed: {exc}.") from exc
            if sample["goal_reached"]:
                reason = "goal"
                print(
                    f"Harness: goal reached at t={sample['t']:.2f}s.", flush=True
                )
                break
            if sample["collision"]:
                reason = "collision"
                print(
                    f"Harness: collision at t={sample['t']:.2f}s; stopping.",
                    flush=True,
                )
                break
            if loop.sample_count >= max_samples:
                reason = "sample_cap"
                print(
                    f"Harness: sample cap reached ({max_samples}); stopping.",
                    flush=True,
                )
                break
            if budget is not None and sample["t"] >= world.time_limit:
                break
        if reason == "timeout" and budget is not None:
            print(
                f"Harness: sim time limit reached ({world.time_limit:g}s).",
                flush=True,
            )
        try:
            node.destroy_node()
        except Exception:
            pass
        return {
            "exit_code": EXIT_OK,
            "reason": reason,
            "samples": loop.sample_count,
            "timed_out": False,
            "failure_summary": None,
        }
    finally:
        try:
            loop.close()
        finally:
            loopback.install_transport(None)
            try:
                rclpy.shutdown()
            except Exception:
                pass


def run_rclpy(
    workspace: Path,
    out_dir: Path,
    seed: int,
    entry: str,
    estop_file: Path | None,
    max_samples: int,
) -> dict[str, Any]:
    """Closed loop on real ROS 2 pub/sub (Docker runner with Jazzy)."""
    # Refuse the shim: real rclpy must provide these imports.
    sys.path = [
        p for p in sys.path
        if Path(p).name != "shim" and "app/runner/shim" not in p
    ]
    for name in (
        "rclpy",
        "geometry_msgs",
        "nav_msgs",
        "sensor_msgs",
        "std_msgs",
    ):
        sys.modules.pop(name, None)
    try:
        import rclpy  # type: ignore
        from rclpy.executors import SingleThreadedExecutor  # type: ignore
        from rclpy.node import Node as RclpyNode  # type: ignore
        from geometry_msgs.msg import Twist, PoseStamped  # type: ignore
        from nav_msgs.msg import Odometry  # type: ignore
        from sensor_msgs.msg import LaserScan  # type: ignore
        from std_msgs.msg import Bool, String  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            f"Real rclpy is required for --transport rclpy: {exc}."
        ) from None

    from app.runner import topics
    from app.runner.loopback import ClosedLoop
    from app.simulator.world import WorldConfig

    world = WorldConfig.from_dict(load_world(workspace))
    rclpy.init()
    loop = ClosedLoop(world, seed, out_dir, max_samples=max_samples)
    try:
        module = load_controller_module(workspace, entry)
        warn_blocking_calls(workspace / entry, entry)
        controller = find_controller_node(module, RclpyNode)

        class _Bridge(RclpyNode):
            def __init__(self) -> None:
                super().__init__("heywalk_sim_bridge")
                self._estop = False
                self.create_subscription(
                    Twist, topics.CMD_VEL_TOPIC, self._on_cmd, 10
                )
                self.create_subscription(
                    Bool, topics.EMERGENCY_STOP_TOPIC, self._on_estop, 10
                )
                self._odom_pub = self.create_publisher(
                    Odometry, topics.ODOM_TOPIC, 10
                )
                self._scan_pub = self.create_publisher(
                    LaserScan, topics.SCAN_TOPIC, 10
                )
                self._goal_pub = self.create_publisher(
                    PoseStamped, topics.GOAL_POSE_TOPIC, 10
                )
                self._objective_pub = self.create_publisher(
                    String, topics.OBJECTIVE_STATE_TOPIC, 10
                )
                self.last_sample: dict[str, Any] | None = None
                self._last_tick: float | None = None
                self._last_body_s: float | None = None
                self._stall_warnings = 0
                self.create_timer(world.dt, self._on_tick)

            def _on_cmd(self, message: Any) -> None:
                loop.on_twist_message(message)

            def _on_estop(self, message: Any) -> None:
                if getattr(message, "data", False):
                    self._estop = True

            @property
            def estop_active(self) -> bool:
                return self._estop or _estop_latched(estop_file)

            def _on_tick(self) -> None:
                now = time.monotonic()
                if self._last_tick is not None:
                    note = stall_warning(
                        now - self._last_tick, world.dt, self._stall_warnings,
                        self._last_body_s,
                    )
                    if note is not None:
                        self._stall_warnings += 1
                        print(note, flush=True)
                self._last_tick = now
                body_start = time.monotonic()
                self.last_sample = loop.tick(
                    estop_active=self.estop_active
                )
                self._odom_pub.publish(
                    topics.fill_odometry_message(Odometry(), loop.odom_payload())
                )
                self._scan_pub.publish(
                    topics.fill_laser_scan_message(LaserScan(), loop.scan_payload())
                )
                self._objective_pub.publish(
                    topics.string_message(loop.objective_payload())
                )
                self._last_body_s = time.monotonic() - body_start

        bridge = _Bridge()
        bridge._goal_pub.publish(
            topics.fill_goal_message(PoseStamped(), loop.goal_payload())
        )
        bridge._objective_pub.publish(
            topics.string_message(loop.objective_payload())
        )
        limit_text = (
            "none" if world.time_limit is None else f"{world.time_limit:g}s"
        )
        print(
            f"Harness: controller '{controller.get_name()}' on real rclpy; "
            f"world dt={world.dt:g}s limit={limit_text} seed={seed}.",
            flush=True,
        )
        executor = SingleThreadedExecutor()
        executor.add_node(bridge)
        executor.add_node(controller)
        budget = (
            None
            if world.time_limit is None
            else max(1, int(math.ceil(world.time_limit / world.dt)))
        )
        reason = "timeout"
        try:
            steps = 0
            # Budget is enforced against recorded sim time: spin iterations
            # can outnumber sim ticks (stale last_sample), so counting them
            # would stop the run before the sim clock reaches the limit.
            while rclpy.ok():
                executor.spin_once(timeout_sec=min(1.0, world.dt * 2))
                sample = bridge.last_sample
                if sample is None:
                    continue
                steps += 1
                if bridge.estop_active and steps % ESTOP_SETTLE_STEPS == 0:
                    reason = "estop"
                    print("Harness: emergency stop latched; halting.", flush=True)
                    break
                if sample["goal_reached"]:
                    reason = "goal"
                    print(
                        f"Harness: goal reached at t={sample['t']:.2f}s.",
                        flush=True,
                    )
                    break
                if sample["collision"]:
                    reason = "collision"
                    print(
                        f"Harness: collision at t={sample['t']:.2f}s; stopping.",
                        flush=True,
                    )
                    break
                if loop.sample_count >= max_samples:
                    reason = "sample_cap"
                    print(
                        f"Harness: sample cap reached ({max_samples}); stopping.",
                        flush=True,
                    )
                    break
                if budget is not None and sample["t"] >= world.time_limit:
                    break
        finally:
            executor.shutdown()
            try:
                controller.destroy_node()
            except Exception:
                pass
            try:
                bridge.destroy_node()
            except Exception:
                pass
        return {
            "exit_code": EXIT_OK,
            "reason": reason,
            "samples": loop.sample_count,
            "timed_out": False,
            "failure_summary": None,
        }
    finally:
        loop.close()
        try:
            rclpy.shutdown()
        except Exception:
            pass


def _mujoco_scenario(workspace: Path, seed: int):
    from dataclasses import replace

    from app.simulator.scenario import MujocoScenario

    scenario = MujocoScenario.from_dict(load_world(workspace))
    return replace(scenario, seed=seed)


def run_mujoco_loopback(
    workspace: Path,
    out_dir: Path,
    seed: int,
    entry: str,
    estop_file: Path | None,
    max_samples: int,
) -> dict[str, Any]:
    """Automated joint-test loop on the in-process loopback transport."""
    _ensure_importable()
    from app.runner import loopback, topics
    from app.runner.mujoco_loop import MujocoLoop, summarize_log_events

    from geometry_msgs.msg import PoseStamped  # noqa: E402,F401
    from nav_msgs.msg import Odometry  # noqa: E402
    from sensor_msgs.msg import JointState  # noqa: E402
    from std_msgs.msg import Float64MultiArray  # noqa: E402
    from rclpy.node import Node as ShimNode  # noqa: E402
    import rclpy  # noqa: E402

    scenario = _mujoco_scenario(workspace, seed)
    transport = loopback.LoopbackTransport()
    loopback.install_transport(transport)
    loop = MujocoLoop(workspace, out_dir, scenario, max_samples=max_samples)
    try:
        rclpy.init()
        module = load_controller_module(workspace, entry)
        warn_blocking_calls(workspace / entry, entry)
        node = find_controller_node(module, ShimNode)
        print(
            f"MuJoCo harness: controller '{node.get_name()}' on loopback;"
            f" {loop.engine.joint_count} joint(s)"
            f" ({', '.join(loop.joint_names)});"
            f" dt={scenario.dt:g}s control={scenario.control_hz:g}Hz"
            f" seed={seed}.",
            flush=True,
        )
        transport.add_subscription(
            topics.JOINT_COMMANDS_TOPIC, loop.on_joint_commands
        )
        if scenario.locomotion:
            transport.add_subscription(
                topics.CMD_VEL_TOPIC, loop.on_cmd_vel
            )
        for line in summarize_log_events(loop.drain_events()):
            print(line, flush=True)

        def publish_outputs() -> None:
            transport.publish(
                topics.JOINT_STATES_TOPIC,
                topics.fill_joint_state_message(JointState(), loop.joint_state_payload()),
            )
            transport.publish(
                topics.JOINT_TARGETS_TOPIC,
                topics.fill_float64_array_message(
                    Float64MultiArray(), loop.target_payload()
                ),
            )
            transport.publish(
                topics.EMERGENCY_STOP_TOPIC,
                topics.bool_message(loop.estop_active),
            )
            if scenario.locomotion:
                x, y, yaw, v, omega = loop.engine.base_pose()
                transport.publish(
                    topics.ODOM_TOPIC,
                    topics.fill_odometry_message(
                        Odometry(), topics.odom_from_state(x, y, yaw, v, omega)
                    ),
                )
                goal_x, goal_y = loop.runner.goal_xy
                transport.publish(
                    topics.GOAL_POSE_TOPIC,
                    topics.fill_goal_message(
                        PoseStamped(),
                        topics.goal_pose_from_goal(goal_x, goal_y),
                    ),
                )
                transport.publish(
                    topics.OBJECTIVE_STATE_TOPIC,
                    topics.string_message(loop.runner.objective_type),
                )

        publish_outputs()
        transport.dispatch()

        reason = "tests_complete"
        external_estop = False
        estop_ticks = 0
        # Locomotion runs settle to a balanced STAND through the walking
        # policy (zero twist) instead of freezing mid-stride: 2 s gives
        # the policy time to plant its feet. Joint-test runs hold the
        # current pose, so 3 ticks is enough.
        estop_settle_ticks = (
            max(ESTOP_SETTLE_STEPS, int(2.0 / loop.control_period))
            if scenario.locomotion
            else ESTOP_SETTLE_STEPS
        )
        while True:
            if _estop_latched(estop_file):
                if not external_estop:
                    if scenario.locomotion:
                        print(
                            "MuJoCo harness: emergency stop latched;"
                            " settling to STAND.",
                            flush=True,
                        )
                    else:
                        print(
                            "MuJoCo harness: emergency stop latched; halting.",
                            flush=True,
                        )
                    loop.runner.estop_active = True
                    external_estop = True
                estop_ticks += 1
                if estop_ticks >= estop_settle_ticks:
                    reason = "estop"
                    break
            try:
                transport.advance(loop.control_period)
            except Exception as exc:
                raise RuntimeError(f"Controller callback failed: {exc}.") from exc
            loop.tick()
            publish_outputs()
            try:
                transport.dispatch()
            except Exception as exc:
                raise RuntimeError(f"Controller callback failed: {exc}.") from exc
            for line in summarize_log_events(loop.drain_events()):
                print(line, flush=True)
            if loop.finished:
                reason = "tests_complete"
                break
            if loop.timed_out:
                reason = "timeout"
                print(
                    f"MuJoCo harness: scenario time limit reached"
                    f" ({scenario.time_limit:g}s).",
                    flush=True,
                )
                break
            if loop.max_samples_reached:
                reason = "sample_cap"
                print(
                    f"MuJoCo harness: sample cap reached ({max_samples}).",
                    flush=True,
                )
                break
        try:
            node.destroy_node()
        except Exception:
            pass
        result: dict[str, Any] = {
            "exit_code": EXIT_OK,
            "timed_out": False,
            "failure_summary": None,
        }
        result.update(loop.result_payload(reason))
        planned = len(loop.runner.tests)
        if scenario.locomotion:
            planned = len(getattr(loop.runner, "phases", None) or [None])
        result["suite_size"] = planned + (
            1 if scenario.estop_after_s is not None else 0
        )
        return result
    finally:
        try:
            loop.close()
        finally:
            loopback.install_transport(None)
            try:
                rclpy.shutdown()
            except Exception:
                pass


def run_mujoco_rclpy(
    workspace: Path,
    out_dir: Path,
    seed: int,
    entry: str,
    estop_file: Path | None,
    max_samples: int,
) -> dict[str, Any]:
    """Automated joint-test loop on real ROS 2 pub/sub (Docker runner)."""
    sys.path = [
        p for p in sys.path
        if Path(p).name != "shim" and "app/runner/shim" not in p
    ]
    for name in (
        "rclpy",
        "geometry_msgs",
        "nav_msgs",
        "sensor_msgs",
        "std_msgs",
    ):
        sys.modules.pop(name, None)
    try:
        import rclpy  # type: ignore
        from rclpy.executors import SingleThreadedExecutor  # type: ignore
        from rclpy.node import Node as RclpyNode  # type: ignore
        from geometry_msgs.msg import PoseStamped, Twist  # type: ignore
        from nav_msgs.msg import Odometry  # type: ignore
        from sensor_msgs.msg import JointState  # type: ignore
        from std_msgs.msg import (  # type: ignore
            Bool,
            Float64MultiArray,
            String,
        )
    except ImportError as exc:
        raise RuntimeError(
            f"Real rclpy is required for --transport rclpy: {exc}."
        ) from None

    from app.runner import topics
    from app.runner.mujoco_loop import (
        MujocoLoop,
        format_tick_breakdown,
        summarize_log_events,
    )

    scenario = _mujoco_scenario(workspace, seed)
    rclpy.init()
    loop = MujocoLoop(workspace, out_dir, scenario, max_samples=max_samples)
    try:
        module = load_controller_module(workspace, entry)
        warn_blocking_calls(workspace / entry, entry)
        controller = find_controller_node(module, RclpyNode)

        class _JointBridge(RclpyNode):
            def __init__(self) -> None:
                super().__init__("heywalk_mujoco_bridge")
                self._state_pub = self.create_publisher(
                    JointState, topics.JOINT_STATES_TOPIC, 10
                )
                self._target_pub = self.create_publisher(
                    Float64MultiArray, topics.JOINT_TARGETS_TOPIC, 10
                )
                self._estop_pub = self.create_publisher(
                    Bool, topics.EMERGENCY_STOP_TOPIC, 10
                )
                self._odom_pub: Any = None
                self._goal_pub: Any = None
                self._objective_pub: Any = None
                if scenario.locomotion:
                    self._odom_pub = self.create_publisher(
                        Odometry, topics.ODOM_TOPIC, 10
                    )
                    self._goal_pub = self.create_publisher(
                        PoseStamped, topics.GOAL_POSE_TOPIC, 10
                    )
                    self._objective_pub = self.create_publisher(
                        String, topics.OBJECTIVE_STATE_TOPIC, 10
                    )
                self.create_subscription(
                    Float64MultiArray,
                    topics.JOINT_COMMANDS_TOPIC,
                    loop.on_joint_commands,
                    10,
                )
                if scenario.locomotion:
                    self.create_subscription(
                        Twist,
                        topics.CMD_VEL_TOPIC,
                        loop.on_cmd_vel,
                        10,
                    )
                self.finished = False
                self._last_tick: float | None = None
                self._last_body_s: float | None = None
                self._last_breakdown: str | None = None
                self._stall_warnings = 0
                self.create_timer(loop.control_period, self._on_tick)

            def _on_tick(self) -> None:
                now = time.monotonic()
                if self._last_tick is not None:
                    note = stall_warning(
                        now - self._last_tick,
                        loop.control_period,
                        self._stall_warnings,
                        self._last_body_s,
                        self._last_breakdown,
                    )
                    if note is not None:
                        self._stall_warnings += 1
                        print(note, flush=True)
                self._last_tick = now
                body_start = time.monotonic()
                self._target_pub.publish(
                    topics.fill_float64_array_message(
                        Float64MultiArray(), loop.target_payload()
                    )
                )
                loop.tick()
                self._state_pub.publish(
                    topics.fill_joint_state_message(
                        JointState(), loop.joint_state_payload()
                    )
                )
                self._estop_pub.publish(topics.bool_message(loop.estop_active))
                if scenario.locomotion:
                    x, y, yaw, v, omega = loop.engine.base_pose()
                    assert self._odom_pub is not None
                    assert self._goal_pub is not None
                    assert self._objective_pub is not None
                    self._odom_pub.publish(
                        topics.fill_odometry_message(
                            Odometry(),
                            topics.odom_from_state(x, y, yaw, v, omega),
                        )
                    )
                    goal_x, goal_y = loop.runner.goal_xy
                    self._goal_pub.publish(
                        topics.fill_goal_message(
                            PoseStamped(),
                            topics.goal_pose_from_goal(goal_x, goal_y),
                        )
                    )
                    self._objective_pub.publish(
                        topics.string_message(loop.runner.objective_type)
                    )
                for line in summarize_log_events(loop.drain_events()):
                    print(line, flush=True)
                if loop.finished or loop.timed_out or loop.max_samples_reached:
                    self.finished = True
                body_s = time.monotonic() - body_start
                self._last_body_s = body_s
                pub_s = max(0.0, body_s - loop.last_tick_s)
                self._last_breakdown = format_tick_breakdown(
                    loop.last_tick_phases, pub_s=pub_s
                )

        bridge = _JointBridge()
        print(
            f"MuJoCo harness: controller '{controller.get_name()}' on real"
            f" rclpy; {loop.engine.joint_count} joint(s);"
            f" dt={scenario.dt:g}s control={scenario.control_hz:g}Hz"
            f" seed={seed}.",
            flush=True,
        )
        for line in summarize_log_events(loop.drain_events()):
            print(line, flush=True)
        executor = SingleThreadedExecutor()
        executor.add_node(bridge)
        executor.add_node(controller)
        external_estop = False
        estop_deadline: float | None = None
        reason = "tests_complete"
        try:
            while rclpy.ok():
                if _estop_latched(estop_file):
                    if not external_estop:
                        if scenario.locomotion:
                            print(
                                "MuJoCo harness: emergency stop latched;"
                                " settling to STAND.",
                                flush=True,
                            )
                        else:
                            print(
                                "MuJoCo harness: emergency stop latched; halting.",
                                flush=True,
                            )
                        loop.runner.estop_active = True
                        external_estop = True
                        # Locomotion settles through the policy; joint tests
                        # halt immediately (pose is already held).
                        if scenario.locomotion:
                            estop_deadline = time.monotonic() + 5.0
                executor.spin_once(timeout_sec=min(1.0, loop.control_period * 4))
                if external_estop and (
                    estop_deadline is None
                    or bridge.finished
                    or time.monotonic() >= estop_deadline
                ):
                    reason = "estop"
                    break
                if bridge.finished:
                    if loop.finished:
                        reason = "tests_complete"
                    elif loop.timed_out:
                        reason = "timeout"
                    else:
                        reason = "sample_cap"
                    break
        finally:
            executor.shutdown()
            try:
                controller.destroy_node()
            except Exception:
                pass
            try:
                bridge.destroy_node()
            except Exception:
                pass
        result: dict[str, Any] = {
            "exit_code": EXIT_OK,
            "timed_out": False,
            "failure_summary": None,
        }
        result.update(loop.result_payload(reason))
        planned = len(loop.runner.tests)
        if scenario.locomotion:
            planned = len(getattr(loop.runner, "phases", None) or [None])
        result["suite_size"] = planned + (
            1 if scenario.estop_after_s is not None else 0
        )
        return result
    finally:
        loop.close()
        try:
            rclpy.shutdown()
        except Exception:
            pass


#: A wall-clock tick slower than this multiple of its period means the
#: controller is blocking the executor (sleep/busy loop/heavy callback):
#: physics only advances after callbacks return, so the run lags realtime,
#: telemetry arrives in bursts, and the wall-clock budget drains.
STALL_TICK_FACTOR = 5.0
MAX_STALL_WARNINGS = 3


def stall_warning(
    elapsed_s: float,
    period_s: float,
    warnings_sent: int,
    body_s: float | None = None,
    breakdown: str | None = None,
) -> str | None:
    """Explain a stalled control tick, or None when pacing is healthy.

    ``body_s`` is the measured harness tick-body duration (policy +
    physics + publish). When it is known, the warning splits the gap
    into harness vs controller time instead of guessing: a fast body
    proves the controller blocked the executor for the remainder.
    ``breakdown`` is the measured per-phase split (see
    :func:`app.runner.mujoco_loop.format_tick_breakdown`); when present
    it names the slow phase instead of guessing physics/policy.
    """
    if warnings_sent >= MAX_STALL_WARNINGS:
        return None
    if period_s <= 0 or elapsed_s <= period_s * STALL_TICK_FACTOR:
        return None
    if body_s is not None and body_s > period_s * STALL_TICK_FACTOR:
        detail = breakdown or "physics/policy slow?"
        return (
            f"Harness: control tick took {elapsed_s:.2f}s for a"
            f" {period_s:g}s period — the harness tick body itself took"
            f" {body_s:.2f}s ({detail}), so the run lags"
            " realtime and telemetry arrives in bursts."
        )
    if body_s is not None:
        blocked = max(0.0, elapsed_s - body_s)
        return (
            f"Harness: control tick took {elapsed_s:.2f}s for a"
            f" {period_s:g}s period (harness body {body_s:.3f}s) — the"
            f" controller is blocking the executor the other ~{blocked:.2f}s"
            " (sleep/busy loop/slow callback?), or the container is"
            " CPU-starved (cgroup throttling — a library sized its thread"
            " pool to the host cores instead of the --cpus quota), so the"
            " run lags realtime and telemetry arrives in bursts."
        )
    return (
        f"Harness: control tick took {elapsed_s:.2f}s for a"
        f" {period_s:g}s period — the controller is blocking the executor"
        " (sleep/busy loop/slow callback?) so the run lags realtime and"
        " telemetry arrives in bursts."
    )


def find_blocking_calls(source: str) -> list[int]:
    """Line numbers calling ``sleep()`` (any ``sleep``/``*.sleep`` call).

    Warn-only preflight for the harness: generation-time validation
    already rejects model output with sleep calls, but hand-edited
    controllers bypass that check, and a sleep in a timer/subscription
    callback stalls every tick. Unparseable source yields no lines —
    the loader reports its own precise error for that case.
    """
    import ast

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            is_sleep = func.id == "sleep"
        else:
            is_sleep = isinstance(func, ast.Attribute) and func.attr == "sleep"
        if is_sleep:
            lines.append(int(getattr(node, "lineno", 0) or 0))
    return sorted(lines)


def warn_blocking_calls(entry_path: Path, entry: str) -> None:
    """Log a warning per sleep call in the controller entry (max 3)."""
    try:
        source = entry_path.read_text(encoding="utf-8")
    except OSError:
        return
    for lineno in find_blocking_calls(source)[:3]:
        print(
            f"Harness: {entry}:{lineno} calls sleep() — if that runs in a"
            " timer/subscription callback it stalls every tick; use the"
            " timer period for pacing, not a sleep call.",
            flush=True,
        )


def _failure_summary(exc: Exception) -> str:
    """Human-readable harness failure, actionable when the cause is known.

    The local runner spawns the harness with the API's own interpreter, so
    a missing physics engine means the API itself runs outside the project
    environment. Say exactly that instead of a bare ModuleNotFoundError.
    """
    if isinstance(exc, ModuleNotFoundError):
        missing = (exc.name or "").split(".")[0]
        if missing in ("mujoco", "numpy", "onnxruntime"):
            return (
                f"MuJoCo physics is not installed for the harness Python"
                f" ({sys.executable}). Start the API from the project"
                " environment — services/api/.venv/bin/uvicorn app.main:app"
                " --port 8000 — or install the sim extras: cd services/api"
                " && .venv/bin/pip install -e '.[test]'."
            )
    return f"Harness failed: {exc}"


def main(argv: list[str] | None = None) -> int:
    _ensure_importable()
    parser = argparse.ArgumentParser(description="HeyWalk isolated-run harness.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--transport", default="loopback",
                        choices=("loopback", "rclpy"))
    parser.add_argument("--backend", default="auto",
                        choices=("auto", "mujoco", "differential_drive"))
    parser.add_argument("--entry", default=DEFAULT_ENTRY)
    parser.add_argument("--estop-file", default=None)
    parser.add_argument("--max-samples", default=200_000, type=int)
    args = parser.parse_args(argv)

    workspace = Path(args.workspace)
    out_dir = Path(args.out)
    estop_file = Path(args.estop_file) if args.estop_file else None
    try:
        world = load_world(workspace)
        from app.simulator.scenario import infer_backend

        backend = (
            infer_backend(world)
            if args.backend == "auto"
            else args.backend
        )
        if backend == "mujoco":
            runner = (
                run_mujoco_rclpy
                if args.transport == "rclpy"
                else run_mujoco_loopback
            )
        else:
            runner = run_rclpy if args.transport == "rclpy" else run_loopback
        result = runner(
            workspace, out_dir, args.seed, args.entry,
            estop_file, args.max_samples,
        )
    except Exception as exc:
        traceback.print_exc()
        result = {
            "exit_code": EXIT_HARNESS_ERROR,
            "reason": "harness_error",
            "samples": 0,
            "timed_out": False,
            "failure_summary": _failure_summary(exc),
        }
    write_result(out_dir, result)
    print(f"Harness finished: {json.dumps(result)}", flush=True)
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
