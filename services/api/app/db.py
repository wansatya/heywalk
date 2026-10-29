"""SQLite storage for the single-user local MVP (AGENTS.md section 11).

Tables: projects, project_files, runs, run_metrics, run_events, model_requests,
plus run_logs and run_telemetry backing the logs/telemetry endpoints.

Project file *contents* live on disk under ``<sequence_root>/<project_id>/`` so
each project is isolated at the folder level and browsable; the database keeps
the file index (path, version, timestamps) plus a content mirror used to
self-heal a missing disk file (e.g. rows written before folders existed).

The on-disk folders are called **sequences** (not "projects") to keep them
distinct from the SQLite ``projects`` table and the ``/api/projects`` routes:
one folder holds the working copy of one sequence's files.

A short-lived connection is opened per operation (with a writer lock) so the
store is safe to use from FastAPI worker threads and asyncio tasks alike.
API keys are never stored here.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  task_description TEXT NOT NULL DEFAULT '',
  objective TEXT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (project_id, path)
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  parent_run_id TEXT NULL REFERENCES runs(id),
  controller_version INTEGER NOT NULL DEFAULT 0,
  backend TEXT NOT NULL DEFAULT 'differential_drive',
  status TEXT NOT NULL,
  world_config TEXT NULL,
  seed INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NULL,
  finished_at TEXT NULL,
  exit_code INTEGER NULL,
  failure_summary TEXT NULL,
  estop_active INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id);
CREATE TABLE IF NOT EXISTS run_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
  success INTEGER NOT NULL,
  collision_count INTEGER NOT NULL,
  goal_distance REAL NOT NULL,
  completion_time REAL NOT NULL,
  path_length REAL NOT NULL,
  emergency_stop_latency REAL NULL,
  evaluator_version TEXT NOT NULL,
  tests_total INTEGER NOT NULL DEFAULT 0,
  tests_passed INTEGER NOT NULL DEFAULT 0,
  max_joint_error REAL NOT NULL DEFAULT 0,
  total_contacts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS run_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  timestamp TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id);
CREATE TABLE IF NOT EXISTS run_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  timestamp TEXT NOT NULL,
  line TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_logs_run ON run_logs(run_id);
CREATE TABLE IF NOT EXISTS run_telemetry (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  t REAL NOT NULL,
  x REAL NOT NULL,
  y REAL NOT NULL,
  theta REAL NOT NULL,
  v REAL NOT NULL,
  omega REAL NOT NULL,
  collision INTEGER NOT NULL DEFAULT 0,
  goal_reached INTEGER NOT NULL DEFAULT 0,
  estop_active INTEGER NOT NULL DEFAULT 0,
  min_range REAL NULL,
  joints TEXT NULL,
  contacts INTEGER NOT NULL DEFAULT 0,
  max_contact_force REAL NOT NULL DEFAULT 0,
  test_id TEXT NULL,
  fallen INTEGER NOT NULL DEFAULT 0,
  trunk_z REAL NULL,
  expected_contacts INTEGER NOT NULL DEFAULT 0,
  sensors TEXT NULL,
  bodies TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_telemetry_run ON run_telemetry(run_id);
CREATE TABLE IF NOT EXISTS run_tests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  case_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  joint TEXT NOT NULL,
  target REAL NOT NULL,
  tolerance REAL NOT NULL,
  final_position REAL NOT NULL,
  final_error REAL NOT NULL,
  max_error REAL NOT NULL,
  max_velocity REAL NOT NULL,
  max_effort REAL NOT NULL DEFAULT 0,
  settling_time REAL NULL,
  duration REAL NOT NULL DEFAULT 0,
  contacts INTEGER NOT NULL DEFAULT 0,
  max_contact_force REAL NOT NULL DEFAULT 0,
  passed INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  estop_latency REAL NULL
);
CREATE INDEX IF NOT EXISTS idx_run_tests_run ON run_tests(run_id);
CREATE TABLE IF NOT EXISTS model_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id TEXT NULL REFERENCES runs(id) ON DELETE SET NULL,
  purpose TEXT NOT NULL,
  model_name TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  status TEXT NOT NULL,
  latency_ms INTEGER NULL,
  error_summary TEXT NULL,
  created_at TEXT NOT NULL
);
"""


