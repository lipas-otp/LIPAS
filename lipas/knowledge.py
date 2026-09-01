"""Small local lexical knowledge index for retrieval-augmented workflows.

The index is deliberately an application-context store, not a Claim or
conversation authority.  Hosts choose the document source/scope, ingest
already-authorized text, and retain the returned source/digest as citation
evidence.  A later embedding provider can implement the same boundary without
changing LIPAS execution semantics.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .sqlite_storage import connect_sqlite
from .tools import Tool, tool

__all__ = [
    "KnowledgeError",
    "KnowledgeDocument",
    "KnowledgeHit",
    "KnowledgeStore",
    "knowledge_search_tool",
]


class KnowledgeError(ValueError):
    """A knowledge document or retrieval request is invalid."""


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    id: str
    source: str
    scope: str | None
    sha256: str
    chunks: int
    metadata: Mapping[str, Any]
    updated_at: float


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    document_id: str
    source: str
    scope: str | None
    chunk: int
    text: str
    score: float
    sha256: str
    metadata: Mapping[str, Any]

    def citation(self) -> dict[str, object]:
        return {
            "source": self.source,
            "scope": self.scope,
            "document_sha256": self.sha256,
            "chunk": self.chunk,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "source": self.source,
            "scope": self.scope,
            "chunk": self.chunk,
            "text": self.text,
            "score": self.score,
            "sha256": self.sha256,
            "citation": self.citation(),
            "metadata": dict(self.metadata),
        }


_SCHEMA_VERSION = 1
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _strict_json(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise KnowledgeError("metadata must be an object")
    try:
        return json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise KnowledgeError("metadata must contain strict JSON values") from exc


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_TOKEN_RE.findall(value.casefold())))


def _normalise_scope(scope: str | None) -> str | None:
    if scope is not None and (not isinstance(scope, str) or not scope.strip()):
        raise KnowledgeError("scope must be a non-empty string or None")
    return scope.strip() if scope is not None else None


def _chunk_text(text: str, *, size: int, overlap: int) -> tuple[str, ...]:
    if not text.strip():
        return ()
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        value = text[start:end].strip()
        if value:
            chunks.append(value)
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return tuple(chunks)


class KnowledgeStore:
    """Durable, scope-filtered lexical retrieval store."""

    def __init__(
        self,
        database: str | Path = ":memory:",
        *,
        chunk_size: int = 1_200,
        chunk_overlap: int = 120,
        max_text_chars: int = 2_000_000,
        max_chunks_per_document: int = 10_000,
        max_chunks_total: int = 500_000,
    ) -> None:
        for name, value, minimum, maximum in (
            ("chunk_size", chunk_size, 100, 20_000),
            ("chunk_overlap", chunk_overlap, 0, 5_000),
            ("max_text_chars", max_text_chars, 1, 20_000_000),
            ("max_chunks_per_document", max_chunks_per_document, 1, 100_000),
            ("max_chunks_total", max_chunks_total, 1, 2_000_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.max_text_chars = max_text_chars
        self.max_chunks_per_document = max_chunks_per_document
        self.max_chunks_total = max_chunks_total
        self._conn = connect_sqlite(database)
        self._closed = False
        try:
            with self._conn:
                self._conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS knowledge_meta (
                        key TEXT PRIMARY KEY, value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS knowledge_documents (
                        id TEXT PRIMARY KEY,
                        source TEXT NOT NULL,
                        scope TEXT,
                        sha256 TEXT NOT NULL,
                        text_chars INTEGER NOT NULL,
                        metadata_json TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS knowledge_chunks (
                        id TEXT PRIMARY KEY,
                        document_id TEXT NOT NULL REFERENCES knowledge_documents(id)
                            ON DELETE CASCADE,
                        ordinal INTEGER NOT NULL,
                        text TEXT NOT NULL,
                        UNIQUE(document_id, ordinal)
                    );
                    CREATE INDEX IF NOT EXISTS knowledge_chunks_document
                        ON knowledge_chunks(document_id, ordinal);
                    """,
                )
                row = self._conn.execute(
                    "SELECT value FROM knowledge_meta WHERE key='schema_version'",
                ).fetchone()
                if row is None:
                    self._conn.execute(
                        "INSERT INTO knowledge_meta(key,value) VALUES('schema_version',?)",
                        (str(_SCHEMA_VERSION),),
                    )
                elif row[0] != str(_SCHEMA_VERSION):
                    raise KnowledgeError(
                        f"knowledge schema is {row[0]!r}; expected {_SCHEMA_VERSION}",
                    )
        except BaseException:
            self._conn.close()
            raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("KnowledgeStore is closed")

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    def __enter__(self) -> "KnowledgeStore":
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def ingest(
        self,
        source: str,
        text: str,
        *,
        scope: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> KnowledgeDocument:
        """Upsert one authorized text source and return its digest identity."""
        self._ensure_open()
        if not isinstance(source, str) or not source.strip():
            raise KnowledgeError("source must be a non-empty string")
        if not isinstance(text, str):
            raise KnowledgeError("text must be a string")
        if len(text) > self.max_text_chars:
            raise KnowledgeError("document exceeds the text-size limit")
        source = source.strip()
        if len(source) > 2_000:
            raise KnowledgeError("source exceeds the length limit")
        scope = _normalise_scope(scope)
        if scope is not None and len(scope) > 256:
            raise KnowledgeError("scope exceeds the length limit")
        encoded = text.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        metadata_json = _strict_json(metadata or {})
        if len(metadata_json.encode("utf-8")) > 100_000:
            raise KnowledgeError("metadata exceeds the size limit")
        document_id = hashlib.sha256(
            f"{scope or ''}\0{source}".encode("utf-8"),
        ).hexdigest()[:32]
        chunks = _chunk_text(
            text, size=self.chunk_size, overlap=self.chunk_overlap,
        )
        if len(chunks) > self.max_chunks_per_document:
            raise KnowledgeError("document exceeds the chunk limit")
        now = time.time()
        with self._conn:
            current = self._conn.execute(
                "SELECT count(*) FROM knowledge_chunks WHERE document_id=?",
                (document_id,),
            ).fetchone()
            current_chunks = int(current[0]) if current is not None else 0
            total = self._conn.execute(
                "SELECT count(*) FROM knowledge_chunks",
            ).fetchone()
            total_chunks = int(total[0]) if total is not None else 0
            if total_chunks - current_chunks + len(chunks) > self.max_chunks_total:
                raise KnowledgeError("knowledge index exceeds the total chunk limit")
            self._conn.execute(
                "INSERT INTO knowledge_documents"
                "(id,source,scope,sha256,text_chars,metadata_json,updated_at)"
                " VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
                " source=excluded.source,scope=excluded.scope,sha256=excluded.sha256,"
                " text_chars=excluded.text_chars,metadata_json=excluded.metadata_json,"
                " updated_at=excluded.updated_at",
                (document_id, source, scope, digest, len(text), metadata_json, now),
            )
            self._conn.execute(
                "DELETE FROM knowledge_chunks WHERE document_id=?", (document_id,),
            )
            self._conn.executemany(
                "INSERT INTO knowledge_chunks(id,document_id,ordinal,text) VALUES(?,?,?,?)",
                (
                    (f"{document_id}:{index}", document_id, index, chunk)
                    for index, chunk in enumerate(chunks)
                ),
            )
        return KnowledgeDocument(
            document_id, source, scope, digest, len(chunks),
            json.loads(metadata_json), now,
        )

    def remove(self, source: str, *, scope: str | None = None) -> bool:
        """Remove one source identity; return whether it existed."""
        self._ensure_open()
        if not isinstance(source, str) or not source.strip():
            raise KnowledgeError("source must be a non-empty string")
        scope = _normalise_scope(scope)
        key = hashlib.sha256(
            f"{scope or ''}\0{source.strip()}".encode("utf-8"),
        ).hexdigest()[:32]
        with self._conn:
            result = self._conn.execute(
                "DELETE FROM knowledge_documents WHERE id=?", (key,),
            )
        return result.rowcount > 0

    def documents(self, *, scope: str | None = None) -> tuple[KnowledgeDocument, ...]:
        """List indexed documents in stable source order."""
        self._ensure_open()
        scope = _normalise_scope(scope)
        if scope is None:
            rows = self._conn.execute(
                "SELECT d.id,d.source,d.scope,d.sha256,d.metadata_json,d.updated_at,"
                "(SELECT count(*) FROM knowledge_chunks c WHERE c.document_id=d.id) "
                "FROM knowledge_documents d ORDER BY d.source,d.id",
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT d.id,d.source,d.scope,d.sha256,d.metadata_json,d.updated_at,"
                "(SELECT count(*) FROM knowledge_chunks c WHERE c.document_id=d.id) "
                "FROM knowledge_documents d WHERE d.scope=? ORDER BY d.source,d.id",
                (scope,),
            ).fetchall()
        return tuple(
            KnowledgeDocument(
                row[0], row[1], row[2], row[3], row[6], json.loads(row[4]), row[5],
            )
            for row in rows
        )

    def search(
        self,
        query: str,
        *,
        scope: str | None = None,
        limit: int = 10,
    ) -> tuple[KnowledgeHit, ...]:
        """Return deterministic lexical hits with source/digest citations."""
        self._ensure_open()
        if not isinstance(query, str) or not query.strip():
            raise KnowledgeError("query must be a non-empty string")
        if len(query) > 1_000:
            raise KnowledgeError("query exceeds the length limit")
        scope = _normalise_scope(scope)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise KnowledgeError("limit must be between 1 and 100")
        tokens = _tokens(query)
        if not tokens:
            return ()
        if scope is None:
            rows = self._conn.execute(
                "SELECT d.id,d.source,d.scope,d.sha256,d.metadata_json,c.ordinal,c.text "
                "FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id",
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT d.id,d.source,d.scope,d.sha256,d.metadata_json,c.ordinal,c.text "
                "FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id "
                "WHERE d.scope=?", (scope,),
            ).fetchall()
        ranked: list[KnowledgeHit] = []
        for row in rows:
            haystack = str(row[6]).casefold()
            occurrences = sum(haystack.count(token) for token in tokens)
            if occurrences == 0:
                continue
            score = occurrences / len(tokens)
            ranked.append(KnowledgeHit(
                row[0], row[1], row[2], row[5], row[6][:2_000], score, row[3],
                json.loads(row[4]),
            ))
        ranked.sort(key=lambda value: (-value.score, value.source, value.chunk, value.document_id))
        return tuple(ranked[:limit])


def knowledge_search_tool(store: KnowledgeStore) -> Tool:
    """Create a read-only Tool over an explicitly scoped KnowledgeStore."""
    if not isinstance(store, KnowledgeStore):
        raise TypeError("store must be a KnowledgeStore")

    @tool(name="search_knowledge", side_effect="read_only")
    def search_knowledge(
        query: str,
        scope: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        """Search indexed context and return citation-bearing text chunks."""
        return [value.as_dict() for value in store.search(
            query, scope=scope, limit=limit,
        )]

    return search_knowledge
