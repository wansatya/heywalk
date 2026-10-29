"""Project file access with path-traversal guard and size limits.

Project files live on disk under ``<sequence_root>/<project_id>/`` (one folder
per project) with the database as the file index; paths still come from users
and untrusted model output, so every path is validated before use (AGENTS.md
sections 9 and 12).
"""

from __future__ import annotations

import posixpath
from typing import Any

from ..db import Store

MAX_PATH_LEN = 256
MAX_FILE_BYTES = 256 * 1024
MAX_FILES_PER_PROJECT = 100


class FilePathError(ValueError):
    """Raised when a project file path is rejected."""


class FileTooLargeError(ValueError):
    """Raised when file content exceeds the size limit."""


class TooManyFilesError(ValueError):
    """Raised when a project already holds the maximum number of files."""


def validate_file_path(path: str) -> str:
    """Return the normalized path, or raise FilePathError.

    Rejects absolute paths, Windows drive prefixes, traversal (..),
    backslashes, control characters, and empty segments.
    """
    if not isinstance(path, str):
        raise FilePathError("File path must be a string.")
    if not path or len(path) > MAX_PATH_LEN:
        raise FilePathError("File path is empty or too long.")
    if "\x00" in path:
        raise FilePathError("File path contains a null byte.")
    if "\\" in path:
        raise FilePathError("Backslashes are not allowed in file paths.")
    if path.startswith("/") or path.startswith("~"):
        raise FilePathError("Absolute paths are not allowed.")
    if len(path) >= 2 and path[1] == ":":
        raise FilePathError("Drive-prefixed paths are not allowed.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        raise FilePathError("File path contains control characters.")

    normalized = posixpath.normpath(path.strip())
    if normalized in ("", "."):
        raise FilePathError("File path is empty.")
    if normalized.startswith("../") or normalized == "..":
        raise FilePathError("Path traversal ('..') is not allowed.")
    if normalized.startswith("/"):
        raise FilePathError("Absolute paths are not allowed.")
    segments = normalized.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise FilePathError("File path contains an illegal segment.")
    return normalized


def check_content_size(content: str) -> None:
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise FileTooLargeError(
            f"File content exceeds the {MAX_FILE_BYTES // 1024} KiB limit."
        )


def save_project_file(
    store: Store, project_id: str, path: str, content: str
) -> dict[str, Any]:
    normalized = validate_file_path(path)
    check_content_size(content)
    if (
        store.get_file(project_id, normalized) is None
        and store.count_files(project_id) >= MAX_FILES_PER_PROJECT
    ):
        raise TooManyFilesError(
            f"Project already has the maximum of {MAX_FILES_PER_PROJECT} files."
        )
    return store.save_file(project_id, normalized, content)


def read_project_file(store: Store, project_id: str, path: str) -> dict[str, Any] | None:
    return store.get_file(project_id, validate_file_path(path))
