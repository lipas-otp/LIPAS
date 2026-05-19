"""Test-time enforcement of the strategy purity contract (A3 §3)."""
from __future__ import annotations

import contextlib
import os
import random
import socket
import time
from typing import Iterator
from unittest.mock import patch


class StrategyContractViolation(AssertionError):
    """Raised when a strategy touches a forbidden input/side-effect.

    Carries the offending API name so that test failure messages point
    directly at the violation rather than at a deep stack frame.
    """

    def __init__(self, api: str, hint: str = ""):
        msg = f"Strategy violated A3 purity contract: touched `{api}`."
        if hint:
            msg += f" {hint}"
        super().__init__(msg)
        self.api = api


def _trip(api: str, hint: str = ""):
    def _raise(*_a, **_kw):
        raise StrategyContractViolation(api, hint)
    return _raise


# Closed list of forbidden APIs. Adding to this list is an A3 amendment.
_FORBIDDEN = {
    "time.time":           _trip("time.time"),
    "time.monotonic":      _trip("time.monotonic"),
    "time.perf_counter":   _trip("time.perf_counter"),
    "random.random":       _trip("random.random"),
    "random.randint":      _trip("random.randint"),
    "random.choice":       _trip("random.choice"),
    "socket.socket":       _trip("socket.socket", "no I/O in strategies"),
}


class _EnvProxy:
    """os.environ proxy that raises on any access during fold."""
    def __getitem__(self, key):
        raise StrategyContractViolation("os.environ", f"read of {key!r}")
    def __setitem__(self, key, value):
        raise StrategyContractViolation("os.environ", f"write of {key!r}")
    def get(self, key, default=None):
        raise StrategyContractViolation("os.environ", f"read of {key!r}")
    def __contains__(self, key):
        raise StrategyContractViolation("os.environ", f"check of {key!r}")


@contextlib.contextmanager
def deterministic_fold() -> Iterator[None]:
    """Context manager: enforce A3 strategy purity contract within block.

    Usage::

        from lipas.testing.deterministic_fold import deterministic_fold

        with deterministic_fold():
            store.fold(claims)

    Any strategy that calls a forbidden API during fold raises
    `StrategyContractViolation` immediately, with the API name in the message.

    Note: this wraps the *entire* fold call. Tools running inside the same
    process but outside `store.fold` are unaffected — the patches are torn
    down on exit. Do NOT nest with itself; nested entry is a no-op but the
    inner exit will tear down patches the outer relied on (deliberately
    not supported in v0.1; raises RuntimeError if attempted).
    """
    if getattr(deterministic_fold, "_active", False):
        raise RuntimeError(
            "deterministic_fold() is not re-entrant in v0.1. "
            "Wrap the outermost fold call only."
        )
    deterministic_fold._active = True

    patches = []
    try:
        for dotted, replacement in _FORBIDDEN.items():
            mod, _, attr = dotted.rpartition(".")
            patches.append(patch(f"{mod}.{attr}", replacement))
        patches.append(patch.object(os, "environ", _EnvProxy()))

        for p in patches:
            p.start()
        yield
    finally:
        for p in reversed(patches):
            try:
                p.stop()
            except RuntimeError:
                pass  # already stopped
        deterministic_fold._active = False
