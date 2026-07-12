"""The small, complete high-level Agent API."""
from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .adapter import Request
from .adapter.protocol import LLMAdapter
from .behaviour import AgentState, FinalResult
from .calculus import StrategyRegistry
from .harness import LLMHarness
from .react import ReActAgent
from .rows import RowSet
from .rows.capability import CapabilityRow
from .rows.effect import EffectRow
from .rows.history import HistoryRow
from .session import open_session
from .skills import Skill, SkillRegistry
from .store import ClaimStore
from .supervisor import Policy, Supervisor
from .tool_harness import ToolHarness
from .tools import Tool, ToolRegistry

__all__ = ["Agent"]


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
    skills: SkillRegistry | Sequence[Skill] | str | Path = field(default_factory=SkillRegistry)
    session_path: str | Path | None = None
    budgets: Mapping[str, float] | None = None
    harness_kwargs: Mapping[str, Any] = field(default_factory=dict)
    tool_guards: Sequence[Any] = ()
    request_extras: Mapping[str, Any] = field(default_factory=dict)
    registry: StrategyRegistry | None = None
    supervisor_policy: Policy | None = None
    supervisor_session_id: str | None = None

    rowset: RowSet = field(init=False)
    # Exposed for the rare application that needs to inspect its own complete
    # audited loop. Most callers only need ``await agent(prompt)``.
    harness: LLMHarness = field(init=False, repr=False)
    tool_harness: ToolHarness = field(init=False, repr=False)
    behaviour: ReActAgent = field(init=False, repr=False)

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

    def __post_init__(self) -> None:
        if self.instructions is not None and self.system:
            raise ValueError("pass either instructions= or system=, not both")
        system = self.instructions if self.instructions is not None else self.system
        tool_registry = self.tools if isinstance(self.tools, ToolRegistry) else ToolRegistry(self.tools)
        if self.session_path is not None:
            self.rowset = open_session(
                self.session_path,
                registry=self.registry,
                budgets=self.budgets,
            )
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
            supervisor=supervisor,
        )

    async def run(self, prompt: str | tuple[Any, ...] | list[Any], *, state: AgentState | None = None) -> FinalResult:
        messages = self._messages_from_prompt(prompt)
        initial = (
            AgentState(messages=tuple(messages))
            if state is None else state.with_messages(*messages)
        )
        return await self.behaviour.run(initial)

    async def __call__(self, prompt: str | tuple[Any, ...] | list[Any]) -> FinalResult:
        """Allow the natural ``result = await agent('...')`` spelling."""
        return await self.run(prompt)

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
        close = getattr(self.rowset.store, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "Agent":
        """Support ``with Agent.ollama(...) as agent`` in normal scripts."""
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
