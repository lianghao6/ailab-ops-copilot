"""The default V2 runtime: model-directed investigations with online or replay."""

from .config import Settings, get_settings


def build_runtime(settings: Settings | None = None, *, gateway=None):
    from .v2_runtime import InvestigationRuntime
    return InvestigationRuntime(settings or get_settings(), gateway=gateway)


def build_legacy_runtime(*args, **kwargs):
    """Explicit compatibility entry point for unsupported V1 simulations."""
    from .legacy.runtime import build_legacy_runtime as build
    return build(*args, **kwargs)
