"""Public AgentState and FinalResult shape invariants."""
from __future__ import annotations

import pytest

from lipas.behaviour import AgentState, FinalResult, TerminationReason


@pytest.mark.parametrize(
    "kwargs,error_type",
    [
        ({"messages": []}, TypeError),
        ({"iteration": True}, ValueError),
        ({"iteration": -1}, ValueError),
        ({"metadata": []}, TypeError),
    ],
)
def test_agent_state_rejects_shapes_that_cannot_run_safely(kwargs, error_type):
    with pytest.raises(error_type):
        AgentState(**kwargs)


@pytest.mark.parametrize(
    "kwargs,error_type",
    [
        ({"text": 1}, TypeError),
        ({"stop_reason": ""}, ValueError),
        ({"stop_reason": TerminationReason.ERROR}, ValueError),
        ({"stop_reason": TerminationReason.CANCELLED, "error": {"type": "x"}}, ValueError),
    ],
)
def test_final_result_enforces_error_and_terminal_shape(kwargs, error_type):
    with pytest.raises(error_type):
        FinalResult(**kwargs)
