"""Helpers for the `_lipas` sidecar field on message dicts.

Implements INV-LIPAS-STRIP, INV-LIPAS-HASH, INV-LIPAS-TRANSPARENT.

The `_lipas` sidecar lives on assistant and tool messages under the key
`"_lipas"`. Its schema (version 1):

    {
        "provider":       str,     # e.g. "anthropic"
        "content_blocks": list,    # provider-native content blocks
        "content_hash":   str,     # sha256(canonical_json(...))[:16]
        "version":        1,
    }
"""
from __future__ import annotations

import copy
import hashlib
import json
import warnings
from typing import Any

from .exceptions import LipasStaleNativeWarning

LIPAS_KEY = "_lipas"
LIPAS_SCHEMA_VERSION = 1

# Known provider identifiers. Different gateways for the same underlying model
# are DISTINCT providers for _lipas purposes — signatures and cache tokens are
# not portable across gateways (e.g. Bedrock Anthropic re-signs thinking blocks).
KNOWN_PROVIDERS = frozenset({
    "openai",
    "anthropic",
    "gemini",
    "bedrock-anthropic",
    "vertex-anthropic",
    "vertex-gemini",
})


def canonical_json(obj: Any) -> str:
    """Deterministic JSON serialization used for content hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_content_hash(msg: dict) -> str:
    """Compute the content_hash for a message per INV-LIPAS-HASH.

    Hashes only the *semantic* payload (`content` + `tool_calls`); structural
    fields (`role`, `tool_call_id`, `name`, etc.) are intentionally excluded.
    """
    payload = {
        "content": msg.get("content"),
        "tool_calls": msg.get("tool_calls"),
    }
    canon = canonical_json(payload).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()[:16]


def strip_lipas(messages: list[dict]) -> list[dict]:
    """Return a deep copy of `messages` with all `_lipas` keys removed.

    Public helper backing INV-LIPAS-STRIP. Every adapter MUST call this (or
    equivalent) before serializing messages to a provider HTTP body.

    Compliance assertion for adapters:

        body = json.dumps(adapter_serialize(messages)).encode()
        assert b'"_lipas"' not in body

    Note: this substring check relies on lipas NOT producing any other key
    whose name contains `_lipas` (e.g. `_lipas_is_error`). v1 upholds this.
    """
    out = copy.deepcopy(messages)
    _strip_in_place(out)
    return out


def _strip_in_place(node: Any) -> None:
    if isinstance(node, dict):
        node.pop(LIPAS_KEY, None)
        for v in node.values():
            _strip_in_place(v)
    elif isinstance(node, list):
        for v in node:
            _strip_in_place(v)


def invalidate_native(msg: dict) -> None:
    """Drop the `_lipas` sidecar from a message in place.

    Use after editing `content` or `tool_calls` if you prefer explicit
    invalidation over hash-fallback. Semantically equivalent to letting the
    hash check fail, but avoids the LipasDesyncWarning.
    """
    msg.pop(LIPAS_KEY, None)


def attach_lipas(
    msg: dict,
    *,
    provider: str,
    content_blocks: list,
    extra: dict | None = None,
) -> dict:
    """Attach a `_lipas` sidecar to `msg` in place and return it.

    Adapter-facing. Call AFTER `msg['content']` and `msg['tool_calls']` are
    in their final form — `content_hash` is computed from `msg` at this moment.
    """
    sidecar = {
        "provider": provider,
        "content_blocks": content_blocks,
        "content_hash": compute_content_hash(msg),
        "version": LIPAS_SCHEMA_VERSION,
    }
    if extra:
        sidecar = {**extra, **sidecar}
    msg[LIPAS_KEY] = sidecar
    return msg


def should_use_native(msg: dict, adapter_provider: str) -> bool:
    """Adapter-facing predicate: may this message's native content_blocks be used?

    Implements the decision logic of INV-LIPAS-HASH. Returns False (emitting
    LipasStaleNativeWarning) on hash mismatch. Returns False silently on
    missing sidecar, provider mismatch, or version mismatch.

    Version policy: EXACT equality required. A v0 or v2 sidecar is not
    readable by a v1 runtime — we degrade to the OpenAI-format path rather
    than risk misinterpreting the schema. Future v2+ must ship with explicit
    back-compat handling if it wants to read v1 sidecars.
    """
    lipas = msg.get(LIPAS_KEY)
    if not lipas:
        return False
    if lipas.get("provider") != adapter_provider:
        return False
    if lipas.get("version") != LIPAS_SCHEMA_VERSION:
        return False
    expected = lipas.get("content_hash")
    actual = compute_content_hash(msg)
    if expected != actual:
        warnings.warn(
            "lipas: message content modified after _lipas was attached; "
            "falling back to OpenAI-format path. Prompt cache and "
            "provider-native features (thinking signatures, multimodal "
            "tool_result) will not be used for this message. "
            "Suppress with `del msg['_lipas']` or "
            "`lipas.invalidate_native(msg)` after editing.",
            LipasStaleNativeWarning,
            stacklevel=2,
        )
        return False
    return True
