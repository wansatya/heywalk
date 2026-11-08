"""Background job runner for Foundry stages.

Jobs are persisted in ``foundry_jobs`` and executed on a small thread pool.
The API returns a job id immediately; the UI polls ``/api/foundry/jobs/{id}``.
A job never runs user-generated Python: every stage is platform code.
"""

from __future__ import annotations

import threading
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from ..db import utcnow_iso
from .store import FoundryStore

ProgressCallback = Callable[[int, str], None]
JobFunction = Callable[[ProgressCallback], dict[str, Any]]

#: Project status while a stage job runs. The frontend renders these as
#: in-progress states; the stage itself sets NEEDS_REVIEW/READY on success.
JOB_RUNNING_STATUS = {
    "analyze": "ANALYZING",
    "reconstruct": "RECONSTRUCTING",
    "assemble": "ASSEMBLING",
    "generate_mjcf": "GENERATING",
    "generate": "GENERATING",
    "validate": "VALIDATING",
}


class FoundryJobManager:
    def __init__(self, store: FoundryStore, max_workers: int = 2) -> None:
        self.store = store
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="foundry",
        )
        self._futures: dict[str, Future[Any]] = {}
        self._lock = threading.Lock()

    def submit(
        self, project_id: str, job_type: str, fn: JobFunction
    ) -> dict[str, Any]:
        active = self.store.active_job(project_id)
        if active is not None:
            raise ValueError(
                f"Foundry project already has a {active['job_type']} job"
                f" running ({active['status']})."
            )
        job = self.store.create_job(project_id, job_type)
        running = JOB_RUNNING_STATUS.get(job_type)
        if running is not None:
            self.store.update_project(
                project_id, status=running, error_message=None
            )
        future = self._executor.submit(self._run, job["id"], fn)
        with self._lock:
            self._futures[job["id"]] = future
        return job

    def _run(self, job_id: str, fn: JobFunction) -> None:
        self.store.update_job(
            job_id,
            status="running",
            progress=1,
            message="Starting…",
        )

        def progress(value: int, message: str) -> None:
            self.store.update_job(
                job_id,
                progress=max(0, min(100, int(value))),
                message=str(message)[:500],
            )

        try:
            result = fn(progress)
            self.store.update_job(
                job_id,
                status="succeeded",
                progress=100,
                message="Done.",
                result_json=result if isinstance(result, dict) else {"result": result},
                completed_at=utcnow_iso(),
            )
        except Exception as exc:  # job failures are data, not crashes
            summary = f"{type(exc).__name__}: {exc}"
            self.store.update_job(
                job_id,
                status="failed",
                progress=100,
                message="Failed.",
                error_message=summary[:2000],
                result_json={"traceback": traceback.format_exc()[-4000:]},
                completed_at=utcnow_iso(),
            )
            failed = self.store.get_job(job_id)
            if failed is not None:
                self.store.update_project(
                    failed["project_id"],
                    status="FAILED",
                    error_message=summary[:2000],
                )
        finally:
            with self._lock:
                self._futures.pop(job_id, None)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "JOB_RUNNING_STATUS",
    "FoundryJobManager",
    "JobFunction",
    "ProgressCallback",
]
