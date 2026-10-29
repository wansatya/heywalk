"""In-process ROS 2 topic transport + closed-loop driver (dev/test only).

LoopbackTransport implements the topic contract from
:mod:`app.runner.topics` as deterministic in-memory pub/sub: same topic
names, same callback structure as the real rclpy transport, so generated
controllers run unmodified. ClosedLoop owns the deterministic simulator,
consumes /cmd_vel commands, and records the timestamped telemetry trace.

Production runs use real ROS 2 Jazzy pub/sub inside the Docker runner; this
module backs the local subprocess runner and unit tests only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..simulator.engine import Simulator
from ..simulator.world import WorldConfig
from . import topics

_ACTIVE: LoopbackTransport | None = None


def install_transport(transport: LoopbackTransport | None) -> None:
    """Install the transport the shim nodes bind to (harness-owned)."""
    global _ACTIVE
    _ACTIVE = transport


def active_transport() -> LoopbackTransport | None:
    return _ACTIVE


class LoopbackTransport:
    """Deterministic in-memory pub/sub over the HeyWalk topic contract."""

    def __init__(self) -> None:
        self.time = 0.0
        self._subscriptions: dict[str, list[Callable[[Any], None]]] = {}
        self._queue: list[tuple[str, Any]] = []
        self._timers: list[Any] = []
        self._timer_next: dict[int, float] = {}

    # ---- pub/sub ----

    def add_subscription(
        self, topic: str, callback: Callable[[Any], None]
    ) -> None:
        self._subscriptions.setdefault(topic, []).append(callback)

    def publish(self, topic: str, message: Any) -> None:
        self._queue.append((topic, message))

    def dispatch(self) -> None:
        """Deliver every queued message in publish order (deterministic)."""
        queued, self._queue = self._queue, []
        for topic, message in queued:
            for callback in list(self._subscriptions.get(topic, ())):
                callback(message)

    # ---- timers ----

    def add_timer(self, timer: Any) -> None:
        self._timers.append(timer)
        self._timer_next[id(timer)] = self.time + float(timer.timer_period_s)

    def advance(self, dt: float) -> None:
        """Advance sim time, fire due timers, then dispatch messages."""
        self.time += dt
        for timer in list(self._timers):
            if getattr(timer, "is_cancelled", False):
                continue
            key = id(timer)
            due = self._timer_next.get(key, self.time)
            if self.time + 1e-12 >= due:
                timer.callback()
                period = float(timer.timer_period_s)
                self._timer_next[key] = due + period if period > 0 else self.time
        self.dispatch()


class ClosedLoop:
    """Simulator side of the control loop with telemetry collection.

    Consumes velocity commands (from /cmd_vel on either transport), steps
    the deterministic simulator at its fixed timestep, and appends one
    validated telemetry sample per step to ``telemetry.jsonl``.
    """

    def __init__(
        self,
        world: WorldConfig,
        seed: int,
        out_dir: str | Path,
        *,
        max_samples: int = 200_000,
    ) -> None:
        self.world = world
        self.seed = seed
        self.sim = Simulator(world, seed=seed)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.max_samples = max(1, max_samples)
        self.command: tuple[float, float] = (0.0, 0.0)
        self.sample_count = 0
        self._telemetry_path = self.out_dir / "telemetry.jsonl"
        self._handle = open(self._telemetry_path, "w", encoding="utf-8")
        initial = self.sim.reset()
        self._write(initial)

    # ---- ROS-facing inputs ----

    def on_cmd_vel(self, linear_x: Any, angular_z: Any) -> None:
        self.command = topics.twist_to_command(linear_x, angular_z)

    def on_twist_message(self, message: Any) -> None:
        try:
            linear_x = message.linear.x
            angular_z = message.angular.z
        except AttributeError:
            linear_x, angular_z = 0.0, 0.0
        self.on_cmd_vel(linear_x, angular_z)

    # ---- stepping ----

    def tick(self, estop_active: bool = False) -> dict[str, Any]:
        v, omega = self.command
        sample = self.sim.step(v, omega, estop_active=estop_active)
        self._write(sample)
        return sample

    def odom_payload(self) -> dict[str, Any]:
        return topics.odom_from_state(
            self.sim.x, self.sim.y, self.sim.theta, self.sim.v, self.sim.omega
        )

    def scan_payload(self) -> dict[str, Any]:
        ranges = self.sim.scan()
        world = self.world
        return topics.scan_from_ranges(
            ranges,
            angle_min=-world.scan_fov / 2.0,
            angle_max=world.scan_fov / 2.0,
            range_min=0.02,
            range_max=world.scan_max_range,
        )

    def goal_payload(self) -> dict[str, Any]:
        return topics.goal_pose_from_goal(
            self.world.goal.x, self.world.goal.y
        )

    def objective_payload(self) -> str:
        """Current phase type for /objective_state.

        The 2D arena has exactly one phase — drive to the goal region — so
        it always reports ``go_to``. Locomotion runs on the MuJoCo harness
        report the live phase of the objective sequence instead.
        """
        return topics.ARENA_OBJECTIVE_TYPE

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    # ---- internals ----

    def _write(self, sample: dict[str, Any]) -> None:
        normalized = topics.normalize_telemetry_sample(sample)
        if self.sample_count >= self.max_samples:
            return
        self._handle.write(json.dumps(normalized) + "\n")
        self._handle.flush()
        self.sample_count += 1
