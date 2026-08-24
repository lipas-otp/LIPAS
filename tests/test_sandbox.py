"""OS sandbox construction and explicit local fallback contracts."""
from __future__ import annotations

import asyncio

import pytest

from lipas.sandbox import (
    BubblewrapSandbox,
    LocalCommandSandbox,
    SandboxUnavailable,
    sandbox_from_name,
)


def test_bubblewrap_command_has_minimal_root_and_no_network_escape(tmp_path):
    command = BubblewrapSandbox.build_command(
        "/usr/bin/bwrap",
        tmp_path,
        ["pytest", "-q"],
        {"HOME": "/home/operator", "PATH": "/unsafe", "LANG": "C.UTF-8"},
    )
    assert "--unshare-all" in command
    assert "--share-net" not in command
    assert ("--bind", str(tmp_path), "/workspace") == command[
        command.index("--bind"):command.index("--bind") + 3
    ]
    assert "/home/operator" not in command
    assert command[-3:] == ("--", "pytest", "-q")


def test_auto_sandbox_fails_closed_when_bwrap_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("lipas.sandbox.shutil.which", lambda _name: None)
    backend = sandbox_from_name("auto")
    with pytest.raises(SandboxUnavailable):
        asyncio.run(backend.run(
            ["pytest"], workspace=tmp_path, environment={}, timeout_s=1,
        ))


def test_local_sandbox_is_an_explicit_nonisolated_backend(tmp_path):
    result = asyncio.run(LocalCommandSandbox().run(
        ["/usr/bin/printf", "ok"],
        workspace=tmp_path,
        environment={"PATH": "/usr/bin:/bin"},
        timeout_s=2,
    ))
    assert result.stdout == "ok"
    assert result.exit_code == 0
    assert not result.isolated
    assert not result.network_isolated


def test_local_sandbox_preserves_output_when_communication_hits_timeout_boundary(
    tmp_path,
):
    async def run_many():
        return [
            await LocalCommandSandbox().run(
                ["/usr/bin/printf", "ok"],
                workspace=tmp_path,
                environment={"PATH": "/usr/bin:/bin"},
                timeout_s=2,
            )
            for _ in range(12)
        ]

    results = asyncio.run(run_many())
    assert all(result.stdout == "ok" for result in results)
    assert all(result.exit_code == 0 for result in results)
