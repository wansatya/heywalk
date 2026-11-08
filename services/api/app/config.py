"""Server-side configuration. Secrets stay here; never sent to the browser.

Credential loading order for every setting below:

1. An explicit process environment variable always wins (compose passes
   ``${...}`` values this way; ``HEYWALK_NO_DOTENV=1`` skips step 2).
2. Otherwise the value is read from the repo ``.env`` file, so host-side
   ``uvicorn app.main:app`` picks up the same credentials as the compose
   stack without exporting them first.
3. Otherwise the documented default applies.

Only known HeyWalk keys are imported from ``.env``; anything else in the
file is ignored. Values are never logged.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


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
    # Robot Foundry robot library. Empty derives
    # ``<api package>/simulator/foundry`` so generated robots live next to
    # the bundled MicroDuck/ED-209 packages and are discoverable by the
    # simulator robot catalog. Each robot is one folder, not a sequence.
    foundry_root: str = ""
    foundry_workers: int = 2
    foundry_validation_timeout_s: float = 30.0
    # Meta SAM 3.1 runs on the Responses API. Leave sam_endpoint empty to
    # derive "{META_API_BASE_URL}/responses" when MODEL_API_KEY is set.
    # SAM31_ENDPOINT may also be a full custom Responses URL. When neither
    # a key nor an endpoint is configured the Foundry uses its deterministic
    # offline fallback, clearly labelled heuristic_v1.
    sam_endpoint: str = ""
    sam_api_key: str = ""
    sam_model: str = "sam-3.1"
    sam_timeout_s: float = 60.0
    # Optional learned image-to-3D provider. When empty the reconstruction
    # stage uses mask-outline extrusion (labelled outline_extrusion_v1).
    image3d_endpoint: str = ""
    image3d_api_key: str = ""
    image3d_timeout_s: float = 120.0
    # Which .env file supplied fallback values ("" when none was used).
    dotenv_path: str = ""


#: Keys load_settings may import from .env. Anything else in the file is
#: ignored so unrelated entries can never leak into the process config.
KNOWN_DOTENV_KEYS = frozenset(
    {
        "HEYWALK_DB",
        "HEYWALK_SEQUENCES",
        "HEYWALK_PROJECTS",
        "MODEL_API_KEY",
        "META_API_BASE_URL",
        "MUSE_MODEL",
        "MODEL_TIMEOUT_S",
        "CORS_ORIGINS",
        "RUNNER_BACKEND",
        "RUNNER_IMAGE",
        "RUNNER_TIMEOUT_S",
        "RUNNER_CPUS",
        "RUNNER_MEMORY",
        "RUNNER_PIDS_LIMIT",
        "RUNNER_NETWORK",
        "MAX_FIX_ATTEMPTS",
        "FOUNDRY_ROOT",
        "FOUNDRY_WORKERS",
        "FOUNDRY_VALIDATION_TIMEOUT_S",
        "SAM31_ENDPOINT",
        "SAM31_API_KEY",
        "SAM31_MODEL",
        "SAM31_TIMEOUT_S",
        "IMAGE_TO_3D_ENDPOINT",
        "IMAGE_TO_3D_API_KEY",
        "IMAGE_TO_3D_TIMEOUT_S",
    }
)

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _find_dotenv() -> Path | None:
    """Locate the .env file: explicit override, cwd, then package parents."""
    explicit = os.environ.get("HEYWALK_DOTENV", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None
    candidates = [Path.cwd() / ".env"]
    candidates.extend(
        parent / ".env" for parent in Path(__file__).resolve().parents[:6]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines; skips comments, blanks, and unknown keys."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not _KEY_RE.fullmatch(key) or key not in KNOWN_DOTENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            quote, value = value[0], value[1:-1]
            if quote == '"':
                value = value.replace('\\"', '"').replace("\\\\", "\\")
        values[key] = value
    return values


def _load_dotenv_values() -> tuple[dict[str, str], str]:
    """Return (.env fallback values, loaded path) without touching environ."""
    if os.environ.get("HEYWALK_NO_DOTENV", "").strip() == "1":
        return {}, ""
    path = _find_dotenv()
    if path is None:
        return {}, ""
    return _parse_dotenv(path), str(path)


def load_settings() -> Settings:
    dotenv, dotenv_path = _load_dotenv_values()

    def get(key: str, default: str = "") -> str:
        value = os.environ.get(key)
        if value is not None:
            return value
        return dotenv.get(key, default)

    origins = tuple(
        o.strip() for o in get("CORS_ORIGINS", "*").split(",") if o.strip()
    ) or ("*",)
    return Settings(
        db_path=get("HEYWALK_DB", "./heywalk.db"),
        model_api_key=get("MODEL_API_KEY", ""),
        meta_api_base_url=get("META_API_BASE_URL", "https://api.meta.ai/v1"),
        muse_model=get("MUSE_MODEL", "muse-spark-1.3"),
        model_timeout_s=float(get("MODEL_TIMEOUT_S", "120")),
        cors_origins=origins,
        runner_backend=get("RUNNER_BACKEND", "none").strip().lower(),
        runner_image=get("RUNNER_IMAGE", "heywalk-runner:jazzy"),
        runner_timeout_s=float(get("RUNNER_TIMEOUT_S", "180")),
        runner_cpus=float(get("RUNNER_CPUS", "1.0")),
        runner_memory=get("RUNNER_MEMORY", "1024m"),
        runner_pids_limit=int(get("RUNNER_PIDS_LIMIT", "128")),
        runner_network=get("RUNNER_NETWORK", "none"),
        max_fix_attempts=int(get("MAX_FIX_ATTEMPTS", "2")),
        # HEYWALK_PROJECTS is the pre-rename variable name; still honored so
        # existing .env files keep pointing at the same folders.
        sequence_root=get("HEYWALK_SEQUENCES", "")
        or get("HEYWALK_PROJECTS", ""),
        foundry_root=get("FOUNDRY_ROOT", ""),
        foundry_workers=int(get("FOUNDRY_WORKERS", "2")),
        foundry_validation_timeout_s=float(
            get("FOUNDRY_VALIDATION_TIMEOUT_S", "30")
        ),
        sam_endpoint=get("SAM31_ENDPOINT", ""),
        sam_api_key=get("SAM31_API_KEY", ""),
        sam_model=get("SAM31_MODEL", "sam-3.1"),
        sam_timeout_s=float(get("SAM31_TIMEOUT_S", "60")),
        image3d_endpoint=get("IMAGE_TO_3D_ENDPOINT", ""),
        image3d_api_key=get("IMAGE_TO_3D_API_KEY", ""),
        image3d_timeout_s=float(get("IMAGE_TO_3D_TIMEOUT_S", "120")),
        dotenv_path=dotenv_path,
    )
