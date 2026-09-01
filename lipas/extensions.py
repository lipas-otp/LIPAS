"""Small SDK for packaging Scenarios/Skills without importing them into core."""
from __future__ import annotations

import re
import hashlib
import hmac
import json
import binascii
import math
import sqlite3
import threading
import time
import contextlib
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from ._version import __version__
from .sqlite_storage import connect_sqlite, immediate_transaction

try:  # POSIX advisory locks fence separate Registry instances/processes.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    _fcntl = None  # type: ignore[assignment]

__all__ = [
    "ConformanceCheck",
    "ConformanceReport",
    "ExtensionCertification",
    "ExtensionManifest", "ExtensionTrustPolicy",
    "ExtensionRegistry", "ExtensionSigner", "ExtensionRegistryService",
    "run_conformance",
    "scaffold_extension",
]


def _finite_number(value: Any, name: str) -> float:
    """Validate certification timestamps without leaking float overflow."""
    try:
        valid = (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
        )
    except (OverflowError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite")
    return float(value)


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    """Provider-neutral package metadata for a LIPAS extension."""

    name: str
    version: str = "0.1.0"
    lipas_min_version: str = "0.39.0"
    entrypoint: str = "extension:build"
    scenarios: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    lipas_max_version: str | None = None
    # ``provenance`` is intentionally a declaration, not a trust decision.
    # Hosts can apply their own signing/registry policy before installation.
    provenance: str = "local"
    connector_scope: tuple[str, ...] = ()
    requires_approval: bool = False
    supports_reconciliation: bool = False
    # A digest binds registry metadata to the artifact supplied by the host;
    # it is not a signature and never grants execution authority by itself.
    artifact_sha256: str | None = None
    signer: str | None = None
    signature: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "name", "version", "lipas_min_version", "entrypoint", "provenance",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
            object.__setattr__(self, field_name, value.strip())
        if not _SEMVER.fullmatch(self.version):
            raise ValueError("version must be a semantic version such as 0.1.0")
        if not _SEMVER.fullmatch(self.lipas_min_version):
            raise ValueError(
                "lipas_min_version must be a semantic version such as 0.39.0",
            )
        if self.lipas_max_version is not None:
            if not isinstance(self.lipas_max_version, str):
                raise ValueError(
                    "lipas_max_version must be a semantic version or None",
                )
            maximum = self.lipas_max_version.strip()
            if not _SEMVER.fullmatch(maximum):
                raise ValueError(
                    "lipas_max_version must be a semantic version or None",
                )
            object.__setattr__(self, "lipas_max_version", maximum)
        if (
            self.lipas_max_version is not None
            and _semver_key(self.lipas_max_version) < _semver_key(self.lipas_min_version)
        ):
            raise ValueError("lipas_max_version cannot be below lipas_min_version")
        if (
            ":" not in self.entrypoint
            or self.entrypoint.startswith(":")
            or self.entrypoint.endswith(":")
            or any(not part.strip() for part in self.entrypoint.split(":", 1))
        ):
            raise ValueError("entrypoint must be module:function")
        for field_name in ("scenarios", "skills", "connector_scope"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple):
                raise TypeError(f"{field_name} must be a tuple")
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"{field_name} must contain non-empty strings")
            normalized_values = tuple(value.strip() for value in values)
            if len(set(normalized_values)) != len(normalized_values):
                raise ValueError(f"{field_name} must contain unique strings")
            object.__setattr__(self, field_name, normalized_values)
        for field_name in ("requires_approval", "supports_reconciliation"):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be bool")
        if self.artifact_sha256 is not None and (
            not isinstance(self.artifact_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.artifact_sha256)
        ):
            raise ValueError("artifact_sha256 must be a lowercase SHA-256 hex digest")
        if self.signer is not None and (not isinstance(self.signer, str) or not self.signer.strip()):
            raise ValueError("signer must be non-empty or None")
        if self.signer is not None:
            object.__setattr__(self, "signer", self.signer.strip())
        if self.signature is not None and (
            not isinstance(self.signature, str) or not re.fullmatch(r"[0-9a-f]{64}", self.signature)
        ):
            raise ValueError("signature must be a lowercase SHA-256 hex digest or None")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExtensionManifest":
        if not isinstance(value, Mapping):
            raise TypeError("extension manifest must be a mapping")
        schema_version = value.get("schema_version", 1)
        if isinstance(schema_version, bool) or schema_version != 1:
            raise ValueError(
                f"unsupported extension manifest schema version: {schema_version!r}",
            )
        scenarios = value.get("scenarios", ())
        skills = value.get("skills", ())
        connector_scope = value.get("connector_scope", ())
        for field_name, values in (
            ("scenarios", scenarios),
            ("skills", skills),
            ("connector_scope", connector_scope),
        ):
            if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
                raise TypeError(f"{field_name} must be a sequence of strings")
        return cls(
            name=value.get("name", ""),
            version=value.get("version", "0.1.0"),
            lipas_min_version=value.get("lipas_min_version", "0.39.0"),
            lipas_max_version=value.get("lipas_max_version"),
            entrypoint=value.get("entrypoint", "extension:build"),
            scenarios=tuple(scenarios),
            skills=tuple(skills),
            provenance=value.get("provenance", "local"),
            connector_scope=tuple(connector_scope),
            requires_approval=value.get("requires_approval", False),
            supports_reconciliation=value.get("supports_reconciliation", False),
            artifact_sha256=value.get("artifact_sha256"),
            signer=value.get("signer"),
            signature=value.get("signature"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "name": self.name,
            "version": self.version,
            "lipas_min_version": self.lipas_min_version,
            "lipas_max_version": self.lipas_max_version,
            "entrypoint": self.entrypoint,
            "scenarios": list(self.scenarios),
            "skills": list(self.skills),
            "provenance": self.provenance,
            "connector_scope": list(self.connector_scope),
            "requires_approval": self.requires_approval,
            "supports_reconciliation": self.supports_reconciliation,
            "artifact_sha256": self.artifact_sha256,
            "signer": self.signer,
            "signature": self.signature,
        }


@dataclass(frozen=True, slots=True)
class ExtensionTrustPolicy:
    """Host-side trust checks; manifest provenance alone never grants trust."""

    allowed_provenance: frozenset[str] = frozenset()
    trusted_signers: frozenset[str] = frozenset()
    require_signature: bool = False
    signer_secrets: Mapping[str, bytes | str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_provenance, frozenset) or not isinstance(self.trusted_signers, frozenset):
            raise TypeError("trust policy sets must be frozensets")
        if any(not isinstance(value, str) or not value.strip() for value in (*self.allowed_provenance, *self.trusted_signers)):
            raise ValueError("trust policy values must be non-empty strings")
        if not isinstance(self.require_signature, bool):
            raise TypeError("require_signature must be bool")
        if not isinstance(self.signer_secrets, Mapping):
            raise TypeError("signer_secrets must be a mapping")
        normalized_provenance = frozenset(value.strip() for value in self.allowed_provenance)
        normalized_signers = frozenset(value.strip() for value in self.trusted_signers)
        if len(normalized_provenance) != len(self.allowed_provenance) or len(normalized_signers) != len(self.trusted_signers):
            raise ValueError("trust policy values contain duplicates after normalization")
        normalized: dict[str, bytes | str] = {}
        for signer, secret in self.signer_secrets.items():
            if not isinstance(signer, str) or not signer.strip():
                raise ValueError("signer secret names must be non-empty strings")
            normalized_signer = signer.strip()
            if normalized_signer in normalized:
                raise ValueError(
                    "signer secret names contain duplicates after normalization",
                )
            if isinstance(secret, str):
                secret = secret.encode("utf-8")
            if not isinstance(secret, bytes) or len(secret) < 16:
                raise ValueError("signer secrets must contain at least 16 bytes")
            normalized[normalized_signer] = secret
        object.__setattr__(self, "signer_secrets", normalized)
        object.__setattr__(
            self,
            "allowed_provenance",
            normalized_provenance,
        )
        object.__setattr__(
            self,
            "trusted_signers",
            normalized_signers,
        )

    def check(self, manifest: ExtensionManifest) -> None:
        if self.allowed_provenance and manifest.provenance not in self.allowed_provenance:
            raise ValueError(f"extension provenance {manifest.provenance!r} is not trusted")
        if self.require_signature and manifest.signature is None:
            raise ValueError("extension signature is required by trust policy")
        if self.trusted_signers and manifest.signer not in self.trusted_signers:
            raise ValueError(f"extension signer {manifest.signer!r} is not trusted")


@dataclass(frozen=True, slots=True)
class ConformanceCheck:
    name: str
    passed: bool
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("conformance check name must be non-empty")
        if not isinstance(self.passed, bool):
            raise TypeError("conformance check passed must be bool")
        if not isinstance(self.detail, str):
            raise TypeError("conformance check detail must be a string")


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    manifest: ExtensionManifest
    checks: tuple[ConformanceCheck, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, ExtensionManifest):
            raise TypeError("ConformanceReport.manifest must be ExtensionManifest")
        if not isinstance(self.checks, tuple) or not self.checks or any(
            not isinstance(check, ConformanceCheck) for check in self.checks
        ):
            raise ValueError("ConformanceReport.checks must be a non-empty tuple")

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> tuple[ConformanceCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.as_dict(),
            "passed": self.passed,
            "checks": [
                {
                    "name": check.name,
                    "passed": check.passed,
                    "detail": check.detail,
                }
                for check in self.checks
            ],
        }


@dataclass(frozen=True, slots=True)
class ExtensionCertification:
    """An offline certification result suitable for a host registry."""

    manifest: ExtensionManifest
    report: ConformanceReport
    artifact_sha256: str | None
    certified_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, ExtensionManifest):
            raise TypeError("ExtensionCertification.manifest must be ExtensionManifest")
        if not isinstance(self.report, ConformanceReport):
            raise TypeError("ExtensionCertification.report must be ConformanceReport")
        if self.report.manifest != self.manifest:
            raise ValueError("certification report manifest does not match manifest")
        if self.artifact_sha256 is not None and (
            not isinstance(self.artifact_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.artifact_sha256)
        ):
            raise ValueError("artifact_sha256 must be a lowercase SHA-256 hex digest")
        if self.manifest.artifact_sha256 is not None and (
            self.artifact_sha256 != self.manifest.artifact_sha256
        ):
            raise ValueError("certification artifact digest does not match manifest")
        object.__setattr__(self, "certified_at", _finite_number(self.certified_at, "certified_at"))

    @property
    def certified(self) -> bool:
        return self.report.passed and self.artifact_sha256 is not None


