"""Automated joint-test runner + objective MuJoCo evaluation.

The platform owns the test suite: each case commands a joint target through
``/joint_targets``, lets the generated controller drive the MuJoCo model via
``/joint_commands``, and then judges the observed joint trajectory. The
agent never grades its own work.

A test passes only when the observed joint position reaches the target
within tolerance, the joint is quiescent (hold window), the velocity/effort
limits hold, and no disqualifying contact occurred. The run succeeds only
when every case passes and the run exits cleanly (AGENTS.md section 8).

Optional emergency-stop demonstration: after the joint cases, the runner
commands motion for ``estop_after_s`` seconds, latches ``/emergency_stop``,
and measures how long the physical system takes to become stationary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .scenario import JointTest, MujocoScenario, default_tests_for_joints

if TYPE_CHECKING:  # pragma: no cover - the physics engine only runs in the runner
    from .mujoco_engine import MujocoEngine

EVALUATOR_VERSION = "heywalk-mujoco-eval-0.1"
ESTOP_TEST_ID = "estop"


@dataclass
class _Active:
    test: JointTest | None
    joint_index: int
    started_at: float
    hold_started_at: float | None = None
    settled_at: float | None = None
    max_error: float = 0.0
    max_velocity: float = 0.0
    max_effort: float = 0.0
    contacts: int = 0
    max_contact_force: float = 0.0


class JointTestRunner:
    """Deterministic state machine that drives the scenario's test cases."""

    def __init__(
        self,
        engine: MujocoEngine,
        scenario: MujocoScenario,
    ) -> None:
        self.engine = engine
        self.scenario = scenario
        tests = scenario.tests or default_tests_for_joints(
            [
                (binding.name, binding.lower, binding.upper)
                for binding in engine.bindings
            ]
        )
        self.tests = tuple(tests)
        self.joint_index = {
            binding.name: index for index, binding in enumerate(engine.bindings)
        }
        unknown = [t.joint for t in self.tests if t.joint not in self.joint_index]
        if unknown:
            raise ValueError(
                f"Test suite names unknown joint(s) {', '.join(unknown)};"
                f" the robot exposes {', '.join(self.joint_index)}."
            )
        self.targets: list[float] = list(engine.home)
        self.estop_active = False
        self.results: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.index = -1
        self.finished = False
        self.phase = "joint"
        self.current_test: JointTest | None = None
        self._active: _Active | None = None
        self._estop_phase = "idle"  # idle | motion | stopping | done
        self._estop_motion_started = 0.0
        self._estop_latched_at: float | None = None
        self._estop_joint = 0
        self._begin_next_test(0.0)

    # ---- public state ----

    @property
    def current_test_id(self) -> str | None:
        if self.phase == "estop":
            return ESTOP_TEST_ID
        return self.current_test.id if self.current_test else None

    @property
    def all_tests_passed(self) -> bool:
        return bool(self.results) and all(r["passed"] for r in self.results)

    def target_payload(self) -> list[float]:
        return list(self.targets)

    def joint_efforts(self) -> list[float]:
        return self.engine.joint_efforts()

    def pre_tick(self) -> None:
        """Joint tests judge controller commands; nothing runs pre-physics."""

    def finalize(self, now: float) -> None:
        """Joint results stand as recorded when the harness ends early."""

    # ---- transitions ----

    def _event(self, kind: str, **payload: Any) -> None:
        self.events.append({"type": kind, **payload})

    def _begin_next_test(self, now: float) -> None:
        self.index += 1
        if self.index >= len(self.tests):
            self._begin_estop(now)
            return
        test = self.tests[self.index]
        self.engine.reset()
        self.phase = "joint"
        self.current_test = test
        self.targets = list(self.engine.home)
        joint_index = self.joint_index[test.joint]
        self.targets[joint_index] = test.target
        self._active = _Active(test=test, joint_index=joint_index, started_at=now)
        self._event(
            "test_started",
            test=test.id,
            joint=test.joint,
            target=test.target,
            tolerance=test.tolerance,
            timeout_s=test.timeout_s,
        )

    def _begin_estop(self, now: float) -> None:
        if self.scenario.estop_after_s is None or self._estop_phase != "idle":
            self._finish(now)
            return
        self.phase = "estop"
        self.current_test = None
        self._estop_phase = "motion"
        self._estop_motion_started = now
        self._estop_latched_at = None
        self.engine.reset()
        self.targets = list(self.engine.home)
        # Command a clear motion so the stop latency is measurable.
        binding = self.engine.bindings[0]
        lower = binding.lower
        upper = binding.upper
        if lower is not None and upper is not None:
            default_target = min(max(0.25, lower), upper)
            if abs(default_target) < 1e-6:
                default_target = min(max(-0.25, lower), upper)
        else:
            default_target = 0.5
        self._estop_joint = 0
        self.targets[0] = default_target
        self._event(
            "estop_armed",
            joint=binding.name,
            target=default_target,
            motion_s=self.scenario.estop_after_s,
        )

    def _finish(self, now: float) -> None:
        self.finished = True
        self.phase = "done"
        self._event("tests_complete", passed=self.all_tests_passed)

    def _record(self, now: float, *, passed: bool, reason: str) -> None:
        active = self._active
        if active is None or active.test is None:
            return
        positions = self.engine.joint_positions()
        final_position = float(positions[active.joint_index])
        test = active.test
        final_error = abs(final_position - test.target)
        self.results.append(
            {
                "id": test.id,
                "kind": test.kind,
                "joint": test.joint,
                "target": test.target,
                "tolerance": test.tolerance,
                "final_position": final_position,
                "final_error": final_error,
                "max_error": active.max_error,
                "max_velocity": active.max_velocity,
                "max_effort": active.max_effort,
                "settling_time": (
                    None
                    if active.settled_at is None
                    else max(0.0, active.settled_at - active.started_at)
                ),
                "duration": max(0.0, now - active.started_at),
                "contacts": active.contacts,
                "max_contact_force": active.max_contact_force,
                "passed": passed,
                "reason": reason,
            }
        )
        self._event(
            "test_finished",
            test=test.id,
            passed=passed,
            final_error=final_error,
            reason=reason,
        )
        self._active = None
        self._begin_next_test(now)

    # ---- per-control-step update ----

    def tick(self, now: float) -> None:
        if self.finished:
            return
        engine = self.engine
        if self.phase == "estop":
            self._tick_estop(now)
            return
        active = self._active
        if active is None or active.test is None:
            return
        test = active.test
        positions = engine.joint_positions()
        velocities = engine.joint_velocities()
        efforts = engine.joint_efforts()
        position = float(positions[active.joint_index])
        velocity = float(velocities[active.joint_index])
        error = abs(position - test.target)
        active.max_error = max(active.max_error, error)
        active.max_velocity = max(active.max_velocity, abs(velocity))
        active.max_effort = max(active.max_effort, abs(float(efforts[active.joint_index])))
        if engine.contacts:
            active.contacts += engine.contacts
            active.max_contact_force = max(
                active.max_contact_force, engine.max_contact_force
            )

        if engine.fallen:
            self._record(
                now,
                passed=False,
                reason=(
                    f"Robot fell during test {test.id!r} (trunk height"
                    f" {engine.trunk_z:.3f} m); a free-standing robot must"
                    " stay upright while tracking."
                ),
            )
            return

        within = (
            error <= test.tolerance
            and abs(velocity) <= self.scenario.joint_velocity_tolerance
        )
        if within and active.hold_started_at is None:
            active.hold_started_at = now
            if active.settled_at is None:
                active.settled_at = now
        elif not within:
            active.hold_started_at = None

        elapsed = now - active.started_at
        if within and active.hold_started_at is not None:
            if now - active.hold_started_at >= test.hold_s:
                self._record(
                    now,
                    passed=True,
                    reason=(
                        f"Reached {test.target:+.3f} rad within"
                        f" ±{test.tolerance:g} rad and held for"
                        f" {test.hold_s:g}s."
                    ),
                )
                return
        if elapsed >= test.timeout_s:
            if error > test.tolerance:
                reason = (
                    f"Joint {test.joint!r} did not reach {test.target:+.3f} rad"
                    f" within {test.timeout_s:g}s (final error"
                    f" {error:.3f} rad, tolerance {test.tolerance:g})."
                )
            elif abs(velocity) > self.scenario.joint_velocity_tolerance:
                reason = (
                    f"Joint {test.joint!r} reached the target but did not"
                    f" settle within {test.timeout_s:g}s (velocity"
                    f" {abs(velocity):.3f} rad/s)."
                )
            else:
                reason = f"Test {test.id!r} timed out after {test.timeout_s:g}s."
            self._record(now, passed=False, reason=reason)
            return
        if active.contacts and test.kind == "joint_move":
            # Contacts are recorded as evidence; a collision is disqualifying
            # for the run even if the joint still reaches the target.
            pass

    def _tick_estop(self, now: float) -> None:
        if self._estop_phase == "motion":
            if now - self._estop_motion_started >= self.scenario.estop_after_s:
                self.estop_active = True
                self._estop_latched_at = now
                self.engine.hold_current()
                self._estop_phase = "stopping"
                self._event("estop_latched", t=now)
            return
        if self._estop_phase == "stopping":
            self.engine.hold_current()
            velocities = self.engine.joint_velocities()
            moving = any(
                abs(value) > self.scenario.joint_velocity_tolerance
                for value in velocities
            )
            assert self._estop_latched_at is not None
            latency = now - self._estop_latched_at
            if not moving or latency >= self.scenario.estop_latency_limit:
                passed = not moving and latency <= self.scenario.estop_latency_limit
                reason = (
                    f"Motion stopped {latency:.3f}s after the emergency stop"
                    f" (limit {self.scenario.estop_latency_limit:g}s)."
                    if passed
                    else "Motion did not stop within"
                    f" {self.scenario.estop_latency_limit:g}s after the"
                    " emergency stop."
                )
                self.results.append(
                    {
                        "id": ESTOP_TEST_ID,
                        "kind": "estop",
                        "joint": self.engine.bindings[self._estop_joint].name,
                        "target": self.targets[self._estop_joint],
                        "tolerance": self.scenario.joint_velocity_tolerance,
                        "final_position": self.engine.joint_positions()[
                            self._estop_joint
                        ],
                        "final_error": 0.0,
                        "max_error": 0.0,
                        "max_velocity": max(
                            abs(v) for v in velocities
                        ),
                        "max_effort": 0.0,
                        "settling_time": latency,
                        "duration": latency,
                        "contacts": self.engine.contacts,
                        "max_contact_force": self.engine.max_contact_force,
                        "passed": passed,
                        "reason": reason,
                        "estop_latency": latency if passed else None,
                    }
                )
                self._event(
                    "test_finished",
                    test=ESTOP_TEST_ID,
                    passed=passed,
                    final_error=0.0,
                    reason=reason,
                )
                self._estop_phase = "done"
                self._finish(now)


