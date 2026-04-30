"""LIPAS adapter layer — provider-neutral shapes and Protocol.

Phase 2.1 (this commit):
    - Data shapes: Usage, ErrorKind, content blocks, Reply, stream events
    - Request shape: Message, ToolSpec, Request
    - Pricing: ModelPrice, PriceTable, UnknownModelError
    - Forward estimate: ResourceEstimate
    - Protocol: LLMAdapter, complete(), StreamProtocolError

Phase 2.1 next:
    - Minimal Anthropic adapter spike implementing LLMAdapter.
"""
from .content import (
    ContentBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from .errors import ErrorKind
from .estimate import ResourceEstimate
from .pricing import ModelPrice, PriceTable, UnknownModelError
from .protocol import LLMAdapter, StreamProtocolError, complete
from .reply import Reply, StopReason
from .request import Message, Request, ToolSpec
from .streaming import (
    Delta,
    Done,
    StreamEvent,
    Thinking,
    ToolUseDelta,
)
from .usage import Usage
from .ollama import OllamaAdapter

__all__ = [
    # usage / errors
    "Usage",
    "ErrorKind",
    # content
    "TextBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "ContentBlock",
    # reply
    "Reply",
    "StopReason",
    # streaming
    "Delta",
    "ToolUseDelta",
    "Thinking",
    "Done",
    "StreamEvent",
    # request
    "Message",
    "ToolSpec",
    "Request",
    # pricing
    "ModelPrice",
    "PriceTable",
    "UnknownModelError",
    # estimate
    "ResourceEstimate",
    # protocol
    "LLMAdapter",
    "StreamProtocolError",
    "complete",
    # ollama
    "OllamaAdapter",
]
