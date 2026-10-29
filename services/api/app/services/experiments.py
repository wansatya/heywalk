"""Before-and-after experiments + failure-to-fix guards (AGENTS.md §8-9).

- ``compare_with_baseline``: compare a run against its baseline (an
  explicitly requested run when valid, otherwise the parent run when
  set, otherwise the previous run in the project). No verdict is
  invented -- callers compare the returned metric values.
- ``check_fix_budget``: enforce the configurable cap on automatic
  correction attempts per failed run (default 2).
- ``validate_correction_files``: restrict correction writes to approved
  project paths (existing files plus the documented controller entry).
- ``snapshot_files`` / ``restore_files``: preserve previous controller
  versions so a failed correction write can be rolled back.
"""

from __future__ import annotations

import ast
from typing import Any

from ..db import Store
from ..simulator.scenario import (
    DEFAULT_MODEL_PATH,
    DEFAULT_ROS2_CONTROL_PATH,
    DEFAULT_URDF_PATH,
)
from . import files as file_service
from .meta_model import MetaModelResponseError

MAIN_CONTROLLER_ENTRY = "controller.py"

#: Files a first generation or a correction may (re)create even when the
#: project does not hold them yet: the controller entry and the robot
#: description package MuJoCo loads (AGENTS.md §6-§7).
APPROVED_ENTRIES: frozenset[str] = frozenset(
    {
        MAIN_CONTROLLER_ENTRY,
        DEFAULT_MODEL_PATH,
        DEFAULT_URDF_PATH,
        DEFAULT_ROS2_CONTROL_PATH,
    }
)

DEFAULT_MAX_FIX_ATTEMPTS = 2


class FixBudgetExhaustedError(ValueError):
    """Raised when a run already used its correction attempts."""


def summarize(store: Store, run: dict[str, Any]) -> dict[str, Any]:
    metrics = store.get_metrics(run["id"])
    tests = store.list_test_results(run["id"])
    return {
        "run_id": run["id"],
        "status": run["status"],
        "backend": run.get("backend") or "differential_drive",
        "controller_version": run["controller_version"],
        "failure_summary": run.get("failure_summary"),
        "metrics": metrics,
        "tests_total": len(tests),
        "tests_passed": sum(1 for test in tests if test.get("passed")),
    }


def compare_with_baseline(
    store: Store, run_id: str, baseline_run_id: str | None = None
) -> dict[str, Any] | None:
    run = store.get_run(run_id)
    if run is None:
        return None
    baseline = None
    if baseline_run_id:
        candidate = store.get_run(baseline_run_id)
        if (
            candidate is not None
            and candidate["project_id"] == run["project_id"]
            and candidate["id"] != run_id
        ):
            baseline = candidate
    if baseline is None and run.get("parent_run_id"):
        parent = store.get_run(run["parent_run_id"])
        if parent is not None and parent["project_id"] == run["project_id"]:
            baseline = parent
    if baseline is None:
        rowid = store.run_rowid(run_id)
        if rowid is not None:
            baseline = store.previous_run(run["project_id"], rowid)
    return {
        "baseline": summarize(store, baseline) if baseline else None,
        "current": summarize(store, run),
    }


def count_fix_attempts(store: Store, project_id: str, run_id: str) -> int:
    """Count correction attempts already made for one failed run."""
    return store.count_model_requests(project_id, "fix", run_id=run_id)


def check_fix_budget(
    store: Store,
    project_id: str,
    run_id: str,
    *,
    max_attempts: int = DEFAULT_MAX_FIX_ATTEMPTS,
) -> int:
    """Enforce the correction-attempt cap; return attempts used so far.

    Raises FixBudgetExhaustedError when the run already consumed
    ``max_attempts`` corrections, so the UI shows the evidence and stops.
    """
    used = count_fix_attempts(store, project_id, run_id)
    if used >= max(0, max_attempts):
        raise FixBudgetExhaustedError(
            f"Run {run_id} already used {used} of {max_attempts} correction"
            " attempt(s); inspect the failure evidence before retrying"
            " manually."
        )
    return used


class BlockingControllerError(MetaModelResponseError):
    """A model-authored controller would stall the simulator (mapped to 502)."""


def _is_sleep_call(func: ast.AST) -> bool:
    if isinstance(func, ast.Name):
        return func.id == "sleep"
    return isinstance(func, ast.Attribute) and func.attr == "sleep"


def reject_blocking_calls(files: list[tuple[str, str]]) -> None:
    """Reject Python files whose callbacks could stall the simulator.

    The harness advances physics only after each controller callback
    returns, so a ``time.sleep`` (or any ``*.sleep`` call) in a callback
    stretches every tick: choppy telemetry on a good day, a wall-clock
    timeout on a bad one. The AST walk flags real calls only — a comment
    or docstring mentioning ``sleep(`` is fine. Unparseable files are left
    for the later stages, which report their own precise errors.
    """
    for path, content in files:
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_sleep_call(node.func):
                raise BlockingControllerError(
                    f"Model output {path}:{node.lineno} calls sleep(), which"
                    " would stall the simulator: callbacks must return in a"
                    " few milliseconds. Use the timer period for pacing, not"
                    " a sleep call."
                )


def validate_correction_files(
    store: Store,
    project_id: str,
    model_files: list[Any],
) -> list[tuple[str, str]]:
    """Validate a correction's file list against the approved-path policy.

    Corrections may update existing project files or (re)create the
    documented controller entry. Anything else -- traversal, unexpected new
    files -- is rejected before anything is written.
    """
    if not model_files:
        raise MetaModelResponseError(
            "Model correction output contained no files; refusing to write"
            " an empty update."
        )
    approved = {entry["path"] for entry in store.list_files(project_id)}
    approved |= APPROVED_ENTRIES
    validated: list[tuple[str, str]] = []
    for model_file in model_files:
        path = file_service.validate_file_path(model_file.path)
        file_service.check_content_size(model_file.content)
        if path not in approved:
            raise MetaModelResponseError(
                f"Model correction targets unexpected file {path!r};"
                " corrections may only update existing project files or the"
                f" documented entries {', '.join(sorted(APPROVED_ENTRIES))}."
            )
        validated.append((path, model_file.content))
    reject_blocking_calls(validated)
    return validated


def snapshot_files(
    store: Store, project_id: str, paths: list[str]
) -> dict[str, str | None]:
    """Record current contents so a failed correction can be rolled back."""
    snapshot: dict[str, str | None] = {}
    for path in paths:
        stored = store.get_file(project_id, path)
        snapshot[path] = stored["content"] if stored else None
    return snapshot


def restore_files(
    store: Store, project_id: str, snapshot: dict[str, str | None]
) -> None:
    """Best-effort rollback of a partially applied correction."""
    for path, content in snapshot.items():
        try:
            if content is None:
                store.delete_file(project_id, path)
            else:
                store.save_file(project_id, path, content)
        except Exception:
            continue
