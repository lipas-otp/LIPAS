"""Explicit live-provider workflow evidence.

The helper in this module is intentionally opt-in.  It wires an ordinary
durable :class:`Agent` invocation to one LIPAS Task/Run and returns a bounded
evidence record.  It does not hide credentials, retry a provider write, or
turn a local fixture into external acceptance evidence.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .execution import ExecutionStateError, ExecutionStore, RunState
from .security import SecretPolicy

__all__ = ["ProviderWorkflowEvidence", "run_provider_workflow"]


def _strict_json(value: Any, name: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{name} must be strict JSON") from exc


@dataclass(frozen=True, slots=True)
class ProviderWorkflowEvidence:
    """Redacted result of one explicitly authorized live-provider workflow."""

    provider: str
    model: str
    task_id: str
    run_id: str
    started_at: float
    finished_at: float
    stop_reason: str
    success: bool
    external: bool = True
    usage: Mapping[str, Any] = field(default_factory=dict)
    error: Mapping[str, Any] | None = None
    evidence_scope: str = "external_provider"
    provider_request_id: str | None = None
    attempt: int = 0

    def __post_init__(self) -> None:
        for name in ("provider", "model", "task_id", "run_id", "stop_reason", "evidence_scope"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"ProviderWorkflowEvidence.{name} must be non-empty")
            object.__setattr__(self, name, value.strip())
        for name in ("started_at", "finished_at"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"ProviderWorkflowEvidence.{name} must be finite")
            object.__setattr__(self, name, float(value))
        if self.finished_at < self.started_at:
            raise ValueError("ProviderWorkflowEvidence.finished_at cannot precede started_at")
        if not isinstance(self.success, bool) or not isinstance(self.external, bool):
            raise TypeError("ProviderWorkflowEvidence flags must be bool")
        if self.evidence_scope not in {"external_provider", "local_fixture"}:
            raise ValueError("evidence_scope must be external_provider or local_fixture")
        if self.external != (self.evidence_scope == "external_provider"):
            raise ValueError("external flag conflicts with evidence_scope")
        if not isinstance(self.usage, Mapping):
            raise TypeError("usage must be a mapping")
        object.__setattr__(self, "usage", _strict_json(dict(self.usage), "usage"))
        if self.error is not None:
            if not isinstance(self.error, Mapping):
                raise TypeError("error must be a mapping or None")
            object.__setattr__(self, "error", _strict_json(dict(self.error), "error"))
        if self.stop_reason == "error" and self.error is None:
            raise ValueError("error stop_reason requires an error mapping")
        if self.stop_reason != "error" and self.error is not None:
            raise ValueError("non-error stop_reason must not include an error mapping")
        if self.provider_request_id is not None:
            if not isinstance(self.provider_request_id, str) or not self.provider_request_id.strip():
                raise ValueError("provider_request_id must be a non-empty string or None")
            provider_request_id = self.provider_request_id.strip()
            if any(ord(char) < 0x20 or ord(char) == 0x7F for char in provider_request_id):
                raise ValueError("provider_request_id must not contain control characters")
            object.__setattr__(self, "provider_request_id", provider_request_id[:256])
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 0
        ):
            raise ValueError("attempt must be a non-negative int")

    @property
    def duration_s(self) -> float:
        return self.finished_at - self.started_at

    @property
    def outcome(self) -> str:
        """Classify terminal evidence for operators without guessing success."""
        if self.success:
            return "succeeded"
        if self.stop_reason == "cancelled":
            return "cancelled"
        if isinstance(self.error, Mapping) and (
            self.error.get("uncertain") is True
            or self.error.get("recovery_required") is True
        ):
            return "uncertain"
        if self.stop_reason == "error":
            return "provider_error"
        return "non_success"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": self.duration_s,
            "outcome": self.outcome,
            "stop_reason": self.stop_reason,
            "success": self.success,
            "external": self.external,
            "usage": dict(self.usage),
            "error": None if self.error is None else dict(self.error),
            "evidence_scope": self.evidence_scope,
            "provider_request_id": self.provider_request_id,
            "attempt": self.attempt,
        }


_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|private[_-]?key|secret|authorization|cookie)",
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{12,}|sk-[A-Za-z0-9_-]{12,}|"
    r"AKIA[A-Z0-9]{16}|-----BEGIN [^-]*PRIVATE KEY-----|"
    r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|private[_-]?key|secret)\s*[:=]\s*\S+)",
)


def _safe_label(value: Any, name: str) -> str:
    """Return bounded provider metadata without exposing control characters."""
    if not isinstance(value, str) or not value.strip():
        return f"unknown-{name}"
    text = value.strip()
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        return f"unknown-{name}"
    if "@" in text and "://" in text or _SECRET_VALUE.search(text):
        return f"redacted-{name}"
    return text[:128]


def _redact_payload(value: Any, *, key: str = "$", depth: int = 0) -> Any:
    """Bound and redact untrusted provider diagnostics before evidence storage."""
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, str):
        if _SECRET_VALUE.search(value):
            return "[REDACTED SECRET]"
        return value[:2048] + ("…" if len(value) > 2048 else "")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= 128:
                result["[TRUNCATED_KEYS]"] = True
                break
            if not isinstance(raw_key, str):
                raw_key = str(raw_key)[:128]
            normalized = raw_key[:128]
            result[normalized] = (
                "[REDACTED SECRET]" if _SENSITIVE_KEY.search(normalized)
                else _redact_payload(item, key=normalized, depth=depth + 1)
            )
        return result
    if isinstance(value, (list, tuple)):
        items = [_redact_payload(item, key=key, depth=depth + 1) for item in value[:128]]
        if len(value) > 128:
            items.append("[TRUNCATED_ITEMS]")
        return items
    return f"[{type(value).__name__} omitted]"


def _normalize_request_id(request_id: str | None, *, provider: str, model: str, prompt: str) -> str:
    if request_id is None:
        return hashlib.sha256(
            f"{provider}\0{model}\0{prompt}".encode("utf-8"),
        ).hexdigest()[:32]
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("request_id must be a non-empty ASCII identifier")
    normalized = request_id.strip()
    if _REQUEST_ID.fullmatch(normalized) is None:
        raise ValueError(
            "request_id must start with an ASCII letter/number and contain "
            "only letters, numbers, '.', '_', ':', or '-'; max 128 chars",
        )
    return normalized


def _event_usage(
    execution: ExecutionStore,
    run_id: str,
    fallback: Mapping[str, Any],
) -> dict[str, Any]:
    """Aggregate usage from durable model-completed events when available."""
    totals: dict[str, float] = {}
    for event in execution.agent_events(run_id):
        if getattr(event, "type", None) != "model_completed":
            continue
        data = event.data if isinstance(event.data, Mapping) else {}
        usage = data.get("usage") if isinstance(data, Mapping) else None
        if not isinstance(usage, Mapping):
            continue
        for key, value in usage.items():
            if not isinstance(key, str) or isinstance(value, bool):
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(numeric) and numeric >= 0:
                totals[key] = totals.get(key, 0.0) + numeric
    if not totals:
        return dict(fallback)
    result: dict[str, Any] = {}
    for key, value in totals.items():
        result[key] = int(value) if value.is_integer() else value
    return result


async def run_provider_workflow(
    agent: Any,
    execution: ExecutionStore,
    prompt: str,
    *,
    workspace: str | Path,
    live: bool = False,
    request_id: str | None = None,
    lease_seconds: float = 300.0,
) -> ProviderWorkflowEvidence:
    """Run one durable Agent prompt against an explicitly live provider.

    ``live=True`` is mandatory to make billing/network intent visible at the
    call site.  ``request_id`` gives retries the same Task/Run identity; when
    omitted, a deterministic digest of the provider/model/prompt is used
    (the prompt itself is never placed in the returned evidence record).
    """
    if not live:
        raise ValueError("run_provider_workflow requires live=True")
    if not hasattr(agent, "run_durable") or not callable(agent.run_durable):
        raise TypeError("agent must provide run_durable")
    if not isinstance(execution, ExecutionStore):
        raise TypeError("execution must be an ExecutionStore")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be non-empty")
    # The Task goal is durable state. Reject obvious raw credentials before
    # creating it, just as ActionGateway does before recording Effect intent.
    SecretPolicy().check(prompt.strip(), path="provider_prompt")
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"workspace must be an existing directory: {root}")
    try:
        lease_numeric = float(lease_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("lease_seconds must be finite and positive") from exc
    if isinstance(lease_seconds, bool) or not math.isfinite(lease_numeric) or lease_numeric <= 0:
        raise ValueError("lease_seconds must be finite and positive")
    raw_provider = getattr(getattr(agent, "adapter", None), "name", None)
    raw_model = getattr(agent, "model", None)
    provider = _safe_label(raw_provider, "provider")
    model = _safe_label(raw_model, "model")
    identity = _normalize_request_id(
        request_id,
        # Hash the raw labels for collision resistance, but never return them
        # in evidence; the public labels above are bounded and redacted.
        provider=str(raw_provider) if raw_provider is not None else "unknown-provider",
        model=str(raw_model) if raw_model is not None else "unknown-model",
        prompt=prompt.strip(),
    )
    task_id = f"task_provider_{identity}"
    run_id = f"run_provider_{identity}"
    task = execution.get_task(task_id)
    if task is None:
        task = execution.create_task(prompt.strip(), root, task_id=task_id)
    elif task.workspace != str(root) or task.goal != prompt.strip():
        raise ValueError("request_id is already bound to a different provider workflow")
    run = execution.get_run(run_id)
    if run is None:
        run = execution.create_run(task.id, run_id=run_id)
    elif run.task_id != task.id:
        raise ValueError("provider workflow Run belongs to another Task")
    # A caller-supplied request id is a durable identity, not merely a cache
    # key.  Once the first checkpoint exists, bind it to the provider/model so
    # a retry cannot accidentally relabel an old result as a different
    # provider's evidence.
    checkpoint = execution.get_checkpoint(run.id)
    provider_digest = hashlib.sha256(
        str(raw_provider if raw_provider is not None else "unknown-provider").encode("utf-8"),
    ).hexdigest()
    model_digest = hashlib.sha256(
        str(raw_model if raw_model is not None else "unknown-model").encode("utf-8"),
    ).hexdigest()
    if checkpoint is not None:
        state = checkpoint.state.get("agent_state")
        metadata = state.get("metadata") if isinstance(state, Mapping) else None
        bound = metadata.get("provider_workflow") if isinstance(metadata, Mapping) else None
        if isinstance(bound, Mapping):
            identity_matches = (
                bound.get("provider_sha256") == provider_digest
                and bound.get("model_sha256") == model_digest
            ) if "provider_sha256" in bound or "model_sha256" in bound else (
                bound.get("provider") == provider and bound.get("model") == model
            )
            if not identity_matches:
                raise ValueError("request_id is already bound to a different provider or model")
        else:
            raise ValueError(
                "request_id already has a durable checkpoint without provider binding",
            )
    started = time.time()
    durable_kwargs: dict[str, Any] = {
        "execution_store": execution,
        "run_id": run.id,
        "lease_seconds": lease_numeric,
    }
    # The built-in Agent accepts this private metadata hook. Keep the public
    # helper duck-typed for small host adapters that expose only the stable
    # ``prompt/execution_store/run_id/lease_seconds`` subset.
    try:
        parameters = inspect.signature(agent.run_durable).parameters
        if "_initial_metadata" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            durable_kwargs["_initial_metadata"] = {
                "provider_workflow": {
                    "provider": provider,
                    "model": model,
                    "provider_sha256": provider_digest,
                    "model_sha256": model_digest,
                },
            }
    except (TypeError, ValueError):
        # Some extension callables do not expose a signature. Calling them
        # with the stable arguments remains preferable to rejecting a valid
        # provider integration up front.
        pass
    result = await agent.run_durable(
        # A worker may die after Run creation but before its first
        # checkpoint. An expired running lease is reclaimable and still needs
        # the original prompt; once a checkpoint exists, resume exclusively
        # from durable state to avoid appending the prompt twice.
        prompt.strip()
        if checkpoint is None and run.state not in {
            RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED,
        }
        else None,
        **durable_kwargs,
    )
    finished = time.time()
    metadata = result.metadata if isinstance(result.metadata, Mapping) else {}
    usage = metadata.get("usage", {})
    usage_payload = _redact_payload(usage, key="usage") if isinstance(usage, Mapping) else {}
    if isinstance(usage_payload, Mapping):
        usage_payload = _event_usage(execution, run.id, usage_payload)
    error = _redact_payload(result.error, key="error") if result.error is not None else None
    success = result.stop_reason == "natural_stop"
    provider_request_id = metadata.get("provider_request_id", metadata.get("request_id"))
    if isinstance(provider_request_id, str):
        provider_request_id = _redact_payload(provider_request_id, key="provider_request_id")
    settled = execution.get_run(run.id)
    if settled is None or settled.state not in {
        RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED,
    }:
        raise ExecutionStateError(
            f"provider workflow returned before Run {run.id!r} reached a terminal state",
        )
    return ProviderWorkflowEvidence(
        provider,
        model,
        task.id,
        run.id,
        started,
        finished,
        result.stop_reason,
        success,
        usage=usage_payload if isinstance(usage_payload, Mapping) else {},
        error=error,
        provider_request_id=(
            provider_request_id if isinstance(provider_request_id, str) else None
        ),
        attempt=0 if settled is None else settled.attempt,
    )
