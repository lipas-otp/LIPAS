"""Product-edge secret policy applied before prompts/actions are persisted."""
from __future__ import annotations

import re
import os
import json
import ssl
import stat
import hashlib
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

__all__ = [
    "EnvironmentSecretResolver",
    "SecretDetected",
    "SecretPolicy",
    "SecretResolutionError",
    "SecretResolver",
    "ManagedSecretResolver",
    "FileSecretResolver",
    "TLSConfig",
]


class SecretDetected(ValueError):
    """Potential raw secret was rejected without echoing its value."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(
            f"potential raw secret at {path}; pass a secret reference instead "
            f"({reason})",
        )


class SecretResolutionError(RuntimeError):
    """An opaque secret reference cannot be resolved safely."""


_SECRET_ROTATION_LOCK = threading.RLock()


class SecretResolver(Protocol):
    def resolve(self, reference: str) -> str: ...
    def resolve_arguments(
        self, tool: Any, arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...
    def redact(self, value: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class ManagedSecretResolver:
    """Adapter for an operator-owned KMS, HSM, or secret-manager client.

    LIPAS stores only opaque references.  The injected resolver is called at
    execution time and its result is never written to the Claim/Effect tape.
    A provider may inject ``redactor`` to apply deployment-specific masking.
    Without one, a bounded in-memory exact-value redactor is used; no value is
    guessed or fetched merely for redaction.
    """

    resolve_reference: Callable[[str], str]
    redactor: Callable[[Any], Any] | None = None
    allowed_prefixes: frozenset[str] = frozenset({"secret://"})
    _resolved_values: dict[str, str] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )

    def __init__(
        self,
        resolve_reference: Callable[[str], str],
        *,
        redactor: Callable[[Any], Any] | None = None,
        allowed_prefixes: Sequence[str] = ("secret://",),
    ) -> None:
        if not callable(resolve_reference):
            raise TypeError("resolve_reference must be callable")
        if redactor is not None and not callable(redactor):
            raise TypeError("redactor must be callable or None")
        if isinstance(allowed_prefixes, (str, bytes, bytearray)):
            raise TypeError("allowed_prefixes must be a sequence of strings")
        prefixes = frozenset(allowed_prefixes)
        if not prefixes or any(not isinstance(item, str) or not item for item in prefixes):
            raise ValueError("allowed_prefixes must contain non-empty strings")
        object.__setattr__(self, "resolve_reference", resolve_reference)
        object.__setattr__(self, "redactor", redactor)
        object.__setattr__(self, "allowed_prefixes", prefixes)
        object.__setattr__(self, "_resolved_values", {})

    def resolve(self, reference: str) -> str:
        if not isinstance(reference, str) or not any(
            reference.startswith(prefix) and len(reference) > len(prefix)
            for prefix in self.allowed_prefixes
        ) or any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in reference):
            raise SecretResolutionError("unsupported managed secret reference")
        try:
            value = self.resolve_reference(reference)
        except SecretResolutionError:
            raise
        except Exception as exc:
            # Do not surface provider exception text: it may contain a
            # credential, request URL, or account metadata.
            raise SecretResolutionError("managed secret lookup failed") from exc
        if not isinstance(value, str) or not value:
            raise SecretResolutionError("managed secret lookup returned no value")
        # Even without a deployment-specific redactor, keep a bounded
        # in-memory exact-value set so provider output and exception messages
        # cannot write a resolved credential into the durable Effect tape.
        with _SECRET_ROTATION_LOCK:
            self._resolved_values[reference] = value
            while len(self._resolved_values) > 128:
                self._resolved_values.pop(next(iter(self._resolved_values)))
        return value

    def resolve_value(self, value: Any) -> Any:
        """Resolve references in a detached argument tree.

        The cycle guard matters when a host passes a mutable mapping directly
        to a gateway.  Without it, a malformed self-reference could recurse
        forever before the normal :class:`SecretPolicy` gets a chance to
        reject the request.
        """
        return self._resolve_value(value, set())

    def _resolve_value(self, value: Any, active: set[int]) -> Any:
        if isinstance(value, str):
            # Use the configured namespace rather than a hard-coded
            # ``secret://`` check. Deployments commonly expose references such
            # as ``vault://`` or ``kms://``; treating those as ordinary
            # strings would silently send an unresolved reference to a tool.
            return self.resolve(value) if any(
                value.startswith(prefix) for prefix in self.allowed_prefixes
            ) else value
        if isinstance(value, Mapping):
            identity = id(value)
            if identity in active:
                raise SecretResolutionError("managed secret arguments contain a cycle")
            active.add(identity)
            try:
                return {
                    key: self._resolve_value(item, active)
                    for key, item in value.items()
                }
            finally:
                active.remove(identity)
        if isinstance(value, list):
            identity = id(value)
            if identity in active:
                raise SecretResolutionError("managed secret arguments contain a cycle")
            active.add(identity)
            try:
                return [self._resolve_value(item, active) for item in value]
            finally:
                active.remove(identity)
        if isinstance(value, tuple):
            identity = id(value)
            if identity in active:
                raise SecretResolutionError("managed secret arguments contain a cycle")
            active.add(identity)
            try:
                return tuple(self._resolve_value(item, active) for item in value)
            finally:
                active.remove(identity)
        return value

    def resolve_arguments(
        self, _tool: Any, arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        resolved = self.resolve_value(arguments)
        if not isinstance(resolved, Mapping):
            raise SecretResolutionError("managed secret arguments must be a mapping")
        return resolved

    def redact(self, value: Any) -> Any:
        if self.redactor is not None:
            try:
                return self.redactor(value)
            except Exception as exc:
                raise SecretResolutionError("managed secret redaction failed") from exc
        with _SECRET_ROTATION_LOCK:
            secrets = tuple(self._resolved_values.values())
        return _redact_exact(value, secrets)


@dataclass(frozen=True, slots=True)
class SecretPolicy:
    """Conservative raw-secret rejection for product ingress.

    This is a leak-prevention layer, not an adversarial security boundary.
    Values such as ``secret://provider/name`` are opaque references and pass;
    the actual secret should be resolved only inside an isolated capability.
    """

    reference_prefix: str = "secret://"
    reference_prefixes: tuple[str, ...] | None = None

    _KEY = re.compile(
        r"(?i)(?:^|[_-])(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
        r"client[_-]?secret|password|private[_-]?key|secret)(?:$|[_-])",
    )
    _VALUE_PATTERNS = (
        ("private key", re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----")),
        ("bearer token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")),
        ("provider token", re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")),
        ("cloud access key", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
        (
            "secret assignment",
            re.compile(
                r"(?im)^\s*(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
                r"client[_-]?secret|password|private[_-]?key|secret)"
                r"[\w.-]*\s*[:=]\s*\S+",
            ),
        ),
    )

    def __post_init__(self) -> None:
        if not isinstance(self.reference_prefix, str) or not self.reference_prefix:
            raise ValueError("reference_prefix must be a non-empty string")
        raw = self.reference_prefixes
        prefixes: tuple[str, ...]
        if raw is None:
            prefixes = (self.reference_prefix,)
        else:
            if isinstance(raw, (str, bytes, bytearray)):
                raise TypeError("reference_prefixes must be a sequence of strings")
            prefixes = tuple(raw)
            if not prefixes:
                raise ValueError("reference_prefixes must not be empty")
            if any(not isinstance(item, str) or not item for item in prefixes):
                raise ValueError("reference_prefixes must contain non-empty strings")
            # Keep the historical single-prefix attribute canonical for
            # callers that inspect it directly.
            object.__setattr__(self, "reference_prefix", prefixes[0])
        object.__setattr__(self, "reference_prefixes", tuple(dict.fromkeys(prefixes)))

    def _is_reference(self, value: str) -> bool:
        return any(value.startswith(prefix) for prefix in self.reference_prefixes or ())

    def check(
        self,
        value: Any,
        *,
        path: str = "$",
        _active: set[int] | None = None,
    ) -> None:
        if _active is None:
            _active = set()
        if isinstance(value, str):
            if self._is_reference(value):
                return
            for reason, pattern in self._VALUE_PATTERNS:
                if pattern.search(value):
                    raise SecretDetected(path, reason)
            return
        if isinstance(value, Mapping):
            identity = id(value)
            if identity in _active:
                raise ValueError(f"secret policy input contains a reference cycle at {path}")
            _active.add(identity)
            try:
                for key, item in value.items():
                    child = f"{path}.{key}"
                    if isinstance(key, str) and self._KEY.search(key):
                        if not (
                            isinstance(item, str)
                            and self._is_reference(item)
                        ):
                            raise SecretDetected(child, "sensitive field name")
                    self.check(item, path=child, _active=_active)
            finally:
                _active.remove(identity)
            return
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            identity = id(value)
            if identity in _active:
                raise ValueError(f"secret policy input contains a reference cycle at {path}")
            _active.add(identity)
            try:
                for index, item in enumerate(value):
                    self.check(item, path=f"{path}[{index}]", _active=_active)
            finally:
                _active.remove(identity)

    def check_tool_arguments(
        self, tool_name: str, arguments: Mapping[str, Any],
    ) -> None:
        self.check(arguments, path=f"tool:{tool_name}")

    def references(self, value: Any) -> tuple[str, ...]:
        found: list[str] = []
        self._collect_references(value, found)
        return tuple(found)

    def _collect_references(self, value: Any, found: list[str]) -> None:
        if isinstance(value, str):
            if self._is_reference(value):
                found.append(value)
            return
        if isinstance(value, Mapping):
            for item in value.values():
                self._collect_references(item, found)
            return
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            for item in value:
                self._collect_references(item, found)


@dataclass(frozen=True, slots=True)
class EnvironmentSecretResolver:
    """Resolve allowlisted ``secret://env/NAME`` references at execution time."""

    allowed_names: frozenset[str]
    environ: Mapping[str, str] = field(
        default_factory=lambda: os.environ,
        repr=False,
        compare=False,
    )

    def __init__(
        self,
        allowed_names: Sequence[str],
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        names = frozenset(allowed_names)
        if not names or any(
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
            for name in names
        ):
            raise ValueError("allowed_names must contain valid environment names")
        object.__setattr__(self, "allowed_names", names)
        object.__setattr__(self, "environ", os.environ if environ is None else environ)

    def resolve(self, reference: str) -> str:
        prefix = "secret://env/"
        if not reference.startswith(prefix):
            raise SecretResolutionError("unsupported secret reference provider")
        name = reference[len(prefix):]
        if name not in self.allowed_names:
            raise SecretResolutionError(
                f"environment secret {name!r} is not in the resolver allowlist",
            )
        value = self.environ.get(name)
        if value is None or value == "":
            raise SecretResolutionError(
                f"environment secret {name!r} is unavailable",
            )
        return value

    def resolve_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.resolve(value) if value.startswith("secret://") else value
        if isinstance(value, Mapping):
            return {key: self.resolve_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.resolve_value(item) for item in value)
        return value

    def resolve_arguments(
        self, _tool: Any, arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        resolved = self.resolve_value(arguments)
        assert isinstance(resolved, Mapping)
        return resolved

    def redact(self, value: Any) -> Any:
        secrets = tuple(
            secret for name in self.allowed_names
            if (secret := self.environ.get(name))
        )
        return _redact_exact(value, secrets)


def _redact_exact(
    value: Any,
    secrets: Sequence[str],
    *,
    _active: set[int] | None = None,
    _depth: int = 0,
) -> Any:
    if _depth > 32:
        return "[TRUNCATED]"
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED SECRET]")
        return value
    if _active is None:
        _active = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in _active:
            return "[CYCLE]"
        _active.add(identity)
        try:
            return {
                key: _redact_exact(item, secrets, _active=_active, _depth=_depth + 1)
                for key, item in value.items()
            }
        finally:
            _active.remove(identity)
    if isinstance(value, list):
        identity = id(value)
        if identity in _active:
            return "[CYCLE]"
        _active.add(identity)
        try:
            return [
                _redact_exact(item, secrets, _active=_active, _depth=_depth + 1)
                for item in value
            ]
        finally:
            _active.remove(identity)
    if isinstance(value, tuple):
        identity = id(value)
        if identity in _active:
            return "[CYCLE]"
        _active.add(identity)
        try:
            return tuple(
                _redact_exact(item, secrets, _active=_active, _depth=_depth + 1)
                for item in value
            )
        finally:
            _active.remove(identity)
    return value


@dataclass(frozen=True, slots=True)
class FileSecretResolver:
    """Resolve ``secret://file/NAME`` references from a local secret file.

    The file is intentionally a small, operator-owned JSON object.  It is
    protected by an atomic replace and restrictive permissions (0600 on
    POSIX).  This is suitable for a single local workspace; teams that need
    HSM/KMS custody should provide their own :class:`SecretResolver` instead
    of pretending that a plaintext file is a hardware-backed vault.
    """

    path: Path
    allowed_names: frozenset[str] | None = None

    def __init__(
        self,
        path: str | Path,
        *,
        allowed_names: Sequence[str] | None = None,
    ) -> None:
        raw_target = Path(path).expanduser()
        if raw_target.is_symlink():
            raise SecretResolutionError("secret file must not be a symbolic link")
        target = raw_target.resolve()
        if isinstance(allowed_names, (str, bytes, bytearray)):
            raise TypeError("allowed_names must be a sequence of names")
        names = None if allowed_names is None else frozenset(allowed_names)
        if names is not None and (
            not names
            or any(
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", name) is None
                for name in names
            )
        ):
            raise ValueError("allowed_names must contain valid secret names")
        object.__setattr__(self, "path", target)
        object.__setattr__(self, "allowed_names", names)
        self._validate_path()

    def _validate_path(self) -> None:
        if self.path.exists():
            if self.path.is_symlink() or not self.path.is_file():
                raise SecretResolutionError("secret file must be a regular file")
            # A secret file readable by group/other would violate the local
            # single-workspace custody promise.  Windows does not expose
            # useful POSIX mode bits, so this check is naturally a no-op there.
            if os.name == "posix" and stat.S_IMODE(self.path.stat().st_mode) & 0o077:
                raise SecretResolutionError("secret file permissions must be 0600")

    def _load(self) -> dict[str, str]:
        self._validate_path()
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecretResolutionError("secret file is not valid UTF-8 JSON") from exc
        if not isinstance(raw, Mapping):
            raise SecretResolutionError("secret file must contain a JSON object")
        result: dict[str, str] = {}
        for name, value in raw.items():
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", name) is None
                or not isinstance(value, str)
                or not value
            ):
                raise SecretResolutionError("secret file contains an invalid entry")
            result[name] = value
        return result

    def resolve(self, reference: str) -> str:
        prefix = "secret://file/"
        if not isinstance(reference, str) or not reference.startswith(prefix):
            raise SecretResolutionError("unsupported secret reference provider")
        name = reference[len(prefix):]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", name) is None:
            raise SecretResolutionError("secret reference name is invalid")
        if self.allowed_names is not None and name not in self.allowed_names:
            raise SecretResolutionError(f"secret {name!r} is not in the resolver allowlist")
        value = self._load().get(name)
        if value is None:
            raise SecretResolutionError(f"secret {name!r} is unavailable")
        return value

    def rotate(self, name: str, value: str) -> str:
        """Atomically create or replace one secret and return its reference."""
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", name or "") is None:
            raise ValueError("secret name is invalid")
        if not isinstance(value, str) or not value:
            raise ValueError("secret value must be a non-empty string")
        if self.allowed_names is not None and name not in self.allowed_names:
            raise SecretResolutionError(f"secret {name!r} is not in the resolver allowlist")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The atomic replace protects readers, while this lock protects the
        # read-modify-write sequence from two operators rotating different
        # names at the same time (which would otherwise lose one update).
        with _SECRET_ROTATION_LOCK:
            lock_fd: int | None = None
            lock_path = self.path.with_name(f".{self.path.name}.lock")
            try:
                if lock_path.is_symlink():
                    raise SecretResolutionError("secret lock must not be a symbolic link")
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                if os.name == "posix":
                    os.chmod(lock_path, 0o600)
                    try:
                        import fcntl
                        fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    except ImportError:  # pragma: no cover - non-POSIX fallback
                        pass
                entries = self._load()
                entries[name] = value
                encoded = json.dumps(
                    entries, sort_keys=True, ensure_ascii=False, indent=2,
                ) + "\n"
                fd, temporary_name = tempfile.mkstemp(
                    prefix=f".{self.path.name}.", dir=str(self.path.parent), text=True,
                )
                temporary = Path(temporary_name)
                try:
                    if os.name == "posix":
                        os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    temporary.replace(self.path)
                    if os.name == "posix":
                        os.chmod(self.path, 0o600)
                finally:
                    temporary.unlink(missing_ok=True)
            finally:
                if lock_fd is not None:
                    if os.name == "posix":
                        try:
                            import fcntl
                            fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        except ImportError:  # pragma: no cover - non-POSIX fallback
                            pass
                    os.close(lock_fd)
        return f"secret://file/{name}"

    def resolve_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.resolve(value) if value.startswith("secret://") else value
        if isinstance(value, Mapping):
            return {key: self.resolve_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.resolve_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.resolve_value(item) for item in value)
        return value

    def resolve_arguments(self, _tool: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        resolved = self.resolve_value(arguments)
        assert isinstance(resolved, Mapping)
        return resolved

    def redact(self, value: Any) -> Any:
        entries = self._load()
        return _redact_exact(value, tuple(entries.values()))


@dataclass(frozen=True, slots=True)
class TLSConfig:
    """Explicit TLS material for local Web and remote Worker servers.

    TLS is deliberately configured with files, not raw PEM strings, so key
    material does not enter durable Runtime metadata or logs.  The default
    minimum is TLS 1.2; callers may provide a stricter minimum to their own
    SSL context when required by policy.
    """

    certfile: Path
    keyfile: Path
    cafile: Path | None = None
    require_client_certificate: bool = False
    minimum_version: ssl.TLSVersion = ssl.TLSVersion.TLSv1_2

    def __init__(
        self,
        certfile: str | Path,
        keyfile: str | Path,
        *,
        cafile: str | Path | None = None,
        require_client_certificate: bool = False,
        minimum_version: ssl.TLSVersion = ssl.TLSVersion.TLSv1_2,
    ) -> None:
        raw_cert = Path(certfile).expanduser()
        raw_key = Path(keyfile).expanduser()
        raw_ca = None if cafile is None else Path(cafile).expanduser()
        if any(path is not None and path.is_symlink() for path in (raw_cert, raw_key, raw_ca)):
            raise ValueError("TLS material must not be a symbolic link")
        cert = raw_cert.resolve()
        key = raw_key.resolve()
        ca = None if raw_ca is None else raw_ca.resolve()
        for label, path in (("certificate", cert), ("private key", key), ("CA", ca)):
            if path is None:
                continue
            if not path.is_file():
                raise ValueError(f"TLS {label} file must be a regular file")
        if os.name == "posix" and stat.S_IMODE(key.stat().st_mode) & 0o077:
            raise ValueError("TLS private key permissions must not expose group/other access")
        if not isinstance(require_client_certificate, bool):
            raise TypeError("require_client_certificate must be bool")
        if not isinstance(minimum_version, ssl.TLSVersion):
            raise TypeError("minimum_version must be ssl.TLSVersion")
        if minimum_version < ssl.TLSVersion.TLSv1_2:
            raise ValueError("TLS minimum_version must be TLS 1.2 or newer")
        object.__setattr__(self, "certfile", cert)
        object.__setattr__(self, "keyfile", key)
        object.__setattr__(self, "cafile", ca)
        object.__setattr__(self, "require_client_certificate", require_client_certificate)
        object.__setattr__(self, "minimum_version", minimum_version)

    def server_context(self) -> ssl.SSLContext:
        self._validate_runtime_material()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = self.minimum_version
        context.load_cert_chain(str(self.certfile), str(self.keyfile))
        if self.cafile is not None:
            context.load_verify_locations(cafile=str(self.cafile))
            if self.require_client_certificate:
                context.verify_mode = ssl.CERT_REQUIRED
        elif self.require_client_certificate:
            raise ValueError("cafile is required for mutual TLS")
        return context

    def certificate_fingerprint(self) -> str:
        """Return the SHA-256 fingerprint of the configured certificate.

        The fingerprint is safe operational metadata: it never exposes the
        private key and gives rotation/audit tooling a stable value to record
        before and after a reload.
        """
        self._validate_runtime_material(check_key=False)
        try:
            payload = self.certfile.read_bytes()
        except OSError as exc:
            raise ValueError("TLS certificate cannot be read") from exc
        return hashlib.sha256(payload).hexdigest()

    def client_context(self) -> ssl.SSLContext:
        if self.cafile is not None and (
            self.cafile.is_symlink() or not self.cafile.is_file()
        ):
            raise ValueError("TLS CA file must be a regular file")
        context = ssl.create_default_context(cafile=None if self.cafile is None else str(self.cafile))
        context.minimum_version = self.minimum_version
        return context

    def _validate_runtime_material(self, *, check_key: bool = True) -> None:
        """Re-check files immediately before loading them after rotation."""
        if self.certfile.is_symlink() or not self.certfile.is_file():
            raise ValueError("TLS certificate file must be a regular file")
        if check_key:
            if self.keyfile.is_symlink() or not self.keyfile.is_file():
                raise ValueError("TLS private key file must be a regular file")
            if os.name == "posix" and stat.S_IMODE(self.keyfile.stat().st_mode) & 0o077:
                raise ValueError("TLS private key permissions must not expose group/other access")
        if self.cafile is not None and (self.cafile.is_symlink() or not self.cafile.is_file()):
            raise ValueError("TLS CA file must be a regular file")
