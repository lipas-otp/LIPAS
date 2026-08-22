"""P2.1 LLMAdapter Protocol contract tests."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import AsyncIterator, Sequence

import pytest

from lipas.adapter import (
    Delta,
    Done,
    LLMAdapter,
    Message,
    ModelPrice,
    PriceTable,
    Reply,
    Request,
    ResourceEstimate,
    StreamEvent,
    StreamProtocolError,
    TextBlock,
    ToolSpec,
    UnknownModelError,
    Usage,
    complete,
)

from test_adapter_leak import assert_no_leak_under_repeated_use


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------- Fake adapter

class FakeAdapter:
    name = "fake"

    def __init__(
        self,
        events: Sequence[StreamEvent] = (),
        estimate: ResourceEstimate | None = None,
    ):
        self._events = list(events)
        self._estimate = estimate or ResourceEstimate(
            model="fake-model",
            input_tokens=0,
            max_output_tokens=0,
            max_cost_usd=Decimal("0"),
        )

    async def estimate_cost(self, request: Request) -> ResourceEstimate:
        return self._estimate

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        for e in self._events:
            yield e


def make_request(**overrides) -> Request:
    defaults = dict(
        model="fake-model",
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
        max_tokens=100,
    )
    defaults.update(overrides)
    return Request(**defaults)  # type: ignore[arg-type]


def make_reply(
    text: str = "ok",
    stop_reason: str = "end_turn",
    error_detail=None,
) -> Reply:
    return Reply(
        content=[TextBlock(text=text)] if text else [],
        usage=Usage(input=10, output=5),
        stop_reason=stop_reason,  # type: ignore[arg-type]
        model="fake-model",
        error_detail=error_detail,
    )


# ------------------------------------------------------------------ Request

class TestRequest:
    def test_minimal_defaults(self):
        r = make_request()
        assert r.system == ""
        assert r.tools == ()
        assert r.temperature is None
        assert r.stop_sequences == ()
        assert r.extra == {}

    def test_max_tokens_must_be_positive(self):
        with pytest.raises(ValueError):
            make_request(max_tokens=0)
        with pytest.raises(ValueError):
            make_request(max_tokens=-5)

    def test_max_tokens_rejects_bool(self):
        with pytest.raises(ValueError):
            make_request(max_tokens=True)  # type: ignore[arg-type]

    def test_model_required(self):
        with pytest.raises(ValueError):
            make_request(model="")

    @pytest.mark.parametrize("model", [None, 42, "   "])
    def test_model_must_be_a_non_blank_string(self, model):
        with pytest.raises(ValueError):
            make_request(model=model)  # type: ignore[arg-type]

    def test_system_must_be_a_string(self):
        with pytest.raises(TypeError):
            make_request(system={"not": "text"})  # type: ignore[arg-type]

    # --- Structural temperature validation only.
    # Provider-specific range checks belong in adapters (see protocol.py).

    def test_temperature_accepts_any_finite_number(self):
        # Including values outside any one provider's range —
        # Request is transport, not validation.
        for t in (-10.0, -1.0, 0.0, 0.7, 2.0, 5.0, 100.0):
            make_request(temperature=t)

    def test_temperature_accepts_none(self):
        make_request(temperature=None)

    def test_temperature_rejects_nan(self):
        with pytest.raises(ValueError):
            make_request(temperature=float("nan"))

    def test_temperature_rejects_inf(self):
        with pytest.raises(ValueError):
            make_request(temperature=float("inf"))
        with pytest.raises(ValueError):
            make_request(temperature=float("-inf"))

    def test_temperature_rejects_non_number(self):
        with pytest.raises(ValueError):
            make_request(temperature="0.7")  # type: ignore[arg-type]

    def test_temperature_rejects_bool(self):
        with pytest.raises(ValueError):
            make_request(temperature=True)  # type: ignore[arg-type]

    def test_extra_is_opaque(self):
        r = make_request(extra={"weird_provider_flag": True})
        assert r.extra == {"weird_provider_flag": True}


# -------------------------------------------------------- Reply contract

class TestReplyErrorDetail:
    def test_error_requires_error_detail(self):
        with pytest.raises(ValueError, match="must populate error_detail"):
            Reply(
                content=[],
                usage=Usage(),
                stop_reason="error",
                model="m",
                error_detail=None,
            )

    def test_non_error_forbids_error_detail(self):
        with pytest.raises(ValueError, match="must not.*populate error_detail"):
            Reply(
                content=[TextBlock(text="ok")],
                usage=Usage(input=1, output=1),
                stop_reason="end_turn",
                model="m",
                error_detail={"type": "ghost"},
            )

    def test_error_with_detail_ok(self):
        r = Reply(
            content=[],
            usage=Usage(),
            stop_reason="error",
            model="m",
            error_detail={"type": "rate_limit", "status_code": 429},
        )
        assert r.error_detail == {"type": "rate_limit", "status_code": 429}

    @pytest.mark.parametrize("kwargs", [
        {"model": ""},
        {"model": 42},
        {"stop_reason": "unknown_stop"},
        {"usage": {"input": 1}},
    ])
    def test_reply_rejects_invalid_core_fields(self, kwargs):
        values = {
            "content": [],
            "usage": Usage(),
            "stop_reason": "end_turn",
            "model": "m",
        }
        values.update(kwargs)
        with pytest.raises((TypeError, ValueError)):
            Reply(**values)  # type: ignore[arg-type]


# -------------------------------------------------------- ModelPrice / Table

class TestModelPrice:
    def test_zero_usage_zero_cost(self):
        p = ModelPrice(input_per_mtok=Decimal("3"), output_per_mtok=Decimal("15"))
        assert p.cost(Usage()) == Decimal("0")

    def test_input_only(self):
        p = ModelPrice(input_per_mtok=Decimal("3"), output_per_mtok=Decimal("15"))
        assert p.cost(Usage(input=1_000_000)) == Decimal("3")

    def test_output_only(self):
        p = ModelPrice(input_per_mtok=Decimal("3"), output_per_mtok=Decimal("15"))
        assert p.cost(Usage(output=1_000_000)) == Decimal("15")

    def test_all_components(self):
        p = ModelPrice(
            input_per_mtok=Decimal("3.00"),
            output_per_mtok=Decimal("15.00"),
            cache_read_per_mtok=Decimal("0.30"),
            cache_write_per_mtok=Decimal("3.75"),
        )
        u = Usage(
            input=1_000_000,
            output=1_000_000,
            cache_read=1_000_000,
            cache_write=1_000_000,
        )
        assert p.cost(u) == Decimal("22.05")

    def test_returns_decimal_not_float(self):
        p = ModelPrice(input_per_mtok=Decimal("0.01"), output_per_mtok=Decimal("0.02"))
        assert isinstance(p.cost(Usage(input=100)), Decimal)

    @pytest.mark.parametrize("kwargs", [
        {"input_per_mtok": Decimal("-1"), "output_per_mtok": Decimal("1")},
        {"input_per_mtok": Decimal("NaN"), "output_per_mtok": Decimal("1")},
        {"input_per_mtok": 1, "output_per_mtok": Decimal("1")},
    ])
    def test_prices_must_be_finite_non_negative_decimals(self, kwargs):
        with pytest.raises((TypeError, ValueError)):
            ModelPrice(**kwargs)  # type: ignore[arg-type]


class TestPriceTable:
    def test_lookup_hit(self):
        p = ModelPrice(input_per_mtok=Decimal("1"), output_per_mtok=Decimal("2"))
        table = PriceTable(prices={"m1": p})
        assert table.for_model("m1") is p

    def test_lookup_miss_raises(self):
        with pytest.raises(UnknownModelError) as ei:
            PriceTable(prices={}).for_model("ghost-model")
        assert ei.value.model == "ghost-model"


# ----------------------------------------------------------- ResourceEstimate

class TestResourceEstimate:
    def test_basic(self):
        e = ResourceEstimate(
            model="m",
            input_tokens=100,
            max_output_tokens=500,
            max_cost_usd=Decimal("0.05"),
        )
        assert e.input_tokens == 100
        assert e.max_cost_usd == Decimal("0.05")

    @pytest.mark.parametrize("kwargs", [
        dict(input_tokens=-1, max_output_tokens=0, max_cost_usd=Decimal("0")),
        dict(input_tokens=0, max_output_tokens=-1, max_cost_usd=Decimal("0")),
        dict(input_tokens=0, max_output_tokens=0, max_cost_usd=Decimal("-0.01")),
    ])
    def test_negatives_rejected(self, kwargs):
        with pytest.raises(ValueError):
            ResourceEstimate(model="m", **kwargs)

    @pytest.mark.parametrize("kwargs", [
        dict(input_tokens=True, max_output_tokens=0, max_cost_usd=Decimal("0")),
        dict(input_tokens=0, max_output_tokens=1.5, max_cost_usd=Decimal("0")),
        dict(input_tokens=0, max_output_tokens=0, max_cost_usd=Decimal("NaN")),
        dict(input_tokens=0, max_output_tokens=0, max_cost_usd=0.0),
    ])
    def test_invalid_numeric_shapes_rejected(self, kwargs):
        with pytest.raises((TypeError, ValueError)):
            ResourceEstimate(model="m", **kwargs)  # type: ignore[arg-type]


# -------------------------------------------------------------- ToolSpec

class TestToolSpec:
    def test_basic(self):
        t = ToolSpec(
            name="search",
            description="search the web",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        )
        assert t.name == "search"


# ---------------------------------------------------- Protocol conformance

class TestProtocolConformance:
    def test_fake_adapter_passes_isinstance(self):
        assert isinstance(FakeAdapter(), LLMAdapter)

    def test_missing_name_fails_isinstance(self):
        class NoName:
            async def estimate_cost(self, r): ...
            def stream(self, r): ...
        assert not isinstance(NoName(), LLMAdapter)

    def test_missing_stream_fails_isinstance(self):
        class NoStream:
            name = "x"
            async def estimate_cost(self, r): ...
        assert not isinstance(NoStream(), LLMAdapter)

    def test_missing_estimate_cost_fails_isinstance(self):
        class NoEstimate:
            name = "x"
            def stream(self, r): ...
        assert not isinstance(NoEstimate(), LLMAdapter)

    def test_estimate_cost_callable_async(self):
        a = FakeAdapter(estimate=ResourceEstimate(
            model="fake-model",
            input_tokens=42,
            max_output_tokens=100,
            max_cost_usd=Decimal("0.01"),
        ))
        result = run(a.estimate_cost(make_request()))
        assert result.input_tokens == 42


# ---------------------------------------------------------- complete()

class TestComplete:
    def test_returns_reply_from_done(self):
        reply = make_reply(text="hello")
        a = FakeAdapter(events=[
            Delta(index=0, text="hel"),
            Delta(index=0, text="lo"),
            Done(reply=reply),
        ])
        assert run(complete(a, make_request())) is reply

    def test_raises_if_stream_ends_without_done(self):
        a = FakeAdapter(events=[Delta(index=0, text="incomplete")])
        with pytest.raises(StreamProtocolError):
            run(complete(a, make_request()))

    def test_raises_on_empty_stream(self):
        with pytest.raises(StreamProtocolError):
            run(complete(FakeAdapter(events=[]), make_request()))

    def test_done_with_error_returns_reply_not_raise(self):
        # Locked error contract: provider-side failures arrive as
        # Done(Reply(stop_reason='error', error_detail=...)). complete()
        # must NOT raise — partial content is preserved, error_detail
        # carries provider-raw diagnostics for upstream ErrorKind
        # classification.
        partial = make_reply(
            text="partial...",
            stop_reason="error",
            error_detail={
                "type": "rate_limit",
                "status_code": 429,
                "message": "Too many requests",
            },
        )
        a = FakeAdapter(events=[
            Delta(index=0, text="partial..."),
            Done(reply=partial),
        ])
        result = run(complete(a, make_request()))
        assert result.stop_reason == "error"
        assert result.error_detail is not None
        assert result.error_detail["status_code"] == 429
        # Partial content preserved.
        assert result.content[0].text == "partial..."  # type: ignore[attr-defined]

    def test_returns_first_done_and_stops(self):
        r1 = make_reply(text="first")
        r2 = make_reply(text="second")
        a = FakeAdapter(events=[Done(reply=r1), Done(reply=r2)])
        assert run(complete(a, make_request())) is r1


# -------------------------------------------------------------- Leak helper

class TestLeakHelperSelfTest:
    """Self-test the leak utility against FakeAdapter (trivially
    stateless) and a deliberately leaky adapter. If the first test
    fails, the utility is too tight; if the second passes, the
    utility is too lax."""

    def test_stateless_adapter_passes_leak_check(self):
        async def call_once(a: FakeAdapter) -> None:
            await complete(a, make_request())

        def construct() -> FakeAdapter:
            return FakeAdapter(events=[Done(reply=make_reply())])

        run(assert_no_leak_under_repeated_use(
            construct=construct,
            call=call_once,
            iterations=100,
        ))

    def test_helper_catches_retained_adapter_instances(self):
        # An adapter that — via some bug — gets stashed in a global
        # registry, callback list, async task, etc. would accumulate
        # exactly like this.
        sink: list[FakeAdapter] = []

        def leaky_construct() -> FakeAdapter:
            a = FakeAdapter(events=[Done(reply=make_reply())])
            sink.append(a)
            return a

        async def call_once(a: FakeAdapter) -> None:
            await complete(a, make_request())

        with pytest.raises(AssertionError, match="remain live"):
            run(assert_no_leak_under_repeated_use(
                construct=leaky_construct,
                call=call_once,
                iterations=100,
            ))

    def test_helper_catches_retained_inner_resource(self):
        # Adapter is freed but its inner client (mock for httpx)
        # is kept alive by some background task / closure. Tracking
        # the inner type catches this even though the adapter itself
        # was correctly released.
        class FakeClient:
            pass

        leaked_clients: list[FakeClient] = []

        class AdapterHoldingClient:
            name = "leaky"

            def __init__(self) -> None:
                self._client = FakeClient()
                leaked_clients.append(self._client)  # the leak

            async def estimate_cost(self, request):
                return ResourceEstimate(
                    model="m", input_tokens=0,
                    max_output_tokens=0, max_cost_usd=Decimal("0"),
                )

            async def stream(self, request):
                yield Done(reply=make_reply())

        async def call_once(a: AdapterHoldingClient) -> None:
            await complete(a, make_request())

        with pytest.raises(AssertionError, match="FakeClient"):
            run(assert_no_leak_under_repeated_use(
                construct=AdapterHoldingClient,
                call=call_once,
                iterations=100,
                track_types=(AdapterHoldingClient, FakeClient),
            ))
