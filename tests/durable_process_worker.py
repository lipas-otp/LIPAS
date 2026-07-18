"""Subprocess worker used to verify recovery after an uncatchable process stop."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import sys

from lipas import Agent, ExecutionStore, tool
from lipas.adapter import Reply, Usage
from tests.fake_adapter import FakeAdapter


def _tool_reply() -> Reply:
    return Reply(
        content=({
            "type": "tool_use",
            "id": "provider-process-write",
            "name": "write_once",
            "input": {"text": "persisted once"},
        },),
        usage=Usage(input=1, output=1),
        stop_reason="tool_use",
        model="process-test",
    )


def _final_reply() -> Reply:
    return Reply(
        content=({"type": "text", "text": "recovered"},),
        usage=Usage(input=1, output=1),
        stop_reason="end_turn",
        model="process-test",
    )


class KillAfterToolResultStore(ExecutionStore):
    """Stop after the Effect result commits but before its checkpoint commits."""

    def save_checkpoint(self, *args, **kwargs):
        if kwargs.get("phase") == "after_tool":
            os.kill(os.getpid(), signal.SIGKILL)
        return super().save_checkpoint(*args, **kwargs)


def main() -> None:
    mode, root_text, run_id = sys.argv[1:]
    root = Path(root_text)
    attempts = root / "write-attempts.log"
    artifact = root / "artifact.txt"

    @tool(side_effect="external_write")
    def write_once(text: str) -> str:
        """Represent a non-repeatable provider write in the child process."""
        with attempts.open("a", encoding="utf-8") as stream:
            stream.write(f"{text}\n")
            stream.flush()
            os.fsync(stream.fileno())
        artifact.write_text(text, encoding="utf-8")
        return text

    adapter = FakeAdapter.from_replies(
        [_tool_reply()] if mode == "crash" else [_final_reply()],
    )
    store_type = KillAfterToolResultStore if mode == "crash" else ExecutionStore
    with store_type(root / "execution.db") as executions:
        with Agent(
            adapter=adapter,
            model="process-test",
            tools=[write_once],
            session_path=root / "claims.db",
        ) as agent:
            if mode == "crash":
                asyncio.run(agent.run_durable(
                    "write exactly once",
                    execution_store=executions,
                    run_id=run_id,
                    lease_seconds=0.05,
                ))
                raise AssertionError("worker should have been killed")
            result = asyncio.run(agent.resume_durable(
                execution_store=executions,
                run_id=run_id,
            ))
            print(result.text)


if __name__ == "__main__":
    main()
