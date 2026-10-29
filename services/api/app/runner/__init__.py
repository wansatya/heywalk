"""Isolated execution-runner interface.

Generated controller code is untrusted: it must run inside an isolated
boundary (container or stronger, AGENTS.md section 12) and NEVER be imported
or exec()'d inside the FastAPI process.

Backends:

- :class:`NotConfiguredRunner` -- default; refuses to run anything until an
  isolated runner is configured (honest error, never fabricated telemetry).
- :class:`app.runner.docker_runner.DockerRunner` -- production backend;
  runs the ROS 2 controller in a dedicated non-root container with CPU,
  memory, process-count, disk, network, and time limits.
- :class:`app.runner.local_runner.LocalSubprocessRunner` -- dev/test
  fallback; runs the same harness as a host subprocess with timeout and
  process-group kill. NOT container isolation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from ..config import Settings


class RunnerError(Exception):
    """Base class for isolated-runner failures."""


class RunnerUnavailableError(RunnerError):
    """No execution runner is configured or reachable."""


@dataclass
class RunnerContext:
    run_id: str
    project_id: str
    controller_files: list[dict[str, str]]
    world_config: dict[str, Any]
    seed: int
    emit_log: Callable[[str], Awaitable[None]]
    emit_telemetry: Callable[[dict[str, Any]], Awaitable[None]]
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    estop_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class RunnerResult:
    exit_code: int | None
    failure_summary: str | None = None
    timed_out: bool = False
    cancelled: bool = False
    # Objective evidence from the MuJoCo automated joint tests (AGENTS.md §8).
    tests: list[dict[str, Any]] | None = None
    suite_size: int | None = None
    sim_timed_out: bool = False


class RunnerBackend(Protocol):
    name: str

    async def execute(self, ctx: RunnerContext) -> RunnerResult: ...

    async def pause(self, run_id: str) -> None:
        """Suspend a live run's physics without losing state."""
        ...

    async def resume(self, run_id: str) -> None:
        """Resume a run suspended by :meth:`pause`."""
        ...


class NotConfiguredRunner:
    """Default backend: refuses to run anything until isolation exists."""

    name = "not-configured"

    async def execute(self, ctx: RunnerContext) -> RunnerResult:
        raise RunnerUnavailableError(
            "No isolated execution runner is configured. The controller was"
            " NOT executed: wire the Docker/ROS 2 runner before running"
            " sequences."
        )

    async def pause(self, run_id: str) -> None:
        raise RunnerUnavailableError("No execution runner is configured.")

    async def resume(self, run_id: str) -> None:
        raise RunnerUnavailableError("No execution runner is configured.")


def build_runner(settings: Settings) -> RunnerBackend:
    """Select the runner backend from server-side settings.

    Unknown backend names fall back to :class:`NotConfiguredRunner` rather
    than silently executing code somewhere unexpected.
    """
    backend = (settings.runner_backend or "none").strip().lower()
    if backend == "docker":
        from .docker_runner import DockerRunner

        try:
            return DockerRunner(settings)
        except RunnerUnavailableError:
            return NotConfiguredRunner()
    if backend == "local":
        from .local_runner import LocalSubprocessRunner

        return LocalSubprocessRunner(settings)
    return NotConfiguredRunner()
