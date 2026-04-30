# $ python -m pytest ./test_types_lockdown.py
# ============================================================= test session starts =============================================================
# platform win32 -- Python 3.12.7, pytest-7.4.4, pluggy-1.0.0
# rootdir: C:\Users\truth\Desktop\redesign
# plugins: jaxtyping-0.3.9, anyio-4.2.0
# collected 42 items
#
# test_types_lockdown.py ..........................................                                                                        [100%]
#
# ============================================================= 42 passed in 0.22s ==============================================================

"""Lockdown tests for types.py — these encode design decisions that future
refactors must not silently break. If you touch types.py and these fail,
STOP and revisit the design discussion before "fixing" the tests.
"""
import asyncio
import threading
import pytest
from lipas import (
    ToolCall, Usage, Reply, ThinkingBlock,
    ToolAlreadyInvoked, LipasError,
)


# =============================================================================
# Usage / Reply: keyword-only construction  (INV-FROZEN-REFS)
# =============================================================================

def test_usage_is_kw_only():
    with pytest.raises(TypeError):
        Usage(100, 200, 50, 10, 5)
    Usage(input_tokens=100)

def test_reply_is_kw_only():
    with pytest.raises(TypeError):
        Reply("openai")
    Reply(provider="openai")

def test_reply_provider_required():
    with pytest.raises(TypeError):
        Reply()


# =============================================================================
# Usage: frozen and defaults  (INV-FROZEN-REFS)
# =============================================================================

def test_usage_defaults_to_zero():
    u = Usage()
    assert u.input_tokens == 0
    assert u.output_tokens == 0
    assert u.reasoning_tokens == 0
    assert u.cache_read_tokens == 0
    assert u.cache_write_tokens == 0

def test_usage_is_frozen():
    u = Usage(input_tokens=5)
    with pytest.raises((AttributeError, TypeError)):
        u.input_tokens = 99  # type: ignore[misc]

def test_usage_equality():
    assert Usage(input_tokens=10, output_tokens=20) == Usage(input_tokens=10, output_tokens=20)
    assert Usage(input_tokens=10) != Usage(input_tokens=99)


# =============================================================================
# Reply: frozen, defaults, field contract  (INV-FROZEN-REFS)
# =============================================================================

def test_reply_is_frozen():
    r = Reply(provider="openai")
    with pytest.raises((AttributeError, TypeError)):
        r.text = "mutated"  # type: ignore[misc]

def test_reply_defaults():
    r = Reply(provider="openai")
    assert r.text == ""
    assert r.thinking == ()
    assert r.tool_calls == ()
    assert r.stop_reason == "end_turn"
    assert isinstance(r.usage, Usage)
    assert r.response_id is None
    assert r.provider_message is None
    assert r.native_content_blocks is None
    assert r.latency_ms is None

def test_reply_tool_calls_is_tuple():
    """tool_calls must be a tuple — list would allow post-construction mutation."""
    tc = ToolCall(id="1", name="f", arguments={})
    r = Reply(provider="openai", tool_calls=(tc,))
    assert isinstance(r.tool_calls, tuple)

def test_reply_thinking_is_tuple():
    tb = ThinkingBlock(text="hmm")
    r = Reply(provider="openai", thinking=(tb,))
    assert isinstance(r.thinking, tuple)

def test_reply_mutable_toolcall_inside_frozen_reply():
    """The frozen-ness applies to the REFERENCE SET, not to ToolCall contents.
    ToolCall.outcome can be written after the Reply is constructed."""
    tc = ToolCall(id="1", name="f", arguments={}, _handler=lambda: 42)
    r = Reply(provider="openai", tool_calls=(tc,))
    # Reply is frozen — can't replace the tuple reference:
    with pytest.raises((AttributeError, TypeError)):
        r.tool_calls = ()  # type: ignore[misc]
    # But the ToolCall inside can still be invoked (mutated):
    r.tool_calls[0].invoke()
    assert r.tool_calls[0].outcome == "ok"
    assert r.tool_calls[0].result == 42


