"""Safe inspection and extraction for ZIP/TAR archives.

Archive members are untrusted input.  Every member is validated before any
bytes are written, links/devices are rejected, and both member count and total
expanded size are bounded to prevent path traversal and simple archive bombs.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import tarfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

__all__ = [
    "ArchiveEntry",
    "ArchiveToolError",
    "ArchiveSummary",
    "inspect_archive",
    "extract_archive",
]


class ArchiveToolError(ValueError):
    """An archive is invalid, unsafe, unsupported, or exceeds a limit."""


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    name: str
    bytes: int
    is_dir: bool


@dataclass(frozen=True, slots=True)
class ArchiveSummary:
    format: str
    members: tuple[ArchiveEntry, ...]
    compressed_bytes: int
    expanded_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "format": self.format,
            "members": [
                {"name": value.name, "bytes": value.bytes, "is_dir": value.is_dir}
                for value in self.members
            ],
            "member_count": len(self.members),
            "compressed_bytes": self.compressed_bytes,
            "expanded_bytes": self.expanded_bytes,
        }


_MAX_BYTES = 100 * 1024 * 1024
_MAX_MEMBERS = 10_000


def _limit(name: str, value: int, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ArchiveToolError(f"{name} must be a positive integer")
    if value > maximum:
        raise ArchiveToolError(f"{name} must be at most {maximum}")
    return value


def _safe_name(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ArchiveToolError("archive member has an invalid name")
    normalized = raw.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:($|/)", normalized):
        raise ArchiveToolError(f"archive member path is unsafe: {raw!r}")
    path = PurePosixPath(normalized)
    # ``PurePosixPath('.')`` has no parts, so check the root marker before
    # inspecting components.  Extracting it would target the temporary root
    # itself instead of a member and makes the archive shape ambiguous.
    if (
        normalized in {".", "./"}
        or not path.parts
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ArchiveToolError(f"archive member path is unsafe: {raw!r}")
    return path.as_posix()


def _validate_entries(
    entries: Iterable[ArchiveEntry],
    *,
    max_members: int,
    max_expanded_bytes: int,
) -> tuple[tuple[ArchiveEntry, ...], int]:
    found: list[ArchiveEntry] = []
    names: set[str] = set()
    expanded = 0
    for entry in entries:
        if len(found) >= max_members:
            raise ArchiveToolError("archive exceeds the member limit")
        name = _safe_name(entry.name)
        if name in names:
            raise ArchiveToolError(f"archive contains duplicate member: {name!r}")
        if isinstance(entry.bytes, bool) or not isinstance(entry.bytes, int) or entry.bytes < 0:
            raise ArchiveToolError("archive member size is invalid")
        names.add(name)
        value = ArchiveEntry(name, entry.bytes, bool(entry.is_dir))
        found.append(value)
        if not value.is_dir:
            expanded += value.bytes
            if expanded > max_expanded_bytes:
                raise ArchiveToolError("archive exceeds the expanded-size limit")
    return tuple(found), expanded


def _format_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".zip":
        return "zip"
    if suffix in {".tar", ".tgz", ".tbz", ".tbz2", ".gz", ".bz2", ".xz"}:
        return "tar"
    raise ArchiveToolError("only ZIP and TAR archives are supported")


def _open_summary(
    path: Path,
    *,
    max_bytes: int,
    max_members: int,
    max_expanded_bytes: int,
) -> tuple[ArchiveSummary, Any]:
    if not path.is_file():
        raise ArchiveToolError(f"not a file: {path}")
    if path.stat().st_size > max_bytes:
        raise ArchiveToolError("archive exceeds the compressed-size limit")
    kind = _format_for(path)
    if kind == "zip":
        zip_archive: zipfile.ZipFile | None = None
        try:
            zip_archive = zipfile.ZipFile(path)
            zip_entries: list[ArchiveEntry] = []
            for zip_info in zip_archive.infolist():
                mode = (zip_info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
                    raise ArchiveToolError(f"archive member is not a regular file: {zip_info.filename!r}")
                zip_entries.append(ArchiveEntry(zip_info.filename, zip_info.file_size, zip_info.is_dir()))
            members, expanded = _validate_entries(
                zip_entries, max_members=max_members,
                max_expanded_bytes=max_expanded_bytes,
            )
            compressed = sum(info.compress_size for info in zip_archive.infolist())
        except (zipfile.BadZipFile, OSError) as exc:
            if zip_archive is not None:
                zip_archive.close()
            raise ArchiveToolError(f"could not read ZIP archive: {exc}") from exc
        except BaseException:
            if zip_archive is not None:
                zip_archive.close()
            raise
        assert zip_archive is not None
        return ArchiveSummary(kind, members, compressed, expanded), zip_archive
    tar_archive: tarfile.TarFile | None = None
    try:
        tar_archive = tarfile.open(path, mode="r:*")
        tar_entries: list[ArchiveEntry] = []
        for tar_info in tar_archive.getmembers():
            if not tar_info.isfile() and not tar_info.isdir():
                raise ArchiveToolError(f"archive member is not a regular file: {tar_info.name!r}")
            tar_entries.append(ArchiveEntry(tar_info.name, tar_info.size, tar_info.isdir()))
        members, expanded = _validate_entries(
            tar_entries, max_members=max_members,
            max_expanded_bytes=max_expanded_bytes,
        )
        compressed = path.stat().st_size
    except (tarfile.TarError, OSError) as exc:
        if tar_archive is not None:
            tar_archive.close()
        raise ArchiveToolError(f"could not read TAR archive: {exc}") from exc
    except BaseException:
        if tar_archive is not None:
            tar_archive.close()
        raise
    assert tar_archive is not None
    return ArchiveSummary(kind, members, compressed, expanded), tar_archive


def inspect_archive(
    path: Path,
    *,
    max_bytes: int = _MAX_BYTES,
    max_members: int = _MAX_MEMBERS,
    max_expanded_bytes: int = _MAX_BYTES,
) -> ArchiveSummary:
    """Inspect member names and sizes without writing archive contents."""
    _limit("max_bytes", max_bytes, maximum=_MAX_BYTES)
    _limit("max_members", max_members, maximum=_MAX_MEMBERS)
    _limit("max_expanded_bytes", max_expanded_bytes, maximum=_MAX_BYTES)
    summary, archive = _open_summary(
        path, max_bytes=max_bytes, max_members=max_members,
        max_expanded_bytes=max_expanded_bytes,
    )
    archive.close()
    return summary


def extract_archive(
    path: Path,
    destination: Path,
    *,
    max_bytes: int = _MAX_BYTES,
    max_members: int = _MAX_MEMBERS,
    max_expanded_bytes: int = _MAX_BYTES,
) -> ArchiveSummary:
    """Extract a validated archive into a new destination directory."""
    _limit("max_bytes", max_bytes, maximum=_MAX_BYTES)
    _limit("max_members", max_members, maximum=_MAX_MEMBERS)
    _limit("max_expanded_bytes", max_expanded_bytes, maximum=_MAX_BYTES)
    if destination.exists():
        raise ArchiveToolError("archive destination already exists")
    summary, archive = _open_summary(
        path, max_bytes=max_bytes, max_members=max_members,
        max_expanded_bytes=max_expanded_bytes,
    )
    temporary = destination.parent / f".lipas-extract-{uuid.uuid4().hex}"
    expanded_written = 0

    def copy_bounded(source: Any, target: Any) -> None:
        nonlocal expanded_written
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                return
            expanded_written += len(chunk)
            if expanded_written > max_expanded_bytes:
                raise ArchiveToolError("archive exceeds the expanded-size limit")
            target.write(chunk)

    try:
        temporary.mkdir(parents=True, exist_ok=False)
        by_name = {entry.name: entry for entry in summary.members}
        if summary.format == "zip":
            for info in archive.infolist():
                name = _safe_name(info.filename)
                target = temporary / name
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source, target.open("wb") as output:
                    copy_bounded(source, output)
                target.chmod(0o600)
        else:
            for info in archive.getmembers():
                name = _safe_name(info.name)
                target = temporary / name
                if info.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(info)
                if source is None:
                    raise ArchiveToolError(f"could not read archive member: {name!r}")
                with source, target.open("wb") as output:
                    copy_bounded(source, output)
                target.chmod(0o600)
        os.replace(temporary, destination)
    except ArchiveToolError:
        raise
    except (
        OSError, RuntimeError, KeyError, ValueError, EOFError,
        zipfile.BadZipFile, tarfile.TarError,
    ) as exc:
        raise ArchiveToolError(f"could not extract archive: {exc}") from exc
    finally:
        archive.close()
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return ArchiveSummary(summary.format, tuple(by_name.values()), summary.compressed_bytes, summary.expanded_bytes)
