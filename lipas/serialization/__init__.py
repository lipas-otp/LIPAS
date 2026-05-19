"""LIPAS · Serialization Layer
Bidirectionally convert Claim and its fields between Python objects and JSON-clean structures.
Coverage in v0.1: Claim, Reply, Request, Message, ToolSpec, Usage, EffectKind.

Design points (see the documentation of the codec.py module):
- Tagged-JSON: Unknown/custom types are represented as {"__lipas_type__": ... , "__lipas_data__": ... } Packaging.
- Reject silent downgrading: Unregistered types directly raise UnserializableClaim, do not degrade to repr().
- dict keys must be str (v0.1 constraint; consistent with JSON).
- tuple silently converts to list (v0.1 compromise; pure JSON has no tuple; if fidelity is required, write your own codec).
"""

from .codec import (
    UnserializableClaim,
    CodecRegistry,
    encode,
    decode,
)
from .types import (
    register_builtin_codecs,
    make_default_codec_registry,
)

__all__ = [
    "UnserializableClaim",
    "CodecRegistry",
    "encode",
    "decode",
    "register_builtin_codecs",
    "make_default_codec_registry",
]
