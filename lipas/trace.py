"""Human-readable and JSONL views over the append-only claim tape."""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, is_dataclass
from typing import Any, TextIO

from .calculus import Claim

__all__ = ["TraceEvent", "iter_trace", "render_trace", "write_jsonl"]


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "value") and isinstance(getattr(value, "value"), str):
        return value.value
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


class TraceEvent(dict[str, Any]):
    """JSON-serialisable representation of a claim, kept dict-compatible."""


def iter_trace(claims: Iterable[Claim]) -> Iterable[TraceEvent]:
    """Yield a stable event shape; accepts ClaimStore, RowSet.store, or logs."""
    for claim in claims:
        yield TraceEvent(
            seq=claim.seq,
            claim_id=claim.claim_id,
            tag=claim.tag,
            kind=claim.kind,
            priority=claim.priority,
            source=claim.source,
            fields=_jsonable(claim.fields),
        )


def render_trace(claims: Iterable[Claim]) -> str:
    """Return concise Markdown appropriate for incident reports and PRs."""
    lines = ["| seq | tag | source | fields |", "| ---: | --- | --- | --- |"]
    for event in iter_trace(claims):
        source = str(event["source"] or "").replace("|", "\\|")
        fields = json.dumps(event["fields"], ensure_ascii=False, sort_keys=True)
        lines.append(f"| {event['seq']} | `{event['tag']}` | {source} | `{fields.replace('|', '\\|')}` |")
    return "\n".join(lines)


def write_jsonl(claims: Iterable[Claim], stream: TextIO) -> None:
    """Write one trace event per line without closing the caller's stream."""
    for event in iter_trace(claims):
        stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
