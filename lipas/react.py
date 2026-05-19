"""
LIPAS · P3.2 — ReActAgent.

The classical Reason → Act → Observe loop, wired through the LIPAS
LLM harness AND tool harness.

P3.1 change: tool dispatch routes through ToolHarness (not the bare
runtime.arun_tools).  This wires every tool call into the same
preflight / fold / spend pipeline as LLM calls — Capability budget
gating, Guard policy, EffectRow lineage, and replay all "just work"
across both kinds.

B3 — optional Supervisor.  When configured, ReActAgent calls
``supervisor.tick(view, ctx)`` after folding each iteration's
history. Returned ``supervisor_terminate`` / ``supervisor_escalate``
claims trigger early loop exit; ``supervisor_retry`` is informational
at this layer (ReAct already re-feeds is_error tool results to the
LLM in the next iteration). See docs/B3-NOTES.md.

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
from .rows.effect import EffectRow
from .supervisor import (
    F_SUP_PAYLOAD,
    F_SUP_REASON,
    Supervisor,
    TAG_SUPERVISOR_ESCALATE,
    TAG_SUPERVISOR_TERMINATE,
)
from .tool_harness import ToolHarness
from .tools import ToolRegistry


__all__ = ["ReActAgent"]

logger = logging.getLogger(__name__)


# Behaviour-specific stop_reason strings (the TerminationReason class
# documents stop_reason as an open string; standard reasons live as
# constants on it, behaviour-specific strings are used directly).
_STOP_SUPERVISOR_TERMINATE = "supervisor_terminate"
_STOP_SUPERVISOR_ESCALATE  = "supervisor_escalate"


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
        Configured ToolHarness (P3.1).  Drives the Act step.
    tools:
        ToolRegistry.  Used for Request.tools descriptor list.
    rowset:
        The RowSet to fold history into.  ``supervisor`` (if set)
        SHOULD share this rowset.
    request_template:
        Static fields baked into every Request.
    max_iterations:
        Hard upper bound on R-A-O cycles.
    supervisor (B3):
        Optional Supervisor.  Called once per iteration, after
        ``_fold_iteration`` and before the loop continues.  A
        ``supervisor_terminate`` or ``supervisor_escalate`` claim
        in the returned batch causes early loop exit with a
        FinalResult whose ``stop_reason`` is the supervisor reason.
    name:
        Behaviour identifier.
    """

    harness:          LLMHarness
    tools:            ToolRegistry
    tool_harness:     ToolHarness
    rowset:           RowSet
    request_template: Request
    max_iterations:   int = 10
    supervisor:       Supervisor | None = None
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
            tool_results: list[Mapping[str, Any]] = []
            for tc in tool_calls:
                result_dict = await self.tool_harness.call(
                    tool_name=tc["name"],
                    arguments=dict(tc.get("input") or {}),
                    effect_id=tc["id"],
                )
                tool_results.append(result_dict)

            # 5. Observe — append to messages, advance, fold.
            assistant_msg = self._message_from_reply(reply)
            results_msg   = self._message_from_tool_results(tool_results)
            state = state.with_messages(
                assistant_msg, results_msg,
            ).next_iteration()
            self._fold_iteration(
                state=state, reply=reply,
                tool_calls=tool_calls, tool_results=tool_results,
            )

            # 6. Supervisor tick (B3) — runs only on the looping path.
            #    Natural-stop / error / max-iterations paths do not tick;
            #    those terminations are already final.
            early = self._maybe_supervisor_tick(state)
            if early is not None:
                return early

    # ── supervisor (B3) ───────────────────────────────────────

    def _maybe_supervisor_tick(self, state: AgentState) -> FinalResult | None:
        if self.supervisor is None:
            return None
        eff_row = next(
            (r for r in self.rowset.rows if isinstance(r, EffectRow)),
            None,
        )
        if eff_row is None:
            # Supervisor needs an EffectView; without one, we cannot
            # build the snapshot.  Log once per absent-EffectRow run
            # would be tidier; for v0.1 a single warning per tick is
            # the simpler choice.
            logger.warning(
                "react: supervisor configured but rowset has no "
                "EffectRow; skipping supervisor tick at iteration %d",
                state.iteration,
            )
            return None
        view = eff_row.project(self.rowset.store)
        ctx  = self.rowset.store.ctx
        emitted = self.supervisor.tick(view, ctx)
        return self._handle_supervisor_outcome(emitted, state)

    @staticmethod
    def _handle_supervisor_outcome(
        emitted: list[Claim], state: AgentState,
    ) -> FinalResult | None:
        """First terminate/escalate wins. Retry claims are advisory at
        the ReAct level — they remain in the rowset for downstream
        tooling but do not alter loop control here."""
        for c in emitted:
            if c.tag == TAG_SUPERVISOR_TERMINATE:
                return FinalResult(
                    text="",
                    state=state,
                    stop_reason=_STOP_SUPERVISOR_TERMINATE,
                    metadata={
                        "iterations":        state.iteration,
                        "supervisor_reason": c.fields.get(F_SUP_REASON),
                    },
                )
            if c.tag == TAG_SUPERVISOR_ESCALATE:
                return FinalResult(
                    text="",
                    state=state,
                    stop_reason=_STOP_SUPERVISOR_ESCALATE,
                    metadata={
                        "iterations":         state.iteration,
                        "supervisor_reason":  c.fields.get(F_SUP_REASON),
                        "supervisor_payload": c.fields.get(F_SUP_PAYLOAD),
                    },
                )
        return None

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
