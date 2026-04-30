"""
LIPAS · P3.4 — Declarative agent builder.

Convenience wiring layer. See module banner for when (not) to use.

P3.1 wiring (vs pre-P3.1)
-------------------------
DeclarativeAgent now constructs a ToolHarness alongside the LLMHarness
and hands BOTH to the behaviour.  Three things to know:

  1. The tool harness shares ``rowset`` with the LLM harness.  This is
     the whole point — tool effects and LLM effects fold into the same
     ClaimStore so EffectRow lineage and CapabilityRow spend tracking
     are unified.  We do NOT expose a knob to split rowsets here; if
     you need that, build the harnesses by hand and bypass this builder.

  2. ``tool_guards`` is independent of any LLM-side guards (which live
     inside ``harness_kwargs``).  Cross-cutting guards (cost ceilings,
     rate limits) that should apply to BOTH must be passed to BOTH
     places — DeclarativeAgent does not auto-share guard instances
     because some guards hold per-instance state (rate-limit counters,
     etc.) and silent sharing would be a footgun.

  3. There is no ``tool_harness_kwargs`` mirror of ``harness_kwargs``
     yet — ToolHarness's surface (tools / rowset / guards) is small
     enough to be expressible as flat fields.  If ToolHarness grows
     more knobs, add the kwargs bag here too.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .adapter import LLMAdapter, Request
from .behaviour import AgentState, FinalResult
from .harness import LLMHarness
from .react import ReActAgent
from .rows import RowSet
from .tool_harness import ToolHarness
from .tools import ToolRegistry


__all__ = ["DeclarativeAgent"]


@dataclass
class DeclarativeAgent:
    """Flat-config builder around ReActAgent + LLMHarness + ToolHarness + RowSet.

    rowset is REQUIRED — there is no canned default because RowSet
    needs a ClaimStore, and which store/rows you want is a deployment
    decision (in-memory vs persisted, which rows to fold). Build one
    explicitly:

        store   = ClaimStore(...)
        rowset  = RowSet(store, [HistoryRow(), CapabilityRow(), EffectRow(), ...])
        agent   = DeclarativeAgent(adapter=..., tools=..., rowset=rowset)

    Tool-side policy (P3.1):

        agent = DeclarativeAgent(
            adapter=...,
            tools=...,
            rowset=...,
            tool_guards=(MyToolRateLimiter(),),
        )

    EffectRow is strongly recommended in the rowset whenever ``tools``
    is non-empty, but it is not enforced here — agents that intentionally
    drop the audit trail (e.g. read-only demos) should not be forbidden.
    """

    adapter:        LLMAdapter
    tools:          ToolRegistry
    rowset:         RowSet

    model:          str  = "claude-sonnet-4-5-20250929"
    system:         str  = ""              # not Optional — Request.system is str
    max_tokens:     int  = 4096
    max_iterations: int  = 10

    # Per-call policy.  ``harness_kwargs`` flows into LLMHarness
    # (LLM-side guards, retry policy, etc.); ``tool_guards`` flows
    # into ToolHarness.  See class docstring re: cross-cutting guards.
    harness_kwargs: Mapping[str, Any]   = field(default_factory=dict)
    tool_guards:    Sequence[Any]       = ()
    request_extras: Mapping[str, Any]   = field(default_factory=dict)
    behaviour_cls:  type                = ReActAgent

    _harness:      LLMHarness  = field(init=False, repr=False)
    _tool_harness: ToolHarness = field(init=False, repr=False)
    _behaviour:    ReActAgent  = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # RowSet doesn't have a sensible no-arg factory (it needs a
        # ClaimStore); refuse rather than guess.
        if self.rowset is None:
            raise ValueError(
                "rowset is required. Build one with "
                "RowSet(store, rows=[...]) and pass it in."
            )

        # 1. LLM harness (Reason side).
        self._harness = LLMHarness(
            adapter=self.adapter,
            rowset=self.rowset,
            **dict(self.harness_kwargs),
        )

        # 2. Tool harness (Act side).  Same rowset by construction —
        #    this is what unifies LLM and tool effects under one
        #    EffectRow lineage and one CapabilityRow spend ledger.
        self._tool_harness = ToolHarness(
            tools=self.tools,
            rowset=self.rowset,
            guards=tuple(self.tool_guards),
        )

        # 3. Request prototype.  ``messages`` and ``tools`` are filled
        #    in per iteration by the behaviour via dataclasses.replace.
        template = Request(
            model=self.model,
            messages=(),
            max_tokens=self.max_tokens,
            system=self.system,
            extra=dict(self.request_extras),
        )

        # 4. Behaviour.  Receives BOTH harnesses; behaviour decides
        #    which one to call when.
        self._behaviour = self.behaviour_cls(
            harness=self._harness,
            tools=self.tools,
            tool_harness=self._tool_harness,
            rowset=self.rowset,
            request_template=template,
            max_iterations=self.max_iterations,
        )

    # ── public API ─────────────────────────────────────────────

    async def run(
        self,
        prompt: str | tuple[Any, ...] | list[Any],
        *,
        state: AgentState | None = None,
    ) -> FinalResult:
        """See module docstring."""
        new_messages = self._messages_from_prompt(prompt)
        if state is None:
            initial = AgentState(messages=tuple(new_messages))
        else:
            initial = state.with_messages(*new_messages)
        return await self._behaviour.run(initial)

    # ── helpers ────────────────────────────────────────────────

    @staticmethod
    def _messages_from_prompt(
        prompt: str | tuple[Any, ...] | list[Any],
    ) -> list[Any]:
        # Anthropic-shape dicts to match ReActAgent's runtime
        # assumption (see anthropic.py module docstring).
        if isinstance(prompt, str):
            return [{"role": "user", "content": prompt}]
        if isinstance(prompt, (tuple, list)):
            return list(prompt)
        raise TypeError(
            f"prompt must be str, tuple, or list; got {type(prompt).__name__}"
        )

    # ── component access ──────────────────────────────────────

    @property
    def harness(self) -> LLMHarness:
        return self._harness

    @property
    def tool_harness(self) -> ToolHarness:
        return self._tool_harness

    @property
    def behaviour(self) -> ReActAgent:
        return self._behaviour
