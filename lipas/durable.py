"""Checkpointed ReAct execution over :mod:`lipas.execution`.

The runner turns the ordinary ReAct loop into a small persistent phase
machine.  Each external call has a stable effect identity, and each completed
tool is checkpointed before the next one starts.  Reopening a run therefore
reuses recorded terminal effects instead of submitting them again.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import math
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Mapping

from .adapter import Delta, Reply, Thinking, ToolUseDelta
from .behaviour import AgentState, FinalResult, TerminationReason
from .context import RunCancelled, RunContext, RunDeadlineExceeded
from .events import AgentEventType, EventEmitter, EventSink
from .execution import (
    Checkpoint,
    ExecutionStateError,
    ExecutionStore,
    InterruptState,
    Run,
    RunState,
    RunSuspended,
)
from .react import ReActAgent
from .rows.effect import EffectRow, F_CAUSED_BY
from .tools import SideEffectClass, Tool, ToolNotFoundError

__all__ = [
    "ApprovalPolicy",
    "DurablePhaseTimeout",
    "DurableRecoveryRequired",
    "DurableReActRunner",
    "InputPolicy",
    "final_result_from_checkpoint",
    "writes_require_approval",
    "CheckpointMigrationError",
    "migrate_checkpoint_payload",
    "register_checkpoint_migration",
]


ApprovalPolicy = Callable[[Tool, Mapping[str, Any]], Mapping[str, Any] | None]
InputPolicy = Callable[[Tool, Mapping[str, Any]], Mapping[str, Any] | None]

logger = logging.getLogger(__name__)


class DurablePhaseTimeout(ExecutionStateError):
    """A durable model or tool phase exceeded its configured deadline."""

    def __init__(self, phase: str, timeout_s: float) -> None:
        self.phase = phase
        self.timeout_s = timeout_s
        super().__init__(f"durable {phase} phase exceeded {timeout_s:g}s")


class DurableRecoveryRequired(ExecutionStateError):
    """Control interruption raced an effect whose external outcome is unknown."""


def writes_require_approval(
    tool: Tool,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Default workbench policy: suspend before local or external writes."""
    if tool.side_effect in {
        SideEffectClass.IDEMPOTENT_WRITE,
        SideEffectClass.EXTERNAL_WRITE,
    }:
        return {
            "tool_name": tool.name,
            "arguments": dict(arguments),
            "side_effect": tool.side_effect.value,
        }
    return None


_CHECKPOINT_SCHEMA = 1
_CLAIM_STORE_ID_KEY = "durable_claim_store_id"
_PHASE_READY = "ready"
_PHASE_BEFORE_LLM = "before_llm"
_PHASE_AFTER_LLM = "after_llm"
_PHASE_AFTER_TOOL = "after_tool"
_PHASE_TERMINAL = "terminal"
_PHASES = {
    _PHASE_READY,
    _PHASE_BEFORE_LLM,
    _PHASE_AFTER_LLM,
    _PHASE_AFTER_TOOL,
    _PHASE_TERMINAL,
}


class CheckpointMigrationError(ExecutionStateError):
    """A durable checkpoint cannot be upgraded safely."""


_CHECKPOINT_MIGRATIONS: dict[tuple[int, int], Callable[[Mapping[str, Any]], Mapping[str, Any]]] = {}


