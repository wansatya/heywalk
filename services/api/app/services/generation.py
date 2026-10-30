"""Controller generation and fix handlers (AGENTS.md sections 6 and 9).

Handlers call the AI service module interface (meta_model.MetaModelClient),
validate the structured response, and write approved files with versioning.
Model output is untrusted: paths are re-validated and unexpected files are
rejected before anything is written.
"""

from __future__ import annotations

import time
from typing import Any

from ..config import Settings
from ..db import Store
from ..models.schemas import (
    FixResult,
    GenerationResult,
    PlannedTestOut,
    RobotJointOut,
    RobotPackageOut,
    TraceStep,
)
from ..simulator.robot_catalog import StarterRobot, get_starter_robot
from ..simulator.robot_description import (
    RobotPackage,
    RobotPackageError,
    build_robot_package,
)
from ..simulator.scenario import ScenarioError, default_tests_for_joints
from . import experiments
from . import files as file_service
from . import meta_model
from .meta_model import (
    FixEvidence,
    GenerationContext,
    MetaModelError,
    MetaModelNotConfiguredError,
    MetaModelResponseError,
    PROMPT_VERSION,
)


class ProjectNotFoundError(KeyError):
    pass


class RunNotReadyError(ValueError):
    """Raised when a fix is requested for a run without failure evidence."""


class Tracer:
    """Record timed stages of a model operation for under-the-hood display."""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self._open: dict[str, float] = {}

    def start(self, stage: str) -> None:
        self._open[stage] = time.monotonic()

    def finish(self, stage: str, detail: str = "") -> None:
        started = self._open.pop(stage, None)
        ms = int((time.monotonic() - started) * 1000) if started else 0
        self.steps.append(
            {"stage": stage, "status": "ok", "ms": ms, "detail": detail}
        )

    def fail(self, stage: str, detail: str = "") -> None:
        started = self._open.pop(stage, None)
        ms = int((time.monotonic() - started) * 1000) if started else 0
        self.steps.append(
            {"stage": stage, "status": "error", "ms": ms, "detail": detail}
        )


def _attempts_detail(model_client: Any) -> str:
    attempts = getattr(model_client, "last_attempts", None) or []
    if not attempts:
        return "1 attempt"
    parts = [
        f"#{a.get('attempt')}: {a.get('outcome')} ({a.get('latency_ms')}ms)"
        for a in attempts
    ]
    return "; ".join(parts)


def _attach(exc: BaseException, tracer: Tracer) -> None:
    try:
        exc.trace = list(tracer.steps)  # type: ignore[attr-defined]
        exc.hint = meta_model.hint_for_error(exc)  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass


def _project_files(store: Store, project_id: str) -> list[dict[str, str]]:
    result = []
    for entry in store.list_files(project_id):
        stored = store.get_file(project_id, entry["path"])
        result.append(
            {"path": entry["path"], "content": stored["content"] if stored else ""}
        )
    return result


def _write_model_files(
    store: Store,
    project_id: str,
    model_files: list[Any],
    purpose: str,
    *,
    require_robot: bool = True,
) -> tuple[list[tuple[str, str]], RobotPackage | None]:
    """Validate model files and complete the robot package before writing.

    Two phases, always: every path/size is validated first, then the robot
    package (MJCF + synthesized/looked-up URDF and ros2_control config) is
    validated, and only then is anything written. A rejected generation can
    therefore never leave a half-written project behind.
    """
    if not model_files:
        raise MetaModelResponseError(
            f"Model {purpose} output contained no files; refusing to write"
            " an empty update."
        )
    validated: list[tuple[str, str]] = []
    for model_file in model_files:
        path = file_service.validate_file_path(model_file.path)
        file_service.check_content_size(model_file.content)
        validated.append((path, model_file.content))

    package: RobotPackage | None = None
    if require_robot:
        merged = {
            entry["path"]: entry["content"]
            for entry in _project_files(store, project_id)
        }
        merged.update(dict(validated))
        try:
            package = build_robot_package(merged)
        except RobotPackageError as exc:
            raise MetaModelResponseError(
                f"Generated robot package is not simulatable: {exc}"
            ) from None
        # Synthesized companions (URDF / ros2_control) are platform-owned
        # artifacts: write them alongside the model-authored files. Files the
        # project already holds are left untouched.
        written_paths = dict(validated)
        for path, content in package.files.items():
            if path in merged or path in written_paths:
                continue
            validated.append((path, content))
            written_paths[path] = content

    experiments.reject_blocking_calls(validated)
    for path, content in validated:
        file_service.save_project_file(store, project_id, path, content)
    return validated, package


