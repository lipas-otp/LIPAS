"""Local lexical knowledge-store contracts."""
from __future__ import annotations

import math

import pytest

from lipas.knowledge import KnowledgeError, KnowledgeStore, knowledge_search_tool


def test_knowledge_store_upserts_chunks_and_returns_citations(tmp_path):
    database = tmp_path / "knowledge.db"
    with KnowledgeStore(database, chunk_size=100, chunk_overlap=10) as store:
        first = store.ingest(
            "docs/release.md",
            "LIPAS release process requires a review.\n" * 8,
            scope="team-a",
            metadata={"kind": "guide"},
        )
        second = store.ingest(
            "docs/release.md",
            "LIPAS release process requires an approval.\n" * 8,
            scope="team-a",
            metadata={"kind": "guide", "revision": 2},
        )
        assert first.id == second.id
        assert first.sha256 != second.sha256
        assert store.documents(scope="team-a")[0].metadata["revision"] == 2
        hits = store.search("approval", scope="team-a")
        assert hits
        assert hits[0].citation()["document_sha256"] == second.sha256
        assert hits[0].citation()["scope"] == "team-a"
        assert store.search("approval", scope="other") == ()


def test_knowledge_store_is_scope_filtered_and_bounded(tmp_path):
    with KnowledgeStore(tmp_path / "knowledge.db") as store:
        store.ingest("a.txt", "private tenant alpha", scope="alpha")
        store.ingest("b.txt", "private tenant beta", scope="beta")
        assert {hit.scope for hit in store.search("private")} == {"alpha", "beta"}
        with pytest.raises(KnowledgeError, match="limit"):
            store.search("private", limit=0)
        with pytest.raises(KnowledgeError, match="metadata"):
            store.ingest("bad", "text", metadata={"nan": math.nan})
        with pytest.raises(KnowledgeError, match="query"):
            store.search("x" * 1_001)


def test_knowledge_store_remove_is_idempotent(tmp_path):
    with KnowledgeStore(tmp_path / "knowledge.db") as store:
        store.ingest("a.txt", "text", scope="one")
        assert store.remove("a.txt", scope="one") is True
        assert store.remove("a.txt", scope="one") is False
        assert store.documents() == ()


def test_knowledge_search_tool_is_read_only_and_citation_bearing(tmp_path):
    with KnowledgeStore(tmp_path / "knowledge.db") as store:
        store.ingest("guide.md", "Approval requires a preview.", scope="team")
        search = knowledge_search_tool(store)
        assert search.name == "search_knowledge"
        assert search.side_effect.value == "read_only"
        result = search.invoke(query="preview", scope="team")
        assert result[0]["citation"]["source"] == "guide.md"
