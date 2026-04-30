"""
Identity Row — the trust / authorization projection.

Uses lipas.identity.Principal and lipas.identity.PrincipalRegistry
as its substrate.  The row adds claim-based commentary: trust score
updates (Beta-distributed), delegation events, revocations.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional, Set

from ..calculus import Claim, StrategyRegistry, strategy_append
from ..store    import ClaimStore

try:
    from ..identity import PrincipalRegistry
except ImportError:
    PrincipalRegistry = None  # type: ignore[misc, assignment]

@dataclass
class IdentityRow:
    name: str = "identity"
    namespace: frozenset[str] = field(
        default_factory=lambda: frozenset({
            "trust_update", "delegation", "revocation",
        })
    )
    registry_of_principals: Optional[PrincipalRegistry] = None

    def register_strategies(self, registry: StrategyRegistry) -> None:
        # Beta-trust merge from perception.py; import lazily to avoid
        # a hard dependency for users who don't use trust tracking.
        try:
            from ..perception import strategy_trust_merge
            registry.register("_perception_of", strategy_trust_merge)
        except ImportError:
            pass
        registry.register("_delegations", strategy_append)
        registry.register("_revocations", strategy_append)

    def check_invariants(self, claim: Claim, store: ClaimStore) -> list[str]:
        msgs: list[str] = []

        if claim.tag == "delegation":
            src = claim.fields.get("from")
            dst = claim.fields.get("to")
            if not src or not dst:
                msgs.append("delegation: missing 'from' or 'to'")
            reg = self.registry_of_principals
            if reg is not None:
                if src and src not in reg:
                    msgs.append(f"delegation: unknown source principal {src!r}")
                if dst and dst not in reg:
                    msgs.append(f"delegation: unknown target principal {dst!r}")

        elif claim.tag == "revocation":
            revs = claim.fields.get("_revocations") or []
            if not revs:
                msgs.append("revocation: missing _revocations list")
            else:
                for i, r in enumerate(revs):
                    if not r.get("claim_id"):
                        msgs.append(f"revocation[{i}]: missing target claim_id")
        return msgs

    def project(self, store: ClaimStore) -> dict:
        fields = store.merged.fields
        perception    = fields.get("_perception_of", {}) or {}
        delegations   = fields.get("_delegations",   []) or []
        revocations   = fields.get("_revocations",   []) or []
        revoked_ids   = {r.get("claim_id") for r in revocations
                         if isinstance(r, dict)}
        active_deleg  = [d for d in delegations
                         if isinstance(d, dict)
                         and d.get("claim_id") not in revoked_ids]
        return {
            "trust_scores":       dict(perception),
            "active_delegations": active_deleg,
            "revocation_count":   len(revocations),
            "principal_count": (len(self.registry_of_principals)
                                if self.registry_of_principals else 0),
        }

    def __repr__(self) -> str:
        return f"IdentityRow(namespace={sorted(self.namespace)})"


