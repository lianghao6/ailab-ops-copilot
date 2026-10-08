"""Public investigation domain types."""

from .models import (
    Budget,
    Claim,
    Hypothesis,
    InvestigationPhase,
    InvestigationReport,
    InvestigationState,
)

__all__ = [
    "Budget", "Claim", "Hypothesis", "InvestigationPhase",
    "InvestigationReport", "InvestigationState", "InvestigationOrchestrator", "InvestigationSessionError",
]


def __getattr__(name):
    # Evidence validation imports investigation.models; keep the public facade
    # lazy to avoid an evidence <-> orchestrator import cycle.
    if name in {"InvestigationOrchestrator", "InvestigationSessionError"}:
        from .orchestrator import InvestigationOrchestrator, InvestigationSessionError
        return {"InvestigationOrchestrator": InvestigationOrchestrator,
                "InvestigationSessionError": InvestigationSessionError}[name]
    raise AttributeError(name)
