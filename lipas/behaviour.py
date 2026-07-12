"""
LIPAS · P3.1 — Agent behaviour protocol.

This module defines the *shape* of an agent run.  No I/O, no logic —
just the dataclasses that flow between the runner-the-caller and the
behaviour, and the Protocol the behaviour implements.

Why a Protocol at all?
----------------------
ReAct (P3.2), Plan-and-Execute (P3.3+), Critique-and-Revise, and
multi-agent orchestrators all answer the same question — "given this
initial state, run the agent and tell me how it ended" — but the loop
SHAPES are genuinely different:

  - ReAct: one LLM call, then maybe N tool calls, repeat.
  - Plan-and-Execute: one planning LLM call up front, then a sequence
    of typed sub-steps each of which may itself loop.
  - Critique-and-Revise: attempt → critique LLM call → revised
    attempt; the loop terminates on critique-says-good or budget.

Trying to factor a "step()" abstraction across these gives you a
``next_action()`` that has to grow union variants for each shape and
breaks the invariants of every shape it doesn't natively support.
The cleaner abstraction is: behaviour OWNS the loop, runner provides
the I/O surface (harness, tools, rowset) at construction time.

What's NOT in the protocol
--------------------------
  - ``harness`` / ``tools`` / ``rowset``: passed via concrete
    ``__init__``.  The Protocol can't fix their types — different
    behaviours may need different surfaces (a single-call behaviour
    needs only a harness; a tool-using behaviour needs a registry; a
    self-RAG behaviour might want a retriever).
  - History folding: behaviour-specific (one row per iteration vs.
    one row per plan-step vs. one row per attempt).  Each behaviour
    decides for itself; the projection into HistoryRow is a private
    concern.
  - Streaming: v1 returns a final ``FinalResult`` only.  Streaming
    intermediate events to the caller is a P3.4+ extension that
    grows ``run`` into ``stream`` returning ``AsyncIterator[Event]``.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable


__all__ = [
    "AgentState",
    "FinalResult",
    "AgentBehaviour",
    "TerminationReason",
]


# =====================================================================
# Termination reasons (string enum-style constants)
# =====================================================================
#
# Strings, not Enum, for the same reason EffectKind is string-valued:
# they round-trip through JSON / pickle without needing a dedicated
# decoder, and ``stop_reason == "natural_stop"`` reads obviously.
#
# Behaviours are NOT restricted to these values — a Critique-and-
# Revise behaviour might use ``"critique_satisfied"``.  These are the
# common ones, exposed as constants so ReActAgent and similar can use
# them by name and downstream consumers can grep on a fixed set.

class TerminationReason:
    NATURAL_STOP    = "natural_stop"     # agent's own decision (no tool calls)
    MAX_ITERATIONS  = "max_iterations"   # ran out of budget for steps
    ERROR           = "error"            # adapter / preflight terminal error
    TOOL_FAILURE    = "tool_failure"     # tool failure that the behaviour
                                         #   decided not to feed back to LLM
                                         #   (ReAct does NOT use this — it
                                         #   feeds tool errors back; other
                                         #   behaviours may.)


# =====================================================================
# AgentState
# =====================================================================

@dataclass(frozen=True)
class AgentState:
    """Conversation context as it flows through an agent run.

    Frozen.  Behaviours evolve state via the ``with_*`` helpers,
    producing new immutable instances.  Rationale: the same state may
    be referenced multiple times within one iteration (build next
    request → fold history → judge termination), and silent mutation
    between those reads is exactly the kind of concurrency bug that's
    expensive to debug in agent loops.

    Fields
    ------
    messages:
        The full conversation, system message included if applicable.
        Stored as ``tuple`` (not list) to enforce structural sharing
        on ``with_messages``.  The element type is intentionally
        ``Any`` at this layer — the agent runtime / behaviour fixes
        the schema (Anthropic-style dicts, OpenAI-style dicts, or a
        typed Message dataclass).  Cross-provider behaviours should
        not be reading individual block contents at this level.

    iteration:
        Zero-based count of completed reason-act-observe cycles.
        ReActAgent increments AFTER folding the iteration's history;
        a state with iteration=0 is "we have not yet completed
        iteration 0" (i.e. pre-first-LLM-call).

    metadata:
        Free-form extension slot for behaviour-private state.  PaE
        might stash the plan here; Critique-and-Revise might stash
        the most recent critique.  The runner reads nothing from
        metadata; behaviours that care about persistence across
        sub-runs use it as an opaque bag.

        ``Mapping[str, Any]``, not ``dict``: signals "do not mutate
        in place".  Use ``with_metadata({**state.metadata, ...})``.
    """
    messages:  tuple[Any, ...]            = ()
    iteration: int                         = 0
    metadata:  Mapping[str, Any]           = field(default_factory=dict)

    # ── evolution ─────────────────────────────────────────────

    def with_messages(self, *new: Any) -> "AgentState":
        """Append messages, returning a new state.

        No-op when ``new`` is empty (returns same instance).  Most
        behaviours append exactly two messages per iteration
        (assistant reply + tool results); calling once with two args
        is cheaper than calling twice with one each.
        """
        if not new:
            return self
        return replace(self, messages=self.messages + tuple(new))

    def next_iteration(self) -> "AgentState":
        """Advance the iteration counter by one."""
        return replace(self, iteration=self.iteration + 1)

    def with_metadata(self, metadata: Mapping[str, Any]) -> "AgentState":
        """Replace metadata wholesale.

        Caller is responsible for merging if they want diff semantics:
        ``state.with_metadata({**state.metadata, "plan": p})``.  The
        explicit-merge convention avoids "did this overwrite or merge?"
        ambiguity on read.
        """
        return replace(self, metadata=metadata)


# =====================================================================
# FinalResult
# =====================================================================

@dataclass(frozen=True)
class FinalResult:
    """Terminal payload of an agent run.

    Behaviours always return a FinalResult, never raise on logical
    termination.  Genuine bugs (invariant violations, protocol misuse)
    propagate as exceptions; everything else is a FinalResult with an
    appropriate ``stop_reason``.

    Fields
    ------
    text:
        The agent's final output, if any.  Empty string for error /
        max-iterations / tool-failure terminations — there's no
        meaningful "answer" to surface in those cases.  Callers
        deciding whether to show ``text`` to a user should branch on
        ``stop_reason``, not on ``text == ""``.

    state:
        The final ``AgentState`` at termination.  Holds the full
        conversation, including the last LLM reply (if any) and any
        tool results that were observed before terminating.  Useful
        for resuming an agent ("here's where I left off, continue")
        and for debugging.

    stop_reason:
        See ``TerminationReason``.  Open string for behaviour-specific
        reasons; standard reasons live as constants on that class.

    error:
        Populated iff ``stop_reason == "error"``.  Mirrors the
        ``Reply.error_detail`` shape from the harness:
            {"type": "preflight_rejection" | "http_error" | ...,
             "reason": "...",
             ...kind-specific fields...}
        Behaviours layered above the harness do not synthesize new
        error shapes — they pass the harness's error_detail through.

    metadata:
        Free-form result metadata.  Distinct from ``state.metadata``:
        this is "things the behaviour wants to surface to its
        caller" (token counts summary, plan executed, critiques
        applied), whereas ``state.metadata`` is private behaviour
        state during the run.
    """
    text:        str                  = ""
    state:       AgentState           = field(default_factory=AgentState)
    stop_reason: str                  = TerminationReason.NATURAL_STOP
    error:       Mapping[str, Any] | None = None
    metadata:    Mapping[str, Any]    = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.stop_reason == TerminationReason.ERROR

    @property
    def is_natural(self) -> bool:
        return self.stop_reason == TerminationReason.NATURAL_STOP


# =====================================================================
# Protocol
# =====================================================================

@runtime_checkable
class AgentBehaviour(Protocol):
    """Strategy for one agent run.

    Implementations OWN the loop: they call into the harness, dispatch
    tools, fold history claims, and decide termination.  The runner-
    the-caller treats them as opaque ``run(initial) -> FinalResult``
    boxes.

    Implementations MUST:
      - return a ``FinalResult`` for every terminating run;
      - tolerate ``initial.iteration > 0`` (resume semantics);
      - be safe to construct once and call ``run`` multiple times
        concurrently with DIFFERENT ``initial`` states (the rowset /
        harness they wrap may not be — that's the deployment's
        responsibility, not the behaviour's contract).

    Implementations MAY:
      - assume their wrapped harness folds claims into a non-shared
        rowset (sharing a rowset across concurrent runs is allowed
        but a deployment concern);
      - mutate ``initial.metadata`` semantics for their own use (it's
        a free-form bag);
      - expose configuration via ``__init__`` (max_iterations, etc.)
        without touching the Protocol.

    The Protocol intentionally does NOT prescribe:
      - whether ``run`` is async (it is, in v1, because every concrete
        behaviour drives an async harness — making ``run`` sync would
        force an event-loop ceremony at the call site);
      - how ``initial`` is constructed (the runner builds it from a
        prompt / system message / tools list as appropriate);
      - what kind of cancellation semantics ``run`` honors (native
        ``asyncio.CancelledError`` should propagate cleanly, but
        partial-fold cleanup is behaviour-specific).
    """

    name: str

    async def run(self, initial: AgentState) -> FinalResult: ...