# =============================================================================
# ThinkingBlock: frozen  (INV-FROZEN-REFS)
# =============================================================================

def test_thinkingblock_is_frozen():
    tb = ThinkingBlock(text="reasoning", signature="sig123")
    with pytest.raises((AttributeError, TypeError)):
        tb.text = "mutated"  # type: ignore[misc]

def test_thinkingblock_signature_optional():
    tb = ThinkingBlock(text="reasoning")
    assert tb.signature is None


# =============================================================================
# ToolCall: unhashable  (INV-FROZEN-REFS)
# =============================================================================

def test_toolcall_is_unhashable():
    tc = ToolCall(id="1", name="x", arguments={})
    assert ToolCall.__hash__ is None
    with pytest.raises(TypeError):
        {tc}
    with pytest.raises(TypeError):
        {tc: 1}


# =============================================================================
# ToolCall: structural equality on user-facing fields only
# =============================================================================

def test_toolcall_structural_equality_ignores_internals():
    a = ToolCall(id="1", name="x", arguments={"q": 1})
    b = ToolCall(id="1", name="x", arguments={"q": 1})
    assert a == b  # different Lock() instances, but compare=False

    # Binding a handler to one side must not break equality.
    b._handler = lambda q: q
    assert a == b

    # Claiming (but not completing) one side must not break equality:
    # _invoked flips True but outcome stays None, and _invoked is compare=False.
    b._claim()
    assert a == b

def test_toolcall_equality_tracks_outcome():
    a = ToolCall(id="1", name="x", arguments={}, _handler=lambda: 42)
    b = ToolCall(id="1", name="x", arguments={}, _handler=lambda: 42)
    a.invoke()
    assert a != b              # a is ok/42, b is fresh
    b.invoke()
    assert a == b              # both ok/42

def test_toolcall_equality_against_fresh_expectation():
    """The intended usage pattern: assert a received ToolCall equals
    a freshly-constructed expected one."""
    received = ToolCall(id="call_1", name="search",
                        arguments={"q": "lipas"}, _handler=lambda q: [q])
    received.invoke()
    expected = ToolCall(id="call_1", name="search", arguments={"q": "lipas"},
                        outcome="ok", result=["lipas"])
    assert received == expected

def test_toolcall_neq_different_id():
    a = ToolCall(id="1", name="f", arguments={})
    b = ToolCall(id="2", name="f", arguments={})
    assert a != b

def test_toolcall_neq_different_name():
    a = ToolCall(id="1", name="f", arguments={})
    b = ToolCall(id="1", name="g", arguments={})
    assert a != b

def test_toolcall_neq_different_arguments():
    a = ToolCall(id="1", name="f", arguments={"x": 1})
    b = ToolCall(id="1", name="f", arguments={"x": 2})
    assert a != b


# =============================================================================
# ToolCall: error-state identity caveat  (docstring lock)
# =============================================================================

def test_toolcall_error_eq_is_identity_per_docstring():
    """Documents (and locks) the caveat: exception equality is identity,
    so two 'equivalent' error ToolCalls do NOT compare equal unless they
    share the exception instance. If Python ever changes this or we wrap
    errors, revisit the docstring."""
    def boom(): raise ValueError("x")
    a = ToolCall(id="1", name="t", arguments={}, _handler=boom)
    b = ToolCall(id="1", name="t", arguments={}, _handler=boom)
    with pytest.raises(ValueError): a.invoke()
    with pytest.raises(ValueError): b.invoke()
    assert a.outcome == "error" and b.outcome == "error"
    assert type(a.error) is type(b.error) is ValueError
    assert a != b  # different ValueError instances — identity

def test_toolcall_error_shared_instance_eq():
    """Complement: sharing the same exception instance makes them equal."""
    exc = ValueError("shared")
    a = ToolCall(id="1", name="t", arguments={}, outcome="error", error=exc)
    b = ToolCall(id="1", name="t", arguments={}, outcome="error", error=exc)
    assert a == b


