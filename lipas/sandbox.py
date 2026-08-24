"""OS command isolation backends for the first-party workbench."""
from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence

__all__ = [
    "BubblewrapSandbox",
    "CommandResult",
    "CommandSandbox",
    "LocalCommandSandbox",
    "SandboxUnavailable",
    "sandbox_from_name",
]


class SandboxUnavailable(RuntimeError):
    """A safe OS isolation backend cannot be established."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    sandbox: str
    isolated: bool
    network_isolated: bool


class CommandSandbox(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def isolated(self) -> bool: ...

    @property
    def network_isolated(self) -> bool: ...

    async def run(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        environment: Mapping[str, str],
        timeout_s: float,
    ) -> CommandResult: ...


@dataclass(frozen=True, slots=True)
class LocalCommandSandbox:
    """Explicitly unsafe compatibility backend with no OS containment."""

    name: str = "local"
    isolated: bool = False
    network_isolated: bool = False

    async def run(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        environment: Mapping[str, str],
        timeout_s: float,
    ) -> CommandResult:
        return await _run_process(
            argv,
            cwd=workspace,
            environment=environment,
            timeout_s=timeout_s,
            sandbox=self.name,
            isolated=self.isolated,
            network_isolated=self.network_isolated,
        )


@dataclass(frozen=True, slots=True)
class BubblewrapSandbox:
    """Linux Bubblewrap backend with a minimal filesystem and no network."""

    executable: str = "bwrap"
    name: str = "bubblewrap"
    isolated: bool = True
    network_isolated: bool = True

    async def run(
        self,
        argv: Sequence[str],
        *,
        workspace: Path,
        environment: Mapping[str, str],
        timeout_s: float,
    ) -> CommandResult:
        resolved = shutil.which(self.executable)
        if resolved is None:
            raise SandboxUnavailable(
                "Bubblewrap is unavailable; install bwrap or explicitly choose "
                "--sandbox local for trusted code",
            )
        command = self.build_command(
            resolved, workspace.resolve(), argv, environment,
        )
        result = await _run_process(
            command,
            cwd=workspace,
            environment={},
            timeout_s=timeout_s,
            sandbox=self.name,
            isolated=self.isolated,
            network_isolated=self.network_isolated,
            reported_argv=argv,
        )
        if result.exit_code not in (0, None) and result.stderr.lstrip().startswith("bwrap:"):
            raise SandboxUnavailable(
                "Bubblewrap could not establish the requested filesystem/network "
                f"isolation: {result.stderr.strip()}",
            )
        return result

    @staticmethod
    def build_command(
        executable: str,
        workspace: Path,
        argv: Sequence[str],
        environment: Mapping[str, str],
    ) -> tuple[str, ...]:
        """Build a no-network, minimal-root Bubblewrap command."""
        if not argv:
            raise ValueError("sandbox argv cannot be empty")
        command: list[str] = [
            executable,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--hostname", "lipas-sandbox",
            "--clearenv",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind-try", "/opt", "/opt",
            "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/sbin", "/sbin",
            "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64",
            "--dir", "/etc",
            "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
            "--ro-bind-try", "/etc/ssl", "/etc/ssl",
            "--dir", "/home",
            "--dir", "/home/lipas",
            "--bind", str(workspace), "/workspace",
            "--chdir", "/workspace",
            "--setenv", "HOME", "/home/lipas",
            "--setenv", "PATH", "/opt/miniconda/bin:/usr/local/bin:/usr/bin:/bin",
        ]
        for key in ("CI", "LANG", "LC_ALL", "TERM"):
            value = environment.get(key)
            if value:
                command.extend(("--setenv", key, value))
        command.extend(("--", *argv))
        return tuple(command)


class _UnavailableSandbox:
    name = "unavailable"
    isolated = True
    network_isolated = True

    async def run(self, *_: object, **__: object) -> CommandResult:
        raise SandboxUnavailable(
            "no supported OS sandbox is installed; install Bubblewrap on Linux "
            "or explicitly choose --sandbox local for trusted code",
        )


def sandbox_from_name(name: str) -> CommandSandbox:
    if name == "local":
        return LocalCommandSandbox()
    if name == "bwrap":
        return BubblewrapSandbox()
    if name == "auto":
        return BubblewrapSandbox() if shutil.which("bwrap") else _UnavailableSandbox()
    raise ValueError("sandbox must be one of: auto, bwrap, local")


async def _run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_s: float,
    sandbox: str,
    isolated: bool,
    network_isolated: bool,
    reported_argv: Sequence[str] | None = None,
) -> CommandResult:
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=dict(environment),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False
    communication = asyncio.create_task(process.communicate())
    try:
        # Shield the pipe reader from wait_for cancellation.  Under transient
        # process-scheduling pressure a tiny child may finish at the timeout
        # boundary; cancelling communicate there loses already-buffered
        # stdout/stderr and turns a successful command into a false timeout.
        stdout, stderr = await asyncio.wait_for(
            asyncio.shield(communication), timeout=timeout_s,
        )
        exit_code = process.returncode
    except TimeoutError:
        timed_out = True
        if process.returncode is None:
            process.kill()
        stdout, stderr = await communication
        # A process that crossed the timeout boundary while already exiting is
        # still reported with its real exit code; callers can inspect
        # ``timed_out`` separately.  If it had to be killed, returncode is the
        # platform's negative signal code and the public exit code remains
        # visibly unknown.
        exit_code = process.returncode if process.returncode == 0 else None
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await communication
        raise
    return CommandResult(
        argv=tuple(reported_argv or argv),
        exit_code=exit_code,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        duration_seconds=time.monotonic() - started,
        sandbox=sandbox,
        isolated=isolated,
        network_isolated=network_isolated,
    )
