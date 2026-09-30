"""Central configuration, loaded from environment with sane offline defaults.

Everything the project needs to run is reachable from `Settings`. Defaults are
chosen so that a fresh clone works with zero configuration and zero network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Project root = two levels up from src/ailab_ops/config.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    # ---- synthetic data ----
    data_dir: Path = field(default_factory=lambda: Path(_env_str("AILAB_DATA_DIR", "./data/generated")))
    seed: int = field(default_factory=lambda: _env_int("AILAB_SEED", 20260929))
    n_jobs: int = field(default_factory=lambda: _env_int("AILAB_N_JOBS", 400))

    # ---- llm backend ----
    llm_backend: str = field(default_factory=lambda: _env_str("AILAB_LLM_BACKEND", "mock"))
    llm_model: str = field(default_factory=lambda: _env_str("AILAB_LLM_MODEL", "mock-diagnoser-v1"))
    llm_base_url: str = field(default_factory=lambda: _env_str("AILAB_LLM_BASE_URL", "http://127.0.0.1:8001/v1"))
    llm_api_key: str = field(default_factory=lambda: _env_str("AILAB_LLM_API_KEY", "EMPTY"))
    llm_timeout_s: float = field(default_factory=lambda: _env_float("AILAB_LLM_TIMEOUT_S", 60.0))
    llm_max_tokens: int = field(default_factory=lambda: _env_int("AILAB_LLM_MAX_TOKENS", 1024))
    # Simulated think time per model call, in milliseconds. Zero by default so
    # the test suite is fast, but a benchmark MUST set this: with an instant
    # model there is never more than one call in flight, so admission control,
    # queueing and degradation are all unobservable. A realistic value is
    # 400-1500ms per call for a served 7B-70B model.
    llm_latency_ms: float = field(default_factory=lambda: _env_float("AILAB_LLM_LATENCY_MS", 0.0))

    # ---- upstream protection ----
    upstream_concurrency: int = field(default_factory=lambda: _env_int("AILAB_UPSTREAM_CONCURRENCY", 8))
    queue_maxsize: int = field(default_factory=lambda: _env_int("AILAB_QUEUE_MAXSIZE", 64))
    queue_timeout_s: float = field(default_factory=lambda: _env_float("AILAB_QUEUE_TIMEOUT_S", 30.0))

    # ---- rate limits & budget ----
    user_qps: float = field(default_factory=lambda: _env_float("AILAB_USER_QPS", 2.0))
    tenant_concurrency: int = field(default_factory=lambda: _env_int("AILAB_TENANT_CONCURRENCY", 16))
    tenant_token_per_min: int = field(default_factory=lambda: _env_int("AILAB_TENANT_TOKEN_PER_MIN", 200_000))
    tenant_cost_per_day_usd: float = field(default_factory=lambda: _env_float("AILAB_TENANT_COST_PER_DAY_USD", 5.0))
    price_input_per_mtok: float = field(default_factory=lambda: _env_float("AILAB_PRICE_INPUT_PER_MTOK", 0.15))
    price_output_per_mtok: float = field(default_factory=lambda: _env_float("AILAB_PRICE_OUTPUT_PER_MTOK", 0.60))

    # ---- cache ----
    semantic_cache_enabled: bool = field(default_factory=lambda: _env_bool("AILAB_SEMANTIC_CACHE_ENABLED", True))
    semantic_cache_ttl_s: float = field(default_factory=lambda: _env_float("AILAB_SEMANTIC_CACHE_TTL_S", 900.0))
    semantic_cache_threshold: float = field(
        default_factory=lambda: _env_float("AILAB_SEMANTIC_CACHE_THRESHOLD", 0.86)
    )

    # ---- server ----
    host: str = field(default_factory=lambda: _env_str("AILAB_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("AILAB_PORT", 8080))
    log_level: str = field(default_factory=lambda: _env_str("AILAB_LOG_LEVEL", "INFO"))

    # ---- agent loop ----
    max_steps: int = field(default_factory=lambda: _env_int("AILAB_AGENT_MAX_STEPS", 8))

    def resolved_data_dir(self) -> Path:
        p = self.data_dir
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p

    def ensure_dirs(self) -> Path:
        d = self.resolved_data_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d


_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    """Process-wide settings singleton."""
    global _settings
    if _settings is None or reload:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Test helper: drop the cached singleton so env changes take effect."""
    global _settings
    _settings = None
