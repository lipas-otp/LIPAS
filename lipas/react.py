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
LLM in the next iteration).

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

import asyncio
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .adapter import Reply, Request
from .behaviour import (
    AgentState, FinalResult, TerminationReason,
)
from .calculus import Claim
from .harness import LLMHarness
from .rows import RowSet
from .rows.capability import CapabilityRow
from .rows.effect import EffectRow
from .supervisor import (
    F_SUP_PAYLOAD,
    F_SUP_REASON,
    Supervisor,
    TAG_SUPERVISOR_ESCALATE,
    TAG_SUPERVISOR_TERMINATE,
)
from .tool_harness import ToolHarness
from .tools import SideEffectClass, ToolNotFoundError, ToolRegistry


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
    max_parallel_tools: int = 4
    supervisor:       Supervisor | None = None
    name:             str = "react"

    _tool_descriptors: tuple[Any, ...] | None = field(
        default=None, init=False, repr=False,
    )

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_parallel_tools, bool)
            or not isinstance(self.max_parallel_tools, int)
            or self.max_parallel_tools < 1
        ):
            raise ValueError("max_parallel_tools must be a positive integer")

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
            caused_by = state.metadata.get("caused_by")
            reply   = await self.harness.call(request, caused_by=caused_by)

            # 2. Error path
            if reply.stop_reason == "error":
                logger.info(
                    "react: harness returned error reply, terminating: %r",
                    reply.error_detail,
                )
                result = FinalResult(
                    text="",
                    state=state,
                    stop_reason=TerminationReason.ERROR,
                    error=reply.error_detail or {"type": "unknown"},
                    metadata={"iterations": state.iteration},
                )
                return self._maybe_supervisor_tick(state) or result

            # 3. Extract tool calls
            tool_calls = self._extract_tool_calls(reply)

            if not tool_calls:
                if reply.stop_reason == "tool_use":
                    return FinalResult(
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
                final_text = self._extract_text(reply)
                next_state = state.with_messages(
                    self._message_from_reply(reply)
                ).next_iteration()
                self._fold_iteration(
                    state=next_state, reply=reply,
                    tool_calls=(), tool_results=(),
                )
                logger.info(
                    "react: terminal model stop=%s at iteration %d",
                    reply.stop_reason, next_state.iteration,
                )
                result = FinalResult(
                    text=final_text,
                    state=next_state,
                    stop_reason=(
                        TerminationReason.MAX_TOKENS
                        if reply.stop_reason == "max_tokens"
                        else TerminationReason.NATURAL_STOP
                    ),
                    metadata={"iterations": next_state.iteration},
                )
                return self._maybe_supervisor_tick(next_state) or result

            # 4. Act — independent safe reads may run concurrently. Writes,
            # guarded/replayed calls, and budgeted calls remain serial.
            tool_results: list[Mapping[str, Any]] = []
            tool_index = 0
            while tool_index < len(tool_calls):
                batch_size = self._parallel_tool_count(tool_calls, tool_index)
                batch = tool_calls[tool_index:tool_index + batch_size]
                calls = [
                    self.tool_harness.call(
                        tool_name=str(tc["name"]),
                        arguments=dict(tc.get("input") or {}),
                        effect_id=f"tool_{uuid.uuid4().hex[:12]}",
                        tool_use_id=str(tc["id"]),
                        caused_by=caused_by,
                    )
                    for tc in batch
                ]
                batch_results = (
                    await asyncio.gather(*calls)
                    if len(calls) > 1 else [await calls[0]]
                )
                tool_results.extend(batch_results)
                tool_index += batch_size

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

            # 6. Supervisor tick (B3). Terminal reply paths above also tick
            #    where an EffectView exists, allowing policy to replace the
            #    behaviour's terminal result with an explicit recommendation.
            early = self._maybe_supervisor_tick(state)
            if early is not None:
                return early

    def _parallel_tool_count(
        self,
        tool_calls: Sequence[Mapping[str, Any]],
        start: int,
    ) -> int:
        """Return a safe contiguous tool batch size, always at least one.

        Concurrent preflight against hard budgets, stateful guards, or replay
        cursors could admit work from a stale projection. Those configurations
        therefore remain serial. Only PURE/READ_ONLY calls are eligible.
        """
        if self.max_parallel_tools == 1:
            return 1
        if (
            self.tool_harness.guards
            or self.tool_harness.tool_replayer is not None
            or self.tool_harness.argument_resolver is not None
            or self.tool_harness.result_sanitizer is not None
        ):
            return 1
        capability = next(
            (
                row for row in self.rowset.rows
                if isinstance(row, CapabilityRow)
            ),
            None,
        )
        if capability is not None and capability.budgets:
            return 1

        count = 0
        stop = min(len(tool_calls), start + self.max_parallel_tools)
        for index in range(start, stop):
            call = tool_calls[index]
            try:
                tool = self.tools.get(str(call["name"]))
            except ToolNotFoundError:
                break
            if tool.side_effect not in {
                SideEffectClass.PURE,
                SideEffectClass.READ_ONLY,
            }:
                break
            count += 1
        return max(1, count)

    # ── supervisor (B3) ───────────────────────────────────────

    def _maybe_supervisor_tick(
        self,
        state: AgentState,
        *,
        claim_id_prefix: str | None = None,
    ) -> FinalResult | None:
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
        emitted = self.supervisor.tick(
            view, ctx, claim_id_prefix=claim_id_prefix,
        )
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
            tool_id = block.get("id")
            tool_name = block.get("name")
            tool_input = block.get("input")
            if (
                not isinstance(tool_id, str)
                or not tool_id
                or not isinstance(tool_name, str)
                or not tool_name
                or (tool_input is not None and not isinstance(tool_input, Mapping))
            ):
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
        claim_id_prefix: str | None = None,
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

        observation_claim_id = (
            f"{claim_id_prefix}:observation"
            if claim_id_prefix is not None else None
        )
        self.rowset.fold(Claim(
            tag="observation",
            fields={"_history": [triple]},
            source=f"react.iteration[{state.iteration}]",
            claim_id=observation_claim_id or uuid.uuid4().hex[:8],
        ))

        for index, (tc, tr) in enumerate(zip(tool_calls, tool_results)):
            if tr.get("is_error"):
                tool_name = tc.get("name", "?")
                outcome_claim_id = (
                    f"{claim_id_prefix}:tool-error:{index}"
                    if claim_id_prefix is not None else None
                )
                self.rowset.fold(Claim(
                    tag="outcome",
                    fields={"_fail_counts": {tool_name: 1}},
                    source=f"react.tool_error[{state.iteration}]",
                    claim_id=outcome_claim_id or uuid.uuid4().hex[:8],
                ))
