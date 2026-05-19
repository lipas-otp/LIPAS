"""Built-in codecs for v0.1 lipas types.

Coverage:
  Claim, Reply, Request, Message, ToolSpec, Usage, EffectKind.

Excluded by design (v0.1):
  - SideEffectClass: stored as its .value (str) on claim fields per
    EffectRow validators (_VALID_SIDE_EFFECTS). If a deployment ever
    folds the enum instance directly, register the codec then.
  - TextBlock / ToolUseBlock / ToolResultBlock: at runtime adapters
    pass Anthropic-shape dicts, not the dataclasses (see content.py
    note + AnthropicAdapter / OllamaAdapter docstrings). Registering
    them now would be dead weight.
"""

from __future__ import annotations

from .codec import CodecRegistry


def register_builtin_codecs(registry: CodecRegistry) -> None:
    """Populate *registry* with codecs for the v0.1 lipas types.

    Idempotent only on a fresh registry; calling twice on the same
    registry raises (CodecRegistry.register rejects duplicate tags).
    """
    # ── Claim ──────────────────────────────────────────────────
    from ..calculus import Claim

    def _enc_claim(c: Claim) -> dict:
        return {
            "tag":      c.tag,
            "fields":   c.fields,
            "kind":     c.kind,
            "priority": c.priority,
            "source":   c.source,
            "claim_id": c.claim_id,
            "seq":      c.seq,
        }

    def _dec_claim(d: dict) -> Claim:
        return Claim(
            tag=d["tag"],
            fields=d.get("fields") or {},
            kind=d.get("kind"),
            priority=d.get("priority", 0),
            source=d.get("source", ""),
            claim_id=d["claim_id"],
            seq=d.get("seq", -1),
        )

    registry.register(Claim, "Claim", _enc_claim, _dec_claim)

    # ── Usage ──────────────────────────────────────────────────
    from ..adapter.usage import Usage

    registry.register(
        Usage, "Usage",
        lambda u: {
            "input":       u.input,
            "output":      u.output,
            "cache_read":  u.cache_read,
            "cache_write": u.cache_write,
        },
        lambda d: Usage(
            input=d.get("input", 0),
            output=d.get("output", 0),
            cache_read=d.get("cache_read", 0),
            cache_write=d.get("cache_write", 0),
        ),
    )

    # ── Reply ──────────────────────────────────────────────────
    # Reply.content is Sequence[ContentBlock] but at runtime it's a
    # list of Anthropic-shape dicts (see adapter docstrings). We pass
    # it through verbatim — the recursive encode/decode handles any
    # nested codec'd values inside (none expected for v0.1).
    from ..adapter.reply import Reply

    registry.register(
        Reply, "Reply",
        lambda r: {
            "content":      list(r.content),
            "usage":        r.usage,
            "stop_reason":  r.stop_reason,
            "model":        r.model,
            "error_detail": (dict(r.error_detail)
                             if r.error_detail is not None else None),
        },
        lambda d: Reply(
            content=d["content"],
            usage=d["usage"],
            stop_reason=d["stop_reason"],
            model=d["model"],
            error_detail=d.get("error_detail"),
        ),
    )

    # ── Message ────────────────────────────────────────────────
    from ..adapter.request import Message, ToolSpec, Request

    registry.register(
        Message, "Message",
        lambda m: {"role": m.role, "content": m.content},
        lambda d: Message(role=d["role"], content=d["content"]),
    )

    # ── ToolSpec ───────────────────────────────────────────────
    registry.register(
        ToolSpec, "ToolSpec",
        lambda t: {
            "name":         t.name,
            "description":  t.description,
            "input_schema": dict(t.input_schema),
        },
        lambda d: ToolSpec(
            name=d["name"],
            description=d["description"],
            input_schema=d["input_schema"],
        ),
    )

    # ── Request ────────────────────────────────────────────────
    # Request.__post_init__ normalizes messages to a list of dicts at
    # construction. We snapshot the post-normalization shape; on decode
    # the constructor re-runs __post_init__ idempotently.
    registry.register(
        Request, "Request",
        lambda r: {
            "model":          r.model,
            "messages":       list(r.messages),
            "max_tokens":     r.max_tokens,
            "system":         r.system,
            "tools":          list(r.tools),
            "temperature":    r.temperature,
            "stop_sequences": list(r.stop_sequences),
            "extra":          dict(r.extra),
        },
        lambda d: Request(
            model=d["model"],
            messages=d["messages"],
            max_tokens=d["max_tokens"],
            system=d.get("system", ""),
            tools=d.get("tools", ()),
            temperature=d.get("temperature"),
            stop_sequences=d.get("stop_sequences", ()),
            extra=d.get("extra", {}),
        ),
    )

    # ── EffectKind ─────────────────────────────────────────────
    # Defensive: EffectRow validators check the .value (str) so claim
    # fields normally store strings, not the enum. Registering anyway
    # lets harnesses fold the enum directly without surprise.
    from ..effect import EffectKind

    registry.register(
        EffectKind, "EffectKind",
        lambda e: e.value,
        lambda v: EffectKind(v),
    )


def make_default_codec_registry() -> CodecRegistry:
    """Fresh CodecRegistry with all built-in codecs registered."""
    r = CodecRegistry()
    register_builtin_codecs(r)
    return r
