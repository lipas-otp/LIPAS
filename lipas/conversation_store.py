"""Optimistic durable storage for high-level conversational state."""
from __future__ import annotations

import json
import hashlib
import math
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable

from .behaviour import AgentState
from .serialization import decode, encode, make_default_codec_registry
from .sqlite_storage import connect_sqlite, immediate_transaction

__all__ = [
    "Conversation",
    "ConversationEvent",
    "ConversationEventPage",
    "Message",
    "Attachment",
    "SessionConflictError",
    "SessionSnapshot",
    "SessionStore",
    "SQLiteSessionStore",
]

_SCHEMA_VERSION = 3
_MAX_SQLITE_INT = 2**63 - 1
_CODECS = make_default_codec_registry()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS lipas_conversation_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lipas_conversations (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lipas_conversations_updated
ON lipas_conversations(updated_at DESC, id);

-- Chat resources deliberately live beside Session snapshots in the same
-- SQLite authority.  They are not a second agent state machine: messages
-- are facts, while execution_tasks/execution_runs remain authoritative for
-- work that was explicitly promoted from a message.
CREATE TABLE IF NOT EXISTS lipas_chat_conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    workspace TEXT NOT NULL,
    state TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lipas_chat_conversations_updated
ON lipas_chat_conversations(updated_at DESC, id);

CREATE TABLE IF NOT EXISTS lipas_chat_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES lipas_chat_conversations(id),
    role TEXT NOT NULL,
    kind TEXT NOT NULL,
    content_json TEXT NOT NULL,
    task_id TEXT,
    run_id TEXT,
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(conversation_id, id)
);
CREATE INDEX IF NOT EXISTS idx_lipas_chat_messages_conversation
ON lipas_chat_messages(conversation_id, created_at, id);

