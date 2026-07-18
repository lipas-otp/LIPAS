"""Tool decorator, schema extraction, and the Tool dataclass.

Public surface:
    - SideEffectClass   : the four-tier side-effect taxonomy (P3.0)
    - Tool              : the frozen dataclass LLM adapters consume
    - tool              : decorator turning a function into a Tool
    - ValidationError   : raised at decoration time on schema problems

P3.1 additions:
    - Tool.declared_buckets : frozenset[str] — buckets the tool may
                              touch. Used by ToolHarness to reject
                              undeclared spend and by deployment-side
                              startup budget checks.
    - Tool.estimate_fn      : Callable[[args_dict], {bucket: float}]
                              | None — upper-bound estimate per call.
                              Honored by ToolHarness pre-flight. Custom
                              buckets are conservatively charged at the
                              admitted estimate; system wall time is
                              replaced by the measured duration.
    - @tool(declared_buckets=..., estimate=...) wires both at the
      decorator level.

P3.2 additions (RFC-001 §3.4):
    - Tool.observability_only : bool — marks tools whose side effects
                                are infrastructure (logging sinks,
                                metric emitters, trace exports) rather
                                than application-semantic. During
                                replay the class is logically
                                downgraded to READ_ONLY when selecting
                                a replay operation; the actual
                                SideEffectClass is preserved on the
                                audit trail. Tools that lie about this
                                break replay correctness — same threat
                                model as a tool lying about its
                                SideEffectClass.

Naming convention (D1):
    - System-owned buckets (no prefix): tool_calls, wall_seconds,
      tokens_in, tokens_out, cost_usd.
    - Tool-private buckets MUST be prefixed: <tool_name>.<bucket>
      (e.g. http_get.requests, python_eval.bytes_out).
    - v0.1 emits a warning on violation; v0.2 may upgrade to
      ValidationError.

All schema extraction happens at decoration time. Any problem surfaces at
import, not at first invoke.
"""
from __future__ import annotations
from collections.abc import Iterable, Iterator, Mapping

import asyncio
import inspect
import logging
import types
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any, Callable, Literal, Union,
    get_args, get_origin, get_type_hints,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    """Raised when a tool's schema cannot be built."""


class ToolNotFoundError(KeyError):
    """Raised when ToolRegistry.get() misses."""


class DuplicateToolError(ValueError):
    """Raised when ToolRegistry receives two Tools with the same name."""


class InvalidArgumentsError(TypeError):
    """Raised by Tool.call when arguments fail signature binding."""


# ---------------------------------------------------------------------------
# SideEffectClass
# ---------------------------------------------------------------------------

class SideEffectClass(str, Enum):
    """The side-effect profile of a tool.  See module docstring P3.0."""
    PURE             = "pure"
    READ_ONLY        = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    EXTERNAL_WRITE   = "external_write"


# ---------------------------------------------------------------------------
# Bucket naming
# ---------------------------------------------------------------------------

#: Buckets owned by the system layer (LLMHarness / ToolHarness).
#: Tools may declare these — meaning is uniform across all tools
#: ("wall_seconds" always means wall-clock seconds, regardless of who
#: is reporting it).
SYSTEM_BUCKETS: frozenset[str] = frozenset({
    "tool_calls",
    "wall_seconds",
    "tokens_in",
    "tokens_out",
    "cost_usd",
})


def _validate_buckets(tool_name: str, buckets: frozenset[str]) -> None:
    """Lenient naming-convention check.

    System buckets pass.  Tool-private buckets must be prefixed with
    ``<tool_name>.``.  Anything else gets a warning — the harness
    will still record claims for the bucket, but two tools picking
    different semantics for the same unprefixed name will silently
    co-mingle their accounting.
    """
    for b in buckets:
        if b in SYSTEM_BUCKETS:
            continue
        if b.startswith(f"{tool_name}."):
            continue
        logger.warning(
            "tool %r declares bucket %r which is neither a system bucket "
            "(%s) nor namespaced as %r.<...>; cross-tool collisions are "
            "possible.  Prefer renaming to %r.",
            tool_name, b, ", ".join(sorted(SYSTEM_BUCKETS)),
            tool_name, f"{tool_name}.{b}",
        )


# ---------------------------------------------------------------------------
# Tool dataclass
# ---------------------------------------------------------------------------

EstimateFn = Callable[[Mapping[str, Any]], Mapping[str, float]]