# =============================================================================
# ToolCall: INV-TOOLCALL-ONCE — second call always raises
# =============================================================================

def test_invoke_second_call_raises_already_invoked():
    tc = ToolCall(id="1", name="f", arguments={}, _handler=lambda: 1)
    tc.invoke()
    with pytest.raises(ToolAlreadyInvoked):
        tc.invoke()

def test_invoke_after_error_raises_already_invoked():
    """Handler raising does NOT unlock retry — _invoked is set on ENTRY."""
    def boom(): raise RuntimeError("handler failed")
    tc = ToolCall(id="1", name="f", arguments={}, _handler=boom)
    with pytest.raises(RuntimeError):
        tc.invoke()
    with pytest.raises(ToolAlreadyInvoked):
        tc.invoke()

def test_ainvoke_second_call_raises():
    async def run():
        tc = ToolCall(id="1", name="f", arguments={}, _handler=lambda: 1)
        tc.invoke()
        with pytest.raises(ToolAlreadyInvoked):
            await tc.ainvoke()
    asyncio.run(run())

def test_invoke_then_ainvoke_raises():
    """INV-TOOLCALL-ONCE: counter is shared across sync and async."""
    async def run():
        tc = ToolCall(id="1", name="f", arguments={}, _handler=lambda: 99)
        tc.invoke()
        with pytest.raises(ToolAlreadyInvoked):
            await tc.ainvoke()
    asyncio.run(run())

def test_ainvoke_then_invoke_raises():
    async def run():
        tc = ToolCall(id="1", name="f", arguments={}, _handler=lambda: 99)
        await tc.ainvoke()
        with pytest.raises(ToolAlreadyInvoked):
            tc.invoke()
    asyncio.run(run())


# =============================================================================
# ToolCall: terminal state invariant — no "claimed but undefined" observable
# =============================================================================

def test_invoke_sets_outcome_ok_on_success():
    tc = ToolCall(id="1", name="f", arguments={}, _handler=lambda: "hello")
    result = tc.invoke()
    assert result == "hello"
    assert tc.outcome == "ok"
    assert tc.result == "hello"
    assert tc.error is None

def test_invoke_sets_outcome_error_on_raise():
    def boom(): raise ValueError("oops")
    tc = ToolCall(id="1", name="f", arguments={}, _handler=boom)
    with pytest.raises(ValueError):
        tc.invoke()
    assert tc.outcome == "error"
    assert isinstance(tc.error, ValueError)
    assert tc.result is None

def test_invoke_no_handler_sets_outcome_error():
    """No handler → LipasError, but outcome MUST be written (terminal state)."""
    tc = ToolCall(id="1", name="f", arguments={})
    with pytest.raises(LipasError):
        tc.invoke()
    assert tc.outcome == "error"
    assert isinstance(tc.error, LipasError)

def test_ainvoke_sets_outcome_ok():
    async def run():
        tc = ToolCall(id="1", name="f", arguments={}, _handler=lambda: 7)
        result = await tc.ainvoke()
        assert result == 7
        assert tc.outcome == "ok"
        assert tc.result == 7
    asyncio.run(run())

def test_ainvoke_async_handler():
    async def ahandler(): return "async_result"
    async def run():
        tc = ToolCall(id="1", name="f", arguments={}, _handler=ahandler)
        result = await tc.ainvoke()
        assert result == "async_result"
        assert tc.outcome == "ok"
    asyncio.run(run())

def test_ainvoke_sets_outcome_error_on_raise():
    async def run():
        def boom(): raise KeyError("k")
        tc = ToolCall(id="1", name="f", arguments={}, _handler=boom)
        with pytest.raises(KeyError):
            await tc.ainvoke()
        assert tc.outcome == "error"
        assert isinstance(tc.error, KeyError)
    asyncio.run(run())

def test_invoke_coroutine_misuse_sets_outcome_error():
    """invoke() called with an async handler → LipasError, outcome written."""
    async def async_handler(): return 1
    tc = ToolCall(id="1", name="f", arguments={}, _handler=async_handler)
    with pytest.raises(LipasError, match="coroutine"):
        tc.invoke()
    assert tc.outcome == "error"
    assert isinstance(tc.error, LipasError)


