"""V2 model gateway construction and public contracts."""

from ailab_ops.config import PROJECT_ROOT, Settings
from .openai import OpenAIModelGateway
from .protocol import ModelBackendError, ModelConfigurationError, ModelGateway
from .replay import ReplayMissError, ReplayModelGateway, canonical_request_hash


def build_model_gateway(settings: Settings) -> ModelGateway:
    if settings.model_mode == "replay":
        path = settings.model_replay_path
        return ReplayModelGateway(path if path.is_absolute() else PROJECT_ROOT / path)
    if settings.model_mode == "online":
        for name in ("llm_base_url", "llm_model", "llm_api_key"):
            if not getattr(settings, name).strip():
                raise ModelConfigurationError(f"Online model mode requires {name}")
        if settings.llm_api_key == "EMPTY":
            raise ModelConfigurationError("Online model mode requires AILAB_LLM_API_KEY")
        if settings.llm_model == "mock-diagnoser-v1":
            raise ModelConfigurationError("Online model mode requires AILAB_LLM_MODEL")
        return OpenAIModelGateway(base_url=settings.llm_base_url, model=settings.llm_model,
                                 api_key=settings.llm_api_key, timeout_s=settings.llm_timeout_s)
    raise ModelConfigurationError("AILAB_MODEL_MODE must be online or replay")


__all__ = ["ModelGateway", "ModelBackendError", "ModelConfigurationError", "OpenAIModelGateway",
           "ReplayModelGateway", "ReplayMissError", "canonical_request_hash", "build_model_gateway"]