def _robot_summary(package: RobotPackage) -> RobotPackageOut:
    """Describe the validated robot and the automated tests it will run."""
    joints = [
        RobotJointOut(
            name=joint.name,
            type=joint.type,
            actuator=joint.actuator,
            lower=joint.lower,
            upper=joint.upper,
        )
        for joint in package.info.joints
    ]
    try:
        tests = default_tests_for_joints(
            [(joint.name, joint.lower, joint.upper) for joint in package.info.joints]
        )
    except ScenarioError:
        tests = ()
    return RobotPackageOut(
        model_path=package.model_path,
        joints=joints,
        tests=[
            PlannedTestOut(
                id=test.id,
                joint=test.joint,
                target=test.target,
                tolerance=test.tolerance,
            )
            for test in tests
        ],
        notes=list(package.notes),
    )


def install_starter_robot(
    store: Store, project_id: str, robot_id: str, *, include_controller: bool = True
) -> RobotPackageOut:
    """Write a bundled starter robot into a project.

    The platform owns these files, so there is no model request and no
    untrusted input: the package is validated before anything is written and
    the companions (URDF, ros2_control) are synthesized from the MJCF exactly
    as they are for generated robots. Existing files are replaced with a new
    version, so a previous controller is never silently lost.

    With ``include_controller=False`` the robot is installed with an EMPTY
    controller.py (fresh experiments start blank; the sample stays
    importable via the sample endpoint). An existing controller is never
    overwritten in that mode, so reinstalls cannot wipe user code.
    """
    if store.get_project(project_id) is None:
        raise ProjectNotFoundError(project_id)
    robot: StarterRobot = get_starter_robot(robot_id)
    files = robot.files()
    if not include_controller:
        files = {k: v for k, v in files.items() if k != "controller.py"}
    package = build_robot_package(files)
    merged = dict(files)
    for path, content in package.files.items():
        if path not in merged:
            merged[path] = content
    if not include_controller and store.get_file(project_id, "controller.py") is None:
        merged["controller.py"] = ""
    for path, content in merged.items():
        file_service.save_project_file(store, project_id, path, content)
    return _robot_summary(package)


def import_sample_controller(
    store: Store, project_id: str, robot_id: str
) -> dict[str, Any]:
    """Write the bundled sample controller and reset to default grading.

    The sample (e.g. walk to the flag) replaces controller.py and the
    project's objective is cleared, so the next Start grades the default
    flag behavior instead of a stale objective from an earlier generate.
    Its matching task text is written too: a fresh sequence otherwise has
    no task, which keeps Start disabled even though the sample is runnable.
    """
    if store.get_project(project_id) is None:
        raise ProjectNotFoundError(project_id)
    robot: StarterRobot = get_starter_robot(robot_id)
    saved = file_service.save_project_file(
        store, project_id, "controller.py", robot.controller
    )
    store.set_project_objectives(project_id, None)
    if robot.sample_task:
        store.set_project_task_description(project_id, robot.sample_task)
    return saved


