"""File path validation and size-limit tests (AGENTS.md sections 12, 14)."""

from __future__ import annotations

import pytest

from app.db import Store
from app.services.files import (
    MAX_FILE_BYTES,
    FilePathError,
    FileTooLargeError,
    check_content_size,
    save_project_file,
    validate_file_path,
)


@pytest.mark.parametrize(
    "path, expected",
    [
        ("controller.py", "controller.py"),
        ("src/controller.py", "src/controller.py"),
        ("a/./b.py", "a/b.py"),
        ("./main.py", "main.py"),
    ],
)
def test_valid_paths(path, expected):
    assert validate_file_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "",
        "   ",
        "/etc/passwd",
        "../escape.py",
        "a/../../escape.py",
        "..",
        "a/..",
        "C:\\controller.py",
        "C:/controller.py",
        "a\\b.py",
        "a\x00b.py",
        "a\nb.py",
        "~/controller.py",
        "x" * 300,
    ],
)
def test_rejected_paths(path):
    with pytest.raises(FilePathError):
        validate_file_path(path)


def test_content_size_limit():
    check_content_size("x" * MAX_FILE_BYTES)
    with pytest.raises(FileTooLargeError):
        check_content_size("x" * (MAX_FILE_BYTES + 1))


def test_save_versions_and_counts(tmp_path):
    store = Store(tmp_path / "t.db")
    project = store.create_project("p", "task")
    first = save_project_file(store, project["id"], "c.py", "v1")
    assert first["version"] == 1
    second = save_project_file(store, project["id"], "c.py", "v2")
    assert second["version"] == 2
    assert second["content"] == "v2"


def test_traversal_save_rejected_via_api(client, project_id):
    response = client.put(
        "/api/projects/abc/files/%2E%2E%2F..%2Fetc%2Fpasswd",
        json={"content": "x"},
    )
    assert response.status_code in (400, 404)
    response = client.put(
        f"/api/projects/{project_id}/files/%2E%2E%2Fescape.py",
        json={"content": "x"},
    )
    assert response.status_code == 400
