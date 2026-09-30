"""Wiring: assemble a ready-to-run copilot from configuration.

Everything that has to exist once per process lives here, so that the server,
the CLI, the evaluation and the benchmark all build the *same* object graph.
A project like this fails in a specific way when each entry point wires itself
up: the benchmark measures a system the evaluation never tested.

Loading is also where the slow steps are, which is why they happen once at
startup rather than per request: generating the world, building the knowledge
base, and constructing the tool registry together take a noticeable fraction of
a second, and paying that per request would dominate the latency of a diagnosis
and make the concurrency numbers meaningless.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent import Agent
from .config import Settings, get_settings
from .datagen import World, generate_world, load_world, write_world
from .datagen.taxonomy import load_playbook
from .llm import LLMClient, build_llm_client
from .obs import Tracer
from .rag import build_knowledge_base
from .tools import ToolRegistry, build_registry


@dataclass
class Runtime:
    """The assembled system."""

    settings: Settings
    world: World
    registry: ToolRegistry
    llm: LLMClient
    kb: Any
    boot_ms: float = 0.0
    source: str = "generated"

    def new_agent(self) -> Agent:
        return Agent(self.llm, self.registry, self.settings)

    def new_tracer(self, record_content: bool = False) -> Tracer:
        return Tracer(record_content=record_content)

    def diagnose(
        self,
        question: str,
        deadline_s: float | None = None,
        record_content: bool = False,
        max_steps: int | None = None,
    ):
        agent = self.new_agent()
        tracer = self.new_tracer(record_content=record_content)
        return agent.run(question, deadline_s=deadline_s, tracer=tracer, max_steps=max_steps), tracer


def build_runtime(
    settings: Settings | None = None,
    data_dir: str | Path | None = None,
    regenerate: bool = False,
    simulate_latency_ms: dict[str, float] | None = None,
) -> Runtime:
    """Build the runtime.

    Data resolution order (deliberate, and worth explaining to a class):
    1. an existing dataset on disk, if present and not `regenerate`;
    2. otherwise generate one and write it out.

    Reusing the dataset on disk rather than regenerating it in memory matters
    because the *evaluation* depends on the artifact the *agent* was served:
    if each process generated its own world, the ground truth would no longer
    correspond to the data the tools return, and the accuracy number would be
    measuring nothing.
    """
    t0 = time.perf_counter()
    s = settings or get_settings()
    target = Path(data_dir) if data_dir else s.resolved_data_dir()

    playbook = load_playbook()
    if not regenerate and (target / "jobs.jsonl").exists():
        world = load_world(target)
        source = f"loaded:{target}"
    else:
        world = generate_world(seed=s.seed, n_jobs=s.n_jobs, playbook=playbook)
        write_world(world, target)
        source = f"generated:{target}"

    kb = build_knowledge_base(playbook)
    registry = build_registry(world, kb.retriever, simulated_latency_ms=simulate_latency_ms)
    llm = build_llm_client(s)

    boot = (time.perf_counter() - t0) * 1000.0
    return Runtime(
        settings=s, world=world, registry=registry, llm=llm, kb=kb, boot_ms=boot, source=source
    )


def default_question(world: World) -> tuple[str, str]:
    """Pick a failed job and a natural question about it.

    Used by the demo and by the smoke test. It deliberately picks a hard case
    when one exists, because an easy `oom_gpu` makes the architecture look like
    it works when what actually worked was a substring match.
    """
    candidates = [j for j in world.jobs if j.status != "SUCCEEDED"]
    if not candidates:
        return "", ""
    hard = [j for j in candidates if j.difficulty == "hard" and not j.is_insufficient_evidence]
    pool = hard or candidates
    import random

    j = random.Random(0).choice(sorted(pool, key=lambda x: x.job_id))
    return j.job_id, f"Why did {j.job_id} ({j.name}) fail? Give me the root cause and what to do about it."
