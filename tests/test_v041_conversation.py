"""Independent conversation-kernel and local-Web-operator contract tests."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lipas import (
    AgentEventType,
    LIPASRuntime,
    OperatorAuthenticator,
    SessionConflictError,
    SQLiteSessionStore,
    WorkspaceStorage,
)
from lipas.behaviour import AgentState


def test_message_identity_and_task_promotion_are_idempotent(tmp_path: Path):
    home = tmp_path / "state"
    with LIPASRuntime.open(home, sandbox="local") as runtime:
        conversation = runtime.create_conversation(
            conversation_id="chat-1", title="Work", workspace=tmp_path,
        )
        first = runtime.append_message(
            conversation.id, role="user", content="inspect files", message_id="m-1",
        )
        assert runtime.append_message(
            conversation.id, role="user", content="inspect files", message_id="m-1",
        ) == first
        with pytest.raises(SessionConflictError):
            runtime.append_message(
                conversation.id, role="user", content="changed", message_id="m-1",
            )
        task, run, linked = runtime.promote_message_to_task(conversation.id, "m-1")
        again_task, again_run, again_linked = runtime.promote_message_to_task(
            conversation.id, "m-1",
        )
        assert (again_task.id, again_run.id, again_linked.task_id) == (
            task.id, run.id, linked.task_id,
        )
        assert len(runtime.execution.list_tasks()) == 1
        assert len(runtime.execution.list_runs()) == 1
        assert linked.run_id == run.id


def test_conversation_cursor_catches_up_and_projects_agent_events(tmp_path: Path):
    home = tmp_path / "state"
    with LIPASRuntime.open(home, sandbox="local") as runtime:
        conversation = runtime.create_conversation(workspace=tmp_path)
        runtime.append_message(
            conversation.id, role="user", content="run", message_id="m-1",
        )
        task, run, _ = runtime.promote_message_to_task(conversation.id, "m-1")
        runtime.execution.append_agent_event(
            run.id, AgentEventType.TOOL_STARTED, identity="tool-1",
            data={"name": "ls"},
        )
        runtime.execution.append_agent_event(
            run.id, AgentEventType.TOOL_COMPLETED, identity="tool-2",
            data={"ok": True},
        )
        page = runtime.conversation_events(conversation.id, limit=2)
        assert page.has_more
        assert [event.sequence for event in page.events] == [1, 2]
        next_page = runtime.conversation_events(
            conversation.id, after=page.next_cursor, limit=10,
        )
        assert [event.kind for event in next_page.events] == [
            "agent_event", "agent_event",
        ]
        assert next_page.events[0].run_id == run.id
        assert next_page.events[0].task_id == task.id
        # Re-reading does not advance the conversation's updated timestamp or
        # duplicate the projected events.
        count = len(runtime.sessions.events(conversation.id, limit=100).events)
        runtime.conversation_events(conversation.id, limit=100)
        assert len(runtime.sessions.events(conversation.id, limit=100).events) == count


def test_chat_schema_additive_migration_and_future_version_fail_closed(tmp_path: Path):
    path = tmp_path / "chat.db"
    with SQLiteSessionStore(path) as store:
        store.save("legacy", AgentState(), expected_version=0)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE lipas_conversation_meta SET value='1' WHERE key='schema_version'",
        )
        connection.commit()
    with SQLiteSessionStore(path) as store:
        assert store.create_conversation(workspace=tmp_path).state == "open"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE lipas_conversation_meta SET value='999' WHERE key='schema_version'",
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="schema version mismatch"):
        SQLiteSessionStore(path)


def test_legacy_workspace_migration_preserves_chat_rows(tmp_path: Path):
    home = tmp_path / "legacy-state"
    with SQLiteSessionStore(home / "conversation.db") as store:
        conversation = store.create_conversation(
            conversation_id="legacy-chat", workspace=tmp_path,
        )
        store.append_message(
            conversation.id, role="user", content="keep me", message_id="legacy-msg",
        )
    result = WorkspaceStorage(home).migrate()
    assert result.database_path.is_file()
    with SQLiteSessionStore(home / "workspace.db") as store:
        assert store.get_conversation("legacy-chat") is not None
        assert store.get_message("legacy-msg").content == "keep me"


def test_local_web_conversation_routes_share_runtime_contract(tmp_path: Path):
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        operator = runtime.operator(operator_token="secret")
        created = operator._post(("api", "conversations"), {"title": "Chat"})
        conversation_id = created["conversation"]["id"]
        message = operator._post(
            ("api", "conversations", conversation_id, "messages"),
            {"message_id": "m-1", "role": "user", "content": "make a task"},
        )
        promoted = operator._post(
            (
                "api", "conversations", conversation_id, "messages", "m-1",
                "promote",
            ),
            {},
        )
        assert message["message"]["id"] == "m-1"
        assert promoted["message"]["task_id"] == promoted["task"]["id"]
        events = operator._get(
            ("api", "conversations", conversation_id, "events"),
            {"limit": ["10"]},
        )
        assert [event["kind"] for event in events["events"]] == [
            "message_created", "task_promoted",
        ]
        delta = operator._post(
            ("api", "conversations", conversation_id, "events"),
            {
                "event_id": "delta-1", "kind": "model_delta",
                "payload": {"text": "working"},
            },
        )
        assert delta["events"][0]["kind"] == "model_delta"
        assert "conversations" in operator.snapshot()


def test_task_run_link_cannot_be_reused_by_another_conversation(tmp_path: Path):
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        first = runtime.create_conversation(conversation_id="chat-a")
        second = runtime.create_conversation(conversation_id="chat-b")
        runtime.append_message(first.id, role="user", content="one", message_id="m-a")
        task, run, _ = runtime.promote_message_to_task(first.id, "m-a")
        with pytest.raises(SessionConflictError):
            runtime.append_message(
                second.id, role="assistant", content="duplicate",
                message_id="m-b", task_id=task.id, run_id=run.id,
            )


def test_runtime_rejects_unknown_direct_task_run_links(tmp_path: Path):
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        conversation = runtime.create_conversation()
        with pytest.raises(KeyError, match="linked task/run"):
            runtime.append_message(
                conversation.id,
                role="assistant",
                content="forged",
                task_id="task-missing",
                run_id="run-missing",
            )


def test_session_store_rejects_blank_links_and_cross_conversation_event_refs(tmp_path: Path):
    with SQLiteSessionStore(tmp_path / "chat.db") as store:
        first = store.create_conversation(conversation_id="chat-a", workspace=tmp_path)
        second = store.create_conversation(conversation_id="chat-b", workspace=tmp_path)
        message = store.append_message(first.id, role="user", content="hello", message_id="m")
        with pytest.raises(ValueError, match="task_id"):
            store.append_message(
                first.id, role="assistant", content="bad", task_id=" ", run_id="run",
            )
        with pytest.raises(SessionConflictError, match="another conversation"):
            store.append_event(
                second.id, kind="bad_ref", message_id=message.id, payload={},
            )
        with pytest.raises(KeyError, match="missing"):
            store.append_event(
                first.id, kind="bad_ref", message_id="missing", payload={},
            )


def test_promoted_workspace_cannot_escape_conversation_root(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        conversation = runtime.create_conversation(workspace=workspace)
        runtime.append_message(
            conversation.id, role="user", content="run", message_id="m-escape",
        )
        with pytest.raises(ValueError, match="conversation workspace"):
            runtime.promote_message_to_task(
                conversation.id, "m-escape", workspace=outside,
            )


def test_linked_run_projection_is_not_limited_to_first_messages(tmp_path: Path):
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        conversation = runtime.create_conversation(workspace=tmp_path)
        runtime.append_message(
            conversation.id, role="user", content="run", message_id="m-many",
        )
        task, run, _ = runtime.promote_message_to_task(
            conversation.id, "m-many",
        )
        # More than the old 10,000-message discovery cap.  The linked run is
        # still discovered from the narrow indexed identity query.
        for index in range(10_001):
            runtime.append_message(
                conversation.id,
                role="assistant",
                content=index,
                message_id=f"m-{index}",
            )
        runtime.execution.append_agent_event(
            run.id, AgentEventType.TOOL_COMPLETED, identity="after-many",
            data={"ok": True},
        )
        runtime.conversation_events(conversation.id, limit=1_000)
        cursor = 0
        found = False
        while True:
            page = runtime.sessions.events(
                conversation.id, after=cursor, limit=1_000,
            )
            found = found or any(
                event.run_id == run.id and event.kind == "agent_event"
                for event in page.events
            )
            if not page.has_more:
                break
            cursor = page.next_cursor
        assert found


def test_attachment_upload_is_idempotent_and_path_safe(tmp_path: Path):
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        conversation = runtime.create_conversation(workspace=tmp_path)
        attachment = runtime.save_attachment(
            conversation.id, b"hello", filename="note.txt", attachment_id="a-1",
        )
        assert attachment.size == 5
        assert Path(attachment.path).read_bytes() == b"hello"
        assert runtime.save_attachment(
            conversation.id, b"hello", filename="note.txt", attachment_id="a-1",
        ) == attachment
        loaded, content = runtime.read_attachment("a-1")
        assert loaded == attachment and content == b"hello"
        Path(attachment.path).write_bytes(b"tampered")
        with pytest.raises(RuntimeError, match="integrity"):
            runtime.read_attachment("a-1")
        Path(attachment.path).write_bytes(b"hello")
        with pytest.raises(SessionConflictError):
            runtime.save_attachment(
                conversation.id, b"changed", filename="note.txt", attachment_id="a-1",
            )
        with pytest.raises(ValueError, match="safe path"):
            runtime.save_attachment(conversation.id, b"x", filename="../escape")


def test_operator_sse_cursor_and_authentication_contract(tmp_path: Path):
    with LIPASRuntime.open(tmp_path / "state", sandbox="local") as runtime:
        conversation = runtime.create_conversation()
        runtime.append_message(conversation.id, role="user", content="hello")
        operator = runtime.operator(
            authenticator=OperatorAuthenticator(
                "operator-secret-012345", ttl_s=60,
            ),
            require_authentication=True,
        )
        events = operator._sse(
            ("api", "conversations", conversation.id, "stream"),
            {"after": ["0"], "limit": ["10"]},
        )
        assert events and events[0][0] == "1" and events[0][1] == "message_created"
        uploaded = operator._post(
            ("api", "conversations", conversation.id, "attachments"),
            {"filename": "a.txt", "content_base64": "aGVsbG8="},
        )
        downloaded = operator._get(
            (
                "api", "conversations", conversation.id, "attachments",
                uploaded["attachment"]["id"],
            ),
            {},
        )
        assert downloaded["content_base64"] == "aGVsbG8="
