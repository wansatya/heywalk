"""Per-sequence folders: files live under <root>/<project_id>/ (folder isolation)."""

from __future__ import annotations

from pathlib import Path

from app.db import Store


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "t.db")


def test_save_writes_disk_file_under_project_dir(tmp_path):
    store = _store(tmp_path)
    project = store.create_project("a", "task")
    store.save_file(project["id"], "controllers/controller.py", "print(1)")

    disk = tmp_path / "sequences" / project["id"] / "controllers" / "controller.py"
    assert disk.is_file()
    assert disk.read_text(encoding="utf-8") == "print(1)"


def test_projects_are_isolated_at_folder_level(tmp_path):
    store = _store(tmp_path)
    first = store.create_project("a", "task")
    second = store.create_project("b", "task")
    store.save_file(first["id"], "controller.py", "FIRST")
    store.save_file(second["id"], "controller.py", "SECOND")

    assert store.get_file(first["id"], "controller.py")["content"] == "FIRST"
    assert store.get_file(second["id"], "controller.py")["content"] == "SECOND"
    first_dir = tmp_path / "sequences" / first["id"]
    second_dir = tmp_path / "sequences" / second["id"]
    assert first_dir.is_dir() and second_dir.is_dir()
    assert (first_dir / "controller.py").read_text() == "FIRST"
    assert (second_dir / "controller.py").read_text() == "SECOND"


def test_project_id_traversal_is_rejected(tmp_path):
    store = _store(tmp_path)
    project = store.create_project("a", "task")
    for evil_id in ("..", "../escape", "a/b", "", ".", "a..b/c"):
        try:
            store.project_dir(evil_id)
        except ValueError:
            continue
        raise AssertionError(f"project id {evil_id!r} was accepted")
    # Real project still resolves inside the root.
    resolved = store.project_dir(project["id"]).resolve()
    assert (tmp_path / "sequences").resolve() in resolved.parents


def test_legacy_db_row_is_restored_to_disk(tmp_path):
    store = _store(tmp_path)
    project = store.create_project("a", "task")
    store.save_file(project["id"], "controller.py", "LEGACY")
    disk = store.project_dir(project["id"]) / "controller.py"
    disk.unlink()
    assert store.get_file(project["id"], "controller.py")["content"] == "LEGACY"
    assert disk.is_file()  # self-healed back to disk


def test_browse_lists_tree_with_versions(client, project_id):
    client.put(
        f"/api/projects/{project_id}/files/controllers/controller.py",
        json={"content": "print(1) enthusiastically"},
    )
    client.put(
        f"/api/projects/{project_id}/files/README.md", json={"content": "hi"}
    )
    response = client.get(f"/api/projects/{project_id}/browse")
    assert response.status_code == 200
    entries = {entry["path"]: entry for entry in response.json()}
    assert entries["controllers"]["type"] == "dir"
    assert entries["controllers/controller.py"]["type"] == "file"
    assert entries["controllers/controller.py"]["version"] == 1
    assert entries["README.md"]["size"] == 2


def test_browse_unknown_project_is_404(client):
    response = client.get("/api/projects/does-not-exist/browse")
    assert response.status_code == 404


def test_export_zips_the_project_folder_at_root(client, project_id, tmp_path):
    """Exported program files match the on-disk project folder layout."""
    import io
    import zipfile

    client.put(
        f"/api/projects/{project_id}/files/launch/bringup.launch.py",
        json={"content": "# launch"},
    )
    response = client.get(f"/api/projects/{project_id}/export")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert "launch/bringup.launch.py" in names
        assert not any(n.startswith("files/") for n in names)
        assert archive.read("launch/bringup.launch.py") == b"# launch"


def test_create_makes_project_dir_and_delete_removes_it(tmp_path):
    store = _store(tmp_path)
    project = store.create_project("a", "task")
    folder = tmp_path / "sequences" / project["id"]
    assert folder.is_dir()
    store.save_file(project["id"], "controller.py", "print(1)")
    assert (folder / "controller.py").is_file()
    assert store.delete_project(project["id"]) is True
    assert not folder.exists()
