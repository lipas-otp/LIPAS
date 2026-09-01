"""Explicit policy contracts shared by coordination branches.

These values are host configuration, not another execution state machine. The
coordinator persists budget reservations in ``ExecutionStore`` so competing
workers cannot both pass the same pre-flight check.
"""
from __future__ import annotations

import math
import time
import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from types import MappingProxyType
from pathlib import Path

from .sqlite_storage import connect_sqlite, immediate_transaction

_MAX_SQLITE_INT = 2**63 - 1

__all__ = [
    "CapabilityPolicy", "SharedBudgetPolicy", "WorkspaceIdentity",
    "ApprovalDelegation", "WorkspacePolicyStore",
]


BudgetEstimator = Callable[[Any, Any], Mapping[str, float]]


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    """Host-owned identity used for shared workspace audit and policy scope."""

    identity_id: str
    display_name: str
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name in ("identity_id", "display_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        for name in ("roles", "scopes"):
            value = getattr(self, name)
            if not isinstance(value, frozenset):
                raise TypeError(f"{name} must be a frozenset")
            if any(not isinstance(item, str) or not item.strip() for item in value):
                raise ValueError(f"{name} must contain non-empty strings")
            normalized = frozenset(item.strip() for item in value)
            if len(normalized) != len(value):
                raise ValueError(f"{name} contains duplicate values after normalization")
            object.__setattr__(self, name, normalized)

    def can(self, scope: str) -> bool:
        if not isinstance(scope, str) or not scope.strip():
            return False
        scope = scope.strip()
        return (
            scope in self.scopes
            or "*" in self.scopes
            or any(
                grant.endswith(":*") and scope.startswith(grant[:-1])
                for grant in self.scopes
            )
        )


@dataclass(frozen=True, slots=True)
class ApprovalDelegation:
    """A bounded approval authority grant; it never changes an Interrupt alone."""

    delegation_id: str
    grantor: WorkspaceIdentity
    delegate: WorkspaceIdentity
    scopes: frozenset[str]
    expires_at: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.delegation_id, str) or not self.delegation_id.strip():
            raise ValueError("delegation_id must be a non-empty string")
        if not isinstance(self.grantor, WorkspaceIdentity) or not isinstance(
            self.delegate, WorkspaceIdentity,
        ):
            raise TypeError("grantor and delegate must be WorkspaceIdentity")
        if not isinstance(self.scopes, frozenset) or not self.scopes:
            raise ValueError("delegation scopes must be a non-empty frozenset")
        if any(not isinstance(item, str) or not item.strip() for item in self.scopes):
            raise ValueError("delegation scopes must contain non-empty strings")
        normalized = frozenset(item.strip() for item in self.scopes)
        if len(normalized) != len(self.scopes):
            raise ValueError("delegation scopes contain duplicate values after normalization")
        object.__setattr__(self, "delegation_id", self.delegation_id.strip())
        object.__setattr__(self, "scopes", normalized)
        if self.expires_at is not None:
            expires_at = _finite_number(self.expires_at, "expires_at")
            object.__setattr__(self, "expires_at", expires_at)

    def allows(self, scope: str, *, now: float | None = None) -> bool:
        if not isinstance(scope, str) or not scope.strip():
            return False
        if now is not None:
            now = _finite_number(now, "now")
        scope = scope.strip()
        if not (
            scope in self.scopes
            or "*" in self.scopes
            or any(
                grant.endswith(":*") and scope.startswith(grant[:-1])
                for grant in self.scopes
            )
        ):
            return False
        return self.expires_at is None or (time.time() if now is None else now) < self.expires_at


