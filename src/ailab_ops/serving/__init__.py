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
from .service import (
    CopilotService,
    DiagnosisRequest,
    DiagnosisResponse,
    ServiceError,
    SessionStore,
    evidence_only_answer,
)

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
