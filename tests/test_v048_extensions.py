"""0.48 extension provenance, registry persistence, and schema contracts."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lipas import (
    ExtensionManifest,
    ExtensionRegistry,
    ExtensionSigner,
    ExtensionTrustPolicy,
)


def test_signed_extension_registry_survives_restart_and_rejects_tampering(
    tmp_path: Path,
):
    path = tmp_path / "registry.db"
    signer = ExtensionSigner("release", "registry-secret-012345")
    signed = signer.sign(
        ExtensionManifest("demo", provenance="registry:test"),
        artifact=b"artifact",
    )
    policy = ExtensionTrustPolicy(
        allowed_provenance=frozenset({"registry:test"}),
        trusted_signers=frozenset({"release"}),
        require_signature=True,
        signer_secrets={"release": "registry-secret-012345"},
    )
    with ExtensionRegistry(path=path, trust_policy=policy) as registry:
        record = registry.register(
            signed, artifact=b"artifact", scenario_names=set(), skill_names=set(),
        )
        assert record.certified
    with ExtensionRegistry(path=path, trust_policy=policy) as reopened:
        assert reopened.get("demo") == record
        reopened.revoke("demo")
    with ExtensionRegistry(path=path, trust_policy=policy) as reopened:
        assert reopened.get("demo") is None

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE lipas_extension_registry SET certification_json='{}' WHERE name='demo'",
    )
    connection.commit()
    connection.close()
    with pytest.raises(ValueError):
        ExtensionRegistry(path=path, trust_policy=policy)


def test_extension_manifest_future_schema_is_rejected():
    with pytest.raises(ValueError, match="schema version"):
        ExtensionManifest.from_mapping({"schema_version": 99, "name": "demo"})
