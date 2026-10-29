"""Locomotion runs: walk to the flag with the upstream policy (AGENTS.md §7).

A locomotion run replaces the automated joint-test suite with a single
goal-directed check: the harness drives the vendored upstream walking policy
at 50 Hz while the generated controller steers it with ``/cmd_vel`` Twist
messages (forward m/s, yaw rad/s). The runner records walking evidence —
distance travelled, closest approach to the flag, falls, disqualifying
contacts, emergency-stop latency — and the objective evaluator judges the
run from that evidence, never from the controller's self-report.

Pass criteria (all required):

1. the trunk comes within ``locomotion_goal_radius`` of the goal — the flag,
   a point declared by the objective, a named prop body, or the ball,
2. the robot never falls (``fall_trunk_z``),
3. no disqualifying contact occurs (foot soles, the flag, the aim prop, and
   the ball are excluded),
4. the run ends normally (goal reached or duration elapsed).

A kick run (objective ``kick``) aims at the dynamic ball: the duck keeps
pushing for ``KICK_PUSH_S`` after the first ball contact and the verdict needs
the ball to have actually rolled at least ``KICK_MIN_DISPLACEMENT`` — a run
that stops short or merely brushes past it fails with measured evidence.

On emergency stop the runner commands zero velocity through the policy —
the duck stands still — instead of freezing the joints mid-stride, and
measures how fast the base settles.

A run never ends mid-stride either: reaching the goal starts a settle
phase that first creeps toward the flag center and then stands still,
so the duck stops NEAR the flag (within GOAL_STOP_RADIUS) in a balanced
STAND — not frozen at the pass-radius edge halfway there. Exhausting
the walk window settles in place instead (the controller failed to
reach, so there is nothing to creep toward). Freezing the STAND
keyframe open-loop is not an option — the pose is an unstable equilibrium
and topples without feedback — so the settled stand is the policy
balancing at STAND, feet planted, trunk upright.
"""

from __future__ import annotations

import math
from typing import Any

from .microduck.model import (
    BALL_BODY,
    FLAG_BODY,
    PROP_LABELS,
    ball_geom_names,
    wall_geom_names,
)
from .microduck.policy import (
    ACTUATOR_ORDER,
    POLICY_HZ,
    PolicyError,
    VelStandPolicy,
)
from .mujoco_engine import MujocoEngineError

LOCOMOTION_TEST_ID = "walk_to_flag"
ESTOP_SETTLE_SPEED = 0.05
ESTOP_SETTLE_YAW_RATE = 0.1
ESTOP_SETTLE_S = 1.0
#: Final stop distance from the flag center (m). The pass radius stays at
#: ``locomotion_goal_radius`` (0.3 m), but the settle phase creeps inward
#: until the trunk is within this distance, so a pass stops NEAR the flag
#: instead of freezing at the radius edge halfway there.
GOAL_STOP_RADIUS = 0.1
#: Creep speed during goal settle (m/s commanded; actual is slower).
SETTLE_CREEP_SPEED = 0.2
SETTLE_TURN_GAIN = 2.0
SETTLE_MAX_TURN = 0.5
#: Stand-settle after the goal (creep + balanced STAND) or the walk window
#: (stand in place) before the verdict. 4 s gives the creep time to close
#: ~0.2 m at the policy's pace and still stand balanced for the verdict.
GOAL_SETTLE_S = 4.0
#: Kick runs keep pushing the ball this long after the first contact, so the
#: measured displacement is a kick, not a graze.
KICK_PUSH_S = 1.5
#: Minimum ball travel (m) a kick run must produce to pass.
KICK_MIN_DISPLACEMENT = 0.10


