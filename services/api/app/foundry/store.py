"""Persistence for Robot Foundry robot builds.

Each Foundry project is one robot package folder in the simulator robot
library (``<foundry_root>/<foundry_id>/``). It is deliberately NOT a sequence:
creating a robot must not create a project under ``sequences/``. When the user
opens a generated robot in the simulator, the normal sequence/install path
copies the package into a sequence exactly like a bundled robot.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..db import PROJECT_ID_PATTERN, new_id, utcnow_iso


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS foundry_projects (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT 'biped',
  status TEXT NOT NULL DEFAULT 'DRAFT',
  thumbnail_url TEXT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  error_message TEXT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_foundry_projects_project
  ON foundry_projects(project_id);
CREATE TABLE IF NOT EXISTS foundry_images (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES foundry_projects(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  file_url TEXT NOT NULL,
  image_type TEXT NOT NULL DEFAULT 'reference',
  analysis_status TEXT NOT NULL DEFAULT 'pending',
  width INTEGER NULL,
  height INTEGER NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_foundry_images_project
  ON foundry_images(project_id);
CREATE TABLE IF NOT EXISTS foundry_components (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES foundry_projects(id) ON DELETE CASCADE,
  parent_id TEXT NULL,
  name TEXT NOT NULL,
  label TEXT NOT NULL,
  component_type TEXT NOT NULL DEFAULT 'other',
  mesh_url TEXT NULL,
  collision_mesh_url TEXT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  confidence REAL NOT NULL DEFAULT 0.5,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_foundry_components_project
  ON foundry_components(project_id);
CREATE TABLE IF NOT EXISTS foundry_joints (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES foundry_projects(id) ON DELETE CASCADE,
  component_id TEXT NOT NULL,
  parent_component_id TEXT NULL,
  name TEXT NOT NULL,
  joint_type TEXT NOT NULL DEFAULT 'fixed',
  axis TEXT NOT NULL DEFAULT '[0,0,1]',
  position TEXT NOT NULL DEFAULT '[0,0,0]',
  limits TEXT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_foundry_joints_project
  ON foundry_joints(project_id);
CREATE TABLE IF NOT EXISTS foundry_actuators (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES foundry_projects(id) ON DELETE CASCADE,
  joint_id TEXT NOT NULL,
  name TEXT NOT NULL,
  actuator_type TEXT NOT NULL DEFAULT 'position',
  configuration_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_foundry_actuators_project
  ON foundry_actuators(project_id);
CREATE TABLE IF NOT EXISTS foundry_jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES foundry_projects(id) ON DELETE CASCADE,
  job_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  progress INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  error_message TEXT NULL,
  result_json TEXT NULL,
  created_at TEXT NOT NULL,
  completed_at TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_foundry_jobs_project
  ON foundry_jobs(project_id);
CREATE TABLE IF NOT EXISTS robot_versions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES foundry_projects(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  mjcf_url TEXT NULL,
  manifest_url TEXT NULL,
  validation_json TEXT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_robot_versions_project
  ON robot_versions(project_id);
"""

#: Characters kept in user-facing slugs used for file and MJCF names.
_SLUG_PATTERN = re.compile(r"[^A-Za-z0-9_]+")


def slug(value: str, fallback: str = "part") -> str:
    """Return a safe identifier for MJCF body/mesh names and file paths."""
    cleaned = _SLUG_PATTERN.sub("_", (value or "").strip()).strip("_").lower()
    if not cleaned:
        cleaned = fallback
    if cleaned[0].isdigit():
        cleaned = f"p_{cleaned}"
    return cleaned[:48]


def _json_loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


