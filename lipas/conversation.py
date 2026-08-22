"""High-level conversational Sessions and asynchronous RunHandles."""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .behaviour import AgentState, FinalResult, TerminationReason
from .context import CancellationToken, RunContext
from .conversation_store import SessionStore
from .events import AgentEvent, AgentEventType, EventEmitter

if TYPE_CHECKING:
    from .agent import Agent

__all__ = ["Session", "RunHandle"]


class Session:
    """Own one explicit conversation state while reusing an Agent runtime."""

    def __init__(
        self,
        agent: "Agent",
        *,
        session_id: str | None = None,
        state: AgentState | None = None,
        store: SessionStore | None = None,
    ) -> None:
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id.strip()
        ):
            raise ValueError("session_id must be a non-empty string or None")
        if state is not None and not isinstance(state, AgentState):
            raise TypeError("state must be an AgentState or None")
        if store is not None and not isinstance(store, SessionStore):
            raise TypeError("store must implement SessionStore or be None")
        self.agent = agent
        self.id = session_id.strip() if session_id else f"session_{uuid.uuid4().hex}"
        self.store = store
        snapshot = None if store is None else store.load(self.id)
        if state is not None and snapshot is not None:
            raise ValueError("pass state only when no persisted snapshot exists")
        self._state = snapshot.state if snapshot is not None else state or AgentState()
        self._version = 0 if snapshot is None else snapshot.version
        self._active: RunHandle | None = None

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def version(self) -> int:
        return self._version

    def reset(self) -> None:
        if self._active is not None and not self._active.done:
            raise RuntimeError("cannot reset a Session while a run is active")
        self._commit_state(AgentState())

    def _commit_state(self, state: AgentState) -> None:
        if self.store is not None:
            snapshot = self.store.save(
                self.id, state, expected_version=self._version,
            )
            self._version = snapshot.version
        self._state = state

    def _next_run_state(self) -> AgentState:
        return AgentState(
            messages=self._state.messages,
            metadata=dict(self._state.metadata),
        )

    async def run(
        self,
        prompt: str | tuple[Any, ...] | list[Any],
        *,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> FinalResult:
        handle = self.start(prompt, timeout_s=timeout_s, deadline=deadline)
        try:
            return await handle.result()
        except asyncio.CancelledError:
            handle.cancel()
            await asyncio.shield(asyncio.gather(handle._task, return_exceptions=True))
            raise

    def start(
        self,
        prompt: str | tuple[Any, ...] | list[Any],
        *,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> "RunHandle":
        if self._active is not None and not self._active.done:
            raise RuntimeError("a Session can run only one prompt at a time")
        handle = RunHandle(
            self, prompt, timeout_s=timeout_s, deadline=deadline,
        )
        self._active = handle
        return handle

    async def stream(
        self,
        prompt: str | tuple[Any, ...] | list[Any],
        *,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> AsyncIterator[AgentEvent]:
        handle = self.start(prompt, timeout_s=timeout_s, deadline=deadline)
        try:
            async for event in handle.events():
                yield event
        finally:
            if not handle.done:
                handle.cancel()
                await asyncio.gather(handle._task, return_exceptions=True)


class RunHandle:
    """A running Session call with one identity, stream, result and cancel."""

    def __init__(
        self,
        session: Session,
        prompt: str | tuple[Any, ...] | list[Any],
        *,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> None:
        self.session = session
        self.prompt = prompt
        self.id = f"run_{uuid.uuid4().hex}"
        self.context = RunContext.create(
            run_id=self.id,
            timeout_s=timeout_s,
            deadline=deadline,
            cancellation=CancellationToken(),
            metadata={"session_id": session.id},
        )
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._sentinel = object()
        self._result_future: asyncio.Future[FinalResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._task = asyncio.create_task(self._produce())

    @property
    def done(self) -> bool:
        return self._task.done()

    @property
    def cancelled(self) -> bool:
        if not self._result_future.done() or self._result_future.cancelled():
            return False
        if self._result_future.exception() is not None:
            return False
        return self._result_future.result().stop_reason == TerminationReason.CANCELLED

    def cancel(self) -> bool:
        if self.done:
            return False
        return self.context.cancel()

    async def result(self) -> FinalResult:
        return await asyncio.shield(self._result_future)

    def __await__(self):
        return self.result().__await__()

    async def wait(self) -> FinalResult:
        return await self.result()

    async def events(self) -> AsyncIterator[AgentEvent]:
        while True:
            item = await self._queue.get()
            if item is self._sentinel:
                break
            if isinstance(item, _RunFailure):
                raise item.error
            yield item

    async def _produce(self) -> None:
        async def sink(event: AgentEvent) -> None:
            await self._queue.put(event)

        emitter = EventEmitter(self.id, sink)
        try:
            await emitter.emit(
                AgentEventType.RUN_STARTED,
                data={
                    "model": self.session.agent.model,
                    "tools": [tool.name for tool in self.session.agent.tool_harness.tools],
                    "session_id": self.session.id,
                },
            )
            result = await self.session.agent._run_internal(
                self.prompt,
                state=self.session._next_run_state(),
                event_emitter=emitter,
                context=self.context,
            )
            if result.stop_reason == TerminationReason.CANCELLED:
                # A withdrawn turn must not become conversation authority.
                result = replace(result, state=self.session.state)
            else:
                self.session._commit_state(result.state)
            terminal_type = (
                AgentEventType.RUN_CANCELLED
                if result.stop_reason == TerminationReason.CANCELLED
                else AgentEventType.RUN_COMPLETED
            )
            await emitter.emit(
                terminal_type,
                iteration=result.state.iteration,
                data={
                    "text": result.text,
                    "stop_reason": result.stop_reason,
                    "error": dict(result.error) if result.error else None,
                    "metadata": dict(result.metadata),
                },
            )
            if not self._result_future.done():
                self._result_future.set_result(result)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                result = FinalResult(
                    state=self.session.state,
                    stop_reason=TerminationReason.CANCELLED,
                    metadata={"run_id": self.id},
                )
                if not self._result_future.done():
                    self._result_future.set_result(result)
                await emitter.emit(
                    AgentEventType.RUN_CANCELLED,
                    data={"stop_reason": TerminationReason.CANCELLED},
                )
            else:
                await emitter.emit(
                    AgentEventType.RUN_FAILED,
                    data={
                        "exception": type(exc).__name__,
                        "message": str(exc),
                    },
                )
                if not self._result_future.done():
                    self._result_future.set_exception(exc)
                await self._queue.put(_RunFailure(exc))
        finally:
            await self._queue.put(self._sentinel)


class _RunFailure:
    def __init__(self, error: BaseException) -> None:
        self.error = error
