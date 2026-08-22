# tests/conftest.py —— pytest autoload it, to add project root to sys.path
# """Shared fixtures for B2 replay-tools tests."""
from __future__ import annotations
import sys
import pathlib
import pytest

from lipas.calculus import make_default_registry
from lipas.rows import RowSet
from lipas.rows.capability import CapabilityRow
from lipas.rows.effect import EffectRow
from lipas.rows.history import HistoryRow
from lipas.store import ClaimStore
from lipas.tools import SideEffectClass, ToolRegistry, tool


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# A few older tests import shared helpers as top-level modules while
# newer ones use ``tests.<module>``.  Make both forms resolve to this
# checkout instead of an unrelated installed ``tests`` package.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


# ── Tools (PURE) ──────────────────────────────────────────────────────

@tool(side_effect=SideEffectClass.PURE)
def add(a: float, b: float) -> float:
    """Return the sum of two numbers."""
    return a + b


@tool(side_effect=SideEffectClass.PURE)
def multiply(a: float, b: float) -> float:
    """Return the product of two numbers."""
    return a * b


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def tools() -> ToolRegistry:
    return ToolRegistry([add, multiply])


@pytest.fixture
def fresh_rowset():
    """Factory: every call returns an independent (store, rowset) pair."""
    def _make() -> RowSet:
        registry = make_default_registry()
        store = ClaimStore(registry=registry)
        return RowSet(store, rows=[
            HistoryRow(),
            CapabilityRow(budgets={
                "tool_calls":   100.0,
                "wall_seconds": 60.0,
            }),
            EffectRow(),
        ])
    return _make


# ─────────────────────────────────────────────────────────────────────

@tool(side_effect=SideEffectClass.READ_ONLY)
async def read_thing(q: str) -> str:
    """Read a thing by id and return its current value."""
    return f"read:{q}"


@tool(side_effect=SideEffectClass.IDEMPOTENT_WRITE)
async def upsert_thing(key: str, value: str) -> str:
    """Upsert a thing by key."""
    return f"upsert:{key}={value}"


@tool(side_effect=SideEffectClass.EXTERNAL_WRITE)
async def send_thing(target: str, payload: str) -> str:
    """Set a thing."""
    return f"sent:{target}:{payload}"


@pytest.fixture
def all_tools() -> ToolRegistry:
    """Registry covering all four SideEffectClass values."""
    return ToolRegistry([
        add, multiply, read_thing, upsert_thing, send_thing,
    ])
