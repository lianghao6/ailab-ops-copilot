"""Default V2 HTTP factory with an explicit V1-runtime compatibility adapter."""


def create_app(runtime=None):
    if runtime is None or callable(getattr(runtime, "investigate", None)):
        from .v2 import create_v2_app
        return create_v2_app(runtime)
    from ..legacy.runtime import Runtime
    if not isinstance(runtime, Runtime):
        raise ValueError("Expected a V2 investigation runtime or an explicit legacy Runtime")
    from .legacy_app import create_legacy_app
    return create_legacy_app(runtime)
