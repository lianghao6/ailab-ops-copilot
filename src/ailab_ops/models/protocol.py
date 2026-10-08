"""Synchronous model boundary during migration from the legacy chat client."""

from typing import Protocol, Sequence, runtime_checkable

from ailab_ops.llm.base import ChatMessage, LLMResponse, ToolSpec


class ModelBackendError(RuntimeError):
    """A credential-safe backend failure with machine-readable classification."""

    def __init__(self, message: str, *, kind: str = "error", phase: str | None = None,
                 status: int | None = None, retryable: bool = False,
                 retry_after_s: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.phase = phase
        self.status = status
        self.retryable = retryable
        self.retry_after_s = retry_after_s


class ModelConfigurationError(ValueError):
    """Model settings or a replay recording cannot satisfy the contract."""


@runtime_checkable
class ModelGateway(Protocol):
    mode: str
    model: str

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] | None = None,
                 *, max_tokens: int = 1024) -> LLMResponse: ...
