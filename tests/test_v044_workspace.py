"""0.44 shared workspace identity, delegation, and audit contracts."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lipas import ApprovalDelegation, WorkspaceIdentity, WorkspacePolicyStore


def test_workspace_policy_rejects_unauthorized_delegation_and_closed_access(
    tmp_path: Path,
):
    owner = WorkspaceIdentity("owner", "Owner", scopes=frozenset({"approve:*"}))
    delegate = WorkspaceIdentity("delegate", "Delegate")
    grant = ApprovalDelegation(
        "grant", owner, delegate, frozenset({"approve:email"}), expires_at=9_999_999_999,
    )
    with WorkspacePolicyStore(tmp_path / "policy.db") as store:
        with pytest.raises(PermissionError):
            store.put_delegation(grant, actor_id="delegate")
        assert store.put_delegation(grant, actor_id="owner") == grant
    with pytest.raises(RuntimeError, match="closed"):
        store.get_identity("owner")


def test_workspace_policy_schema_future_version_fails_closed(tmp_path: Path):
    path = tmp_path / "policy.db"
    with WorkspacePolicyStore(path):
        pass
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE lipas_workspace_policy_meta SET value='999' WHERE key='schema_version'",
    )
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="schema version mismatch"):
        WorkspacePolicyStore(path)


def test_workspace_policy_enforces_scope_and_revocation_authority(tmp_path: Path):
    owner = WorkspaceIdentity("owner", "Owner", scopes=frozenset({"approve:*"}))
    delegate = WorkspaceIdentity("delegate", "Delegate")
    grant = ApprovalDelegation("grant", owner, delegate, frozenset({"approve:email"}))
    with WorkspacePolicyStore(tmp_path / "policy.db") as store:
        with pytest.raises(PermissionError, match="scope"):
            store.put_delegation(
                ApprovalDelegation(
                    "bad", WorkspaceIdentity("limited", "Limited"), delegate,
                    frozenset({"approve:email"}),
                ),
            )
        store.put_delegation(grant)
        with pytest.raises(PermissionError, match="revoke"):
            store.revoke_delegation(grant.delegation_id, actor_id=delegate.identity_id)
        store.revoke_delegation(grant.delegation_id, actor_id=owner.identity_id)
        assert store.get_delegation(grant.delegation_id) is None
        assert store.get_delegation(grant.delegation_id, include_revoked=True) is not None