# =============================================================================
# ToolCall: argument passing and override
# =============================================================================

def test_invoke_passes_arguments():
    captured = {}
    def handler(x, y): captured.update(x=x, y=y)
    tc = ToolCall(id="1", name="f", arguments={"x": 1, "y": 2}, _handler=handler)
    tc.invoke()
    assert captured == {"x": 1, "y": 2}

def test_invoke_override_merges():
    captured = {}
    def handler(x, y): captured.update(x=x, y=y)
    tc = ToolCall(id="1", name="f", arguments={"x": 1, "y": 2}, _handler=handler)
    tc.invoke(y=99)
    assert captured == {"x": 1, "y": 99}


# =============================================================================
# ToolCall: copy() — fresh instance for retry
# =============================================================================

def test_copy_is_fresh():
    tc = ToolCall(id="1", name="f", arguments={"a": 1}, _handler=lambda a: a)
    tc.invoke()
    assert tc.outcome == "ok"

    fresh = tc.copy()
    assert fresh.outcome is None
    assert fresh.result is None
    assert fresh.error is None
    assert fresh._invoked is False

def test_copy_preserves_fields():
    tc = ToolCall(id="x", name="search", arguments={"q": "lipas"},
                  _handler=lambda q: q)
    fresh = tc.copy()
    assert fresh.id == "x"
    assert fresh.name == "search"
    assert fresh.arguments == {"q": "lipas"}
    assert fresh._handler is tc._handler

def test_copy_arguments_is_independent():
    """copy() must deep-copy arguments so mutations don't bleed back."""
    tc = ToolCall(id="1", name="f", arguments={"x": 1}, _handler=lambda x: x)
    fresh = tc.copy()
    fresh.arguments["x"] = 999
    assert tc.arguments["x"] == 1

def test_copy_can_be_invoked():
    tc = ToolCall(id="1", name="f", arguments={}, _handler=lambda: "original")
    tc.invoke()
    fresh = tc.copy()
    result = fresh.invoke()
    assert result == "original"
    assert fresh.outcome == "ok"


# =============================================================================
# ToolCall: INV-PARALLEL-SAFE — concurrent invocations of DIFFERENT instances
# =============================================================================

def test_parallel_different_toolcalls_no_race():
    """Two different ToolCalls can be invoked concurrently without serialization."""
    import time
    results = {}
    def slow_handler(name, delay):
        time.sleep(delay)
        results[name] = True

    tc_a = ToolCall(id="a", name="fa", arguments={"name": "a", "delay": 0.05},
                    _handler=slow_handler)
    tc_b = ToolCall(id="b", name="fb", arguments={"name": "b", "delay": 0.05},
                    _handler=slow_handler)

    t1 = threading.Thread(target=tc_a.invoke)
    t2 = threading.Thread(target=tc_b.invoke)
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert results == {"a": True, "b": True}
    assert tc_a.outcome == "ok"
    assert tc_b.outcome == "ok"

def test_concurrent_double_invoke_same_instance_exactly_one_wins():
    """Exactly one of two racing invoke() calls must succeed; the other raises.
    INV-TOOLCALL-ONCE is enforced by a threading.Lock check-and-set."""
    winner = []
    loser = []
    barrier = threading.Barrier(2)

    tc = ToolCall(id="1", name="f", arguments={}, _handler=lambda: "done")

    def try_invoke():
        barrier.wait()  # synchronize start to maximise contention
        try:
            tc.invoke()
            winner.append(True)
        except ToolAlreadyInvoked:
            loser.append(True)

    t1 = threading.Thread(target=try_invoke)
    t2 = threading.Thread(target=try_invoke)
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert len(winner) == 1, f"expected 1 winner, got {len(winner)}"
    assert len(loser) == 1, f"expected 1 loser, got {len(loser)}"
