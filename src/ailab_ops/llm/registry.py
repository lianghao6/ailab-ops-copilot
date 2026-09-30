"""按配置选择模型后端。"""

from __future__ import annotations

from ..config import Settings, get_settings
from .base import LLMClient
from .mock import MockLLMClient
from .openai_compat import OpenAICompatClient


def build_llm_client(settings: Settings | None = None) -> LLMClient:
    """返回配置指定的后端。

    取值无法识别时，打一条醒目的日志并退回离线推理器，而不是抛异常：一个环境
    变量拼错不该让整个服务起不来，它应该退回到明显离线的行为，并把这件事说出来。
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
