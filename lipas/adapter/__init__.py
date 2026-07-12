"""Provider-neutral adapter contract and optional provider adapters."""
from .errors import ErrorKind
from .protocol import LLMAdapter, StreamProtocolError, complete
from .types import (
    ContentBlock, Delta, Done, Message, ModelPrice, PriceTable, Reply,
    Request, ResourceEstimate, StopReason, StreamEvent, TextBlock,
    Thinking, ToolResultBlock, ToolSpec, ToolUseBlock, ToolUseDelta,
    UnknownModelError, Usage,
)

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
    # Provider adapters are lazy so ``import lipas`` needs no provider extra.
    "OllamaAdapter",
    "AnthropicAdapter",
    "OpenAIResponsesAdapter",
]


def __getattr__(name: str):
    """Load optional provider adapters only when an application asks for one."""
    if name == "OllamaAdapter":
        from .ollama import OllamaAdapter
        return OllamaAdapter
    if name == "AnthropicAdapter":
        from .anthropic import AnthropicAdapter
        return AnthropicAdapter
    if name == "OpenAIResponsesAdapter":
        from .openai import OpenAIResponsesAdapter
        return OpenAIResponsesAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
