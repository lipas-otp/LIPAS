"""Token pricing.

Provider-agnostic price book. Adapters look up a ModelPrice by model
name to compute USD cost from a Usage record.

Decimal everywhere — no float in the cost path.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from .usage import Usage


class UnknownModelError(KeyError):
    """Raised when a model is not present in a PriceTable."""

    def __init__(self, model: str):
        super().__init__(model)
        self.model = model


_MILLION = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Per-1M-token prices, in USD."""
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal = Decimal("0")
    cache_write_per_mtok: Decimal = Decimal("0")

    def cost(self, usage: Usage) -> Decimal:
        return (
            self.input_per_mtok * usage.input
            + self.output_per_mtok * usage.output
            + self.cache_read_per_mtok * usage.cache_read
            + self.cache_write_per_mtok * usage.cache_write
        ) / _MILLION


@dataclass(frozen=True, slots=True)
class PriceTable:
    prices: Mapping[str, ModelPrice]

    def for_model(self, model: str) -> ModelPrice:
        if model not in self.prices:
            raise UnknownModelError(model)
        return self.prices[model]