async def handle_generate(
    store: Store,
    settings: Settings,
    project_id: str,
    task_description: str | None,
    client: meta_model.MetaModelClient | None = None,
) -> GenerationResult:
    project = store.get_project(project_id)
    if project is None:
        raise ProjectNotFoundError(project_id)
    task = task_description or project["task_description"]
    if task_description:
        store.set_project_task_description(project_id, task_description)

    model_client = client
    model_name = settings.muse_model
    if model_client is None:
        try:
            model_client = meta_model.build_client(settings)
        except MetaModelNotConfiguredError as exc:
            store.record_model_request(
                project_id, "generate", settings.muse_model, PROMPT_VERSION,
                "error", error_summary=str(exc),
            )
            raise
    model_name = getattr(model_client, "model_name", model_name)

    context = GenerationContext(
        task_description=task, existing_files=_project_files(store, project_id)
    )
    tracer = Tracer()
    tracer.start("prepare_prompt")
    prompt_chars = sum(
        len(message.get("content", ""))
        for message in meta_model.build_generate_messages(context)
    )
    tracer.finish(
        "prepare_prompt",
        f"{prompt_chars} chars, {len(context.existing_files)} existing file(s)",
    )
    started = time.monotonic()
    tracer.start("model_request")
    try:
        result = await model_client.generate_controller(context)
    except MetaModelError as exc:
        tracer.fail("model_request", _attempts_detail(model_client))
        _attach(exc, tracer)
        store.record_model_request(
            project_id, "generate", model_name, PROMPT_VERSION, "error",
            latency_ms=int((time.monotonic() - started) * 1000),
            error_summary=str(exc),
        )
        raise
    tracer.finish(
        "model_request",
        f"{model_name} ({_attempts_detail(model_client)})",
    )
    tracer.start("validate_files")
    try:
        written, package = _write_model_files(
            store, project_id, list(result.files), "generation"
        )
    except experiments.BlockingControllerError as exc:
        # One automatic repair round: the model gets its own rejected
        # output back plus the complaint, mirroring the JSON-shape repair
        # in the client. A second rejection fails like any other.
        tracer.start("repair_blocking_calls")
        complaint = str(exc)
        try:
            result = await model_client.generate_controller(
                GenerationContext(
                    task_description=(
                        f"{task}\n\n[System constraint — your previous"
                        f" controller was REJECTED and not saved: {complaint}"
                        " Regenerate the same behavior with no sleep call"
                        " anywhere in any Python file.]"
                    ),
                    existing_files=context.existing_files,
                )
            )
            written, package = _write_model_files(
                store, project_id, list(result.files), "generation-repair"
            )
        except (MetaModelError, ValueError) as repair_exc:
            tracer.fail("repair_blocking_calls", str(repair_exc))
            _attach(repair_exc, tracer)
            store.record_model_request(
                project_id, "generate", model_name, PROMPT_VERSION, "error",
                latency_ms=int((time.monotonic() - started) * 1000),
                error_summary=f"Rejected model files: {repair_exc}",
            )
            raise
        tracer.finish("repair_blocking_calls", "repaired without sleep()")
    except (MetaModelError, ValueError) as exc:
        tracer.fail("validate_files", str(exc))
        _attach(exc, tracer)
        store.record_model_request(
            project_id, "generate", model_name, PROMPT_VERSION, "error",
            latency_ms=int((time.monotonic() - started) * 1000),
            error_summary=f"Rejected model files: {exc}",
        )
        raise
    detail = f"{len(written)} file(s) written"
    if package is not None:
        detail += (
            f"; {len(package.info.joints)} actuated joint(s)"
            f" from {package.model_path}"
        )
        result.robot = _robot_summary(package)
    tracer.finish("validate_files", detail)
    # The objectives travel with the controller: runs without explicit
    # phases grade these, so Start always matches the generated code.
    store.set_project_objectives(
        project_id,
        [o.model_dump(mode="json") for o in result.objectives]
        if result.objectives
        else None,
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    store.record_model_request(
        project_id, "generate", model_name, PROMPT_VERSION, "done",
        latency_ms=latency_ms,
    )
    result.trace = [TraceStep(**step) for step in tracer.steps]
    result.model_name = model_name
    result.latency_ms = latency_ms
    return result


async def handle_fix(
    store: Store,
    settings: Settings,
    project_id: str,
    run_id: str,
    client: meta_model.MetaModelClient | None = None,
) -> FixResult:
    project = store.get_project(project_id)
    if project is None:
        raise ProjectNotFoundError(project_id)
    run = store.get_run(run_id)
    if run is None or run["project_id"] != project_id:
        raise KeyError(f"Unknown run: {run_id!r}.")
    if run["status"] not in ("failed", "timeout", "error"):
        raise RunNotReadyError(
            "A correction needs a failed, timed-out, or errored run with"
            f" failure evidence; run {run_id} has status {run['status']!r}."
        )
    # Cap automatic corrections per failed run (AGENTS.md section 9); the
    # caller surfaces the evidence and stops instead of retrying forever.
    experiments.check_fix_budget(
        store, project_id, run_id, max_attempts=settings.max_fix_attempts
    )

    model_client = client
    model_name = settings.muse_model
    if model_client is None:
        try:
            model_client = meta_model.build_client(settings)
        except MetaModelNotConfiguredError as exc:
            store.record_model_request(
                project_id, "fix", settings.muse_model, PROMPT_VERSION,
                "error", run_id=run_id, error_summary=str(exc),
            )
            raise
    model_name = getattr(model_client, "model_name", model_name)

    telemetry = store.list_telemetry(run_id)
    evidence = FixEvidence(
        task_description=project["task_description"],
        controller_files=_project_files(store, project_id),
        run_status=run["status"],
        failure_summary=run.get("failure_summary"),
        exit_code=run.get("exit_code"),
        recent_logs=store.list_logs(run_id)[-50:],
        last_state=telemetry[-1] if telemetry else None,
        evaluation=store.latest_evaluation(run_id),
        metrics=store.get_metrics(run_id),
        test_results=tuple(store.list_test_results(run_id)),
        backend=run.get("backend") or "differential_drive",
    )
    tracer = Tracer()
    tracer.start("collect_evidence")
    evidence_chars = sum(
        len(message.get("content", ""))
        for message in meta_model.build_fix_messages(evidence)
    )
    tracer.finish(
        "collect_evidence",
        f"{evidence_chars} chars from run {run_id[:8]}",
    )
    started = time.monotonic()
    tracer.start("model_request")
    try:
        result = await model_client.propose_fix(evidence)
    except MetaModelError as exc:
        tracer.fail("model_request", _attempts_detail(model_client))
        _attach(exc, tracer)
        store.record_model_request(
            project_id, "fix", model_name, PROMPT_VERSION, "error",
            run_id=run_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            error_summary=str(exc),
        )
        raise
    tracer.finish(
        "model_request",
        f"{model_name} ({_attempts_detail(model_client)})",
    )
    # Corrections are restricted to approved paths (existing files plus
    # the documented controller entry); previous contents are snapshotted
    # so a failed write rolls back instead of leaving a half-patched
    # project. The new controller is rerun via a fresh run, never in-process.
    tracer.start("validate_files")
    try:
        validated = experiments.validate_correction_files(
            store, project_id, list(result.updated_files)
        )
        # A correction that edits the robot description must still yield a
        # simulatable package before the project is touched.
        merged = {
            entry["path"]: entry["content"]
            for entry in _project_files(store, project_id)
        }
        merged.update(dict(validated))
        package = build_robot_package(merged)
    except (MetaModelError, ValueError, RobotPackageError) as exc:
        wrapped = (
            MetaModelResponseError(
                f"Corrected robot package is not simulatable: {exc}"
            )
            if isinstance(exc, RobotPackageError)
            else exc
        )
        tracer.fail("validate_files", str(wrapped))
        _attach(wrapped, tracer)
        store.record_model_request(
            project_id, "fix", model_name, PROMPT_VERSION, "error",
            run_id=run_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            error_summary=f"Rejected model files: {wrapped}",
        )
        raise wrapped from None
    snapshot = experiments.snapshot_files(
        store, project_id, [path for path, _ in validated]
    )
    try:
        for path, content in validated:
            file_service.save_project_file(store, project_id, path, content)
        # Platform-owned companions stay in sync with the (possibly edited)
        # robot description; a correction never needs to hand-write them.
        written_paths = dict(validated)
        for path, content in package.files.items():
            if path in merged or path in written_paths:
                continue
            file_service.save_project_file(store, project_id, path, content)
    except (MetaModelError, ValueError) as exc:
        tracer.fail("validate_files", str(exc))
        _attach(exc, tracer)
        experiments.restore_files(store, project_id, snapshot)
        store.record_model_request(
            project_id, "fix", model_name, PROMPT_VERSION, "error",
            run_id=run_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            error_summary=f"Rejected model files: {exc}",
        )
        raise
    tracer.finish(
        "validate_files",
        f"{len(validated)} file(s) written;"
        f" {len(package.info.joints)} actuated joint(s)",
    )
    result.robot = _robot_summary(package)
    latency_ms = int((time.monotonic() - started) * 1000)
    store.record_model_request(
        project_id, "fix", model_name, PROMPT_VERSION, "done",
        run_id=run_id,
        latency_ms=latency_ms,
    )
    result.trace = [TraceStep(**step) for step in tracer.steps]
    result.model_name = model_name
    result.latency_ms = latency_ms
    return result