class ExtensionSigner:
    """Small standard-library HMAC signer for extension artifacts.

    This is package authenticity, not a sandbox or an execution grant.  Hosts
    should keep the secret outside the extension workspace and still apply a
    trust policy before loading any code.
    """

    def __init__(self, signer: str, secret: bytes | str) -> None:
        if not isinstance(signer, str) or not signer.strip():
            raise ValueError("signer must be non-empty")
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if not isinstance(secret, bytes) or len(secret) < 16:
            raise ValueError("extension signing secret must contain at least 16 bytes")
        self.signer = signer.strip()
        self._secret = secret

    def sign(
        self,
        manifest: ExtensionManifest,
        artifact: bytes | None = None,
        *,
        artifact_sha256: str | None = None,
    ) -> ExtensionManifest:
        if not isinstance(manifest, ExtensionManifest):
            raise TypeError("manifest must be ExtensionManifest")
        declared = artifact_sha256 if artifact_sha256 is not None else manifest.artifact_sha256
        digest = _artifact_digest(artifact, declared)
        if digest is None:
            raise ValueError("artifact or artifact_sha256 is required for signing")
        unsigned = replace(manifest, signer=self.signer, signature=None, artifact_sha256=digest)
        return replace(unsigned, signature=self._signature(unsigned, digest))

    def verify(
        self,
        manifest: ExtensionManifest,
        artifact: bytes | None = None,
        *,
        artifact_sha256: str | None = None,
    ) -> bool:
        if not isinstance(manifest, ExtensionManifest) or manifest.signer != self.signer:
            return False
        if manifest.signature is None:
            return False
        try:
            declared = artifact_sha256 if artifact_sha256 is not None else manifest.artifact_sha256
            digest = _artifact_digest(artifact, declared)
        except (TypeError, ValueError):
            return False
        return digest is not None and hmac.compare_digest(
            manifest.signature, self._signature(manifest, digest),
        )

    def _signature(self, manifest: ExtensionManifest, digest: str) -> str:
        payload = dict(manifest.as_dict())
        payload["signature"] = None
        canonical = json.dumps(
            {"manifest": payload, "artifact_sha256": digest},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        return hmac.new(self._secret, canonical, hashlib.sha256).hexdigest()


class ExtensionRegistry:
    """Host-owned registry metadata; it never imports or executes extensions."""

    def __init__(
        self,
        *,
        trust_policy: ExtensionTrustPolicy | None = None,
        signers: Mapping[str, ExtensionSigner | bytes | str] | None = None,
        path: str | Path | None = None,
    ) -> None:
        self._records: dict[str, ExtensionCertification] = {}
        self._history: dict[str, list[ExtensionCertification]] = {}
        self._revoked: set[str] = set()
        self.path = None if path is None else Path(path).expanduser().resolve()
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._closed = False
        if trust_policy is not None and not isinstance(trust_policy, ExtensionTrustPolicy):
            raise TypeError("trust_policy must be ExtensionTrustPolicy or None")
        self.trust_policy = trust_policy
        raw_signers = dict(
            signers
            if signers is not None
            else (trust_policy.signer_secrets if trust_policy is not None else {})
        )
        self._signers: dict[str, ExtensionSigner] = {}
        for key, value in raw_signers.items():
            if not isinstance(key, str):
                raise TypeError("signer registry keys must be strings")
            if isinstance(value, ExtensionSigner):
                signer = value
            elif isinstance(value, (bytes, str)):
                signer = ExtensionSigner(key, value)
            else:
                raise TypeError("signers must map names to ExtensionSigner or secret values")
            self._signers[key] = signer
        if any(key != value.signer for key, value in self._signers.items()):
            raise ValueError("signer registry keys must match ExtensionSigner.signer")
        if self.path is not None:
            self._conn = connect_sqlite(self.path, check_same_thread=False)
            try:
                self._init_schema()
                self._load_persisted()
            except BaseException:
                self._conn.close()
                self._conn = None
                self._closed = True
                raise

    def register(
        self,
        manifest: ExtensionManifest,
        *,
        artifact: bytes | None = None,
        artifact_sha256: str | None = None,
        signature_secret: bytes | str | None = None,
        scenario_names: set[str] | None = None,
        skill_names: set[str] | None = None,
        lipas_version: str | None = None,
    ) -> ExtensionCertification:
        """Register one certification under the registry lock.

        Registration updates the current record, version history, revocation
        set, and (when configured) SQLite row as one process-local critical
        section.  Without this lock a concurrent register/revoke pair could
        persist a snapshot assembled from half of each mutation.
        """
        with self._lock, self._mutation_lock():
            self._reload_persisted()
            return self._register(
                manifest,
                artifact=artifact,
                artifact_sha256=artifact_sha256,
                signature_secret=signature_secret,
                scenario_names=scenario_names,
                skill_names=skill_names,
                lipas_version=lipas_version,
            )

    def _register(
        self,
        manifest: ExtensionManifest,
        *,
        artifact: bytes | None = None,
        artifact_sha256: str | None = None,
        signature_secret: bytes | str | None = None,
        scenario_names: set[str] | None = None,
        skill_names: set[str] | None = None,
        lipas_version: str | None = None,
    ) -> ExtensionCertification:
        self._ensure_open()
        if not isinstance(manifest, ExtensionManifest):
            raise TypeError("manifest must be ExtensionManifest")
        if self.trust_policy is not None:
            self.trust_policy.check(manifest)
        digest = _artifact_digest(artifact, artifact_sha256)
        if manifest.artifact_sha256 is not None and digest != manifest.artifact_sha256:
            raise ValueError("artifact digest does not match manifest provenance")
        signer = self._signers.get(manifest.signer or "")
        if signature_secret is not None:
            if manifest.signer is None:
                raise ValueError("signature_secret requires manifest.signer")
            signer = ExtensionSigner(manifest.signer, signature_secret)
        if manifest.signature is not None:
            if signer is None or not signer.verify(
                manifest, artifact=artifact, artifact_sha256=digest,
            ):
                raise ValueError("extension signature verification failed")
        elif self.trust_policy is not None and self.trust_policy.require_signature:
            raise ValueError("extension signature is required by trust policy")
        report = run_conformance(
            manifest,
            scenario_names=scenario_names,
            skill_names=skill_names,
            lipas_version=lipas_version,
        )
        record = ExtensionCertification(manifest, report, digest, time.time())
        previous = self._records.get(manifest.name)
        if previous is not None:
            if previous.manifest == manifest and previous.artifact_sha256 == digest:
                # Re-registering the same certified bytes is an idempotent
                # deployment operation.  It also explicitly revives a name
                # that an operator previously revoked; otherwise the early
                # return would leave the durable revoked flag in place.
                if manifest.name in self._revoked:
                    self._revoked.discard(manifest.name)
                    self._persist()
                return previous
            if _semver_key(manifest.version) <= _semver_key(previous.manifest.version):
                raise ValueError(
                    f"extension {manifest.name!r} version must increase for an update",
                )
        self._records[manifest.name] = record
        history = self._history.setdefault(manifest.name, [])
        history.append(record)
        self._revoked.discard(manifest.name)
        self._persist()
        return record

    def get(self, name: str) -> ExtensionCertification | None:
        name = _extension_name(name)
        with self._lock, self._mutation_lock():
            self._ensure_open()
            self._reload_persisted()
            if name in self._revoked:
                return None
            return self._records.get(name)

    def list(self) -> tuple[ExtensionCertification, ...]:
        with self._lock, self._mutation_lock():
            self._ensure_open()
            self._reload_persisted()
            return tuple(
                self._records[name]
                for name in sorted(self._records)
                if name not in self._revoked
            )

    def revoke(self, name: str) -> None:
        name = _extension_name(name)
        with self._lock, self._mutation_lock():
            self._ensure_open()
            self._reload_persisted()
            if name not in self._records:
                raise KeyError(name)
            self._revoked.add(name)
            self._persist()

    def rollback(self, name: str) -> ExtensionCertification:
        """Restore the previous certified version without importing it."""
        name = _extension_name(name)
        with self._lock, self._mutation_lock():
            self._ensure_open()
            self._reload_persisted()
            history = self._history.get(name, [])
            if len(history) < 2:
                raise ValueError(f"extension {name!r} has no previous certified version")
            history.pop()
            previous = history[-1]
            self._records[name] = previous
            self._revoked.discard(name)
            self._persist()
            return previous

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._conn is not None:
                self._conn.close()
            self._closed = True

    def __enter__(self) -> "ExtensionRegistry":
        self._ensure_open()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("ExtensionRegistry is closed")

    @contextlib.contextmanager
    def _mutation_lock(self):
        """Fence mutations across Registry instances sharing one SQLite file.

        The in-memory dictionaries are a convenience cache.  Without a
        process/file lock, two registry handles could each DELETE+INSERT a
        snapshot assembled from stale state and silently erase the other's
        record.  A sidecar advisory lock plus a reload under that lock keeps
        the SQLite row and the cache on one linearized mutation path.
        """
        if self.path is None or _fcntl is None:
            yield
            return
        lock_path = Path(f"{self.path}.registry.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = open(lock_path, "a+b")
        try:
            _fcntl.flock(descriptor.fileno(), _fcntl.LOCK_EX)
            yield
        finally:
            try:
                _fcntl.flock(descriptor.fileno(), _fcntl.LOCK_UN)
            finally:
                descriptor.close()

    def _reload_persisted(self) -> None:
        """Refresh one file-backed cache after acquiring the mutation fence."""
        if self._conn is None:
            return
        self._records.clear()
        self._history.clear()
        self._revoked.clear()
        self._load_persisted()

    def _init_schema(self) -> None:
        assert self._conn is not None
        with self._lock, self._conn:
            # Check compatibility before creating the registry table.  An
            # older runtime opening a future database must fail closed without
            # partially mutating its business schema.
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS lipas_extension_registry_meta("
                "key TEXT PRIMARY KEY,value TEXT NOT NULL)",
            )
            row = self._conn.execute(
                "SELECT value FROM lipas_extension_registry_meta "
                "WHERE key='schema_version'",
            ).fetchone()
            if row is not None:
                try:
                    version = int(row[0])
                except (TypeError, ValueError, OverflowError) as exc:
                    raise RuntimeError(
                        "extension registry schema version is not an int",
                    ) from exc
                if version != 1:
                    raise RuntimeError(
                        "extension registry schema version mismatch: "
                        f"database={version}, runtime=1",
                    )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS lipas_extension_registry("
                "name TEXT PRIMARY KEY, certification_json TEXT NOT NULL,"
                "history_json TEXT NOT NULL, revoked INTEGER NOT NULL "
                "CHECK(revoked IN (0,1)))",
            )
            if row is None:
                self._conn.execute(
                    "INSERT INTO lipas_extension_registry_meta(key,value) "
                    "VALUES('schema_version','1')",
                )

    def _load_persisted(self) -> None:
        assert self._conn is not None
        with self._lock:
            rows = self._conn.execute(
                "SELECT name,certification_json,history_json,revoked "
                "FROM lipas_extension_registry ORDER BY name",
            ).fetchall()
        for name, current_json, history_json, revoked in rows:
            if isinstance(revoked, bool) or revoked not in (0, 1):
                raise ValueError("persisted extension revoked flag is invalid")
            current = _certification_from_mapping(_loads_strict(current_json))
            raw_history = _loads_strict(history_json)
            if not isinstance(raw_history, list):
                raise ValueError("persisted extension history must be an array")
            history = [
                _certification_from_mapping(item)
                for item in raw_history
            ]
            if not history:
                history = [current]
            if current.manifest.name != str(name):
                raise ValueError("persisted extension name does not match manifest")
            if any(item.manifest.name != current.manifest.name for item in history):
                raise ValueError("persisted extension history contains another name")
            if history[-1] != current:
                raise ValueError("persisted extension history does not end at current version")
            if self.trust_policy is not None:
                self.trust_policy.check(current.manifest)
            if current.manifest.signature is not None:
                signer = self._signers.get(current.manifest.signer or "")
                if signer is None or not signer.verify(
                    current.manifest,
                    artifact_sha256=current.artifact_sha256,
                ):
                    raise ValueError(
                        f"persisted extension {name!r} signature verification failed",
                    )
            if self.trust_policy is not None or self._signers:
                for historical in history:
                    if self.trust_policy is not None:
                        self.trust_policy.check(historical.manifest)
                    if historical.manifest.signature is not None:
                        signer = self._signers.get(historical.manifest.signer or "")
                        if signer is None or not signer.verify(
                            historical.manifest,
                            artifact_sha256=historical.artifact_sha256,
                        ):
                            raise ValueError(
                                f"persisted extension {name!r} history signature verification failed",
                            )
            self._records[str(name)] = current
            self._history[str(name)] = history
            if revoked:
                self._revoked.add(str(name))

    def _persist(self) -> None:
        if self._conn is None:
            return
        rows = []
        for name, current in self._records.items():
            history = self._history.get(name, [current])
            rows.append(
                (
                    name,
                    json.dumps(_certification_json(current), sort_keys=True, separators=(",", ":"), allow_nan=False),
                    json.dumps([_certification_json(item) for item in history], sort_keys=True, separators=(",", ":"), allow_nan=False),
                    int(name in self._revoked),
                )
            )
        with self._lock, immediate_transaction(self._conn):
            self._conn.execute("DELETE FROM lipas_extension_registry")
            self._conn.executemany(
                "INSERT INTO lipas_extension_registry(name,certification_json,history_json,revoked) VALUES(?,?,?,?)",
                rows,
            )


class ExtensionRegistryService(HTTPServer):
    """Minimal authenticated registry HTTP service.

    It publishes certification metadata only; it never imports, installs, or
    executes an extension.  ``auth_token`` protects mutations (GET is public
    by design so a deployment can mirror metadata without sharing secrets).
    """

    def __init__(self, address: tuple[str, int], registry: ExtensionRegistry, *, auth_token: str) -> None:
        if not isinstance(registry, ExtensionRegistry):
            raise TypeError("registry must be ExtensionRegistry")
        if not isinstance(auth_token, str) or len(auth_token.strip()) < 16:
            raise ValueError("auth_token must contain at least 16 characters")
        self.registry = registry
        self.auth_token = auth_token.strip()
        super().__init__(address, _ExtensionRegistryHandler)


class _ExtensionRegistryHandler(BaseHTTPRequestHandler):
    server: ExtensionRegistryService

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parts = tuple(unquote(part) for part in urlsplit(self.path).path.split("/") if part)
        if parts == ("v1", "extensions"):
            self._send(200, {"extensions": [_certification_json(item) for item in self.server.registry.list()]})
            return
        if len(parts) == 3 and parts[:2] == ("v1", "extensions"):
            item = self.server.registry.get(parts[2])
            if item is None:
                self._send(404, {"error": "not found"})
            else:
                self._send(200, _certification_json(item))
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        scheme, _, value = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(value, self.server.auth_token):
            self._send(401, {"error": "registry authorization required"})
            return
        parts = tuple(unquote(part) for part in urlsplit(self.path).path.split("/") if part)
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0 or length > 4 * 1024 * 1024:
                raise ValueError("invalid request body length")
            payload = json.loads(
                self.rfile.read(length).decode("utf-8"),
                parse_constant=lambda raw: (_ for _ in ()).throw(
                    ValueError(f"non-JSON numeric constant {raw!r}")
                ),
            )
            if not isinstance(payload, Mapping):
                raise ValueError("request body must be an object")
            if parts == ("v1", "extensions"):
                manifest = ExtensionManifest.from_mapping(payload["manifest"])
                artifact_value = payload.get("artifact_base64")
                artifact = None
                if artifact_value is not None:
                    import base64
                    artifact = base64.b64decode(artifact_value, validate=True)
                record = self.server.registry.register(manifest, artifact=artifact)
                self._send(200, _certification_json(record))
                return
            if len(parts) == 4 and parts[:2] == ("v1", "extensions") and parts[3] == "revoke":
                self.server.registry.revoke(parts[2])
                self._send(200, {"revoked": parts[2]})
                return
            self._send(404, {"error": "not found"})
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, binascii.Error) as exc:
            self._send(400, {"error": "invalid request", "detail": str(exc)})

    def _send(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _certification_json(record: ExtensionCertification) -> dict[str, Any]:
    return {
        "manifest": record.manifest.as_dict(),
        "artifact_sha256": record.artifact_sha256,
        "certified": record.certified,
        "certified_at": record.certified_at,
        "report": record.report.as_dict(),
    }


def _certification_from_mapping(value: Mapping[str, Any]) -> ExtensionCertification:
    if not isinstance(value, Mapping):
        raise ValueError("persisted extension certification must be an object")
    manifest = ExtensionManifest.from_mapping(value.get("manifest", {}))
    raw_report = value.get("report", {})
    if not isinstance(raw_report, Mapping):
        raise ValueError("persisted extension report must be an object")
    raw_checks = raw_report.get("checks", ())
    if not isinstance(raw_checks, list):
        raise ValueError("persisted extension checks must be an array")
    checks_list: list[ConformanceCheck] = []
    for item in raw_checks:
        if not isinstance(item, Mapping):
            raise ValueError("persisted extension check must be an object")
        passed = item.get("passed")
        if not isinstance(passed, bool):
            raise ValueError("persisted extension check passed must be bool")
        checks_list.append(ConformanceCheck(item.get("name", ""), passed, item.get("detail", "")))
    checks = tuple(checks_list)
    report = ConformanceReport(manifest, checks)
    if raw_report.get("manifest") is not None:
        reported_manifest = ExtensionManifest.from_mapping(raw_report["manifest"])
        if reported_manifest != manifest:
            raise ValueError("persisted extension report manifest mismatch")
    certified_at = value.get("certified_at")
    certified_at = _finite_number(certified_at, "persisted extension certified_at")
    digest = value.get("artifact_sha256")
    if digest is not None and (
        not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise ValueError("persisted extension artifact digest is invalid")
    if "certified" in value:
        certified = value["certified"]
        if not isinstance(certified, bool):
            raise ValueError("persisted extension certified flag must be bool")
        if certified != (report.passed and digest is not None):
            raise ValueError("persisted extension certified flag is inconsistent")
    if manifest.artifact_sha256 is not None and digest != manifest.artifact_sha256:
        raise ValueError("persisted extension artifact digest does not match manifest")
    return ExtensionCertification(manifest, report, digest, certified_at)


def _loads_strict(value: str) -> Any:
    try:
        return json.loads(
            value,
            parse_constant=lambda raw: (_ for _ in ()).throw(
                ValueError(f"non-JSON numeric constant {raw!r}")
            ),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("persisted extension data must be strict JSON") from exc


def _artifact_digest(artifact: bytes | None, declared: str | None) -> str | None:
    if artifact is not None:
        if not isinstance(artifact, bytes):
            raise TypeError("artifact must be bytes or None")
        digest = hashlib.sha256(artifact).hexdigest()
        if declared is not None and declared != digest:
            raise ValueError("declared artifact_sha256 does not match artifact")
        return digest
    if declared is not None and (
        not isinstance(declared, str)
        or not re.fullmatch(r"[0-9a-f]{64}", declared)
    ):
        raise ValueError("artifact_sha256 must be a lowercase SHA-256 hex digest")
    return declared


def _extension_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("extension name must be a non-empty string")
    return value.strip()


def run_conformance(
    manifest: ExtensionManifest,
    *,
    scenario_names: set[str] | None = None,
    skill_names: set[str] | None = None,
    lipas_version: str | None = None,
) -> ConformanceReport:
    """Validate package metadata and declared catalog references offline.

    The check is deliberately provider-free.  ``lipas_version`` is injectable
    so release CI and downstream registries can test compatibility fixtures
    without importing or installing a second LIPAS version.
    """
    if not isinstance(manifest, ExtensionManifest):
        raise TypeError("manifest must be ExtensionManifest")
    checks = [
        ConformanceCheck(
            "manifest.schema",
            True,
            "schema version 1",
        ),
        ConformanceCheck(
            "manifest.entrypoint",
            ":" in manifest.entrypoint and not manifest.entrypoint.startswith(":")
            and not manifest.entrypoint.endswith(":"),
            "entrypoint must be module:function",
        ),
        ConformanceCheck(
            "manifest.provenance",
            bool(manifest.provenance.strip()),
            "declared" if manifest.provenance.strip() else "provenance is empty",
        ),
    ]
    selected_version = __version__ if lipas_version is None else lipas_version
    compatibility_ok = False
    compatibility_detail = "lipas_version must be semantic version"
    if isinstance(selected_version, str) and _SEMVER.fullmatch(selected_version):
        current = _semver_key(selected_version)
        minimum = _semver_key(manifest.lipas_min_version)
        maximum = (
            _semver_key(manifest.lipas_max_version)
            if manifest.lipas_max_version is not None else None
        )
        compatibility_ok = current >= minimum and (
            maximum is None or current <= maximum
        )
        compatibility_detail = (
            f"{selected_version} satisfies [{manifest.lipas_min_version}, "
            f"{manifest.lipas_max_version or '∞'}]"
            if compatibility_ok else
            f"{selected_version} outside [{manifest.lipas_min_version}, "
            f"{manifest.lipas_max_version or '∞'}]"
        )
    checks.append(ConformanceCheck(
        "compatibility.lipas",
        compatibility_ok,
        compatibility_detail,
    ))
    connector_contract_ok = not manifest.connector_scope or (
        manifest.requires_approval and manifest.supports_reconciliation
    )
    checks.append(ConformanceCheck(
        "connector.contract",
        connector_contract_ok,
        "not a connector" if not manifest.connector_scope else (
            "scope, approval, and reconciliation declared"
            if connector_contract_ok else
            "connector_scope requires requires_approval=true and "
            "supports_reconciliation=true"
        ),
    ))
    if scenario_names is not None:
        missing = sorted(set(manifest.scenarios) - scenario_names)
        checks.append(ConformanceCheck(
            "scenarios.resolvable",
            not missing,
            "ok" if not missing else f"missing scenarios: {', '.join(missing)}",
        ))
    if skill_names is not None:
        missing = sorted(set(manifest.skills) - skill_names)
        checks.append(ConformanceCheck(
            "skills.resolvable",
            not missing,
            "ok" if not missing else f"missing skills: {', '.join(missing)}",
        ))
    return ConformanceReport(manifest, tuple(checks))


def scaffold_extension(
    path: str | Path,
    name: str,
    *,
    version: str = "0.1.0",
    force: bool = False,
) -> ExtensionManifest:
    """Create a minimal editable extension package and return its manifest."""
    slug = _slug(name)
    root = Path(path).expanduser().resolve()
    if root.exists() and any(root.iterdir()) and not force:
        raise FileExistsError(f"{root} is not empty; pass force=True to replace files")
    root.mkdir(parents=True, exist_ok=True)
    package = root / slug
    package.mkdir(exist_ok=True)
    manifest = ExtensionManifest(
        name=name,
        version=version,
        entrypoint=f"{slug}:build",
    )
    (root / "lipas-extension.json").write_text(
        _json(manifest.as_dict()), encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[build-system]\nrequires = [\"hatchling>=1.25\"]\n"
        "build-backend = \"hatchling.build\"\n\n"
        f"[project]\nname = \"{slug}\"\nversion = \"{version}\"\n"
        "dependencies = [\"lipas>=0.40\"]\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        f"# {name}\n\nGenerated LIPAS extension scaffold.\n",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text(
        "from lipas import ExtensionManifest\n\n"
        "def build():\n    return ()\n",
        encoding="utf-8",
    )
    return manifest


def _slug(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("extension name must be non-empty")
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError("extension name must contain letters or digits")
    return slug.replace("-", "_")


_SEMVER = re.compile(
    r"(?:0|[1-9]\d*)\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?",
)


def _semver_key(value: str) -> tuple[int, int, int, int, str]:
    """Return a small ordering key for the supported semantic-version subset."""
    match = _SEMVER.fullmatch(value)
    if match is None:  # defensive; callers validate before comparing
        raise ValueError(f"invalid semantic version: {value!r}")
    # Build metadata does not participate in semantic-version precedence.
    base = value.split("+", 1)[0]
    if "-" in base:
        core, suffix = base.split("-", 1)
    else:
        core, suffix = base, ""
    major, minor, patch = (int(part) for part in core.split("."))
    # Stable releases sort after prerelease suffixes.  Build metadata does not
    # affect precedence.
    prerelease = 0 if "-" not in base else -1
    return (major, minor, patch, prerelease, suffix)


def _json(value: Mapping[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
