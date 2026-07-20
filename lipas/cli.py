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
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ._version import __version__
from .agent import Agent
from .adapter.errors import DEFAULT_POLICY, ErrorKind, RetryPolicy
from .rows.effect import EffectRow
from .session import open_session
from .trace import render_trace, write_jsonl
from .execution import InterruptState, RunState, RunSuspended
from .dispatcher import DispatchOutcome, TaskDispatcher
from .workbench import TaskReport, Workbench
from .gateway import ActionGateway, result_json
from .integrations import MCPActionServer, OpenClawActionBackend
from .tools import Tool, ToolRegistry

__all__ = ["main"]

_DEFAULT_MODEL = "gemma4:12b"
_DEFAULT_INSTRUCTIONS = (
    "You are a concise local LIPAS demo. You have no web, weather, or other "
    "live-data tools unless the user supplied them through an Agent factory. "
    "Say that limitation plainly instead of implying that you can fetch data."
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


async def _run_with_feedback(agent: Agent, prompt: str, state: Any) -> Any:
    if not sys.stderr.isatty():
        return await agent.run(prompt, state=state)
    spinner = asyncio.create_task(_spinner())
    try:
        return await agent.run(prompt, state=state)
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


def _factory(spec: str) -> Agent:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("factory must use module:callable, e.g. agent:build_agent")
    factory: Callable[[], Any] = getattr(_import_local_module(module_name), attribute)
    agent = factory()
    if not isinstance(agent, Agent):
        raise TypeError(f"factory {spec!r} returned {type(agent).__name__}, expected Agent")
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
Inspect before editing. Use only the supplied workspace tools. Keep changes scoped to
the user's goal. File writes stay in a staging workspace; commands require approval.
The user applies or discards the complete ChangeSet after your Run. After editing,
run the most relevant available verification command. In the final answer state what
was staged, what was verified, and any remaining uncertainty. Never claim delivery
to the original workspace or verification that a tool result does not show."""


def _workbench_home(value: str | None) -> Path:
    configured = value or os.environ.get("LIPAS_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".lipas"


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
    if args.factory:
        factory = _factory_callable(args.factory)
        parameters = inspect.signature(factory).parameters
        kwargs = {
            "tools": tools,
            "session_path": claims_path,
            "workspace": Path(task.workspace),
        }
        if any(value.kind is inspect.Parameter.VAR_KEYWORD for value in parameters.values()):
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
        return agent
    return Agent.ollama(
        args.model,
        host=args.host,
        timeout_s=args.timeout,
        session=claims_path,
        tools=tools,
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
    workbench.attach_rowset(agent.rowset)
    try:
        try:
            result = await agent.run_durable(
                prompt,
                execution_store=workbench.execution,
                run_id=run_id,
                lease_seconds=args.lease_seconds,
                phase_timeout_s=args.phase_timeout,
                approval_policy=workbench.approval_policy(task_id),
                _claimed_run=claimed_run,
            )
        except RunSuspended as suspended:
            workbench.record_approval_required(suspended.interrupt)
            workbench.record_run_state(run_id)
            request = json.dumps(dict(suspended.interrupt.request), ensure_ascii=False)
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
    with Workbench(_workbench_home(args.home), sandbox=args.sandbox) as workbench:
        task, run = workbench.create_task(
            args.goal, args.workspace, isolate_changes=True,
        )
        print(f"created task {task.id} for {task.workspace}")
        return _execute_task(
            args, workbench, task_id=task.id, run_id=run.id, prompt=task.goal,
        )


def _cmd_task_submit(args: argparse.Namespace) -> int:
    """Persist one Task/Run without tying it to the submitting process."""
    with Workbench(_workbench_home(args.home)) as workbench:
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

    async def execute(task: Any, discovered: Any) -> None:
        with Workbench(home, sandbox=args.sandbox) as workbench:
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

    dispatcher = TaskDispatcher(
        home / "execution.db",
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
    with Workbench(_workbench_home(args.home)) as workbench:
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
    with Workbench(_workbench_home(args.home)) as workbench:
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
    with Workbench(_workbench_home(args.home)) as workbench:
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
    with Workbench(_workbench_home(args.home), sandbox=args.sandbox) as workbench:
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
    with Workbench(_workbench_home(args.home), sandbox=args.sandbox) as workbench:
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
    with Workbench(_workbench_home(args.home)) as workbench:
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
    with Workbench(_workbench_home(args.home)) as workbench:
        workbench.execution.cancel_task(args.task_id)
        runs = workbench.execution.list_runs(task_id=args.task_id)
        if runs:
            workbench.record_run_state(runs[0].id)
        _print_task_report(workbench.build_report(args.task_id))
    return 0


def _cmd_task_report(args: argparse.Namespace) -> int:
    with Workbench(_workbench_home(args.home)) as workbench:
        report = workbench.get_report(args.task_id)
        if report is None:
            report = workbench.build_report(args.task_id).as_dict()
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            _print_task_report(report)
    return 0


def _cmd_task_diff(args: argparse.Namespace) -> int:
    with Workbench(_workbench_home(args.home)) as workbench:
        value = workbench.change_set(args.task_id)
        if value is None:
            raise ValueError(f"task {args.task_id!r} has no staged ChangeSet")
        print(workbench.change_set_diff(args.task_id), end="")
    return 0


def _cmd_task_apply(args: argparse.Namespace) -> int:
    with Workbench(_workbench_home(args.home)) as workbench:
        paths = workbench.apply_change_set(args.task_id)
    print(
        f"applied task {args.task_id}: "
        f"{', '.join(paths) if paths else '(no file changes)'}",
    )
    return 0


def _cmd_task_discard(args: argparse.Namespace) -> int:
    with Workbench(_workbench_home(args.home)) as workbench:
        workbench.discard_change_set(args.task_id)
    print(f"discarded staged changes for task {args.task_id}")
    return 0


def _cmd_task_events(args: argparse.Namespace) -> int:
    with Workbench(_workbench_home(args.home)) as workbench:
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


async def _chat(agent: Agent, *, once: str | None) -> None:
    state = None
    prompt = once
    editor = _prompt_session() if once is None else None
    while True:
        if prompt is None:
            try:
                prompt = (await _prompt(editor)).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
        if prompt in {":q", ":quit", ":exit"}:
            return
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
        result = await _run_with_feedback(agent, prompt, state)
        state = result.state
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
    if args.factory:
        agent = _factory(args.factory)
    else:
        retry_policy = dict(DEFAULT_POLICY)
        for kind in (ErrorKind.TIMEOUT, ErrorKind.NETWORK):
            retry_policy[kind] = RetryPolicy(
                should_retry=args.retries > 0,
                base_delay_s=1.0,
                max_attempts=args.retries + 1,
            )
        agent = Agent.ollama(
            args.model,
            host=args.host,
            timeout_s=args.timeout,
            instructions=args.instructions,
            session=args.session,
            harness_kwargs={"retry_policy": retry_policy},
        )
    try:
        with _chat_transport_logs(args.verbose):
            asyncio.run(_chat(agent, once=args.once))
    finally:
        agent.close()
    return 0


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

    init = sub.add_parser("init", help="create a minimal ordinary-Python agent")
    init.add_argument("directory")
    init.add_argument("--model", default=os.environ.get("LIPAS_OLLAMA_MODEL", _DEFAULT_MODEL))
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=_cmd_init)

    chat = sub.add_parser("chat", help="try an Agent interactively")
    chat.add_argument("--model", default=os.environ.get("LIPAS_OLLAMA_MODEL", _DEFAULT_MODEL))
    chat.add_argument("--host", help="Ollama host; defaults to OLLAMA_HOST or localhost:11434")
    chat.add_argument("--timeout", type=float, default=500.0, help="local Ollama response timeout in seconds")
    chat.add_argument("--retries", type=int, default=0, help="extra local timeout/network retries (default: 0)")
    chat.add_argument("--verbose", action="store_true", help="show adapter retry diagnostics")
    chat.add_argument("--instructions", default=_DEFAULT_INSTRUCTIONS)
    chat.add_argument("--session", help="optional SQLite claim-session path")
    chat.add_argument("--factory", help="ordinary Python factory: module:callable")
    chat.add_argument("--once", help="send one prompt instead of opening a REPL")
    chat.set_defaults(func=_cmd_chat)

    trace = sub.add_parser("trace", help="render a durable claim session")
    trace.add_argument("session")
    trace.add_argument("--jsonl", action="store_true")
    trace.set_defaults(func=_cmd_trace)

    effects = sub.add_parser("effects", help="summarize effect lifecycle in a session")
    effects.add_argument("session")
    effects.set_defaults(func=_cmd_effects)

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
                "tools, session_path, and workspace"
            ),
        )
        command.add_argument(
            "--model", default=os.environ.get("LIPAS_OLLAMA_MODEL", _DEFAULT_MODEL),
        )
        command.add_argument("--host", help="Ollama host")
        command.add_argument("--timeout", type=float, default=500.0)
        command.add_argument("--phase-timeout", type=float, default=300.0)
        command.add_argument("--lease-seconds", type=float, default=60.0)
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
        return args.func(args)
    except (ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
