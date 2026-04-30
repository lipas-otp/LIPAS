# lipas/runtime.py
"""Runtime execution: run_tools / arun_tools.

Definition-time concerns live in lipas.tools (@tool, Tool, ToolRegistry,
InvalidArgumentsError). This module covers what happens when an LLM
emits tool calls:

    provider wire format
      -> adapter parses -> list[ToolCall]
      -> run_tools(registry, calls) -> list[ToolResult]
      -> adapter serializes -> provider wire format

run_tools reads only (id, name, arguments) from ToolCall; it does NOT
invoke the ToolCall's own `invoke`/`ainvoke` lifecycle (that path is for
user code that wants per-call lifecycle guarantees). Registry-based
dispatch and ToolCall.invoke() are two independent execution paths; the
runtime never engages INV-TOOLCALL-ONCE.

run_tools never raises on tool failure; failures surface as ToolResult
with error_kind set (and is_error==True as a derived property). The
only exceptions that propagate are BaseException subclasses
(KeyboardInterrupt, SystemExit) and programming errors in run_tools
itself.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from .tools import (
    InvalidArgumentsError,
    ToolNotFoundError,
    ToolRegistry,
)
from .types import ToolCall

logger = logging.getLogger(__name__)

ErrorKind = Literal["unknown_tool", "invalid_arguments", "execution_error"]


@dataclass(frozen=True)
class ToolResult:
    """Outcome of one ToolCall.

    Fields:
        call_id: matches the originating ToolCall.id.
        output: on success, whatever the tool returned (Any). On failure,
            a short human-readable error message string intended for the
            LLM. Adapters stringify / re-shape for the wire as needed;
            run_tools does not stringify because the target format is
            provider-specific (e.g. Anthropic accepts content blocks,
            OpenAI expects a string).
        error_kind: structured classification.
            None                 -> success
            "unknown_tool"       -> tool name not in registry
            "invalid_arguments"  -> arguments did not bind to signature
            "execution_error"    -> tool body raised an exception

        is_error (property): True iff error_kind is not None. Derived
            rather than stored to keep the two fields from drifting.
    """
    call_id: str
    output: Any
    error_kind: ErrorKind | None = None

    @property
    def is_error(self) -> bool:
        return self.error_kind is not None


# ---------------------------------------------------------------------------
# Synchronous path
# ---------------------------------------------------------------------------

def run_tools(
    registry: ToolRegistry,
    calls: Iterable[ToolCall],
) -> list[ToolResult]:
    """Execute a batch of tool calls against a registry, sequentially.

    Error handling contract:
        - Unknown tool name     -> ToolResult(error_kind="unknown_tool")
        - Argument bind failure -> ToolResult(error_kind="invalid_arguments")
        - Tool body raises      -> ToolResult(error_kind="execution_error")
        - BaseException (KeyboardInterrupt, SystemExit) propagates.
        - Programming errors inside run_tools propagate.

    All calls are attempted; one failure does not abort the batch.
    Full tracebacks go to logger only for execution_error (genuine
    system-side failure). unknown_tool and invalid_arguments are model
    errors and log at INFO without exc_info to avoid polluting
    production logs.

    Returns one ToolResult per input ToolCall, in input order.
    """
    return [_run_one(registry, call) for call in calls]


def _run_one(registry: ToolRegistry, call: ToolCall) -> ToolResult:
    # 1. Lookup
    try:
        tool = registry.get(call.name)
    except ToolNotFoundError as e:
        logger.info(
            "UnknownTool %r (call_id=%s)", call.name, call.id,
        )
        return ToolResult(
            call_id=call.id,
            output=f"UnknownTool: {e}",
            error_kind="unknown_tool",
        )

    # 2. Bind + invoke. InvalidArgumentsError is raised by Tool.call
    #    *before* the handler runs; any other Exception comes from
    #    the handler body.
    try:
        output = tool.call(call.arguments)
    except InvalidArgumentsError as e:
        logger.info(
            "InvalidArguments for %r (call_id=%s): %s",
            call.name, call.id, e,
        )
        return ToolResult(
            call_id=call.id,
            output=f"InvalidArguments: tool {call.name!r}: {e}",
            error_kind="invalid_arguments",
        )
    except Exception as e:
        logger.warning(
            "ToolExecutionError in %r (call_id=%s)",
            call.name, call.id, exc_info=True,
        )
        return ToolResult(
            call_id=call.id,
            output=(
                f"ToolExecutionError: tool {call.name!r} raised "
                f"{type(e).__name__}: {e}"
            ),
            error_kind="execution_error",
        )

    return ToolResult(call_id=call.id, output=output)


# ---------------------------------------------------------------------------
# Asynchronous path (P1.1d)
# ---------------------------------------------------------------------------

async def _arun_one(registry: ToolRegistry, call: ToolCall) -> ToolResult:
    """Execute one ToolCall asynchronously, converting all failure modes
    into a ToolResult.

    Like _run_one, this function must not raise for any tool-level or
    argument-level error; only framework bugs (which should propagate)
    and BaseException escape.
    """
    # 1. Lookup
    try:
        tool = registry.get(call.name)
    except ToolNotFoundError as e:
        logger.info(
            "UnknownTool %r (call_id=%s)", call.name, call.id,
        )
        return ToolResult(
            call_id=call.id,
            output=f"UnknownTool: {e}",
            error_kind="unknown_tool",
        )

    # 2. Bind + invoke via tool.acall.
    try:
        output = await tool.acall(call.arguments)
    except InvalidArgumentsError as e:
        logger.info(
            "InvalidArguments for %r (call_id=%s): %s",
            call.name, call.id, e,
        )
        return ToolResult(
            call_id=call.id,
            output=f"InvalidArguments: tool {call.name!r}: {e}",
            error_kind="invalid_arguments",
        )
    except Exception as e:
        logger.warning(
            "ToolExecutionError in %r (call_id=%s)",
            call.name, call.id, exc_info=True,
        )
        return ToolResult(
            call_id=call.id,
            output=(
                f"ToolExecutionError: tool {call.name!r} raised "
                f"{type(e).__name__}: {e}"
            ),
            error_kind="execution_error",
        )

    return ToolResult(call_id=call.id, output=output)


async def arun_tools(
    registry: ToolRegistry,
    calls: Iterable[ToolCall],
) -> list[ToolResult]:
    """Execute tool calls concurrently. Results are returned in input
    order, independent of completion order — preserving the positional
    correspondence between ToolCall and ToolResult that downstream
    message formatters rely on.

    Parallel tool use is a first-class provider semantic (OpenAI /
    Anthropic); concurrent execution is the default and not tunable
    here. If a specific tool cannot run concurrently with others, that
    constraint belongs inside the tool (lock, queue, semaphore) — not
    in the runtime.
    """
    call_list = list(calls)
    return await asyncio.gather(
        *(_arun_one(registry, c) for c in call_list)
    )
