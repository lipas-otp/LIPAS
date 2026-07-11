"""Small operational CLI for the LIPAS runtime.

The CLI deliberately owns no agent DSL or alternate execution semantics. It
either generates ordinary Python, calls an explicitly supplied Python factory,
or reads the same durable sessions that library users create.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from .agent import Agent
from .adapter import OllamaAdapter
from .rows.effect import EffectRow
from .session import open_session
from .trace import render_trace, write_jsonl

__all__ = ["main"]

_DEFAULT_MODEL = "gemma4:12b"


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
    factory: Callable[[], Any] = getattr(importlib.import_module(module_name), attribute)
    agent = factory()
    if not isinstance(agent, Agent):
        raise TypeError(f"factory {spec!r} returned {type(agent).__name__}, expected Agent")
    return agent


async def _chat(agent: Agent, *, once: str | None) -> None:
    state = None
    prompt = once
    while True:
        if prompt is None:
            try:
                prompt = input("you> ").strip()
            except EOFError:
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
        result = await agent.run(prompt, state=state)
        state = result.state
        print(f"agent> {result.text}")
        if result.is_error:
            print(f"error: {result.error}", file=sys.stderr)
        if once is not None:
            return
        prompt = None


def _cmd_chat(args: argparse.Namespace) -> int:
    if args.factory:
        agent = _factory(args.factory)
    else:
        if args.session:
            Path(args.session).parent.mkdir(parents=True, exist_ok=True)
        agent = Agent(
            adapter=OllamaAdapter(),
            model=args.model,
            instructions=args.instructions,
            session_path=args.session,
        )
    try:
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
        "from lipas import Agent\n"
        "from lipas.adapter import OllamaAdapter\n\n"
        "def build_agent() -> Agent:\n"
        "    return Agent(\n"
        "        adapter=OllamaAdapter(),\n"
        f"        model={args.model!r},\n"
        "        instructions=\"Be concise and state uncertainty.\",\n"
        "        session_path=\"runs/chat.db\",\n"
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
        description="Create, try, and inspect ordinary Python LIPAS agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a minimal ordinary-Python agent")
    init.add_argument("directory")
    init.add_argument("--model", default=os.environ.get("LIPAS_OLLAMA_MODEL", _DEFAULT_MODEL))
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=_cmd_init)

    chat = sub.add_parser("chat", help="try an Agent interactively")
    chat.add_argument("--model", default=os.environ.get("LIPAS_OLLAMA_MODEL", _DEFAULT_MODEL))
    chat.add_argument("--instructions", default="You are a concise, helpful assistant.")
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ImportError, OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