class Store:
    def __init__(
        self, db_path: str | Path, sequence_root: str | Path | None = None
    ) -> None:
        self.db_path = str(db_path)
        self._write_lock = threading.Lock()
        if self.db_path != ":memory:":
            parent = Path(self.db_path).expanduser().parent
            parent.mkdir(parents=True, exist_ok=True)
        if sequence_root is not None:
            root = Path(sequence_root).expanduser()
        elif self.db_path == ":memory:":
            root = Path(tempfile.mkdtemp(prefix="heywalk-sequences-"))
        else:
            root = Path(self.db_path).expanduser().parent / "sequences"
        root.mkdir(parents=True, exist_ok=True)
        self.sequence_root = root
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Additive migrations for databases created by earlier versions.

        Existing experiments must stay readable when a new backend arrives:
        columns are added with defaults, never dropped or rewritten.
        """
        additions: dict[str, dict[str, str]] = {
            "projects": {
                "objective": "TEXT NULL",
            },
            "runs": {
                "backend": "TEXT NOT NULL DEFAULT 'differential_drive'",
            },
            "run_metrics": {
                "tests_total": "INTEGER NOT NULL DEFAULT 0",
                "tests_passed": "INTEGER NOT NULL DEFAULT 0",
                "max_joint_error": "REAL NOT NULL DEFAULT 0",
                "total_contacts": "INTEGER NOT NULL DEFAULT 0",
            },
            "run_telemetry": {
                "joints": "TEXT NULL",
                "contacts": "INTEGER NOT NULL DEFAULT 0",
                "max_contact_force": "REAL NOT NULL DEFAULT 0",
                "test_id": "TEXT NULL",
                "fallen": "INTEGER NOT NULL DEFAULT 0",
                "trunk_z": "REAL NULL",
                "expected_contacts": "INTEGER NOT NULL DEFAULT 0",
                "sensors": "TEXT NULL",
                "bodies": "TEXT NULL",
            },
        }
        for table, columns in additions.items():
            existing = {
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, ddl in columns.items():
                if name in existing:
                    continue
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def project_dir(self, project_id: str) -> Path:
        """Return the project's isolated folder, creating it on demand.

        Raises ValueError when the id is not a plain folder-safe token, so a
        crafted project id can never escape the project root.
        """
        if not isinstance(project_id, str) or not PROJECT_ID_PATTERN.fullmatch(
            project_id
        ):
            raise ValueError(f"Invalid project id: {project_id!r}.")
        directory = self.sequence_root / project_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _disk_path(self, project_id: str, path: str) -> Path:
        """Resolve a validated relative path inside the project's folder."""
        if not isinstance(path, str) or not path or path.startswith("/"):
            raise ValueError(f"Invalid file path: {path!r}.")
        target = (self.project_dir(project_id) / path).resolve()
        root = self.sequence_root.resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"File path escapes the project folder: {path!r}.")
        project_dir = self.project_dir(project_id).resolve()
        if target != project_dir and project_dir not in target.parents:
            raise ValueError(f"File path escapes the project folder: {path!r}.")
        return target

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        # WAL + NORMAL: a live run commits one telemetry row per 50 ms tick,
        # and the default synchronous=FULL fsyncs each commit (measured 3.6 ms
        # median, 37 ms max per sample, on the API event loop). The runner's
        # result.json stays authoritative for a finished run; an OS crash can
        # lose the newest commits but cannot corrupt the database.
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ---- projects ----

    def create_project(self, name: str, task_description: str) -> dict[str, Any]:
        """Create a project and its on-disk folder (removed on delete)."""
        now = utcnow_iso()
        project_id = new_id()
        # Folder first: a disk failure must prevent the row, not strand it.
        self.project_dir(project_id)
        try:
            with self._write_lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO projects (id, name, task_description, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (project_id, name, task_description, now, now),
                )
        except Exception:
            self._remove_project_dir(project_id)
            raise
        return self.get_project(project_id)  # type: ignore[return-value]

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return dict(row) if row else None

    def touch_project(self, project_id: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (utcnow_iso(), project_id),
            )

    def set_project_objectives(
        self, project_id: str, objectives: list[dict[str, Any]] | None
    ) -> None:
        """Persist the grading phases for the current controller.

        Set by generation (declared from the task text) and cleared by
        sample import (the sample is the default flag behavior). Runs
        without explicit phases grade these instead of assuming.
        """
        raw = json.dumps(objectives) if objectives is not None else None
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE projects SET objective = ?, updated_at = ? WHERE id = ?",
                (raw, utcnow_iso(), project_id),
            )

    def get_project_objectives(
        self, project_id: str
    ) -> list[dict[str, Any]] | None:
        """Parse the stored phases (None = default flag behavior).

        A legacy single dict reads as a one-phase sequence.
        """
        project = self.get_project(project_id)
        if project is None:
            return None
        raw = project.get("objective")
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if isinstance(parsed, dict):
            return [parsed]
        return parsed if isinstance(parsed, list) else None

    def delete_project(self, project_id: str) -> bool:
        """Delete a project, its files, runs, and evidence (cascades).

        Returns False when the project does not exist. Also removes the
        project's on-disk folder; callers must refuse while a run is
        active (a running harness holds the workspace open).
        """
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "DELETE FROM projects WHERE id = ?", (project_id,)
            )
        self._remove_project_dir(project_id)
        return True

    def _remove_project_dir(self, project_id: str) -> None:
        if not isinstance(project_id, str) or not PROJECT_ID_PATTERN.fullmatch(
            project_id
        ):
            return
        target = (self.sequence_root / project_id).resolve()
        root = self.sequence_root.resolve()
        if target == root or root not in target.parents:
            return
        shutil.rmtree(target, ignore_errors=True)

    # ---- project files ----

    def list_files(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT path, version, updated_at FROM project_files"
                " WHERE project_id = ? ORDER BY path ASC",
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_files(self, project_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM project_files WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return int(row["n"])

    def get_file(self, project_id: str, path: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT path, content, version, updated_at FROM project_files"
                " WHERE project_id = ? AND path = ?",
                (project_id, path),
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        try:
            disk = self._disk_path(project_id, path)
        except ValueError:
            return record
        try:
            record["content"] = disk.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._restore_disk_file(project_id, path, record["content"])
        except (OSError, UnicodeDecodeError):
            pass
        return record

    def _restore_disk_file(
        self, project_id: str, path: str, content: str
    ) -> None:
        """Rewrite a missing disk file from the database mirror (best effort)."""
        try:
            disk = self._disk_path(project_id, path)
            disk.parent.mkdir(parents=True, exist_ok=True)
            disk.write_text(content, encoding="utf-8")
        except (OSError, ValueError):
            pass

    def save_file(self, project_id: str, path: str, content: str) -> dict[str, Any]:
        now = utcnow_iso()
        with self._write_lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT version FROM project_files WHERE project_id = ? AND path = ?",
                (project_id, path),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO project_files (project_id, path, content, version,"
                    " created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                    (project_id, path, content, now, now),
                )
            else:
                conn.execute(
                    "UPDATE project_files SET content = ?, version = version + 1,"
                    " updated_at = ? WHERE project_id = ? AND path = ?",
                    (content, now, project_id, path),
                )
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id)
            )
            disk = self._disk_path(project_id, path)
            disk.parent.mkdir(parents=True, exist_ok=True)
            disk.write_text(content, encoding="utf-8")
        return self.get_file(project_id, path)  # type: ignore[return-value]

    def delete_file(self, project_id: str, path: str) -> bool:
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM project_files WHERE project_id = ? AND path = ?",
                (project_id, path),
            )
            deleted = cursor.rowcount > 0
        if deleted:
            try:
                disk = self._disk_path(project_id, path)
                disk.unlink(missing_ok=True)
                parent = disk.parent
                stop = self.project_dir(project_id).resolve()
                while parent.resolve() != stop and stop in parent.resolve().parents:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
            except ValueError:
                pass
        return deleted

    def browse_files(self, project_id: str) -> list[dict[str, Any]]:
        """List the project's folder tree (dirs + files with sizes/versions)."""
        versions = {entry["path"]: entry["version"] for entry in self.list_files(project_id)}
        for entry in self.list_files(project_id):
            disk = self.project_dir(project_id) / entry["path"]
            if not disk.is_file():
                row = self.get_file(project_id, entry["path"])
                if row is not None:
                    versions[entry["path"]] = row["version"]
        base = self.project_dir(project_id)
        seen_dirs: set[str] = set()
        entries: list[dict[str, Any]] = []
        if base.is_dir():
            for item in sorted(base.rglob("*")):
                if item.is_symlink():
                    continue
                rel = item.relative_to(base).as_posix()
                if item.is_dir():
                    seen_dirs.add(rel)
                    entries.append({"type": "dir", "path": rel})
                elif item.is_file():
                    try:
                        size = item.stat().st_size
                    except OSError:
                        size = 0
                    entries.append(
                        {
                            "type": "file",
                            "path": rel,
                            "size": size,
                            "version": versions.get(rel, 0),
                        }
                    )
        for rel in sorted(seen_dirs):
            parts = rel.split("/")
            for depth in range(1, len(parts)):
                ancestor = "/".join(parts[:depth])
                if ancestor not in seen_dirs:
                    seen_dirs.add(ancestor)
                    entries.append({"type": "dir", "path": ancestor})
        entries.sort(key=lambda entry: (entry["path"], entry["type"]))
        return entries

    def max_file_version(self, project_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM project_files WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return int(row["v"] or 0)

    # ---- runs ----

    def create_run(
        self,
        project_id: str,
        *,
        parent_run_id: str | None,
        controller_version: int,
        status: str,
        world_config: str | None,
        seed: int,
        backend: str = "differential_drive",
    ) -> dict[str, Any]:
        run_id = new_id()
        now = utcnow_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (id, project_id, parent_run_id, controller_version,"
                " backend, status, world_config, seed, started_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    project_id,
                    parent_run_id,
                    controller_version,
                    backend,
                    status,
                    world_config,
                    seed,
                    now,
                ),
            )
        return self.get_run(run_id)  # type: ignore[return-value]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY rowid DESC",
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def previous_run(
        self, project_id: str, before_rowid: int
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT *, rowid FROM runs WHERE project_id = ? AND rowid < ?"
                " ORDER BY rowid DESC LIMIT 1",
                (project_id, before_rowid),
            ).fetchone()
        return dict(row) if row else None

    def run_rowid(self, run_id: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT rowid FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        return int(row["rowid"]) if row else None

    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get_run(run_id)
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self._write_lock, self._connect() as conn:
            conn.execute(
                f"UPDATE runs SET {columns} WHERE id = ?",
                (*fields.values(), run_id),
            )
        return self.get_run(run_id)

    # ---- metrics / events / logs / telemetry ----

    def save_metrics(self, run_id: str, metrics: dict[str, Any]) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO run_metrics (run_id, success, collision_count,"
                " goal_distance, completion_time, path_length, emergency_stop_latency,"
                " evaluator_version, tests_total, tests_passed, max_joint_error,"
                " total_contacts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    1 if metrics["success"] else 0,
                    metrics["collision_count"],
                    metrics["goal_distance"],
                    metrics["completion_time"],
                    metrics["path_length"],
                    metrics.get("emergency_stop_latency"),
                    metrics["evaluator_version"],
                    int(metrics.get("tests_total", 0) or 0),
                    int(metrics.get("tests_passed", 0) or 0),
                    float(metrics.get("max_joint_error", 0.0) or 0.0),
                    int(metrics.get("total_contacts", 0) or 0),
                ),
            )

    def get_metrics(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT success, collision_count, goal_distance, completion_time,"
                " path_length, emergency_stop_latency, evaluator_version,"
                " tests_total, tests_passed, max_joint_error, total_contacts"
                " FROM run_metrics WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["success"] = bool(data["success"])
        return data

    def add_event(
        self, run_id: str, event_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        timestamp = utcnow_iso()
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO run_events (run_id, timestamp, event_type, payload)"
                " VALUES (?, ?, ?, ?)",
                (run_id, timestamp, event_type, json.dumps(payload)),
            )
            event_id = cursor.lastrowid
        return {
            "id": event_id,
            "run_id": run_id,
            "timestamp": timestamp,
            "event_type": event_type,
            "payload": payload,
        }

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, timestamp, event_type, payload FROM run_events"
                " WHERE run_id = ? ORDER BY id ASC",
                (run_id,),
            ).fetchall()
        events = []
        for row in rows:
            data = dict(row)
            try:
                data["payload"] = json.loads(data["payload"])
            except (ValueError, TypeError):
                data["payload"] = {}
            events.append(data)
        return events

    def latest_evaluation(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM run_events WHERE run_id = ? AND event_type = ?"
                " ORDER BY id DESC LIMIT 1",
                (run_id, "evaluation"),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["payload"])
        except (ValueError, TypeError):
            return None

    def add_log(self, run_id: str, line: str) -> int:
        timestamp = utcnow_iso()
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS seq FROM run_logs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            seq = int(row["seq"])
            conn.execute(
                "INSERT INTO run_logs (run_id, seq, timestamp, line)"
                " VALUES (?, ?, ?, ?)",
                (run_id, seq, timestamp, line),
            )
        return seq

    def list_logs(self, run_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT line FROM run_logs WHERE run_id = ? ORDER BY seq ASC",
                (run_id,),
            ).fetchall()
        return [str(r["line"]) for r in rows]

    def record_telemetry(
        self, run_id: str, sample: dict[str, Any]
    ) -> dict[str, Any]:
        """Store a telemetry sample AND its stream event in one transaction.

        The live path used to open two connections and commit twice per
        sample (add_telemetry + add_event), which blocked the API event loop
        long enough to jitter the WebSocket stream.
        """
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS seq FROM run_telemetry"
                " WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            seq = int(row["seq"])
            joints = sample.get("joints")
            sensors = sample.get("sensors")
            bodies = sample.get("bodies")
            conn.execute(
                "INSERT INTO run_telemetry (run_id, seq, t, x, y, theta, v, omega,"
                " collision, goal_reached, estop_active, min_range, joints,"
                " contacts, max_contact_force, test_id, fallen, trunk_z,"
                " expected_contacts, sensors, bodies)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?, ?)",
                (
                    run_id,
                    seq,
                    sample["t"],
                    sample["x"],
                    sample["y"],
                    sample["theta"],
                    sample["v"],
                    sample["omega"],
                    1 if sample.get("collision") else 0,
                    1 if sample.get("goal_reached") else 0,
                    1 if sample.get("estop_active") else 0,
                    sample.get("min_range"),
                    json.dumps(joints) if joints else None,
                    int(sample.get("contacts", 0) or 0),
                    float(sample.get("max_contact_force", 0.0) or 0.0),
                    sample.get("test_id"),
                    1 if sample.get("fallen") else 0,
                    sample.get("trunk_z"),
                    int(sample.get("expected_contacts", 0) or 0),
                    json.dumps(sensors) if sensors else None,
                    json.dumps(bodies) if bodies else None,
                ),
            )
            payload = {"sample": sample}
            timestamp = utcnow_iso()
            cursor = conn.execute(
                "INSERT INTO run_events (run_id, timestamp, event_type, payload)"
                " VALUES (?, ?, ?, ?)",
                (run_id, timestamp, "telemetry", json.dumps(payload)),
            )
        return {
            "id": cursor.lastrowid,
            "run_id": run_id,
            "timestamp": timestamp,
            "event_type": "telemetry",
            "payload": payload,
        }

    def list_telemetry(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT t, x, y, theta, v, omega, collision, goal_reached,"
                " estop_active, min_range, joints, contacts, max_contact_force,"
                " test_id, fallen, trunk_z, expected_contacts, sensors, bodies"
                " FROM run_telemetry"
                " WHERE run_id = ? ORDER BY seq ASC",
                (run_id,),
            ).fetchall()
        samples = []
        for row in rows:
            data = dict(row)
            data["collision"] = bool(data["collision"])
            data["goal_reached"] = bool(data["goal_reached"])
            data["estop_active"] = bool(data["estop_active"])
            data["fallen"] = bool(data["fallen"])
            raw_joints = data.pop("joints", None)
            if raw_joints:
                try:
                    parsed = json.loads(raw_joints)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, list):
                    data["joints"] = parsed
            for key, kind in (("sensors", dict), ("bodies", list)):
                raw = data.pop(key, None)
                if raw:
                    try:
                        parsed = json.loads(raw)
                    except (ValueError, TypeError):
                        parsed = None
                    if isinstance(parsed, kind):
                        data[key] = parsed
            samples.append(data)
        return samples

    # ---- automated test evidence ----

    def save_test_results(
        self, run_id: str, tests: list[dict[str, Any]]
    ) -> None:
        """Replace the recorded automated-test results for a run."""
        with self._write_lock, self._connect() as conn:
            conn.execute("DELETE FROM run_tests WHERE run_id = ?", (run_id,))
            for seq, test in enumerate(tests):
                conn.execute(
                    "INSERT INTO run_tests (run_id, seq, case_id, kind, joint,"
                    " target, tolerance, final_position, final_error, max_error,"
                    " max_velocity, max_effort, settling_time, duration, contacts,"
                    " max_contact_force, passed, reason, estop_latency)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        seq,
                        str(test.get("id", f"test_{seq}")),
                        str(test.get("kind", "joint_move")),
                        str(test.get("joint", "")),
                        float(test.get("target", 0.0)),
                        float(test.get("tolerance", 0.0)),
                        float(test.get("final_position", 0.0)),
                        float(test.get("final_error", 0.0)),
                        float(test.get("max_error", 0.0)),
                        float(test.get("max_velocity", 0.0)),
                        float(test.get("max_effort", 0.0)),
                        test.get("settling_time"),
                        float(test.get("duration", 0.0)),
                        int(test.get("contacts", 0) or 0),
                        float(test.get("max_contact_force", 0.0) or 0.0),
                        1 if test.get("passed") else 0,
                        str(test.get("reason", ""))[:1000],
                        test.get("estop_latency"),
                    ),
                )

    def list_test_results(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT case_id, kind, joint, target, tolerance, final_position,"
                " final_error, max_error, max_velocity, max_effort, settling_time,"
                " duration, contacts, max_contact_force, passed, reason,"
                " estop_latency FROM run_tests WHERE run_id = ? ORDER BY seq ASC",
                (run_id,),
            ).fetchall()
        tests = []
        for row in rows:
            data = dict(row)
            data["id"] = data.pop("case_id")
            data["passed"] = bool(data["passed"])
            tests.append(data)
        return tests

    # ---- model requests ----

    def count_model_requests(
        self,
        project_id: str,
        purpose: str,
        *,
        run_id: str | None = None,
    ) -> int:
        """Count recorded model requests (drives the fix-attempt cap)."""
        with self._connect() as conn:
            if run_id is None:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM model_requests"
                    " WHERE project_id = ? AND purpose = ?",
                    (project_id, purpose),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM model_requests"
                    " WHERE project_id = ? AND purpose = ? AND run_id = ?",
                    (project_id, purpose, run_id),
                ).fetchone()
        return int(row["n"])

    def record_model_request(
        self,
        project_id: str,
        purpose: str,
        model_name: str,
        prompt_version: str,
        status: str,
        *,
        run_id: str | None = None,
        latency_ms: int | None = None,
        error_summary: str | None = None,
    ) -> int:
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO model_requests (project_id, run_id, purpose, model_name,"
                " prompt_version, status, latency_ms, error_summary, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    run_id,
                    purpose,
                    model_name,
                    prompt_version,
                    status,
                    latency_ms,
                    error_summary,
                    utcnow_iso(),
                ),
            )
            return int(cursor.lastrowid or 0)

    def list_model_requests(
        self, project_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, project_id, run_id, purpose, model_name,"
                " prompt_version, status, latency_ms, error_summary,"
                " created_at FROM model_requests WHERE project_id = ?"
                " ORDER BY id DESC LIMIT ?",
                (project_id, max(1, min(limit, 100))),
            ).fetchall()
        return [dict(r) for r in rows]
