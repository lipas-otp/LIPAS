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
import math
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Mapping

from .adapter import Reply
from .behaviour import AgentState, FinalResult, TerminationReason
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
from .tools import SideEffectClass, Tool, ToolNotFoundError

__all__ = [
    "ApprovalPolicy",
    "DurablePhaseTimeout",
    "DurableReActRunner",
    "final_result_from_checkpoint",
    "writes_require_approval",
]


ApprovalPolicy = Callable[[Tool, Mapping[str, Any]], Mapping[str, Any] | None]


class DurablePhaseTimeout(ExecutionStateError):
    """A durable model or tool phase exceeded its configured deadline."""

    def __init__(self, phase: str, timeout_s: float) -> None:
        self.phase = phase
        self.timeout_s = timeout_s
        super().__init__(f"durable {phase} phase exceeded {timeout_s:g}s")


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
    final_data = checkpoint.state.get("final_result")
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
        state_payload = checkpoint.state.get("agent_state")
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
    lease_seconds: float = 60.0
    heartbeat_interval_s: float | None = None
    phase_timeout_s: float | None = None

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
        """Run with an automatic lease heartbeat around the phase machine."""
        execution = asyncio.create_task(self._run_to_completion(initial))
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            done, _ = await asyncio.wait(
                {execution, heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if execution in done:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
                return await execution

            execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution
            # The heartbeat only terminates when renewal fails.
            await heartbeat
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
            await asyncio.sleep(self.heartbeat_interval_s)
            self.run = self.store.renew_lease(
                self.run.id,
                self._token,
                lease_seconds=self.lease_seconds,
            )

    async def _await_phase(self, awaitable: Any, phase: str) -> Any:
        if self.phase_timeout_s is None:
            return await awaitable
        try:
            return await asyncio.wait_for(awaitable, timeout=self.phase_timeout_s)
        except TimeoutError as exc:
            raise DurablePhaseTimeout(phase, self.phase_timeout_s) from exc

    async def _run_to_completion(
        self,
        initial: AgentState | None = None,
    ) -> FinalResult:
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
            payload = dict(checkpoint.state)
            self._validate_payload(phase, payload)
        except Exception as exc:
            self._fail_run(exc)
            raise

        try:
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
                        cancelled = FinalResult(
                            text="",
                            state=state,
                            stop_reason=TerminationReason.CANCELLED,
                            metadata={"iterations": state.iteration},
                        )
                        phase, payload, version = self._checkpoint_terminal(
                            state, cancelled, version,
                        )
                        continue
                    try:
                        return self._settle(restored)
                    except ExecutionStateError:
                        refreshed = self.store.get_run(self.run.id)
                        if refreshed is None or not refreshed.cancel_requested:
                            raise
                        cancelled = FinalResult(
                            text="",
                            state=state,
                            stop_reason=TerminationReason.CANCELLED,
                            metadata={"iterations": state.iteration},
                        )
                        phase, payload, version = self._checkpoint_terminal(
                            state, cancelled, version,
                        )
                        continue

                if current_run.cancel_requested:
                    cancelled = FinalResult(
                        text="",
                        state=state,
                        stop_reason=TerminationReason.CANCELLED,
                        metadata={"iterations": state.iteration},
                    )
                    phase, payload, version = self._checkpoint_terminal(
                        state, cancelled, version,
                    )
                    continue

                if phase == _PHASE_READY:
                    if state.iteration >= self.behaviour.max_iterations:
                        result = FinalResult(
                            text="",
                            state=state,
                            stop_reason=TerminationReason.MAX_ITERATIONS,
                            metadata={"iterations": state.iteration},
                        )
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
                    reply = await self._await_phase(self.behaviour.harness.call(
                        request,
                        caused_by=self.run.id,
                        effect_id=effect_id_value,
                    ), "model")
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
                    result = FinalResult(
                        text="",
                        state=state,
                        stop_reason=TerminationReason.ERROR,
                        error=reply.error_detail or {"type": "unknown"},
                        metadata={"iterations": state.iteration},
                    )
                    result = self._supervisor_result(state) or result
                    phase, payload, version = self._checkpoint_terminal(
                        state, result, version,
                    )
                    continue

                if not tool_calls:
                    if reply.stop_reason == "tool_use":
                        result = FinalResult(
                            text="",
                            state=state,
                            stop_reason=TerminationReason.ERROR,
                            error={
                                "type": "malformed_tool_use",
                                "message": (
                                    "model stopped for tool_use without a valid "
                                    "tool_use block"
                                ),
                            },
                            metadata={"iterations": state.iteration},
                        )
                        phase, payload, version = self._checkpoint_terminal(
                            state, result, version,
                        )
                        continue
                    next_state = state.with_messages(
                        self.behaviour._message_from_reply(reply),
                    ).next_iteration()
                    self.behaviour._fold_iteration(
                        state=next_state,
                        reply=reply,
                        tool_calls=(),
                        tool_results=(),
                        claim_id_prefix=self._iteration_claim_prefix(state.iteration),
                    )
                    result = FinalResult(
                        text=self.behaviour._extract_text(reply),
                        state=next_state,
                        stop_reason=(
                            TerminationReason.MAX_TOKENS
                            if reply.stop_reason == "max_tokens"
                            else TerminationReason.NATURAL_STOP
                        ),
                        metadata={"iterations": next_state.iteration},
                    )
                    result = self._supervisor_result(
                        next_state,
                        iteration=state.iteration,
                    ) or result
                    phase, payload, version = self._checkpoint_terminal(
                        next_state, result, version,
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
                        self.behaviour.tool_harness.call(
                            tool_name=str(tool_call["name"]),
                            arguments=dict(tool_call.get("input") or {}),
                            effect_id=self._tool_effect_id(
                                state.iteration, next_index + offset,
                            ),
                            tool_use_id=str(tool_call["id"]),
                            caused_by=self.run.id,
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

                next_state = state.with_messages(
                    self.behaviour._message_from_reply(reply),
                    self.behaviour._message_from_tool_results(tool_results),
                ).next_iteration()
                self.behaviour._fold_iteration(
                    state=next_state,
                    reply=reply,
                    tool_calls=tool_calls,
                    tool_results=tool_results,
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
                payload = self._payload(next_state)
                checkpoint = self.store.save_checkpoint(
                    self.run.id,
                    self._token,
                    expected_version=version,
                    phase=_PHASE_READY,
                    state=payload,
                )
                version, phase = checkpoint.version, checkpoint.phase
        except RunSuspended:
            raise
        except Exception as exc:
            self._fail_run(exc)
            raise

    def _fail_run(self, exc: Exception) -> None:
        with contextlib.suppress(Exception):
            self.store.fail_run(
                self.run.id,
                self._token,
                error={"type": type(exc).__name__, "message": str(exc)},
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

    @staticmethod
    def _positive_seconds(value: float, name: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise ValueError(f"{name} must be a positive finite number")
        return float(value)
