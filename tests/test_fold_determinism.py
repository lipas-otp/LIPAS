"""The fold strategy purity contract."""
from __future__ import annotations

import contextlib
import os
import time
from typing import Iterator
from unittest.mock import patch

import pytest

from lipas.calculus import Claim, StrategyRegistry, make_default_registry
from lipas.store import ClaimStore


class StrategyContractViolation(AssertionError):
    """A merge strategy observed a forbidden nondeterministic input."""

    def __init__(self, api: str, hint: str = ""):
        message = f"Strategy violated the purity contract: touched `{api}`."
        super().__init__(f"{message} {hint}" if hint else message)


def _trip(api: str, hint: str = ""):
    def raise_violation(*_args, **_kwargs):
        raise StrategyContractViolation(api, hint)
    return raise_violation


class _EnvironmentProxy:
    def __getitem__(self, key):
        raise StrategyContractViolation("os.environ", f"read of {key!r}")

    def get(self, key, default=None):
        raise StrategyContractViolation("os.environ", f"read of {key!r}")


@contextlib.contextmanager
def deterministic_fold() -> Iterator[None]:
    """Temporarily reject I/O, clock, random, and environment access in fold."""
    forbidden = {
        "time.time": _trip("time.time"),
        "time.monotonic": _trip("time.monotonic"),
        "time.perf_counter": _trip("time.perf_counter"),
        "random.random": _trip("random.random"),
        "random.randint": _trip("random.randint"),
        "random.choice": _trip("random.choice"),
        "socket.socket": _trip("socket.socket", "no I/O in strategies"),
    }
    patches = [
        patch(f"{name.rpartition('.')[0]}.{name.rpartition('.')[2]}", replacement)
        for name, replacement in forbidden.items()
    ]
    patches.append(patch.object(os, "environ", _EnvironmentProxy()))
    try:
        for active in patches:
            active.start()
        yield
    finally:
        for active in reversed(patches):
            active.stop()


def _fold_two(store: ClaimStore, first, second) -> None:
    store.fold(Claim(tag="t", fields={"x": first}, source="test"))
    store.fold(Claim(tag="t", fields={"x": second}, source="test"))


def test_clean_fold_passes():
    store = ClaimStore()
    with deterministic_fold():
        _fold_two(store, 1, 2)
    assert store.merged.fields.get("x") == 2


def test_unregistered_fields_are_last_write_in_fold_order():
    registry = StrategyRegistry()
    store = ClaimStore(registry=registry)
    _fold_two(store, "first", "second")
    assert store.merged.fields["x"] == "second"


def test_deterministic_fold_is_a_noop_outside_violations():
    protected, ordinary = ClaimStore(), ClaimStore()
    with deterministic_fold():
        _fold_two(protected, 1, 2)
    _fold_two(ordinary, 1, 2)
    assert protected.merged.fields == ordinary.merged.fields


def test_violation_is_detected_time_time():
    registry = make_default_registry()
    registry.register("impure_field", lambda _a, _b, _ctx: time.time())
    store = ClaimStore(registry=registry)
    store.fold(Claim(tag="t", fields={"impure_field": 1}, source="test"))

    with pytest.raises(StrategyContractViolation, match="time.time"):
        with deterministic_fold():
            store.fold(Claim(tag="t", fields={"impure_field": 2}, source="test"))


def test_violation_message_names_offending_api():
    registry = make_default_registry()
    registry.register("impure_field", lambda _a, _b, _ctx: time.time())
    store = ClaimStore(registry=registry)
    store.fold(Claim(tag="t", fields={"impure_field": 1}, source="test"))

    with pytest.raises(StrategyContractViolation) as excinfo:
        with deterministic_fold():
            store.fold(Claim(tag="t", fields={"impure_field": 2}, source="test"))

    assert "time" in str(excinfo.value).lower()
