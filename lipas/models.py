"""Honest model capability metadata and explicit requirement validation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

__all__ = [
    "CapabilityIssue",
    "ModelCapabilities",
    "ModelCapabilityError",
    "ModelCapabilityReport",
    "ModelRegistry",
    "ModelRequirements",
]


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    provider: str
    model: str
    tool_calling: bool | None = None
    streaming: bool | None = None
    structured_output: bool | None = None
    vision: bool | None = None
    reasoning: bool | None = None
    context_tokens: int | None = None
    local: bool | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("provider", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"ModelCapabilities.{name} must be non-empty")
        for name in (
            "tool_calling", "streaming", "structured_output", "vision",
            "reasoning", "local",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"ModelCapabilities.{name} must be bool or None")
        if self.context_tokens is not None and (
            isinstance(self.context_tokens, bool)
            or not isinstance(self.context_tokens, int)
            or self.context_tokens <= 0
        ):
            raise ValueError("context_tokens must be a positive int or None")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "tool_calling": self.tool_calling,
            "streaming": self.streaming,
            "structured_output": self.structured_output,
            "vision": self.vision,
            "reasoning": self.reasoning,
            "context_tokens": self.context_tokens,
            "local": self.local,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ModelRequirements:
    tool_calling: bool = False
    streaming: bool = False
    structured_output: bool = False
    vision: bool = False
    reasoning: bool = False
    local: bool | None = None
    min_context_tokens: int | None = None
    allow_unknown: bool = False

    def __post_init__(self) -> None:
        for name in (
            "tool_calling", "streaming", "structured_output", "vision",
            "reasoning", "allow_unknown",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"ModelRequirements.{name} must be bool")
        if self.local is not None and not isinstance(self.local, bool):
            raise TypeError("ModelRequirements.local must be bool or None")
        if self.min_context_tokens is not None and (
            isinstance(self.min_context_tokens, bool)
            or not isinstance(self.min_context_tokens, int)
            or self.min_context_tokens <= 0
        ):
            raise ValueError("min_context_tokens must be a positive int or None")


@dataclass(frozen=True, slots=True)
class CapabilityIssue:
    capability: str
    required: Any
    actual: Any
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "required": self.required,
            "actual": self.actual,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ModelCapabilityReport:
    capabilities: ModelCapabilities
    requirements: ModelRequirements
    issues: tuple[CapabilityIssue, ...] = ()

    @property
    def compatible(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "compatible": self.compatible,
            "capabilities": self.capabilities.as_dict(),
            "requirements": {
                name: getattr(self.requirements, name)
                for name in self.requirements.__dataclass_fields__
            },
            "issues": [issue.as_dict() for issue in self.issues],
        }


class ModelCapabilityError(ValueError):
    def __init__(self, report: ModelCapabilityReport) -> None:
        self.report = report
        details = ", ".join(
            f"{item.capability}: {item.reason}" for item in report.issues
        )
        super().__init__(
            f"model {report.capabilities.provider}/{report.capabilities.model} "
            f"does not satisfy requirements ({details})",
        )


class ModelRegistry:
    """Explicit exact/wildcard registry; absent data remains unknown."""

    def __init__(self, entries: Iterable[ModelCapabilities] = ()) -> None:
        self._entries: dict[tuple[str, str], ModelCapabilities] = {}
        for entry in entries:
            self.register(entry)

    @classmethod
    def default(cls) -> "ModelRegistry":
        return cls((
            ModelCapabilities(
                provider="ollama", model="*", streaming=False, local=True,
            ),
            ModelCapabilities(
                provider="ollama", model="gemma4:12b", tool_calling=True,
                streaming=False, local=True,
            ),
            ModelCapabilities(
                provider="openai-responses", model="*", tool_calling=True,
                streaming=True, structured_output=True, local=False,
            ),
            ModelCapabilities(
                provider="openai-compatible", model="*", streaming=False,
                vision=False,
            ),
            ModelCapabilities(
                provider="openai-compatible-stream", model="*", streaming=True,
                vision=False,
            ),
            ModelCapabilities(
                provider="anthropic", model="*", tool_calling=True,
                streaming=False, structured_output=False, local=False,
            ),
        ))

    def register(self, capabilities: ModelCapabilities) -> None:
        if not isinstance(capabilities, ModelCapabilities):
            raise TypeError("ModelRegistry entries must be ModelCapabilities")
        key = (capabilities.provider, capabilities.model)
        if key in self._entries:
            raise ValueError(f"model capability already registered: {key!r}")
        self._entries[key] = capabilities

    def resolve(self, provider: str, model: str) -> ModelCapabilities:
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider must be a non-empty string")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        for key in ((provider, model), (provider, "*"), ("*", model), ("*", "*")):
            value = self._entries.get(key)
            if value is None:
                continue
            if value.provider == provider and value.model == model:
                return value
            return ModelCapabilities(
                provider=provider,
                model=model,
                tool_calling=value.tool_calling,
                streaming=value.streaming,
                structured_output=value.structured_output,
                vision=value.vision,
                reasoning=value.reasoning,
                context_tokens=value.context_tokens,
                local=value.local,
                metadata=value.metadata,
            )
        return ModelCapabilities(provider=provider, model=model)

    def validate(
        self,
        provider: str,
        model: str,
        requirements: ModelRequirements,
    ) -> ModelCapabilityReport:
        if not isinstance(requirements, ModelRequirements):
            raise TypeError("requirements must be ModelRequirements")
        capabilities = self.resolve(provider, model)
        issues: list[CapabilityIssue] = []
        for name in (
            "tool_calling", "streaming", "structured_output", "vision", "reasoning",
        ):
            if not getattr(requirements, name):
                continue
            actual = getattr(capabilities, name)
            if actual is True:
                continue
            if actual is None and requirements.allow_unknown:
                continue
            issues.append(CapabilityIssue(
                name, True, actual,
                "unknown" if actual is None else "adapter reports unsupported",
            ))
        if requirements.local is not None:
            actual = capabilities.local
            if actual != requirements.local and not (
                actual is None and requirements.allow_unknown
            ):
                issues.append(CapabilityIssue(
                    "local", requirements.local, actual,
                    "unknown" if actual is None else "value differs",
                ))
        minimum = requirements.min_context_tokens
        if minimum is not None:
            actual = capabilities.context_tokens
            if (actual is None and not requirements.allow_unknown) or (
                actual is not None and actual < minimum
            ):
                issues.append(CapabilityIssue(
                    "context_tokens", minimum, actual,
                    "unknown" if actual is None else "below required minimum",
                ))
        return ModelCapabilityReport(capabilities, requirements, tuple(issues))

    def require(
        self,
        provider: str,
        model: str,
        requirements: ModelRequirements,
    ) -> ModelCapabilityReport:
        report = self.validate(provider, model, requirements)
        if not report.compatible:
            raise ModelCapabilityError(report)
        return report

    def list(self) -> tuple[ModelCapabilities, ...]:
        return tuple(self._entries.values())
