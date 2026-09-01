"""Small operational CLI for the LIPAS runtime.

The CLI deliberately owns no agent DSL or alternate execution semantics. It
either generates ordinary Python, calls an explicitly supplied Python factory,
or reads the same durable sessions that library users create.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import inspect
import json
import logging
import os
import secrets
import sys
import tempfile
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ._version import __version__
from .agent import Agent
from .adapter.errors import DEFAULT_POLICY, ErrorKind, RetryPolicy
from .rows.effect import EffectRow
from .session import open_session
from .trace import render_trace, write_jsonl
from .execution import InterruptState, RunState, RunSuspended
from .events import AgentEventType
from .document_tools import DocumentToolError, read_pdf_text
from .dispatcher import DispatchOutcome, TaskDispatcher
from .runtime import LIPASRuntime
from .workbench import TaskReport, Workbench
from .workspace_storage import WorkspaceStorage
from .security import TLSConfig
from .deployment import (
    install_workspace,
    release_check,
    upgrade_workspace,
)
from .sandbox import sandbox_from_name
from .gateway import ActionGateway, result_json
from .performance import run_execution_soak
from .integrations import MCPActionServer, OpenClawActionBackend
from .skills import SkillRegistry, builtin_skills, load_builtin_skill
from .scenarios import (
    ScenarioMode,
    ScenarioRegistry,
    builtin_scenarios,
    load_builtin_scenario,
)
from .tools import Tool, ToolRegistry, tool

__all__ = ["main"]

_DEFAULT_MODEL = "gemma4:12b"
_DEFAULT_MODEL_CHECK_PROMPT = "Reply with exactly: OK"
_DEFAULT_INSTRUCTIONS = (
    "You are the LIPAS assistant running through the local-first LIPAS "
    "runtime. LIPAS is this open-source Python agent platform: it records "
    "conversation and execution evidence, keeps risky actions behind explicit "
    "tools and approval. It is not a traffic or map service and has no "
    "official acronym expansion. Do not "
    "claim that you are Microsoft Phi or invent an expansion for LIPAS. If "
    "asked who you are, identify yourself as an AI assistant powered by the "
    "configured model and hosted by LIPAS. Answer in the user's language, "
    "prefer concise useful replies, and use earlier turns as context. "
    "Context and memory policy is important: every turn in this CLI session "
    "is sent to you as the `messages` list, including earlier user messages, "
    "your earlier replies, and tool results. Treat those messages as the "
    "authoritative current conversation. If the user asks what you remember "
    "from this conversation, inspect the messages and answer directly with "
    "the relevant topic; do not give a generic claim that you have no memory "
    "or cannot access previous turns when they are present. Distinguish this "
    "current-session context (which LIPAS can persist in SQLite and restore) "
    "from unrelated sessions or personal data that were not supplied. If no "
    "earlier messages are present, say that this session has no earlier turns. "
    "Earlier assistant messages may be mistaken; correct them when they "
    "conflict with this runtime policy. "
    "在中文提问时也请遵守这条规则：当前会话中已经提供的历史消息可以 "
    "记住并引用，不要笼统地声称自己完全没有记忆。 Be "
    "familiar with the core vocabulary: an Agent reasons, a Tool is an "
    "explicit capability, a Session remembers conversation, and a Task/Run "
    "records durable work with Effects, Approvals, Artifacts, and Reports. "
    "Be honest about limits: web, weather, shell, and other live data are "
    "unavailable unless an explicit tool is supplied; never imply that you "
    "performed an action without a tool result. If tools are supplied, use "
    "them only when they materially help answer the request."
)

_DEFAULT_CHAT_SESSION = Path.home() / ".lipas" / "runs" / "chat.db"
_DEFAULT_CHAT_SESSION_ID = "cli-chat"
_CHAT_MAX_LIST_FILES = 200
_CHAT_MAX_SEARCH_FILES = 500
_CHAT_MAX_READ_CHARS = 120_000
_CHAT_MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
_CHAT_MAX_PDF_PAGES = 200
_CHAT_SENSITIVE_NAMES = frozenset({
    ".env", ".env.local", ".env.production", "credentials", "secrets",
    "id_rsa", "id_ed25519",
})
_CHAT_SENSITIVE_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})


def _configured_scenarios(args: argparse.Namespace) -> ScenarioRegistry:
    """Select only explicitly requested business recipes."""
    return ScenarioRegistry.from_names(getattr(args, "scenario", ()) or ())


def _configured_skills(args: argparse.Namespace) -> SkillRegistry:
    """Compose only the business knowledge explicitly selected by the user."""
    return _configured_scenarios(args).skill_registry(
        builtin_names=getattr(args, "skill", ()) or (),
        paths=getattr(args, "skill_path", ()) or (),
    )


def _prompt_session() -> Any | None:
    """Return a full-screen-safe line editor when the optional extra exists."""
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        history_path = Path.home() / ".lipas" / "history"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        return PromptSession(history=FileHistory(str(history_path)))
    except (ImportError, OSError):
        # CPython's readline fallback still restores left/right arrows, word
        # movement, and in-process history on normal Unix terminals.
        with contextlib.suppress(ImportError):
            import readline  # noqa: F401
        return None


def _chat_runtime_tool(
    *,
    workspace: Path | None,
    session_path: str | Path | None,
    session_id: str,
) -> Tool:
    """Expose factual runtime state without granting filesystem authority.

    The model should not have to infer the process directory, memory scope,
    or tool boundary from prose.  This read-only tool gives it one auditable
    source of truth for questions such as ``pwd`` and ``what do you remember``.
    """
    cwd = Path.cwd().resolve()
    workspace_value = None if workspace is None else str(workspace)
    if session_path is None:
        memory = {"mode": "ephemeral", "path": None}
    else:
        memory = {"mode": "sqlite", "path": str(session_path)}

    @tool(side_effect="read_only")
    def get_runtime_info() -> dict[str, Any]:
        """Return the current directory, memory scope, and available authority."""
        return {
            "current_working_directory": str(cwd),
            "selected_workspace": workspace_value,
            "session_id": session_id,
            "memory": dict(memory),
            "capabilities": {
                "runtime_info": True,
                "filesystem": "read_only" if workspace is not None else "none",
                "write": False,
                "shell": False,
                "network": False,
            },
        }

    return get_runtime_info


def _chat_workspace_tools(root: str | Path) -> ToolRegistry:
    """Build bounded, read-only tools for one explicitly selected directory.

    The interactive chat intentionally does not expose a shell or write
    capability.  This small bundle is enough for a model to inspect a project
    when the user opts in with ``--workspace``; mutations remain in the
    first-party Task Workbench where staging and approval are enforced.
    """
    workspace = Path(root).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"chat workspace is not a directory: {workspace}")

    def _resolve(relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ValueError("relative_path must be a non-empty string")
        candidate = (workspace / relative_path).resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("path escapes the selected chat workspace") from exc
        return candidate

    def _sensitive(path: Path) -> bool:
        name = path.name.lower()
        return (
            name in _CHAT_SENSITIVE_NAMES
            or name.startswith(".env.")
            or path.suffix.lower() in _CHAT_SENSITIVE_SUFFIXES
        )

    @tool(side_effect="read_only")
    def list_workspace_files(relative_path: str = ".") -> list[str]:
        """List up to 200 files below a workspace-relative directory."""
        base = _resolve(relative_path)
        if not base.is_dir():
            raise ValueError(f"not a directory: {relative_path}")
        files: list[str] = []
        for path in sorted(base.rglob("*")):
            if ".git" in path.relative_to(workspace).parts:
                continue
            if path.is_symlink():
                continue
            if _sensitive(path):
                continue
            if path.is_file():
                files.append(path.relative_to(workspace).as_posix())
                if len(files) >= _CHAT_MAX_LIST_FILES:
                    break
        return files

    @tool(side_effect="read_only")
    def read_workspace_file(
        relative_path: str,
        max_chars: int = _CHAT_MAX_READ_CHARS,
    ) -> str:
        """Read a UTF-8 text file inside the selected chat workspace."""
        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or not 1 <= max_chars <= _CHAT_MAX_READ_CHARS
        ):
            raise ValueError(
                f"max_chars must be an integer between 1 and {_CHAT_MAX_READ_CHARS}"
            )
        path = _resolve(relative_path)
        if _sensitive(path):
            raise ValueError("sensitive files are not readable by chat tools")
        if not path.is_file():
            raise ValueError(f"not a file: {relative_path}")
        try:
            return path.read_text(encoding="utf-8")[:max_chars]
        except UnicodeDecodeError as exc:
            raise ValueError("binary/non-UTF-8 files are not readable") from exc

    @tool(side_effect="read_only")
    def search_workspace(
        query: str,
        relative_path: str = ".",
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        """Find a literal string in bounded UTF-8 workspace files."""
        if not isinstance(query, str) or not query:
            raise ValueError("query must be a non-empty string")
        if (
            isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or not 1 <= max_results <= 500
        ):
            raise ValueError("max_results must be between 1 and 500")
        base = _resolve(relative_path)
        if not base.is_dir():
            raise ValueError(f"not a directory: {relative_path}")
        matches: list[dict[str, Any]] = []
        files_seen = 0
        for path in sorted(base.rglob("*")):
            if len(matches) >= max_results:
                break
            if ".git" in path.relative_to(workspace).parts or path.is_symlink():
                continue
            if _sensitive(path) or not path.is_file():
                continue
            files_seen += 1
            if files_seen > _CHAT_MAX_SEARCH_FILES:
                break
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if query not in line:
                    continue
                matches.append({
                    "path": path.relative_to(workspace).as_posix(),
                    "line": line_number,
                    "text": line[:500],
                })
                if len(matches) >= max_results:
                    break
        return matches

    @tool(side_effect="read_only")
    def read_pdf(
        relative_path: str,
        max_pages: int = _CHAT_MAX_PDF_PAGES,
        max_chars: int = _CHAT_MAX_READ_CHARS,
    ) -> dict[str, Any]:
        """Extract bounded text from an unencrypted workspace PDF."""
        path = _resolve(relative_path)
        if _sensitive(path):
            raise ValueError("sensitive files are not readable by chat tools")
        if not path.is_file():
            raise ValueError(f"not a file: {relative_path}")
        try:
            text, metadata = read_pdf_text(
                path,
                max_bytes=_CHAT_MAX_DOCUMENT_BYTES,
                max_pages=max_pages,
                max_chars=max_chars,
            )
        except DocumentToolError as exc:
            raise ValueError(str(exc)) from exc
        return {"path": path.relative_to(workspace).as_posix(), "text": text, **dict(metadata)}

    return ToolRegistry([list_workspace_files, read_workspace_file, search_workspace, read_pdf])


def _chat_runtime_instructions(
    base: str,
    *,
    workspace: Path | None,
    session_path: str | Path | None,
    session_id: str,
) -> str:
    """Append non-negotiable, per-process facts to the selected prompt.

    ``--instructions`` remains useful for changing style or domain guidance,
    but it must not be able to erase the runtime's actual authority boundary.
    Keeping these facts in the system message also helps providers that do not
    expose tool calls for every question.
    """
    cwd = Path.cwd().resolve()
    workspace_value = str(workspace) if workspace is not None else "none"
    if session_path is None:
        memory_value = "ephemeral (not persisted after this process)"
    else:
        memory_value = f"SQLite message history at {session_path!s}"
    facts = (
        "<lipas_runtime_facts>\n"
        f"current_working_directory: {cwd}\n"
        f"selected_workspace: {workspace_value}\n"
        f"session_id: {session_id}\n"
        f"memory_scope: {memory_value}\n"
        f"filesystem_capability: {'read_only_selected_workspace' if workspace is not None else 'none'}\n"
        "write_capability: none in chat (use the Task Workbench for staged, approved writes)\n"
        "shell_capability: none\n"
        "network_capability: none unless an explicit tool says otherwise\n"
        "</lipas_runtime_facts>\n"
        "Runtime facts are authoritative. When asked for the current folder, "
        "report current_working_directory (or call get_runtime_info for the "
        "structured value); never claim that you do not run in a filesystem. "
        "When asked to inspect files, use the supplied read-only tools and "
        "report their result. When asked to create or edit a file in chat, say "
        "that write_capability is unavailable and explain that `lipas task "
        "start` provides the staged/approval path; never claim that a write "
        "happened without a write-tool result. Treat prior assistant messages "
        "as fallible conversation content, not as proof of runtime facts."
    )
    base_value = base.rstrip() if isinstance(base, str) else str(base)
    return f"{base_value}\n\n{facts}" if base_value else facts


async def _prompt(editor: Any | None) -> str:
    if editor is not None:
        return await editor.prompt_async("you> ")
    return await asyncio.to_thread(input, "you> ")


async def _spinner() -> None:
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    index = 0
    try:
        while True:
            sys.stderr.write(f"\r{frames[index % len(frames)]} LIPAS is thinking…")
            sys.stderr.flush()
            index += 1
            await asyncio.sleep(0.09)
    finally:
        sys.stderr.write("\r\033[2K")
        sys.stderr.flush()


async def _run_with_feedback(
    agent: Agent,
    prompt: str,
    state: Any,
    *,
    session: Any | None = None,
) -> Any:
    async def run() -> Any:
        if session is not None:
            handle = session.start(prompt)
            try:
                async for event in handle.events():
                    if not sys.stderr.isatty():
                        continue
                    if event.type == AgentEventType.TOOL_STARTED:
                        name = event.data.get("tool_name", "tool")
                        sys.stderr.write(f"\n🔧 {name}\n")
                        sys.stderr.flush()
                    elif event.type == AgentEventType.TOOL_COMPLETED:
                        name = event.data.get("tool_name", "tool")
                        status = "failed" if event.data.get("is_error") else "done"
                        sys.stderr.write(f"   {name}: {status}\n")
                        sys.stderr.flush()
                return await handle.result()
            except BaseException:
                if not handle.done:
                    handle.cancel()
                    await asyncio.shield(
                        asyncio.gather(handle._task, return_exceptions=True)
                    )
                else:
                    # ``events()`` raises the producer exception via its
                    # failure sentinel; retrieve the result too so asyncio
                    # does not report an unhandled Future exception.
                    with contextlib.suppress(BaseException):
                        await handle.result()
                raise
        return await agent.run(prompt, state=state)

    if not sys.stderr.isatty():
        return await run()
    spinner = asyncio.create_task(_spinner())
    try:
        return await run()
    finally:
        spinner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await spinner


def _friendly_error(agent: Agent, error: Any) -> str:
    """Explain local transport failures without implying internet access."""
    if not isinstance(error, dict):
        return f"error: {error!r}"
    # Ollama/httpx is an optional extra. Identify the adapter without importing
    # that extra so a core-only custom factory can still report its own errors.
    is_ollama = any(
        value.__module__ == "lipas.adapter.ollama"
        and value.__name__ == "OllamaAdapter"
        for value in type(agent.adapter).__mro__
    )
    if error.get("type") == "network_error" and is_ollama:
        host = str(getattr(agent.adapter, "host", "localhost:11434"))
        timeout_s = float(getattr(agent.adapter, "timeout_s", 0.0))
        return (
            f"Local Ollama at {host} did not answer within "
            f"{timeout_s:g}s ({error.get('exception_type', 'transport error')}). "
            "This is a localhost timeout, not an internet request. Check "
            "`ollama ps`, try `ollama run <model> \"hello\"`, or rerun with "
            "a larger `--timeout`, a smaller model, or explicit `--retries 1`."
        )
    if is_ollama and error.get("type") == "http_error":
        status_code = error.get("status_code")
        if status_code == 404:
            model = str(getattr(agent, "model", "the selected model"))
            return (
                f"Ollama does not have model `{model}` yet. Pull it with "
                f"`ollama pull {model}`, then try again. Run `lipas model list` "
                "to see installed models."
            )
    if is_ollama and error.get("type") == "provider_error":
        detail = error.get("provider_error")
        detail = detail if isinstance(detail, dict) else {}
        message = str(detail.get("message", ""))
        if "Unknown scheme for proxy URL" in message:
            return (
                "Ollama could not start because the configured proxy URL is "
                "unsupported. Local Ollama ignores proxy environment variables "
                "by default; for a remote Ollama use `--trust-env` with a "
                "supported HTTP(S)/SOCKS5 proxy."
            )
    return f"error: {error}"


@contextlib.contextmanager
def _chat_transport_logs(verbose: bool):
    """Keep adapter retry warnings from corrupting the interactive spinner."""
    logger = logging.getLogger("lipas.adapter.ollama")
    previous_level = logger.level
    if not verbose:
        logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(previous_level)


def _close(rowset: Any) -> None:
    close = getattr(rowset.store, "close", None)
    if callable(close):
        close()


def _require_session(path: str) -> Path:
    session = Path(path)
    if not session.is_file():
        raise ValueError(f"session does not exist: {session}")
    return session


def _effect_row(rowset: Any) -> EffectRow:
    row = next((row for row in rowset.rows if isinstance(row, EffectRow)), None)
    if row is None:
        raise ValueError("session has no EffectRow projection")
    return row


def _cmd_trace(args: argparse.Namespace) -> int:
    rowset = open_session(str(_require_session(args.session)))
    try:
        if args.jsonl:
            write_jsonl(rowset.store, sys.stdout)
        else:
            print(render_trace(rowset.store))
    finally:
        _close(rowset)
    return 0


def _cmd_effects(args: argparse.Namespace) -> int:
    rowset = open_session(str(_require_session(args.session)))
    try:
        view = _effect_row(rowset).project(rowset.store)
        print("effect_id\tkind\tstatus\tcaused_by\tcompensates")
        for effect_id, node in sorted(view.nodes.items()):
            terminal = node.result or node.rejection
            status = "orphan" if terminal is None else str(
                terminal.fields.get("status", "rejected" if node.rejection else "ok"),
            )
            fields = node.intent.fields
            print("\t".join((
                effect_id,
                node.kind.value,
                status,
                str(fields.get("caused_by", "")),
                str(node.compensates or ""),
            )))
        if view.orphans:
            print(f"\nwarning: {len(view.orphans)} interrupted/orphan effect(s)", file=sys.stderr)
    finally:
        _close(rowset)
    return 0


def _factory(
    spec: str,
    *,
    skills: SkillRegistry | None = None,
    scenarios: ScenarioRegistry | None = None,
) -> Agent:
    factory = _factory_callable(spec)
    parameters = inspect.signature(factory).parameters
    accepts_kwargs = any(
        value.kind is inspect.Parameter.VAR_KEYWORD
        for value in parameters.values()
    )
    kwargs: dict[str, Any] = {}
    if skills is not None and skills.skills:
        if "skills" not in parameters and not accepts_kwargs:
            raise ValueError(
                "selected --scenario/--skill/--skill-path values require a chat "
                "factory that accepts skills= or **kwargs",
            )
        kwargs["skills"] = skills
    if scenarios is not None and scenarios.scenarios and (
        "scenarios" in parameters or accepts_kwargs
    ):
        kwargs["scenarios"] = scenarios
    agent = factory(**kwargs)
    if not isinstance(agent, Agent):
        raise TypeError(f"factory {spec!r} returned {type(agent).__name__}, expected Agent")
    if scenarios is not None and scenarios.scenarios:
        scenarios.require_compatible(agent.tool_harness.tools)
    return agent


def _factory_callable(spec: str) -> Callable[..., Any]:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("factory must use module:callable, e.g. agent:build_agent")
    value = getattr(_import_local_module(module_name), attribute)
    if not callable(value):
        raise TypeError(f"factory {spec!r} is not callable")
    return value


def _import_local_module(module_name: str) -> Any:
    """Import a user factory with the CLI's working directory visible.

    Installed console scripts start with their ``bin`` directory at
    ``sys.path[0]`` rather than the directory where the operator invoked them.
    LIPAS explicitly documents local ``agent:build_agent`` factories, so make
    that directory importable only for the duration of this explicit import.
    """
    working_directory = str(Path.cwd())
    sys.path.insert(0, working_directory)
    importlib.invalidate_caches()
    try:
        return importlib.import_module(module_name)
    finally:
        if sys.path and sys.path[0] == working_directory:
            del sys.path[0]
        else:  # pragma: no cover - defensive against factory import mutation
            with contextlib.suppress(ValueError):
                sys.path.remove(working_directory)


def _tool_factory(spec: str) -> ToolRegistry:
    value = _factory_callable(spec)()
    if isinstance(value, ToolRegistry):
        return value
    if isinstance(value, Tool):
        return ToolRegistry([value])
    try:
        return ToolRegistry(value)
    except TypeError as exc:
        raise TypeError(
            f"tool factory {spec!r} must return Tool, ToolRegistry, or iterable of Tool",
        ) from exc


def _action_gateway(args: argparse.Namespace) -> ActionGateway:
    return ActionGateway(
        _tool_factory(args.factory),
        session=args.session,
        allow_writes=getattr(args, "allow_writes", False),
        default_timeout_s=getattr(args, "timeout", 300.0),
    )


def _cmd_action_call(args: argparse.Namespace) -> int:
    arguments = json.loads(args.arguments)
    if not isinstance(arguments, dict):
        raise ValueError("--arguments must decode to a JSON object")
    with _action_gateway(args) as gateway:
        result = gateway.call_sync(
            args.tool,
            arguments,
            request_id=args.request_id,
            approved=args.approved,
        )
    print(result_json(result))
    return 0 if not result.is_error else 1


def _cmd_action_openclaw(args: argparse.Namespace) -> int:
    raw = args.payload if args.payload is not None else sys.stdin.read()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("OpenClaw payload must be a JSON object")
    with _action_gateway(args) as gateway:
        backend = OpenClawActionBackend(
            gateway, trust_caller_approval=args.trust_caller_approval,
        )
        result = asyncio.run(backend.execute(payload))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


def _cmd_action_manifest(args: argparse.Namespace) -> int:
    with _action_gateway(args) as gateway:
        backend = OpenClawActionBackend(gateway)
        print(json.dumps(backend.tool_manifest(), indent=2, ensure_ascii=False))
    return 0


def _cmd_mcp_serve(args: argparse.Namespace) -> int:
    gateway = _action_gateway(args)
    try:
        asyncio.run(MCPActionServer(gateway).serve_stdio())
    finally:
        gateway.close()
    return 0


_WORKBENCH_INSTRUCTIONS = """You are operating one explicitly selected local workspace.
Inspect before editing. Use only the supplied workspace tools. Call
get_workspace_info when an exact workspace path or authority fact is needed. Keep
changes scoped to the user's goal. File writes stay in a staging workspace; commands
require approval. The user applies or discards the complete ChangeSet after your Run.
For documents, use read_pdf for bounded extraction and convert_workspace_file for
explicit source/destination conversions; preserve the source and verify the output.
After editing, run the most relevant available verification command. In the final
answer state what was staged, what was verified, and any remaining uncertainty. Never
claim delivery to the original workspace or verification that a tool result does not
show."""


def _workbench_home(value: str | None) -> Path:
    configured = value or os.environ.get("LIPAS_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".lipas"


@contextlib.contextmanager
def _runtime_workbench(
    home: str | Path,
    *,
    sandbox: str = "auto",
):
    """Give CLI commands the product view owned by the composition root."""
    with LIPASRuntime.open(home, sandbox=sandbox) as runtime:
        yield runtime.workbench


def _storage_verification_payload(storage: WorkspaceStorage) -> dict[str, Any]:
    """Return storage-only health used by migration verification and Doctor."""
    status = storage.inspect()
    issues = list(storage.audit()) if status.current else list(status.issues)
    return {
        "version": __version__,
        "storage": status.as_dict(),
        "issues": [issue.as_dict() for issue in issues],
        "initialized": status.current,
        "healthy": status.state in {"current", "uninitialized"} and not any(
            issue.severity == "error" for issue in issues
        ),
    }


def _sandbox_diagnostics() -> dict[str, Any]:
    """Probe the default sandbox instead of inferring support from PATH."""
    sandbox = sandbox_from_name("auto")
    discovered = sandbox.name != "unavailable"
    operational = False
    error: str | None = None
    if discovered:
        try:
            with tempfile.TemporaryDirectory(prefix="lipas-sandbox-probe-") as root:
                result = asyncio.run(sandbox.run(
                    ("/usr/bin/true",),
                    workspace=Path(root),
                    environment={},
                    timeout_s=2.0,
                ))
            operational = result.exit_code == 0 and not result.timed_out
            if not operational:
                error = (
                    "sandbox probe timed out"
                    if result.timed_out
                    else f"sandbox probe exited with {result.exit_code}"
                )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
    else:
        error = "no supported OS sandbox was discovered"
    return {
        "name": sandbox.name,
        "discovered": discovered,
        "operational": operational,
        "isolated": sandbox.isolated,
        "network_isolated": sandbox.network_isolated,
        "error": error,
    }


def _doctor_payload(storage: WorkspaceStorage) -> dict[str, Any]:
    payload = _storage_verification_payload(storage)
    storage_healthy = bool(payload.pop("healthy"))
    payload["storage_healthy"] = storage_healthy
    payload["sandbox"] = _sandbox_diagnostics()
    maintenance_active = any(
        issue["code"] in {
            "active_migration_lock", "migration_lock_initializing",
        }
        for issue in payload["issues"]
    )
    payload["ready"] = (
        storage_healthy
        and payload["sandbox"]["operational"]
        and not maintenance_active
    )
    payload["healthy"] = payload["ready"]
    return payload


def _cmd_doctor(args: argparse.Namespace) -> int:
    storage = WorkspaceStorage(_workbench_home(args.home))
    payload = _doctor_payload(storage)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        state = payload["storage"]["state"]
        print(f"LIPAS {payload['version']}")
        print(f"workspace: {payload['storage']['home']}")
        print(f"storage: {state}")
        print(
            "sandbox: "
            f"{payload['sandbox']['name']} "
            f"({'operational' if payload['sandbox']['operational'] else 'not operational'})"
        )
        if payload["sandbox"]["error"]:
            print(f"sandbox detail: {payload['sandbox']['error']}")
        for issue in payload["issues"]:
            print(f"{issue['severity']}: {issue['code']}: {issue['message']}")
        if state == "migration_required":
            print("next: lipas migrate plan --home <workspace>")
        elif state == "uninitialized":
            print("next: run a task to initialize the workspace")
        elif payload["ready"]:
            print("result: healthy")
        elif payload["storage_healthy"]:
            print("result: storage healthy, default sandbox not operational")
    return 0 if payload["ready"] else 1


def _cmd_audit(args: argparse.Namespace) -> int:
    home = _workbench_home(args.home)
    storage = WorkspaceStorage(home)
    if args.repair:
        with LIPASRuntime.open(home, sandbox="local") as runtime:
            report = runtime.audit(repair=True)
            payload = {
                "healthy": report.healthy,
                "claim_audit": "completed",
                "storage_issues": [
                    issue.as_dict() for issue in report.storage_issues
                ],
                "claim_issues": [str(issue) for issue in report.claim_issues],
                "repaired": {
                    "execution": report.execution_events_repaired,
                    "operations": report.operation_events_repaired,
                    "handoffs": report.handoff_events_repaired,
                },
            }
    else:
        status = storage.inspect()
        issues = storage.audit() if status.current else status.issues
        payload = {
            "healthy": status.current and not any(
                issue.severity == "error" for issue in issues
            ),
            "claim_audit": "not_run",
            "storage_issues": [issue.as_dict() for issue in issues],
            "claim_issues": None,
            "repaired": {"execution": 0, "operations": 0, "handoffs": 0},
        }
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if payload["healthy"] else 1


def _cmd_migrate_plan(args: argparse.Namespace) -> int:
    plan = WorkspaceStorage(_workbench_home(args.home)).plan_migration()
    payload = plan.as_dict()
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(f"target: {payload['database_path']}")
        print(f"required: {'yes' if payload['required'] else 'no'}")
        print(f"legacy files: {', '.join(path.name for path in plan.legacy_files) or '(none)'}")
        print(f"rows: {payload['rows']}")
        for issue in payload["issues"]:
            print(f"{issue['severity']}: {issue['message']}")
        if plan.can_apply:
            print("apply: lipas migrate apply --yes")
    return 0 if not any(issue.severity == "error" for issue in plan.issues) else 1


def _cmd_migrate_apply(args: argparse.Namespace) -> int:
    if not args.yes:
        raise ValueError(
            "migration is copy-on-write but changes the active layout; "
            "inspect `lipas migrate plan` and pass --yes",
        )
    result = WorkspaceStorage(_workbench_home(args.home)).migrate()
    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_migrate_verify(args: argparse.Namespace) -> int:
    storage = WorkspaceStorage(_workbench_home(args.home))
    payload = _storage_verification_payload(storage)
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if payload["healthy"] and payload["initialized"] else 1


def _cmd_migrate_rollback(args: argparse.Namespace) -> int:
    if not args.yes:
        raise ValueError(
            "rollback stops using all v2-only writes; pass --yes after ensuring "
            "that restoring the migration-time v1 state is intended",
        )
    moved = WorkspaceStorage(_workbench_home(args.home)).rollback()
    print(f"v2 database preserved at {moved}")
    print("legacy v1 files are active again; no files were deleted")
    return 0


def _cmd_tour(args: argparse.Namespace) -> int:
    """Run the authority and recovery path without a model or live write."""
    if not args.offline:
        raise ValueError("the offline authority tour is provider-free; pass --offline")

    from .adapter import Done, Reply, ResourceEstimate, Usage
    from .durable import writes_require_approval
    from .tools import tool

    class OfflineTourAdapter:
        name = "offline-tour"

        def __init__(self) -> None:
            self.replies = [
                Reply(
                    content=({
                        "type": "tool_use", "id": "tour-input",
                        "name": "ask_operator",
                        "input": {"question": "Which preview label?"},
                    },),
                    usage=Usage(input=1, output=1),
                    stop_reason="tool_use",
                    model=self.name,
                ),
                Reply(
                    content=({
                        "type": "tool_use", "id": "tour-publish",
                        "name": "publish_preview",
                        "input": {"label": "approved-offline-preview"},
                    },),
                    usage=Usage(input=1, output=1),
                    stop_reason="tool_use",
                    model=self.name,
                ),
                Reply(
                    content=({
                        "type": "text",
                        "text": "Offline tour completed with separate input and approval.",
                    },),
                    usage=Usage(input=1, output=1),
                    stop_reason="end_turn",
                    model=self.name,
                ),
            ]

        async def estimate_cost(self, request: Any) -> Any:
            return ResourceEstimate(request.model, 1, 1, Decimal("0"))

        async def stream(self, _request: Any):
            yield Done(self.replies.pop(0))

    input_body_calls: list[str] = []
    published: list[str] = []

    @tool(side_effect="pure")
    def ask_operator(question: str) -> str:
        """Request missing information from the human operator."""
        input_body_calls.append(question)
        return "input body must not execute"

    @tool(side_effect="idempotent_write")
    async def publish_preview(label: str) -> str:
        """Publish only an in-memory preview after explicit approval."""
        published.append(label)
        return label

    def input_policy(tool_value: Tool, arguments: Mapping[str, Any]):
        if tool_value.name == "ask_operator":
            return {"question": arguments["question"]}
        return None

    async def run_scenario(root_path: Path) -> dict[str, Any]:
        home = root_path / "state"
        project = root_path / "project"
        project.mkdir()
        with LIPASRuntime.open(home, sandbox="local") as runtime:
            task, run = runtime.workbench.create_task(
                "demonstrate authority-separated recovery", project,
            )
            agent = runtime.agent_for_run(
                run.id,
                adapter=OfflineTourAdapter(),
                model="offline-tour",
                tools=[ask_operator, publish_preview],
            )
            stages: list[dict[str, Any]] = []
            try:
                try:
                    await runtime.run_durable(
                        agent,
                        task.goal,
                        run_id=run.id,
                        input_policy=input_policy,
                        approval_policy=writes_require_approval,
                    )
                except RunSuspended as suspended:
                    if suspended.interrupt.kind != "input":
                        raise AssertionError("tour must suspend for input first")
                    stages.append({
                        "stage": "input_requested",
                        "interrupt_id": suspended.interrupt.id,
                    })
                    runtime.execution.resolve_interrupt(
                        suspended.interrupt.id,
                        allow=True,
                        response={"answer": "offline-preview"},
                    )
                else:  # pragma: no cover - deterministic adapter contract
                    raise AssertionError("tour input did not suspend")

                try:
                    await runtime.resume_durable(
                        agent,
                        run_id=run.id,
                        input_policy=input_policy,
                        approval_policy=writes_require_approval,
                    )
                except RunSuspended as suspended:
                    if suspended.interrupt.kind != "approval":
                        raise AssertionError("tour must request approval second")
                    stages.append({
                        "stage": "approval_requested",
                        "interrupt_id": suspended.interrupt.id,
                    })
                    runtime.execution.resolve_interrupt(
                        suspended.interrupt.id,
                        allow=True,
                        response={"approved_by": "offline-tour-operator"},
                    )
                else:  # pragma: no cover - deterministic adapter contract
                    raise AssertionError("tour approval did not suspend")

                result = await runtime.resume_durable(
                    agent,
                    run_id=run.id,
                    input_policy=input_policy,
                    approval_policy=writes_require_approval,
                )
                stages.append({"stage": "run_completed", "text": result.text})
                artifact = runtime.artifacts.add(
                    task_id=task.id,
                    run_id=run.id,
                    kind="offline_tour",
                    metadata={"published": list(published)},
                )
                report = runtime.workbench.build_report(task.id, result)
                audit = runtime.audit()
                events = runtime.execution.agent_events(run.id)
            finally:
                agent.close()

        return {
            "version": __version__,
            "mode": "offline",
            "task_id": task.id,
            "run_id": run.id,
            "stages": stages,
            "input_tool_body_executed": bool(input_body_calls),
            "published": list(published),
            "artifact_id": artifact.id,
            "report_status": report.status,
            "event_types": [event.type for event in events],
            "audit_healthy": audit.healthy,
        }

    with tempfile.TemporaryDirectory(prefix="lipas-offline-tour-") as root:
        payload = asyncio.run(run_scenario(Path(root)))
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print("LIPAS offline authority tour")
        for stage in payload["stages"]:
            print(f"- {stage['stage']}")
        print("- input supplied information but granted no write authority")
        print("- approval authorized exactly one preview write")
        print(f"- persistent audit: {'healthy' if payload['audit_healthy'] else 'unhealthy'}")
    return 0 if payload["audit_healthy"] and not input_body_calls else 1


def _workbench_agent(
    args: argparse.Namespace,
    workbench: Workbench,
    *,
    task_id: str,
    run_id: str,
) -> Agent:
    task = workbench.execution.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    tools = workbench.workspace_tools(task_id, run_id)
    claims_path = workbench.claims_path_for_run(run_id)
    scenarios = _configured_scenarios(args)
    skills = _configured_skills(args)
    if args.factory:
        factory = _factory_callable(args.factory)
        parameters = inspect.signature(factory).parameters
        kwargs = {
            "tools": tools,
            "session_path": claims_path,
            "workspace": Path(task.workspace),
        }
        accepts_kwargs = any(
            value.kind is inspect.Parameter.VAR_KEYWORD
            for value in parameters.values()
        )
        if skills.skills:
            if "skills" not in parameters and not accepts_kwargs:
                raise ValueError(
                    "selected --skill/--skill-path values require a task factory "
                    "that accepts skills= or **kwargs",
                )
            kwargs["skills"] = skills
        if scenarios.scenarios and (
            "scenarios" in parameters or accepts_kwargs
        ):
            kwargs["scenarios"] = scenarios
        if accepts_kwargs:
            selected = kwargs
        else:
            selected = {key: value for key, value in kwargs.items() if key in parameters}
        agent = factory(**selected)
        if not isinstance(agent, Agent):
            raise TypeError(
                f"factory {args.factory!r} returned {type(agent).__name__}, expected Agent",
            )
        if agent.session_path is None or Path(agent.session_path).resolve() != claims_path:
            agent.close()
            raise ValueError(
                "a task factory must use the supplied session_path so durable "
                "effects can be recovered",
            )
        try:
            scenarios.require_compatible(agent.tool_harness.tools)
        except BaseException:
            agent.close()
            raise
        return agent
    scenarios.require_compatible(tools)
    if args.base_url:
        credential_options = _compatible_credential_options(args)
        return Agent.openai_compatible(
            args.model,
            base_url=args.base_url,
            timeout_s=args.timeout,
            streaming=args.model_streaming,
            max_tokens_field=args.max_tokens_field,
            session=claims_path,
            tools=tools,
            skills=skills,
            instructions=_WORKBENCH_INSTRUCTIONS,
            **credential_options,
        )
    return Agent.ollama(
        args.model,
        host=args.host,
        timeout_s=args.timeout,
        session=claims_path,
        tools=tools,
        skills=skills,
        instructions=_WORKBENCH_INSTRUCTIONS,
    )


def _print_task_report(report: TaskReport | Mapping[str, Any]) -> None:
    data = report.as_dict() if isinstance(report, TaskReport) else dict(report)
    print(f"task: {data['task_id']}")
    print(f"run: {data['run_id']}")
    print(f"status: {data['status']}")
    print(f"verified: {'yes' if data.get('verified') else 'no'}")
    if data.get("change_set_state"):
        print(f"delivery: {data['change_set_state']}")
    changed = data.get("changed_files") or []
    print(f"changed files: {', '.join(changed) if changed else '(none recorded)'}")
    for risk in data.get("unresolved_risks") or []:
        print(f"risk: {risk}")
    if data.get("final_text"):
        print(f"result: {data['final_text']}")


async def _execute_task_async(
    args: argparse.Namespace,
    workbench: Workbench,
    *,
    task_id: str,
    run_id: str,
    prompt: str | None,
    claimed_run: Any | None = None,
) -> int:
    agent = _workbench_agent(
        args, workbench, task_id=task_id, run_id=run_id,
    )
    try:
        with workbench.execution_scope(agent.rowset, run_id=run_id) as execution:
            try:
                result = await agent.run_durable(
                    prompt,
                    execution_store=execution,
                    run_id=run_id,
                    lease_seconds=args.lease_seconds,
                    phase_timeout_s=args.phase_timeout,
                    approval_policy=workbench.approval_policy(task_id),
                    _claimed_run=claimed_run,
                )
            except RunSuspended as suspended:
                workbench.record_approval_required(suspended.interrupt)
                workbench.record_run_state(run_id)
                request = json.dumps(
                    dict(suspended.interrupt.request), ensure_ascii=False,
                )
                print(f"task {task_id} is waiting for approval {suspended.interrupt.id}")
                print(f"request: {request}")
                print(f"resume with: lipas task approve {suspended.interrupt.id}")
                return 0
        workbench.record_run_state(run_id)
        report = workbench.build_report(task_id, result)
        _print_task_report(report)
        return 0 if report.status == RunState.COMPLETED.value else 1
    except Exception:
        run = workbench.execution.get_run(run_id)
        if run is not None and run.state in {
            RunState.FAILED, RunState.CANCELLED, RunState.COMPLETED,
        }:
            _print_task_report(workbench.build_report(task_id))
        raise
    finally:
        agent.close()


def _execute_task(
    args: argparse.Namespace,
    workbench: Workbench,
    *,
    task_id: str,
    run_id: str,
    prompt: str | None,
) -> int:
    return asyncio.run(_execute_task_async(
        args, workbench, task_id=task_id, run_id=run_id, prompt=prompt,
    ))


def _cmd_task_start(args: argparse.Namespace) -> int:
    with _runtime_workbench(
        _workbench_home(args.home), sandbox=args.sandbox,
    ) as workbench:
        task, run = workbench.create_task(
            args.goal, args.workspace, isolate_changes=True,
        )
        print(f"created task {task.id} for {task.workspace}")
        return _execute_task(
            args, workbench, task_id=task.id, run_id=run.id, prompt=task.goal,
        )


def _cmd_task_submit(args: argparse.Namespace) -> int:
    """Persist one Task/Run without tying it to the submitting process."""
    with _runtime_workbench(_workbench_home(args.home)) as workbench:
        task, run = workbench.create_task(
            args.goal, args.workspace, isolate_changes=True,
        )
    print(f"submitted task {task.id} run {run.id} for {task.workspace}")
    return 0


def _print_dispatch_outcome(outcome: DispatchOutcome) -> None:
    detail = f" error={outcome.error_type}" if outcome.error_type else ""
    print(
        f"worker: task={outcome.task_id} run={outcome.run_id} "
        f"status={outcome.status} attempt={outcome.attempt}{detail}",
    )


def _cmd_task_worker(args: argparse.Namespace) -> int:
    home = _workbench_home(args.home).resolve()
    execution_path: Path

    async def execute(task: Any, discovered: Any) -> None:
        # The worker owns one composition root. Concurrent executions open
        # narrow product views over its database and attach only their own
        # Run evidence, avoiding multiple writers to the global Claim view.
        with Workbench(
            home, sandbox=args.sandbox, database_path=execution_path,
        ) as workbench:
            run = workbench.execution.get_run(discovered.id)
            if run is None:
                raise KeyError(discovered.id)
            workbench.add_event(
                task_id=task.id,
                run_id=run.id,
                kind="dispatch_started",
                data={"attempt": run.attempt},
                event_id=f"run:{run.id}:dispatch:{run.attempt}:started",
            )
            try:
                checkpoint = workbench.execution.get_checkpoint(run.id)
                prompt = (
                    task.goal
                    if checkpoint is None and not run.cancel_requested
                    else None
                )
                await _execute_task_async(
                    args,
                    workbench,
                    task_id=task.id,
                    run_id=run.id,
                    prompt=prompt,
                    claimed_run=run,
                )
            finally:
                current = workbench.execution.get_run(run.id)
                workbench.add_event(
                    task_id=task.id,
                    run_id=run.id,
                    kind="dispatch_finished",
                    data={
                        "attempt": run.attempt,
                        "state": current.state.value if current else "missing",
                    },
                    event_id=f"run:{run.id}:dispatch:{run.attempt}:finished",
                )

    with LIPASRuntime.open(home, sandbox=args.sandbox) as runtime:
        execution_path = runtime.database_path
        dispatcher = TaskDispatcher(
            execution_path,
            execute,
            max_concurrency=args.max_concurrency,
            lease_seconds=args.lease_seconds,
            poll_interval_s=args.poll_interval,
            retry_delay_s=args.retry_delay,
            outcome_sink=_print_dispatch_outcome,
        )
        if args.once:
            outcomes = asyncio.run(dispatcher.run_until_idle())
            return 1 if any(
                value.status == "worker_error" for value in outcomes
            ) else 0
        print(
            f"worker started: home={home} concurrency={args.max_concurrency}; "
            "press Ctrl-C to stop",
        )
        asyncio.run(dispatcher.serve())
        return 0


def _cmd_task_list(args: argparse.Namespace) -> int:
    with _runtime_workbench(_workbench_home(args.home)) as workbench:
        print(
            "task_id\ttask_state\trun_state\tdelivery\tattempt\tworkspace\tgoal",
        )
        for task in workbench.list_tasks():
            runs = workbench.execution.list_runs(task_id=task.id)
            run = runs[0] if runs else None
            change_set = workbench.change_set(task.id)
            print("\t".join((
                task.id,
                task.state.value,
                run.state.value if run else "none",
                change_set.state if change_set else "direct",
                str(run.attempt if run else 0),
                task.workspace,
                task.goal,
            )))
    return 0


def _cmd_task_show(args: argparse.Namespace) -> int:
    with _runtime_workbench(_workbench_home(args.home)) as workbench:
        task = workbench.execution.get_task(args.task_id)
        if task is None:
            raise KeyError(args.task_id)
        runs = workbench.execution.list_runs(task_id=task.id)
        approvals = [
            asdict for asdict in (
                {
                    "id": value.id, "state": value.state,
                    "request": dict(value.request),
                }
                for value in workbench.approvals()
                if any(run.id == value.run_id for run in runs)
            )
        ]
        change_set = workbench.change_set(task.id)
        print(json.dumps({
            "task": {
                "id": task.id, "goal": task.goal, "workspace": task.workspace,
                "state": task.state.value,
            },
            "runs": [
                {"id": run.id, "state": run.state.value, "attempt": run.attempt}
                for run in runs
            ],
            "approvals": approvals,
            "change_set": asdict(change_set) if change_set is not None else None,
            "report": workbench.get_report(task.id),
        }, indent=2, ensure_ascii=False))
    return 0


def _cmd_task_approvals(args: argparse.Namespace) -> int:
    with _runtime_workbench(_workbench_home(args.home)) as workbench:
        items: list[dict[str, Any]] = []
        for approval in workbench.approvals(pending_only=not args.all):
            run = workbench.execution.get_run(approval.run_id)
            task = (
                workbench.execution.get_task(run.task_id)
                if run is not None else None
            )
            items.append({
                "approval_id": approval.id,
                "state": approval.state,
                "task_id": task.id if task else None,
                "run_id": approval.run_id,
                "goal": task.goal if task else None,
                "request": dict(approval.request),
                "created_at": approval.created_at,
            })
    if args.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return 0
    print("approval_id\tstate\ttask_id\tgoal\trequest")
    for item in items:
        print("\t".join((
            str(item["approval_id"]),
            str(item["state"]),
            str(item["task_id"] or ""),
            str(item["goal"] or ""),
            json.dumps(item["request"], ensure_ascii=False, sort_keys=True),
        )))
    return 0


def _cmd_task_resume(args: argparse.Namespace) -> int:
    with _runtime_workbench(
        _workbench_home(args.home), sandbox=args.sandbox,
    ) as workbench:
        task = workbench.execution.get_task(args.task_id)
        if task is None:
            raise KeyError(args.task_id)
        runs = workbench.execution.list_runs(task_id=task.id)
        if not runs:
            raise ValueError("task has no run")
        run = runs[0]
        if run.state is RunState.WAITING:
            pending = workbench.execution.list_interrupts(
                run_id=run.id, state=InterruptState.PENDING,
            )
            detail = pending[0].id if pending else "unknown"
            raise ValueError(f"task is waiting for approval {detail}")
        return _execute_task(
            args, workbench, task_id=task.id, run_id=run.id, prompt=None,
        )


def _cmd_task_approve(args: argparse.Namespace) -> int:
    with _runtime_workbench(
        _workbench_home(args.home), sandbox=args.sandbox,
    ) as workbench:
        interrupt = workbench.execution.get_interrupt(args.approval_id)
        if interrupt is None:
            raise KeyError(args.approval_id)
        run = workbench.execution.get_run(interrupt.run_id)
        if run is None:
            raise KeyError(interrupt.run_id)
        workbench.resolve_approval(
            interrupt.id, allow=True, response={"approved_by": "local_user"},
        )
        if args.defer_resume:
            print(
                f"approved {interrupt.id}; run {run.id} is queued for a worker",
            )
            return 0
        return _execute_task(
            args, workbench, task_id=run.task_id, run_id=run.id, prompt=None,
        )


def _cmd_task_deny(args: argparse.Namespace) -> int:
    with _runtime_workbench(_workbench_home(args.home)) as workbench:
        interrupt = workbench.execution.get_interrupt(args.approval_id)
        if interrupt is None:
            raise KeyError(args.approval_id)
        run = workbench.execution.get_run(interrupt.run_id)
        if run is None:
            raise KeyError(interrupt.run_id)
        workbench.resolve_approval(
            interrupt.id, allow=False,
            response={"reason": args.reason or "denied_by_local_user"},
        )
        workbench.execution.cancel_task(run.task_id)
        workbench.record_run_state(run.id)
        _print_task_report(workbench.build_report(run.task_id))
    return 0


def _cmd_task_cancel(args: argparse.Namespace) -> int:
    with _runtime_workbench(_workbench_home(args.home)) as workbench:
        workbench.execution.cancel_task(args.task_id)
        runs = workbench.execution.list_runs(task_id=args.task_id)
        if runs:
            workbench.record_run_state(runs[0].id)
        _print_task_report(workbench.build_report(args.task_id))
    return 0


def _cmd_task_report(args: argparse.Namespace) -> int:
    with _runtime_workbench(_workbench_home(args.home)) as workbench:
        report = workbench.get_report(args.task_id)
        if report is None:
            report = workbench.build_report(args.task_id).as_dict()
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            _print_task_report(report)
    return 0


def _cmd_task_diff(args: argparse.Namespace) -> int:
    with _runtime_workbench(_workbench_home(args.home)) as workbench:
        value = workbench.change_set(args.task_id)
        if value is None:
            raise ValueError(f"task {args.task_id!r} has no staged ChangeSet")
        print(workbench.change_set_diff(args.task_id), end="")
    return 0


def _cmd_task_apply(args: argparse.Namespace) -> int:
    with _runtime_workbench(_workbench_home(args.home)) as workbench:
        paths = workbench.apply_change_set(args.task_id)
    print(
        f"applied task {args.task_id}: "
        f"{', '.join(paths) if paths else '(no file changes)'}",
    )
    return 0


def _cmd_task_discard(args: argparse.Namespace) -> int:
    with _runtime_workbench(_workbench_home(args.home)) as workbench:
        workbench.discard_change_set(args.task_id)
    print(f"discarded staged changes for task {args.task_id}")
    return 0


def _cmd_task_events(args: argparse.Namespace) -> int:
    with _runtime_workbench(_workbench_home(args.home)) as workbench:
        for event in workbench.events(args.task_id):
            print(json.dumps({
                "id": event.id,
                "task_id": event.task_id,
                "run_id": event.run_id,
                "kind": event.kind,
                "data": dict(event.data),
                "created_at": event.created_at,
            }, ensure_ascii=False, sort_keys=True))
    return 0


async def _chat(
    agent: Agent,
    *,
    once: str | None,
    session_id: str = _DEFAULT_CHAT_SESSION_ID,
) -> None:
    conversation = agent.session(session_id=session_id)
    state = None
    prompt = once
    editor = _prompt_session() if once is None else None
    if once is None and sys.stdout.isatty():
        restored = len(conversation.state.messages)
        memory = f"restored {restored} message(s)" if restored else "new memory"
        store_path = getattr(conversation.store, "path", None)
        memory_path = f" · memory={store_path}" if store_path is not None else ""
        print(f"LIPAS chat · model={agent.model} · {memory}{memory_path}")
        print("Type :help for commands; :quit exits.")
    while True:
        if prompt is None:
            if once is not None:
                return
            try:
                prompt = (await _prompt(editor)).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
        if prompt in {":q", ":quit", ":exit"}:
            return
        if prompt in {":h", ":help"}:
            print(
                ":help                 show this help\n"
                ":memory               show remembered turns\n"
                ":runtime              show current directory and capabilities\n"
                ":about                explain LIPAS and its safety boundary\n"
                ":reset                clear this conversation\n"
                ":tools                list available tools\n"
                ":model                show the active model/provider\n"
                ":trace                render the claim tape\n"
                ":effects              show effect diagnostics\n"
                ":quit                 exit chat",
            )
            prompt = None
            continue
        if prompt == ":about":
            print(
                "LIPAS is a local-first Python agent runtime. It keeps chat and "
                "execution evidence durable, exposes only explicit tools, and "
                "requires approval for risky actions. The model provides the "
                "language ability; LIPAS provides the runtime, memory, tools, "
                "and safety boundary."
            )
            prompt = None
            continue
        if prompt == ":memory":
            store_path = getattr(conversation.store, "path", None)
            memory_path = f" · path: {store_path}" if store_path is not None else ""
            print(
                f"session: {conversation.id} · messages: "
                f"{len(conversation.state.messages)} · version: {conversation.version}"
                f"{memory_path}"
            )
            prompt = None
            continue
        if prompt in {":runtime", ":where"}:
            runtime_tool = next(
                (
                    value for value in agent.tool_harness.tools
                    if value.name == "get_runtime_info"
                ),
                None,
            )
            if runtime_tool is None:
                print("runtime facts: unavailable")
            else:
                print(json.dumps(runtime_tool.invoke(), ensure_ascii=False, indent=2))
            prompt = None
            continue
        if prompt in {":reset", ":clear"}:
            conversation.reset()
            state = None
            print("conversation memory cleared")
            prompt = None
            continue
        if prompt == ":model":
            print(f"model: {agent.model} · provider: {getattr(agent.adapter, 'name', 'unknown')}")
            prompt = None
            continue
        if prompt == ":tools":
            tools = tuple(agent.tool_harness.tools)
            if not tools:
                print("tools: none")
            else:
                print("tools:")
                for value in tools:
                    print(f"- {value.name} [{value.side_effect.value}] — {value.description}")
            prompt = None
            continue
        if prompt == ":trace":
            print(render_trace(agent.rowset.store))
            prompt = None
            continue
        if prompt == ":effects":
            view = _effect_row(agent.rowset).project(agent.rowset.store)
            print(json.dumps({"orphans": view.orphans, "rejected": view.rejected}, indent=2))
            prompt = None
            continue
        if not prompt:
            prompt = None
            continue
        result = await _run_with_feedback(
            agent,
            prompt,
            state,
            session=conversation,
        )
        state = result.state
        if not result.is_error or result.text:
            print(f"agent> {result.text}")
        if result.is_error:
            print(_friendly_error(agent, result.error), file=sys.stderr)
        if once is not None:
            return
        prompt = None


def _cmd_chat(args: argparse.Namespace) -> int:
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    if args.retries < 0:
        raise ValueError("--retries must be zero or greater")
    positional_model = getattr(args, "model_positional", None)
    option_model = getattr(args, "model", None)
    if positional_model and option_model:
        raise ValueError("pass the model either positionally or with --model, not both")
    if (
        positional_model == "list"
        and not option_model
        and not args.base_url
        and args.once is None
        and not args.factory
        and not getattr(args, "workspace", None)
        and not getattr(args, "tool_factory", None)
        and not getattr(args, "session", None)
        and not getattr(args, "no_memory", False)
    ):
        return _cmd_model_list(
            argparse.Namespace(
                host=args.host,
                timeout=min(args.timeout, 5.0),
                trust_env=getattr(args, "trust_env", False),
                json=False,
            )
        )
    model = (
        positional_model
        or option_model
        or os.environ.get("LIPAS_OLLAMA_MODEL")
        or _DEFAULT_MODEL
    )
    if args.factory and (
        getattr(args, "tool_factory", None)
        or getattr(args, "workspace", None)
    ):
        raise ValueError("--workspace/--tool-factory configure the built-in Agent, not --factory")
    if getattr(args, "tool_factory", None) and getattr(args, "workspace", None):
        raise ValueError("pass either --workspace or --tool-factory, not both")
    workspace_root = None
    selected_tools = ToolRegistry()
    if getattr(args, "tool_factory", None):
        selected_tools = _tool_factory(args.tool_factory)
    elif getattr(args, "workspace", None):
        selected_tools = _chat_workspace_tools(args.workspace)
        workspace_root = Path(args.workspace).expanduser().resolve()
    if getattr(args, "no_memory", False) and getattr(args, "session", None):
        raise ValueError("pass either --session or --no-memory, not both")
    session_path = (
        None
        if getattr(args, "no_memory", False)
        else getattr(args, "session", None) or str(_DEFAULT_CHAT_SESSION)
    )
    # Even a tool-less chat gets a read-only, auditable source of truth for
    # cwd, session scope, and capability boundaries.  User-selected tools are
    # additive; they never replace this runtime fact tool.
    runtime_tool = _chat_runtime_tool(
        workspace=workspace_root,
        session_path=session_path,
        session_id=getattr(args, "session_id", _DEFAULT_CHAT_SESSION_ID),
    )
    if "get_runtime_info" not in selected_tools:
        selected_tools = ToolRegistry([runtime_tool, *tuple(selected_tools)])
    scenarios = _configured_scenarios(args)
    skills = _configured_skills(args)
    if args.factory:
        agent = (
            _factory(args.factory, skills=skills, scenarios=scenarios)
            if skills.skills or scenarios.scenarios
            else _factory(args.factory)
        )
    else:
        scenarios.require_compatible(())
        retry_policy = dict(DEFAULT_POLICY)
        for kind in (ErrorKind.TIMEOUT, ErrorKind.NETWORK):
            retry_policy[kind] = RetryPolicy(
                should_retry=args.retries > 0,
                base_delay_s=1.0,
                max_attempts=args.retries + 1,
            )
        # Factories intentionally accept a broad set of Agent kwargs.  Keep
        # this staging dictionary typed as ``Any`` so the two explicit
        # constructors below remain checked for their named parameters while
        # optional passthrough values are not widened to ``object``.
        common: dict[str, Any] = {
            "instructions": _chat_runtime_instructions(
                args.instructions,
                workspace=workspace_root,
                session_path=session_path,
                session_id=getattr(args, "session_id", _DEFAULT_CHAT_SESSION_ID),
            ),
            "skills": skills,
            "harness_kwargs": {"retry_policy": retry_policy},
        }
        if session_path is not None:
            common["session"] = session_path
        common["tools"] = selected_tools
        if args.base_url:
            credential_options = _compatible_credential_options(args)
            agent = Agent.openai_compatible(
                model,
                base_url=args.base_url,
                timeout_s=args.timeout,
                streaming=args.model_streaming,
                max_tokens_field=args.max_tokens_field,
                **common,
                **credential_options,
            )
        else:
            agent = Agent.ollama(
                model,
                host=args.host,
                timeout_s=args.timeout,
                trust_env=getattr(args, "trust_env", False),
                **common,
            )
    try:
        with _chat_transport_logs(args.verbose):
            asyncio.run(
                _chat(
                    agent,
                    once=args.once,
                    session_id=getattr(args, "session_id", _DEFAULT_CHAT_SESSION_ID),
                )
            )
    finally:
        agent.close()
    return 0


def _cmd_model_list(args: argparse.Namespace) -> int:
    """List models from a local Ollama daemon without opening a chat session."""
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    endpoint = str(
        getattr(args, "host", None)
        or os.environ.get("OLLAMA_HOST")
        or "http://localhost:11434"
    ).rstrip("/")
    try:
        import httpx
        from .adapter.ollama import OllamaAdapter

        adapter = OllamaAdapter(
            host=args.host,
            timeout_s=args.timeout,
            trust_env=getattr(args, "trust_env", False),
        )

        async def fetch() -> list[dict[str, Any]]:
            async with httpx.AsyncClient(
                timeout=args.timeout,
                trust_env=adapter.trust_env,
            ) as client:
                response = await client.get(f"{adapter.host}/api/tags")
                response.raise_for_status()
                payload = response.json()
            models = payload.get("models", []) if isinstance(payload, dict) else []
            if not isinstance(models, list):
                raise ValueError("Ollama returned an invalid model list")
            return [value for value in models if isinstance(value, dict)]

        models = asyncio.run(fetch())
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        print(
            f"Could not list Ollama models at {endpoint}: {detail}. "
            "Check `ollama ps` and the configured host.",
            file=sys.stderr,
        )
        return 1

    if getattr(args, "json", False):
        print(json.dumps(models, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    if not models:
        print("No Ollama models found. Pull one with `ollama pull <model>`.")
        return 0
    print("name\tsize\tmodified")
    for model in models:
        print(
            "\t".join(
                (
                    str(model.get("name", "")),
                    str(model.get("size", "")),
                    str(model.get("modified_at", "")),
                )
            )
        )
    return 0


def _cmd_skill_list(args: argparse.Namespace) -> int:
    values = [
        {
            "name": skill.name,
            "description": skill.description,
            "category": skill.metadata.get("category"),
            "authority": skill.metadata.get("authority", "instructions-only"),
        }
        for skill in builtin_skills()
    ]
    if args.json:
        print(json.dumps(values, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        for value in values:
            print(
                f"{value['name']}\t{value['category'] or 'general'}\t"
                f"{value['description']}",
            )
    return 0


def _cmd_skill_show(args: argparse.Namespace) -> int:
    skill = load_builtin_skill(args.name)
    payload = {
        "name": skill.name,
        "description": skill.description,
        "instructions": skill.instructions,
        "metadata": dict(skill.metadata),
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(f"{skill.name} — {skill.description}\n")
        print(skill.instructions)
    return 0


def _cmd_scenario_list(args: argparse.Namespace) -> int:
    scenarios = builtin_scenarios()
    if args.category:
        scenarios = tuple(
            value for value in scenarios if value.category == args.category
        )
    values = [
        {
            "name": value.name,
            "title": value.title,
            "description": value.description,
            "category": value.category,
            "mode": value.mode.value,
            "skills": list(value.skill_names),
            "capability_count": len(value.capabilities),
        }
        for value in scenarios
    ]
    if args.json:
        print(json.dumps(values, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        for value in values:
            print(
                f"{value['name']}\t{value['mode']}\t{value['category']}\t"
                f"{value['description']}",
            )
    return 0


def _cmd_scenario_show(args: argparse.Namespace) -> int:
    scenario = load_builtin_scenario(args.name)
    payload = scenario.as_dict()
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(f"{scenario.name} — {scenario.title}")
        print(scenario.description)
        print(f"mode: {scenario.mode.value}")
        print(f"skills: {', '.join(scenario.skill_names)}")
        print("lifecycle: " + " -> ".join(scenario.lifecycle))
        if scenario.capabilities:
            print("capabilities:")
            for value in scenario.capabilities:
                print(
                    f"- {value.name} ({value.side_effect.value}, "
                    f"approval={value.approval}) — {value.purpose}",
                )
        if scenario.host_requirements:
            print("host requirements:")
            for host_requirement in scenario.host_requirements:
                print(f"- {host_requirement}")
    return 0


def _cmd_scenario_check(args: argparse.Namespace) -> int:
    scenario = load_builtin_scenario(args.name)
    tools = _tool_factory(args.factory) if args.factory else ToolRegistry()
    assessment = scenario.assess(tools)
    payload = assessment.as_dict()
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(f"scenario: {scenario.name}")
        print(f"tool contract: {'compatible' if assessment.compatible else 'incomplete'}")
        if assessment.missing_tools:
            print(f"missing: {', '.join(assessment.missing_tools)}")
        for value in assessment.mismatches:
            print(
                f"mismatch: {value.name} expected {value.expected.value}, "
                f"got {value.actual.value}",
            )
        for schema_issue in assessment.schema_mismatches:
            print(
                f"schema mismatch: {schema_issue.name} missing "
                f"{', '.join(schema_issue.missing_parameters)}",
            )
        if scenario.mode is ScenarioMode.CONNECTOR:
            print("connector host policy still requires independent review")
    return 0 if assessment.compatible else 1


def _model_check_adapter(args: argparse.Namespace) -> Any:
    """Construct the diagnostic adapter without exposing its credential."""
    from .adapter import OpenAICompatibleAdapter
    credential_options = _compatible_credential_options(args)
    return OpenAICompatibleAdapter(
        base_url=args.base_url,
        timeout_s=args.timeout,
        streaming=args.model_streaming,
        include_usage=args.include_usage,
        max_tokens_field=args.max_tokens_field,
        **credential_options,
    )


def _reply_text(reply: Any) -> str:
    text: list[str] = []
    for block in reply.content:
        if isinstance(block, Mapping) and block.get("type") == "text":
            text.append(str(block.get("text", "")))
        elif getattr(block, "type", None) == "text":
            text.append(str(getattr(block, "text", "")))
    return "".join(text)


def _cmd_model_check(args: argparse.Namespace) -> int:
    """Validate endpoint configuration and optionally run one explicit probe."""
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    if args.max_tokens <= 0:
        raise ValueError("--max-tokens must be positive")
    if args.prompt is not None and not args.live:
        raise ValueError("--prompt requires --live")
    adapter = _model_check_adapter(args)
    from .models import ModelRegistry

    capabilities = ModelRegistry.default().resolve(adapter.name, args.model)
    capability_data = capabilities.as_dict()
    payload: dict[str, Any] = {
        "version": __version__,
        "configured": True,
        "live": bool(args.live),
        "network_request_sent": False,
        "endpoint": adapter.url,
        "provider": adapter.name,
        "model": args.model,
        "api_key": {
            "environment": None if args.no_api_key else args.api_key_env,
            "required": not args.no_api_key,
            "present": adapter.api_key is not None,
            "value_exposed": False,
        },
        "request": {
            "streaming": adapter.streaming,
            "include_usage": adapter.include_usage,
            "max_tokens_field": adapter.max_tokens_field,
            "max_tokens": args.max_tokens,
        },
        "capabilities": capability_data,
        "unknown_capabilities": sorted(
            name for name in (
                "tool_calling",
                "streaming",
                "structured_output",
                "vision",
                "reasoning",
                "context_tokens",
                "local",
            )
            if capability_data[name] is None
        ),
    }
    status = 0
    if args.live:
        from .adapter import Request, complete
        from .adapter.errors import classify

        reply = asyncio.run(complete(adapter, Request(
            model=args.model,
            messages=[{
                "role": "user",
                "content": args.prompt or _DEFAULT_MODEL_CHECK_PROMPT,
            }],
            max_tokens=args.max_tokens,
        )))
        payload["network_request_sent"] = True
        payload["result"] = {
            "ok": reply.stop_reason != "error",
            "model": reply.model,
            "stop_reason": reply.stop_reason,
            "usage": asdict(reply.usage),
            "text": _reply_text(reply),
            "error_kind": (
                classify(reply).value if reply.stop_reason == "error" else None
            ),
            "error_detail": (
                dict(reply.error_detail or {})
                if reply.stop_reason == "error"
                else None
            ),
        }
        status = 0 if payload["result"]["ok"] else 1
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(f"endpoint: {payload['endpoint']}")
        print(f"model: {payload['model']}")
        if args.no_api_key:
            print("credential: disabled explicitly")
        else:
            print(f"credential: present in {args.api_key_env} (value not displayed)")
        print(
            "transport: "
            f"{'SSE' if adapter.streaming else 'single response'}",
        )
        visible_capabilities = {
            name: value
            for name, value in payload["capabilities"].items()
            if name not in {"provider", "model", "metadata"}
        }
        print(
            "capabilities: "
            + json.dumps(visible_capabilities, ensure_ascii=False, sort_keys=True),
        )
        if args.live:
            result = payload["result"]
            print(f"live result: {'ok' if result['ok'] else 'failed'}")
            print(f"provider model: {result['model']}")
            print(f"stop reason: {result['stop_reason']}")
            print(
                "usage: "
                + json.dumps(result["usage"], ensure_ascii=False, sort_keys=True),
            )
            if result["error_kind"]:
                print(f"error kind: {result['error_kind']}")
                print(
                    "error detail: "
                    + json.dumps(
                        result["error_detail"], ensure_ascii=False, sort_keys=True,
                    ),
                )
            elif result["text"]:
                print(
                    "response text: "
                    + json.dumps(result["text"], ensure_ascii=False),
                )
        else:
            print("live request: not sent (pass --live to opt in)")
    return status


def _validate_model_endpoint_args(args: argparse.Namespace) -> None:
    """Reject provider options that would otherwise be silently ignored."""
    if args.base_url and args.host:
        raise ValueError("pass either --base-url or --host, not both")
    if args.factory and args.base_url:
        raise ValueError("--base-url configures the built-in Agent, not --factory")
    if getattr(args, "trust_env", False) and args.factory:
        raise ValueError("--trust-env configures the built-in Ollama Agent, not --factory")
    if args.base_url:
        if getattr(args, "trust_env", False):
            raise ValueError("--trust-env configures Ollama, not --base-url")
        return
    if args.model_streaming:
        raise ValueError("--model-streaming requires --base-url")
    if args.max_tokens_field != "max_tokens":
        raise ValueError("--max-tokens-field requires --base-url")
    if args.api_key_env != "OPENAI_API_KEY":
        raise ValueError("--api-key-env requires --base-url")
    if args.no_api_key:
        raise ValueError("--no-api-key requires --base-url")


def _compatible_credential_options(args: argparse.Namespace) -> dict[str, Any]:
    """Translate explicit CLI credential policy into adapter arguments."""
    if args.no_api_key:
        return {"api_key_env": None, "require_api_key": False}
    return {"api_key_env": args.api_key_env, "require_api_key": True}


def _cmd_install(args: argparse.Namespace) -> int:
    manifest = install_workspace(
        _workbench_home(args.home), sandbox=args.sandbox, force=args.force,
    )
    payload = manifest.as_dict()
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(f"installed LIPAS {manifest.package_version} at {manifest.home}")
        print(f"schema: {manifest.schema_version}; sandbox: {manifest.sandbox}")
        print(f"database: {manifest.home / 'workspace.db'}")
    return 0


def _cmd_upgrade(args: argparse.Namespace) -> int:
    manifest = upgrade_workspace(
        _workbench_home(args.home), sandbox=args.sandbox, force=True,
    )
    payload = manifest.as_dict()
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(f"upgraded LIPAS workspace at {manifest.home}")
        print(f"schema: {manifest.schema_version}; sandbox: {manifest.sandbox}")
    return 0


def _cmd_release_check(args: argparse.Namespace) -> int:
    report = release_check(_workbench_home(args.home))
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(f"workspace: {report.home}")
        for check in report.checks:
            print(f"{'ok' if check.passed else 'FAIL'}: {check.name}: {check.detail}")
        print(f"result: {'ready' if report.ready else 'not ready'}")
    return 0 if report.ready else 1


def _cmd_operator_serve(args: argparse.Namespace) -> int:
    home = _workbench_home(args.home)
    token = os.environ.get(args.token_env)
    generated = token is None
    if token is None:
        # An ephemeral token is convenient for a loopback first run and is
        # never persisted. Non-loopback binds still require an operator to
        # supply a stable secret through the environment.
        normalized_host = args.host.strip().lower().strip("[]")
        if normalized_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError(
                f"{args.token_env} must be set for a non-loopback operator bind",
            )
        token = secrets.token_urlsafe(24)
    if len(token.encode("utf-8")) < 16:
        raise ValueError(f"{args.token_env} must contain at least 16 bytes")
    tls = None
    if args.certfile or args.keyfile or args.cafile:
        if not args.certfile or not args.keyfile:
            raise ValueError("--certfile and --keyfile must be provided together")
        tls = TLSConfig(
            args.certfile,
            args.keyfile,
            cafile=args.cafile,
            require_client_certificate=args.require_client_certificate,
        )
    with LIPASRuntime.open(home, sandbox=args.sandbox) as runtime:
        operator = runtime.operator(
            operator_token=token,
            require_authentication=True,
        )
        if generated:
            print(f"operator token (ephemeral): {token}", file=sys.stderr)
        scheme = "https" if tls is not None else "http"
        print(f"operator listening: {scheme}://{args.host}:{args.port}")
        operator.serve_forever(args.host, args.port, tls=tls)
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    storage = WorkspaceStorage(_workbench_home(args.home))
    result = (
        storage.backup_bundle(args.destination)
        if args.include_evidence
        else storage.backup(args.destination)
    )
    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    if not args.yes:
        raise ValueError("restore replaces workspace.db; pass --yes after inspecting the backup")
    storage = WorkspaceStorage(_workbench_home(args.home))
    source = Path(args.source).expanduser()
    result = (
        storage.restore_bundle(source, keep_backup=not args.no_backup)
        if source.is_dir()
        else storage.restore(source, keep_backup=not args.no_backup)
    )
    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_verify_bundle(args: argparse.Namespace) -> int:
    """Verify an evidence bundle without touching the target workspace."""
    storage = WorkspaceStorage(_workbench_home(args.home))
    result = storage.verify_bundle(Path(args.source).expanduser())
    payload = result.as_dict()
    payload["verified"] = True
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(f"verified bundle: {result.bundle_path}")
        print(f"files: {len(result.files)}")
    return 0


def _cmd_soak(args: argparse.Namespace) -> int:
    """Run a bounded local durability soak and emit its evidence report."""
    home = _workbench_home(args.home)
    with LIPASRuntime.open(home, sandbox=args.sandbox) as runtime:
        report = run_execution_soak(
            runtime.execution,
            iterations=args.iterations,
            duration_s=args.duration,
            workspace=home,
            task_prefix=args.task_prefix,
        )
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"soak: {'healthy' if report.healthy else 'FAILED'} "
            f"{report.executed_iterations}/{report.requested_iterations} iterations, "
            f"p95={report.p95_latency_ms:.2f}ms",
        )
        for issue in report.invariant_failures:
            print(f"failure: {issue}")
    return 0 if report.healthy else 1


def _cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.directory)
    if target.exists() and any(target.iterdir()) and not args.force:
        raise ValueError(f"{target} is not empty; pass --force to add scaffold files")
    target.mkdir(parents=True, exist_ok=True)
    agent_py = target / "agent.py"
    readme = target / "README.md"
    if (agent_py.exists() or readme.exists()) and not args.force:
        raise ValueError(f"{target} already contains a LIPAS scaffold; pass --force to replace it")
    agent_py.write_text(
        "\"\"\"Your ordinary Python LIPAS agent.\"\"\"\n"
        "from lipas import Agent\n\n"
        "def build_agent() -> Agent:\n"
        f"    return Agent.ollama({args.model!r},\n"
        "        instructions=\"Be concise and state uncertainty.\",\n"
        "        session=\"runs/chat.db\",\n"
        "    )\n",
        encoding="utf-8",
    )
    readme.write_text(
        "# LIPAS prototype\n\n"
        "This is ordinary Python. Add tools and business logic to `agent.py`.\n\n"
        "```bash\n"
        f"ollama pull {args.model}\n"
        "lipas chat --factory agent:build_agent\n"
        "lipas trace runs/chat.db\n"
        "lipas effects runs/chat.db\n"
        "```\n",
        encoding="utf-8",
    )
    print(f"created {target}/agent.py and {target}/README.md")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lipas",
        description="Run durable local tasks and inspect Python LIPAS agents.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_compatible_credentials(command: argparse.ArgumentParser) -> None:
        credentials = command.add_mutually_exclusive_group()
        credentials.add_argument(
            "--api-key-env", default="OPENAI_API_KEY",
            help="environment variable containing the compatible API key",
        )
        credentials.add_argument(
            "--no-api-key", action="store_true",
            help="explicitly use a trusted compatible endpoint without auth",
        )

    def add_skill_options(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--scenario", action="append", default=[], metavar="NAME",
            help="select one packaged business Scenario; repeat to compose",
        )
        command.add_argument(
            "--skill", action="append", default=[], metavar="NAME",
            help="select one packaged business Skill; repeat to compose",
        )
        command.add_argument(
            "--skill-path", action="append", default=[], metavar="PATH",
            help="load a portable SKILL.md file or directory; repeat to compose",
        )

    init = sub.add_parser("init", help="create a minimal ordinary-Python agent")
    init.add_argument("directory")
    init.add_argument("--model", default=os.environ.get("LIPAS_OLLAMA_MODEL", _DEFAULT_MODEL))
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=_cmd_init)

    install = sub.add_parser(
        "install", help="initialize a local workspace and harden its layout",
    )
    install.add_argument(
        "--home", help="workspace directory (default: LIPAS_HOME or ~/.lipas)",
    )
    install.add_argument(
        "--sandbox", choices=("auto", "bwrap", "local"),
        default=os.environ.get("LIPAS_SANDBOX", "auto"),
    )
    install.add_argument("--force", action="store_true", help="refresh the manifest")
    install.add_argument("--json", action="store_true")
    install.set_defaults(func=_cmd_install)

    upgrade = sub.add_parser(
        "upgrade", help="migrate a legacy workspace and refresh its manifest",
    )
    upgrade.add_argument(
        "--home", help="workspace directory (default: LIPAS_HOME or ~/.lipas)",
    )
    upgrade.add_argument(
        "--sandbox", choices=("auto", "bwrap", "local"),
        default=os.environ.get("LIPAS_SANDBOX", "auto"),
    )
    upgrade.add_argument("--json", action="store_true")
    upgrade.set_defaults(func=_cmd_upgrade)

    release = sub.add_parser(
        "release", help="run local single-workspace release readiness checks",
    )
    release_sub = release.add_subparsers(dest="release_command", required=True)
    release_check_parser = release_sub.add_parser(
        "check", help="check installation, schema, integrity, and permissions",
    )
    release_check_parser.add_argument(
        "--home", help="workspace directory (default: LIPAS_HOME or ~/.lipas)",
    )
    release_check_parser.add_argument("--json", action="store_true")
    release_check_parser.set_defaults(func=_cmd_release_check)

    operator = sub.add_parser(
        "operator", help="serve the authenticated local Web operator",
    )
    operator_sub = operator.add_subparsers(dest="operator_command", required=True)
    operator_serve = operator_sub.add_parser(
        "serve", help="serve Tasks, Conversations, approvals, and evidence",
    )
    operator_serve.add_argument("--home", help="workspace directory")
    operator_serve.add_argument(
        "--host", default="127.0.0.1",
        help="bind host; non-loopback requires TLS and an environment token",
    )
    operator_serve.add_argument("--port", type=int, default=8787)
    operator_serve.add_argument(
        "--token-env", default="LIPAS_OPERATOR_TOKEN",
        help="environment variable containing the bearer token",
    )
    operator_serve.add_argument(
        "--certfile", help="PEM certificate; required for non-loopback binds",
    )
    operator_serve.add_argument("--keyfile", help="PEM private key")
    operator_serve.add_argument("--cafile", help="optional CA for mutual TLS")
    operator_serve.add_argument(
        "--require-client-certificate", action="store_true",
        help="require client certificates when --cafile is supplied",
    )
    operator_serve.add_argument(
        "--sandbox", choices=("auto", "bwrap", "local"),
        default=os.environ.get("LIPAS_SANDBOX", "auto"),
    )
    operator_serve.set_defaults(func=_cmd_operator_serve)

    backup = sub.add_parser("backup", help="create an integrity-checked workspace backup")
    backup.add_argument("--home", help="workspace directory")
    backup.add_argument("--destination", required=True, help="backup SQLite path or bundle directory")
    backup.add_argument(
        "--include-evidence", action="store_true",
        help="include runs/<run-id> evidence in a manifest- and hash-verified bundle",
    )
    backup.set_defaults(func=_cmd_backup)

    restore = sub.add_parser("restore", help="restore an integrity-checked workspace backup")
    restore.add_argument("--home", help="workspace directory")
    restore.add_argument("--source", required=True, help="backup SQLite path or evidence bundle directory")
    restore.add_argument("--yes", action="store_true")
    restore.add_argument("--no-backup", action="store_true", help="do not preserve pre-restore state")
    restore.set_defaults(func=_cmd_restore)

    verify_bundle = sub.add_parser(
        "verify-bundle", help="verify a complete workspace/evidence bundle",
    )
    verify_bundle.add_argument("--home", help="workspace directory (used for schema context)")
    verify_bundle.add_argument("--source", required=True, help="evidence bundle directory")
    verify_bundle.add_argument("--json", action="store_true")
    verify_bundle.set_defaults(func=_cmd_verify_bundle)

    soak = sub.add_parser(
        "soak", help="run a bounded local Task/Run durability soak",
    )
    soak.add_argument("--home", help="workspace directory")
    soak.add_argument("--iterations", type=int, default=100)
    soak.add_argument(
        "--duration", type=float,
        help="optional wall-clock cap in seconds (iterations remains the hard cap)",
    )
    soak.add_argument("--task-prefix", default="soak")
    soak.add_argument(
        "--sandbox", choices=("auto", "bwrap", "local"),
        default=os.environ.get("LIPAS_SANDBOX", "auto"),
    )
    soak.add_argument("--json", action="store_true")
    soak.set_defaults(func=_cmd_soak)

    chat = sub.add_parser("chat", help="try an Agent interactively")
    chat.add_argument(
        "model_positional", nargs="?", metavar="MODEL",
        help="model name (shortcut for --model)",
    )
    chat.add_argument(
        "--model", default=None,
        help=(
            "model name (default: LIPAS_OLLAMA_MODEL or "
            f"{_DEFAULT_MODEL})"
        ),
    )
    chat_endpoint = chat.add_mutually_exclusive_group()
    chat_endpoint.add_argument(
        "--host",
        help=(
            "Ollama host (http:// is optional); defaults to OLLAMA_HOST or "
            "localhost:11434"
        ),
    )
    chat_endpoint.add_argument(
        "--base-url",
        help="OpenAI-compatible API root or full /chat/completions URL",
    )
    add_compatible_credentials(chat)
    chat.add_argument(
        "--model-streaming",
        action="store_true",
        help="use the endpoint's SSE streaming contract",
    )
    chat.add_argument(
        "--max-tokens-field",
        choices=("max_tokens", "max_completion_tokens"),
        default="max_tokens",
    )
    chat.add_argument(
        "--timeout", type=float, default=500.0,
        help="model response timeout in seconds",
    )
    chat.add_argument(
        "--retries", type=int, default=0,
        help="extra timeout/network retries (default: 0)",
    )
    chat.add_argument(
        "--trust-env", action="store_true",
        help="honor HTTP(S)_PROXY/ALL_PROXY when connecting to Ollama",
    )
    chat.add_argument("--verbose", action="store_true", help="show adapter retry diagnostics")
    chat.add_argument("--instructions", default=_DEFAULT_INSTRUCTIONS)
    chat_session = chat.add_mutually_exclusive_group()
    chat_session.add_argument(
        "--session", help="SQLite claim/conversation path (default: ~/.lipas/runs/chat.db)",
    )
    chat_session.add_argument(
        "--no-memory", action="store_true",
        help="keep this chat in memory only; do not persist conversation context",
    )
    chat.add_argument(
        "--session-id", default=_DEFAULT_CHAT_SESSION_ID,
        help="conversation identity inside --session (default: cli-chat)",
    )
    chat.add_argument(
        "--workspace", metavar="PATH",
        help="opt in to bounded read-only file tools for this directory",
    )
    chat.add_argument(
        "--tool-factory", metavar="MODULE:CALLABLE",
        help="load additional tools from a Python factory returning Tool(s)",
    )
    chat.add_argument("--factory", help="ordinary Python factory: module:callable")
    add_skill_options(chat)
    chat.add_argument("--once", help="send one prompt instead of opening a REPL")
    chat.set_defaults(func=_cmd_chat)

    skill = sub.add_parser(
        "skill", help="inspect packaged instruction-only business Skills",
    )
    skill_sub = skill.add_subparsers(dest="skill_command", required=True)
    skill_list = skill_sub.add_parser("list", help="list packaged Skills")
    skill_list.add_argument("--json", action="store_true")
    skill_list.set_defaults(func=_cmd_skill_list)
    skill_show = skill_sub.add_parser("show", help="show one packaged Skill")
    skill_show.add_argument("name")
    skill_show.add_argument("--json", action="store_true")
    skill_show.set_defaults(func=_cmd_skill_show)

    scenario = sub.add_parser(
        "scenario", help="inspect and validate composable business Scenarios",
    )
    scenario_sub = scenario.add_subparsers(
        dest="scenario_command", required=True,
    )
    scenario_list = scenario_sub.add_parser(
        "list", help="list packaged business Scenarios",
    )
    scenario_list.add_argument("--category")
    scenario_list.add_argument("--json", action="store_true")
    scenario_list.set_defaults(func=_cmd_scenario_list)
    scenario_show = scenario_sub.add_parser(
        "show", help="show a Scenario lifecycle and capability contract",
    )
    scenario_show.add_argument("name")
    scenario_show.add_argument("--json", action="store_true")
    scenario_show.set_defaults(func=_cmd_scenario_show)
    scenario_check = scenario_sub.add_parser(
        "check", help="check a Scenario against a supplied Tool factory",
    )
    scenario_check.add_argument("name")
    scenario_check.add_argument(
        "--factory",
        help="module:callable returning Tool, ToolRegistry, or iterable of Tool",
    )
    scenario_check.add_argument("--json", action="store_true")
    scenario_check.set_defaults(func=_cmd_scenario_check)

    model = sub.add_parser(
        "model", help="list local models or validate endpoint configuration",
    )
    model_sub = model.add_subparsers(dest="model_command", required=True)
    model_list = model_sub.add_parser(
        "list", help="list models available from the local Ollama daemon",
    )
    model_list.add_argument(
        "--host",
        help="Ollama host (http:// is optional; default: OLLAMA_HOST or localhost:11434)",
    )
    model_list.add_argument("--timeout", type=float, default=5.0)
    model_list.add_argument(
        "--trust-env", action="store_true",
        help="honor HTTP(S)_PROXY/ALL_PROXY when connecting to Ollama",
    )
    model_list.add_argument("--json", action="store_true")
    model_list.set_defaults(func=_cmd_model_list)
    model_check = model_sub.add_parser(
        "check", help="check a compatible endpoint without calling it by default",
    )
    model_check.add_argument(
        "--base-url", required=True,
        help="OpenAI-compatible API root or full /chat/completions URL",
    )
    model_check.add_argument("--model", required=True)
    add_compatible_credentials(model_check)
    model_check.add_argument(
        "--model-streaming", action="store_true",
        help="validate and probe the SSE streaming route",
    )
    model_check.add_argument(
        "--include-usage", action="store_true",
        help="request a terminal streaming usage chunk; requires streaming",
    )
    model_check.add_argument(
        "--max-tokens-field",
        choices=("max_tokens", "max_completion_tokens"),
        default="max_tokens",
    )
    model_check.add_argument("--timeout", type=float, default=30.0)
    model_check.add_argument("--max-tokens", type=int, default=16)
    model_check.add_argument(
        "--prompt",
        help=(
            "minimal prompt used only with --live; defaults to a fixed OK probe"
        ),
    )
    model_check.add_argument(
        "--live", action="store_true",
        help="send one explicit external request; it may be billable",
    )
    model_check.add_argument("--json", action="store_true")
    model_check.set_defaults(host=None, factory=None, func=_cmd_model_check)

    trace = sub.add_parser("trace", help="render a durable claim session")
    trace.add_argument("session")
    trace.add_argument("--jsonl", action="store_true")
    trace.set_defaults(func=_cmd_trace)

    effects = sub.add_parser("effects", help="summarize effect lifecycle in a session")
    effects.add_argument("session")
    effects.set_defaults(func=_cmd_effects)

    doctor = sub.add_parser(
        "doctor", help="inspect runtime storage and sandbox readiness",
    )
    doctor.add_argument(
        "--home", help="runtime state directory (default: LIPAS_HOME or ~/.lipas)",
    )
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=_cmd_doctor)

    audit = sub.add_parser(
        "audit", help="check persistent runtime invariants without changing state",
    )
    audit.add_argument(
        "--home", help="runtime state directory (default: LIPAS_HOME or ~/.lipas)",
    )
    audit.add_argument(
        "--repair", action="store_true",
        help="explicitly replay recoverable audit outboxes after checking storage",
    )
    audit.set_defaults(func=_cmd_audit)

    tour = sub.add_parser(
        "tour", help="run a provider-free authority and recovery walkthrough",
    )
    tour.add_argument(
        "--offline", action="store_true",
        help="use the deterministic built-in adapter and a temporary workspace",
    )
    tour.add_argument("--json", action="store_true")
    tour.set_defaults(func=_cmd_tour)

    migrate = sub.add_parser(
        "migrate", help="plan and apply explicit workspace schema migrations",
    )
    migrate_sub = migrate.add_subparsers(dest="migrate_command", required=True)

    def add_migration_home(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--home", help="runtime state directory (default: LIPAS_HOME or ~/.lipas)",
        )

    migrate_plan = migrate_sub.add_parser(
        "plan", help="inspect legacy databases without changing them",
    )
    add_migration_home(migrate_plan)
    migrate_plan.add_argument("--json", action="store_true")
    migrate_plan.set_defaults(func=_cmd_migrate_plan)

    migrate_apply = migrate_sub.add_parser(
        "apply", help="back up legacy databases and build workspace.db",
    )
    add_migration_home(migrate_apply)
    migrate_apply.add_argument("--yes", action="store_true")
    migrate_apply.set_defaults(func=_cmd_migrate_apply)

    migrate_verify = migrate_sub.add_parser(
        "verify", help="verify the current workspace schema and invariants",
    )
    add_migration_home(migrate_verify)
    migrate_verify.set_defaults(func=_cmd_migrate_verify)

    migrate_rollback = migrate_sub.add_parser(
        "rollback", help="preserve v2 state and reactivate retained v1 files",
    )
    add_migration_home(migrate_rollback)
    migrate_rollback.add_argument("--yes", action="store_true")
    migrate_rollback.set_defaults(func=_cmd_migrate_rollback)

    task = sub.add_parser("task", help="run a durable local workspace task")
    task_sub = task.add_subparsers(dest="task_command", required=True)

    def add_home(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--home",
            help="workbench state directory (default: LIPAS_HOME or ~/.lipas)",
        )

    def add_runner(command: argparse.ArgumentParser) -> None:
        add_home(command)
        command.add_argument(
            "--factory",
            help=(
                "Python factory module:callable; accepted keyword arguments are "
                "tools, session_path, workspace, selected skills, and scenarios"
            ),
        )
        command.add_argument(
            "--model", default=os.environ.get("LIPAS_OLLAMA_MODEL", _DEFAULT_MODEL),
        )
        model_endpoint = command.add_mutually_exclusive_group()
        model_endpoint.add_argument("--host", help="Ollama host")
        model_endpoint.add_argument(
            "--base-url",
            help="OpenAI-compatible API root or full /chat/completions URL",
        )
        add_compatible_credentials(command)
        command.add_argument(
            "--model-streaming",
            action="store_true",
            help="use the endpoint's SSE streaming contract",
        )
        command.add_argument(
            "--max-tokens-field",
            choices=("max_tokens", "max_completion_tokens"),
            default="max_tokens",
        )
        command.add_argument("--timeout", type=float, default=500.0)
        command.add_argument("--phase-timeout", type=float, default=300.0)
        command.add_argument("--lease-seconds", type=float, default=60.0)
        add_skill_options(command)
        command.add_argument(
            "--sandbox",
            choices=("auto", "bwrap", "local"),
            default=os.environ.get("LIPAS_SANDBOX", "auto"),
            help=(
                "command isolation: auto/bwrap fail closed without Bubblewrap; "
                "local is an explicit trusted-code fallback"
            ),
        )

    task_start = task_sub.add_parser(
        "start", help="create and execute a workspace task",
    )
    task_start.add_argument("workspace")
    task_start.add_argument("goal")
    add_runner(task_start)
    task_start.set_defaults(func=_cmd_task_start)

    task_submit = task_sub.add_parser(
        "submit", help="persist a workspace task for a background worker",
    )
    task_submit.add_argument("workspace")
    task_submit.add_argument("goal")
    add_home(task_submit)
    task_submit.set_defaults(func=_cmd_task_submit)

    task_worker = task_sub.add_parser(
        "worker", help="run queued/recoverable tasks with bounded concurrency",
    )
    add_runner(task_worker)
    task_worker.add_argument(
        "--max-concurrency", type=int, default=2,
        help="maximum Tasks executing at once (default: 2)",
    )
    task_worker.add_argument(
        "--poll-interval", type=float, default=1.0,
        help="seconds between idle queue polls (default: 1)",
    )
    task_worker.add_argument(
        "--retry-delay", type=float, default=5.0,
        help="in-memory delay after worker/setup errors (default: 5)",
    )
    task_worker.add_argument(
        "--once", action="store_true",
        help="drain currently discoverable work and exit",
    )
    task_worker.set_defaults(func=_cmd_task_worker)

    task_list = task_sub.add_parser("list", help="list durable tasks")
    add_home(task_list)
    task_list.set_defaults(func=_cmd_task_list)

    task_show = task_sub.add_parser("show", help="show task, runs and approvals")
    task_show.add_argument("task_id")
    add_home(task_show)
    task_show.set_defaults(func=_cmd_task_show)

    task_approvals = task_sub.add_parser(
        "approvals", help="show the durable approval inbox",
    )
    task_approvals.add_argument(
        "--all", action="store_true", help="include resolved approvals",
    )
    task_approvals.add_argument("--json", action="store_true")
    add_home(task_approvals)
    task_approvals.set_defaults(func=_cmd_task_approvals)

    task_resume = task_sub.add_parser("resume", help="resume an interrupted task")
    task_resume.add_argument("task_id")
    add_runner(task_resume)
    task_resume.set_defaults(func=_cmd_task_resume)

    task_approve = task_sub.add_parser(
        "approve", help="approve one pending operation and resume",
    )
    task_approve.add_argument("approval_id")
    task_approve.add_argument(
        "--defer-resume", action="store_true",
        help="queue the approved Run for a worker instead of resuming here",
    )
    add_runner(task_approve)
    task_approve.set_defaults(func=_cmd_task_approve)

    task_deny = task_sub.add_parser("deny", help="deny one pending operation")
    task_deny.add_argument("approval_id")
    task_deny.add_argument("--reason")
    add_home(task_deny)
    task_deny.set_defaults(func=_cmd_task_deny)

    task_cancel = task_sub.add_parser("cancel", help="cancel a task")
    task_cancel.add_argument("task_id")
    add_home(task_cancel)
    task_cancel.set_defaults(func=_cmd_task_cancel)

    task_report = task_sub.add_parser("report", help="show the evidence report")
    task_report.add_argument("task_id")
    task_report.add_argument("--json", action="store_true")
    add_home(task_report)
    task_report.set_defaults(func=_cmd_task_report)

    task_diff = task_sub.add_parser(
        "diff", help="show the staged ChangeSet without modifying the workspace",
    )
    task_diff.add_argument("task_id")
    add_home(task_diff)
    task_diff.set_defaults(func=_cmd_task_diff)

    task_apply = task_sub.add_parser(
        "apply", help="apply a staged ChangeSet after baseline validation",
    )
    task_apply.add_argument("task_id")
    add_home(task_apply)
    task_apply.set_defaults(func=_cmd_task_apply)

    task_discard = task_sub.add_parser(
        "discard", help="discard a staged ChangeSet without changing the workspace",
    )
    task_discard.add_argument("task_id")
    add_home(task_discard)
    task_discard.set_defaults(func=_cmd_task_discard)

    task_events = task_sub.add_parser(
        "events", help="stream-friendly JSONL task event history",
    )
    task_events.add_argument("task_id")
    add_home(task_events)
    task_events.set_defaults(func=_cmd_task_events)

    def add_gateway(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--factory", required=True,
            help="module:callable returning Tool, ToolRegistry, or iterable of Tool",
        )
        command.add_argument(
            "--session", required=True,
            help="SQLite Effect/audit session",
        )
        command.add_argument("--timeout", type=float, default=300.0)
        command.add_argument(
            "--allow-writes", action="store_true",
            help="operator grants this gateway authority to execute write tools",
        )

    action = sub.add_parser(
        "action", help="experimental action-gateway compatibility commands",
    )
    action_sub = action.add_subparsers(dest="action_command", required=True)

    action_call = action_sub.add_parser("call", help="call one audited tool")
    add_gateway(action_call)
    action_call.add_argument("--tool", required=True)
    action_call.add_argument("--arguments", default="{}")
    action_call.add_argument("--request-id", required=True)
    action_call.add_argument(
        "--approved", action="store_true",
        help="this individual call has already received trusted approval",
    )
    action_call.set_defaults(func=_cmd_action_call)

    action_openclaw = action_sub.add_parser(
        "openclaw", help="execute an OpenClaw/OpenCrew JSON action envelope",
    )
    add_gateway(action_openclaw)
    action_openclaw.add_argument("--payload", help="JSON; defaults to stdin")
    action_openclaw.add_argument(
        "--trust-caller-approval", action="store_true",
        help="accept payload approved=true from an authenticated host",
    )
    action_openclaw.set_defaults(func=_cmd_action_openclaw)

    action_manifest = action_sub.add_parser(
        "manifest", help="print the OpenClaw/OpenCrew shim manifest",
    )
    add_gateway(action_manifest)
    action_manifest.set_defaults(func=_cmd_action_manifest)

    mcp = sub.add_parser(
        "mcp", help="experimental: serve LIPAS tools over standard MCP",
    )
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_sub.add_parser("serve", help="run the stdio MCP server")
    add_gateway(mcp_serve)
    mcp_serve.set_defaults(func=_cmd_mcp_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if hasattr(args, "base_url"):
            _validate_model_endpoint_args(args)
        return args.func(args)
    except (ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
