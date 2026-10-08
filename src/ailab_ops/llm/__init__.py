"""Shared message contracts and OpenAI-compatible transport.

V2 inference is configured through ailab_ops.models (online or strict replay).
The unsupported V1 simulator is not imported or exported by this package.
"""

from .base import (
    ChatMessage,
    FinishReason,
    LLMClient,
    LLMResponse,
    ToolCall,
    ToolSpec,
    Usage,
    approx_tokens,
)
from .openai_compat import OpenAICompatClient, OpenAICompatError, VLLMClient

__all__ = [
    "ChatMessage",
    "FinishReason",
    "LLMClient",
    "LLMResponse",
    "OpenAICompatClient",
    "OpenAICompatError",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "VLLMClient",
    "approx_tokens",
]