CREATE TABLE IF NOT EXISTS lipas_chat_events (
    event_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES lipas_chat_conversations(id),
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    kind TEXT NOT NULL,
    message_id TEXT,
    task_id TEXT,
    run_id TEXT,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(conversation_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_lipas_chat_events_conversation
ON lipas_chat_events(conversation_id, sequence);

CREATE TABLE IF NOT EXISTS lipas_chat_attachments (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES lipas_chat_conversations(id),
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size >= 0),
    sha256 TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lipas_chat_attachments_conversation
ON lipas_chat_attachments(conversation_id, created_at, id);
"""


class SessionConflictError(RuntimeError):
    """A Session snapshot changed after the caller loaded it."""


@dataclass(frozen=True, slots=True)
class Conversation:
    """A durable chat container; execution authority lives elsewhere."""

    id: str
    title: str
    workspace: str
    state: str
    metadata: Mapping[str, Any]
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class Message:
    """One identified conversational fact and its optional work link."""

    id: str
    conversation_id: str
    role: str
    kind: str
    content: Any
    task_id: str | None
    run_id: str | None
    metadata: Mapping[str, Any]
    created_at: float


@dataclass(frozen=True, slots=True)
class Attachment:
    """An immutable, content-addressed conversation attachment."""

    id: str
    conversation_id: str
    filename: str
    mime_type: str
    size: int
    sha256: str
    path: str
    created_at: float


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    """A reconnectable, per-conversation projection event."""

    event_id: str
    conversation_id: str
    sequence: int
    kind: str
    message_id: str | None
    task_id: str | None
    run_id: str | None
    payload: Mapping[str, Any]
    created_at: float


@dataclass(frozen=True, slots=True)
class ConversationEventPage:
    events: tuple[ConversationEvent, ...]
    next_cursor: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    session_id: str
    state: AgentState
    version: int
    updated_at: float

    def __post_init__(self) -> None:
        _session_id(self.session_id)
        if not isinstance(self.state, AgentState):
            raise TypeError("SessionSnapshot.state must be AgentState")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise ValueError("SessionSnapshot.version must be a positive int")


@runtime_checkable
class SessionStore(Protocol):
    def load(self, session_id: str) -> SessionSnapshot | None: ...

    def save(
        self,
        session_id: str,
        state: AgentState,
        *,
        expected_version: int,
    ) -> SessionSnapshot: ...


class SQLiteSessionStore:
    """Thread-safe SQLite snapshots with compare-and-swap writes."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        if not isinstance(path, (str, Path)):
            raise TypeError("SQLiteSessionStore path must be a string or Path")
        self.path = path
        self._lock = threading.RLock()
        self._conn = connect_sqlite(
            path, check_same_thread=False, row_factory=sqlite3.Row,
        )
        self._closed = False
        try:
            self._init_schema()
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    def load(self, session_id: str) -> SessionSnapshot | None:
        session_id = _session_id(session_id)
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT id,version,state_json,updated_at "
                "FROM lipas_conversations WHERE id=?",
                (session_id,),
            ).fetchone()
        return None if row is None else self._snapshot(row)

    def save(
        self,
        session_id: str,
        state: AgentState,
        *,
        expected_version: int,
    ) -> SessionSnapshot:
        session_id = _session_id(session_id)
        if not isinstance(state, AgentState):
            raise TypeError("SessionStore state must be an AgentState")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise ValueError("expected_version must be a non-negative int")
        state_json = _state_json(state)
        version = expected_version + 1
        updated_at = time.time()
        with self._lock, self._transaction():
            if expected_version == 0:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO lipas_conversations"
                    "(id,version,state_json,updated_at) VALUES(?,?,?,?)",
                    (session_id, version, state_json, updated_at),
                )
            else:
                cursor = self._conn.execute(
                    "UPDATE lipas_conversations SET version=?,state_json=?,updated_at=? "
                    "WHERE id=? AND version=?",
                    (version, state_json, updated_at, session_id, expected_version),
                )
            if cursor.rowcount != 1:
                current = self._conn.execute(
                    "SELECT version FROM lipas_conversations WHERE id=?",
                    (session_id,),
                ).fetchone()
                actual = None if current is None else int(current[0])
                raise SessionConflictError(
                    f"session {session_id!r} is version {actual!r}, "
                    f"expected {expected_version}",
                )
        return SessionSnapshot(session_id, state, version, updated_at)

    def list(self, *, limit: int = 100) -> tuple[SessionSnapshot, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > _MAX_SQLITE_INT
        ):
            raise ValueError("SessionStore limit must be a positive int")
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT id,version,state_json,updated_at FROM lipas_conversations "
                "ORDER BY updated_at DESC,id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._snapshot(row) for row in rows)

    # -- Conversation kernel -----------------------------------------

    def create_conversation(
        self,
        *,
        conversation_id: str | None = None,
        title: str = "New conversation",
        workspace: str | Path = ".",
        metadata: Mapping[str, Any] | None = None,
    ) -> Conversation:
        """Create a chat resource with a stable caller-supplied identity.

        ``conversation_id`` is optional for ergonomic clients, but callers
        that retry a create request should provide one.  Reusing an id with
        different data is rejected rather than silently mutating history.
        """
        identifier = _resource_id(conversation_id, "conversation")
        title = _text(title, "title")
        root = _workspace(workspace)
        normalized = _metadata(metadata)
        now = time.time()
        with self._lock, self._transaction():
            existing = self._conn.execute(
                "SELECT id,title,workspace,state,metadata_json,created_at,updated_at "
                "FROM lipas_chat_conversations WHERE id=?",
                (identifier,),
            ).fetchone()
            if existing is not None:
                current = self._conversation(existing)
                if (
                    current.title != title
                    or current.workspace != root
                    or dict(current.metadata) != normalized
                ):
                    raise SessionConflictError(
                        f"conversation {identifier!r} already exists with different data",
                    )
                return current
            try:
                self._conn.execute(
                    "INSERT INTO lipas_chat_conversations"
                    "(id,title,workspace,state,metadata_json,created_at,updated_at) "
                    "VALUES(?,?,?,'open',?,?,?)",
                    (
                        identifier, title, root, _json(normalized), now, now,
                    ),
                )
            except sqlite3.IntegrityError:
                # A second SessionStore may have won the same deterministic
                # create race after our initial SELECT.  Re-read the
                # committed row and apply the same idempotency comparison as
                # the fast path above rather than leaking a raw SQLite error.
                existing = self._conn.execute(
                    "SELECT id,title,workspace,state,metadata_json,created_at,updated_at "
                    "FROM lipas_chat_conversations WHERE id=?",
                    (identifier,),
                ).fetchone()
                if existing is None:
                    raise
                current = self._conversation(existing)
                if (
                    current.title != title
                    or current.workspace != root
                    or dict(current.metadata) != normalized
                ):
                    raise SessionConflictError(
                        f"conversation {identifier!r} already exists with different data",
                    ) from None
                return current
        return Conversation(identifier, title, root, "open", normalized, now, now)

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        identifier = _text(conversation_id, "conversation_id")
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT id,title,workspace,state,metadata_json,created_at,updated_at "
                "FROM lipas_chat_conversations WHERE id=?",
                (identifier,),
            ).fetchone()
        return None if row is None else self._conversation(row)

    def list_conversations(self, *, limit: int = 100) -> tuple[Conversation, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > _MAX_SQLITE_INT
        ):
            raise ValueError("conversation limit must be a positive int")
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT id,title,workspace,state,metadata_json,created_at,updated_at "
                "FROM lipas_chat_conversations ORDER BY updated_at DESC,id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._conversation(row) for row in rows)

    def append_message(
        self,
        conversation_id: str,
        *,
        role: str,
        content: Any,
        message_id: str | None = None,
        kind: str = "message",
        task_id: str | None = None,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Message:
        """Append one message and its event atomically and idempotently."""
        conversation_id = _text(conversation_id, "conversation_id")
        role = _text(role, "role")
        if role not in {"user", "assistant", "system", "tool", "developer"}:
            raise ValueError(
                "role must be one of user, assistant, system, tool, developer",
            )
        kind = _text(kind, "kind")
        identifier = _resource_id(message_id, "message")
        normalized = _metadata(metadata)
        if (task_id is None) != (run_id is None):
            raise ValueError("task_id and run_id must be provided together")
        if task_id is not None:
            task_id = _text(task_id, "task_id")
            assert run_id is not None
            run_id = _text(run_id, "run_id")
        content_json = _json(content)
        now = time.time()
        with self._lock, self._transaction():
            if self._conn.execute(
                "SELECT 1 FROM lipas_chat_conversations WHERE id=?",
                (conversation_id,),
            ).fetchone() is None:
                raise KeyError(conversation_id)
            # A linked execution is an authority reference, not decorative
            # metadata.  Never allow the same Task/Run to appear in two
            # conversations: doing so makes a reconnecting client unable to
            # tell which chat owns the work.  Runtime promotion performs the
            # actual existence check against ExecutionStore; this store still
            # protects the conversation-level ownership invariant for direct
            # callers and retries.
            if task_id is not None:
                duplicate = self._conn.execute(
                    "SELECT conversation_id FROM lipas_chat_messages "
                    "WHERE task_id=? AND run_id=? AND id<>?",
                    (task_id, run_id, identifier),
                ).fetchone()
                if duplicate is not None:
                    raise SessionConflictError(
                        f"Task/Run {task_id!r}/{run_id!r} is already owned by "
                        f"conversation {duplicate[0]!r}",
                    )
            row = self._conn.execute(
                "SELECT id,conversation_id,role,kind,content_json,task_id,run_id,"
                "metadata_json,created_at FROM lipas_chat_messages WHERE id=?",
                (identifier,),
            ).fetchone()
            if row is not None:
                current = self._message(row)
                if (
                    current.conversation_id != conversation_id
                    or current.role != role
                    or current.kind != kind
                    or _json(current.content) != content_json
                    or current.task_id != task_id
                    or current.run_id != run_id
                    or dict(current.metadata) != normalized
                ):
                    raise SessionConflictError(
                        f"message {identifier!r} was reused with different data",
                    )
                return current
            try:
                self._conn.execute(
                    "INSERT INTO lipas_chat_messages"
                    "(id,conversation_id,role,kind,content_json,task_id,run_id,"
                    "metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        identifier, conversation_id, role, kind, content_json,
                        task_id, run_id, _json(normalized), now,
                    ),
                )
            except sqlite3.IntegrityError:
                # Deterministic message ids are the retry boundary.  Another
                # store handle may have committed the same id between our
                # initial SELECT and this insert; compare the durable row
                # instead of surfacing a backend-specific constraint error.
                existing = self._conn.execute(
                    "SELECT id,conversation_id,role,kind,content_json,task_id,run_id,"
                    "metadata_json,created_at FROM lipas_chat_messages WHERE id=?",
                    (identifier,),
                ).fetchone()
                if existing is None:
                    raise
                current = self._message(existing)
                if (
                    current.conversation_id != conversation_id
                    or current.role != role
                    or current.kind != kind
                    or _json(current.content) != content_json
                    or current.task_id != task_id
                    or current.run_id != run_id
                    or dict(current.metadata) != normalized
                ):
                    raise SessionConflictError(
                        f"message {identifier!r} was reused with different data",
                    ) from None
                return current
            self._append_event_locked(
                conversation_id,
                event_id=f"message:{identifier}",
                kind="message_created",
                message_id=identifier,
                task_id=task_id,
                run_id=run_id,
                payload={
                    "role": role, "kind": kind, "content": content,
                    "metadata": normalized,
                },
                created_at=now,
            )
            self._touch_conversation_locked(conversation_id, now)
            row = self._conn.execute(
                "SELECT id,conversation_id,role,kind,content_json,task_id,run_id,"
                "metadata_json,created_at FROM lipas_chat_messages WHERE id=?",
                (identifier,),
            ).fetchone()
        assert row is not None
        return self._message(row)

    def get_message(self, message_id: str) -> Message | None:
        identifier = _text(message_id, "message_id")
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT id,conversation_id,role,kind,content_json,task_id,run_id,"
                "metadata_json,created_at FROM lipas_chat_messages WHERE id=?",
                (identifier,),
            ).fetchone()
        return None if row is None else self._message(row)

    def list_messages(
        self, conversation_id: str, *, limit: int = 500,
    ) -> tuple[Message, ...]:
        conversation_id = _text(conversation_id, "conversation_id")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > _MAX_SQLITE_INT
        ):
            raise ValueError("message limit must be a positive int")
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT id,conversation_id,role,kind,content_json,task_id,run_id,"
                "metadata_json,created_at FROM lipas_chat_messages "
                "WHERE conversation_id=? ORDER BY created_at,id LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        return tuple(self._message(row) for row in rows)

    def linked_run_ids(self, conversation_id: str) -> tuple[str, ...]:
        """Return every distinct execution Run linked to a conversation.

        Projection code must not use a UI-sized message limit to discover
        linked Runs: an old, long conversation may otherwise silently lose
        execution events during catch-up.  The query is intentionally narrow
        and lets SQLite stream only the identities needed for projection.
        """
        conversation_id = _text(conversation_id, "conversation_id")
        with self._lock:
            self._ensure_open()
            if self._conn.execute(
                "SELECT 1 FROM lipas_chat_conversations WHERE id=?",
                (conversation_id,),
            ).fetchone() is None:
                raise KeyError(conversation_id)
            rows = self._conn.execute(
                "SELECT DISTINCT run_id FROM lipas_chat_messages "
                "WHERE conversation_id=? AND run_id IS NOT NULL ORDER BY run_id",
                (conversation_id,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def save_attachment(
        self,
        conversation_id: str,
        content: bytes,
        *,
        filename: str,
        mime_type: str = "application/octet-stream",
        attachment_id: str | None = None,
        max_bytes: int = 10 * 1024 * 1024,
    ) -> Attachment:
        """Persist one immutable attachment beneath the SQLite workspace.

        Filenames are metadata only; the stored path is generated by LIPAS and
        never follows user-supplied separators or symlinks. Reusing an id with
        different bytes is rejected, making upload retries idempotent.
        """
        conversation_id = _text(conversation_id, "conversation_id")
        if not isinstance(content, bytes):
            raise TypeError("attachment content must be bytes")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive int")
        if len(content) > max_bytes:
            raise ValueError("attachment exceeds max_bytes")
        filename = _attachment_filename(filename)
        mime_type = _text(mime_type, "mime_type")
        identifier = _resource_id(attachment_id, "attachment")
        digest = hashlib.sha256(content).hexdigest()
        now = time.time()
        with self._lock, self._transaction():
            conversation = self._conn.execute(
                "SELECT workspace FROM lipas_chat_conversations WHERE id=?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise KeyError(conversation_id)
            existing = self._conn.execute(
                "SELECT id,conversation_id,filename,mime_type,size,sha256,path,created_at "
                "FROM lipas_chat_attachments WHERE id=?",
                (identifier,),
            ).fetchone()
            if existing is not None:
                current = self._attachment(existing)
                if (
                    current.conversation_id != conversation_id
                    or current.sha256 != digest
                    or current.filename != filename
                    or current.mime_type != mime_type
                ):
                    raise SessionConflictError(f"attachment {identifier!r} was reused with different content")
                existing_path = Path(current.path)
                if existing_path.is_symlink() or not existing_path.is_file():
                    raise RuntimeError(f"attachment {identifier!r} record has no regular file")
                if existing_path.stat().st_size != current.size or hashlib.sha256(existing_path.read_bytes()).hexdigest() != current.sha256:
                    raise RuntimeError(f"attachment {identifier!r} failed integrity verification")
                return current
            root = Path(str(conversation[0])).expanduser().resolve()
            storage = (root / ".lipas-attachments" / conversation_id).resolve()
            if root != storage and root not in storage.parents:
                raise ValueError("attachment storage escaped conversation workspace")
            storage.mkdir(parents=True, exist_ok=True)
            target = storage / f"{identifier}-{digest[:16]}"
            # ``Path.exists()`` is false for a dangling symlink; check the
            # symlink bit independently so an upload can never replace one
            # through ``os.replace``.
            if target.is_symlink():
                raise ValueError("attachment target must not be a symlink")
            temporary = storage / f".{identifier}.tmp-{uuid.uuid4().hex}"
            try:
                temporary.write_bytes(content)
                temporary.replace(target)
                try:
                    self._conn.execute(
                        "INSERT INTO lipas_chat_attachments(id,conversation_id,filename,mime_type,size,sha256,path,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (identifier, conversation_id, filename, mime_type, len(content), digest, str(target), now),
                    )
                except sqlite3.IntegrityError:
                    # Another SessionStore may have won the same attachment
                    # identity while this handle was writing its temporary
                    # file.  Treat an identical committed row as the normal
                    # idempotent retry; never remove the winner's target.
                    existing = self._conn.execute(
                        "SELECT id,conversation_id,filename,mime_type,size,sha256,path,created_at "
                        "FROM lipas_chat_attachments WHERE id=?",
                        (identifier,),
                    ).fetchone()
                    if existing is None:
                        raise
                    current = self._attachment(existing)
                    if (
                        current.conversation_id != conversation_id
                        or current.sha256 != digest
                        or current.filename != filename
                        or current.mime_type != mime_type
                    ):
                        raise SessionConflictError(
                            f"attachment {identifier!r} was reused with different content",
                        ) from None
                    # The durable row belongs to the competing writer.  Our
                    # private temporary must not survive an idempotent retry.
                    try:
                        if temporary.exists() and not temporary.is_symlink():
                            temporary.unlink()
                    except OSError:
                        pass
                    return current
            except BaseException:
                # The SQLite transaction rolls back, so a failed insert must
                # not leave an apparently usable attachment on disk.  A peer
                # may already have committed this identity, however; retain
                # its target so a losing retry cannot corrupt the durable
                # attachment row.
                existing_row = self._conn.execute(
                    "SELECT path FROM lipas_chat_attachments WHERE id=?",
                    (identifier,),
                ).fetchone()
                # Keep a target only when it is the path committed by a peer;
                # otherwise remove both files created by this failed attempt.
                committed_path = None if existing_row is None else str(existing_row[0])
                paths = (
                    (temporary,)
                    if committed_path == str(target)
                    else (temporary, target)
                )
                for orphan in paths:
                    try:
                        if orphan.exists() and not orphan.is_symlink():
                            orphan.unlink()
                    except OSError:
                        pass
                raise
            self._touch_conversation_locked(conversation_id, now)
            row = self._conn.execute(
                "SELECT id,conversation_id,filename,mime_type,size,sha256,path,created_at FROM lipas_chat_attachments WHERE id=?",
                (identifier,),
            ).fetchone()
        assert row is not None
        return self._attachment(row)

    def get_attachment(self, attachment_id: str) -> Attachment | None:
        identifier = _text(attachment_id, "attachment_id")
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT id,conversation_id,filename,mime_type,size,sha256,path,created_at FROM lipas_chat_attachments WHERE id=?",
                (identifier,),
            ).fetchone()
        return None if row is None else self._attachment(row)

    def read_attachment(self, attachment_id: str) -> tuple[Attachment, bytes]:
        """Read an attachment after rechecking its immutable file evidence."""
        attachment = self.get_attachment(attachment_id)
        if attachment is None:
            raise KeyError(attachment_id)
        path = Path(attachment.path)
        conversation = self.get_conversation(attachment.conversation_id)
        if conversation is None:
            raise KeyError(attachment.conversation_id)
        root = Path(conversation.workspace).expanduser().resolve()
        storage = (root / ".lipas-attachments" / attachment.conversation_id).resolve()
        if root != storage and root not in storage.parents:
            raise RuntimeError(f"attachment {attachment.id!r} is outside its workspace")
        if path.parent.resolve() != storage:
            raise RuntimeError(f"attachment {attachment.id!r} is outside its workspace")
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(attachment.path)
        content = path.read_bytes()
        if len(content) != attachment.size or hashlib.sha256(content).hexdigest() != attachment.sha256:
            raise RuntimeError(f"attachment {attachment.id!r} failed integrity verification")
        return attachment, content

    def list_attachments(self, conversation_id: str, *, limit: int = 100) -> tuple[Attachment, ...]:
        conversation_id = _text(conversation_id, "conversation_id")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > _MAX_SQLITE_INT
        ):
            raise ValueError("attachment limit must be a positive int")
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT id,conversation_id,filename,mime_type,size,sha256,path,created_at FROM lipas_chat_attachments WHERE conversation_id=? ORDER BY created_at,id LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        return tuple(self._attachment(row) for row in rows)

    def attach_message(
        self,
        message_id: str,
        *,
        task_id: str,
        run_id: str,
    ) -> Message:
        """Attach a message to one Task/Run, rejecting conflicting links."""
        identifier = _text(message_id, "message_id")
        task_id = _text(task_id, "task_id")
        run_id = _text(run_id, "run_id")
        now = time.time()
        with self._lock, self._transaction():
            row = self._conn.execute(
                "SELECT id,conversation_id,role,kind,content_json,task_id,run_id,"
                "metadata_json,created_at FROM lipas_chat_messages WHERE id=?",
                (identifier,),
            ).fetchone()
            if row is None:
                raise KeyError(identifier)
            current = self._message(row)
            if current.task_id is not None or current.run_id is not None:
                if current.task_id == task_id and current.run_id == run_id:
                    return current
                raise SessionConflictError(
                    f"message {identifier!r} is already linked to another run",
                )
            duplicate = self._conn.execute(
                "SELECT conversation_id,id FROM lipas_chat_messages "
                "WHERE task_id=? AND run_id=? AND id<>?",
                (task_id, run_id, identifier),
            ).fetchone()
            if duplicate is not None:
                raise SessionConflictError(
                    f"Task/Run {task_id!r}/{run_id!r} is already owned by "
                    f"conversation {duplicate[0]!r}",
                )
            self._conn.execute(
                "UPDATE lipas_chat_messages SET task_id=?,run_id=? WHERE id=?",
                (task_id, run_id, identifier),
            )
            self._append_event_locked(
                current.conversation_id,
                event_id=f"task-link:{identifier}",
                kind="task_promoted",
                message_id=identifier,
                task_id=task_id,
                run_id=run_id,
                payload={"task_id": task_id, "run_id": run_id},
                created_at=now,
            )
            self._touch_conversation_locked(current.conversation_id, now)
            row = self._conn.execute(
                "SELECT id,conversation_id,role,kind,content_json,task_id,run_id,"
                "metadata_json,created_at FROM lipas_chat_messages WHERE id=?",
                (identifier,),
            ).fetchone()
        assert row is not None
        return self._message(row)

    def append_event(
        self,
        conversation_id: str,
        *,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        message_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> ConversationEvent:
        """Append an idempotent projection event for streaming/catch-up."""
        conversation_id = _text(conversation_id, "conversation_id")
        kind = _text(kind, "kind")
        if any(char in kind for char in "\r\n"):
            raise ValueError("event kind must not contain CR/LF")
        identifier = _resource_id(event_id, "event")
        if (task_id is None) != (run_id is None):
            raise ValueError("task_id and run_id must be provided together")
        if message_id is not None:
            message_id = _text(message_id, "message_id")
        if task_id is not None:
            task_id = _text(task_id, "task_id")
            assert run_id is not None
            run_id = _text(run_id, "run_id")
        normalized_payload = _strict_json_copy(
            dict(payload or {}), "event payload",
        )
        if not isinstance(normalized_payload, dict):
            raise ValueError("event payload must be a JSON object")
        now = time.time()
        with self._lock, self._transaction():
            if self._conn.execute(
                "SELECT 1 FROM lipas_chat_conversations WHERE id=?",
                (conversation_id,),
            ).fetchone() is None:
                raise KeyError(conversation_id)
            if message_id is not None:
                linked_message = self._conn.execute(
                    "SELECT conversation_id,task_id,run_id "
                    "FROM lipas_chat_messages WHERE id=?",
                    (message_id,),
                ).fetchone()
                if linked_message is None:
                    raise KeyError(message_id)
                if linked_message[0] != conversation_id:
                    raise SessionConflictError(
                        f"message {message_id!r} belongs to another conversation",
                    )
                if (
                    task_id is not None
                    and (linked_message[1] != task_id or linked_message[2] != run_id)
                ):
                    raise SessionConflictError(
                        f"message {message_id!r} is linked to another Task/Run",
                    )
            existed = self._conn.execute(
                "SELECT 1 FROM lipas_chat_events WHERE event_id=?",
                (identifier,),
            ).fetchone() is not None
            event = self._append_event_locked(
                conversation_id, event_id=identifier, kind=kind,
                message_id=message_id, task_id=task_id, run_id=run_id,
                payload=normalized_payload, created_at=now,
            )
            if not existed:
                self._touch_conversation_locked(conversation_id, now)
            return event

    def events(
        self,
        conversation_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> ConversationEventPage:
        conversation_id = _text(conversation_id, "conversation_id")
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after must be a non-negative int")
        if after > _MAX_SQLITE_INT:
            raise ValueError("after exceeds SQLite integer range")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1_000
        ):
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            self._ensure_open()
            if self._conn.execute(
                "SELECT 1 FROM lipas_chat_conversations WHERE id=?",
                (conversation_id,),
            ).fetchone() is None:
                raise KeyError(conversation_id)
            rows = self._conn.execute(
                "SELECT event_id,conversation_id,sequence,kind,message_id,task_id,"
                "run_id,payload_json,created_at FROM lipas_chat_events "
                "WHERE conversation_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (conversation_id, after, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        events = tuple(self._event(row) for row in selected)
        return ConversationEventPage(
            events=events,
            next_cursor=events[-1].sequence if events else after,
            has_more=has_more,
        )

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> "SQLiteSessionStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _init_schema(self) -> None:
        with self._conn:
            # Read the stamp before creating any v2 objects: a workspace from
            # a newer release must fail closed without being partially
            # modified by this older process.
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS lipas_conversation_meta "
                "(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
            )
            row = self._conn.execute(
                "SELECT value FROM lipas_conversation_meta "
                "WHERE key='schema_version'",
            ).fetchone()
            existing: int | None = None
            if row is not None:
                try:
                    existing = int(row[0])
                except (TypeError, ValueError, OverflowError) as exc:
                    raise RuntimeError(
                        f"SQLiteSessionStore schema version is not an int: {row[0]!r}",
                    ) from exc
                if existing > _SCHEMA_VERSION:
                    raise RuntimeError(
                        "SQLiteSessionStore schema version mismatch: "
                        f"database={existing!r}, runtime={_SCHEMA_VERSION}",
                    )
            self._conn.executescript(_SCHEMA)
            if existing is None:
                # Two processes may initialise the same workspace at once.
                # The schema DDL is idempotent; the metadata stamp must be as
                # well, otherwise the loser observes a spurious UNIQUE error
                # during an otherwise safe first open.
                self._conn.execute(
                    "INSERT OR IGNORE INTO lipas_conversation_meta(key,value) VALUES(?,?)",
                    ("schema_version", str(_SCHEMA_VERSION)),
                )
                row = self._conn.execute(
                    "SELECT value FROM lipas_conversation_meta WHERE key='schema_version'",
                ).fetchone()
                raw_version = None if row is None else row[0]
                try:
                    existing = int(raw_version) if raw_version is not None else -1
                except (TypeError, ValueError, OverflowError) as exc:
                    raise RuntimeError(
                        f"SQLiteSessionStore schema version is not an int: {raw_version!r}",
                    ) from exc
            if existing > _SCHEMA_VERSION:
                raise RuntimeError(
                    "SQLiteSessionStore schema version mismatch: "
                    f"database={existing!r}, runtime={_SCHEMA_VERSION}",
                )
            if existing < _SCHEMA_VERSION:
                # Chat/session schema changes are additive: legacy snapshots,
                # messages and events remain untouched while new attachment
                # tables are created before stamping the upgrade.
                self._conn.execute(
                    "UPDATE lipas_conversation_meta SET value=? WHERE key=?",
                    (str(_SCHEMA_VERSION), "schema_version"),
                )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._ensure_open()
        with immediate_transaction(self._conn):
            yield

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=str(row[0]),
            state=_state_from_json(str(row[2])),
            version=int(row[1]),
            updated_at=float(row[3]),
        )

    @staticmethod
    def _conversation(row: sqlite3.Row) -> Conversation:
        return Conversation(
            id=str(row[0]), title=str(row[1]), workspace=str(row[2]),
            state=str(row[3]), metadata=_mapping(row[4]),
            created_at=float(row[5]), updated_at=float(row[6]),
        )

    @staticmethod
    def _message(row: sqlite3.Row) -> Message:
        return Message(
            id=str(row[0]), conversation_id=str(row[1]), role=str(row[2]),
            kind=str(row[3]), content=_from_json(str(row[4])),
            task_id=None if row[5] is None else str(row[5]),
            run_id=None if row[6] is None else str(row[6]),
            metadata=_mapping(row[7]), created_at=float(row[8]),
        )

    @staticmethod
    def _attachment(row: sqlite3.Row) -> Attachment:
        return Attachment(
            id=str(row[0]), conversation_id=str(row[1]), filename=str(row[2]),
            mime_type=str(row[3]), size=int(row[4]), sha256=str(row[5]),
            path=str(row[6]), created_at=float(row[7]),
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> ConversationEvent:
        return ConversationEvent(
            event_id=str(row[0]), conversation_id=str(row[1]),
            sequence=int(row[2]), kind=str(row[3]),
            message_id=None if row[4] is None else str(row[4]),
            task_id=None if row[5] is None else str(row[5]),
            run_id=None if row[6] is None else str(row[6]),
            payload=_mapping(row[7]), created_at=float(row[8]),
        )

    def _append_event_locked(
        self,
        conversation_id: str,
        *,
        event_id: str,
        kind: str,
        message_id: str | None,
        task_id: str | None,
        run_id: str | None,
        payload: Mapping[str, Any],
        created_at: float,
    ) -> ConversationEvent:
        existing = self._conn.execute(
            "SELECT event_id,conversation_id,sequence,kind,message_id,task_id,"
            "run_id,payload_json,created_at FROM lipas_chat_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        payload_json = _json(dict(payload))
        if existing is not None:
            current = self._event(existing)
            if (
                current.conversation_id != conversation_id
                or current.kind != kind
                or current.message_id != message_id
                or current.task_id != task_id
                or current.run_id != run_id
                or _json(current.payload) != payload_json
            ):
                raise SessionConflictError(
                    f"event {event_id!r} was reused with different data",
                )
            return current
        sequence = int(self._conn.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM lipas_chat_events "
            "WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0])
        try:
            self._conn.execute(
                "INSERT INTO lipas_chat_events"
                "(event_id,conversation_id,sequence,kind,message_id,task_id,run_id,"
                "payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    event_id, conversation_id, sequence, kind, message_id,
                    task_id, run_id, payload_json, created_at,
                ),
            )
        except sqlite3.IntegrityError:
            # A same-conversation concurrent writer can win the sequence. A
            # transaction retry is safer than returning a cursor gap.
            existing = self._conn.execute(
                "SELECT event_id,conversation_id,sequence,kind,message_id,task_id,"
                "run_id,payload_json,created_at FROM lipas_chat_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if existing is None:
                raise
            current = self._event(existing)
            if (
                current.conversation_id != conversation_id
                or current.kind != kind
                or current.message_id != message_id
                or current.task_id != task_id
                or current.run_id != run_id
                or _json(current.payload) != payload_json
            ):
                raise SessionConflictError(
                    f"event {event_id!r} was reused with different data",
                ) from None
            return current
        row = self._conn.execute(
            "SELECT event_id,conversation_id,sequence,kind,message_id,task_id,"
            "run_id,payload_json,created_at FROM lipas_chat_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        assert row is not None
        return self._event(row)

    def _touch_conversation_locked(self, conversation_id: str, now: float) -> None:
        self._conn.execute(
            "UPDATE lipas_chat_conversations SET updated_at=? WHERE id=?",
            (now, conversation_id),
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("SQLiteSessionStore is closed")


def _session_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("session_id must be a non-empty string")
    return value.strip()


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _resource_id(value: str | None, prefix: str) -> str:
    if value is None:
        return f"{prefix}_{uuid.uuid4().hex}"
    return _text(value, f"{prefix}_id")


def _attachment_filename(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("attachment filename must be non-empty")
    value = value.strip()
    if len(value) > 255 or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("attachment filename must be a single safe path component")
    if any(ord(char) < 32 for char in value):
        raise ValueError("attachment filename contains control characters")
    return value


def _workspace(value: str | Path) -> str:
    if not isinstance(value, (str, Path)):
        raise TypeError("workspace must be a string or Path")
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"workspace is not a directory: {root}")
    return str(root)


def _metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("metadata must be a mapping or None")
    normalized = _strict_json_copy(dict(value), "metadata")
    if not isinstance(normalized, dict):
        raise ValueError("metadata must be a JSON object")
    return normalized


def _mapping(value: str) -> Mapping[str, Any]:
    decoded = _from_json(value)
    if not isinstance(decoded, Mapping):
        raise TypeError("stored conversation payload must be a mapping")
    return dict(decoded)


def _json(value: Any) -> str:
    return json.dumps(
        encode(value, _CODECS), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def _strict_json_copy(value: Any, name: str) -> Any:
    """Detach JSON-only conversation metadata and projection payloads."""
    active: set[int] = set()

    def validate(item: Any, path: str) -> None:
        if item is None or isinstance(item, (bool, int, str)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{path} contains a non-finite number")
            return
        if not isinstance(item, (list, tuple, Mapping)):
            raise TypeError(f"{path} contains unsupported {type(item).__name__}")
        marker = id(item)
        if marker in active:
            raise ValueError(f"{path} contains a reference cycle")
        active.add(marker)
        try:
            if isinstance(item, Mapping):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError(f"{path} must use string object keys")
                    validate(child, f"{path}.{key}")
            else:
                for index, child in enumerate(item):
                    validate(child, f"{path}[{index}]")
        finally:
            active.remove(marker)

    try:
        validate(value, name)
        return json.loads(json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{name} must be strict JSON") from exc


def _from_json(value: str) -> Any:
    def reject_constant(raw: str) -> None:
        raise ValueError(f"non-JSON numeric constant {raw!r}")

    return decode(json.loads(value, parse_constant=reject_constant), _CODECS)


def _state_json(state: AgentState) -> str:
    payload = {
        "messages": list(state.messages),
        "iteration": state.iteration,
        "metadata": dict(state.metadata),
    }
    return json.dumps(
        encode(payload, _CODECS), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def _state_from_json(value: str) -> AgentState:
    def reject_constant(raw: str) -> None:
        raise ValueError(f"non-JSON numeric constant {raw!r}")

    payload = decode(json.loads(value, parse_constant=reject_constant), _CODECS)
    if not isinstance(payload, Mapping):
        raise TypeError("stored Session state must be a mapping")
    messages = payload.get("messages", ())
    metadata = payload.get("metadata", {})
    if not isinstance(messages, (list, tuple)):
        raise TypeError("stored Session messages must be a list")
    if not isinstance(metadata, Mapping):
        raise TypeError("stored Session metadata must be a mapping")
    return AgentState(
        messages=tuple(messages),
        iteration=payload.get("iteration", 0),
        metadata=dict(metadata),
    )
