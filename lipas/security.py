"""Product-edge secret policy applied before prompts/actions are persisted."""
from __future__ import annotations

import re
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

__all__ = [
    "EnvironmentSecretResolver",
    "SecretDetected",
    "SecretPolicy",
    "SecretResolutionError",
    "SecretResolver",
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


class SecretResolver(Protocol):
    def resolve(self, reference: str) -> str: ...
    def resolve_arguments(
        self, tool: Any, arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...
    def redact(self, value: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class SecretPolicy:
    """Conservative raw-secret rejection for product ingress.

    This is a leak-prevention layer, not an adversarial security boundary.
    Values such as ``secret://provider/name`` are opaque references and pass;
    the actual secret should be resolved only inside an isolated capability.
    """

    reference_prefix: str = "secret://"

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

    def check(self, value: Any, *, path: str = "$") -> None:
        if isinstance(value, str):
            if value.startswith(self.reference_prefix):
                return
            for reason, pattern in self._VALUE_PATTERNS:
                if pattern.search(value):
                    raise SecretDetected(path, reason)
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                child = f"{path}.{key}"
                if isinstance(key, str) and self._KEY.search(key):
                    if not (
                        isinstance(item, str)
                        and item.startswith(self.reference_prefix)
                    ):
                        raise SecretDetected(child, "sensitive field name")
                self.check(item, path=child)
            return
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            for index, item in enumerate(value):
                self.check(item, path=f"{path}[{index}]")

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
            if value.startswith(self.reference_prefix):
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


def _redact_exact(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED SECRET]")
        return value
    if isinstance(value, Mapping):
        return {key: _redact_exact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_exact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_exact(item, secrets) for item in value)
    return value
