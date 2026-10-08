"""Credential-safe V2 tracing."""

from .events import TraceEvent
from .recorder import TraceRecorder

__all__ = ["TraceEvent", "TraceRecorder"]
