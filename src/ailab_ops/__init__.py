"""AILab Ops Copilot — a simulated enterprise AIOps agent for teaching.

The package is layered so each layer can be taught and tested on its own:

    datagen  -> synthetic platform data (the "world")
    rag      -> retrieval over that world's knowledge base
    tools    -> the agent's read-only tool surface
    llm      -> model adapters (offline mock + any OpenAI-compatible endpoint)
    agent    -> the diagnosis loop tying retrieval, tools and the model together
    obs      -> tracing and metrics
    serving  -> FastAPI + SSE, concurrency gates, limits, caches
    eval     -> accuracy against ground truth
    bench    -> concurrent load generation
"""

__version__ = "0.1.0"
