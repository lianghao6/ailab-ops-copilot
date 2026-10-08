"""Synchronous model boundary during migration from the legacy chat client."""

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event
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


class ModelRequestInterrupted(RuntimeError):
    """A control-plane stop, distinct from a backend failure."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ModelRequestControl:
    cancelled: Event
    deadline_at: datetime

    def remaining(self) -> float:
        if self.cancelled.is_set():
            raise ModelRequestInterrupted("cancelled")
        remaining = (self.deadline_at - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise ModelRequestInterrupted("budget_exhausted:deadline")
        return remaining

    def wait(self, delay: float) -> None:
        self.cancelled.wait(min(delay, self.remaining()))
        self.remaining()


@runtime_checkable
class ModelGateway(Protocol):
    mode: str
    model: str

    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] | None = None,
                 *, max_tokens: int = 1024) -> LLMResponse: ...