class LocomotionRunner:
    """Task-objective walking check with the upstream locomotion policy.

    The objectives (scenario.phases) run as an ordered sequence with no
    cap: each completed phase records its own result and the next phase's
    goal is published on /goal_pose, so one run can e.g. kick the ball
    and then walk to the flag. go_to reaches a point (the flag by
    default) and stops, touch ends passing on arena wall contact, kick
    needs measured ball displacement, and wander roams until the window
    ends (so it must be last) with survival (no fall, no disqualifying
    contact) as the verdict. The run passes only when every phase does.
    """

    def __init__(self, engine: Any, scenario: Any) -> None:
        if getattr(engine, "base_fixed", False):
            raise MujocoEngineError(
                "Locomotion runs need a free-standing robot: the walking"
                " policy balances a free trunk, so rerun with fix_base=false"
                " (the API sets this automatically for locomotion runs)."
            )
        if abs(float(scenario.control_hz) - POLICY_HZ) > 1e-6:
            raise MujocoEngineError(
                f"Locomotion runs need control_hz={POLICY_HZ:g} (the walking"
                f" policy runs at {POLICY_HZ:g} Hz); got"
                f" {float(scenario.control_hz):g}."
            )
        if tuple(engine.joint_names) != ACTUATOR_ORDER:
            raise PolicyError(
                "Locomotion runs need the MicroDuck joint layout; the"
                " walking policy only fits the official 14-joint model."
            )
        flag = engine.body_position(FLAG_BODY)
        if flag is None:
            raise MujocoEngineError(
                f"Locomotion runs need the {FLAG_BODY!r} goal body in the"
                " model; install the MicroDuck starter robot."
            )
        self.engine = engine
        self.scenario = scenario
        self.policy = VelStandPolicy()
        self.tests: tuple[Any, ...] = ()
        self.twist: tuple[float, float, float] = tuple(
            float(v) for v in scenario.locomotion_twist
        )  # type: ignore[assignment]
        self.targets: list[float] = list(engine.home)
        self.estop_active = False
        self.results: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.finished = False
        # Ordered objective phases (1+, no cap); single-phase runs behave
        # exactly as before. Phase 0 state is entered below.
        phases = getattr(scenario, "objectives", None) or (
            getattr(scenario, "objective", None),
        )
        self.phases: list[Any] = [p for p in phases if p is not None] or [
            getattr(scenario, "objective", None)
        ]
        self.start_xy = engine.base_pose()[0:2]
        self.flag_xy = (float(flag[0]), float(flag[1]))
        # Fail fast on any phase's static requirements (missing ball/prop),
        # with the same errors a single-phase run raises at construction.
        for phase in self.phases:
            self._resolve_goal(phase)
        self.distance_m = 0.0
        self.min_trunk_z = float(engine.trunk_z or 0.0)
        self.contact_ticks = 0
        self._estop_latched_at: float | None = None
        self.estop_latency: float | None = None
        self._last_xy = self.start_xy
        self.phase_index = -1
        # Every phase gets its own ``locomotion_duration_s`` window counted
        # from the moment the phase starts (see ``phase_deadline``). One
        # shared window starved later phases: a kick that used most of the
        # budget left the following go_to phase failing with "not reached"
        # even though the controller was still walking toward the target.
        self.phase_started_at = 0.0
        self.phase_deadline = 0.0
        self.phase_ended_at: float | None = None
        self._enter_phase(0)

    def _resolve_goal(self, objective: Any) -> tuple[float, float]:
        """Goal point for one phase (live positions, validated)."""
        objective_type = str(getattr(objective, "type", None) or "go_to")
        target_body = getattr(objective, "target_body", None)
        target = getattr(objective, "target", None)
        if objective_type == "kick":
            self._require_ball_goal_geoms()
            ball = self.engine.body_position(BALL_BODY)
            if ball is None:
                raise MujocoEngineError(
                    f"Kick runs need the {BALL_BODY!r} prop in the model;"
                    " install the MicroDuck starter robot."
                )
            return (float(ball[0]), float(ball[1]))
        if target_body is not None:
            body = self.engine.body_position(str(target_body))
            if body is None:
                raise MujocoEngineError(
                    f"Objective target body {target_body!r} is not in"
                    " the model; use one of the composed props"
                    " (env_box_1, env_box_2, env_flag, env_ball)."
                )
            return (float(body[0]), float(body[1]))
        if target is not None:
            return (float(target[0]), float(target[1]))
        return self.flag_xy

    def _enter_phase(self, index: int, now: float = 0.0) -> None:
        """Start phase ``index``: fresh goal, fresh window, fresh evidence.

        The harness republishes /goal_pose from goal_xy every tick, so the
        running controller steers to the new goal with no restart. This
        phase's duration window starts here, not at the run start.
        """
        phase = self.phases[index]
        self.phase_index = index
        self.phase_started_at = float(now)
        self.phase_deadline = float(now) + float(
            self.scenario.locomotion_duration_s
        )
        self.phase_ended_at = None
        self.objective_type = str(getattr(phase, "type", None) or "go_to")
        self.target_body = getattr(phase, "target_body", None)
        self.goal_xy = self._resolve_goal(phase)
        self.min_flag_m = self._flag_distance()
        self.reached = False
        self.touched = False
        self.ball_touched = False
        self._ball_start_xy = self._ball_xy()
        self._kick_end_at = None
        self._settle_until = None
        self._settle_outcome = None
        if index == 0:
            self._event(
                "locomotion_started",
                test=self.phase_id,
                objective=self.objective_type,
                target_body=self.target_body,
                goal=[self.goal_xy[0], self.goal_xy[1]],
                radius=float(self.scenario.locomotion_goal_radius),
                phase_window_s=float(self.scenario.locomotion_duration_s),
                phases=[
                    str(getattr(p, "type", None) or "go_to")
                    for p in self.phases
                ],
            )
        else:
            self._event(
                "locomotion_phase",
                test=self.phase_id,
                phase=index + 1,
                phases=len(self.phases),
                objective=self.objective_type,
                target_body=self.target_body,
                goal=[self.goal_xy[0], self.goal_xy[1]],
                phase_window_s=float(self.scenario.locomotion_duration_s),
            )

    @property
    def phase_id(self) -> str:
        """Result id for the current phase (legacy id when single-phase)."""
        if len(self.phases) == 1:
            return LOCOMOTION_TEST_ID
        return (
            f"{LOCOMOTION_TEST_ID}_p{self.phase_index + 1}_{self.objective_type}"
        )

    def _require_ball_goal_geoms(self) -> None:
        """Kick runs must whitelist the ball, or their own aim would fail."""
        ball = set(ball_geom_names())
        if not ball or not ball <= set(self.scenario.goal_geoms):
            raise MujocoEngineError(
                "Kick runs must list the ball collision geoms"
                f" ({', '.join(sorted(ball))}) in goal_geoms, so touching the"
                " ball is the objective instead of a disqualifying contact."
            )

    def _ball_xy(self) -> tuple[float, float] | None:
        try:
            position = self.engine.body_position(BALL_BODY)
        except Exception:
            return None
        return None if position is None else (float(position[0]), float(position[1]))

    @property
    def ball_displacement_m(self) -> float:
        """How far the ball has rolled from its start (0 without a ball)."""
        current = self._ball_xy()
        if current is None or self._ball_start_xy is None:
            return 0.0
        return math.hypot(
            current[0] - self._ball_start_xy[0],
            current[1] - self._ball_start_xy[1],
        )

    # ---- public state (JointTestRunner-compatible surface) ----

    @property
    def current_test_id(self) -> str | None:
        return None if self.finished else self.phase_id

    @property
    def all_tests_passed(self) -> bool:
        return bool(self.results) and all(r["passed"] for r in self.results)

    def target_payload(self) -> list[float]:
        return list(self.targets)

    def set_twist(self, vx: float, vy: float, yaw: float) -> None:
        """Steer the policy from a controller Twist (clamped to ±1)."""
        self.twist = (
            max(-1.0, min(1.0, float(vx))),
            max(-1.0, min(1.0, float(vy))),
            max(-1.0, min(1.0, float(yaw))),
        )

    @property
    def commanded_twist(self) -> tuple[float, float, float]:
        """Twist actually sent to the policy.

        Zero on emergency stop and while standing still. During the
        goal-reached settle the runner creeps toward the flag center
        until within GOAL_STOP_RADIUS, then stands — so the final stop
        is NEAR the flag, not at the pass-radius edge.
        """
        if self.estop_active:
            return (0.0, 0.0, 0.0)
        if self._settle_until is not None:
            if self._settle_outcome == "goal_reached":
                creep = self._creep_to_flag()
                if creep is not None:
                    return creep
            return (0.0, 0.0, 0.0)
        return self.twist

    def _creep_to_flag(self) -> tuple[float, float, float] | None:
        """Slow approach twist, or None when close enough to stand.

        Only the flag gets the creep-close treatment. A prop goal (crate) or
        a kick stops where it is: creeping into a crate would be a
        disqualifying contact, and creeping into the ball after a kick would
        chase the very thing the run measures.
        """
        if self.objective_type == "kick" or self.target_body is not None:
            return None
        try:
            x, y, yaw, _, _ = self.engine.base_pose()
        except Exception:
            return None
        goal_x, goal_y = self.goal_xy
        distance = math.hypot(goal_x - x, goal_y - y)
        if distance <= GOAL_STOP_RADIUS:
            return None
        heading = math.atan2(goal_y - y, goal_x - x)
        error = (heading - yaw + math.pi) % (2.0 * math.pi) - math.pi
        turn = max(
            -SETTLE_MAX_TURN, min(SETTLE_MAX_TURN, SETTLE_TURN_GAIN * error)
        )
        return (SETTLE_CREEP_SPEED, 0.0, turn)

    # ---- control loop ----

    def pre_tick(self) -> None:
        """Run one 50 Hz policy step before physics advances."""
        if self.finished:
            return
        twist = self.commanded_twist
        readings = self.engine.sensor_readings()
        targets = self.policy.step(
            {
                "ang_vel": readings["imu_ang_vel"],
                "quat": self.engine.root_orientation(),
                "joint_pos": self.engine.joint_positions(),
                "joint_vel": self.engine.joint_velocities(),
                "twist": twist,
                "head": (0.0, 0.0, 0.0, 0.0),
                "body": (0.0,) * 6,
            }
        )
        self.targets = [float(v) for v in targets]
        self.engine.set_commands(self.targets)

    def tick(self, now: float) -> None:
        """Accumulate walking evidence after physics advances."""
        if self.finished:
            return
        scenario = self.scenario
        x, y, _, v, omega = self.engine.base_pose()
        step = math.hypot(x - self._last_xy[0], y - self._last_xy[1])
        self.distance_m += step
        self._last_xy = (x, y)
        self.min_flag_m = min(self.min_flag_m, self._flag_distance())
        self.min_trunk_z = min(self.min_trunk_z, float(self.engine.trunk_z))
        if self.engine.contacts > 0:
            self.contact_ticks += 1
        if (
            scenario.estop_after_s is not None
            and now >= scenario.estop_after_s
            and not self.estop_active
        ):
            self.estop_active = True
            self._event("estop_latched", t=now)
        if self.estop_active and self._estop_latched_at is None:
            self._estop_latched_at = now
        if (
            self._estop_latched_at is not None
            and self.estop_latency is None
            and abs(v) < ESTOP_SETTLE_SPEED
            and abs(omega) < ESTOP_SETTLE_YAW_RATE
        ):
            self.estop_latency = now - self._estop_latched_at
        if self.engine.fallen:
            self._finish(now, "fell")
        elif self._settle_until is not None:
            if now >= self._settle_until:
                self._finish(now, self._settle_outcome or "run_ended")
        elif (
            self._estop_latched_at is not None
            and now - self._estop_latched_at >= ESTOP_SETTLE_S
        ):
            self._finish(now, "estop_settled")
        elif self.objective_type == "kick":
            # Latched contact: once the ball has been touched the push window
            # runs to its end even after the ball rolls out of contact, so
            # the verdict is recorded as a kick instead of waiting out the
            # whole window.
            if self._ball_touched_geom() and not self.ball_touched:
                self.ball_touched = True
                self._kick_end_at = now + KICK_PUSH_S
                self._event("locomotion_kick", test=self.phase_id, t=now)
            if self.ball_touched and self._kick_end_at is not None:
                if now >= self._kick_end_at:
                    self._phase_succeeded(now, "ball_kicked")
            elif now >= self.phase_deadline:
                self._window_elapsed(now)
        elif self.objective_type == "touch" and self._touched_wall():
            self.touched = True
            self._phase_succeeded(now, "wall_touched")
        elif (
            self.objective_type == "go_to"
            and self.min_flag_m <= float(scenario.locomotion_goal_radius)
        ):
            self.reached = True
            self._phase_succeeded(now, "goal_reached")
        elif now >= self.phase_deadline:
            if self.objective_type == "wander":
                self._phase_succeeded(now, "wander_complete")
            else:
                self._window_elapsed(now)

    @property
    def _is_last_phase(self) -> bool:
        return self.phase_index >= len(self.phases) - 1

    def _phase_succeeded(self, now: float, outcome: str) -> None:
        """A phase trigger fired: settle (last) or record and advance."""
        if self._is_last_phase:
            self._begin_settle(now, outcome)
        elif self.contact_ticks:
            # A disqualifying contact already dooms the run: fail now
            # instead of walking the remaining phases.
            self._finish(now, outcome)
        else:
            passed, reason = self._verdict(now, outcome)
            if not passed:
                self._finish(now, outcome)
            else:
                self._record_result(now, outcome, passed, reason)
                self._enter_phase(self.phase_index + 1, now)

    def _window_elapsed(self, now: float) -> None:
        """This phase's own duration window ran out without meeting it."""
        if self._is_last_phase:
            self._begin_settle(now, "duration_elapsed")
        else:
            # A phase that never completes fails the run: the sequence is
            # ordered, so the remaining phases are not graded.
            self._finish(now, "duration_elapsed")

    def _begin_settle(self, now: float, outcome: str) -> None:
        """Settle before the verdict: creep to the flag, then stand.

        Goal runs creep toward the flag center (see commanded_twist)
        before standing, so the final pose is NEAR the flag in a
        balanced STAND. Other outcomes stand in place.
        """
        self._settle_until = now + GOAL_SETTLE_S
        self._settle_outcome = outcome
        # The phase's own time ends at the decision, not after the settle.
        self.phase_ended_at = now
        self._event(
            "locomotion_settling", test=self.phase_id, outcome=outcome
        )

    def finalize(self, now: float) -> None:
        """Record the walking result when the harness ends the run early."""
        if not self.finished:
            self._finish(now, "run_ended")

    # ---- outcome ----

    def _goal_noun(self) -> str:
        """Human label of what this run aims at (for evidence reasons)."""
        if self.objective_type == "kick":
            return "ball"
        if self.target_body is not None:
            return self.target_body
        return "goal" if self.goal_xy != self.flag_xy else "flag"

    def _goal_phrase(self) -> str:
        """The goal phrase used in reasons: flag, ball, prop name, or point."""
        if self.objective_type == "kick":
            return "ball"
        if self.target_body is not None:
            label = PROP_LABELS.get(self.target_body)
            return f"{self.target_body!r}" + (f" ({label})" if label else "")
        if self.goal_xy != self.flag_xy:
            return f"goal ({self.goal_xy[0]:.2f}, {self.goal_xy[1]:.2f})"
        return "flag"

    def _finish(self, now: float, outcome: str) -> None:
        self.finished = True
        if self.phase_ended_at is None:
            self.phase_ended_at = now
        passed, reason = self._verdict(now, outcome)
        self._record_result(now, outcome, passed, reason)
        self._event(
            "locomotion_finished",
            test=self.phase_id,
            passed=passed,
            reason=reason,
        )

    def _verdict(self, now: float, outcome: str) -> tuple[bool, str]:
        """Pass/fail + reason for the current phase from its evidence."""
        scenario = self.scenario
        fallen = bool(self.engine.fallen)
        final_flag_m = self._flag_distance()
        ball_displacement = self.ball_displacement_m
        goal_phrase = self._goal_phrase()
        if fallen:
            passed = False
            reason = (
                f"Robot fell (trunk reached {self.min_trunk_z:.3f} m);"
                f" closest {goal_phrase} approach {self.min_flag_m:.2f} m."
            )
        elif self.contact_ticks:
            passed = False
            reason = (
                f"Disqualifying contact ({self.contact_ticks} tick(s));"
                f" closest {goal_phrase} approach {self.min_flag_m:.2f} m."
            )
        elif self.objective_type == "kick":
            passed = self.ball_touched and ball_displacement >= (
                KICK_MIN_DISPLACEMENT
            )
            if self.ball_touched:
                verdict = "PASS" if passed else "FAIL"
                reason = (
                    f"Kicked the ball {ball_displacement:.2f} m"
                    f" (needs {KICK_MIN_DISPLACEMENT:.2f} m): {verdict} —"
                    f" walked {self.distance_m:.2f} m, no fall and no"
                    " disqualifying contact."
                )
            else:
                reason = (
                    "Ball never touched: walked"
                    f" {self.distance_m:.2f} m, closest approach"
                    f" {self.min_flag_m:.2f} m (radius"
                    f" {float(scenario.locomotion_goal_radius):g} m) in this"
                    f" phase's {self.phase_deadline - self.phase_started_at:g}s"
                    " window."
                )
        elif self.objective_type == "touch" and self.touched:
            passed = True
            reason = (
                f"Touched the arena wall and stopped after walking"
                f" {self.distance_m:.2f} m with no fall and no other"
                " disqualifying contact."
            )
        elif self.objective_type == "touch":
            passed = False
            reason = (
                f"Never touched the wall: walked {self.distance_m:.2f} m,"
                f" closest goal approach {self.min_flag_m:.2f} m in this"
                f" phase's {self.phase_deadline - self.phase_started_at:g}s"
                " window."
            )
        elif self.objective_type == "wander":
            passed = True
            reason = (
                f"Wandered {self.distance_m:.2f} m with no fall and no"
                " disqualifying contact."
            )
        elif not self.reached:
            passed = False
            missed_noun = goal_phrase
            reason = (
                f"{missed_noun[0].upper() + missed_noun[1:]} not reached:"
                f" closest approach {self.min_flag_m:.2f} m, radius"
                f" {float(scenario.locomotion_goal_radius):g} m after"
                f" walking {self.distance_m:.2f} m in this phase's"
                f" {self.phase_deadline - self.phase_started_at:g}s window."
            )
        else:
            passed = True
            reached_noun = goal_phrase
            reason = (
                f"Reached the {reached_noun} (closest"
                f" {self.min_flag_m:.2f} m, stopped {final_flag_m:.2f} m)"
                f" after walking {self.distance_m:.2f} m with no fall"
                " and no disqualifying contact."
            )
        if outcome == "run_ended" and not passed:
            reason = f"Run ended early ({now:.1f}s): {reason}"
        return passed, reason

    def _record_result(
        self, now: float, outcome: str, passed: bool, reason: str
    ) -> None:
        """Append the current phase's result (one entry per phase)."""
        scenario = self.scenario
        self.results.append(
            {
                "id": self.phase_id,
                "kind": "locomotion",
                "passed": passed,
                "reason": reason,
                "outcome": outcome,
                "phase": self.phase_index + 1,
                "phases": len(self.phases),
                "distance_m": self.distance_m,
                "min_flag_distance_m": self.min_flag_m,
                "final_flag_distance_m": self._flag_distance(),
                "min_trunk_z": self.min_trunk_z,
                "fallen": bool(self.engine.fallen),
                "contacts": self.contact_ticks,
                "final_error": max(
                    0.0,
                    self.min_flag_m
                    - float(scenario.locomotion_goal_radius),
                ),
                "tolerance": float(scenario.locomotion_goal_radius),
                "estop_latency": self.estop_latency,
                "duration_s": now,
                "phase_duration_s": max(
                    0.0,
                    (self.phase_ended_at if self.phase_ended_at is not None else now)
                    - self.phase_started_at,
                ),
                "phase_budget_s": (
                    self.phase_deadline - self.phase_started_at
                ),
                "objective": self.objective_type,
                "target_body": self.target_body,
                "ball_touched": self.ball_touched,
                "ball_displacement_m": self.ball_displacement_m,
            }
        )

    def _touched_wall(self) -> bool:
        """True when this tick touched a WALL geom (not the flag).

        Touch runs exclude both flag and walls from disqualifying
        contacts, but only a wall touch finishes the run passing — a
        flag brush on the way there must be ignored.
        """
        touched = getattr(self.engine, "goal_touch_geoms", None) or set()
        return bool(set(touched) & set(wall_geom_names()))

    def _ball_touched_geom(self) -> bool:
        """True when this tick touched a ball collision geom."""
        touched = getattr(self.engine, "goal_touch_geoms", None) or set()
        return bool(set(touched) & set(ball_geom_names()))

    def _flag_distance(self) -> float:
        # Distance to the run's goal (the flag unless the objective names
        # another target). The min/final attribute and result keys keep
        # their flag-era names so stored runs and the UI keep working.
        x, y = self.engine.base_pose()[0:2]
        return math.hypot(x - self.goal_xy[0], y - self.goal_xy[1])

    def _event(self, kind: str, **payload: Any) -> None:
        self.events.append({"type": kind, **payload})
