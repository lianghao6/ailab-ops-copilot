"""Serving layer: HTTP, concurrency control, limits, cache and degradation."""

from .app import create_app
from .cache import (
    BreakerRegistry,
    CircuitBreaker,
    DegradeLevel,
    SemanticCache,
    cache_key,
    decide_degradation,
    normalise_question,
)
from .gate import GateRejected, Priority, UpstreamGate
from .limits import LimitExceeded, RateLimiter, SlidingWindow, TokenBucket


def __getattr__(name):
    # Compatibility consumers opt into the unsupported V1 service explicitly.
    if name in {"CopilotService", "DiagnosisRequest", "DiagnosisResponse", "ServiceError",
                "SessionStore", "evidence_only_answer"}:
        from . import service
        return getattr(service, name)
    raise AttributeError(name)

__all__ = [
    "BreakerRegistry",
    "CircuitBreaker",
    "CopilotService",
    "DegradeLevel",
    "DiagnosisRequest",
    "DiagnosisResponse",
    "GateRejected",
    "LimitExceeded",
    "Priority",
    "RateLimiter",
    "SemanticCache",
    "ServiceError",
    "SessionStore",
    "SlidingWindow",
    "TokenBucket",
    "UpstreamGate",
    "cache_key",
    "create_app",
    "decide_degradation",
    "evidence_only_answer",
    "normalise_question",
]
