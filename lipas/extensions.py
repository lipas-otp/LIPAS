"""Small SDK for packaging Scenarios/Skills without importing them into core."""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ._version import __version__

__all__ = [
    "ConformanceCheck",
    "ConformanceReport",
    "ExtensionManifest",
    "run_conformance",
    "scaffold_extension",
]


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

    def __post_init__(self) -> None:
        for field_name in (
            "name", "version", "lipas_min_version", "entrypoint", "provenance",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if not _SEMVER.fullmatch(self.version):
            raise ValueError("version must be a semantic version such as 0.1.0")
        if not _SEMVER.fullmatch(self.lipas_min_version):
            raise ValueError(
                "lipas_min_version must be a semantic version such as 0.39.0",
            )
        if self.lipas_max_version is not None and (
            not isinstance(self.lipas_max_version, str)
            or not _SEMVER.fullmatch(self.lipas_max_version)
        ):
            raise ValueError(
                "lipas_max_version must be a semantic version or None",
            )
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
        for field_name in ("requires_approval", "supports_reconciliation"):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be bool")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExtensionManifest":
        if not isinstance(value, Mapping):
            raise TypeError("extension manifest must be a mapping")
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
        }


@dataclass(frozen=True, slots=True)
class ConformanceCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    manifest: ExtensionManifest
    checks: tuple[ConformanceCheck, ...]

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
