"""First local-workspace product slice: policy, approval, evidence and CLI."""
from __future__ import annotations

import asyncio
import re
import shutil
import sys
from pathlib import Path

import pytest

from lipas import Agent, RunSuspended
from lipas.adapter import Reply, Usage
from lipas.cli import main
from lipas.workbench import (
    Workbench, WorkspacePolicyError, workbench_approval_policy,
)
from tests.fake_adapter import FakeAdapter


def _tool_reply(name: str, arguments: dict, call_id: str) -> Reply:
    return Reply(
        content=({
            "type": "tool_use", "id": call_id,
            "name": name, "input": arguments,
        },),
        usage=Usage(input=1, output=1),
        stop_reason="tool_use",
        model="fake",
    )


def _final_reply() -> Reply:
    return Reply(
        content=({
            "type": "text",
            "text": "Updated note.txt and verified the workspace tests.",
        },),
        usage=Usage(input=1, output=1),
        stop_reason="end_turn",
        model="fake",
    )


def _workspace(root: Path) -> None:
    (root / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8",
    )


def _complete_control_run(workbench: Workbench, run_id: str) -> None:
    claimed = workbench.execution.claim_run(run_id)
    workbench.execution.complete_run(
        run_id, claimed.lease_token, result={},  # type: ignore[arg-type]
    )


def test_execution_scope_is_run_scoped_stable_and_failure_atomic(
    tmp_path, monkeypatch,
):
    from lipas import workbench as workbench_module
    from lipas.session import open_session

    rowset = open_session(tmp_path / "claims.db")
    try:
        with Workbench(tmp_path / "home", sandbox="local") as workbench:
            _, run = workbench.create_task("scope", tmp_path)
            previous = workbench.execution
            with workbench.execution_scope(rowset, run_id=run.id) as scoped:
                assert scoped is not previous
                assert workbench.execution is previous
                assert scoped.get_run(run.id) is not None
            assert workbench.execution is previous

            class ScopeFailure(RuntimeError):
                pass

            def fail_replacement(*_args, **_kwargs):
                raise ScopeFailure("cannot open scope")

            monkeypatch.setattr(
                workbench_module, "ExecutionStore", fail_replacement,
            )
            with pytest.raises(ScopeFailure, match="cannot open scope"):
                with workbench.execution_scope(rowset, run_id=run.id):
                    pass
            assert workbench.execution is previous
            assert workbench.execution.get_run(run.id) is not None
    finally:
        rowset.store.close()


