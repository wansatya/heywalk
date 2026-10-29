"""WebSocket stream tests: replay, live events, terminal close, pacing."""

from __future__ import annotations

import time

import pytest

from app.api.stream import (
    REPLAY_TELEMETRY_SAMPLES,
    TELEMETRY_MIN_INTERVAL_S,
    TelemetryCoalescer,
    replay_events,
)


def _install_robot(client, project_id, robot_id="arm"):
    """Give the project a validated starter robot so MuJoCo runs can start."""
    response = client.post(
        f"/api/projects/{project_id}/robot", json={"robot_id": robot_id}
    )
    assert response.status_code == 200, response.text
    return response.json()



def test_coalescer_sends_newest_sample_without_queueing():
    """Skipping samples keeps the live view fresh; queueing makes it drift.

    The harness samples at 20 Hz (50 ms). Sleeping 50 ms before every send
    made the send rate lag the sample rate, so a 20 s run ended about a
    second behind real time.
    """
    coalescer = TelemetryCoalescer(min_interval_s=0.05)
    assert not coalescer.pending
    coalescer.offer({"type": "telemetry", "sample": {"t": 0.0}})
    assert coalescer.wait_s(100.0) == pytest.approx(0.0)  # first is prompt
    assert coalescer.take(now=100.0)["sample"]["t"] == 0.0
    # Too soon: nothing is sent yet and the interval is reported.
    coalescer.offer({"type": "telemetry", "sample": {"t": 0.05}})
    assert coalescer.wait_s(100.02) > 0.0
    # Newer samples replace the pending one instead of queueing behind it.
    coalescer.offer({"type": "telemetry", "sample": {"t": 0.10}})
    coalescer.offer({"type": "telemetry", "sample": {"t": 0.15}})
    assert coalescer.wait_s(100.06) == pytest.approx(0.0)
    assert coalescer.take(now=100.06)["sample"]["t"] == 0.15
    assert not coalescer.pending
    assert TELEMETRY_MIN_INTERVAL_S == pytest.approx(0.05)


def test_replay_keeps_every_event_but_only_recent_telemetry():
    events = [{"type": "status", "status": "queued"}]
    for index in range(REPLAY_TELEMETRY_SAMPLES + 10):
        events.append({"type": "telemetry", "sample": {"t": index * 0.05}})
        if index == 2:
            events.append({"type": "log", "line": "hello"})
    events.append({"type": "evaluation", "evaluation": {}})

    replayed = replay_events(events)
    kinds = [event["type"] for event in replayed]
    assert kinds[0] == "status"
    assert kinds[-1] == "evaluation"
    assert "log" in kinds
    telemetry = [e for e in replayed if e["type"] == "telemetry"]
    assert len(telemetry) == REPLAY_TELEMETRY_SAMPLES
    # The newest samples survive, so a client joining mid-run starts live.
    assert telemetry[-1]["sample"]["t"] == pytest.approx(
        (REPLAY_TELEMETRY_SAMPLES + 9) * 0.05
    )


def test_stream_unknown_run_errors_and_closes(client):
    with client.websocket_connect("/api/runs/missing/stream") as websocket:
        event = websocket.receive_json()
        assert event["type"] == "error"
        assert "Unknown run" in event["message"]


def test_stream_replays_history_and_closes_on_terminal(client, project_id):
    _install_robot(client, project_id)
    run_id = client.post(f"/api/projects/{project_id}/runs", json={}).json()["id"]

    events: list[dict] = []
    deadline = time.time() + 15.0
    with client.websocket_connect(f"/api/runs/{run_id}/stream") as websocket:
        while time.time() < deadline:
            try:
                events.append(websocket.receive_json())
            except Exception:
                break

    kinds = {e["type"] for e in events}
    assert "status" in kinds
    assert "log" in kinds
    assert "evaluation" in kinds
    statuses = [e["status"] for e in events if e["type"] == "status"]
    assert statuses[0] == "queued"
    assert statuses[-1] in ("succeeded", "failed", "timeout", "cancelled", "error")
    evaluation = next(e for e in events if e["type"] == "evaluation")
    assert evaluation["evaluation"]["success"] is False
