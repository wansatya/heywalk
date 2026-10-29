"""Run state-machine tests (AGENTS.md sections 10 and 14)."""

from __future__ import annotations

import pytest

from app.db import Store
from app.services.execution import (
    ALLOWED_TRANSITIONS,
    ExecutionManager,
    InvalidTransitionError,
    RunEventBus,
    check_transition,
    is_terminal,
)


def _run_id(store: Store) -> str:
    project = store.create_project("p", "task")
    run = store.create_run(
        project["id"],
        parent_run_id=None,
        controller_version=0,
        status="queued",
        world_config=None,
        seed=0,
    )
    return run["id"]


def test_happy_path_transitions(tmp_path):
    store = Store(tmp_path / "t.db")
    manager = ExecutionManager(store, bus=RunEventBus())
    run_id = _run_id(store)
    for status in ("preparing", "running", "evaluating", "succeeded"):
        manager.transition(run_id, status)
    run = store.get_run(run_id)
    assert run is not None and run["status"] == "succeeded"
    assert run["finished_at"] is not None
    events = store.list_events(run_id)
    statuses = [e["payload"]["status"] for e in events if e["event_type"] == "status"]
    assert statuses == ["preparing", "running", "evaluating", "succeeded"]
    assert all(e["timestamp"] for e in events)


def test_terminal_states_have_no_outgoing_transitions():
    for status in ("succeeded", "failed", "timeout", "cancelled", "error"):
        assert is_terminal(status)
        assert ALLOWED_TRANSITIONS[status] == ()


def test_illegal_transitions_rejected(tmp_path):
    store = Store(tmp_path / "t.db")
    manager = ExecutionManager(store, bus=RunEventBus())
    run_id = _run_id(store)
    with pytest.raises(InvalidTransitionError):
        manager.transition(run_id, "running")  # queued -> running skips preparing
    with pytest.raises(InvalidTransitionError):
        manager.transition(run_id, "succeeded")
    with pytest.raises(InvalidTransitionError):
        check_transition("running", "queued")  # backwards
    with pytest.raises(InvalidTransitionError):
        check_transition("bogus", "queued")  # unknown state


def test_cancel_paths(tmp_path):
    store = Store(tmp_path / "t.db")
    manager = ExecutionManager(store, bus=RunEventBus())
    first = _run_id(store)
    manager.transition(first, "cancelled", failure_summary="user")
    assert store.get_run(first)["status"] == "cancelled"

    second = _run_id(store)
    manager.transition(second, "preparing")
    manager.transition(second, "running")
    manager.transition(second, "cancelled", failure_summary="user")
    assert store.get_run(second)["status"] == "cancelled"


def test_terminal_run_cannot_transition(tmp_path):
    store = Store(tmp_path / "t.db")
    manager = ExecutionManager(store, bus=RunEventBus())
    run_id = _run_id(store)
    manager.transition(run_id, "preparing")
    manager.transition(run_id, "running")
    manager.transition(run_id, "evaluating")
    manager.transition(run_id, "failed", failure_summary="collision")
    with pytest.raises(InvalidTransitionError):
        manager.transition(run_id, "succeeded")
