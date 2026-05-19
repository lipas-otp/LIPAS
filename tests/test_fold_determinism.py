"""A3 §5 — regression tests for fold-time strategy purity."""
import pytest

from lipas.calculus import register_strategy, strategy_registry
from lipas.store import Store
from lipas.testing.deterministic_fold import (
    StrategyContractViolation,
    deterministic_fold,
)
# Import each example to register its strategies + run a representative trace.
from examples import (
    ex01_minimal,
    ex02_two_tools,
    ex03_supervisor_lite,
    ex04_belief_merge,
    ex05_replay_cursor,
    ex06_capability_budget,
)


# --- Test 1: examples regression -------------------------------------------

ALL_EXAMPLES = [
    ex01_minimal,
    ex02_two_tools,
    ex03_supervisor_lite,
    ex04_belief_merge,
    ex05_replay_cursor,
    ex06_capability_budget,
]


@pytest.mark.parametrize("example", ALL_EXAMPLES, ids=lambda m: m.__name__)
def test_example_folds_are_deterministic(example):
    """Every shipped example must fold cleanly under the A3 contract."""
    with deterministic_fold():
        result = example.run()  # each example exposes a top-level run()
    assert result is not None, f"{example.__name__}.run() returned None"


# --- Test 2: positive control (the tool MUST catch a real violation) -------

def test_violation_is_detected():
    """Sanity check: a deliberately-impure strategy is caught.

    This guards against the test tool silently no-op'ing — without this,
    a future refactor that breaks the patches would make Test 1 pass
    vacuously."""
    import time

    @register_strategy("a3_test_impure_strategy")
    def impure(a, b, ctx, registry):
        return a + time.time()  # forbidden

    store = Store()
    try:
        with pytest.raises(StrategyContractViolation) as excinfo:
            with deterministic_fold():
                # a minimal claim sequence that will route to `impure`
                store.fold_with_strategy("a3_test_impure_strategy", 1, 2)
        assert excinfo.value.api == "time.time"
    finally:
        # registry hygiene: unregister so other tests aren't polluted
        strategy_registry.unregister("a3_test_impure_strategy")
