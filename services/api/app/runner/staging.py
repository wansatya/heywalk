"""Shared staging helpers for isolated-runner backends (AGENTS.md §12).

Both the Docker runner and the local subprocess fallback stage the same
inputs: validated controller files plus the world config. Secrets never
enter the staging area, and the child environment is scrubbed of any
credential-shaped variables.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..services.files import validate_file_path
from . import topics
from .harness import DEFAULT_ENTRY, RESULT_FILENAME, WORLD_FILENAME

TELEMETRY_FILENAME = "telemetry.jsonl"
ESTOP_FILENAME = "estop"

SECRET_ENV_SUBSTRINGS: tuple[str, ...] = ("KEY", "SECRET", "TOKEN", "PASSWORD")

#: Numeric thread caps for the run boundary. MuJoCo/NumPy link OpenBLAS and
#: OpenMP, whose thread pools would otherwise fail (or spawn unpredictably)
#: under the runner's process-count/address-space limits. Single-threaded
#: numerics also keep MuJoCo runs deterministic.
THREAD_ENV_DEFAULTS: dict[str, str] = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    # The run boundary is headless: MuJoCo physics only, never rendering.
    # Disabling GL skips the GLFW probe (which forks) and any display need.
    "MUJOCO_GL": "disable",
}


def prepare_workspace(
    staging: Path,
    controller_files: list[dict[str, str]],
    world_config: dict[str, Any],
) -> tuple[Path, Path, str]:
    """Write controller files + world config; return (workspace, out, entry).

    Raises ValueError when no Python controller entry exists.
    """
    workspace = staging / "workspace"
    out_dir = staging / "out"
    workspace.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not isinstance(world_config, dict):
        raise ValueError("world_config must be a JSON object.")
    (workspace / WORLD_FILENAME).write_text(
        json.dumps(world_config), encoding="utf-8"
    )
    wrote: list[str] = []
    for entry in controller_files:
        raw_path = str(entry.get("path", ""))
        normalized = validate_file_path(raw_path)
        target = (workspace / normalized).resolve()
        workspace_resolved = workspace.resolve()
        if target != workspace_resolved and workspace_resolved not in (
            target.parents
        ):
            raise ValueError(f"Controller path escapes workspace: {raw_path!r}.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry.get("content", ""), encoding="utf-8")
        wrote.append(normalized)
    if DEFAULT_ENTRY in wrote:
        entry = DEFAULT_ENTRY
    else:
        entry = next(
            (name for name in sorted(wrote) if name.endswith(".py")), ""
        )
        if not entry:
            raise ValueError(
                "No Python controller file found; expected 'controller.py'."
            )
    stage_bundled_assets(workspace)
    return workspace, out_dir, entry


def stage_bundled_assets(workspace: Path) -> int:
    """Copy platform-bundled mesh assets a staged robot references.

    Generated robot descriptions are single-file MJCF; the official MicroDuck
    composition additionally references ``assets/*.stl`` meshes that live in
    the platform bundle (binary files cannot be project files). When a staged
    MJCF references that directory, copy the missing meshes from the bundle
    so MuJoCo resolves them in both development and production. Returns the
    number of files copied.
    """
    from ..simulator.microduck import ASSETS_DIR, is_composed_scene

    needs_assets = False
    for path in sorted(workspace.rglob("*.xml")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if (
            is_composed_scene(text)
            or f"{ASSETS_DIR}/" in text
            or f'meshdir="{ASSETS_DIR}"' in text
        ):
            needs_assets = True
            break
    if not needs_assets:
        return 0
    from ..simulator.microduck import stage_assets

    try:
        return stage_assets(workspace)
    except Exception as exc:
        raise ValueError(f"Could not stage MicroDuck mesh assets: {exc}.")


def scrub_env(env: dict[str, str]) -> dict[str, str]:
    """Drop credential-shaped variables before spawning a child boundary."""
    scrubbed = {}
    for key, value in env.items():
        upper = key.upper()
        if any(token in upper for token in SECRET_ENV_SUBSTRINGS):
            continue
        scrubbed[key] = value
    return scrubbed


def child_env(env: dict[str, str]) -> dict[str, str]:
    """Build the child environment: scrubbed credentials + thread caps."""
    prepared = scrub_env(env)
    prepared.update(THREAD_ENV_DEFAULTS)
    return prepared


def read_result(out_dir: Path) -> dict[str, Any]:
    path = out_dir / RESULT_FILENAME
    if not path.is_file():
        return {
            "exit_code": 1,
            "reason": "missing_result",
            "samples": 0,
            "timed_out": False,
            "failure_summary": "Harness ended without writing result.json.",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return {
            "exit_code": 1,
            "reason": "bad_result",
            "samples": 0,
            "timed_out": False,
            "failure_summary": f"Harness result was unreadable: {exc}.",
        }
    if not isinstance(data, dict):
        return {
            "exit_code": 1,
            "reason": "bad_result",
            "samples": 0,
            "timed_out": False,
            "failure_summary": "Harness result was not a JSON object.",
        }
    return data


class TelemetryTailer:
    """Incremental reader for the harness telemetry.jsonl file."""

    def __init__(self, out_dir: Path) -> None:
        self._path = out_dir / TELEMETRY_FILENAME
        self._offset = 0
        self.malformed_lines = 0

    def poll(self) -> list[dict[str, Any]]:
        samples: list[dict[str, Any]] = []
        try:
            with open(self._path, encoding="utf-8") as handle:
                handle.seek(self._offset)
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        raw = json.loads(stripped)
                        samples.append(
                            topics.normalize_telemetry_sample(raw)
                        )
                    except (ValueError, TypeError):
                        self.malformed_lines += 1
                self._offset = handle.tell()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return samples


def make_world_readable(tree: Path) -> None:
    """Open staging permissions so a non-root container user can read them."""
    for root, dirs, files in os.walk(tree):
        try:
            os.chmod(root, 0o755)
        except OSError:
            pass
        for name in list(dirs):
            try:
                os.chmod(os.path.join(root, name), 0o755)
            except OSError:
                pass
        for name in files:
            try:
                os.chmod(os.path.join(root, name), 0o644)
            except OSError:
                pass


def make_world_writable(directory: Path) -> None:
    """Open an output directory so a non-root container user can write."""
    try:
        os.chmod(directory, 0o777)
    except OSError:
        pass


def tests_payload(result: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Extract the automated-test evidence from a harness result."""
    raw = result.get("tests")
    if not isinstance(raw, list):
        return None
    tests = [item for item in raw if isinstance(item, dict)]
    return tests


def suite_size(result: dict[str, Any]) -> int | None:
    raw = result.get("suite_size")
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return None
    return max(0, size)
