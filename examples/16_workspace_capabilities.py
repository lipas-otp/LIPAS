"""Lesson 16 — compose bounded workspace capabilities offline.

Run::

    python -m examples.16_workspace_capabilities

This lesson is intentionally provider-free.  It shows the small, composable
capabilities that a Workbench exposes for a coding/document task: CSV
profiling, arithmetic, Markdown conversion, archive inspection/extraction,
atomic file writing, and a scoped KnowledgeStore.  The same tools can be
passed to an Agent; invoking them directly here keeps the example deterministic
and runnable on a fresh checkout.
"""
from __future__ import annotations

import asyncio
import tempfile
import zipfile
from pathlib import Path

from lipas import KnowledgeStore, Workbench


def run_demo(root: Path) -> None:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "metrics.csv").write_text(
        "name,value\nalpha,10\nbeta,20\n", encoding="utf-8",
    )
    (workspace / "brief.md").write_text(
        "# Weekly brief\n\nThe release is ready for review.\n", encoding="utf-8",
    )
    with zipfile.ZipFile(workspace / "notes.zip", "w") as archive:
        archive.writestr("notes/todo.txt", "review the release\n")

    with Workbench(root / "workbench", sandbox="local") as workbench:
        task, run = workbench.create_task("Profile and prepare the brief", workspace)
        tools = {value.name: value for value in workbench.workspace_tools(task.id, run.id)}

        profile = tools["analyze_csv"].invoke(relative_path="metrics.csv")
        arithmetic = tools["calculate"].invoke(expression="10 * 2 + 1")
        converted = asyncio.run(tools["convert_workspace_file"].acall({
            "source_path": "brief.md",
            "destination_path": "brief.html",
        }))
        inspected = tools["inspect_archive"].invoke(relative_path="notes.zip")
        extracted = asyncio.run(tools["extract_archive"].acall({
            "relative_path": "notes.zip",
            "destination_path": "unpacked",
        }))
        written = asyncio.run(tools["write_workspace_file"].acall({
            "relative_path": "summary.txt",
            "content": f"mean={profile['numeric']['value']['mean']}\n",
        }))

        with KnowledgeStore(root / "knowledge.db") as knowledge:
            knowledge.ingest(
                "brief.md",
                (workspace / "brief.md").read_text(encoding="utf-8"),
                scope="demo",
            )
            hits = knowledge.search("release review", scope="demo")

        print("csv rows:", profile["rows"])
        print("calculation:", arithmetic["result"])
        print("converted:", converted["destination_path"])
        print("archive members:", inspected["member_count"])
        print("extracted:", extracted["destination_path"])
        print("written:", written["path"])
        print("knowledge hits:", len(hits))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="lipas-capabilities-") as directory:
        run_demo(Path(directory))


if __name__ == "__main__":
    main()
