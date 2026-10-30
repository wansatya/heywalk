"""Run lifecycle orchestration (AGENTS.md sections 5 and 10).

State machine: queued -> preparing -> running -> evaluating ->
succeeded | failed | timeout | cancelled | error.

Every transition is validated and recorded as a timestamped run event.
Generated code is never executed here; the isolated runner backend owns
execution (see app/runner/).
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

from ..db import Store, utcnow_iso
from ..models.schemas import TERMINAL_RUN_STATUSES
from ..runner import NotConfiguredRunner, RunnerBackend, RunnerError
from ..simulator import DEFAULT_WORLD
from ..simulator.robot_description import (
    RobotPackageError,
    build_robot_package,
)
from ..simulator.scenario import (
    BACKEND_DIFF_DRIVE,
    BACKEND_MUJOCO,
    MUJOCO_DEFAULTS,
    MujocoScenario,
    ScenarioError,
    default_tests_for_joints,
    infer_backend,
)
from . import evaluation as evaluator
from .experiments import MAIN_CONTROLLER_ENTRY

ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "queued": ("preparing", "cancelled"),
    "preparing": ("running", "cancelled", "error"),
    "running": ("evaluating", "cancelled", "timeout", "error"),
    "evaluating": ("succeeded", "failed", "timeout", "cancelled", "error"),
    "succeeded": (),
    "failed": (),
    "timeout": (),
    "cancelled": (),
    "error": (),
}

TERMINAL_STATUSES = frozenset(TERMINAL_RUN_STATUSES)

# Evaluator verdicts map 1:1 onto terminal run states so a timeout proven
# by telemetry is reported as "timeout", not a generic failure.
STATUS_BY_REPORT: dict[str, str] = {
    "success": "succeeded",
    "failed": "failed",
    "timeout": "timeout",
    "error": "error",
    "cancelled": "cancelled",
}

MAX_WORLD_CONFIG_BYTES = 64 * 1024

# Each joint test can take up to its own timeout (5 s by default). The run's
# sim-time budget is scaled so a large suite is not cut short and reported as
# a controller failure.
TIME_BUDGET_PER_TEST_S = 6.0

# Slack over the summed per-phase windows: the final settle (GOAL_SETTLE_S)
# plus prop drift toward a moving goal. The scenario ``time_limit`` stays a
# safety net; the graded stop is each locomotion phase's own window.
LOCOMOTION_FINAL_MARGIN_S = 8.0


def _locomotion_phase_count(world: dict[str, Any]) -> int:
    """How many ordered phases this locomotion world grades."""
    phases = world.get("objectives")
    if isinstance(phases, list):
        count = sum(1 for phase in phases if isinstance(phase, dict))
        if count:
            return count
    return 1


def _locomotion_time_limit(world: dict[str, Any]) -> float:
    """Hard scenario stop that covers EVERY phase's window.

    ``LocomotionRunner`` gives each phase its own ``locomotion_duration_s``
    window (see ``phase_deadline``). The plain 30 s default would cut a
    multi-phase run dead and fail its last phase for lack of time instead of
    for behavior — the reason a "kick, then go to the crate behind you"
    sequence failed while the controller was still walking.
    """
    window = float(
        world.get("locomotion_duration_s")
        or MUJOCO_DEFAULTS["locomotion_duration_s"]
    )
    phases = _locomotion_phase_count(world)
    return max(
        float(MUJOCO_DEFAULTS["time_limit"]),
        phases * window + LOCOMOTION_FINAL_MARGIN_S,
    )


def _microduck_defaults(store: Store, project_id: str) -> dict[str, Any]:
    """Official-model physics defaults when the project's robot is MicroDuck.

    Detected from the stored robot description (composition marker), so no
    extra project state is needed and explicit run settings always win.
    Returns {} for generated robots and for projects without a model.
    """
    from ..simulator.microduck import is_composed_scene
    from ..simulator.robot_catalog import MICRODUCK_SCENARIO_DEFAULTS

    try:
        for entry in store.list_files(project_id):
            if not entry["path"].lower().endswith(".xml"):
                continue
            stored = store.get_file(project_id, entry["path"])
            content = stored["content"] if stored else ""
            if "<mujoco" in content[:4096].lower() and is_composed_scene(
                content
            ):
                return dict(MICRODUCK_SCENARIO_DEFAULTS)
    except (OSError, KeyError):
        pass
    return {}


def _microduck_goal_geoms(
    store: Store, project_id: str, world: dict[str, Any]
) -> list[str]:
    """Goal-geom names for a MicroDuck locomotion run (else []).

    The flag is always the aim-or-marker, never an obstacle. Any touch
    or wander phase adds the arena walls and any kick phase adds the
    ball: contacting them is an objective, so they must not count as
    disqualifying collisions.
    """
    from ..simulator.microduck import is_composed_scene
    from ..simulator.microduck.model import (
        ball_geom_names,
        flag_geom_names,
        wall_geom_names,
    )

    raw = world.get("objectives")
    if raw is None:
        raw = world.get("objective")
    phases = raw if isinstance(raw, list) else [raw]
    kinds = {
        phase.get("type") for phase in phases if isinstance(phase, dict)
    }
    try:
        for entry in store.list_files(project_id):
            if not entry["path"].lower().endswith(".xml"):
                continue
            stored = store.get_file(project_id, entry["path"])
            content = stored["content"] if stored else ""
            if "<mujoco" in content[:4096].lower() and is_composed_scene(
                content
            ):
                geoms = list(flag_geom_names())
                if "touch" in kinds or "wander" in kinds:
                    geoms.extend(wall_geom_names())
                if "kick" in kinds:
                    geoms.extend(ball_geom_names())
                return geoms
    except (OSError, KeyError):
        pass
    return []


def _default_suite(store: Store, project_id: str) -> list[dict[str, Any]]:
    """Derive the automated joint-test suite from the project's robot model.

    Raises when the project has no usable robot description; the caller turns
    that into an actionable error at run creation instead of letting the
    runner fail later with a terse log line.
    """
    merged = {}
    for entry in store.list_files(project_id):
        stored = store.get_file(project_id, entry["path"])
        merged[entry["path"]] = stored["content"] if stored else ""
    package = build_robot_package(merged)
    tests = default_tests_for_joints(
        [(j.name, j.lower, j.upper) for j in package.info.joints]
    )
    return [test.to_dict() for test in tests]


class RunPreconditionError(ValueError):
    """The project cannot run this scenario yet (for example, no robot model).

    Kept separate from an invalid scenario so the API can answer with a clear,
    actionable message instead of a generic validation error.
    """


def _resolve_controller_entry(store: Store, project_id: str) -> tuple[str | None, str]:
    """Mirror staging: controller.py wins, else the first .py file.

    Returns (entry path or None, entry content or ""). Runs always execute
    the saved controller, so an empty entry is rejected at creation with an
    actionable error instead of failing in the runner with a terse log.
    """
    try:
        paths = sorted(entry["path"] for entry in store.list_files(project_id))
    except (OSError, KeyError):
        return None, ""
    if MAIN_CONTROLLER_ENTRY in paths:
        entry = MAIN_CONTROLLER_ENTRY
    else:
        entry = next((p for p in paths if p.endswith(".py")), "")
    if not entry:
        return None, ""
    try:
        stored = store.get_file(project_id, entry)
    except (OSError, KeyError):
        return entry, ""
    return entry, stored["content"] if stored else ""


class WorldConfigTooLargeError(ValueError):
    """The serialized scenario exceeds the size limit."""


class InvalidTransitionError(ValueError):
    """Raised when a run-state transition is not allowed."""


class RunNotFoundError(KeyError):
    """Raised when a run id does not exist."""


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def check_transition(from_status: str, to_status: str) -> None:
    allowed = ALLOWED_TRANSITIONS.get(from_status)
    if allowed is None:
        raise InvalidTransitionError(f"Unknown run status: {from_status!r}.")
    if to_status not in allowed:
        raise InvalidTransitionError(
            f"Illegal run transition: {from_status!r} -> {to_status!r}."
        )


class RunEventBus:
    """In-memory fan-out of run events to live WebSocket subscribers."""

    def __init__(self, max_queue: int = 2000) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._max_queue = max_queue

    def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers[run_id].add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        self._subscribers[run_id].discard(queue)

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(run_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                continue


class ExecutionManager:
    def __init__(
        self,
        store: Store,
        bus: RunEventBus | None = None,
        runner: RunnerBackend | None = None,
    ) -> None:
        self.store = store
        self.bus = bus or RunEventBus()
        self.runner: RunnerBackend = runner or NotConfiguredRunner()
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._estop_events: dict[str, asyncio.Event] = {}

    # ---- lifecycle API ----

    async def start_run(
        self,
        project_id: str,
        *,
        world_config: dict[str, Any] | None,
        seed: int,
        parent_run_id: str | None = None,
    ) -> dict[str, Any]:
        if self.store.get_project(project_id) is None:
            raise KeyError(f"Unknown project: {project_id!r}.")
        if parent_run_id is not None:
            parent = self.store.get_run(parent_run_id)
            if parent is None or parent["project_id"] != project_id:
                raise KeyError(f"Unknown parent run: {parent_run_id!r}.")
        # Partial overrides merge over the backend's defaults so callers can
        # send e.g. {"time_limit": None} without repeating the whole scenario;
        # the stored world stays complete for reruns, the viewport, and (for
        # MuJoCo) the deterministic test suite. The legacy 2D arena defaults
        # are only merged into differential-drive runs.
        try:
            backend = infer_backend(
                world_config if isinstance(world_config, dict) else None
            )
        except ScenarioError as exc:
            raise ValueError(str(exc)) from None
        if backend == BACKEND_MUJOCO:
            entry, content = _resolve_controller_entry(self.store, project_id)
            if entry is None or not content.strip():
                raise RunPreconditionError(
                    "controller.py is empty, so there is nothing to run. Import"
                    " the walk-to-flag sample script or Generate a controller"
                    " from the task, then press Start again."
                )
        world = dict(DEFAULT_WORLD) if backend == BACKEND_DIFF_DRIVE else {}
        if backend == BACKEND_MUJOCO:
            world.update(MUJOCO_DEFAULTS)
            # Official-model runs use the official physics (dt, keyframe,
            # integrator, contacts); explicit caller keys always win.
            for key, value in _microduck_defaults(
                self.store, project_id
            ).items():
                if key not in (world_config or {}):
                    world[key] = value
        if world_config is not None:
            if not isinstance(world_config, dict):
                raise ValueError("world_config must be a JSON object.")
            world.update(world_config)
        world["backend"] = backend
        if (
            backend == BACKEND_MUJOCO
            and world.get("objectives") is None
            and world.get("objective") is None
        ):
            # The controller's own phases travel with the project: Start
            # without explicit objectives grades what the code was
            # generated for instead of silently assuming the flag.
            # Anything explicit (list or legacy single) wins.
            stored = self.store.get_project_objectives(project_id)
            if stored is not None:
                world["objectives"] = stored
                world["objective"] = stored[0] if stored else None
        if backend == BACKEND_MUJOCO and world.get("locomotion"):
            # Locomotion runs walk free-standing toward the run's goal: the
            # test stand would pin the trunk, so it is always off here
            # (the stored world records the effective setting for reruns).
            world["fix_base"] = False
            if "goal_geoms" not in (world_config or {}):
                world["goal_geoms"] = _microduck_goal_geoms(
                    self.store, project_id, world
                )
            if "time_limit" not in (world_config or {}):
                world["time_limit"] = _locomotion_time_limit(world)
        if backend == BACKEND_MUJOCO and not world.get("locomotion"):
            # Resolve the automated suite up front so the stored scenario is
            # complete: the UI can show the plan before the run starts and a
            # rerun repeats exactly the same tests (AGENTS.md sections 7-9).
            if not world.get("tests"):
                try:
                    world["tests"] = _default_suite(self.store, project_id)
                except (RobotPackageError, ScenarioError, OSError, KeyError) as exc:
                    raise RunPreconditionError(
                        "This project has no simulatable robot description, so"
                        f" the joint tests cannot be planned: {exc} Generate a"
                        " controller, install a starter robot (GET /api/robots),"
                        " or run the 2D differential-drive backend instead."
                    ) from None
            if "time_limit" not in (world_config or {}):
                # A suite with more joints needs more simulated time; the
                # default 30 s would otherwise time out before the last tests
                # run, which would look like a controller failure.
                world["time_limit"] = max(
                    float(MUJOCO_DEFAULTS["time_limit"]),
                    TIME_BUDGET_PER_TEST_S * len(world["tests"]),
                )
            try:
                MujocoScenario.from_dict(world)
            except ScenarioError as exc:
                raise ValueError(f"Invalid MuJoCo scenario: {exc}") from None
        raw = json.dumps(world)
        if len(raw.encode("utf-8")) > MAX_WORLD_CONFIG_BYTES:
            raise WorldConfigTooLargeError("world_config exceeds the 64 KiB limit.")
        run = self.store.create_run(
            project_id,
            parent_run_id=parent_run_id,
            controller_version=self.store.max_file_version(project_id),
            backend=backend,
            status="queued",
            world_config=raw,
            seed=seed,
        )
        await self._record_status(run["id"], "queued")
        self._cancel_events[run["id"]] = asyncio.Event()
        self._estop_events[run["id"]] = asyncio.Event()
        task = asyncio.get_running_loop().create_task(self._drive(run["id"]))
        self._tasks[run["id"]] = task
        return self._attach(run)

    async def rerun_run(self, run_id: str) -> dict[str, Any]:
        """Start a fresh run of the same scenario (AGENTS.md section 9).

        The new run reuses the original world config and seed with
        ``parent_run_id`` set for before/after comparison. Simulation state
        is never reused: the runner always starts from a clean boundary.
        """
        run = self.store.get_run(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        try:
            world = (
                json.loads(run["world_config"]) if run["world_config"] else None
            )
        except (ValueError, TypeError):
            world = None
        return await self.start_run(
            run["project_id"],
            world_config=world,
            seed=run["seed"],
            parent_run_id=run_id,
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        return self._attach(run)

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        if is_terminal(run["status"]):
            return self._attach(run)
        event = self._cancel_events.get(run_id)
        if event is not None:
            event.set()
        # If the driver has not picked the run up yet, cancel it directly.
        current = self.store.get_run(run_id)
        assert current is not None
        if current["status"] == "queued":
            self.transition(run_id, "cancelled", failure_summary="Run cancelled.")
        return self.get_run(run_id)

    async def latch_estop(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        self.store.update_run(run_id, estop_active=1)
        event = self._estop_events.get(run_id)
        if event is not None:
            event.set()
        await self._write_log(run_id, "EMERGENCY STOP latched by user.")
        return self.get_run(run_id)

    def is_paused(self, run_id: str) -> bool:
        paused = getattr(self.runner, "is_paused", None)
        try:
            return bool(paused(run_id)) if callable(paused) else False
        except Exception:
            return False

    async def pause_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        if is_terminal(run["status"]):
            raise ValueError(f"Run {run_id} is already {run['status']}.")
        try:
            await self.runner.pause(run_id)
        except RunnerError as exc:
            raise ValueError(str(exc)) from None
        await self._write_log(run_id, "Run paused by user; physics suspended.")
        return self.get_run(run_id)

    async def resume_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        if is_terminal(run["status"]):
            raise ValueError(f"Run {run_id} is already {run['status']}.")
        try:
            await self.runner.resume(run_id)
        except RunnerError as exc:
            raise ValueError(str(exc)) from None
        await self._write_log(run_id, "Run resumed by user.")
        return self.get_run(run_id)

    def transition(
        self,
        run_id: str,
        to_status: str,
        *,
        failure_summary: str | None = None,
        exit_code: int | None = None,
    ) -> dict[str, Any]:
        """Validate and apply a state transition (sync; used by tests too)."""
        run = self.store.get_run(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        check_transition(run["status"], to_status)
        fields: dict[str, Any] = {"status": to_status}
        if failure_summary is not None:
            fields["failure_summary"] = failure_summary
        if exit_code is not None:
            fields["exit_code"] = exit_code
        if is_terminal(to_status):
            fields["finished_at"] = utcnow_iso()
        updated = self.store.update_run(run_id, **fields)
        assert updated is not None
        self.store.add_event(run_id, "status", {"status": to_status})
        return self._attach(updated)

    async def shutdown(self) -> None:
        for event in self._cancel_events.values():
            event.set()
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    # ---- driver ----

    async def _drive(self, run_id: str) -> None:
        cancel_event = self._cancel_events[run_id]
        estop_event = self._estop_events[run_id]
        try:
            await asyncio.sleep(0.1)
            if not await self._step(run_id, "preparing", cancel_event, estop_event):
                return
            await self._write_log(
                run_id, f"Preparing isolated runner (backend: {self.runner.name})..."
            )
            await asyncio.sleep(0.1)
            if not await self._step(run_id, "running", cancel_event, estop_event):
                return
            await self._write_log(run_id, "Run started.")

            run = self.store.get_run(run_id)
            assert run is not None
            from ..runner import RunnerContext

            ctx = RunnerContext(
                run_id=run_id,
                project_id=run["project_id"],
                controller_files=[
                    {"path": f["path"], "content": ""}
                    for f in self.store.list_files(run["project_id"])
                ],
                world_config=json.loads(run["world_config"] or "{}"),
                seed=run["seed"],
                emit_log=lambda line: self._write_log(run_id, line),
                emit_telemetry=lambda sample: self._write_telemetry(
                    run_id, sample
                ),
                cancel_event=cancel_event,
                estop_event=estop_event,
            )
            # Controller source is loaded for the isolated runner only; it is
            # never imported or exec()'d in this process.
            for entry in ctx.controller_files:
                stored = self.store.get_file(run["project_id"], entry["path"])
                entry["content"] = stored["content"] if stored else ""

            try:
                result = await self.runner.execute(ctx)
            except RunnerError as exc:
                await self._write_log(run_id, f"Runner error: {exc}")
                await self._step(run_id, "evaluating", cancel_event, estop_event)
                report = evaluator.failure_report([str(exc)])
                await self._finish(
                    run_id, "error", report, metrics=None, exit_code=1,
                    failure_summary=str(exc),
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # defensive: never strand a run
                message = f"Unexpected execution failure: {exc}"
                await self._write_log(run_id, message)
                await self._step(run_id, "evaluating", cancel_event, estop_event)
                await self._finish(
                    run_id, "error", evaluator.failure_report([message]),
                    metrics=None, exit_code=1, failure_summary=message,
                )
                return

            if cancel_event.is_set() or result.cancelled:
                await self._finish_cancelled(run_id, "Run cancelled.")
                return
            if estop_event.is_set():
                await self._step(run_id, "evaluating", cancel_event, estop_event)
                report = evaluator.failure_report(
                    ["Emergency stop was latched during the run."],
                    run_status="cancelled",
                )
                await self._finish(
                    run_id, "cancelled", report, metrics=None, exit_code=None,
                    failure_summary="Emergency stop activated.",
                )
                return

            # Automated-test evidence is recorded for every MuJoCo outcome,
            # including timeouts and cancellations, so a run can never end
            # without its measured evidence (AGENTS.md §8).
            if result.tests:
                self.store.save_test_results(run_id, result.tests)

            await self._step(run_id, "evaluating", cancel_event, estop_event)
            samples = self.store.list_telemetry(run_id)
            world_config = json.loads(run["world_config"] or "{}") or {}
            backend = run.get("backend") or world_config.get("backend") or BACKEND_DIFF_DRIVE
            if backend == BACKEND_MUJOCO:
                report, metrics = self._evaluate_mujoco(
                    world_config, result, samples
                )
                status = STATUS_BY_REPORT.get(
                    report.get("run_status", ""), "failed"
                )
                await self._finish(
                    run_id, status, report, metrics=metrics,
                    exit_code=result.exit_code,
                    failure_summary=result.failure_summary,
                )
                return
            goal = world_config.get("goal")
            try:
                time_limit = float(world_config.get("time_limit"))
            except (TypeError, ValueError):
                time_limit = None
            report, metrics = evaluator.evaluate_from_telemetry(
                samples,
                goal=goal,
                time_limit=time_limit,
                exit_code=result.exit_code,
                failure_summary=result.failure_summary,
            )
            if result.timed_out:
                # Wall-clock timeout: keep the partial-telemetry metrics but
                # report the proven timeout verdict, not a generic failure.
                report = dict(report)
                report["success"] = False
                report["run_status"] = "timeout"
                reasons = [result.failure_summary or "Run timed out."]
                reasons.extend(
                    r for r in report.get("reasons", []) if r not in reasons
                )
                report["reasons"] = reasons
                await self._finish(
                    run_id, "timeout", report, metrics=metrics,
                    exit_code=result.exit_code,
                    failure_summary=result.failure_summary,
                )
                return
            status = STATUS_BY_REPORT.get(report.get("run_status", ""), "failed")
            await self._finish(
                run_id, status, report, metrics=metrics,
                exit_code=result.exit_code, failure_summary=result.failure_summary,
            )
        except asyncio.CancelledError:
            try:
                await self._finish_cancelled(run_id, "Run cancelled.")
            except (RunNotFoundError, InvalidTransitionError):
                pass
            raise
        finally:
            self._tasks.pop(run_id, None)

    async def _step(
        self,
        run_id: str,
        to_status: str,
        cancel_event: asyncio.Event,
        estop_event: asyncio.Event,
    ) -> bool:
        """Advance one state; return False when the run was diverted."""
        if cancel_event.is_set():
            await self._finish_cancelled(run_id, "Run cancelled.")
            return False
        if estop_event.is_set() and to_status in ("preparing", "running"):
            run = self.store.get_run(run_id)
            if run is not None and not is_terminal(run["status"]):
                self.transition(run_id, "cancelled",
                                failure_summary="Emergency stop activated.")
                await self._publish_status(run_id, "cancelled")
            return False
        run = self.store.get_run(run_id)
        if run is None or is_terminal(run["status"]):
            return False
        self.transition(run_id, to_status)
        await self._publish_status(run_id, to_status)
        return True

    def _evaluate_mujoco(
        self,
        world_config: dict[str, Any],
        result: Any,
        samples: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Objective MuJoCo evaluation from joint tests + telemetry."""
        from ..simulator.mujoco_tests import evaluate_mujoco

        scenario = MujocoScenario.from_dict(world_config)
        tests = result.tests or []
        suite_size = result.suite_size
        sim_timed_out = bool(result.sim_timed_out)
        if (
            scenario.time_limit is not None
            and suite_size is not None
            and len(tests) < suite_size
            and result.exit_code in (None, 0)
        ):
            sim_timed_out = True
        report, metrics = evaluate_mujoco(
            tests,
            samples,
            scenario=scenario,
            exit_code=result.exit_code,
            failure_summary=result.failure_summary,
            timed_out=sim_timed_out or result.timed_out,
        )
        if result.timed_out:
            report = dict(report)
            report["success"] = False
            report["run_status"] = "timeout"
            reasons = [result.failure_summary or "Run timed out."]
            reasons.extend(
                reason
                for reason in report.get("reasons", [])
                if reason not in reasons
            )
            report["reasons"] = reasons
        return report, metrics

    async def _finish(
        self,
        run_id: str,
        status: str,
        report: dict[str, Any],
        *,
        metrics: dict[str, Any] | None,
        exit_code: int | None,
        failure_summary: str | None,
    ) -> None:
        if metrics is not None:
            self.store.save_metrics(run_id, metrics)
        failure = failure_summary
        if status in ("failed", "error", "timeout") and not failure:
            reasons = report.get("reasons", [])
            failure = reasons[0] if reasons else f"Run {status}."
        self.transition(run_id, status, failure_summary=failure, exit_code=exit_code)
        event = self.store.add_event(
            run_id, "evaluation", {"evaluation": report, "metrics": metrics}
        )
        await self.bus.publish(run_id, self._stream_payload(event))
        await self._publish_status(run_id, status)
        if status == "succeeded":
            await self._write_log(run_id, "Run succeeded.")
        else:
            await self._write_log(run_id, f"Run {status}: {failure}")

    async def _finish_cancelled(self, run_id: str, message: str) -> None:
        run = self.store.get_run(run_id)
        if run is None or is_terminal(run["status"]):
            return
        report = evaluator.failure_report([message], run_status="cancelled")
        event = self.store.add_event(
            run_id, "evaluation", {"evaluation": report, "metrics": None}
        )
        await self.bus.publish(run_id, self._stream_payload(event))
        self.transition(run_id, "cancelled", failure_summary=message)
        await self._publish_status(run_id, "cancelled")
        await self._write_log(run_id, message)

    # ---- event helpers ----

    async def _record_status(self, run_id: str, status: str) -> None:
        event = self.store.add_event(run_id, "status", {"status": status})
        await self.bus.publish(run_id, self._stream_payload(event))

    async def _publish_status(self, run_id: str, status: str) -> None:
        await self.bus.publish(run_id, {"type": "status", "status": status})

    async def _write_log(self, run_id: str, line: str) -> None:
        self.store.add_log(run_id, line)
        event = self.store.add_event(run_id, "log", {"line": line})
        await self.bus.publish(run_id, self._stream_payload(event))

    async def _write_telemetry(self, run_id: str, sample: dict[str, Any]) -> None:
        # One transaction, off the event loop: the live WebSocket stream must
        # not wait behind SQLite commits (they used to block for 3.6 ms
        # median / 37 ms max per sample).
        event = await asyncio.to_thread(
            self.store.record_telemetry, run_id, sample
        )
        await self.bus.publish(run_id, self._stream_payload(event))

    @staticmethod
    def _stream_payload(event: dict[str, Any]) -> dict[str, Any]:
        payload = dict(event.get("payload") or {})
        payload["type"] = event["event_type"]
        return payload

    def replay_events(self, run_id: str) -> list[dict[str, Any]]:
        return [self._stream_payload(e) for e in self.store.list_events(run_id)]

    def _attach(self, run: dict[str, Any]) -> dict[str, Any]:
        data = dict(run)
        try:
            data["world_config"] = (
                json.loads(data["world_config"]) if data["world_config"] else None
            )
        except (ValueError, TypeError):
            data["world_config"] = None
        data["metrics"] = self.store.get_metrics(run["id"])
        stored_evaluation = self.store.latest_evaluation(run["id"])
        data["evaluation"] = (
            stored_evaluation.get("evaluation") if stored_evaluation else None
        )
        # MuJoCo runs carry objective automated-test evidence (AGENTS.md §8);
        # 2D runs simply have an empty list.
        data["tests"] = self.store.list_test_results(run["id"])
        data["paused"] = self.is_paused(run["id"])
        return data
