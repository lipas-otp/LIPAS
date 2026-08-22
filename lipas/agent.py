"""The small, complete high-level Agent API."""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .adapter import PriceTable, Request
from .adapter.protocol import LLMAdapter
from .behaviour import AgentState, FinalResult
from .context import RunContext
from .conversation_store import SessionStore, SQLiteSessionStore
from .events import AgentEvent, AgentEventType, EventEmitter, EventSink
from .calculus import StrategyRegistry
from .harness import LLMHarness
from .react import ReActAgent
from .rows import RowSet
from .rows.capability import CapabilityRow
from .rows.effect import EffectRow
from .rows.history import HistoryRow
from .session import open_session
from .skills import Skill, SkillRegistry
from .models import (
    ModelCapabilities, ModelCapabilityReport, ModelRegistry, ModelRequirements,
)
from .observer import RunObserver
from .store import ClaimStore
from .supervisor import Policy, Supervisor
from .tool_harness import ToolHarness
from .tools import Tool, ToolRegistry

if TYPE_CHECKING:
    from .durable import ApprovalPolicy, InputPolicy
    from .execution import ExecutionStore, Run

__all__ = ["Agent"]

logger = logging.getLogger(__name__)


class _StreamFailure:
    def __init__(self, error: BaseException) -> None:
        self.error = error


async def _deliver_durable_event(sink: EventSink, event: AgentEvent) -> bool:
    """Deliver one UI event without letting a sink override durable truth."""
    try:
        delivered = sink(event)
        if inspect.isawaitable(delivered):
            await delivered
    except Exception:
        logger.exception("durable AgentEvent sink failed; continuing")
        return False
    return True


