"""Framework-neutral reliable action gateway.

This is the stable integration seam between the independent LIPAS product and
other agent/orchestration systems.  External callers supply a stable request
id; LIPAS maps it to an Effect id, applies approval/budget/guard policy, and
returns the previously recorded terminal result on safe redelivery.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .calculus import Claim
from .rows import RowSet
from .rows.capability import CapabilityRow
from .rows.effect import (
    EffectRow, F_DETAIL, F_ERROR, F_OUTPUT, F_REASON, F_STATUS,
)
from .rows.history import HistoryRow
from .session import open_session
from .store import ClaimStore
from .tool_harness import ToolHarness
from .tools import SideEffectClass, Tool, ToolRegistry
from .security import SecretPolicy, SecretResolutionError, SecretResolver

__all__ = ["ActionGateway", "ActionResult", "ActionSpec"]


@dataclass(frozen=True, slots=True)
class ActionSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    side_effect: str

    @property
    def requires_approval(self) -> bool:
        return self.side_effect in {
            SideEffectClass.IDEMPOTENT_WRITE.value,
            SideEffectClass.EXTERNAL_WRITE.value,
        }


@dataclass(frozen=True, slots=True)
class ActionResult:
    request_id: str
    effect_id: str
    tool_name: str
    status: str
    content: str
    output: Any | None = None
    detail: Mapping[str, Any] | None = None

    @property
    def is_error(self) -> bool:
        return self.status != "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "effect_id": self.effect_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "content": self.content,
            "output": self.output,
            "detail": dict(self.detail) if self.detail is not None else None,
        }


class ActionGateway:
    """Serialize audited tool calls behind one framework-neutral API.

    ``allow_writes=False`` is fail-closed. Integrations should only set it to
    true when their trusted host has already completed a human approval or the
    gateway itself is running inside an operator-approved policy boundary.
    """

    def __init__(
        self,
        tools: ToolRegistry | Iterable[Tool],
        *,
        session: str | Path | None = None,
        budgets: Mapping[str, float] | None = None,
        guards: Sequence[Any] = (),
        allow_writes: bool = False,
        default_timeout_s: float | None = 300.0,
        secret_policy: SecretPolicy | None = None,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        self.tools = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools)
        if session is None:
            self.rowset = RowSet(ClaimStore(), [
                HistoryRow(), CapabilityRow(budgets=dict(budgets or {})), EffectRow(),
            ])
        else:
            self.rowset = open_session(session, budgets=budgets)
        self.secret_resolver = secret_resolver
        self.harness = ToolHarness(
            tools=self.tools,
            rowset=self.rowset,
            guards=tuple(guards),
            argument_resolver=(
                getattr(secret_resolver, "resolve_arguments")
                if secret_resolver is not None else None
            ),
            result_sanitizer=(secret_resolver.redact if secret_resolver else None),
        )
        if not isinstance(allow_writes, bool):
            raise TypeError("allow_writes must be bool")
        self.allow_writes = allow_writes
        self.default_timeout_s = self._timeout(default_timeout_s)
        if secret_policy is None:
            # Keep the ingress policy in lock-step with a managed resolver's
            # namespace.  Otherwise a perfectly valid ``vault://`` reference
            # would either be rejected as a raw sensitive value or bypass the
            # resolver and reach the tool unchanged.
            prefixes = getattr(secret_resolver, "allowed_prefixes", None)
            self.secret_policy = (
                SecretPolicy(reference_prefixes=tuple(sorted(prefixes)))
                if prefixes is not None else SecretPolicy()
            )
        else:
            self.secret_policy = secret_policy
        self._lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Task[Any]] = {}

    def specs(self) -> tuple[ActionSpec, ...]:
        return tuple(
            ActionSpec(
                name=value.name,
                description=value.description,
                input_schema=dict(value.parameters_schema),
                side_effect=value.side_effect.value,
            )
            for value in self.tools
        )

    async def call(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        request_id: str | None = None,
        approved: bool = False,
        timeout_s: float | None = None,
        caused_by: str | None = None,
    ) -> ActionResult:
        if request_id is None:
            request_id = f"request_{uuid.uuid4().hex}"
        elif not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        else:
            request_id = request_id.strip()
        if caused_by is not None and (
            not isinstance(caused_by, str) or not caused_by.strip()
        ):
            raise ValueError("caused_by must be a non-empty string or None")
        if caused_by is not None:
            caused_by = caused_by.strip()
        if not isinstance(arguments, Mapping):
            raise TypeError("arguments must be a mapping")
        # This must happen before approval/audit Claims so a rejected raw
        # secret never enters the persistent Effect tape.
        self.secret_policy.check_tool_arguments(tool_name, arguments)
        references = self.secret_policy.references(arguments)
        if references and self.secret_resolver is None:
            raise SecretResolutionError(
                "secret references require an explicit resolver",
            )
        effect_id = self.effect_id(request_id)
        tool = self.tools.get(tool_name)
        # Approval is itself a durable fact. Bind it to the complete request
        # shape so a caller cannot approve one payload and then reuse the same
        # request id for a different write (especially across a process
        # restart, where an in-memory idempotency map would be insufficient).
        request_fingerprint = self._request_fingerprint(tool_name, arguments)
        self._check_prior_approval(
            effect_id,
            tool_name=tool_name,
            request_fingerprint=request_fingerprint,
            caused_by=caused_by if caused_by is not None else request_id,
        )
        if (
            tool.side_effect in {
                SideEffectClass.IDEMPOTENT_WRITE,
                SideEffectClass.EXTERNAL_WRITE,
            }
            and not (self.allow_writes or approved)
        ):
            fields = {
                "request_id": request_id,
                "effect_id": effect_id,
                "tool_name": tool_name,
                "side_effect": tool.side_effect.value,
                "arguments_sha256": request_fingerprint,
                "caused_by": caused_by if caused_by is not None else request_id,
            }
            self.rowset.fold(Claim(
                tag="gateway_approval_required",
                fields=fields,
                source="action_gateway",
                claim_id=f"approval_{effect_id}",
            ))
            return ActionResult(
                request_id=request_id,
                effect_id=effect_id,
                tool_name=tool_name,
                status="approval_required",
                content=f"approval required for {tool.side_effect.value} action",
                detail={"side_effect": tool.side_effect.value},
            )

        deadline = self.default_timeout_s if timeout_s is None else self._timeout(timeout_s)
        # Protect only task admission. Never hold the gateway-wide lock while
        # a tool is running: unrelated requests must be able to progress in
        # parallel, while the per-effect task still deduplicates redelivery.
        async with self._lock:
            task = self._inflight.get(effect_id)
            if task is None:
                task = asyncio.create_task(self.harness.call(
                    tool_name=tool_name,
                    arguments=arguments,
                    effect_id=effect_id,
                    tool_use_id=effect_id,
                    caused_by=caused_by if caused_by is not None else request_id,
                ))
                self._inflight[effect_id] = task

                def _forget(done: asyncio.Task[Any]) -> None:
                    if self._inflight.get(effect_id) is done:
                        self._inflight.pop(effect_id, None)
                    # A harness failure is represented by the orphaned intent;
                    # consuming the exception prevents an unhandled-task log.
                    if not done.cancelled():
                        with contextlib.suppress(BaseException):
                            done.exception()

                task.add_done_callback(_forget)
        try:
            tool_result = (
                await task if deadline is None
                else await asyncio.wait_for(asyncio.shield(task), timeout=deadline)
            )
        except TimeoutError:
            return ActionResult(
                request_id=request_id,
                effect_id=effect_id,
                tool_name=tool_name,
                status="uncertain",
                content=(
                    f"action exceeded {deadline:g}s; its intent remains "
                    "uncertain while the isolated call converges"
                ),
                detail={"timeout_s": deadline, "orphan": True},
            )
        return self._result_from_effect(
            request_id, effect_id, tool_name, tool_result,
        )

    def call_sync(self, *args: Any, **kwargs: Any) -> ActionResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.call(*args, **kwargs))
        raise RuntimeError("call_sync cannot run inside an active event loop")

    def reconcile_orphan(
        self,
        request_id: str,
        *,
        output: Any = None,
        error: Mapping[str, Any] | None = None,
        wall_seconds: float = 0.0,
    ) -> ActionResult:
        """Record an operator/provider observation for a timed-out action."""
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        effect_id = self.effect_id(request_id)
        result = self.harness.reconcile_orphan(
            effect_id,
            output=output,
            error=error,
            wall_seconds=wall_seconds,
        )
        tool_name = next(
            (
                node.intent.fields.get("tool_name")
                for node in self._effect_nodes()
                if node.effect_id == effect_id and node.intent is not None
            ),
            None,
        )
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise KeyError(request_id)
        tool = self.tools.get(tool_name)
        return self._result_from_effect(request_id, effect_id, tool.name, result)

    def _effect_nodes(self):
        effect_row = next(
            value for value in self.rowset.rows if isinstance(value, EffectRow)
        )
        return effect_row.project(self.rowset.store).nodes.values()

    def _check_prior_approval(
        self,
        effect_id: str,
        *,
        tool_name: str,
        request_fingerprint: str,
        caused_by: str,
    ) -> None:
        claim_id = f"approval_{effect_id}"
        for claim in self.rowset.store.filter(tag="gateway_approval_required"):
            if claim.claim_id != claim_id:
                continue
            fields = claim.fields
            if fields.get("tool_name") != tool_name:
                raise ValueError(
                    f"request id {effect_id!r} was reused for a different tool",
                )
            recorded = fields.get("arguments_sha256")
            # Legacy approval claims predate the payload binding. They are
            # still safe to read, but cannot be safely approved for a changed
            # payload; require a fresh request identity instead.
            if recorded != request_fingerprint:
                raise ValueError(
                    f"request id {effect_id!r} was reused for different arguments",
                )
            if fields.get("caused_by") != caused_by:
                raise ValueError(
                    f"request id {effect_id!r} was reused with different causation",
                )
            return

    @staticmethod
    def _request_fingerprint(
        tool_name: str, arguments: Mapping[str, Any],
    ) -> str:
        _validate_json_shape(arguments, "action arguments")
        try:
            payload = json.dumps(
                {"tool_name": tool_name, "arguments": dict(arguments)},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("action arguments must be strict JSON") from exc
        return hashlib.sha256(payload).hexdigest()
    def close(self) -> None:
        close = getattr(self.rowset.store, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "ActionGateway":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @staticmethod
    def effect_id(request_id: str) -> str:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:12]
        return f"tool_{digest}"

    @staticmethod
    def _timeout(value: float | None) -> float | None:
        if value is None:
            return None
        try:
            valid = (
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(float(value))
                and value > 0
            )
        except (OverflowError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("timeout must be a positive finite number or None")
        return float(value)

    def _result_from_effect(
        self,
        request_id: str,
        effect_id: str,
        tool_name: str,
        tool_result: Mapping[str, Any],
    ) -> ActionResult:
        effect_row = next(
            value for value in self.rowset.rows if isinstance(value, EffectRow)
        )
        node = effect_row.project(self.rowset.store).nodes[effect_id]
        if node.rejection is not None:
            detail = node.rejection.fields.get(F_DETAIL)
            reason = str(node.rejection.fields.get(F_REASON, "rejected"))
            return ActionResult(
                request_id=request_id, effect_id=effect_id, tool_name=tool_name,
                status="rejected", content=str(tool_result.get("content", "")),
                detail={
                    "reason": reason,
                    **(dict(detail) if isinstance(detail, Mapping) else {}),
                },
            )
        assert node.result is not None
        fields = node.result.fields
        status = "ok" if fields.get(F_STATUS) == "ok" else "error"
        output = fields.get(F_OUTPUT)
        detail = fields.get(F_ERROR)
        return ActionResult(
            request_id=request_id,
            effect_id=effect_id,
            tool_name=tool_name,
            status=status,
            content=str(tool_result.get("content", "")),
            output=output,
            detail=dict(detail) if isinstance(detail, Mapping) else None,
        )


def _validate_json_shape(
    value: Any,
    path: str,
    *,
    active: set[int] | None = None,
) -> None:
    """Validate the complete argument tree before deriving replay identity.

    Python's JSON encoder silently stringifies integer mapping keys.  A
    request fingerprint must instead describe exactly one canonical payload,
    so nested keys, finite numbers, and reference cycles are checked before
    serialization.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain finite numbers")
        return
    if not isinstance(value, (list, tuple, Mapping)):
        raise TypeError(f"{path} contains unsupported {type(value).__name__}")
    active = set() if active is None else active
    marker = id(value)
    if marker in active:
        raise ValueError(f"{path} must not contain reference cycles")
    active.add(marker)
    try:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ValueError(f"{path} must use string object keys")
                _validate_json_shape(child, f"{path}.{key}", active=active)
        else:
            for index, child in enumerate(value):
                _validate_json_shape(child, f"{path}[{index}]", active=active)
    finally:
        active.remove(marker)


def result_json(result: ActionResult) -> str:
    """Stable compact representation useful to line-oriented hosts."""
    return json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True)
