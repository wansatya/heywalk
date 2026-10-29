"""Live run event stream (WS /api/runs/{run_id}/stream).

On connect the handler replays the run's history, then forwards live events
until a terminal status arrives.

Telemetry is *coalesced*: the newest sample is sent at most every
``TELEMETRY_MIN_INTERVAL_S`` and samples that arrive in between are skipped
rather than queued. Too much depends on this to be a plain sleep: the
harness samples at 20 Hz and sleeping 50 ms before *every* send caps the
send rate just below 20 Hz, so the stream drifts behind real time (~1 s
after a 25 s run, unbounded for longer ones). Skipped samples are already
stored in ``run_telemetry``: the client loads full history through
``GET /api/runs/{id}/telemetry`` and only needs the newest pose to animate.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..services.execution import ExecutionManager, is_terminal

router = APIRouter(tags=["stream"])

TELEMETRY_MIN_INTERVAL_S = 0.05  # ~20/s

#: Stored telemetry samples replayed instantly to a (re)connecting client to
#: seed its trail. Replaying *every* sample at 20 Hz made a client that
#: connected mid-run fall behind by the run's whole history; the full trail
#: comes from the REST telemetry endpoint instead.
REPLAY_TELEMETRY_SAMPLES = 40


class TelemetryCoalescer:
    """Keep only the newest telemetry sample until it may be sent."""

    def __init__(self, min_interval_s: float = TELEMETRY_MIN_INTERVAL_S) -> None:
        self.min_interval_s = min_interval_s
        self._latest: dict[str, Any] | None = None
        self._last_sent: float | None = None

    def offer(self, event: dict[str, Any]) -> None:
        """Accept a sample, replacing any older one that was never sent."""
        self._latest = event

    @property
    def pending(self) -> bool:
        return self._latest is not None

    def wait_s(self, now: float) -> float:
        """Seconds until the pending sample may be sent (0 = send now)."""
        if self._latest is None or self._last_sent is None:
            return 0.0
        return max(0.0, self._last_sent + self.min_interval_s - now)

    def take(self, now: float | None = None) -> dict[str, Any]:
        """Pop the pending sample and restart the interval."""
        event = self._latest
        self._latest = None
        self._last_sent = time.monotonic() if now is None else now
        assert event is not None
        return event


def replay_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """History for a (re)connecting client.

    Every non-telemetry event keeps its place and order; telemetry is
    trimmed to the newest ``REPLAY_TELEMETRY_SAMPLES`` so the client starts
    near the live state instead of watching the whole run again.
    """
    telemetry_total = sum(1 for e in events if e.get("type") == "telemetry")
    skip = telemetry_total - REPLAY_TELEMETRY_SAMPLES
    kept: list[dict[str, Any]] = []
    seen = 0
    for event in events:
        if event.get("type") == "telemetry":
            seen += 1
            if seen <= skip:
                continue
        kept.append(event)
    return kept


@router.websocket("/api/runs/{run_id}/stream")
async def stream_run(websocket: WebSocket, run_id: str) -> None:
    await websocket.accept()
    manager: ExecutionManager = websocket.app.state.exec
    try:
        run = manager.store.get_run(run_id)
    except Exception:
        run = None
    if run is None:
        await websocket.send_json(
            {"type": "error", "message": f"Unknown run: {run_id}."}
        )
        await websocket.close(code=4404)
        return

    try:
        history = manager.replay_events(run_id)
        last_status: str | None = None
        for event in replay_events(history):
            if event.get("type") == "status":
                last_status = event.get("status")
            await websocket.send_json(event)
        if last_status is not None and is_terminal(last_status):
            await websocket.close(code=1000)
            return

        queue = manager.bus.subscribe(run_id)
        coalescer = TelemetryCoalescer()
        try:
            while True:
                if coalescer.pending:
                    delay = coalescer.wait_s(time.monotonic())
                    if delay <= 0:
                        await websocket.send_json(coalescer.take())
                        continue
                    timeout: float | None = delay
                else:
                    timeout = None
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=timeout)
                except (asyncio.TimeoutError, TimeoutError):
                    continue
                if event.get("type") == "telemetry":
                    coalescer.offer(event)
                    continue
                if event.get("type") == "status":
                    last_status = event.get("status")
                await websocket.send_json(event)
                if event.get("type") == "status" and is_terminal(
                    event.get("status")
                ):
                    await websocket.close(code=1000)
                    return
        finally:
            manager.bus.unsubscribe(run_id, queue)
    except WebSocketDisconnect:
        return
