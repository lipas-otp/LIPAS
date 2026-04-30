"""Token usage accounting.

Usage is the canonical token-cost record for a single LLM call. It is
folded into CapabilityRow.resource_spent at call_result time.

Design notes
------------
- Frozen + slots: Claims are immutable facts.
- `total` is a derived property, not a stored field, to prevent drift
  between components and total. Provider-reported totals are recomputed
  by the adapter from components when ingested.
- `__add__` is the semilattice fold over Usage. It is commutative,
  associative, and has Usage() as identity. This is what CapabilityRow
  uses when accumulating spend across calls.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def __post_init__(self) -> None:
        for name in ("input", "output", "cache_read", "cache_write"):
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ValueError(
                    f"Usage.{name} must be a non-negative int, got {v!r}"
                )

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def __add__(self, other: "Usage") -> "Usage":
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
        )