def test_workspace_capabilities_reject_escape_and_record_evidence(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    _workspace(workspace)
    (workspace / ".env").write_text("API_KEY=sk-verysecretvalue\n", encoding="utf-8")
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        task, run = workbench.create_task("update a note", workspace)
        tools = {value.name: value for value in workbench.workspace_tools(task.id, run.id)}

        info = tools["get_workspace_info"].invoke()
        assert info["current_working_directory"] == str(workspace.resolve())
        assert info["selected_workspace"] == str(workspace.resolve())
        assert info["filesystem_capability"] == "read_write"
        matches = tools["search_workspace"].invoke(query="test_ok")
        assert matches[0]["path"] == "test_sample.py"
        assert matches[0]["line"] == 1

        with pytest.raises(WorkspacePolicyError):
            tools["read_workspace_file"].invoke(relative_path="../outside/secret")
        with pytest.raises(WorkspacePolicyError):
            asyncio.run(tools["write_workspace_file"].acall({
                "relative_path": ".git/config", "content": "unsafe",
            }))
        with pytest.raises(WorkspacePolicyError):
            tools["read_workspace_file"].invoke(relative_path=".env")
        assert ".env" not in tools["list_workspace_files"].invoke(relative_path=".")
        with pytest.raises(WorkspacePolicyError):
            asyncio.run(tools["run_workspace_command"].acall({
                "argv": ["sh", "-c", "echo unsafe"], "timeout_seconds": 1,
            }))

        written = asyncio.run(tools["write_workspace_file"].acall({
            "relative_path": "note.txt", "content": "safe\n",
        }))
        assert written["path"] == "note.txt"
        artifacts = workbench.artifacts(task.id)
        assert len(artifacts) == 1
        assert artifacts[0].kind == "file_write"
        assert artifacts[0].metadata["before_sha256"] is None

        with pytest.raises(WorkspacePolicyError, match="must be a string"):
            tools["read_workspace_file"].invoke(relative_path=None)
        with pytest.raises(WorkspacePolicyError, match="content must be a string"):
            asyncio.run(tools["write_workspace_file"].acall({
                "relative_path": "bad.txt", "content": 123,
            }))


def test_document_conversion_is_bounded_and_records_evidence(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.md").write_text(
        "# Title\n\nA short paragraph.", encoding="utf-8",
    )
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        task, run = workbench.create_task("convert note", workspace)
        tools = {value.name: value for value in workbench.workspace_tools(task.id, run.id)}
        result = asyncio.run(tools["convert_workspace_file"].acall({
            "source_path": "note.md",
            "destination_path": "out.html",
        }))
        assert result["source_format"] == "md"
        assert result["target_format"] == "html"
        assert "<h1>Title</h1>" in (workspace / "out.html").read_text(encoding="utf-8")
        artifacts = workbench.artifacts(task.id)
        conversion = next(value for value in artifacts if value.kind == "document_conversion")
        assert conversion.path == "out.html"
        assert conversion.metadata["source_path"] == "note.md"


def test_pdf_tool_reports_optional_dependency_and_never_reads_outside_workspace(
    tmp_path, monkeypatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.pdf").write_bytes(b"%PDF-not-a-real-document")
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        task, run = workbench.create_task("read PDF", workspace)
        tools = {value.name: value for value in workbench.workspace_tools(task.id, run.id)}
        monkeypatch.setitem(sys.modules, "pypdf", None)
        with pytest.raises(WorkspacePolicyError, match=r"lipas\[documents\]"):
            tools["read_pdf"].invoke(relative_path="sample.pdf")
        with pytest.raises(WorkspacePolicyError):
            tools["read_pdf"].invoke(relative_path="../outside.pdf")


def test_document_outputs_report_optional_dependencies(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("hello\n", encoding="utf-8")
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        task, run = workbench.create_task("office output", workspace)
        tools = {value.name: value for value in workbench.workspace_tools(task.id, run.id)}
        monkeypatch.setitem(sys.modules, "docx", None)
        with pytest.raises(WorkspacePolicyError, match=r"lipas\[documents\]"):
            asyncio.run(tools["convert_workspace_file"].acall({
                "source_path": "note.txt",
                "destination_path": "note.docx",
            }))
        monkeypatch.setitem(sys.modules, "openpyxl", None)
        with pytest.raises(WorkspacePolicyError, match=r"lipas\[documents\]"):
            asyncio.run(tools["convert_workspace_file"].acall({
                "source_path": "note.txt",
                "destination_path": "note.xlsx",
            }))
        monkeypatch.setitem(sys.modules, "pptx", None)
        (workspace / "slides.pptx").write_bytes(b"not-a-pptx")
        with pytest.raises(WorkspacePolicyError, match=r"lipas\[documents\]"):
            asyncio.run(tools["convert_workspace_file"].acall({
                "source_path": "slides.pptx",
                "destination_path": "slides.txt",
            }))


def test_new_runs_receive_distinct_claim_sessions(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        _, first = workbench.create_task("first", workspace)
        _, second = workbench.create_task("second", workspace)
        first_path = workbench.claims_path_for_run(first.id)
        second_path = workbench.claims_path_for_run(second.id)

        assert first_path != second_path
        assert first_path.parent.name == first.id
        assert second_path.parent.name == second.id

    with Workbench(tmp_path / "home", sandbox="local") as reopened:
        assert reopened.claims_path_for_run(first.id) == first_path
        assert reopened.claims_path_for_run(second.id) == second_path


def test_workbench_rejects_cross_task_evidence_and_event_id_conflicts(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        first_task, first_run = workbench.create_task("first", workspace)
        second_task, second_run = workbench.create_task("second", workspace)

        with pytest.raises(WorkspacePolicyError, match="does not belong"):
            workbench.workspace_tools(first_task.id, second_run.id)
        with pytest.raises(WorkspacePolicyError, match="does not belong"):
            workbench.add_artifact(
                task_id=second_task.id,
                run_id=first_run.id,
                kind="invalid",
            )

        event = workbench.add_event(
            task_id=first_task.id,
            run_id=first_run.id,
            kind="stable",
            data={"value": 1},
            event_id="stable-event",
        )
        assert workbench.add_event(
            task_id=first_task.id,
            run_id=first_run.id,
            kind="stable",
            data={"value": 1},
            event_id="stable-event",
        ) == event
        with pytest.raises(WorkspacePolicyError, match="different data"):
            workbench.add_event(
                task_id=first_task.id,
                run_id=first_run.id,
                kind="stable",
                data={"value": 2},
                event_id="stable-event",
            )


def test_checkpointed_unmapped_run_uses_legacy_claim_session(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        task = workbench.execution.create_task("legacy", workspace)
        run = workbench.execution.create_run(task.id)
        claimed = workbench.execution.claim_run(run.id)
        workbench.execution.save_checkpoint(
            run.id,
            claimed.lease_token,  # type: ignore[arg-type]
            expected_version=0,
            phase="ready",
            state={"legacy": True},
        )

        assert workbench.claims_path_for_run(run.id) == workbench.claims_path


def test_change_set_stages_diff_and_applies_without_early_workspace_write(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("before\n", encoding="utf-8")
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        task, run = workbench.create_task(
            "update note", workspace, isolate_changes=True,
        )
        tools = {value.name: value for value in workbench.workspace_tools(task.id, run.id)}
        write = tools["write_workspace_file"]
        arguments = {"relative_path": "note.txt", "content": "after\n"}

        assert workbench.approval_policy(task.id)(write, arguments) is None
        asyncio.run(write.acall(arguments))
        assert (workspace / "note.txt").read_text(encoding="utf-8") == "before\n"
        assert "+after" in workbench.change_set_diff(task.id)
        assert "M note.txt" in tools["git_status"].invoke()
        assert "+after" in tools["git_diff"].invoke()
        with pytest.raises(WorkspacePolicyError, match="only after"):
            workbench.apply_change_set(task.id)

        _complete_control_run(workbench, run.id)
        workbench.prepare_change_set(task.id)
        assert workbench.apply_change_set(task.id) == ("note.txt",)
        assert (workspace / "note.txt").read_text(encoding="utf-8") == "after\n"
        assert workbench.change_set(task.id).state == "applied"  # type: ignore[union-attr]


def test_change_set_snapshot_rejects_a_source_file_race(tmp_path, monkeypatch):
    from lipas import workbench as workbench_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "note.txt"
    source.write_text("before\n", encoding="utf-8")
    original_copy = workbench_module.shutil.copy2

    def copy_then_mutate(source_path, destination_path):
        result = original_copy(source_path, destination_path)
        Path(source_path).write_text("changed concurrently\n", encoding="utf-8")
        return result

    monkeypatch.setattr(workbench_module.shutil, "copy2", copy_then_mutate)
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        task, run = workbench.create_task("snapshot", workspace)
        with pytest.raises(WorkspacePolicyError, match="changed during snapshot"):
            workbench.create_change_set(task.id, run.id)
        assert workbench.change_set(task.id) is None


def test_change_set_apply_rejects_a_staged_file_race(tmp_path, monkeypatch):
    from lipas import workbench as workbench_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = workspace / "note.txt"
    original.write_text("before\n", encoding="utf-8")
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        task, run = workbench.create_task(
            "update", workspace, isolate_changes=True,
        )
        change_set = workbench.change_set(task.id)
        assert change_set is not None
        staged = Path(change_set.stage_root) / "note.txt"
        staged.write_text("after\n", encoding="utf-8")
        _complete_control_run(workbench, run.id)
        workbench.prepare_change_set(task.id)

        original_manifest = workbench_module._stage_manifest
        calls = 0

        def manifest_then_mutate(root):
            nonlocal calls
            result = original_manifest(root)
            calls += 1
            if calls == 2:
                staged.write_text("raced\n", encoding="utf-8")
            return result

        monkeypatch.setattr(
            workbench_module, "_stage_manifest", manifest_then_mutate,
        )
        with pytest.raises(WorkspacePolicyError, match="staged file changed"):
            workbench.apply_change_set(task.id)
        assert original.read_text(encoding="utf-8") == "before\n"


def test_change_set_fails_closed_on_workspace_drift(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    note = workspace / "note.txt"
    note.write_text("baseline\n", encoding="utf-8")
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        task, run = workbench.create_task(
            "update note", workspace, isolate_changes=True,
        )
        tools = {value.name: value for value in workbench.workspace_tools(task.id, run.id)}
        asyncio.run(tools["write_workspace_file"].acall({
            "relative_path": "note.txt", "content": "staged\n",
        }))
        _complete_control_run(workbench, run.id)
        workbench.prepare_change_set(task.id)
        note.write_text("external edit\n", encoding="utf-8")

        with pytest.raises(WorkspacePolicyError, match="workspace drift"):
            workbench.apply_change_set(task.id)
        assert note.read_text(encoding="utf-8") == "external edit\n"


def test_change_set_apply_resumes_when_one_file_already_matches_desired(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("a.txt", "b.txt"):
        (workspace / name).write_text("before\n", encoding="utf-8")
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        task, run = workbench.create_task(
            "update two files", workspace, isolate_changes=True,
        )
        write = {
            value.name: value for value in workbench.workspace_tools(task.id, run.id)
        }["write_workspace_file"]
        for name in ("a.txt", "b.txt"):
            asyncio.run(write.acall({
                "relative_path": name, "content": f"after {name}\n",
            }))
        _complete_control_run(workbench, run.id)
        value = workbench.prepare_change_set(task.id)
        assert value is not None
        shutil.copy2(Path(value.stage_root) / "a.txt", workspace / "a.txt")

        assert workbench.apply_change_set(task.id) == ("a.txt", "b.txt")
        assert (workspace / "a.txt").read_text(encoding="utf-8") == "after a.txt\n"
        assert (workspace / "b.txt").read_text(encoding="utf-8") == "after b.txt\n"


def test_discarded_change_set_leaves_workspace_unchanged(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        task, run = workbench.create_task(
            "create note", workspace, isolate_changes=True,
        )
        write = {
            value.name: value for value in workbench.workspace_tools(task.id, run.id)
        }["write_workspace_file"]
        asyncio.run(write.acall({
            "relative_path": "note.txt", "content": "discard me\n",
        }))
        stage = Path(workbench.change_set(task.id).stage_root)  # type: ignore[union-attr]

        workbench.execution.cancel_task(task.id)
        workbench.discard_change_set(task.id)
        assert not stage.exists()
        assert not (workspace / "note.txt").exists()
        assert workbench.change_set(task.id).state == "discarded"  # type: ignore[union-attr]


def test_change_set_excludes_secret_content_before_snapshot_persistence(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "ordinary.txt").write_text("safe\n", encoding="utf-8")
    (workspace / "config.txt").write_text(
        "API_KEY=sk-supersecretvalue123\n", encoding="utf-8",
    )
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        task, _ = workbench.create_task(
            "inspect safely", workspace, isolate_changes=True,
        )
        value = workbench.change_set(task.id)
        assert value is not None

        assert (Path(value.stage_root) / "ordinary.txt").is_file()
        assert not (Path(value.stage_root) / "config.txt").exists()
        created = next(
            event for event in workbench.events(task.id)
            if event.kind == "change_set_created"
        )
        assert created.data["excluded_secret_files"] == 1


def test_workbench_durable_approval_to_verified_report(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _workspace(workspace)
    with Workbench(tmp_path / "home", sandbox="local") as workbench:
        task, run = workbench.create_task("write and verify a note", workspace)
        agent = Agent(
            adapter=FakeAdapter.from_replies([
                _tool_reply(
                    "write_workspace_file",
                    {"relative_path": "note.txt", "content": "done\n"},
                    "write-call",
                ),
                _tool_reply(
                    "run_workspace_command",
                    {"argv": ["pytest", "-q"], "timeout_seconds": 30},
                    "verify-call",
                ),
                _final_reply(),
            ]),
            model="fake",
            tools=workbench.workspace_tools(task.id, run.id),
            session_path=workbench.claims_path_for_run(run.id),
        )
        try:
            with workbench.execution_scope(agent.rowset, run_id=run.id) as execution:
                with pytest.raises(RunSuspended) as write_approval:
                    asyncio.run(agent.run_durable(
                        task.goal, execution_store=execution,
                        run_id=run.id, approval_policy=workbench_approval_policy,
                    ))
                write_request = write_approval.value.interrupt.request
                assert write_request["arguments"]["content"]["redacted"] is True
                assert "done" not in str(write_request)
                workbench.record_approval_required(write_approval.value.interrupt)
                workbench.resolve_approval(
                    write_approval.value.interrupt.id, allow=True,
                    response={"by": "test"},
                )
                with pytest.raises(RunSuspended) as command_approval:
                    asyncio.run(agent.resume_durable(
                        execution_store=execution, run_id=run.id,
                        approval_policy=workbench_approval_policy,
                    ))
                workbench.record_approval_required(command_approval.value.interrupt)
                workbench.resolve_approval(
                    command_approval.value.interrupt.id, allow=True,
                    response={"by": "test"},
                )
                result = asyncio.run(agent.resume_durable(
                    execution_store=execution, run_id=run.id,
                    approval_policy=workbench_approval_policy,
                ))
        finally:
            agent.close()

        report = workbench.build_report(task.id, result)
        assert report.status == "completed"
        assert report.changed_files == ("note.txt",)
        assert report.verified
        assert report.verifications[0].passed
        assert report.verifications[0].sandbox == "local"
        assert not report.verifications[0].isolated
        assert "verification ran without an OS isolation boundary" in report.unresolved_risks
        assert "verification ran without network egress isolation" in report.unresolved_risks
        assert (workspace / "note.txt").read_text(encoding="utf-8") == "done\n"
        kinds = [event.kind for event in workbench.events(task.id)]
        assert kinds[:2] == ["task_created", "run_created"]
        assert kinds.count("approval_required") == 2
        assert kinds.count("approval_resolved") == 2
        assert "artifact_created" in kinds
        assert "verification_recorded" in kinds
        assert "report_created" in kinds


def test_task_cli_stages_then_applies_verified_change(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _workspace(workspace)
    home = tmp_path / "home"
    replies = iter([
        _tool_reply(
            "write_workspace_file",
            {"relative_path": "note.txt", "content": "from cli\n"},
            "write-cli",
        ),
        _tool_reply(
            "run_workspace_command",
            {"argv": ["pytest", "-q"], "timeout_seconds": 30},
            "verify-cli",
        ),
        _final_reply(),
    ])

    def fake_agent(_args, workbench, *, task_id, run_id):
        return Agent(
            adapter=FakeAdapter.from_handler(lambda _request: next(replies)),
            model="fake",
            tools=workbench.workspace_tools(task_id, run_id),
            session_path=workbench.claims_path_for_run(run_id),
        )

    monkeypatch.setattr("lipas.cli._workbench_agent", fake_agent)
    assert main([
        "task", "start", str(workspace), "write and verify", "--home", str(home),
        "--sandbox", "local",
    ]) == 0
    first = capsys.readouterr().out
    task_id = re.search(r"created task (task_[a-f0-9]+)", first).group(1)  # type: ignore[union-attr]
    approval = re.search(r"approval (approval_[a-f0-9]+)", first).group(1)  # type: ignore[union-attr]

    assert main([
        "task", "approve", approval, "--home", str(home), "--sandbox", "local",
    ]) == 0
    completed = capsys.readouterr().out
    assert "status: completed" in completed
    assert "verified: yes" in completed
    assert "delivery: ready" in completed
    assert "original workspace is unchanged" in completed
    assert not (workspace / "note.txt").exists()

    assert main(["task", "diff", task_id, "--home", str(home)]) == 0
    assert "+from cli" in capsys.readouterr().out
    assert main(["task", "apply", task_id, "--home", str(home)]) == 0
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "from cli\n"

    assert main(["task", "report", task_id, "--home", str(home)]) == 0
    report_output = capsys.readouterr().out
    assert "changed files: note.txt" in report_output
    assert "delivery: applied" in report_output
    assert "original workspace is unchanged" not in report_output
    assert main(["task", "events", task_id, "--home", str(home)]) == 0
    events = capsys.readouterr().out
    assert '"kind": "approval_required"' in events
    assert '"kind": "report_created"' in events
    assert '"kind": "change_set_ready"' in events
    assert '"kind": "change_set_applied"' in events