# ---------------------------------------------------------------------------
# Objective evaluation
# ---------------------------------------------------------------------------


def _collision_events(samples: list[dict[str, Any]]) -> int:
    count = 0
    active = False
    for sample in samples:
        colliding = bool(sample.get("collision"))
        if colliding and not active:
            count += 1
        active = colliding
    return count


def _joint_path_length(samples: list[dict[str, Any]]) -> float:
    total = 0.0
    for previous, current in zip(samples, samples[1:]):
        prev_joints = previous.get("joints") or []
        cur_joints = current.get("joints") or []
        for a, b in zip(prev_joints, cur_joints):
            total += abs(float(b.get("position", 0.0)) - float(a.get("position", 0.0)))
    return total


def _max_joint_error(samples: list[dict[str, Any]]) -> float:
    worst = 0.0
    for sample in samples:
        for joint in sample.get("joints") or []:
            worst = max(
                worst,
                abs(float(joint.get("position", 0.0)) - float(joint.get("target", 0.0))),
            )
    return worst


def evaluate_mujoco(
    results: list[dict[str, Any]],
    samples: list[dict[str, Any]],
    *,
    scenario: MujocoScenario,
    exit_code: int | None = None,
    failure_summary: str | None = None,
    timed_out: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Judge a MuJoCo run from recorded test outcomes and joint telemetry."""
    reasons: list[str] = []
    total = len(results)
    passed = sum(1 for result in results if result.get("passed"))
    collision_count = _collision_events(samples)
    completion_time = float(samples[-1]["t"]) if samples else 0.0
    max_joint_error = max(
        (float(result.get("final_error", 0.0)) for result in results),
        default=0.0,
    )
    peak_error = _max_joint_error(samples)
    path_length = _joint_path_length(samples)
    estop_result = next(
        (
            result
            for result in results
            if result.get("kind") == "estop"
            or (
                result.get("kind") == "locomotion"
                and result.get("estop_latency") is not None
            )
        ),
        None,
    )
    estop_latency = (
        float(estop_result["estop_latency"])
        if estop_result and estop_result.get("estop_latency") is not None
        else None
    )

    if not samples:
        reasons.append("No MuJoCo telemetry was recorded for this run.")
    if total == 0:
        reasons.append("No automated joint tests were executed.")
    if timed_out:
        reasons.append(
            "Run exceeded the scenario time limit before all tests finished."
        )
    if collision_count:
        reasons.append(
            f"Collision/contact detected ({collision_count} event(s));"
            " the robot must complete the tests without disqualifying contacts."
        )
    for result in results:
        if result.get("passed"):
            continue
        kind = result.get("kind")
        if kind == "estop":
            label = "Emergency stop"
        elif kind == "locomotion":
            label = f"Locomotion {result.get('id')!r}"
        else:
            label = f"Test {result.get('id')!r}"
        detail = result.get("reason") or (
            f"final error {float(result.get('final_error', 0.0)):.3f}"
            f" rad, tolerance {float(result.get('tolerance', 0.0)):g}"
        )
        reasons.append(f"{label} failed: {detail}")
    if exit_code not in (None, 0):
        reasons.append(f"Controller process exited with code {exit_code}.")
    if failure_summary and not reasons:
        reasons.append(failure_summary)

    success = (
        bool(samples)
        and total > 0
        and passed == total
        and collision_count == 0
        and exit_code in (None, 0)
        and not timed_out
    )
    if success:
        if all(result.get("kind") == "locomotion" for result in results):
            if total == 1:
                reasons = [str(results[0].get("reason") or "Walk passed.")]
            else:
                reasons = [
                    f"All {total} locomotion phase(s) passed with no fall and"
                    " no disqualifying contact."
                ]
        else:
            reasons = [
                f"All {total} automated joint test(s) passed with no"
                " disqualifying collision."
            ]

    if success:
        run_status = "success"
    elif exit_code not in (None, 0):
        run_status = "error"
    elif timed_out:
        run_status = "timeout"
    else:
        run_status = "failed"

    report = {
        "success": success,
        "reasons": reasons,
        "evaluator_version": EVALUATOR_VERSION,
        "run_status": run_status,
        "tests": results,
    }
    metrics = {
        "success": success,
        "collision_count": collision_count,
        # Joint-space equivalents: final error (rad) and path length (rad).
        "goal_distance": max_joint_error,
        "completion_time": completion_time,
        "path_length": path_length,
        "emergency_stop_latency": estop_latency,
        "evaluator_version": EVALUATOR_VERSION,
        "tests_total": total,
        "tests_passed": passed,
        "max_joint_error": peak_error,
        "total_contacts": sum(
            int(result.get("contacts", 0)) for result in results
        ),
    }
    return report, metrics
