"""Local subprocess runner: dev/test fallback WITHOUT container isolation.

LocalSubprocessRunner executes the same harness entrypoint as the Docker
runner but as a plain host subprocess. It still never imports or exec()'s
generated code in the FastAPI process, and it applies belt-and-braces
limits where the OS allows (POSIX rlimits, wall-clock timeout, process-group
kill on cancel/timeout, scrubbed environment).

This backend is explicitly NOT the §12 isolation boundary: it cannot disable
host networking or filesystem access for the child. It exists so developers
and unit tests can exercise the real execute -> telemetry -> evaluate path
without Docker or ROS 2. Production runs must use DockerRunner.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from ..config import Settings
from . import RunnerContext, RunnerError, RunnerResult
from . import staging as staging_mod

# Also the telemetry-file poll cadence (kept under the sample period).
POLL_INTERVAL_S = 0.03
MAX_LOG_LINES = 5000
MAX_LOG_LINE_CHARS = 2000
# POSIX rlimits for the child (best effort; ignored off-POSIX).
CHILD_CPU_SECONDS = 600
CHILD_ADDRESS_SPACE_BYTES = 1 << 30  # 1 GiB
#: Process cap enforced by DockerRunner (PID namespace), not by rlimits.
CHILD_MAX_PROCESSES = 64
CHILD_MAX_FILE_BYTES = 256 << 20  # 256 MiB


def _child_limits() -> None:
    try:
        import resource
    except ImportError:  # non-POSIX: no rlimits available
        return
    try:
        resource.setrlimit(
            resource.RLIMIT_CPU, (CHILD_CPU_SECONDS, CHILD_CPU_SECONDS)
        )
        resource.setrlimit(
            resource.RLIMIT_AS,
            (CHILD_ADDRESS_SPACE_BYTES, CHILD_ADDRESS_SPACE_BYTES),
        )
        # RLIMIT_NPROC is deliberately NOT set here: it is counted per real
        # UID, and this fallback runs as the developer's own user, so a small
        # cap breaks unrelated processes. The Docker runner enforces the real
        # process limit inside its own PID namespace.
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (CHILD_MAX_FILE_BYTES, CHILD_MAX_FILE_BYTES),
        )
    except (ValueError, OSError):
        pass


class LocalSubprocessRunner:
    """Subprocess harness execution (dev/test fallback, NOT isolated)."""

    name = "local-subprocess"

    def __init__(self, settings: Settings | None = None) -> None:
        timeout = settings.runner_timeout_s if settings else 180.0
        self.timeout_s = max(1.0, float(timeout))
        self._live: dict[str, int] = {}
        self._paused: set[str] = set()

    def is_paused(self, run_id: str) -> bool:
        return run_id in self._paused

    async def pause(self, run_id: str) -> None:
        """Suspend the harness process group (SIGSTOP); state is kept."""
        pgid = self._live.get(run_id)
        if pgid is None:
            raise RunnerError(f"Run {run_id} has no live process to pause.")
        if os.name != "posix":
            raise RunnerError("Pause is only supported on POSIX systems.")
        try:
            os.killpg(pgid, signal.SIGSTOP)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            raise RunnerError(f"Could not pause run {run_id}: {exc}.") from None
        self._paused.add(run_id)

    async def resume(self, run_id: str) -> None:
        """Resume a process group suspended by :meth:`pause` (SIGCONT)."""
        pgid = self._live.get(run_id)
        if pgid is None:
            raise RunnerError(f"Run {run_id} has no live process to resume.")
        if run_id not in self._paused:
            return
        if os.name != "posix":
            raise RunnerError("Resume is only supported on POSIX systems.")
        try:
            os.killpg(pgid, signal.SIGCONT)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            raise RunnerError(f"Could not resume run {run_id}: {exc}.") from None
        self._paused.discard(run_id)

    def _command(
        self, workspace: Path, out_dir: Path, seed: int, entry: str,
        estop_file: Path,
    ) -> list[str]:
        return [
            sys.executable,
            "-m",
            "app.runner.harness",
            "--workspace",
            str(workspace),
            "--out",
            str(out_dir),
            "--seed",
            str(seed),
            "--transport",
            "loopback",
            "--entry",
            entry,
            "--estop-file",
            str(estop_file),
        ]

    async def execute(self, ctx: RunnerContext) -> RunnerResult:
        try:
            with tempfile.TemporaryDirectory(
                prefix="heywalk-run-"
            ) as staging_dir:
                staging = Path(staging_dir)
                workspace, out_dir, entry = staging_mod.prepare_workspace(
                    staging, ctx.controller_files, ctx.world_config
                )
                estop_file = out_dir / staging_mod.ESTOP_FILENAME
                return await self._run_harness(
                    ctx, workspace, out_dir, entry, estop_file
                )
        except ValueError as exc:
            raise RunnerError(str(exc)) from None

    async def _run_harness(
        self,
        ctx: RunnerContext,
        workspace: Path,
        out_dir: Path,
        entry: str,
        estop_file: Path,
    ) -> RunnerResult:
        command = self._command(
            workspace, out_dir, ctx.seed, entry, estop_file
        )
        await ctx.emit_log(
            "Local subprocess runner (dev/test fallback; NOT container-isolated)."
        )
        await ctx.emit_log(f"Entry: {entry}; seed: {ctx.seed}.")

        # The harness entrypoint must resolve regardless of the API's cwd.
        import app as _app_package

        api_root = str(Path(_app_package.__file__).resolve().parent.parent)
        child_env = staging_mod.child_env(dict(os.environ))
        existing_path = child_env.get("PYTHONPATH", "")
        child_env["PYTHONPATH"] = (
            api_root if not existing_path
            else api_root + os.pathsep + existing_path
        )
        preexec = _child_limits if os.name == "posix" else None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=child_env,
                preexec_fn=preexec,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise RunnerError(f"Failed to spawn harness: {exc}.") from None

        assert process.stdout is not None
        if os.name == "posix" and process.pid:
            self._live[ctx.run_id] = process.pid
        tailer = staging_mod.TelemetryTailer(out_dir)
        log_lines = 0
        estop_touched = False
        timed_out = False
        cancelled = False
        started = time.monotonic()
        last_poll = started
        pending_reader: asyncio.Task[bytes] | None = None

        async def kill_tree() -> None:
            if process.returncode is not None:
                return
            try:
                if os.name == "posix" and process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass

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
                    # (locomotion) or hold pose (joint tests) instead of
                    # freezing mid-stride, then exit cleanly.
                    cancelling = True
                    cancel_deadline = now + 6.0
                    await ctx.emit_log(
                        "Stop requested; settling to a safe stand…"
                    )
                    try:
                        estop_file.write_text("estop\n", encoding="utf-8")
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
                            "Settle timed out; killing run."
                        )
                        await kill_tree()
                        break
                if ctx.estop_event.is_set() and not estop_touched:
                    try:
                        estop_file.write_text("estop\n", encoding="utf-8")
                    except OSError:
                        pass
                    estop_touched = True
                if time.monotonic() - started > self.timeout_s:
                    timed_out = True
                    await ctx.emit_log(
                        f"Run exceeded the {self.timeout_s:g}s wall-clock limit;"
                        " killing run."
                    )
                    await kill_tree()
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
                    # Drain any last line, then exit.
                    leftover = await process.stdout.read()
                    for raw in leftover.splitlines():
                        text = raw.decode("utf-8", "replace")
                        if log_lines < MAX_LOG_LINES:
                            log_lines += 1
                            await ctx.emit_log(text[:MAX_LOG_LINE_CHARS])
                    break
            try:
                await asyncio.wait_for(process.wait(), timeout=10.0)
            except (asyncio.TimeoutError, TimeoutError):
                await kill_tree()
                await process.wait()
        finally:
            if pending_reader is not None:
                pending_reader.cancel()
            # Final telemetry drain.
            for sample in tailer.poll():
                await ctx.emit_telemetry(sample)
            self._live.pop(ctx.run_id, None)
            self._paused.discard(ctx.run_id)

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
                    f"Run exceeded the {self.timeout_s:g}s wall-clock limit."
                ),
                timed_out=True,
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
