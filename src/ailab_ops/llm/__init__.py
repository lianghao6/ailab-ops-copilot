"""LLM adapters: the seam between the agent loop and whatever model is serving.

Three pieces:

* a small, provider-neutral message/tool/response vocabulary (`base`);
* a fully offline deterministic reasoner (`mock`) so the project runs with no
  network, no GPU and no cost, and so tests are reproducible;
* a client for any OpenAI-compatible `/chat/completions` endpoint (`openai_compat`)
  which is what you point at a real deployment.

The important design decision is that the agent loop talks to the `LLMClient`
protocol and never to a concrete provider. That is what makes the swap a
one-line config change, and it is also what makes the mock honest: it is a
genuine implementation of the same interface, not a stub that special-cases
the tests.
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
from .mock import MockLLMClient
from .openai_compat import OpenAICompatClient, OpenAICompatError, VLLMClient
from .registry import build_llm_client

__all__ = [
    "ChatMessage",
    "FinishReason",
    "LLMClient",
    "LLMResponse",
    "MockLLMClient",
    "OpenAICompatClient",
    "OpenAICompatError",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "VLLMClient",
    "approx_tokens",
    "build_llm_client",
]
