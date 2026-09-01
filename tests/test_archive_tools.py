"""Archive traversal and expansion-limit regression tests."""
from __future__ import annotations

import tarfile
import zipfile
import asyncio
from pathlib import Path

import pytest

from lipas.archive_tools import ArchiveToolError, extract_archive, inspect_archive
from lipas.workbench import Workbench, WorkspacePolicyError


def _zip(path: Path, name: str, content: bytes = b"ok") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, content)


def test_archive_inspection_rejects_traversal_and_extracts_regular_zip(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe.zip"
    _zip(unsafe, "../escape.txt")
    with pytest.raises(ArchiveToolError, match="unsafe"):
        inspect_archive(unsafe)
    drive = tmp_path / "drive.zip"
    _zip(drive, "C:/escape.txt")
    with pytest.raises(ArchiveToolError, match="unsafe"):
        inspect_archive(drive)

    safe = tmp_path / "safe.zip"
    _zip(safe, "nested/note.txt", b"hello")
    summary = extract_archive(safe, tmp_path / "out")
    assert summary.members[0].name == "nested/note.txt"
    assert (tmp_path / "out/nested/note.txt").read_bytes() == b"hello"

    dot = tmp_path / "dot.zip"
    _zip(dot, ".")
    with pytest.raises(ArchiveToolError, match="unsafe"):
        inspect_archive(dot)


def test_tar_symlink_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "links.tar"
    with tarfile.open(source, "w") as archive:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)
    with pytest.raises(ArchiveToolError, match="regular file"):
        inspect_archive(source)


def test_workbench_archive_tools_are_contained_and_evidenced(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "bundle.zip"
    _zip(source, "note.txt", b"hello")
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        task, run = workbench.create_task("extract", workspace)
        tools = {value.name: value for value in workbench.workspace_tools(task.id, run.id)}
        info = tools["inspect_archive"].invoke(relative_path="bundle.zip")
        assert info["member_count"] == 1
        result = __import__("asyncio").run(tools["extract_archive"].acall({
            "relative_path": "bundle.zip",
            "destination_path": "out",
        }))
        assert result["destination_path"] == "out"
        assert (workspace / "out/note.txt").read_text(encoding="utf-8") == "hello"
        assert {value.kind for value in workbench.artifacts(task.id)} >= {
            "archive_inspection", "archive_extraction",
        }
        with pytest.raises(WorkspacePolicyError):
            asyncio.run(tools["extract_archive"].acall({
                "relative_path": "bundle.zip", "destination_path": "out",
            }))
