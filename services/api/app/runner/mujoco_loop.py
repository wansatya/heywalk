"""MuJoCo closed-loop driver for the isolated harness (AGENTS.md §5, §7).

Owns the MuJoCo engine, the automated joint-test runner, and telemetry
authoring. It is transport-agnostic: the harness wires it to either the
in-process loopback transport or real rclpy, using the same topic contract
(:mod:`app.runner.topics`).

Control loop per tick:

1. publish the current test target on ``/joint_targets``;
2. deliver the controller's latest ``/joint_commands`` to the MuJoCo position
   actuators (the emulated ros2_control command interface);
3. advance physics at the fixed timestep;
4. publish ``/joint_states`` from MuJoCo state;
5. let the test runner judge tracking, settling, velocity, and contacts.

The driver never judges success itself; it records evidence for the
objective evaluator.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..simulator.mujoco_engine import MujocoEngine, MujocoEngineError
from ..simulator.mujoco_tests import JointTestRunner
from ..simulator.robot_description import RobotInfo, find_model_file, parse_mjcf
from ..simulator.scenario import MujocoScenario
from . import topics

TELEMETRY_FILENAME = "telemetry.jsonl"
MAX_INVALID_COMMAND_LOGS = 3

#: Tick phases timed on every control step (seconds, wall clock).
TICK_PHASES = ("policy", "physics", "runner", "sample", "write")


def format_tick_breakdown(
    phases: dict[str, float], *, pub_s: float = 0.0
) -> str:
    """One-line stall diagnosis, e.g. ``policy=3.1ms physics=0.6ms ...``."""
    parts = [
        f"{name}={max(0.0, float(phases.get(name, 0.0))) * 1000.0:.1f}ms"
        for name in TICK_PHASES
    ]
    parts.append(f"pub={max(0.0, float(pub_s)) * 1000.0:.1f}ms")
    return " ".join(parts)


def load_robot_model(
    workspace: Path, scenario: MujocoScenario
) -> tuple[Path, RobotInfo]:
    """Resolve and validate the generated MJCF inside the run workspace."""
    candidate = workspace / scenario.model_path
    if candidate.is_file():
        model_path = scenario.model_path
    else:
        files: dict[str, str] = {}
        for path in sorted(workspace.rglob("*.xml")):
            if path.is_file():
                try:
                    files[path.relative_to(workspace).as_posix()] = path.read_text(
                        encoding="utf-8"
                    )
                except (OSError, UnicodeDecodeError):
                    continue
        model_path = find_model_file(files, scenario.model_path)
    path = (workspace / model_path).resolve()
    root = workspace.resolve()
    if path != root and root not in path.parents:
        raise MujocoEngineError(
            f"Robot model path escapes the workspace: {model_path!r}."
        )
    if not path.is_file():
        raise MujocoEngineError(f"Robot model not found: {model_path!r}.")
    info = parse_mjcf(path.read_text(encoding="utf-8"), path.name)
    return path, info


class MujocoLoop:
    """MuJoCo closed loop with test sequencing and telemetry recording."""

    def __init__(
        self,
        workspace: Path,
        out_dir: Path,
        scenario: MujocoScenario,
        *,
        max_samples: int = 200_000,
    ) -> None:
        self.workspace = workspace
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.scenario = scenario
        self.model_path, self.info = load_robot_model(workspace, scenario)
        self.engine = MujocoEngine(
            self.model_path, self.info, scenario=scenario
        )
        if self.engine.layout_skipped:
            skipped = ", ".join(self.engine.layout_skipped)
            print(
                "MuJoCo harness: scene layout ignored unknown"
                f" bodies: {skipped}.",
                flush=True,
            )
        if scenario.locomotion:
            from ..simulator.locomotion import LocomotionRunner

            self.runner = LocomotionRunner(self.engine, scenario)
        else:
            self.runner = JointTestRunner(self.engine, scenario)
        self.max_samples = max(1, max_samples)
        self.sim_time = 0.0
        self.sample_count = 0
        self.invalid_commands = 0
        self._invalid_logs = 0
        self._next_sample_at = 0.0
        self._handle = open(
            self.out_dir / TELEMETRY_FILENAME, "w", encoding="utf-8"
        )
        self.joint_names = [binding.name for binding in self.engine.bindings]
        # Per-phase wall time of the most recent tick (see tick()); the
        # harness stall warning reports these instead of guessing.
        self.last_tick_phases: dict[str, float] = dict.fromkeys(TICK_PHASES, 0.0)
        self.last_tick_s = 0.0
        self._write(self._sample())

    # ---- controller-facing inputs ----

    def on_joint_commands(self, message: Any) -> None:
        if self.scenario.locomotion:
            # The walking policy owns the joints on locomotion runs; the
            # controller steers with /cmd_vel instead. Accepted silently so
            # the dual-mode reference controller runs unmodified.
            return
        values = topics.commands_from_message(message)
        if values is None:
            self._invalid_command("Malformed /joint_commands array ignored.")
            return
        if not self.engine.set_commands(values):
            self._invalid_command(
                f"/joint_commands had {len(values)} value(s); expected"
                f" {self.engine.joint_count}. Command ignored."
            )

    def _invalid_command(self, line: str) -> None:
        self.invalid_commands += 1
        if self._invalid_logs < MAX_INVALID_COMMAND_LOGS:
            self._invalid_logs += 1
            print(f"MuJoCo harness: {line}", flush=True)

    def on_cmd_vel(self, message: Any) -> None:
        """Steer the locomotion policy from a controller Twist."""
        twist = topics.twist_from_message(message)
        if twist is None:
            self._invalid_command("Malformed /cmd_vel message ignored.")
            return
        vx, vy, yaw = twist
        if not self.scenario.locomotion:
            self._invalid_command(
                "/cmd_vel is only honoured on locomotion runs."
            )
            return
        self.runner.set_twist(vx, vy, yaw)

    # ---- ROS-facing outputs ----

    def joint_state_payload(self) -> dict[str, Any]:
        return topics.joint_state_from_values(
            [binding.name for binding in self.engine.bindings],
            self.engine.joint_positions(),
            self.engine.joint_velocities(),
            self.engine.joint_efforts(),
        )

    def target_payload(self) -> list[float]:
        return self.runner.target_payload()

    @property
    def estop_active(self) -> bool:
        return self.runner.estop_active

    @property
    def finished(self) -> bool:
        return self.runner.finished

    @property
    def timed_out(self) -> bool:
        limit = self.scenario.time_limit
        return limit is not None and self.sim_time >= limit and not self.finished

    def drain_events(self) -> list[dict[str, Any]]:
        events, self.runner.events = self.runner.events, []
        return events

    def results(self) -> list[dict[str, Any]]:
        return [dict(result) for result in self.runner.results]

    # ---- control loop ----

    def tick(self) -> dict[str, Any]:
        scenario = self.scenario
        if self.runner.estop_active and not scenario.locomotion:
            # Safety: the harness holds the current pose regardless of what
            # the controller publishes after the stop is latched. Locomotion
            # runs instead command zero velocity through the walking policy
            # (see LocomotionRunner.pre_tick): freezing a striding biped
            # mid-pose would topple it, while standing still is a safe stop.
            self.engine.hold_current()
        start = time.monotonic()
        self.runner.pre_tick()
        pre = time.monotonic()
        self.engine.advance(scenario.physics_steps_per_control)
        stepped = time.monotonic()
        self.sim_time += scenario.control_period
        self.runner.tick(self.sim_time)
        judged = time.monotonic()
        sample = self._sample()
        sampled = time.monotonic()
        if self.sim_time + 1e-12 >= self._next_sample_at:
            self._write(sample)
            interval = 1.0 / scenario.telemetry_hz
            self._next_sample_at += interval
            if self._next_sample_at <= self.sim_time:
                self._next_sample_at = self.sim_time + interval
        end = time.monotonic()
        self.last_tick_phases = {
            "policy": pre - start,
            "physics": stepped - pre,
            "runner": judged - stepped,
            "sample": sampled - judged,
            "write": end - sampled,
        }
        self.last_tick_s = end - start
        return sample

    def _sample(self) -> dict[str, Any]:
        snapshot = self.engine.snapshot()
        sample: dict[str, Any] = {
            "t": self.sim_time,
            "x": snapshot["x"],
            "y": snapshot["y"],
            "theta": snapshot["theta"],
            "v": snapshot["v"],
            "omega": snapshot["omega"],
            "joints": snapshot["joints"],
            "collision": snapshot["collision"],
            "contacts": snapshot["contacts"],
            "max_contact_force": snapshot["max_contact_force"],
            "goal_reached": self.runner.finished
            and self.runner.all_tests_passed,
            "estop_active": self.runner.estop_active,
            "min_range": None,
            "test_id": self.runner.current_test_id,
            "fallen": snapshot.get("fallen", False),
            "trunk_z": snapshot.get("trunk_z", 0.0),
            "expected_contacts": snapshot.get("expected_contacts", 0),
        }
        commanded = getattr(self.runner, "commanded_twist", None)
        if commanded is not None:
            sample["twist"] = [float(v) for v in commanded]
        if "sensors" in snapshot:
            sample["sensors"] = snapshot["sensors"]
        if "bodies" in snapshot:
            sample["bodies"] = snapshot["bodies"]
        return sample

    def _write(self, sample: dict[str, Any]) -> None:
        if self.sample_count >= self.max_samples:
            return
        normalized = topics.normalize_telemetry_sample(sample)
        self._handle.write(json.dumps(normalized) + "\n")
        self._handle.flush()
        self.sample_count += 1

    # ---- summary ----

    def result_payload(self, reason: str) -> dict[str, Any]:
        """Structured outcome for ``result.json`` (tests are evidence)."""
        self.runner.finalize(self.sim_time)
        results = self.results()
        return {
            "reason": reason,
            "samples": self.sample_count,
            "sim_time": self.sim_time,
            "sim_timed_out": reason == "timeout",
            "joints": [binding.name for binding in self.engine.bindings],
            "tests": results,
            "tests_passed": sum(1 for r in results if r.get("passed")),
            "tests_total": len(results),
            "invalid_commands": self.invalid_commands,
            "model": self.model_path.name,
        }

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()
        self.engine.close()

    @property
    def all_tests_passed(self) -> bool:
        return self.runner.all_tests_passed

    @property
    def elapsed(self) -> float:
        return self.sim_time

    @property
    def max_samples_reached(self) -> bool:
        return self.sample_count >= self.max_samples

    @property
    def sample_interval(self) -> float:
        return 1.0 / self.scenario.telemetry_hz if self.scenario.telemetry_hz else 0.0

    @property
    def control_period(self) -> float:
        return self.scenario.control_period


def summarize_log_events(events: list[dict[str, Any]]) -> list[str]:
    """Render test-runner events as harness log lines."""
    lines: list[str] = []
    for event in events:
        kind = event.get("type")
        if kind == "test_started":
            lines.append(
                f"Test {event.get('test')}: move joint {event.get('joint')!r}"
                f" to {float(event.get('target', 0.0)):+.3f} rad"
                f" (tolerance ±{float(event.get('tolerance', 0.0)):g})."
            )
        elif kind == "test_finished":
            verdict = "PASS" if event.get("passed") else "FAIL"
            lines.append(
                f"Test {event.get('test')}: {verdict} — {event.get('reason')}"
            )
        elif kind == "tests_complete":
            passed = "all tests passed" if event.get("passed") else "failures recorded"
            lines.append(f"Automated test suite finished: {passed}.")
        elif kind == "locomotion_started":
            goal = event.get("goal") or [0.0, 0.0]
            objective = event.get("objective") or "go_to"
            target_body = event.get("target_body")
            if objective == "wander":
                lines.append(
                    f"Locomotion {event.get('test')}: wander with no"
                    " destination; the verdict is survival (no fall, no"
                    " disqualifying contact)."
                )
            elif objective == "kick":
                lines.append(
                    f"Locomotion {event.get('test')}: kick the ball at"
                    f" ({float(goal[0]):.2f}, {float(goal[1]):.2f}); the"
                    " verdict needs ball contact and real displacement."
                )
            elif objective == "touch":
                lines.append(
                    f"Locomotion {event.get('test')}: walk toward"
                    f" ({float(goal[0]):.2f}, {float(goal[1]):.2f}) and"
                    " stop on arena wall contact."
                )
            else:
                destination = (
                    f"the {target_body} prop"
                    if target_body
                    else "the goal"
                )
                lines.append(
                    f"Locomotion {event.get('test')}: walk to {destination}"
                    f" at ({float(goal[0]):.2f}, {float(goal[1]):.2f}) within"
                    f" {float(event.get('radius', 0.0)):g} m,"
                    f" {float(event.get('phase_window_s', 0.0)):g}s window."
                )
        elif kind == "locomotion_kick":
            lines.append(
                f"Locomotion {event.get('test')}: ball contacted at"
                f" t={float(event.get('t', 0.0)):.2f}s; pushing it away."
            )
        elif kind == "locomotion_phase":
            goal = event.get("goal") or [0.0, 0.0]
            lines.append(
                f"Locomotion {event.get('test')}: phase {event.get('phase')}"
                f"/{event.get('phases')} ({event.get('objective')}) — goal"
                f" ({float(goal[0]):.2f}, {float(goal[1]):.2f}),"
                f" {float(event.get('phase_window_s', 0.0)):g}s window."
            )
        elif kind == "locomotion_settling":
            lines.append(
                f"Locomotion {event.get('test')}: standing still to settle"
                f" ({event.get('outcome')})."
            )
        elif kind == "locomotion_finished":
            verdict = "PASS" if event.get("passed") else "FAIL"
            lines.append(
                f"Locomotion {event.get('test')}: {verdict} —"
                f" {event.get('reason')}"
            )
        elif kind == "estop_armed":
            lines.append(
                f"Emergency-stop test armed: joint {event.get('joint')!r}"
                f" moves for {float(event.get('motion_s', 0.0)):g}s before the stop."
            )
        elif kind == "estop_latched":
            lines.append(
                f"Emergency stop latched at t={float(event.get('t', 0.0)):.2f}s."
            )
    return lines
