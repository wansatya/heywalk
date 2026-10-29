"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import generation, projects, robots, runs, stream
from .config import Settings, load_settings
from .db import Store
from .runner import build_runner
from .services.execution import ExecutionManager, RunEventBus

API_VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    manager: ExecutionManager | None = getattr(app.state, "exec", None)
    if manager is not None:
        await manager.shutdown()


def create_app(
    db_path: str | None = None, settings: Settings | None = None
) -> FastAPI:
    resolved = settings or load_settings()
    store = Store(
        db_path or resolved.db_path,
        sequence_root=resolved.sequence_root or None,
    )
    manager = ExecutionManager(
        store, bus=RunEventBus(), runner=build_runner(resolved)
    )

    app = FastAPI(title="HeyWalk API", version=API_VERSION, lifespan=lifespan)
    app.state.settings = resolved
    app.state.store = store
    app.state.exec = manager

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health", tags=["meta"])
    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "heywalk-api", "version": API_VERSION}

    app.include_router(projects.router)
    app.include_router(generation.router)
    app.include_router(robots.router)
    app.include_router(runs.project_router)
    app.include_router(runs.run_router)
    app.include_router(stream.router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
