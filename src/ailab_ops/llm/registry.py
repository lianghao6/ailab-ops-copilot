"""Explicit V1 adapter factory. V2 uses models.build_model_gateway."""

from __future__ import annotations

from ..config import Settings, get_settings
from .base import LLMClient
from .openai_compat import OpenAICompatClient


def build_legacy_llm_client(settings: Settings | None = None) -> LLMClient:
    """Construct an explicitly requested legacy simulation or V1 adapter."""
    s = settings or get_settings()
    backend = (s.llm_backend or "mock").strip().lower()

    if backend in {"mock", "offline", "deterministic"}:
        from .mock import MockLLMClient
        return MockLLMClient(
            model=s.llm_model or "mock-diagnoser-v1",
            latency_s=max(s.llm_latency_ms, 0.0) / 1000.0,
        )

    if backend in {"openai", "openai_compat", "vllm", "sglang", "compatible"}:
        return OpenAICompatClient(
            base_url=s.llm_base_url,
            model=s.llm_model,
            api_key=s.llm_api_key,
            timeout_s=s.llm_timeout_s,
        )

    raise ValueError(f"Unknown legacy AILAB_LLM_BACKEND={backend!r}; valid values: mock | openai")
