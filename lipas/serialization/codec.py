"""
The core of Codec: CodecRegistry + recursive encode/decode.

Tagged-JSON format
----------------
Python object  JSON-clean form
-------------  ----------------------------
None / bool      passthrough
int / float      passthrough
str              passthrough
list             [encode(v) for v in value]
tuple            list  (lossy — see module design decisions) dict[str, V]     {k: encode(v) for k, v in value.items()}
registered T     {"__lipas_type__": <tag>, "__lipas_data__": encode(payload)}

Retain key
----------
``__lipas_type__`` and ``__lipas_data__`` are codec marker keys.
When the user dict contains these keys, encode directly raises UnserializableClaim - not silently writing data that would be misinterpreted by the decoder.

Non-target
----------
- No schema evolution is required (A2 is the responsibility of the store layer; each claim row can carry a version number).
- No in-place compatibility is needed (if the name of the Reply field changes, just modify the codec; the old store will be migrated to v0.2).
- No circular reference detection is performed (claim fields in lipas do not self-reference; it is safer to crash when encountered than to pretend to support it).
"""

from __future__ import annotations

from typing import Any, Callable

from ..exceptions import LipasError


# 与 store.fields_json 一起出现在 store 文件里的两个保留键。
# 用最长 + 最不像业务字段的形式，最大化避免误碰。
_LIPAS_TYPE_KEY = "__lipas_type__"
_LIPAS_DATA_KEY = "__lipas_data__"


class UnserializableClaim(LipasError):
    """A claim contains a value the codec cannot (de)serialize.

    Raised on:
      - a Python type with no registered codec (encode);
      - a dict with non-str keys (encode);
      - a dict carrying a reserved codec key (encode);
      - a tagged-JSON entry whose tag has no decoder (decode);
      - any otherwise unrecognized JSON value type (decode).

    Categorically a programming / configuration error — the codec
    registry is a closed set known at process start. Surface, do not
    swallow: a missing codec means the audit trail would be lossy,
    which defeats the entire point of the store.
    """


# (encoder_fn signature: T -> Python structure ready for further encoding)
# (decoder_fn signature: Python structure already fully decoded -> T)
EncoderFn = Callable[[Any], Any]
DecoderFn = Callable[[Any], Any]


class CodecRegistry:
    """Type ↔ tag-string codec table.

    Identity is by Python type. MRO walk allows subclass support but
    is intentionally limited: registering a codec for ``dict`` would
    shadow the built-in dict path and break encode(); don't do it.

    Tags are unique strings; collisions raise at register time so that
    a misconfigured deployment fails at import, not at first fold.
    """

    def __init__(self) -> None:
        self._by_type: dict[type, tuple[str, EncoderFn]] = {}
        self._by_tag:  dict[str, DecoderFn] = {}

    def register(
        self,
        type_: type,
        tag:   str,
        encode_fn: EncoderFn,
        decode_fn: DecoderFn,
    ) -> None:
        if not isinstance(tag, str) or not tag:
            raise ValueError(f"codec tag must be a non-empty str, got {tag!r}")
        if tag in self._by_tag:
            raise ValueError(
                f"codec tag {tag!r} already registered "
                f"(existing decoder: {self._by_tag[tag]!r})"
            )
        self._by_type[type_] = (tag, encode_fn)
        self._by_tag[tag]    = decode_fn

    def find_encoder(self, value: Any) -> tuple[str, EncoderFn] | None:
        t = type(value)
        # Direct match (the common case).
        hit = self._by_type.get(t)
        if hit is not None:
            return hit
        # MRO walk — supports e.g. str-Enum subclasses if user explicitly
        # registers their base. Skip `object` which would catch everything.
        for base in t.__mro__[1:-1]:
            hit = self._by_type.get(base)
            if hit is not None:
                return hit
        return None

    def get_decoder(self, tag: str) -> DecoderFn | None:
        return self._by_tag.get(tag)

    def __contains__(self, type_or_tag: Any) -> bool:
        if isinstance(type_or_tag, type):
            return type_or_tag in self._by_type
        return type_or_tag in self._by_tag

    def __repr__(self) -> str:
        return f"CodecRegistry(tags={sorted(self._by_tag)})"


# =====================================================================
# encode / decode — recursive walkers.
# =====================================================================

# Strings/bool/int/float go through unchanged. JSON-clean primitives.
_PRIMITIVES = (bool, int, float, str)


def encode(value: Any, registry: CodecRegistry) -> Any:
    """Recursively encode *value* to a JSON-clean Python structure."""
    if value is None or isinstance(value, _PRIMITIVES):
        return value

    if isinstance(value, list):
        return [encode(v, registry) for v in value]

    # Tuples are not JSON-native; v0.1 silently lowers to list. If the
    # user truly needs round-trip-stable tuples, they should register a
    # tuple codec or wrap the value in a typed container.
    if isinstance(value, tuple):
        return [encode(v, registry) for v in value]

    if isinstance(value, dict):
        # Reserved-key collision check first — fail loud rather than write
        # data that the decoder would misinterpret.
        if _LIPAS_TYPE_KEY in value or _LIPAS_DATA_KEY in value:
            raise UnserializableClaim(
                f"dict carries reserved codec key "
                f"({_LIPAS_TYPE_KEY!r} or {_LIPAS_DATA_KEY!r}); "
                f"these are not allowed in user data"
            )
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise UnserializableClaim(
                    f"dict key must be str, got "
                    f"{type(k).__name__}={k!r} "
                    f"(v0.1 limitation; write a custom codec for "
                    f"non-str-keyed dicts)"
                )
            out[k] = encode(v, registry)
        return out

    # Registered Python type → tagged form.
    found = registry.find_encoder(value)
    if found is None:
        raise UnserializableClaim(
            f"no codec registered for "
            f"{type(value).__module__}.{type(value).__name__}; "
            f"register one via CodecRegistry.register or pre-decode "
            f"the value to JSON-clean shape before folding"
        )
    tag, encoder_fn = found
    payload = encoder_fn(value)
    encoded_payload = encode(payload, registry)  # may itself contain typed values
    return {_LIPAS_TYPE_KEY: tag, _LIPAS_DATA_KEY: encoded_payload}


def decode(value: Any, registry: CodecRegistry) -> Any:
    """Recursively decode a JSON-clean structure back to Python."""
    if value is None or isinstance(value, _PRIMITIVES):
        return value

    if isinstance(value, list):
        return [decode(v, registry) for v in value]

    if isinstance(value, dict):
        # Tagged form first — a dict carrying both reserved keys is a
        # codec marker, not user data (encode() rejects collisions).
        if _LIPAS_TYPE_KEY in value:
            tag = value[_LIPAS_TYPE_KEY]
            decoder_fn = registry.get_decoder(tag)
            if decoder_fn is None:
                raise UnserializableClaim(
                    f"no decoder registered for tag {tag!r} "
                    f"(store may be from a newer lipas runtime)"
                )
            if _LIPAS_DATA_KEY not in value:
                raise UnserializableClaim(
                    f"tagged value missing {_LIPAS_DATA_KEY!r}: {value!r}"
                )
            payload = decode(value[_LIPAS_DATA_KEY], registry)
            return decoder_fn(payload)
        # Plain dict — recurse on values.
        return {k: decode(v, registry) for k, v in value.items()}

    raise UnserializableClaim(
        f"unexpected JSON value of type {type(value).__name__}: {value!r}"
    )
