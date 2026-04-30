"""Forward-looking resource estimate for an LLM call.

ResourceEstimate is what `LLMAdapter.estimate_cost(request)` returns —
an UPPER BOUND on what the call will consume:

    input_tokens       : exact (we know what we're sending)
    max_output_tokens  : = request.max_tokens (true ceiling)
    max_cost_usd       : computed from above + ModelPrice (worst case)

This is what policy uses for admission control: "would this call put us
over budget?" Because output is unknown ahead of time, the estimate is
always worst-case.

Actual spend is recorded post-call as Usage on call_result and folded
into CapabilityRow.resource_spent.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ResourceEstimate:
    model: str
    input_tokens: int
    max_output_tokens: int
    max_cost_usd: Decimal

    def __post_init__(self) -> None:
        if self.input_tokens < 0:
            raise ValueError(f"input_tokens must be >= 0, got {self.input_tokens}")
        if self.max_output_tokens < 0:
            raise ValueError(f"max_output_tokens must be >= 0, got {self.max_output_tokens}")
        if self.max_cost_usd < 0:
            raise ValueError(f"max_cost_usd must be >= 0, got {self.max_cost_usd}")
