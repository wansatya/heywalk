"""Server-side configuration. Secrets stay here; never sent to the browser."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str
    model_api_key: str
    meta_api_base_url: str
    muse_model: str
    model_timeout_s: float
    cors_origins: tuple[str, ...]
    # Isolated execution runner (AGENTS.md sections 5 and 12). "none" keeps
    # the honest not-configured backend; "local" uses the subprocess fallback
    # (dev/test only); "docker" uses the isolated container runner.
    runner_backend: str = "none"
    runner_image: str = "heywalk-runner:jazzy"
    runner_timeout_s: float = 180.0
    runner_cpus: float = 1.0
    runner_memory: str = "1024m"
    runner_pids_limit: int = 128
    runner_network: str = "none"
    # Failure-to-fix loop (AGENTS.md section 9): max correction attempts
    # per failed run before the UI must show evidence and stop.
    max_fix_attempts: int = 2
    # Root for the per-sequence working folders (<root>/<project_id>/). These
    # hold one folder per sequence, so they are deliberately NOT called
    # "projects": the SQLite table and the /api/projects routes keep that
    # name. Empty derives "<db_dir>/sequences". Compose sets
    # HEYWALK_SEQUENCES to a bind mount on the repo's ./sequences so the
    # folders are visible on the host.
    sequence_root: str = ""


def load_settings() -> Settings:
    origins = tuple(
        o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()
    ) or ("*",)
    return Settings(
        db_path=os.environ.get("HEYWALK_DB", "./heywalk.db"),
        model_api_key=os.environ.get("MODEL_API_KEY", ""),
        meta_api_base_url=os.environ.get(
            "META_API_BASE_URL", "https://api.meta.ai/v1"
        ),
        muse_model=os.environ.get("MUSE_MODEL", "muse-spark-1.3"),
        model_timeout_s=float(os.environ.get("MODEL_TIMEOUT_S", "120")),
        cors_origins=origins,
        runner_backend=os.environ.get("RUNNER_BACKEND", "none").strip().lower(),
        runner_image=os.environ.get("RUNNER_IMAGE", "heywalk-runner:jazzy"),
        runner_timeout_s=float(os.environ.get("RUNNER_TIMEOUT_S", "180")),
        runner_cpus=float(os.environ.get("RUNNER_CPUS", "1.0")),
        runner_memory=os.environ.get("RUNNER_MEMORY", "1024m"),
        runner_pids_limit=int(os.environ.get("RUNNER_PIDS_LIMIT", "128")),
        runner_network=os.environ.get("RUNNER_NETWORK", "none"),
        max_fix_attempts=int(os.environ.get("MAX_FIX_ATTEMPTS", "2")),
        # HEYWALK_PROJECTS is the pre-rename variable name; still honored so
        # existing .env files keep pointing at the same folders.
        sequence_root=os.environ.get("HEYWALK_SEQUENCES")
        or os.environ.get("HEYWALK_PROJECTS", ""),
    )
