"""Choosing a backend from configuration."""

from __future__ import annotations

from ..config import Settings, get_settings
from .base import LLMClient
from .mock import MockLLMClient
from .openai_compat import OpenAICompatClient


def build_llm_client(settings: Settings | None = None) -> LLMClient:
    """Return the configured backend.

    Unknown values fall back to the mock with a clear message rather than
    raising, because a typo in an env var should not make the teaching project
    fail to start -- it should make it behave in the obviously-offline way and
    say so.
    """
    s = settings or get_settings()
    backend = (s.llm_backend or "mock").strip().lower()

    if backend in {"mock", "offline", "deterministic"}:
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

    import sys

    print(
        f"[ailab-ops] unknown AILAB_LLM_BACKEND={backend!r}; falling back to the offline mock. "
        f"valid values: mock | openai",
        file=sys.stderr,
    )
    return MockLLMClient(model="mock-diagnoser-v1")
