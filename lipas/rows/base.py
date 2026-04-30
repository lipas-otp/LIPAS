"""
Row protocol + RowSet composition container.
"""

from __future__ import annotations
from typing import Any, Protocol, runtime_checkable

from ..calculus import Claim, StrategyRegistry
from ..store    import ClaimStore


@runtime_checkable
class Row(Protocol):
    """Structural protocol for a Row."""

    name: str

    namespace: frozenset[str]

    def register_strategies(self, registry: StrategyRegistry) -> None: ...

    def check_invariants(self, claim: Claim, store: ClaimStore) -> list[str]:
        """Return a list of violation messages. Empty = accepted."""
        ...

    def project(self, store: ClaimStore) -> Any:
        """Return this row's view over the store."""
        ...


class InvariantViolation(Exception):
    def __init__(self, claim: Claim, violations: list[tuple[str, str]]) -> None:
        self.claim = claim
        self.violations = violations
        msgs = "; ".join(f"{r}: {m}" for r, m in violations)
        super().__init__(f"fold({claim.tag!r}) rejected: {msgs}")


class RowSet:
    """A composition of rows over a single ClaimStore.

    On fold, RowSet consults every row whose namespace contains the
    claim's tag; all must pass their invariants for the fold to be
    committed.  Rows outside the namespace are unaffected.
    """

    def __init__(self, store: ClaimStore, rows: list[Row] | None = None):
        self.store = store
        self._rows: list[Row] = []
        for r in rows or ():
            self.add(r)

    # ── mutation ──────────────────────────────────────────────

    def add(self, row: Row) -> "RowSet":
        row.register_strategies(self.store.registry)
        self._rows.append(row)
        return self

    # ── lookup ────────────────────────────────────────────────

    def get(self, name: str) -> Row | None:
        for r in self._rows:
            if r.name == name:
                return r
        return None

    def owners(self, tag: str) -> list[Row]:
        return [r for r in self._rows if tag in r.namespace]

    @property
    def rows(self) -> tuple[Row, ...]:
        return tuple(self._rows)

    # ── operation ─────────────────────────────────────────────

    def fold(self, claim: Claim) -> Claim:
        violations: list[tuple[str, str]] = []
        for row in self.owners(claim.tag):
            for msg in row.check_invariants(claim, self.store):
                violations.append((row.name, msg))
        if violations:
            raise InvariantViolation(claim, violations)
        return self.store.fold(claim)

    def project(self, row_name: str) -> Any:
        r = self.get(row_name)
        if r is None:
            raise KeyError(f"no row named {row_name!r}")
        return r.project(self.store)

    def project_all(self) -> dict[str, Any]:
        return {r.name: r.project(self.store) for r in self._rows}

    def __repr__(self) -> str:
        names = ", ".join(r.name for r in self._rows)
        return f"RowSet(store_size={len(self.store)}, rows=[{names}])"