@dataclass
class Agent:
    """A natural default for a single, auditable ReAct agent.

    ``tools`` can be a list of ``@tool(side_effect=...)`` objects, so ordinary
    Python functions remain the authoring unit while the runtime retains the
    side-effect declaration it needs for replay and policy enforcement.
    Set ``session_path`` to make the complete claim tape durable.
    """

    adapter: LLMAdapter
    tools: ToolRegistry | Iterable[Tool] = ()
    model: str = "claude-sonnet-4-5-20250929"
    system: str = ""
    # ``instructions`` is the familiar name used by Claude-style agent
    # examples. ``system`` remains a supported provider-neutral alias.
    instructions: str | None = None
    max_tokens: int = 4096
    max_iterations: int = 10
    max_parallel_tools: int = 4
    skills: SkillRegistry | Sequence[Skill] | str | Path = field(default_factory=SkillRegistry)
    session_path: str | Path | None = None
    budgets: Mapping[str, float] | None = None
    harness_kwargs: Mapping[str, Any] = field(default_factory=dict)
    tool_guards: Sequence[Any] = ()
    request_extras: Mapping[str, Any] = field(default_factory=dict)
    registry: StrategyRegistry | None = None
    supervisor_policy: Policy | None = None
    supervisor_session_id: str | None = None
    observers: Sequence[RunObserver] = ()
    honor_observer_recommendations: bool = False
    model_registry: ModelRegistry | None = None
    model_requirements: ModelRequirements | None = None
    session_store: SessionStore | None = None

    rowset: RowSet = field(init=False)
    capabilities: ModelCapabilities = field(init=False)
    capability_report: ModelCapabilityReport = field(init=False)
    # Exposed for the rare application that needs to inspect its own complete
    # audited loop. Most callers only need ``await agent(prompt)``.
    harness: LLMHarness = field(init=False, repr=False)
    tool_harness: ToolHarness = field(init=False, repr=False)
    behaviour: ReActAgent = field(init=False, repr=False)
    _owns_session_store: bool = field(init=False, default=False, repr=False)

    @classmethod
    def ollama(
        cls,
        model: str = "gemma4:12b",
        *,
        session: str | Path | None = None,
        host: str | None = None,
        timeout_s: float = 500.0,
        **kwargs: Any,
    ) -> "Agent":
        """Create a local Ollama-backed Agent with one short expression.

        ``session=`` is the friendly alias for ``session_path=``.  This
        convenience constructor changes no policy: tools still need an
        explicit side-effect declaration and all other ``Agent`` keywords
        remain available through ``kwargs``.
        """
        if session is not None:
            if "session_path" in kwargs:
                raise ValueError("pass either session= or session_path=, not both")
            kwargs["session_path"] = session
        from .adapter.ollama import OllamaAdapter
        return cls(
            adapter=OllamaAdapter(host=host, timeout_s=timeout_s),
            model=model,
            **kwargs,
        )

    @classmethod
    def openai_compatible(
        cls,
        model: str,
        *,
        base_url: str,
        api_key: str | None = None,
        api_key_env: str | None = "OPENAI_API_KEY",
        require_api_key: bool = True,
        session: str | Path | None = None,
        timeout_s: float = 120.0,
        streaming: bool = False,
        include_usage: bool = False,
        max_tokens_field: str = "max_tokens",
        headers: Mapping[str, str] | None = None,
        prices: PriceTable | None = None,
        client: Any | None = None,
        adapter_name: str | None = None,
        **kwargs: Any,
    ) -> "Agent":
        """Build an Agent for an OpenAI-compatible Chat Completions API.

        The endpoint, model, and credential are explicit.  Non-streaming is
        the compatibility-first default; pass ``streaming=True`` only when the
        selected provider/model route implements the SSE contract.  The
        adapter performs no silent provider or model fallback.
        """
        if session is not None:
            if "session_path" in kwargs:
                raise ValueError("pass either session= or session_path=, not both")
            kwargs["session_path"] = session
        from .adapter.openai_compatible import OpenAICompatibleAdapter
        return cls(
            adapter=OpenAICompatibleAdapter(
                base_url=base_url,
                api_key=api_key,
                api_key_env=api_key_env,
                require_api_key=require_api_key,
                prices=prices,
                timeout_s=timeout_s,
                streaming=streaming,
                include_usage=include_usage,
                max_tokens_field=max_tokens_field,
                headers=headers,
                client=client,
                name=adapter_name,
            ),
            model=model,
            **kwargs,
        )

    def __post_init__(self) -> None:
        if self.instructions is not None and self.system:
            raise ValueError("pass either instructions= or system=, not both")
        system = self.instructions if self.instructions is not None else self.system
        if self.session_store is not None and not isinstance(
            self.session_store, SessionStore,
        ):
            raise TypeError("session_store must implement SessionStore or be None")
        model_registry = self.model_registry or ModelRegistry.default()
        if not isinstance(model_registry, ModelRegistry):
            raise TypeError("model_registry must be a ModelRegistry or None")
        # Copy registry state at composition time so a live Agent's advertised
        # capabilities cannot change behind its back.
        model_registry = ModelRegistry(model_registry.list())
        self.model_registry = model_registry
        provider = getattr(self.adapter, "name", "unknown")
        if not isinstance(provider, str) or not provider:
            provider = "unknown"
        requirements = self.model_requirements or ModelRequirements()
        if not isinstance(requirements, ModelRequirements):
            raise TypeError("model_requirements must be ModelRequirements or None")
        self.capability_report = model_registry.validate(
            provider, self.model, requirements,
        )
        self.capabilities = self.capability_report.capabilities
        if self.model_requirements is not None:
            model_registry.require(provider, self.model, requirements)
        tool_registry = (
            self.tools
            if isinstance(self.tools, ToolRegistry)
            else ToolRegistry(self.tools)
        )
        try:
            self._compose_runtime(tool_registry, system)
        except BaseException:
            self._close_resources()
            raise

    def _compose_runtime(self, tool_registry: ToolRegistry, system: str) -> None:
        if self.session_path is not None:
            self.rowset = open_session(
                self.session_path,
                registry=self.registry,
                budgets=self.budgets,
            )
            if self.session_store is None:
                self.session_store = SQLiteSessionStore(self.session_path)
                self._owns_session_store = True
        else:
            self.rowset = RowSet(ClaimStore(registry=self.registry), [
                HistoryRow(), CapabilityRow(budgets=dict(self.budgets or {})), EffectRow(),
            ])
        supervisor = None
        if self.supervisor_policy is not None:
            supervisor_session_id = self.supervisor_session_id or (
                str(self.session_path)
                if self.session_path is not None else "in-memory-agent"
            )
            supervisor = Supervisor(
                self.supervisor_policy, self.rowset,
                supervisor_session_id,
            )
        # A Skill directory is a pleasant default for ordinary projects:
        # ``skills="skills/support-triage"``.  Advanced callers can still
        # provide an explicit registry or already-loaded Skill objects.
        if isinstance(self.skills, SkillRegistry):
            skill_registry = self.skills
        elif isinstance(self.skills, (str, Path)):
            from .skills import discover_skills
            skill_registry = SkillRegistry(discover_skills(self.skills))
        else:
            skill_registry = SkillRegistry(self.skills)
        self.harness = LLMHarness(
            adapter=self.adapter,
            rowset=self.rowset,
            **dict(self.harness_kwargs),
        )
        self.tool_harness = ToolHarness(
            tools=tool_registry,
            rowset=self.rowset,
            guards=tuple(self.tool_guards),
        )
        self.behaviour = ReActAgent(
            harness=self.harness,
            tools=tool_registry,
            tool_harness=self.tool_harness,
            rowset=self.rowset,
            request_template=Request(
                model=self.model,
                messages=(),
                max_tokens=self.max_tokens,
                system=skill_registry.system_prompt(system),
                extra=dict(self.request_extras),
            ),
            max_iterations=self.max_iterations,
            max_parallel_tools=self.max_parallel_tools,
            supervisor=supervisor,
            observers=tuple(self.observers),
            honor_observer_recommendations=self.honor_observer_recommendations,
        )

    def _close_resources(self) -> BaseException | None:
        first_error: BaseException | None = None
        resources: list[Any] = []
        if self._owns_session_store and self.session_store is not None:
            resources.append(self.session_store)
            self._owns_session_store = False
        rowset = getattr(self, "rowset", None)
        if rowset is not None:
            resources.append(rowset.store)
        for resource in resources:
            close = getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        return first_error

    async def _run_internal(
        self,
        prompt: str | tuple[Any, ...] | list[Any],
        *,
        state: AgentState | None = None,
        context: RunContext | None = None,
        event_emitter: EventEmitter | None = None,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> FinalResult:
        if context is not None and (timeout_s is not None or deadline is not None):
            raise ValueError("a supplied RunContext already owns its deadline")
        if context is None:
            context = RunContext.create(timeout_s=timeout_s, deadline=deadline)
        if event_emitter is not None and event_emitter.run_id != context.run_id:
            raise ValueError("EventEmitter and RunContext must share run_id")
        messages = self._messages_from_prompt(prompt)
        initial = (
            AgentState(messages=tuple(messages))
            if state is None else state.with_messages(*messages)
        )
        initial = initial.with_metadata({
            **initial.metadata,
            "run_id": context.run_id,
        })
        return await self.behaviour.run(
            initial,
            context=context,
            event_emitter=event_emitter,
        )

    async def run(
        self,
        prompt: str | tuple[Any, ...] | list[Any],
        *,
        state: AgentState | None = None,
        context: RunContext | None = None,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> FinalResult:
        return await self._run_internal(
            prompt,
            state=state,
            context=context,
            timeout_s=timeout_s,
            deadline=deadline,
        )

    async def stream(
        self,
        prompt: str | tuple[Any, ...] | list[Any],
        *,
        state: AgentState | None = None,
        context: RunContext | None = None,
        timeout_s: float | None = None,
        deadline: float | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Yield the same lifecycle/model/tool protocol used by Sessions."""
        if context is not None and (timeout_s is not None or deadline is not None):
            raise ValueError("a supplied RunContext already owns its deadline")
        context = context or RunContext.create(
            timeout_s=timeout_s, deadline=deadline,
        )
        queue: asyncio.Queue[Any] = asyncio.Queue()
        sentinel = object()

        async def sink(event: AgentEvent) -> None:
            await queue.put(event)

        async def produce() -> None:
            emitter = EventEmitter(context.run_id, sink)
            try:
                await emitter.emit(
                    AgentEventType.RUN_STARTED,
                    data={
                        "model": self.model,
                        "tools": [tool.name for tool in self.tool_harness.tools],
                    },
                )
                result = await self._run_internal(
                    prompt,
                    state=state,
                    context=context,
                    event_emitter=emitter,
                )
                terminal_type = (
                    AgentEventType.RUN_CANCELLED
                    if result.stop_reason == "cancelled"
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
            except BaseException as exc:
                if not isinstance(exc, asyncio.CancelledError):
                    await emitter.emit(
                        AgentEventType.RUN_FAILED,
                        data={
                            "exception": type(exc).__name__,
                            "message": str(exc),
                        },
                    )
                    await queue.put(_StreamFailure(exc))
            finally:
                await queue.put(sentinel)

        producer = asyncio.create_task(produce())
        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    break
                if isinstance(item, _StreamFailure):
                    raise item.error
                yield item
        finally:
            if not producer.done():
                context.cancel()
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    async def run_durable(
        self,
        prompt: str | tuple[Any, ...] | list[Any] | None,
        *,
        execution_store: ExecutionStore,
        run_id: str,
        lease_seconds: float = 300.0,
        heartbeat_interval_s: float | None = None,
        phase_timeout_s: float | None = None,
        approval_policy: ApprovalPolicy | None = None,
        input_policy: InputPolicy | None = None,
        context: RunContext | None = None,
        timeout_s: float | None = None,
        deadline: float | None = None,
        event_sink: EventSink | None = None,
        event_cursor: int | None = None,
        _claimed_run: Run | None = None,
    ) -> FinalResult:
        """Run or resume this Agent through the durable ReAct phase machine.

        A new run requires ``prompt``.  Resume an existing checkpoint with
        ``prompt=None`` so the original input cannot accidentally be appended
        twice.  The Agent's claim session must be SQLite-backed: execution
        checkpoints and the Effect tape are separate durable records and both
        are required for safe recovery.
        """
        from .durable import DurableReActRunner, settled_result_from_run
        from .execution import ExecutionStore, RunState
        from .serialization.store_sqlite import SqliteClaimStore

        if not isinstance(execution_store, ExecutionStore):
            raise TypeError("execution_store must be an ExecutionStore")
        if not isinstance(self.rowset.store, SqliteClaimStore):
            raise ValueError(
                "durable Agent execution requires session_path= or session=",
            )
        run = execution_store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if context is not None and (timeout_s is not None or deadline is not None):
            raise ValueError("a supplied RunContext already owns its deadline")
        if context is None:
            context = RunContext.create(
                run_id=run_id, timeout_s=timeout_s, deadline=deadline,
            )
        elif context.run_id != run_id:
            raise ValueError("durable RunContext.run_id must equal run_id")
        prior_cancel_check = context.cancel_check

        def durable_cancel_requested() -> bool:
            if prior_cancel_check is not None and prior_cancel_check():
                return True
            current = execution_store.get_run(run_id)
            return current is not None and current.cancel_requested

        context.cancel_check = durable_cancel_requested
        delivery_cursor = execution_store.agent_event_cursor(run_id)
        if event_cursor is not None:
            if event_sink is None:
                raise ValueError("event_cursor requires event_sink")
            if isinstance(event_cursor, bool) or not isinstance(event_cursor, int) or event_cursor < 0:
                raise ValueError("event_cursor must be a non-negative int")
            delivery_cursor = event_cursor
            for event in execution_store.agent_events(run_id, after=event_cursor):
                if not await _deliver_durable_event(event_sink, event):
                    event_sink = None
                    break
                delivery_cursor = event.sequence
        checkpoint = execution_store.get_checkpoint(run_id)
        if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
            if prompt is not None:
                raise ValueError(
                    "a terminal durable run can only be restored with prompt=None",
                )
            result = settled_result_from_run(
                run,
                checkpoint,
                claim_store_id=self.rowset.store.store_id,
            )
            if result.stop_reason == "cancelled":
                terminal_event = execution_store.append_agent_event(
                    run_id,
                    AgentEventType.RUN_CANCELLED,
                    identity="run:cancelled",
                    iteration=result.state.iteration,
                    data={"stop_reason": result.stop_reason},
                )
            elif checkpoint is not None and checkpoint.phase == "terminal":
                terminal_event = execution_store.append_agent_event(
                    run_id,
                    AgentEventType.RUN_COMPLETED,
                    identity="run:completed",
                    iteration=result.state.iteration,
                    data={
                        "text": result.text,
                        "stop_reason": result.stop_reason,
                        "error": dict(result.error) if result.error else None,
                        "metadata": dict(result.metadata),
                    },
                )
            else:
                terminal_event = execution_store.append_agent_event(
                    run_id,
                    AgentEventType.RUN_FAILED,
                    identity="run:failed",
                    data={"error": dict(run.error or {})},
                )
            if event_sink is not None and terminal_event.sequence > delivery_cursor:
                await _deliver_durable_event(event_sink, terminal_event)
            return result
        if checkpoint is None:
            if prompt is None and not run.cancel_requested:
                raise ValueError("a new durable run requires a prompt")
            messages = [] if prompt is None else self._messages_from_prompt(prompt)
            initial = AgentState(
                messages=tuple(messages),
                metadata={
                    "caused_by": run_id,
                    "execution_run_id": run_id,
                    "run_id": run_id,
                },
            )
        else:
            if prompt is not None:
                raise ValueError(
                    "resume a durable run with prompt=None; its input is checkpointed",
                )
            initial = None
        if _claimed_run is None:
            claimed = execution_store.claim_run(
                run_id,
                lease_seconds=lease_seconds,
            )
        else:
            current = execution_store.get_run(run_id)
            if (
                _claimed_run.id != run_id
                or current is None
                or current.state is not RunState.RUNNING
                or not current.lease_token
                or current.lease_token != _claimed_run.lease_token
            ):
                raise ValueError("_claimed_run is not the active Run lease")
            claimed = current
        runner = DurableReActRunner(
            self.behaviour,
            execution_store,
            claimed,
            approval_policy=approval_policy,
            input_policy=input_policy,
            lease_seconds=lease_seconds,
            heartbeat_interval_s=heartbeat_interval_s,
            phase_timeout_s=phase_timeout_s,
            context=context,
            event_sink=event_sink,
            event_cursor=delivery_cursor,
        )
        return await runner.run_to_completion(initial)

    async def resume_durable(
        self,
        *,
        execution_store: ExecutionStore,
        run_id: str,
        lease_seconds: float = 300.0,
        heartbeat_interval_s: float | None = None,
        phase_timeout_s: float | None = None,
        approval_policy: ApprovalPolicy | None = None,
        input_policy: InputPolicy | None = None,
        context: RunContext | None = None,
        timeout_s: float | None = None,
        deadline: float | None = None,
        event_sink: EventSink | None = None,
        event_cursor: int | None = None,
    ) -> FinalResult:
        """Resume a checkpointed durable run without appending new input."""
        return await self.run_durable(
            None,
            execution_store=execution_store,
            run_id=run_id,
            lease_seconds=lease_seconds,
            heartbeat_interval_s=heartbeat_interval_s,
            phase_timeout_s=phase_timeout_s,
            approval_policy=approval_policy,
            input_policy=input_policy,
            context=context,
            timeout_s=timeout_s,
            deadline=deadline,
            event_sink=event_sink,
            event_cursor=event_cursor,
        )

    async def __call__(self, prompt: str | tuple[Any, ...] | list[Any]) -> FinalResult:
        """Allow the natural ``result = await agent('...')`` spelling."""
        return await self.run(prompt)

    def session(
        self,
        *,
        session_id: str | None = None,
        state: AgentState | None = None,
    ):
        """Create an explicit, optionally persisted conversation Session."""
        from .conversation import Session
        return Session(
            self,
            session_id=session_id,
            state=state,
            store=self.session_store,
        )

    def ask(
        self,
        prompt: str | tuple[Any, ...] | list[Any],
    ) -> FinalResult:
        """Run one prompt from a normal synchronous Python script.

        This is the recommended first-touch API: ``agent.ask("...")``.
        Async applications should use ``await agent.run("...")`` instead.
        Calling this method from an already-running event loop is rejected
        rather than trying to nest loops and producing surprising behaviour.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run(prompt))
        raise RuntimeError(
            "Agent.ask() cannot run inside an active event loop; "
            "use `await agent.run(...)` instead"
        )

    @staticmethod
    def _messages_from_prompt(
        prompt: str | tuple[Any, ...] | list[Any],
    ) -> list[Any]:
        if isinstance(prompt, str):
            return [{"role": "user", "content": prompt}]
        if isinstance(prompt, (tuple, list)):
            return list(prompt)
        raise TypeError(
            f"prompt must be str, tuple, or list; got {type(prompt).__name__}"
        )

    def close(self) -> None:
        """Close the durable store, if this Agent created one."""
        error = self._close_resources()
        if error is not None:
            raise error

    def __enter__(self) -> "Agent":
        """Support context-managed Agent factories in normal scripts."""
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
