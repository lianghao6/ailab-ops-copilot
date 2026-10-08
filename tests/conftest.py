"""Shared fixtures.

Two design decisions worth noting:

* The world is generated once per session (`scope="session"`) at a small size.
  Generating it per test would make the suite slow, and using the on-disk
  dataset would make the tests depend on whatever the developer last ran.
* Tests assert on *properties*, not on exact values wherever possible. A test
  that pins `job-68b290e6-0001 == oom_gpu` breaks every time the generator
  changes, and teaches nothing. A test that asserts "no ground-truth field is
  reachable through any tool" is both stable and load-bearing.
"""

from __future__ import annotations

import pytest

from ailab_ops.config import reset_settings
from ailab_ops.datagen import generate_world, load_playbook
from ailab_ops.rag import build_knowledge_base
from ailab_ops.legacy.runtime import Runtime
from ailab_ops.tools import build_registry


@pytest.fixture(scope="session")
def playbook():
    return load_playbook()


@pytest.fixture(scope="session")
def world():
    return generate_world(seed=4242, n_jobs=150)


@pytest.fixture(scope="session")
def kb(world):
    return build_knowledge_base(world.playbook)


@pytest.fixture(scope="session")
def registry(world):
    """Tools with zero simulated latency, so tests do not sleep."""
    kb_ = build_knowledge_base(world.playbook)
    return build_registry(world, kb_.retriever, simulated_latency_ms={k: 0.0 for k in
        ["get_job", "search_logs", "get_metrics", "list_jobs", "search_runbooks",
         "get_incident_history", "get_exit_code_meaning"]})


@pytest.fixture(scope="session")
def runtime(tmp_path_factory, world) -> Runtime:
    """Explicit legacy Runtime for preserved generated-world regression tests."""
    from ailab_ops.llm.registry import build_legacy_llm_client

    kb_ = build_knowledge_base(world.playbook)
    reg = build_registry(world, kb_.retriever, simulated_latency_ms={k: 0.0 for k in
        ["get_job", "search_logs", "get_metrics", "list_jobs", "search_runbooks",
         "get_incident_history", "get_exit_code_meaning"]})
    from ailab_ops.config import get_settings

    return Runtime(
        settings=get_settings(),
        world=world,
        registry=reg,
        llm=build_legacy_llm_client(get_settings()),
        kb=kb_,
        boot_ms=0.0,
        source="test",
    )


@pytest.fixture(autouse=True)
def _clean_env():
    """Keep a developer's `.env` or exported variables from changing test results."""
    yield
    reset_settings()