def register_checkpoint_migration(
    from_version: int,
    to_version: int,
    migration: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> None:
    """Register one deterministic checkpoint payload migration step."""
    if (
        isinstance(from_version, bool) or isinstance(to_version, bool)
        or not isinstance(from_version, int) or not isinstance(to_version, int)
        or from_version < 0 or to_version != from_version + 1
    ):
        raise ValueError("checkpoint migrations must advance one integer version")
    if not callable(migration):
        raise TypeError("migration must be callable")
    key = (from_version, to_version)
    if key in _CHECKPOINT_MIGRATIONS:
        raise ValueError(f"checkpoint migration {from_version}->{to_version} is already registered")
    _CHECKPOINT_MIGRATIONS[key] = migration


def migrate_checkpoint_payload(
    payload: Mapping[str, Any],
    *,
    target_version: int = _CHECKPOINT_SCHEMA,
) -> dict[str, Any]:
    """Upgrade a checkpoint without guessing about unknown future schemas."""
    if not isinstance(payload, Mapping):
        raise TypeError("checkpoint payload must be a mapping")
    if (
        isinstance(target_version, bool)
        or not isinstance(target_version, int)
        or target_version < 0
    ):
        raise ValueError("target_version must be a non-negative int")
    raw_version = payload.get("schema_version", 0)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 0:
        raise CheckpointMigrationError(f"invalid checkpoint schema version {raw_version!r}")
    if target_version < raw_version:
        raise CheckpointMigrationError(
            f"checkpoint schema {raw_version} is newer than target {target_version}",
        )
    current = dict(payload)
    while raw_version < target_version:
        migration = _CHECKPOINT_MIGRATIONS.get((raw_version, raw_version + 1))
        if migration is None:
            raise CheckpointMigrationError(
                f"no checkpoint migration {raw_version}->{raw_version + 1}",
            )
        try:
            migrated = migration(current)
        except Exception as exc:
            raise CheckpointMigrationError(
                f"checkpoint migration {raw_version}->{raw_version + 1} failed",
            ) from exc
        if not isinstance(migrated, Mapping):
            raise CheckpointMigrationError("checkpoint migration must return a mapping")
        current = dict(migrated)
        raw_version += 1
        current["schema_version"] = raw_version
    return current


def _migrate_checkpoint_0_to_1(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    # Early durable development checkpoints omitted the envelope marker.  No
    # semantic state is changed; the marker makes the boundary explicit.
    return {**dict(payload), "schema_version": 1}


register_checkpoint_migration(0, 1, _migrate_checkpoint_0_to_1)


def _agent_state_payload(state: AgentState) -> dict[str, Any]:
    return {
        "messages": list(state.messages),
        "iteration": state.iteration,
        "metadata": dict(state.metadata),
    }


def _agent_state_from_payload(payload: Mapping[str, Any]) -> AgentState:
    messages = payload.get("messages", ())
    iteration = payload.get("iteration", 0)
    metadata = payload.get("metadata", {})
    if not isinstance(messages, (list, tuple)):
        raise TypeError("durable AgentState.messages must be a list")
    if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration < 0:
        raise TypeError("durable AgentState.iteration must be a non-negative int")
    if not isinstance(metadata, Mapping):
        raise TypeError("durable AgentState.metadata must be a mapping")
    return AgentState(
        messages=tuple(messages),
        iteration=iteration,
        metadata=dict(metadata),
    )


def _final_payload(result: FinalResult) -> dict[str, Any]:
    return {
        "text": result.text,
        "state": _agent_state_payload(result.state),
        "stop_reason": result.stop_reason,
        "error": dict(result.error) if result.error is not None else None,
        "metadata": dict(result.metadata),
    }


def _final_from_payload(payload: Mapping[str, Any]) -> FinalResult:
    state = payload.get("state")
    if not isinstance(state, Mapping):
        raise TypeError("durable FinalResult.state must be a mapping")
    error = payload.get("error")
    metadata = payload.get("metadata", {})
    if error is not None and not isinstance(error, Mapping):
        raise TypeError("durable FinalResult.error must be a mapping or None")
    if not isinstance(metadata, Mapping):
        raise TypeError("durable FinalResult.metadata must be a mapping")
    return FinalResult(
        text=str(payload.get("text", "")),
        state=_agent_state_from_payload(state),
        stop_reason=str(payload.get("stop_reason", TerminationReason.ERROR)),
        error=dict(error) if error is not None else None,
        metadata=dict(metadata),
    )


def final_result_from_checkpoint(
    checkpoint: Checkpoint,
    *,
    claim_store_id: str | None = None,
) -> FinalResult:
    """Restore a settled logical result without reclaiming a terminal Run."""
    if checkpoint.phase != _PHASE_TERMINAL:
        raise ExecutionStateError(
            f"run {checkpoint.run_id!r} has no terminal checkpoint",
        )
    # Terminal restoration is also a recovery boundary.  Do not let this
    # convenience API bypass the same schema gate used by the live runner;
    # otherwise a future/unknown checkpoint could be presented as a completed
    # result merely because it contains a ``final_result`` field.
    payload = migrate_checkpoint_payload(checkpoint.state)
    DurableReActRunner._validate_payload(checkpoint.phase, payload)
    final_data = payload.get("final_result")
    if not isinstance(final_data, Mapping):
        raise TypeError("terminal checkpoint has no final_result mapping")
    result = _final_from_payload(final_data)
    if (
        claim_store_id is not None
        and result.state.metadata.get(_CLAIM_STORE_ID_KEY) != claim_store_id
    ):
        raise ExecutionStateError(
            f"run {checkpoint.run_id!r} checkpoint belongs to a different "
            "claim store",
        )
    return result


def settled_result_from_run(
    run: Run,
    checkpoint: Checkpoint | None,
    *,
    claim_store_id: str,
) -> FinalResult:
    """Restore any terminal Run without performing new external work."""
    if checkpoint is not None and checkpoint.phase == _PHASE_TERMINAL:
        return final_result_from_checkpoint(
            checkpoint,
            claim_store_id=claim_store_id,
        )
    if run.state is RunState.COMPLETED:
        raise ExecutionStateError(
            f"completed run {run.id!r} has no terminal checkpointed result",
        )

    if checkpoint is None:
        state = AgentState(metadata={
            "caused_by": run.id,
            "execution_run_id": run.id,
            _CLAIM_STORE_ID_KEY: claim_store_id,
        })
    else:
        payload = migrate_checkpoint_payload(checkpoint.state)
        DurableReActRunner._validate_payload(checkpoint.phase, payload)
        state_payload = payload.get("agent_state")
        if not isinstance(state_payload, Mapping):
            raise TypeError("terminal run checkpoint has no agent_state mapping")
        state = _agent_state_from_payload(state_payload)
        if state.metadata.get(_CLAIM_STORE_ID_KEY) != claim_store_id:
            raise ExecutionStateError(
                f"run {run.id!r} checkpoint belongs to a different claim store",
            )

    if run.state is RunState.CANCELLED:
        return FinalResult(
            text="",
            state=state,
            stop_reason=TerminationReason.CANCELLED,
            metadata={"iterations": state.iteration},
        )
    if run.state is RunState.FAILED:
        return FinalResult(
            text="",
            state=state,
            stop_reason=TerminationReason.ERROR,
            error=dict(run.error or {"type": "execution_failed"}),
            metadata={"iterations": state.iteration},
        )
    raise ExecutionStateError(
        f"run {run.id!r} is {run.state.value}, not terminal",
    )


@dataclass
class DurableReActRunner:
    """Run one already-claimed execution lease through a durable ReAct loop."""

    behaviour: ReActAgent
    store: ExecutionStore
    run: Run
    approval_policy: ApprovalPolicy | None = None
    input_policy: InputPolicy | None = None
    lease_seconds: float = 60.0
    heartbeat_interval_s: float | None = None
    phase_timeout_s: float | None = None
    context: RunContext | None = None
    event_sink: EventSink | None = None
    event_cursor: int = 0

    def __post_init__(self) -> None:
        if self.run.state is not RunState.RUNNING or not self.run.lease_token:
            raise ValueError("DurableReActRunner requires a claimed running Run")
        self.lease_seconds = self._positive_seconds(
            self.lease_seconds, "lease_seconds",
        )
        if self.heartbeat_interval_s is None:
            self.heartbeat_interval_s = self.lease_seconds / 3
        else:
            self.heartbeat_interval_s = self._positive_seconds(
                self.heartbeat_interval_s, "heartbeat_interval_s",
            )
        if self.heartbeat_interval_s >= self.lease_seconds:
            raise ValueError("heartbeat_interval_s must be less than lease_seconds")
        if self.phase_timeout_s is not None:
            self.phase_timeout_s = self._positive_seconds(
                self.phase_timeout_s, "phase_timeout_s",
            )
        if self.context is None:
            self.context = RunContext.create(run_id=self.run.id)
        elif not isinstance(self.context, RunContext):
            raise TypeError("context must be a RunContext or None")
        if self.context.run_id != self.run.id:
            raise ValueError("durable RunContext.run_id must equal Run.id")
        current_cursor = self.store.agent_event_cursor(self.run.id)
        if self.event_cursor < 0:
            raise ValueError("event_cursor must be non-negative")
        self.event_cursor = max(self.event_cursor, current_cursor)

    @property
    def _token(self) -> str:
        assert self.run.lease_token is not None
        return self.run.lease_token

    @property
    def _claim_store_id(self) -> str:
        store_id = getattr(self.behaviour.rowset.store, "store_id", None)
        if not isinstance(store_id, str) or not store_id:
            raise ExecutionStateError(
                "durable ReAct requires a claim store with a stable store_id",
            )
        return store_id

    async def run_to_completion(
        self,
        initial: AgentState | None = None,
    ) -> FinalResult:
        """Run durably while persisting exceptional failure events."""
        try:
            return await self._run_with_heartbeat(initial)
        except (RunSuspended, asyncio.CancelledError):
            raise
        except Exception as exc:
            await self._emit(
                "run:failed",
                AgentEventType.RUN_FAILED,
                data={
                    "exception": type(exc).__name__,
                    "message": str(exc),
                },
            )
            raise

    async def _run_with_heartbeat(
        self,
        initial: AgentState | None = None,
    ) -> FinalResult:
        """Run with an automatic lease heartbeat around the phase machine."""
        heartbeat = asyncio.create_task(self._heartbeat())
        execution = asyncio.create_task(self._run_to_completion(initial))
        try:
            done, _ = await asyncio.wait(
                {execution, heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if execution in done:
                heartbeat.cancel()
                # Execution settlement and the next heartbeat may become
                # ready in the same loop tick. Once execution is done, its
                # result (including any lease-fencing failure) is the primary
                # outcome; a heartbeat observing the now-terminal Run is not
                # a second failure.
                with contextlib.suppress(
                    asyncio.CancelledError, ExecutionStateError,
                ):
                    await heartbeat
                try:
                    return await execution
                except RunCancelled as exc:
                    return await self._finish_controlled(initial, exc)
                except RunDeadlineExceeded as exc:
                    return await self._finish_controlled(initial, exc)

            # Settlement changes the Run to terminal immediately before the
            # execution coroutine returns. A heartbeat scheduled in that tiny
            # window can lose its lease even though execution succeeded.
            try:
                await heartbeat
            except ExecutionStateError:
                refreshed = self.store.get_run(self.run.id)
                if refreshed is not None and refreshed.state in {
                    RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED,
                }:
                    return await execution
                execution.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await execution
                raise
            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
            raise AssertionError("lease heartbeat terminated without an error")
        finally:
            for task in (execution, heartbeat):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                execution, heartbeat, return_exceptions=True,
            )

    async def _heartbeat(self) -> None:
        assert self.heartbeat_interval_s is not None
        while True:
            self.run = self.store.renew_lease(
                self.run.id,
                self._token,
                lease_seconds=self.lease_seconds,
            )
            await asyncio.sleep(self.heartbeat_interval_s)

    async def _await_phase(self, awaitable: Any, phase: str) -> Any:
        assert self.context is not None
        controlled = self.context.wait(awaitable)
        if self.phase_timeout_s is None:
            return await controlled
        try:
            return await asyncio.wait_for(controlled, timeout=self.phase_timeout_s)
        except RunDeadlineExceeded:
            raise
        except TimeoutError as exc:
            raise DurablePhaseTimeout(phase, self.phase_timeout_s) from exc

    async def _run_to_completion(
        self,
        initial: AgentState | None = None,
    ) -> FinalResult:
        assert self.context is not None
        try:
            checkpoint = self.store.get_checkpoint(self.run.id)
            if checkpoint is None:
                if initial is None:
                    raise ValueError("a new durable run requires an initial AgentState")
                existing_store_id = initial.metadata.get(_CLAIM_STORE_ID_KEY)
                if (
                    existing_store_id is not None
                    and existing_store_id != self._claim_store_id
                ):
                    raise ExecutionStateError(
                        "initial AgentState is bound to a different claim store",
                    )
                state = initial.with_metadata({
                    **initial.metadata,
                    _CLAIM_STORE_ID_KEY: self._claim_store_id,
                })
                version = 0
                payload = self._payload(state)
                checkpoint = self.store.save_checkpoint(
                    self.run.id,
                    self._token,
                    expected_version=version,
                    phase=_PHASE_READY,
                    state=payload,
                )
            elif initial is not None:
                raise ValueError(
                    "a resumed durable run restores state from its checkpoint",
                )

            version = checkpoint.version
            phase = checkpoint.phase
            payload = migrate_checkpoint_payload(checkpoint.state)
            self._validate_payload(phase, payload)
        except Exception as exc:
            self._fail_run(exc)
            raise

        try:
            await self._emit(
                "run:started",
                AgentEventType.RUN_STARTED,
                data={
                    "model": self.behaviour.request_template.model,
                    "tools": [tool.name for tool in self.behaviour.tools],
                },
            )
            while True:
                state_data = payload.get("agent_state")
                if not isinstance(state_data, Mapping):
                    raise TypeError("durable checkpoint has no agent_state mapping")
                state = _agent_state_from_payload(state_data)
                checkpoint_store_id = state.metadata.get(_CLAIM_STORE_ID_KEY)
                if checkpoint_store_id != self._claim_store_id:
                    raise ExecutionStateError(
                        f"run {self.run.id!r} checkpoint belongs to a different "
                        "claim store",
                    )

                current_run = self.store.get_run(self.run.id)
                if current_run is None:
                    raise KeyError(self.run.id)

                if phase == _PHASE_TERMINAL:
                    final_data = payload.get("final_result")
                    if not isinstance(final_data, Mapping):
                        raise TypeError("terminal checkpoint has no final_result")
                    restored = _final_from_payload(final_data)
                    if (
                        current_run.cancel_requested
                        and restored.stop_reason != TerminationReason.CANCELLED
                    ):
                        cancelled = self.behaviour._cancelled_result(state)
                        phase, payload, version = self._checkpoint_terminal(
                            state, cancelled, version,
                        )
                        continue
                    try:
                        await self._emit_terminal(restored)
                        return self._settle(restored)
                    except ExecutionStateError:
                        refreshed = self.store.get_run(self.run.id)
                        if refreshed is None or not refreshed.cancel_requested:
                            raise
                        cancelled = self.behaviour._cancelled_result(state)
                        phase, payload, version = self._checkpoint_terminal(
                            state, cancelled, version,
                        )
                        continue

                if current_run.cancel_requested:
                    cancelled = self.behaviour._cancelled_result(state)
                    phase, payload, version = self._checkpoint_terminal(
                        state, cancelled, version,
                    )
                    continue

                if phase == _PHASE_READY:
                    if state.iteration >= self.behaviour.max_iterations:
                        result = self.behaviour._max_iterations_result(state)
                        phase, payload, version = self._checkpoint_terminal(
                            state, result, version,
                        )
                        continue

                    effect_id = self._llm_effect_id(state.iteration)
                    payload = self._payload(state, llm_effect_id=effect_id)
                    checkpoint = self.store.save_checkpoint(
                        self.run.id,
                        self._token,
                        expected_version=version,
                        phase=_PHASE_BEFORE_LLM,
                        state=payload,
                    )
                    version, phase = checkpoint.version, checkpoint.phase
                    continue

                if phase == _PHASE_BEFORE_LLM:
                    effect_id_value = payload.get("llm_effect_id")
                    if not isinstance(effect_id_value, str):
                        raise TypeError("before_llm checkpoint has no llm_effect_id")
                    request = self.behaviour._build_request(state)
                    await self._emit(
                        f"model:{state.iteration}:started",
                        AgentEventType.MODEL_STARTED,
                        iteration=state.iteration,
                        data={"model": request.model},
                    )
                    reply = await self._await_phase(self.behaviour.harness.call(
                        request,
                        caused_by=self.run.id,
                        effect_id=effect_id_value,
                        stream_sink=self._model_event_sink(state.iteration),
                    ), "model")
                    await self._emit(
                        f"model:{state.iteration}:completed",
                        AgentEventType.MODEL_COMPLETED,
                        iteration=state.iteration,
                        data={
                            "model": reply.model,
                            "stop_reason": reply.stop_reason,
                            "usage": {
                                "input": reply.usage.input,
                                "output": reply.usage.output,
                                "cache_read": reply.usage.cache_read,
                                "cache_write": reply.usage.cache_write,
                            },
                        },
                    )
                    tool_calls = self.behaviour._extract_tool_calls(reply)
                    payload = self._payload(
                        state,
                        llm_effect_id=effect_id_value,
                        reply=reply,
                        tool_calls=tool_calls,
                        tool_results=(),
                        next_tool_index=0,
                    )
                    checkpoint = self.store.save_checkpoint(
                        self.run.id,
                        self._token,
                        expected_version=version,
                        phase=_PHASE_AFTER_LLM,
                        state=payload,
                    )
                    version, phase = checkpoint.version, checkpoint.phase
                    continue

                checkpoint_reply = payload.get("reply")
                if not isinstance(checkpoint_reply, Reply):
                    raise TypeError(f"{phase} checkpoint has no Reply")
                reply = checkpoint_reply
                tool_calls = self._mapping_sequence(payload.get("tool_calls", ()), "tool_calls")
                tool_results = list(
                    self._mapping_sequence(payload.get("tool_results", ()), "tool_results"),
                )

                if reply.stop_reason == "error":
                    result = self.behaviour._error_result(state, reply)
                    result = self._supervisor_result(state) or result
                    result = await self.behaviour._observe_result(
                        result,
                        reply,
                        (),
                        (),
                        self.context,
                        self._observer_emitter("terminal", state.iteration),
                    )
                    phase, payload, version = self._checkpoint_terminal(
                        state, result, version,
                    )
                    continue

                if not tool_calls:
                    if reply.stop_reason == "tool_use":
                        result = self.behaviour._malformed_tool_result(state)
                        result = await self.behaviour._observe_result(
                            result,
                            reply,
                            (),
                            (),
                            self.context,
                            self._observer_emitter("terminal", state.iteration),
                        )
                        phase, payload, version = self._checkpoint_terminal(
                            state, result, version,
                        )
                        continue
                    result = self.behaviour._terminal_result(
                        state,
                        reply,
                        claim_id_prefix=self._iteration_claim_prefix(state.iteration),
                    )
                    result = await self.behaviour._observe_result(
                        result,
                        reply,
                        (),
                        (),
                        self.context,
                        self._observer_emitter(
                            "terminal", result.state.iteration,
                        ),
                    )
                    phase, payload, version = self._checkpoint_terminal(
                        result.state, result, version,
                    )
                    continue

                next_index = payload.get("next_tool_index", 0)
                if isinstance(next_index, bool) or not isinstance(next_index, int):
                    raise TypeError("next_tool_index must be an int")
                if next_index < 0 or next_index > len(tool_calls):
                    raise ValueError("next_tool_index is outside tool_calls")

                if next_index < len(tool_calls):
                    batch_size = self.behaviour._parallel_tool_count(
                        tool_calls, next_index,
                    )
                    input_request: Mapping[str, Any] | None = None
                    input_id = payload.get("input_interrupt_id")
                    if input_id is not None:
                        if not isinstance(input_id, str):
                            raise TypeError("input_interrupt_id must be a string")
                        interrupt = self.store.get_interrupt(input_id)
                        if interrupt is None:
                            raise ExecutionStateError(
                                f"checkpoint references missing interrupt {input_id!r}",
                            )
                        if interrupt.kind != "input":
                            raise ExecutionStateError(
                                f"interrupt {input_id!r} is {interrupt.kind!r}, not input",
                            )
                        if interrupt.state is not InterruptState.ALLOWED:
                            raise ExecutionStateError(
                                f"interrupt {input_id!r} is {interrupt.state.value}, "
                                "not allowed",
                            )
                        current_call = tool_calls[next_index]
                        details = self._tool_event_details(current_call)
                        await self._emit(
                            f"tool:{state.iteration}:{next_index}:started",
                            AgentEventType.TOOL_STARTED,
                            iteration=state.iteration,
                            data=details,
                        )
                        input_result = {
                            "type": "tool_result",
                            "tool_use_id": str(current_call["id"]),
                            "content": self._input_response_text(interrupt.response),
                        }
                        tool_results.append(input_result)
                        await self._emit(
                            f"tool:{state.iteration}:{next_index}:completed",
                            AgentEventType.TOOL_COMPLETED,
                            iteration=state.iteration,
                            data={
                                **details,
                                "is_error": False,
                                "content": input_result["content"],
                                "input_boundary": True,
                            },
                        )
                        payload = self._payload(
                            state,
                            llm_effect_id=payload.get("llm_effect_id"),
                            reply=reply,
                            tool_calls=tool_calls,
                            tool_results=tool_results,
                            next_tool_index=next_index + 1,
                        )
                        checkpoint = self.store.save_checkpoint(
                            self.run.id,
                            self._token,
                            expected_version=version,
                            phase=_PHASE_AFTER_TOOL,
                            state=payload,
                        )
                        version, phase = checkpoint.version, checkpoint.phase
                        continue

                    if self.input_policy is not None:
                        for offset in range(batch_size):
                            candidate = tool_calls[next_index + offset]
                            try:
                                tool = self.behaviour.tools.get(
                                    str(candidate["name"]),
                                )
                            except ToolNotFoundError:
                                break
                            decision = self.input_policy(
                                tool, dict(candidate.get("input") or {}),
                            )
                            if decision is None:
                                continue
                            if offset == 0:
                                input_request = decision
                                batch_size = 1
                            else:
                                batch_size = offset
                            break
                    if input_request is not None:
                        await self._emit_tool_requested(
                            state.iteration,
                            next_index,
                            tool_calls[next_index],
                        )
                        input_id = self._input_interrupt_id(
                            state.iteration, next_index,
                        )
                        interrupt = self.store.suspend(
                            self.run.id,
                            self._token,
                            expected_version=version,
                            phase=phase,
                            checkpoint_state={
                                **payload,
                                "input_interrupt_id": input_id,
                            },
                            kind="input",
                            request=input_request,
                            interrupt_id=input_id,
                        )
                        raise RunSuspended(interrupt)

                    approval_request: Mapping[str, Any] | None = None
                    approval_id = payload.get("approval_interrupt_id")
                    if approval_id is not None:
                        # A restored approval applies to exactly the current
                        # tool. Do not infer authority for later calls.
                        batch_size = 1
                        if not isinstance(approval_id, str):
                            raise TypeError("approval_interrupt_id must be a string")
                        interrupt = self.store.get_interrupt(approval_id)
                        if interrupt is None:
                            raise ExecutionStateError(
                                f"checkpoint references missing interrupt {approval_id!r}",
                            )
                        if interrupt.state is not InterruptState.ALLOWED:
                            raise ExecutionStateError(
                                f"interrupt {approval_id!r} is {interrupt.state.value}, "
                                "not allowed",
                            )
                    elif self.approval_policy is not None:
                        # Approval policies are consulted before any member of
                        # a candidate parallel batch starts. A later approval
                        # boundary shortens the batch; the current call then
                        # checkpoints before that boundary is revisited.
                        for offset in range(batch_size):
                            candidate = tool_calls[next_index + offset]
                            try:
                                tool = self.behaviour.tools.get(
                                    str(candidate["name"]),
                                )
                            except ToolNotFoundError:
                                break
                            approval_decision = self.approval_policy(
                                tool, dict(candidate.get("input") or {}),
                            )
                            if approval_decision is None:
                                continue
                            if offset == 0:
                                approval_request = approval_decision
                                batch_size = 1
                            else:
                                batch_size = offset
                            break
                    if approval_request is not None:
                        await self._emit_tool_requested(
                            state.iteration,
                            next_index,
                            tool_calls[next_index],
                        )
                        approval_id = self._approval_interrupt_id(
                            state.iteration, next_index,
                        )
                        suspended_payload = {
                            **payload,
                            "approval_interrupt_id": approval_id,
                        }
                        interrupt = self.store.suspend(
                            self.run.id,
                            self._token,
                            expected_version=version,
                            phase=phase,
                            checkpoint_state=suspended_payload,
                            kind="approval",
                            request=approval_request,
                            interrupt_id=approval_id,
                        )
                        raise RunSuspended(interrupt)

                    # Provider tool-use ids are correlation ids inside the
                    # conversation, not globally unique effect identities.
                    # Namespace the durable effect by run/iteration/index so
                    # two runs sharing one claim session can never replay one
                    # another's tool result. The original ids and result order
                    # are restored on the blocks sent back to the model.
                    batch = tool_calls[next_index:next_index + batch_size]
                    calls = [
                        self._run_tool_call(
                            tool_call,
                            state.iteration,
                            next_index + offset,
                        )
                        for offset, tool_call in enumerate(batch)
                    ]
                    batch_results = await self._await_phase(
                        (
                            asyncio.gather(*calls)
                            if len(calls) > 1 else calls[0]
                        ),
                        (
                            f"tools:{len(calls)}"
                            if len(calls) > 1
                            else f"tool:{batch[0]['name']}"
                        ),
                    )
                    if len(calls) == 1:
                        batch_results = [batch_results]
                    tool_results.extend(batch_results)
                    payload = self._payload(
                        state,
                        llm_effect_id=payload.get("llm_effect_id"),
                        reply=reply,
                        tool_calls=tool_calls,
                        tool_results=tool_results,
                        next_tool_index=next_index + batch_size,
                    )
                    checkpoint = self.store.save_checkpoint(
                        self.run.id,
                        self._token,
                        expected_version=version,
                        phase=_PHASE_AFTER_TOOL,
                        state=payload,
                    )
                    version, phase = checkpoint.version, checkpoint.phase
                    continue

                next_state = self.behaviour._advance_after_tools(
                    state,
                    reply,
                    tool_calls,
                    tool_results,
                    claim_id_prefix=self._iteration_claim_prefix(state.iteration),
                )
                early = self._supervisor_result(
                    next_state,
                    iteration=state.iteration,
                )
                if early is not None:
                    phase, payload, version = self._checkpoint_terminal(
                        next_state, early, version,
                    )
                    continue
                observer_result = await self.behaviour._observe(
                    next_state,
                    phase="after_tools",
                    reply=reply,
                    tool_calls=tool_calls,
                    tool_results=tool_results,
                    context=self.context,
                    event_emitter=self._observer_emitter(
                        "after_tools", next_state.iteration,
                    ),
                )
                if observer_result is not None:
                    phase, payload, version = self._checkpoint_terminal(
                        next_state, observer_result, version,
                    )
                    continue
                payload = self._payload(next_state)
                checkpoint = self.store.save_checkpoint(
                    self.run.id,
                    self._token,
                    expected_version=version,
                    phase=_PHASE_READY,
                    state=payload,
                )
                version, phase = checkpoint.version, checkpoint.phase
        except (RunSuspended, RunCancelled, RunDeadlineExceeded):
            raise
        except Exception as exc:
            self._fail_run(exc)
            raise

    async def _emit(
        self,
        identity: str,
        event_type: str,
        *,
        iteration: int = 0,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        event = self.store.append_agent_event(
            self.run.id,
            event_type,
            identity=identity,
            iteration=iteration,
            data=data,
        )
        if event.sequence <= self.event_cursor:
            return
        self.event_cursor = event.sequence
        if self.event_sink is None:
            return
        try:
            delivered = self.event_sink(event)
            if inspect.isawaitable(delivered):
                await delivered
        except Exception:
            # Persistence is authoritative. A UI reconnects with its cursor.
            logger.exception("durable AgentEvent sink failed; continuing")
            self.event_sink = None

    def _model_event_sink(self, iteration: int):
        stream_index = 0

        async def deliver(event: Any) -> None:
            nonlocal stream_index
            event_type: str | None = None
            data: Mapping[str, Any] | None = None
            if isinstance(event, Delta):
                event_type = AgentEventType.MODEL_DELTA
                data = {"index": event.index, "text": event.text}
            elif isinstance(event, Thinking):
                event_type = AgentEventType.MODEL_THINKING
                data = {"text": event.text}
            elif isinstance(event, ToolUseDelta):
                event_type = AgentEventType.MODEL_TOOL_DELTA
                data = {"index": event.index, "partial_json": event.partial_json}
            if event_type is not None:
                await self._emit(
                    f"model:{iteration}:stream:{stream_index}",
                    event_type,
                    iteration=iteration,
                    data=data,
                )
                stream_index += 1

        return deliver

    def _observer_emitter(self, phase: str, iteration: int) -> EventEmitter:
        delivered = 0

        async def persist(event) -> None:
            nonlocal delivered
            await self._emit(
                f"observer:{phase}:{iteration}:{delivered}",
                event.type,
                iteration=event.iteration,
                data=event.data,
            )
            delivered += 1

        return EventEmitter(self.run.id, persist)

    def _tool_event_details(
        self, tool_call: Mapping[str, Any],
    ) -> dict[str, Any]:
        tool_name = str(tool_call["name"])
        details: dict[str, Any] = {
            "tool_use_id": str(tool_call["id"]),
            "tool_name": tool_name,
            "arguments": dict(tool_call.get("input") or {}),
        }
        try:
            details["side_effect"] = self.behaviour.tools.get(
                tool_name,
            ).side_effect.value
        except ToolNotFoundError:
            pass
        return details

    async def _emit_tool_requested(
        self,
        iteration: int,
        tool_index: int,
        tool_call: Mapping[str, Any],
    ) -> None:
        await self._emit(
            f"tool:{iteration}:{tool_index}:requested",
            AgentEventType.TOOL_REQUESTED,
            iteration=iteration,
            data=self._tool_event_details(tool_call),
        )

    async def _run_tool_call(
        self,
        tool_call: Mapping[str, Any],
        iteration: int,
        tool_index: int,
    ) -> Mapping[str, Any]:
        details = self._tool_event_details(tool_call)
        await self._emit_tool_requested(iteration, tool_index, tool_call)
        await self._emit(
            f"tool:{iteration}:{tool_index}:started",
            AgentEventType.TOOL_STARTED,
            iteration=iteration,
            data=details,
        )
        result = await self.behaviour.tool_harness.call(
            tool_name=str(tool_call["name"]),
            arguments=dict(tool_call.get("input") or {}),
            effect_id=self._tool_effect_id(iteration, tool_index),
            tool_use_id=str(tool_call["id"]),
            caused_by=self.run.id,
        )
        await self._emit(
            f"tool:{iteration}:{tool_index}:completed",
            AgentEventType.TOOL_COMPLETED,
            iteration=iteration,
            data={
                **details,
                "is_error": bool(result.get("is_error")),
                "content": result.get("content", ""),
            },
        )
        return result

    async def _emit_terminal(self, result: FinalResult) -> None:
        if result.stop_reason == TerminationReason.CANCELLED:
            event_type = AgentEventType.RUN_CANCELLED
            identity = "run:cancelled"
        else:
            # A logical error is still a completed Agent protocol result.
            # RUN_FAILED is reserved for exceptions that escaped the contract.
            event_type = AgentEventType.RUN_COMPLETED
            identity = "run:completed"
        await self._emit(
            identity,
            event_type,
            iteration=result.state.iteration,
            data={
                "text": result.text,
                "stop_reason": result.stop_reason,
                "error": dict(result.error) if result.error else None,
                "metadata": dict(result.metadata),
            },
        )

    async def _finish_controlled(
        self,
        initial: AgentState | None,
        exc: RunCancelled | RunDeadlineExceeded,
    ) -> FinalResult:
        if self._has_unsettled_effect():
            recovery = DurableRecoveryRequired(
                f"run {self.run.id!r} was {type(exc).__name__} while an "
                "effect had no terminal outcome; reconcile before reopening",
            )
            self._fail_run(recovery, recovery_required=True)
            raise recovery
        checkpoint = self.store.get_checkpoint(self.run.id)
        if checkpoint is not None:
            raw_state = checkpoint.state.get("agent_state")
            state = (
                _agent_state_from_payload(raw_state)
                if isinstance(raw_state, Mapping) else initial or AgentState()
            )
            version = checkpoint.version
        else:
            state = initial or AgentState()
            version = 0
        if isinstance(exc, RunCancelled):
            current = self.store.get_run(self.run.id)
            if current is not None and not current.cancel_requested:
                self.store.request_cancel(self.run.id)
            result = self.behaviour._cancelled_result(state)
        else:
            result = self.behaviour._deadline_result(state, exc)
        self._checkpoint_terminal(state, result, version)
        await self._emit_terminal(result)
        return self._settle(result)

    def _fail_run(
        self,
        exc: Exception,
        *,
        recovery_required: bool | None = None,
    ) -> None:
        with contextlib.suppress(Exception):
            if recovery_required is None:
                recovery_required = isinstance(
                    exc, (DurablePhaseTimeout, DurableRecoveryRequired),
                )
            self.store.fail_run(
                self.run.id,
                self._token,
                error={
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "recovery_required": recovery_required,
                    "reconcile_before_resume": recovery_required,
                },
            )

    def _has_unsettled_effect(self) -> bool:
        """Detect an intent for this Run that has no terminal Effect claim."""
        effect_row = next(
            (row for row in self.behaviour.rowset.rows if isinstance(row, EffectRow)),
            None,
        )
        if effect_row is None:
            return False
        view = effect_row.project(self.behaviour.rowset.store)
        return any(
            node.intent is not None
            and node.intent.fields.get(F_CAUSED_BY) == self.run.id
            and not node.is_terminal
            for node in view.nodes.values()
        )

    def _checkpoint_terminal(
        self,
        state: AgentState,
        result: FinalResult,
        version: int,
    ) -> tuple[str, dict[str, Any], int]:
        payload = self._payload(state, final_result=_final_payload(result))
        checkpoint = self.store.save_checkpoint(
            self.run.id,
            self._token,
            expected_version=version,
            phase=_PHASE_TERMINAL,
            state=payload,
        )
        return checkpoint.phase, dict(checkpoint.state), checkpoint.version

    def _settle(self, result: FinalResult) -> FinalResult:
        if result.stop_reason == TerminationReason.CANCELLED:
            self.store.finish_cancelled(
                self.run.id,
                self._token,
            )
        elif result.is_natural:
            self.store.complete_run(
                self.run.id,
                self._token,
                result=_final_payload(result),
            )
        else:
            self.store.fail_run(
                self.run.id,
                self._token,
                error={
                    "type": "agent_termination",
                    "stop_reason": result.stop_reason,
                    "detail": dict(result.error) if result.error is not None else None,
                },
            )
        return result

    def _payload(self, state: AgentState, **extra: Any) -> dict[str, Any]:
        return {
            "schema_version": _CHECKPOINT_SCHEMA,
            "agent_state": _agent_state_payload(state),
            **extra,
        }

    @staticmethod
    def _validate_payload(phase: str, payload: Mapping[str, Any]) -> None:
        if phase not in _PHASES:
            raise ValueError(f"unknown durable ReAct phase: {phase!r}")
        if payload.get("schema_version") != _CHECKPOINT_SCHEMA:
            raise ValueError(
                f"unsupported durable ReAct checkpoint schema: "
                f"{payload.get('schema_version')!r}",
            )

    @staticmethod
    def _mapping_sequence(value: Any, name: str) -> tuple[Mapping[str, Any], ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"{name} must be a list")
        if not all(isinstance(item, Mapping) for item in value):
            raise TypeError(f"{name} entries must be mappings")
        return tuple(value)

    def _llm_effect_id(self, iteration: int) -> str:
        digest = hashlib.sha256(
            f"{self.run.id}:llm:{iteration}".encode("utf-8"),
        ).hexdigest()[:12]
        return f"call_{digest}"

    def _iteration_claim_prefix(self, iteration: int) -> str:
        return f"durable:{self.run.id}:iteration:{iteration}"

    def _supervisor_result(
        self,
        state: AgentState,
        *,
        iteration: int | None = None,
    ) -> FinalResult | None:
        return self.behaviour._maybe_supervisor_tick(
            state,
            claim_id_prefix=(
                f"durable:{self.run.id}:supervisor:"
                f"{state.iteration if iteration is None else iteration}"
            ),
        )

    def _tool_effect_id(self, iteration: int, tool_index: int) -> str:
        digest = hashlib.sha256(
            f"{self.run.id}:tool:{iteration}:{tool_index}".encode("utf-8"),
        ).hexdigest()[:12]
        return f"tool_{digest}"

    def _approval_interrupt_id(self, iteration: int, tool_index: int) -> str:
        digest = hashlib.sha256(
            f"{self.run.id}:approval:{iteration}:{tool_index}".encode("utf-8"),
        ).hexdigest()[:20]
        return f"approval_{digest}"

    def _input_interrupt_id(self, iteration: int, tool_index: int) -> str:
        digest = hashlib.sha256(
            f"{self.run.id}:input:{iteration}:{tool_index}".encode("utf-8"),
        ).hexdigest()[:20]
        return f"input_{digest}"

    @staticmethod
    def _input_response_text(response: Any) -> str:
        if isinstance(response, str):
            return response
        return json.dumps(response, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _positive_seconds(value: float, name: str) -> float:
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
            raise ValueError(f"{name} must be a positive finite number")
        return float(value)
