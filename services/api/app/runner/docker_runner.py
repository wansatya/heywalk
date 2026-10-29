"""Docker-isolated execution runner (AGENTS.md section 12).

DockerRunner runs the generated ROS 2 controller inside a dedicated,
single-use container built from ``docker/runner.Dockerfile`` (ROS 2 Jazzy +
deterministic simulator + harness). The host API process never imports or
executes generated code.

Isolation properties per run:

- Non-root container user (host uid/gid when unprivileged, else ``nobody``).
- No Docker socket mount, no host credentials, no secret env passthrough.
- CPU, memory (+swap pinned to memory), and process-count limits.
- No outbound network by default (``--network none``).
- Read-only root filesystem; only ``/out`` (host temp dir) is writable,
  plus a small ``/tmp`` tmpfs for disk bounding.
- Dropped capabilities and ``no-new-privileges``.
- Wall-clock timeout plus cancel support; the container is killed and
  removed (``--rm`` + best-effort ``docker rm -f``).

The API host needs the ``docker`` CLI on PATH plus daemon access: on a host
that is the local daemon, and in compose that is the socket mounted into the
API *orchestrator* container (see docker-compose.yml). The socket must still
never be mounted into a *run* container, and generated code must never be
imported by the API process itself.
Staging honors ``TMPDIR``: in compose it is a host path bind-mounted at the
identical path inside the API container, so sibling bind-mounts resolve on
the daemon host (see docker-compose.yml).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from . import RunnerContext, RunnerError, RunnerResult, RunnerUnavailableError
from . import staging as staging_mod

# Also the telemetry-file poll cadence: the live view can only be as
# fresh as this, so keep it well under the 50 ms sample period.
POLL_INTERVAL_S = 0.03
MAX_LOG_LINES = 5000
MAX_LOG_LINE_CHARS = 2000
CONTAINER_STOP_TIMEOUT_S = 5
TMP_TMPFS = "/tmp:size=64m,mode=1777"
FALLBACK_CONTAINER_USER = "65534:65534"


@dataclass(frozen=True)
class DockerLimits:
    image: str
    cpus: float
    memory: str
    pids_limit: int
    network: str
    timeout_s: float


def container_user() -> str:
    """Non-root user spec for the run container."""
    if os.name == "posix" and hasattr(os, "geteuid"):
        try:
            if os.geteuid() != 0:  # type: ignore[attr-defined]
                return f"{os.getuid()}:{os.getgid()}"  # type: ignore[attr-defined]
        except OSError:
            pass
    return FALLBACK_CONTAINER_USER


def _sanitize_container_name(run_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "-", run_id)[:48]
    return f"heywalk-run-{cleaned or 'run'}"


def build_docker_command(
    *,
    container_name: str,
    workspace: Path,
    out_dir: Path,
    estop_file: Path,
    seed: int,
    entry: str,
    limits: DockerLimits,
    run_user: str,
) -> list[str]:
    """Build the ``docker run`` command with the full isolation profile."""
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--user",
        run_user,
        "--network",
        limits.network,
        "--cpus",
        f"{limits.cpus:g}",
        "--memory",
        limits.memory,
        "--memory-swap",
        limits.memory,
        "--pids-limit",
        str(limits.pids_limit),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        TMP_TMPFS,
        "--stop-timeout",
        str(CONTAINER_STOP_TIMEOUT_S),
        "-v",
        f"{workspace}:/workspace:ro",
        "-v",
        f"{out_dir}:/out",
        limits.image,
        "python3",
        "-m",
        "app.runner.harness",
        "--workspace",
        "/workspace",
        "--out",
        "/out",
        "--seed",
        str(seed),
        "--transport",
        "rclpy",
        "--entry",
        entry,
        "--estop-file",
        "/out/" + staging_mod.ESTOP_FILENAME,
    ]


class DockerRunner:
    """Container-isolated ROS 2 execution backend."""

    name = "docker"

    def __init__(self, settings: Settings) -> None:
        if shutil.which("docker") is None:
            raise RunnerUnavailableError(
                "Docker CLI not found on PATH: cannot start an isolated run."
            )
        self.limits = DockerLimits(
            image=settings.runner_image,
            cpus=max(0.1, float(settings.runner_cpus)),
            memory=settings.runner_memory,
            pids_limit=max(16, int(settings.runner_pids_limit)),
            network=settings.runner_network or "none",
            timeout_s=max(1.0, float(settings.runner_timeout_s)),
        )
        self._live: dict[str, str] = {}
        self._paused: set[str] = set()

    def is_paused(self, run_id: str) -> bool:
        return run_id in self._paused

    async def pause(self, run_id: str) -> None:
        """Suspend the run container (``docker pause``); state is kept."""
        name = self._live.get(run_id)
        if name is None:
            raise RunnerError(f"Run {run_id} has no live container to pause.")
        await _docker_pause(name, run_id, paused=True)
        self._paused.add(run_id)

    async def resume(self, run_id: str) -> None:
        """Resume a container suspended by :meth:`pause` (``docker unpause``)."""
        name = self._live.get(run_id)
        if name is None:
            raise RunnerError(f"Run {run_id} has no live container to resume.")
        if run_id not in self._paused:
            return
        await _docker_pause(name, run_id, paused=False)
        self._paused.discard(run_id)

    async def execute(self, ctx: RunnerContext) -> RunnerResult:
        try:
            with tempfile.TemporaryDirectory(
                prefix="heywalk-run-"
            ) as staging_dir:
                staging = Path(staging_dir)
                workspace, out_dir, entry = staging_mod.prepare_workspace(
                    staging, ctx.controller_files, ctx.world_config
                )
                staging_mod.make_world_readable(workspace)
                staging_mod.make_world_writable(out_dir)
                return await self._run_container(
                    ctx, workspace, out_dir, entry
                )
        except ValueError as exc:
            raise RunnerError(str(exc)) from None

    async def _run_container(
        self,
        ctx: RunnerContext,
        workspace: Path,
        out_dir: Path,
        entry: str,
    ) -> RunnerResult:
        name = _sanitize_container_name(ctx.run_id)
        command = build_docker_command(
            container_name=name,
            workspace=workspace,
            out_dir=out_dir,
            estop_file=out_dir / staging_mod.ESTOP_FILENAME,
            seed=ctx.seed,
            entry=entry,
            limits=self.limits,
            run_user=container_user(),
        )
        await ctx.emit_log(
            f"Starting isolated container {name} (image {self.limits.image},"
            f" network {self.limits.network}, cpus {self.limits.cpus:g},"
            f" memory {self.limits.memory})."
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=staging_mod.child_env(dict(os.environ)),
            )
        except OSError as exc:
            raise RunnerError(f"Failed to start container: {exc}.") from None

        assert process.stdout is not None
        self._live[ctx.run_id] = name
        tailer = staging_mod.TelemetryTailer(out_dir)
        log_lines = 0
        estop_touched = False
        timed_out = False
        cancelled = False
        started = time.monotonic()
        last_poll = started
        pending_reader: asyncio.Task[bytes] | None = None
        image_missing_hint: str | None = None

        cancelling = False
        cancel_deadline = 0.0
        try:
            while True:
                now = time.monotonic()
                if ctx.run_id in self._paused:
                    # A paused run holds its physics state; the wall-clock
                    # budget must not drain while it is suspended.
                    started += now - last_poll
                    if cancelling:
                        cancel_deadline += now - last_poll
                last_poll = now
                if ctx.cancel_event.is_set() and not cancelling:
                    # Graceful stop: let the harness settle to STAND
                    # instead of freezing mid-stride, then exit cleanly.
                    cancelling = True
                    cancel_deadline = now + 6.0
                    await ctx.emit_log(
                        "Stop requested; settling to a safe stand…"
                    )
                    try:
                        (out_dir / staging_mod.ESTOP_FILENAME).write_text(
                            "estop\n", encoding="utf-8"
                        )
                    except OSError:
                        pass
                    estop_touched = True
                if cancelling:
                    if process.returncode is not None:
                        cancelled = True
                        break
                    if now >= cancel_deadline:
                        cancelled = True
                        await ctx.emit_log(
                            "Settle timed out; killing container."
                        )
                        await _docker_kill(name)
                        break
                if ctx.estop_event.is_set() and not estop_touched:
                    try:
                        (out_dir / staging_mod.ESTOP_FILENAME).write_text(
                            "estop\n", encoding="utf-8"
                        )
                    except OSError:
                        pass
                    estop_touched = True
                if time.monotonic() - started > self.limits.timeout_s:
                    timed_out = True
                    await ctx.emit_log(
                        f"Run exceeded the {self.limits.timeout_s:g}s"
                        " wall-clock limit; killing container."
                    )
                    await _docker_kill(name)
                    break
                if pending_reader is None:
                    pending_reader = asyncio.create_task(
                        process.stdout.readline()
                    )
                done, _ = await asyncio.wait(
                    {pending_reader}, timeout=POLL_INTERVAL_S
                )
                if pending_reader in done:
                    try:
                        line = pending_reader.result()
                    except (asyncio.CancelledError, Exception):
                        line = b""
                    pending_reader = None
                    if line:
                        text = line.decode("utf-8", "replace").rstrip("\n")
                        if (
                            "Unable to find image" in text
                            or "No such image" in text
                        ):
                            image_missing_hint = text.strip()
                        if len(text) > MAX_LOG_LINE_CHARS:
                            text = text[:MAX_LOG_LINE_CHARS] + "…[truncated]"
                        if log_lines < MAX_LOG_LINES:
                            log_lines += 1
                            await ctx.emit_log(text)
                    elif process.returncode is not None:
                        break
                for sample in tailer.poll():
                    await ctx.emit_telemetry(sample)
                if process.returncode is not None and pending_reader is None:
                    leftover = await process.stdout.read()
                    for raw in leftover.splitlines():
                        text = raw.decode("utf-8", "replace")
                        if log_lines < MAX_LOG_LINES:
                            log_lines += 1
                            await ctx.emit_log(text[:MAX_LOG_LINE_CHARS])
                    break
            try:
                await asyncio.wait_for(process.wait(), timeout=30.0)
            except (asyncio.TimeoutError, TimeoutError):
                await _docker_kill(name)
                await process.wait()
        finally:
            if pending_reader is not None:
                pending_reader.cancel()
            for sample in tailer.poll():
                await ctx.emit_telemetry(sample)
            self._live.pop(ctx.run_id, None)
            self._paused.discard(ctx.run_id)
            await _docker_rm(name)

        if cancelling:
            # The harness exited on its own after the graceful-stop
            # signal (settle to STAND, then exit 0): still a cancel.
            cancelled = True
        if tailer.malformed_lines:
            await ctx.emit_log(
                f"Ignored {tailer.malformed_lines} malformed telemetry line(s)."
            )
        if cancelled:
            return RunnerResult(
                exit_code=None, failure_summary="Run cancelled.",
                cancelled=True,
            )
        if timed_out:
            return RunnerResult(
                exit_code=None,
                failure_summary=(
                    f"Run exceeded the {self.limits.timeout_s:g}s"
                    " wall-clock limit."
                ),
                timed_out=True,
            )
        if image_missing_hint is not None or process.returncode == 125:
            raise RunnerError(
                "Runner image"
                f" '{self.limits.image}' is unavailable"
                f" ({image_missing_hint or 'docker exit 125'})."
                " Build it with: docker compose --profile runner-build build runner."
            )
        result = staging_mod.read_result(out_dir)
        exit_code = result.get("exit_code", process.returncode or 0)
        try:
            exit_code_int: int | None = int(exit_code)
        except (TypeError, ValueError):
            exit_code_int = process.returncode
        failure = result.get("failure_summary")
        if exit_code_int not in (None, 0) and not failure:
            failure = f"Harness exited with code {exit_code_int}."
        return RunnerResult(
            exit_code=exit_code_int,
            failure_summary=failure,
            tests=staging_mod.tests_payload(result),
            suite_size=staging_mod.suite_size(result),
            sim_timed_out=bool(result.get("sim_timed_out", False)),
        )


async def _docker_pause(container_name: str, run_id: str, *, paused: bool) -> None:
    action = "pause" if paused else "unpause"
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            action,
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(process.wait(), timeout=15.0)
    except (OSError, asyncio.TimeoutError, TimeoutError) as exc:
        raise RunnerError(
            f"Could not {action} run {run_id}: {exc}."
        ) from None
    if process.returncode != 0:
        raise RunnerError(
            f"Could not {action} run {run_id}: docker exited"
            f" {process.returncode}."
        )


async def _docker_kill(container_name: str) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "kill",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(process.wait(), timeout=15.0)
    except (OSError, asyncio.TimeoutError, TimeoutError):
        pass


async def _docker_rm(container_name: str) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "-f",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(process.wait(), timeout=15.0)
    except (OSError, asyncio.TimeoutError, TimeoutError):
        pass
