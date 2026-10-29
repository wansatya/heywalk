"""Run orchestration endpoints (AGENTS.md section 10)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..db import Store
from ..models.schemas import (
    RunComparison,
    RunCreate,
    RunOut,
    TelemetrySample,
    TestResultOut,
)
from ..services import experiments
from ..services.execution import (
    ExecutionManager,
    RunNotFoundError,
    RunPreconditionError,
    WorldConfigTooLargeError,
)

project_router = APIRouter(prefix="/api/projects", tags=["runs"])
run_router = APIRouter(prefix="/api/runs", tags=["runs"])


def _exec(request: Request) -> ExecutionManager:
    return request.app.state.exec


def _store(request: Request) -> Store:
    return request.app.state.store


@project_router.post("/{project_id}/runs", response_model=RunOut, status_code=201)
async def create_run(
    project_id: str, body: RunCreate, request: Request
) -> RunOut:
    try:
        run = await _exec(request).start_run(
            project_id,
            world_config=body.world_config,
            seed=body.seed,
            parent_run_id=body.parent_run_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RunPreconditionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WorldConfigTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RunOut(**run)


@project_router.get("/{project_id}/runs", response_model=list[RunOut])
def list_runs(project_id: str, request: Request) -> list[RunOut]:
    store = _store(request)
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    manager = _exec(request)
    return [RunOut(**manager.get_run(r["id"])) for r in store.list_runs(project_id)]


@run_router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: str, request: Request) -> RunOut:
    try:
        return RunOut(**_exec(request).get_run(run_id))
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found.") from exc


@run_router.post("/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: str, request: Request) -> RunOut:
    try:
        return RunOut(**await _exec(request).cancel_run(run_id))
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found.") from exc


@run_router.post("/{run_id}/rerun", response_model=RunOut, status_code=201)
async def rerun_run(run_id: str, request: Request) -> RunOut:
    """Start a fresh run of the same scenario (world + seed).

    The corrected controller is rerun in a fully reset simulation with the
    same scenario configuration; the new run records the rerun parent so
    the UI can compare before/after metrics.
    """
    try:
        return RunOut(**await _exec(request).rerun_run(run_id))
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found.") from exc
    except RunPreconditionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WorldConfigTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@run_router.post("/{run_id}/estop", response_model=RunOut)
async def latch_estop(run_id: str, request: Request) -> RunOut:
    """Latch the emergency stop for a live run (frontend extension)."""
    try:
        return RunOut(**await _exec(request).latch_estop(run_id))
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found.") from exc


@run_router.post("/{run_id}/pause", response_model=RunOut)
async def pause_run(run_id: str, request: Request) -> RunOut:
    """Suspend a live run's physics without losing state."""
    try:
        return RunOut(**await _exec(request).pause_run(run_id))
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@run_router.post("/{run_id}/resume", response_model=RunOut)
async def resume_run(run_id: str, request: Request) -> RunOut:
    """Resume a run suspended by pause."""
    try:
        return RunOut(**await _exec(request).resume_run(run_id))
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@run_router.get("/{run_id}/logs", response_model=list[str])
def get_logs(run_id: str, request: Request) -> list[str]:
    store = _store(request)
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return store.list_logs(run_id)


@run_router.get("/{run_id}/telemetry", response_model=list[TelemetrySample])
def get_telemetry(run_id: str, request: Request) -> list[TelemetrySample]:
    store = _store(request)
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return [TelemetrySample(**s) for s in store.list_telemetry(run_id)]


@run_router.get("/{run_id}/tests", response_model=list[TestResultOut])
def get_tests(run_id: str, request: Request) -> list[TestResultOut]:
    """Objective automated-test evidence for a MuJoCo run (AGENTS.md §8)."""
    store = _store(request)
    if store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return [TestResultOut(**test) for test in store.list_test_results(run_id)]


@run_router.get("/{run_id}/comparison", response_model=RunComparison)
def get_comparison(
    run_id: str, request: Request, baseline_run_id: str | None = None
) -> RunComparison:
    comparison = experiments.compare_with_baseline(
        _store(request), run_id, baseline_run_id=baseline_run_id
    )
    if comparison is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return RunComparison(**comparison)