class FoundryStore:
    """Short-lived-connection store for Foundry projects and artifacts."""

    def __init__(self, db_path: str | Path, root: str | Path) -> None:
        self.db_path = str(db_path)
        self._write_lock = threading.Lock()
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ---- folders ----

    def project_dir(self, project_id: str) -> Path:
        """Return the isolated artifact folder for a Foundry project."""
        if not isinstance(project_id, str) or not PROJECT_ID_PATTERN.fullmatch(
            project_id
        ):
            raise ValueError(f"Invalid Foundry project id: {project_id!r}.")
        directory = self.root / project_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def resolve_asset(self, project_id: str, relative: str) -> Path:
        """Resolve a relative artifact path, refusing traversal."""
        if not isinstance(relative, str) or not relative or relative.startswith("/"):
            raise ValueError(f"Invalid asset path: {relative!r}.")
        base = self.project_dir(project_id).resolve()
        target = (base / relative).resolve()
        if target != base and base not in target.parents:
            raise ValueError(f"Asset path escapes the project folder: {relative!r}.")
        return target

    def remove_project_dir(self, project_id: str) -> None:
        if not isinstance(project_id, str) or not PROJECT_ID_PATTERN.fullmatch(
            project_id
        ):
            return
        target = (self.root / project_id).resolve()
        root = self.root.resolve()
        if target == root or root not in target.parents:
            return
        shutil.rmtree(target, ignore_errors=True)

    # ---- projects ----

    def create_project(
        self,
        *,
        name: str,
        description: str = "",
        category: str = "biped",
        metadata: dict[str, Any] | None = None,
        project_id: str = "",
    ) -> dict[str, Any]:
        foundry_id = new_id()
        now = utcnow_iso()
        self.project_dir(foundry_id)
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO foundry_projects (id, project_id, name, description,"
                " category, status, metadata_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?)",
                (
                    foundry_id,
                    project_id,
                    name,
                    description,
                    category,
                    json.dumps(metadata or {}),
                    now,
                    now,
                ),
            )
        return self.get_project(foundry_id)  # type: ignore[return-value]

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM foundry_projects ORDER BY created_at DESC"
            ).fetchall()
        return [self._project_row(dict(row)) for row in rows]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM foundry_projects WHERE id = ?", (project_id,)
            ).fetchone()
        return self._project_row(dict(row)) if row else None

    def get_by_sequence(self, sequence_project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM foundry_projects WHERE project_id = ?"
                " ORDER BY created_at DESC LIMIT 1",
                (sequence_project_id,),
            ).fetchone()
        return self._project_row(dict(row)) if row else None

    def update_project(self, project_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "name",
            "description",
            "category",
            "status",
            "thumbnail_url",
            "metadata_json",
            "error_message",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_project(project_id)
        if "metadata_json" in updates and not isinstance(
            updates["metadata_json"], str
        ):
            updates["metadata_json"] = json.dumps(updates["metadata_json"])
        updates["updated_at"] = utcnow_iso()
        columns = ", ".join(f"{key} = ?" for key in updates)
        with self._write_lock, self._connect() as conn:
            conn.execute(
                f"UPDATE foundry_projects SET {columns} WHERE id = ?",
                (*updates.values(), project_id),
            )
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> bool:
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM foundry_projects WHERE id = ?", (project_id,)
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM foundry_projects WHERE id = ?", (project_id,))
        self.remove_project_dir(project_id)
        return True

    @staticmethod
    def _project_row(row: dict[str, Any]) -> dict[str, Any]:
        row["metadata"] = _json_loads(row.pop("metadata_json", None), {})
        return row

    # ---- images ----

    def add_image(
        self,
        project_id: str,
        *,
        filename: str,
        file_url: str,
        image_type: str,
        width: int,
        height: int,
        analysis_status: str = "pending",
    ) -> dict[str, Any]:
        image_id = new_id()
        now = utcnow_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO foundry_images (id, project_id, filename, file_url,"
                " image_type, analysis_status, width, height, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    image_id,
                    project_id,
                    filename,
                    file_url,
                    image_type,
                    analysis_status,
                    width,
                    height,
                    now,
                ),
            )
        return self.get_image(image_id)  # type: ignore[return-value]

    def list_images(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM foundry_images WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_image(self, image_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM foundry_images WHERE id = ?", (image_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_image(self, image_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"analysis_status", "width", "height", "file_url", "filename"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if updates:
            columns = ", ".join(f"{key} = ?" for key in updates)
            with self._write_lock, self._connect() as conn:
                conn.execute(
                    f"UPDATE foundry_images SET {columns} WHERE id = ?",
                    (*updates.values(), image_id),
                )
        return self.get_image(image_id)

    # ---- components ----

    def create_component(
        self,
        project_id: str,
        *,
        name: str,
        label: str,
        component_type: str = "other",
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        confidence: float = 0.5,
    ) -> dict[str, Any]:
        """Add one manually-created component without replacing the set."""
        component_id = new_id()
        now = utcnow_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO foundry_components (id, project_id, parent_id, name,"
                " label, component_type, mesh_url, collision_mesh_url,"
                " metadata_json, confidence, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
                (
                    component_id,
                    project_id,
                    parent_id,
                    name,
                    label,
                    component_type,
                    json.dumps(metadata or {}),
                    float(confidence),
                    now,
                    now,
                ),
            )
        return self.get_component(component_id)  # type: ignore[return-value]

    def replace_components(
        self, project_id: str, components: list[dict[str, Any]]
    ) -> None:
        """Replace the detected component set and any joints/actuators."""
        now = utcnow_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM foundry_actuators WHERE project_id = ?", (project_id,)
            )
            conn.execute(
                "DELETE FROM foundry_joints WHERE project_id = ?", (project_id,)
            )
            conn.execute(
                "DELETE FROM foundry_components WHERE project_id = ?", (project_id,)
            )
            for component in components:
                conn.execute(
                    "INSERT INTO foundry_components (id, project_id, parent_id,"
                    " name, label, component_type, mesh_url, collision_mesh_url,"
                    " metadata_json, confidence, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        component["id"],
                        project_id,
                        component.get("parent_id"),
                        component["name"],
                        component["label"],
                        component.get("component_type", "other"),
                        component.get("mesh_url"),
                        component.get("collision_mesh_url"),
                        json.dumps(component.get("metadata", {})),
                        float(component.get("confidence", 0.5)),
                        now,
                        now,
                    ),
                )
        self.update_project(project_id, status="NEEDS_REVIEW")

    def list_components(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM foundry_components WHERE project_id = ?"
                " ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
        return [self._component_row(dict(row)) for row in rows]

    def get_component(self, component_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM foundry_components WHERE id = ?", (component_id,)
            ).fetchone()
        return self._component_row(dict(row)) if row else None

    def update_component(
        self, component_id: str, **fields: Any
    ) -> dict[str, Any] | None:
        allowed = {
            "parent_id",
            "name",
            "label",
            "component_type",
            "mesh_url",
            "collision_mesh_url",
            "metadata_json",
            "confidence",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_component(component_id)
        if "metadata_json" in updates and not isinstance(
            updates["metadata_json"], str
        ):
            updates["metadata_json"] = json.dumps(updates["metadata_json"])
        updates["updated_at"] = utcnow_iso()
        columns = ", ".join(f"{key} = ?" for key in updates)
        with self._write_lock, self._connect() as conn:
            conn.execute(
                f"UPDATE foundry_components SET {columns} WHERE id = ?",
                (*updates.values(), component_id),
            )
        return self.get_component(component_id)

    def delete_component(self, component_id: str) -> bool:
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT project_id FROM foundry_components WHERE id = ?",
                (component_id,),
            ).fetchone()
            if row is None:
                return False
            project_id = row["project_id"]
            joint_rows = conn.execute(
                "SELECT id FROM foundry_joints WHERE component_id = ?"
                " OR parent_component_id = ?",
                (component_id, component_id),
            ).fetchall()
            for joint in joint_rows:
                conn.execute(
                    "DELETE FROM foundry_actuators WHERE joint_id = ?", (joint["id"],)
                )
            conn.execute(
                "DELETE FROM foundry_joints WHERE component_id = ?"
                " OR parent_component_id = ?",
                (component_id, component_id),
            )
            conn.execute(
                "UPDATE foundry_components SET parent_id = NULL WHERE parent_id = ?",
                (component_id,),
            )
            conn.execute(
                "DELETE FROM foundry_components WHERE id = ?", (component_id,)
            )
            conn.execute(
                "UPDATE foundry_projects SET updated_at = ? WHERE id = ?",
                (utcnow_iso(), project_id),
            )
        return True

    @staticmethod
    def _component_row(row: dict[str, Any]) -> dict[str, Any]:
        row["metadata"] = _json_loads(row.pop("metadata_json", None), {})
        return row

    # ---- joints ----

    def replace_joints(
        self, project_id: str, joints: list[dict[str, Any]]
    ) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM foundry_actuators WHERE project_id = ?", (project_id,)
            )
            conn.execute(
                "DELETE FROM foundry_joints WHERE project_id = ?", (project_id,)
            )
            for joint in joints:
                conn.execute(
                    "INSERT INTO foundry_joints (id, project_id, component_id,"
                    " parent_component_id, name, joint_type, axis, position,"
                    " limits, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        joint["id"],
                        project_id,
                        joint["component_id"],
                        joint.get("parent_component_id"),
                        joint["name"],
                        joint.get("joint_type", "fixed"),
                        json.dumps(joint.get("axis", [0, 0, 1])),
                        json.dumps(joint.get("position", [0, 0, 0])),
                        json.dumps(joint["limits"])
                        if joint.get("limits") is not None
                        else None,
                        json.dumps(joint.get("metadata", {})),
                    ),
                )

    def list_joints(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM foundry_joints WHERE project_id = ? ORDER BY rowid",
                (project_id,),
            ).fetchall()
        return [self._joint_row(dict(row)) for row in rows]

    def get_joint(self, joint_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM foundry_joints WHERE id = ?", (joint_id,)
            ).fetchone()
        return self._joint_row(dict(row)) if row else None

    def update_joint(self, joint_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "component_id",
            "parent_component_id",
            "name",
            "joint_type",
            "axis",
            "position",
            "limits",
            "metadata_json",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_joint(joint_id)
        for key in ("axis", "position", "limits"):
            if key in updates and updates[key] is not None and not isinstance(
                updates[key], str
            ):
                updates[key] = json.dumps(updates[key])
        if "metadata_json" in updates and not isinstance(
            updates["metadata_json"], str
        ):
            updates["metadata_json"] = json.dumps(updates["metadata_json"])
        columns = ", ".join(f"{key} = ?" for key in updates)
        with self._write_lock, self._connect() as conn:
            conn.execute(
                f"UPDATE foundry_joints SET {columns} WHERE id = ?",
                (*updates.values(), joint_id),
            )
        return self.get_joint(joint_id)

    @staticmethod
    def _joint_row(row: dict[str, Any]) -> dict[str, Any]:
        row["axis"] = _json_loads(row.get("axis"), [0, 0, 1])
        row["position"] = _json_loads(row.get("position"), [0, 0, 0])
        row["limits"] = _json_loads(row.get("limits"), None)
        row["metadata"] = _json_loads(row.pop("metadata_json", None), {})
        return row

    # ---- actuators ----

    def replace_actuators(
        self, project_id: str, actuators: list[dict[str, Any]]
    ) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM foundry_actuators WHERE project_id = ?", (project_id,)
            )
            for actuator in actuators:
                conn.execute(
                    "INSERT INTO foundry_actuators (id, project_id, joint_id, name,"
                    " actuator_type, configuration_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        actuator["id"],
                        project_id,
                        actuator["joint_id"],
                        actuator["name"],
                        actuator.get("actuator_type", "position"),
                        json.dumps(actuator.get("configuration", {})),
                    ),
                )

    def list_actuators(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM foundry_actuators WHERE project_id = ? ORDER BY rowid",
                (project_id,),
            ).fetchall()
        return [self._actuator_row(dict(row)) for row in rows]

    def get_actuator(self, actuator_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM foundry_actuators WHERE id = ?", (actuator_id,)
            ).fetchone()
        return self._actuator_row(dict(row)) if row else None

    def update_actuator(
        self, actuator_id: str, **fields: Any
    ) -> dict[str, Any] | None:
        allowed = {"joint_id", "name", "actuator_type", "configuration_json"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_actuator(actuator_id)
        if "configuration_json" in updates and not isinstance(
            updates["configuration_json"], str
        ):
            updates["configuration_json"] = json.dumps(updates["configuration_json"])
        columns = ", ".join(f"{key} = ?" for key in updates)
        with self._write_lock, self._connect() as conn:
            conn.execute(
                f"UPDATE foundry_actuators SET {columns} WHERE id = ?",
                (*updates.values(), actuator_id),
            )
        return self.get_actuator(actuator_id)

    @staticmethod
    def _actuator_row(row: dict[str, Any]) -> dict[str, Any]:
        row["configuration"] = _json_loads(
            row.pop("configuration_json", None), {}
        )
        return row

    # ---- jobs ----

    def create_job(self, project_id: str, job_type: str) -> dict[str, Any]:
        job_id = new_id()
        now = utcnow_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO foundry_jobs (id, project_id, job_type, status,"
                " progress, message, created_at) VALUES (?, ?, ?, 'queued', 0, '', ?)",
                (job_id, project_id, job_type, now),
            )
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM foundry_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._job_row(dict(row)) if row else None

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "status",
            "progress",
            "message",
            "error_message",
            "result_json",
            "completed_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if "result_json" in updates and updates["result_json"] is not None and not isinstance(
            updates["result_json"], str
        ):
            updates["result_json"] = json.dumps(updates["result_json"])
        if updates:
            columns = ", ".join(f"{key} = ?" for key in updates)
            with self._write_lock, self._connect() as conn:
                conn.execute(
                    f"UPDATE foundry_jobs SET {columns} WHERE id = ?",
                    (*updates.values(), job_id),
                )
        return self.get_job(job_id)

    def active_job(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM foundry_jobs WHERE project_id = ?"
                " AND status IN ('queued', 'running')"
                " ORDER BY rowid DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return self._job_row(dict(row)) if row else None

    def list_jobs(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM foundry_jobs WHERE project_id = ?"
                " ORDER BY rowid DESC LIMIT 50",
                (project_id,),
            ).fetchall()
        return [self._job_row(dict(row)) for row in rows]

    @staticmethod
    def _job_row(row: dict[str, Any]) -> dict[str, Any]:
        row["result"] = _json_loads(row.pop("result_json", None), None)
        return row

    # ---- versions ----

    def add_version(
        self,
        project_id: str,
        *,
        mjcf_url: str | None,
        manifest_url: str | None,
        validation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        latest = self.latest_version(project_id)
        version = int(latest["version"]) + 1 if latest else 1
        version_id = new_id()
        now = utcnow_iso()
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO robot_versions (id, project_id, version, mjcf_url,"
                " manifest_url, validation_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    project_id,
                    version,
                    mjcf_url,
                    manifest_url,
                    json.dumps(validation) if validation is not None else None,
                    now,
                ),
            )
        return self.get_version(version_id)  # type: ignore[return-value]

    def get_version(self, version_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM robot_versions WHERE id = ?", (version_id,)
            ).fetchone()
        return self._version_row(dict(row)) if row else None

    def latest_version(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM robot_versions WHERE project_id = ?"
                " ORDER BY version DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return self._version_row(dict(row)) if row else None

    def update_latest_version_validation(
        self, project_id: str, validation: dict[str, Any]
    ) -> dict[str, Any] | None:
        latest = self.latest_version(project_id)
        if latest is None:
            return None
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE robot_versions SET validation_json = ? WHERE id = ?",
                (json.dumps(validation), latest["id"]),
            )
        return self.get_version(latest["id"])

    def list_versions(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM robot_versions WHERE project_id = ?"
                " ORDER BY version DESC",
                (project_id,),
            ).fetchall()
        return [self._version_row(dict(row)) for row in rows]

    @staticmethod
    def _version_row(row: dict[str, Any]) -> dict[str, Any]:
        row["validation"] = _json_loads(row.pop("validation_json", None), None)
        return row


__all__ = ["FoundryStore", "slug"]