class WorkspacePolicyStore:
    """Small durable identity/delegation registry for a shared workspace.

    The store is deliberately policy data, not execution authority. Approval
    resolution still happens through the canonical Interrupt/Run APIs; this
    registry only records who may request that resolution and leaves an
    append-only audit trail for operator review.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS lipas_workspace_policy_meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lipas_workspace_identities (
        identity_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
        roles_json TEXT NOT NULL, scopes_json TEXT NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lipas_workspace_delegations (
        delegation_id TEXT PRIMARY KEY, grantor_id TEXT NOT NULL,
        delegate_id TEXT NOT NULL, scopes_json TEXT NOT NULL,
        expires_at REAL, revoked_at REAL, created_at REAL NOT NULL,
        FOREIGN KEY(grantor_id) REFERENCES lipas_workspace_identities(identity_id),
        FOREIGN KEY(delegate_id) REFERENCES lipas_workspace_identities(identity_id)
    );
    CREATE TABLE IF NOT EXISTS lipas_workspace_policy_audit (
        event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, actor_id TEXT NOT NULL,
        payload_json TEXT NOT NULL, created_at REAL NOT NULL
    );
    """
    _SCHEMA_VERSION = 1

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = path
        self._conn = connect_sqlite(path, check_same_thread=False, row_factory=sqlite3.Row)
        self._lock = threading.RLock()
        self._closed = False
        try:
            with self._lock, self._conn:
                # Read the stamp before creating the remaining policy tables.
                # A newer workspace must fail closed without being partially
                # modified by this older runtime.  Creating the tiny metadata
                # table is unavoidable for an empty database, but no identity,
                # delegation, or audit table is touched until compatibility is
                # established.
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS lipas_workspace_policy_meta "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                )
                row = self._conn.execute(
                    "SELECT value FROM lipas_workspace_policy_meta "
                    "WHERE key='schema_version'",
                ).fetchone()
                if row is not None:
                    try:
                        version = int(row[0])
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise RuntimeError(
                            "workspace policy schema version is not an int",
                        ) from exc
                    if version > self._SCHEMA_VERSION:
                        raise RuntimeError(
                            "workspace policy schema version mismatch: "
                            f"database={version}, runtime={self._SCHEMA_VERSION}",
                        )
                else:
                    version = self._SCHEMA_VERSION
                self._conn.executescript(self._SCHEMA)
                self._conn.execute(
                    "INSERT OR IGNORE INTO lipas_workspace_policy_meta(key,value) "
                    "VALUES('schema_version',?)",
                    (str(self._SCHEMA_VERSION),),
                )
                if version != self._SCHEMA_VERSION:
                    raise RuntimeError(
                        "workspace policy schema version mismatch: "
                        f"database={version}, runtime={self._SCHEMA_VERSION}",
                    )
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    def put_identity(self, identity: WorkspaceIdentity, *, actor_id: str = "system") -> WorkspaceIdentity:
        self._ensure_open()
        if not isinstance(identity, WorkspaceIdentity):
            raise TypeError("identity must be WorkspaceIdentity")
        actor_id = _policy_text(actor_id, "actor_id")
        now = time.time()
        with self._lock, immediate_transaction(self._conn):
            row = self._conn.execute(
                "SELECT identity_id,display_name,roles_json,scopes_json FROM lipas_workspace_identities WHERE identity_id=?",
                (identity.identity_id,),
            ).fetchone()
            if row is not None:
                existing = self._identity_row(identity.identity_id, row)
                if existing != identity:
                    raise ValueError(f"identity {identity.identity_id!r} already exists with different data")
                return existing
            try:
                self._conn.execute(
                    "INSERT INTO lipas_workspace_identities(identity_id,display_name,roles_json,scopes_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (identity.identity_id, identity.display_name, _json(sorted(identity.roles)), _json(sorted(identity.scopes)), now, now),
                )
            except sqlite3.IntegrityError:
                # A peer store may have committed the same identity after our
                # initial SELECT.  Re-read and apply the idempotency check
                # instead of leaking a backend UNIQUE error.
                existing_row = self._conn.execute(
                    "SELECT identity_id,display_name,roles_json,scopes_json "
                    "FROM lipas_workspace_identities WHERE identity_id=?",
                    (identity.identity_id,),
                ).fetchone()
                if existing_row is None:
                    raise
                existing = self._identity_row(identity.identity_id, existing_row)
                if existing != identity:
                    raise ValueError(
                        f"identity {identity.identity_id!r} already exists with different data",
                    ) from None
                return existing
            self._audit_locked(f"identity:{identity.identity_id}:created", "identity_created", actor_id, {"identity_id": identity.identity_id}, now)
        return identity

    def get_identity(self, identity_id: str) -> WorkspaceIdentity | None:
        self._ensure_open()
        identity_id = _policy_text(identity_id, "identity_id")
        with self._lock:
            row = self._conn.execute(
                "SELECT identity_id,display_name,roles_json,scopes_json FROM lipas_workspace_identities WHERE identity_id=?",
                (identity_id,),
            ).fetchone()
        return None if row is None else self._identity_row(identity_id, row)

    def list_identities(self) -> tuple[WorkspaceIdentity, ...]:
        self._ensure_open()
        with self._lock:
            rows = self._conn.execute(
                "SELECT identity_id,display_name,roles_json,scopes_json FROM lipas_workspace_identities ORDER BY identity_id",
            ).fetchall()
        return tuple(self._identity_row(str(row[0]), row) for row in rows)

    def put_delegation(self, delegation: ApprovalDelegation, *, actor_id: str | None = None) -> ApprovalDelegation:
        self._ensure_open()
        if not isinstance(delegation, ApprovalDelegation):
            raise TypeError("delegation must be ApprovalDelegation")
        actor = (
            delegation.grantor.identity_id
            if actor_id is None else _policy_text(actor_id, "actor_id")
        )
        if actor not in {delegation.grantor.identity_id, "system"}:
            raise PermissionError("only the grantor or system may create a delegation")
        if actor != "system" and not all(
            delegation.grantor.can(scope) for scope in delegation.scopes
        ):
            raise PermissionError("grantor cannot delegate a scope they do not hold")
        self.put_identity(delegation.grantor, actor_id=actor)
        self.put_identity(delegation.delegate, actor_id=actor)
        now = time.time()
        with self._lock, immediate_transaction(self._conn):
            row = self._conn.execute(
                "SELECT grantor_id,delegate_id,scopes_json,expires_at,revoked_at FROM lipas_workspace_delegations WHERE delegation_id=?",
                (delegation.delegation_id,),
            ).fetchone()
            if row is not None:
                if self._delegation_row(delegation.delegation_id, row) != delegation:
                    raise ValueError(f"delegation {delegation.delegation_id!r} already exists with different data")
                return delegation
            try:
                self._conn.execute(
                    "INSERT INTO lipas_workspace_delegations(delegation_id,grantor_id,delegate_id,scopes_json,expires_at,revoked_at,created_at) VALUES(?,?,?,?,?,?,?)",
                    (delegation.delegation_id, delegation.grantor.identity_id, delegation.delegate.identity_id, _json(sorted(delegation.scopes)), delegation.expires_at, None, now),
                )
            except sqlite3.IntegrityError:
                existing_row = self._conn.execute(
                    "SELECT grantor_id,delegate_id,scopes_json,expires_at,revoked_at "
                    "FROM lipas_workspace_delegations WHERE delegation_id=?",
                    (delegation.delegation_id,),
                ).fetchone()
                if existing_row is None:
                    raise
                existing = self._delegation_row(delegation.delegation_id, existing_row)
                if existing != delegation:
                    raise ValueError(
                        f"delegation {delegation.delegation_id!r} already exists with different data",
                    ) from None
                return existing
            self._audit_locked(f"delegation:{delegation.delegation_id}:created", "delegation_created", actor, {"delegation_id": delegation.delegation_id}, now)
        return delegation

    def revoke_delegation(self, delegation_id: str, *, actor_id: str) -> None:
        self._ensure_open()
        delegation_id = _policy_text(delegation_id, "delegation_id")
        actor_id = _policy_text(actor_id, "actor_id")
        now = time.time()
        with self._lock, immediate_transaction(self._conn):
            row = self._conn.execute(
                "SELECT grantor_id,delegate_id,scopes_json,expires_at,revoked_at "
                "FROM lipas_workspace_delegations WHERE delegation_id=?",
                (delegation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(delegation_id)
            delegation = self._delegation_row(delegation_id, row)
            if actor_id not in {delegation.grantor.identity_id, "system"}:
                raise PermissionError("only the grantor or system may revoke a delegation")
            if row[4] is None:
                self._conn.execute("UPDATE lipas_workspace_delegations SET revoked_at=? WHERE delegation_id=?", (now, delegation_id))
                self._audit_locked(f"delegation:{delegation_id}:revoked", "delegation_revoked", actor_id, {"delegation_id": delegation_id}, now)

    def get_delegation(self, delegation_id: str, *, include_revoked: bool = False) -> ApprovalDelegation | None:
        self._ensure_open()
        delegation_id = _policy_text(delegation_id, "delegation_id")
        with self._lock:
            row = self._conn.execute(
                "SELECT grantor_id,delegate_id,scopes_json,expires_at,revoked_at FROM lipas_workspace_delegations WHERE delegation_id=?",
                (delegation_id,),
            ).fetchone()
        if row is None or (not include_revoked and row[4] is not None):
            return None
        return self._delegation_row(delegation_id, row)

    def audit(self, *, limit: int = 100) -> tuple[Mapping[str, Any], ...]:
        self._ensure_open()
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > _MAX_SQLITE_INT
        ):
            raise ValueError("limit must be a positive int")
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id,kind,actor_id,payload_json,created_at FROM lipas_workspace_policy_audit ORDER BY created_at,event_id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple({"event_id": row[0], "kind": row[1], "actor_id": row[2], "payload": _loads_strict(row[3]), "created_at": row[4]} for row in rows)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> "WorkspacePolicyStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _identity_row(self, identity_id: str, row: sqlite3.Row) -> WorkspaceIdentity:
        roles = _loads_strict(row[2])
        scopes = _loads_strict(row[3])
        if not isinstance(roles, list) or not isinstance(scopes, list):
            raise RuntimeError("persisted workspace identity sets must be arrays")
        return WorkspaceIdentity(identity_id, str(row[1]), frozenset(roles), frozenset(scopes))

    def _delegation_row(self, delegation_id: str, row: sqlite3.Row) -> ApprovalDelegation:
        grantor = self.get_identity(str(row[0]))
        delegate = self.get_identity(str(row[1]))
        if grantor is None or delegate is None:
            raise RuntimeError(f"delegation {delegation_id!r} references missing identity")
        scopes = _loads_strict(row[2])
        if not isinstance(scopes, list):
            raise RuntimeError("persisted delegation scopes must be an array")
        return ApprovalDelegation(delegation_id, grantor, delegate, frozenset(scopes), row[3])

    def _audit_locked(self, event_id: str, kind: str, actor_id: str, payload: Mapping[str, Any], now: float) -> None:
        payload_json = _json(dict(payload))
        existing = self._conn.execute(
            "SELECT kind,actor_id,payload_json FROM lipas_workspace_policy_audit "
            "WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            if (str(existing[0]), str(existing[1]), str(existing[2])) != (
                kind, actor_id, payload_json,
            ):
                raise ValueError(f"policy audit event {event_id!r} was reused with different data")
            return
        self._conn.execute(
            "INSERT INTO lipas_workspace_policy_audit(event_id,kind,actor_id,payload_json,created_at) VALUES(?,?,?,?,?)",
            (event_id, kind, actor_id, payload_json, now),
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("WorkspacePolicyStore is closed")


def _policy_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _json(value: Any) -> str:
    try:
        _validate_json_shape(value)
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("policy payload must be strict JSON") from exc


def _loads_strict(value: Any) -> Any:
    if not isinstance(value, str):
        raise RuntimeError("persisted policy payload must be strict JSON")
    try:
        parsed = json.loads(
            value,
            parse_constant=lambda raw: (_ for _ in ()).throw(
                ValueError(f"non-JSON numeric constant {raw!r}")
            ),
        )
        _validate_json_shape(parsed)
        return parsed
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("persisted policy payload must be strict JSON") from exc


def _validate_json_shape(value: Any, *, _active: set[int] | None = None) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("policy payload must contain finite numbers")
        return
    if not isinstance(value, (list, tuple, Mapping)):
        raise TypeError("policy payload contains unsupported values")
    active = set() if _active is None else _active
    marker = id(value)
    if marker in active:
        raise ValueError("policy payload must not contain reference cycles")
    active.add(marker)
    try:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("policy payload object keys must be strings")
                _validate_json_shape(item, _active=active)
        else:
            for item in value:
                _validate_json_shape(item, _active=active)
    finally:
        active.remove(marker)


@dataclass(frozen=True, slots=True)
class SharedBudgetPolicy:
    """A durable reservation policy shared by coordination handoffs.

    ``limits`` are hard upper bounds. By default one ``handoffs`` unit is
    reserved per new envelope when that bucket is configured. Applications can
    supply ``estimator`` to reserve tokens, cost, or tool-specific resources
    from the immutable envelope and member contract.
    """

    limits: Mapping[str, float]
    scope: str = "default"
    estimator: BudgetEstimator | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.limits, Mapping) or not self.limits:
            raise ValueError("SharedBudgetPolicy.limits must be non-empty")
        normalized: dict[str, float] = {}
        for bucket, limit in self.limits.items():
            if (
                not isinstance(bucket, str)
                or not bucket.strip()
                or bucket != bucket.strip()
            ):
                raise ValueError("budget bucket names must be trimmed strings")
            try:
                valid_limit = (
                    not isinstance(limit, bool)
                    and isinstance(limit, (int, float))
                    and math.isfinite(float(limit))
                    and limit >= 0
                )
            except (OverflowError, TypeError, ValueError):
                valid_limit = False
            if not valid_limit:
                raise ValueError(
                    f"budget {bucket!r} must be finite and non-negative",
                )
            normalized[bucket] = float(limit)
        if not isinstance(self.scope, str) or not self.scope.strip():
            raise ValueError("SharedBudgetPolicy.scope must be non-empty")
        if self.estimator is not None and not callable(self.estimator):
            raise TypeError("SharedBudgetPolicy.estimator must be callable or None")
        object.__setattr__(self, "limits", MappingProxyType(normalized))
        object.__setattr__(self, "scope", self.scope.strip())

    def estimate(self, envelope: Any, member: Any) -> dict[str, float]:
        """Return a validated reservation for one not-yet-admitted envelope."""
        raw = (
            {"handoffs": 1.0}
            if self.estimator is None and "handoffs" in self.limits
            else ({} if self.estimator is None else self.estimator(envelope, member))
        )
        if not isinstance(raw, Mapping):
            raise TypeError("budget estimator must return a mapping")
        estimate: dict[str, float] = {}
        for bucket, amount in raw.items():
            if not isinstance(bucket, str) or not bucket.strip():
                raise ValueError("budget estimator bucket names must be non-empty strings")
            normalized_bucket = bucket.strip()
            if normalized_bucket != bucket:
                raise ValueError("budget estimator bucket names must be trimmed strings")
            if normalized_bucket not in self.limits:
                raise ValueError(
                    f"budget estimator returned undeclared bucket {bucket!r}",
                )
            try:
                valid_amount = (
                    not isinstance(amount, bool)
                    and isinstance(amount, (int, float))
                    and math.isfinite(float(amount))
                    and amount >= 0
                )
            except (OverflowError, TypeError, ValueError):
                valid_amount = False
            if not valid_amount:
                raise ValueError(
                    f"budget estimate for {bucket!r} must be finite and non-negative",
                )
            if amount:
                estimate[normalized_bucket] = float(amount)
        return estimate


@dataclass(frozen=True, slots=True)
class CapabilityPolicy:
    """Allowlist declared member capabilities before a handoff is claimed."""

    grants: Mapping[str, Iterable[str]]
    default: Iterable[str] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.grants, Mapping):
            raise TypeError("CapabilityPolicy.grants must be a mapping")
        normalized: dict[str, frozenset[str]] = {}
        for member, capabilities in self.grants.items():
            if (
                not isinstance(member, str)
                or not member.strip()
                or member != member.strip()
            ):
                raise ValueError(
                    "capability grant member names must be trimmed non-empty strings",
                )
            normalized[member] = _capability_set(capabilities)
        object.__setattr__(self, "grants", MappingProxyType(normalized))
        object.__setattr__(self, "default", _capability_set(self.default))

    def allowed_for(self, member: str) -> frozenset[str]:
        if not isinstance(member, str) or not member.strip():
            raise ValueError("member must be a non-empty string")
        member = member.strip()
        return frozenset(self.grants.get(member, self.grants.get("*", self.default)))

    def missing(self, member: str, required: Iterable[str]) -> frozenset[str]:
        return _capability_set(required) - self.allowed_for(member)


def _capability_set(values: Iterable[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise TypeError("capabilities must be an iterable of strings, not a string")
    try:
        raw = tuple(values)
    except TypeError as exc:
        raise TypeError("capabilities must be an iterable of strings") from exc
    if any(
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        for value in raw
    ):
        raise ValueError(
            "capabilities must contain trimmed non-empty strings",
        )
    normalized = frozenset(raw)
    if len(normalized) != len(raw):
        raise ValueError("capabilities contain duplicate values")
    return normalized


def _finite_number(value: Any, name: str) -> float:
    """Convert a numeric policy value without leaking conversion errors."""
    try:
        valid = (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
        )
    except (OverflowError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError(f"{name} must be finite")
    return float(value)
