"""
LIPAS · P3.2 — ReActAgent.

The classical Reason → Act → Observe loop, wired through the LIPAS
LLM harness AND tool harness.

P3.1 change: tool dispatch routes through ToolHarness (not the bare
runtime.arun_tools).  This wires every tool call into the same
preflight / fold / spend pipeline as LLM calls — Capability budget
gating, Guard policy, EffectRow lineage, and replay all "just work"
across both kinds.

What ReActAgent does NOT do
---------------------------
  - It does NOT reclassify harness errors (LLM or tool).  A budget
    rejection looks the same as an HTTP 503 to the loop: error
    reply → terminal (LLM); is_error tool_result → fed back to LLM
    (tools, ReAct's Observe step).
  - It does NOT do streaming.

Provider assumptions: see module docstring sections in the original
file.  Unchanged.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .adapter import Reply, Request
from .behaviour import (
    AgentBehaviour, AgentState, FinalResult, TerminationReason,
)
from .calculus import Claim
from .harness import LLMHarness
from .rows import RowSet
from .tool_harness import ToolHarness
from .tools import ToolRegistry


__all__ = ["ReActAgent"]

logger = logging.getLogger(__name__)


# =====================================================================
# ReActAgent
# =====================================================================

@dataclass
class ReActAgent:
    """ReAct loop over LIPAS LLMHarness + ToolHarness + tool registry.

    Construction
    ------------
    harness:
        Configured LLMHarness.  Drives the Reason step.
    tool_harness:
        Configured ToolHarness (P3.1).  Drives the Act step — each
        tool_use block from the LLM is dispatched through here, so
        tool calls share the same preflight / fold / spend machinery
        as LLM calls.  Almost always shares ``rowset`` with
        ``harness`` (so EffectRow sees both kinds in one lineage
        graph) but the harness layer does not require it.
    tools:
        ToolRegistry.  Used for Request.tools descriptor list (and
        passed-through to tool_harness which does its own lookup).
    rowset:
        The RowSet to fold history into.
    request_template:
        Static fields baked into every Request.
    max_iterations:
        Hard upper bound on R-A-O cycles.
    name:
        Behaviour identifier.
    """

    harness:          LLMHarness
    tools:            ToolRegistry
    tool_harness:     ToolHarness
    rowset:           RowSet
    request_template: Request
    max_iterations:   int = 10
    name:             str = "react"

    _tool_descriptors: tuple[Any, ...] | None = field(
        default=None, init=False, repr=False,
    )

    # ── public API ─────────────────────────────────────────────

    async def run(self, initial: AgentState) -> FinalResult:
        """Drive the ReAct loop to termination."""
        state = initial

        while True:
            if state.iteration >= self.max_iterations:
                logger.info(
                    "react: max_iterations=%d reached, terminating",
                    self.max_iterations,
                )
                return FinalResult(
                    text="",
                    state=state,
                    stop_reason=TerminationReason.MAX_ITERATIONS,
                    metadata={"iterations": state.iteration},
                )

            # 1. Reason
            request = self._build_request(state)
            reply   = await self.harness.call(request)

            # 2. Error path
            if reply.stop_reason == "error":
                logger.info(
                    "react: harness returned error reply, terminating: %r",
                    reply.error_detail,
                )
                return FinalResult(
                    text="",
                    state=state,
                    stop_reason=TerminationReason.ERROR,
                    error=reply.error_detail or {"type": "unknown"},
                    metadata={"iterations": state.iteration},
                )

            # 3. Extract tool calls
            tool_calls = self._extract_tool_calls(reply)

            if not tool_calls:
                final_text = self._extract_text(reply)
                next_state = state.with_messages(
                    self._message_from_reply(reply)
                ).next_iteration()
                self._fold_iteration(
                    state=next_state, reply=reply,
                    tool_calls=(), tool_results=(),
                )
                logger.info(
                    "react: natural stop at iteration %d",
                    next_state.iteration,
                )
                return FinalResult(
                    text=final_text,
                    state=next_state,
                    stop_reason=TerminationReason.NATURAL_STOP,
                    metadata={"iterations": next_state.iteration},
                )

            # 4. Act — dispatch via ToolHarness, one call at a time.
            #    Serial (not gather) for v0.1: rowset folds may not
            #    be safe under concurrent fold, and ordering of
            #    history claims is part of the audit trail.
            tool_results: list[Mapping[str, Any]] = []
            for tc in tool_calls:
                # tc shape: {"id": ..., "name": ..., "input": {...}, "type": "tool_use"}
                # The LLM-issued id IS our effect_id — preserves the
                # tool_use_id / tool_result.tool_use_id linkage AND
                # gives EffectRow a stable lineage key across replay.
                result_dict = await self.tool_harness.call(
                    tool_name=tc["name"],
                    arguments=dict(tc.get("input") or {}),
                    effect_id=tc["id"],
                )
                tool_results.append(result_dict)

            # 5. Observe — append to messages, advance, fold, loop.
            assistant_msg = self._message_from_reply(reply)
            results_msg   = self._message_from_tool_results(tool_results)
            state = state.with_messages(
                assistant_msg, results_msg,
            ).next_iteration()
            self._fold_iteration(
                state=state, reply=reply,
                tool_calls=tool_calls, tool_results=tool_results,
            )

    # ── request construction ──────────────────────────────────

    def _build_request(self, state: AgentState) -> Request:
        from dataclasses import replace as _replace
        return _replace(
            self.request_template,
            messages=state.messages,
            tools=self._get_tool_descriptors(),
        )

    def _get_tool_descriptors(self) -> tuple[Any, ...]:
        if self._tool_descriptors is None:
            descriptors = tuple(
                {
                    "name":         t.name,
                    "description":  t.description,
                    "input_schema": t.parameters_schema,
                }
                for t in self.tools
            )
            self._tool_descriptors = descriptors
        return self._tool_descriptors

    # ── reply parsing (override for non-Anthropic providers) ──

    def _extract_tool_calls(self, reply: Reply) -> tuple[Mapping[str, Any], ...]:
        out: list[Mapping[str, Any]] = []
        for block in getattr(reply, "content", ()) or ():
            if not isinstance(block, Mapping):
                continue
            if block.get("type") != "tool_use":
                continue
            if "id" not in block or "name" not in block:
                continue
            out.append(block)
        return tuple(out)

    def _extract_text(self, reply: Reply) -> str:
        parts: list[str] = []
        for block in getattr(reply, "content", ()) or ():
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)

    def _message_from_reply(self, reply: Reply) -> Mapping[str, Any]:
        return {
            "role":    "assistant",
            "content": list(getattr(reply, "content", ()) or ()),
        }

    def _message_from_tool_results(
        self, tool_results: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        return {
            "role":    "user",
            "content": list(tool_results),
        }

    # ── history fold ──────────────────────────────────────────

    def _fold_iteration(
        self,
        *,
        state: AgentState,
        reply: Reply,
        tool_calls:   Sequence[Mapping[str, Any]],
        tool_results: Sequence[Mapping[str, Any]],
    ) -> None:
        """Fold this iteration into HistoryRow.

        Unchanged from pre-P3.1 — the per-tool failure counters still
        bump on is_error tool_results, regardless of whether the
        is_error came from execution failure or from a ToolHarness
        rejection (schema/guard/budget).  That's the whole point of
        unifying both onto the tool_result wire shape.
        """
        triple = {
            "iteration":    state.iteration,
            "natural_stop": not tool_calls,
            "tool_calls": [
                {"id": tc.get("id"), "name": tc.get("name")}
                for tc in tool_calls
            ],
            "tool_errors": [
                tr.get("tool_use_id")
                for tr in tool_results
                if tr.get("is_error")
            ],
            "stop_reason": getattr(reply, "stop_reason", None),
        }

        self.rowset.fold(Claim(
            tag="observation",
            fields={"_history": [triple]},
            source=f"react.iteration[{state.iteration}]",
        ))

        for tc, tr in zip(tool_calls, tool_results):
            if tr.get("is_error"):
                tool_name = tc.get("name", "?")
                self.rowset.fold(Claim(
                    tag="outcome",
                    fields={"_fail_counts": {tool_name: 1}},
                    source=f"react.tool_error[{state.iteration}]",
                ))
