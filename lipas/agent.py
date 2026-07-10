"""Ergonomic high-level agent entry point.

``DeclarativeAgent`` remains the explicit wiring API.  ``Agent`` is the
small default for application code: it provisions the standard audited rowset
and accepts a plain iterable of explicitly classified tools.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .adapter.protocol import LLMAdapter
from .behaviour import AgentState, FinalResult
from .calculus import StrategyRegistry
from .declarativeagent import DeclarativeAgent
from .rows import RowSet
from .rows.capability import CapabilityRow
from .rows.effect import EffectRow
from .rows.history import HistoryRow
from .session import open_session
from .skills import Skill, SkillRegistry
from .store import ClaimStore
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
    skills: SkillRegistry | Sequence[Skill] = field(default_factory=SkillRegistry)
    session_path: str | None = None
    budgets: Mapping[str, float] | None = None
    harness_kwargs: Mapping[str, Any] = field(default_factory=dict)
    tool_guards: Sequence[Any] = ()
    request_extras: Mapping[str, Any] = field(default_factory=dict)
    registry: StrategyRegistry | None = None

    rowset: RowSet = field(init=False)
    _delegate: DeclarativeAgent = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.instructions is not None and self.system:
            raise ValueError("pass either instructions= or system=, not both")
        system = self.instructions if self.instructions is not None else self.system
        tool_registry = self.tools if isinstance(self.tools, ToolRegistry) else ToolRegistry(self.tools)
        if self.session_path is not None:
            self.rowset = open_session(self.session_path, registry=self.registry)
        else:
            self.rowset = RowSet(ClaimStore(registry=self.registry), [
                HistoryRow(), CapabilityRow(budgets=dict(self.budgets or {})), EffectRow(),
            ])
        self._delegate = DeclarativeAgent(
            adapter=self.adapter, tools=tool_registry, rowset=self.rowset,
            model=self.model, system=system, max_tokens=self.max_tokens,
            max_iterations=self.max_iterations, skills=self.skills,
            harness_kwargs=self.harness_kwargs, tool_guards=self.tool_guards,
            request_extras=self.request_extras,
        )

    async def run(self, prompt: str | tuple[Any, ...] | list[Any], *, state: AgentState | None = None) -> FinalResult:
        return await self._delegate.run(prompt, state=state)

    async def __call__(self, prompt: str | tuple[Any, ...] | list[Any]) -> FinalResult:
        """Allow the natural ``result = await agent('...')`` spelling."""
        return await self.run(prompt)

    def close(self) -> None:
        """Close the durable store, if this Agent created one."""
        close = getattr(self.rowset.store, "close", None)
        if callable(close):
            close()
