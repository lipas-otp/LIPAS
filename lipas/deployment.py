"""Local single-workspace installation and release-readiness contracts.

The Runtime already owns schema creation and migration.  This module adds the
missing product boundary around it: an explicit installation manifest,
permission hardening, idempotent upgrade, and a machine-readable readiness
report.  It intentionally performs no package-manager or network work; the
Python wheel/venv remains the operator's deployment choice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import platform
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, cast

from ._version import __version__
from .workspace_storage import (
    WORKSPACE_DATABASE_NAME,
    WORKSPACE_SCHEMA_VERSION,
    RuntimeStorageIssue,
    WorkspaceMigrationRequired,
    WorkspaceStorage,
)

__all__ = [
    "INSTALLATION_MANIFEST_NAME",
    "INSTALLATION_MANIFEST_VERSION",
    "DeploymentCheck",
    "DeploymentReport",
    "InstallationManifest",
    "install_workspace",
    "upgrade_workspace",
    "verify_installation",
    "release_check",
]


INSTALLATION_MANIFEST_NAME = ".installation.json"
INSTALLATION_MANIFEST_VERSION = 1


def _finite_timestamp(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite timestamp") from exc
    if result != result or result in {float("inf"), float("-inf")}:
        raise ValueError(f"{name} must be a finite timestamp")
    return result


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
    ) + "\n"
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    temporary = Path(raw)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _harden_permissions(path: Path, mode: int) -> None:
    if os.name != "posix" or not path.exists():
        return
    if path.is_symlink():
        raise ValueError(f"path must not be a symlink: {path}")
    path.chmod(mode)


def _harden_evidence_tree(root: Path) -> None:
    """Apply private permissions to existing Run evidence files/directories."""
    if os.name != "posix" or not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        raise ValueError("runs path must be a regular directory")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"run evidence symlink is not allowed: {path}")
        if path.is_dir():
            path.chmod(0o700)
        elif path.is_file():
            path.chmod(0o600)
        else:
            raise ValueError(f"run evidence special file is not allowed: {path}")


@dataclass(frozen=True, slots=True)
class InstallationManifest:
    """Durable, non-secret description of one installed workspace."""

    home: Path
    package_version: str = __version__
    schema_version: int = WORKSPACE_SCHEMA_VERSION
    installed_at: float = field(default_factory=time.time)
    updated_at: float | None = None
    sandbox: str = "auto"

    def __post_init__(self) -> None:
        root = Path(self.home).expanduser().resolve()
        if not isinstance(self.package_version, str) or not self.package_version.strip():
            raise ValueError("package_version must be non-empty")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version < 1
        ):
            raise ValueError("schema_version must be a positive int")
        if self.sandbox not in {"auto", "bwrap", "local"}:
            raise ValueError("sandbox must be auto, bwrap, or local")
        object.__setattr__(self, "home", root)
        object.__setattr__(self, "package_version", self.package_version.strip())
        object.__setattr__(self, "installed_at", _finite_timestamp(self.installed_at, "installed_at"))
        if self.updated_at is not None:
            object.__setattr__(self, "updated_at", _finite_timestamp(self.updated_at, "updated_at"))

    @property
    def path(self) -> Path:
        return self.home / INSTALLATION_MANIFEST_NAME

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": INSTALLATION_MANIFEST_VERSION,
            "package_version": self.package_version,
            "schema_version": self.schema_version,
            "home": str(self.home),
            "database": str(self.home / WORKSPACE_DATABASE_NAME),
            "runs": str(self.home / "runs"),
            "sandbox": self.sandbox,
            "installed_at": self.installed_at,
            "updated_at": self.updated_at,
            "python": platform.python_version(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "InstallationManifest":
        if not isinstance(value, Mapping):
            raise ValueError("installation manifest must be an object")
        version = value.get("manifest_version")
        if version != INSTALLATION_MANIFEST_VERSION:
            raise ValueError("unsupported installation manifest version")
        home = value.get("home")
        if not isinstance(home, str) or not home.strip():
            raise ValueError("installation manifest home is invalid")
        root = Path(home).expanduser().resolve()
        for key, expected in (
            ("database", root / WORKSPACE_DATABASE_NAME),
            ("runs", root / "runs"),
        ):
            declared = value.get(key)
            if not isinstance(declared, str) or Path(declared).expanduser().resolve() != expected:
                raise ValueError(f"installation manifest {key} path is invalid")
        installed_at = value.get("installed_at")
        if installed_at is None:
            raise ValueError("installation manifest installed_at is invalid")
        return cls(
            Path(home),
            package_version=value.get("package_version", ""),
            schema_version=value.get("schema_version", -1),
            installed_at=cast(float, installed_at),
            updated_at=value.get("updated_at"),
            sandbox=value.get("sandbox", "auto"),
        )


@dataclass(frozen=True, slots=True)
class DeploymentCheck:
    name: str
    passed: bool
    detail: str
    severity: str = "error"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "severity": self.severity,
        }


@dataclass(frozen=True, slots=True)
class DeploymentReport:
    home: Path
    checks: tuple[DeploymentCheck, ...]
    manifest: InstallationManifest | None = None
    storage_issues: tuple[RuntimeStorageIssue, ...] = ()

    @property
    def ready(self) -> bool:
        return all(item.passed for item in self.checks)

    @property
    def failed(self) -> tuple[DeploymentCheck, ...]:
        return tuple(item for item in self.checks if not item.passed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "home": str(self.home),
            "manifest": None if self.manifest is None else self.manifest.as_dict(),
            "checks": [item.as_dict() for item in self.checks],
            "storage_issues": [item.as_dict() for item in self.storage_issues],
        }


def _read_manifest(home: Path) -> InstallationManifest | None:
    path = home / INSTALLATION_MANIFEST_NAME
    if not path.is_file() or path.is_symlink():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        manifest = InstallationManifest.from_mapping(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if manifest.home != home:
        return None
    return manifest


def install_workspace(
    home: str | Path,
    *,
    sandbox: str = "auto",
    force: bool = False,
) -> InstallationManifest:
    """Initialize a workspace and write its installation manifest.

    Installation is idempotent when the existing manifest matches the target
    home.  ``force`` only refreshes metadata; it never deletes databases.
    """
    if not isinstance(force, bool):
        raise TypeError("force must be bool")
    root = Path(home).expanduser().resolve()
    if sandbox not in {"auto", "bwrap", "local"}:
        raise ValueError("sandbox must be auto, bwrap, or local")
    root.mkdir(parents=True, exist_ok=True)
    storage = WorkspaceStorage(root)
    storage.recover_pending_restore()
    storage.require_current(create=True)
    # Schema bootstrap for component stores is owned by the composition root;
    # invoking it here makes a freshly installed workspace immediately usable
    # by ``release check`` and by the CLI, instead of leaving only
    # ``runtime_meta`` behind.
    from .runtime import LIPASRuntime
    with LIPASRuntime.open(root, sandbox=sandbox):
        pass
    (root / "runs").mkdir(exist_ok=True)
    _harden_permissions(root, 0o700)
    _harden_permissions(root / "runs", 0o700)
    _harden_evidence_tree(root / "runs")
    _harden_permissions(root / WORKSPACE_DATABASE_NAME, 0o600)
    previous = _read_manifest(root)
    if previous is not None and not force:
        if previous.sandbox != sandbox:
            raise ValueError(
                f"workspace is installed with sandbox={previous.sandbox!r}; "
                "pass --force to change the deployment setting",
            )
        if (
            previous.package_version == __version__
            and previous.schema_version == WORKSPACE_SCHEMA_VERSION
        ):
            return previous
    now = time.time()
    manifest = InstallationManifest(
        root,
        package_version=__version__,
        schema_version=WORKSPACE_SCHEMA_VERSION,
        installed_at=previous.installed_at if previous is not None else now,
        updated_at=now if previous is not None else None,
        sandbox=sandbox,
    )
    _atomic_json(manifest.path, manifest.as_dict())
    return manifest


def upgrade_workspace(
    home: str | Path,
    *,
    sandbox: str = "auto",
    force: bool = False,
) -> InstallationManifest:
    """Apply an explicit legacy migration, then refresh installation metadata."""
    root = Path(home).expanduser().resolve()
    storage = WorkspaceStorage(root)
    storage.recover_pending_restore()
    status = storage.inspect()
    if status.migration_required:
        storage.migrate()
    elif status.state == "invalid":
        raise WorkspaceMigrationRequired(
            "workspace is invalid; preserve workspace.db and repair it before upgrade",
        )
    return install_workspace(root, sandbox=sandbox, force=force)


def verify_installation(home: str | Path) -> DeploymentReport:
    """Check storage, manifest, layout, and local permission invariants."""
    root = Path(home).expanduser().resolve()
    storage = WorkspaceStorage(root)
    status = storage.inspect()
    issues = storage.audit() if status.current else status.issues
    manifest = _read_manifest(root)
    checks: list[DeploymentCheck] = []
    checks.append(DeploymentCheck(
        "manifest",
        manifest is not None,
        "installation manifest is valid" if manifest else "missing or invalid installation manifest",
    ))
    checks.append(DeploymentCheck(
        "manifest-schema",
        manifest is not None and manifest.schema_version == WORKSPACE_SCHEMA_VERSION,
        "manifest schema matches this release"
        if manifest is not None and manifest.schema_version == WORKSPACE_SCHEMA_VERSION
        else "manifest schema does not match this release",
    ))
    checks.append(DeploymentCheck(
        "manifest-package",
        manifest is not None and manifest.package_version == __version__,
        "manifest package version matches this release"
        if manifest is not None and manifest.package_version == __version__
        else "manifest package version differs; run `lipas upgrade`",
    ))
    checks.append(DeploymentCheck(
        "schema",
        status.current and status.schema_version == WORKSPACE_SCHEMA_VERSION,
        f"workspace state={status.state}, schema={status.schema_version}",
    ))
    checks.append(DeploymentCheck(
        "sqlite-integrity",
        not any(issue.severity == "error" for issue in issues),
        "SQLite and storage audit passed" if not any(issue.severity == "error" for issue in issues)
        else "; ".join(issue.message for issue in issues if issue.severity == "error"),
    ))
    layout_ok = status.current and (root / "runs").is_dir()
    checks.append(DeploymentCheck(
        "layout", layout_ok, "workspace.db and runs/ are present" if layout_ok else "runs/ directory is missing",
    ))
    if os.name == "posix" and root.exists():
        mode = stat.S_IMODE(root.stat().st_mode)
        checks.append(DeploymentCheck(
            "workspace-permissions", mode & 0o077 == 0,
            f"workspace mode is {mode:04o}",
        ))
        for name, path in (
            ("manifest-permissions", root / INSTALLATION_MANIFEST_NAME),
            ("database-permissions", root / WORKSPACE_DATABASE_NAME),
            ("runs-permissions", root / "runs"),
        ):
            if path.exists():
                file_mode = stat.S_IMODE(path.stat().st_mode)
                checks.append(DeploymentCheck(
                    name, file_mode & 0o077 == 0,
                    f"{path.name} mode is {file_mode:04o}",
                ))
        evidence_ok = True
        evidence_detail = "run evidence permissions are private"
        runs_root = root / "runs"
        if runs_root.exists():
            for evidence_path in runs_root.rglob("*"):
                if evidence_path.is_symlink():
                    evidence_ok = False
                    evidence_detail = f"run evidence symlink is not allowed: {evidence_path.name}"
                    break
                if evidence_path.is_file() or evidence_path.is_dir():
                    evidence_mode = stat.S_IMODE(evidence_path.stat().st_mode)
                    if evidence_mode & 0o077:
                        evidence_ok = False
                        evidence_detail = (
                            f"run evidence path {evidence_path.name} mode is {evidence_mode:04o}"
                        )
                        break
        checks.append(DeploymentCheck("evidence-permissions", evidence_ok, evidence_detail))
    checks.append(_backup_restore_check(root, status.current))
    checks.append(_evidence_bundle_check(root, status.current))
    return DeploymentReport(root, tuple(checks), manifest, tuple(issues))


def release_check(home: str | Path) -> DeploymentReport:
    """Alias used by the CLI and release automation for readiness checks."""
    return verify_installation(home)


def _backup_restore_check(root: Path, current: bool) -> DeploymentCheck:
    """Run a bounded backup/restore drill in a disposable directory."""
    if not current:
        return DeploymentCheck(
            "backup-restore", False,
            "workspace is not current; backup/restore drill was not run",
        )
    try:
        with tempfile.TemporaryDirectory(prefix="lipas-release-drill-") as raw:
            temporary = Path(raw)
            backup = WorkspaceStorage(root).backup(temporary / "backup.db")
            assert backup.backup_path is not None
            restored_root = temporary / "restored"
            restored = WorkspaceStorage(restored_root).restore(backup.backup_path)
            restored_status = WorkspaceStorage(restored_root).inspect()
            passed = restored.restored and restored_status.current
            detail = "backup and restore integrity drill passed" if passed else (
                "restored workspace did not pass current-schema verification"
            )
            return DeploymentCheck("backup-restore", passed, detail)
    except Exception as exc:
        # Readiness diagnostics should report a bounded failure, not expose a
        # path or provider payload from the temporary drill.
        return DeploymentCheck(
            "backup-restore", False,
            f"backup/restore drill failed: {type(exc).__name__}",
        )


def _evidence_bundle_check(root: Path, current: bool) -> DeploymentCheck:
    """Exercise the complete workspace/evidence bundle contract."""
    if not current:
        return DeploymentCheck(
            "evidence-bundle", False,
            "workspace is not current; evidence bundle drill was not run",
        )
    try:
        with tempfile.TemporaryDirectory(prefix="lipas-evidence-drill-") as raw:
            temporary = Path(raw)
            storage = WorkspaceStorage(root)
            bundle = storage.backup_bundle(temporary / "bundle")
            restored_root = temporary / "restored"
            restored = WorkspaceStorage(restored_root).restore_bundle(bundle.bundle_path)
            status = WorkspaceStorage(restored_root).inspect()
            passed = restored.restored and status.current
            detail = "workspace/evidence bundle drill passed" if passed else (
                "restored evidence bundle did not pass current-schema verification"
            )
            return DeploymentCheck("evidence-bundle", passed, detail)
    except Exception as exc:
        return DeploymentCheck(
            "evidence-bundle", False,
            f"evidence bundle drill failed: {type(exc).__name__}",
        )