@dataclass(frozen=True)
class Tool:
    """A callable tool exposed to LLM providers.

    Tool objects are produced by @tool. They are NOT directly callable —
    use `.invoke(**kwargs)` to execute, or pass to an Agent.

    Notes on `_handler` field options:
        - repr=False: keeps repr(tool) readable.
        - compare=False: equality is over semantic fields only.

    Adapters MUST treat parameters_schema as read-only; deep-copy if augmenting.

    P3.0 — ``side_effect`` field is REQUIRED (no default), and IS part
    of equality.

    P3.1 — ``declared_buckets`` and ``estimate_fn`` describe resource
    consumption.  ``declared_buckets`` is part of equality (it's
    behavioral identity); ``estimate_fn`` is not (callable identity
    is unstable across module reloads).

    P3.2 — ``observability_only`` (RFC-001 §3.4) is part of equality.
    It is a behavioural identity declaration: a tool with
    ``observability_only=True`` promises that its side effect, while
    nominally classified as IDEMPOTENT_WRITE / EXTERNAL_WRITE, has
    no application-semantic consequence and may be safely re-executed
    during replay (treated as READ_ONLY for replay decisions).
    """
    name: str
    description: str
    parameters_schema: dict
    side_effect: SideEffectClass
    _handler: Callable[..., Any] = field(repr=False, compare=False)

    # ── P3.1 resource declarations ─────────────────────────────────
    # Defaults: every tool counts as one tool_call and consumes wall
    # time.  Tools that need budget gating on private buckets must
    # override.
    declared_buckets: frozenset[str] = field(
        default=frozenset({"tool_calls", "wall_seconds"}),
    )
    estimate_fn: EstimateFn | None = field(
        default=None, repr=False, compare=False,
    )

    # ── P3.2 replay declaration (RFC-001 §3.4) ─────────────────────
    # Part of equality: behavioural identity, not callable identity.
    observability_only: bool = False

    # Cached signature.
    _signature: inspect.Signature = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Tool.name must be a non-empty string")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("Tool.description must be a non-empty string")
        if not isinstance(self.parameters_schema, dict):
            raise TypeError("Tool.parameters_schema must be a dict")
        if not isinstance(self.side_effect, SideEffectClass):
            raise TypeError("Tool.side_effect must be SideEffectClass")
        if not callable(self._handler):
            raise TypeError("Tool._handler must be callable")
        if not isinstance(self.declared_buckets, frozenset) or not self.declared_buckets:
            raise ValueError("Tool.declared_buckets must be a non-empty frozenset")
        if any(not isinstance(bucket, str) or not bucket for bucket in self.declared_buckets):
            raise ValueError("Tool.declared_buckets must contain non-empty strings")
        if self.estimate_fn is not None and not callable(self.estimate_fn):
            raise TypeError("Tool.estimate_fn must be callable or None")
        if not isinstance(self.observability_only, bool):
            raise TypeError("Tool.observability_only must be bool")
        object.__setattr__(self, "_signature", inspect.signature(self._handler))
        _validate_buckets(self.name, self.declared_buckets)

    def invoke(self, **kwargs: Any) -> Any:
        """Execute the underlying function with keyword arguments."""
        return self._handler(**kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError(
            f"Tool {self.name!r} is not directly callable. "
            f"Use `{self.name}.invoke(...)` to execute, "
            f"or pass it to an Agent via `Agent(tools=[{self.name}])`."
        )

    def call(self, arguments: dict[str, Any]) -> Any:
        """Bind arguments against the handler signature and invoke (sync)."""
        if asyncio.iscoroutinefunction(self._handler):
            raise TypeError(
                f"Tool {self.name!r} has an async handler; use Tool.acall "
                f"or arun_tools instead of Tool.call / run_tools."
            )
        try:
            bound = self._signature.bind(**arguments)
        except TypeError as e:
            raise InvalidArgumentsError(str(e)) from e
        bound.apply_defaults()
        return self._handler(*bound.args, **bound.kwargs)

    async def acall(self, arguments: dict) -> Any:
        """Async invocation."""
        try:
            bound = self._signature.bind(**arguments)
        except TypeError as e:
            raise InvalidArgumentsError(str(e)) from e
        bound.apply_defaults()
        result = self._handler(*bound.args, **bound.kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        return result


# ---------------------------------------------------------------------------
# Type hint  ->  JSON Schema
# ---------------------------------------------------------------------------

def _is_union(origin: Any) -> bool:
    if origin is Union:
        return True
    if hasattr(types, "UnionType") and origin is types.UnionType:
        return True
    return False


def type_to_json_schema(hint: Any) -> tuple[dict, str | None]:
    """Convert a Python type hint to (JSON Schema, description)."""
    description: str | None = None

    if hasattr(hint, "__metadata__"):
        for meta in hint.__metadata__:
            if isinstance(meta, str):
                description = meta
                break
        hint = hint.__origin__

    origin = get_origin(hint)

    if hint is str:
        return {"type": "string"}, description
    if hint is bool:
        return {"type": "boolean"}, description
    if hint is int:
        return {"type": "integer"}, description
    if hint is float:
        return {"type": "number"}, description

    if origin is Literal:
        return {"enum": list(get_args(hint))}, description

    if _is_union(origin):
        args = get_args(hint)
        if type(None) not in args:
            raise ValidationError(
                f"Union types without None (got {hint!r}) are not supported in v1."
            )
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) > 1:
            raise ValidationError(
                f"Union with multiple non-None types (got {hint!r}) is not "
                f"supported in v1."
            )
        sub_schemas = []
        for arg in args:
            if arg is type(None):
                sub_schemas.append({"type": "null"})
            else:
                sub, _ = type_to_json_schema(arg)
                sub_schemas.append(sub)
        return {"anyOf": sub_schemas}, description

    if origin is list:
        args = get_args(hint)
        if not args:
            raise ValidationError(
                "Bare `list` is not supported; use list[T]."
            )
        item_schema, _ = type_to_json_schema(args[0])
        return {"type": "array", "items": item_schema}, description

    if origin is dict:
        args = get_args(hint)
        if not args:
            raise ValidationError(
                "Bare `dict` is not supported; use dict[str, T]."
            )
        key_type, value_type = args
        if key_type is not str:
            raise ValidationError(
                f"dict keys must be `str` (got {key_type!r})."
            )
        value_schema, _ = type_to_json_schema(value_type)
        return {"type": "object", "additionalProperties": value_schema}, description

    raise ValidationError(
        f"Unsupported type hint: {hint!r}. "
        f"For complex types, pass `schema=` to @tool."
    )


# ---------------------------------------------------------------------------
# Schema + description assembly
# ---------------------------------------------------------------------------

def build_parameters_schema(fn: Callable[..., Any]) -> dict:
    hints = get_type_hints(fn, include_extras=True)
    sig = inspect.signature(fn)
    properties: dict[str, dict] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            raise ValidationError(
                f"Tool {fn.__name__!r} uses *args/**kwargs (parameter "
                f"{param_name!r}), which cannot be mapped to a JSON Schema."
            )
        if param_name not in hints:
            raise ValidationError(
                f"Tool parameter {param_name!r} of {fn.__name__!r} has no "
                f"type annotation."
            )

        prop_schema, desc = type_to_json_schema(hints[param_name])
        if desc is not None:
            prop_schema["description"] = desc
        properties[param_name] = prop_schema

        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    return {"type": "object", "properties": properties, "required": required}


def build_description(fn: Callable[..., Any], override: str | None) -> str:
    if override is not None:
        if not override.strip():
            raise ValidationError(
                f"Tool {fn.__name__!r} has empty description override."
            )
        return override
    doc = inspect.getdoc(fn)
    if not doc or not doc.strip():
        raise ValidationError(
            f"Tool {fn.__name__!r} has no description. "
            f"Add a docstring, or pass `description=...` to @tool."
        )
    return doc.split("\n\n", 1)[0].strip()


# ---------------------------------------------------------------------------
# @tool decorator
# ---------------------------------------------------------------------------

def tool(
    fn: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    schema: dict | None = None,
    side_effect: SideEffectClass | str | None = None,
    declared_buckets: Iterable[str] | None = None,
    estimate: EstimateFn | None = None,
    observability_only: bool = False,
) -> Any:
    """Convert a function into a Tool object.

        @tool(side_effect=SideEffectClass.PURE)
        def f(x: int) -> int:
            '''Describe.'''
            ...

        @tool(
            side_effect=SideEffectClass.READ_ONLY,
            declared_buckets={"tool_calls", "wall_seconds", "http_get.requests"},
            estimate=lambda args: {"wall_seconds": float(args.get("timeout", 5.0))},
        )
        def http_get(url: str, timeout: float = 5.0) -> str:
            '''Fetch a URL via HTTP GET.'''
            ...

        @tool(
            side_effect=SideEffectClass.EXTERNAL_WRITE,
            observability_only=True,
        )
        def emit_metric(name: str, value: float) -> None:
            '''Emit a metric to the observability backend.'''
            ...

    `side_effect=` is REQUIRED (P3.0, D4). Use either a
    ``SideEffectClass`` member or its explicit lower-case value such as
    ``"read_only"``; strings are normalized and validated at decoration time.

    `declared_buckets=` (P3.1) lists every bucket this tool MAY touch.
    Defaults to {"tool_calls", "wall_seconds"} — the two system-managed
    buckets ToolHarness records on every call.  Tool-private buckets
    must be prefixed: ``<tool_name>.<bucket>`` (lenient warning on
    violation, see SYSTEM_BUCKETS).

    `estimate=` (P3.1) is a callable taking the bound arguments dict
    and returning a {bucket: float} upper-bound for THIS call. Every returned
    bucket must appear in `declared_buckets`. The harness uses this for
    pre-flight budget gating and conservatively records custom-bucket spend at
    the admitted estimate. Measured wall time replaces its estimate after the
    call and is recorded as TAG_BUDGET_OVERRUN if it exceeds a configured
    budget. The estimate itself must return finite, non-negative numeric
    values. If it raises or returns an invalid or undeclared bucket, the
    harness records an `estimate_invalid` pre-flight rejection and does not
    execute the tool.

    `observability_only=` (P3.2 / RFC-001 §3.4) marks tools whose
    side effects are infrastructure (logging sinks, metric emitters,
    trace exports) rather than application-semantic. During replay,
    such tools are logically downgraded to READ_ONLY when selecting
    a replay operation; the actual SideEffectClass is still preserved
    on the audit trail. Tools that lie about this break replay
    correctness — same threat model as lying about side_effect.
    """
    def wrap(f: Callable[..., Any]) -> Tool:
        if side_effect is None:
            raise ValidationError(
                f"@tool on {f.__name__!r} requires `side_effect=` "
                f"(P3.0: no default).  Choose: SideEffectClass.PURE, "
                f"READ_ONLY, IDEMPOTENT_WRITE, or EXTERNAL_WRITE."
            )
        normalized_side_effect = side_effect
        if isinstance(normalized_side_effect, str):
            try:
                normalized_side_effect = SideEffectClass(normalized_side_effect)
            except ValueError as exc:
                choices = ", ".join(member.value for member in SideEffectClass)
                raise ValidationError(
                    f"@tool on {f.__name__!r}: invalid side_effect="
                    f"{side_effect!r}; choose one of {choices}"
                ) from exc
        if not isinstance(normalized_side_effect, SideEffectClass):
            raise ValidationError(
                f"@tool on {f.__name__!r}: `side_effect=` must be a "
                f"SideEffectClass member or its string value, got "
                f"{type(side_effect).__name__}"
            )
        tool_name = name if name is not None else f.__name__
        tool_desc = build_description(f, description)
        params    = schema if schema is not None else build_parameters_schema(f)

        # Default: pay the two system buckets only.
        if declared_buckets is None:
            buckets = frozenset({"tool_calls", "wall_seconds"})
        else:
            buckets = frozenset(declared_buckets)
            if not buckets:
                raise ValidationError(
                    f"@tool on {f.__name__!r}: declared_buckets must be "
                    f"non-empty if provided.  Omit it for the default "
                    f"(tool_calls, wall_seconds)."
                )

        return Tool(
            name=tool_name,
            description=tool_desc,
            parameters_schema=params,
            side_effect=normalized_side_effect,
            _handler=f,
            declared_buckets=buckets,
            estimate_fn=estimate,
            observability_only=observability_only,
        )

    if fn is None:
        return wrap
    return wrap(fn)


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolRegistry:
    """Name-indexed collection of explicitly classified tools.

    Construction from an iterable is preferred for static applications, while
    ``register`` supports the natural decorator spelling used in small agent
    modules::

        tools = ToolRegistry()

        @tools.register
        @tool(side_effect=SideEffectClass.READ_ONLY)
        def search(query: str) -> str: ...

    The registry shell is frozen but its private index is intentionally owned
    by the registry, making registration a controlled compatibility operation.
    """

    _tools: dict[str, Tool] = field(default_factory=dict, init=False, repr=False)

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        indexed: dict[str, Tool] = {}
        for t in tools:
            if t.name in indexed:
                raise DuplicateToolError(
                    f"duplicate tool name {t.name!r}: "
                    f"{indexed[t.name]!r} vs {t!r}"
                )
            indexed[t.name] = t
        object.__setattr__(self, "_tools", indexed)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            available = ", ".join(sorted(self._tools)) or "<none>"
            raise ToolNotFoundError(
                f"no tool named {name!r}. available: {available}"
            ) from None

    def register(self, item: Tool) -> Tool:
        """Add ``item`` and return it, so this works as a decorator.

        Registration deliberately accepts only an already-created ``Tool``;
        callers must still choose ``side_effect=`` at the ``@tool`` boundary.
        """
        if not isinstance(item, Tool):
            raise TypeError(
                "ToolRegistry.register expects a Tool. Decorate the function "
                "with @tool(side_effect=...) first."
            )
        existing = self._tools.get(item.name)
        if existing is not None:
            raise DuplicateToolError(
                f"duplicate tool name {item.name!r}: {existing!r} vs {item!r}"
            )
        self._tools[item.name] = item
        return item

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        """Return tool names in registration order."""
        return list(self._tools)

    def declared_buckets_union(self) -> frozenset[str]:
        """Union of every Tool.declared_buckets in the registry.

        Used by deployment-side startup checks (cross-reference against
        CapabilityRow.budgets) to spot tools that touch unbudgeted
        buckets.
        """
        out: set[str] = set()
        for t in self._tools.values():
            out.update(t.declared_buckets)
        return frozenset(out)
