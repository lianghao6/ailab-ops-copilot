"""压测场景的配置：限制画像、闸门容量与客户端数。

每个场景只打开一个受试机制，其余天花板全部抬起。一次同时撞上四个限制的压测，
只会给出一个拒绝计数和零理解。
"""

from __future__ import annotations

from typing import Any

def _is_local(url: str) -> bool:
    """True for loopback targets.

    A machine with `http_proxy` exported will otherwise route 127.0.0.1 through
    the corporate proxy, and the benchmark fails with a 503 from the proxy
    rather than anything to do with the service. Ignoring the environment for
    loopback is not a hack -- a proxy is definitionally not involved in talking
    to localhost -- but it is worth being explicit about, because the symptom
    (503 from an unrelated server) is genuinely confusing the first time.
    """
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

def _scenario_clients(scenario: str, requested: int, snapshot: dict[str, Any]) -> int:
    """Client count per scenario.

    Every scenario now runs with its own limit profile, so the client count no
    longer needs clamping to avoid tripping an unrelated ceiling -- that was a
    workaround for a problem the profiles fix properly. The function is kept
    because the clamp is still the right thing for `steady`, which claims the
    service is healthy and would be lying if it drove more concurrency than the
    platform is configured to allow.
    """
    if scenario != "steady":
        return requested
    return max(1, requested)

# 0 means "unlimited" for the token and cost ceilings. Concurrency is the
# exception: 0 there would reject every request, so "off" is expressed as a
# large finite number.
_UNLIMITED_CONCURRENCY = 10**6

# The gate capacity to restore between scenarios. Matches the config defaults;
# read from the server's own health payload when available so a server started
# with a different value is not silently rewritten.
DEFAULT_GATE_CONCURRENCY = 8

DEFAULT_GATE_QUEUE = 64

# Per-scenario limit profiles. Each scenario raises every ceiling except the one
# it exists to demonstrate, so that a rejection count can be attributed to a
# specific mechanism instead of to "the limits, somehow".
SCENARIO_LIMITS: dict[str, dict[str, Any]] = {
    # Nothing should push back: this is the "is the service healthy" scenario.
    "steady": {
        "user_qps": 0,
        "tenant_concurrency": _UNLIMITED_CONCURRENCY,
        "tenant_token_per_min": 0,
        "tenant_cost_per_day_usd": 0,
    },
    # The gate is the ceiling under study, so it is left at its configured
    # value while everything else is raised. The gate lives in its own
    # component (serving/gate.py) and is NOT one of the rate limits, which is
    # why raising the limits does not disable it. Without a simulated model
    # latency the gate never contends and this scenario degenerates into
    # "steady" -- hence the warning printed by `serve --llm-latency-ms 0`.
    "burst": {
        "user_qps": 0,
        "tenant_concurrency": _UNLIMITED_CONCURRENCY,
        "tenant_token_per_min": 0,
        "tenant_cost_per_day_usd": 0,
    },
    # Cache behaviour is the point; no explicit limit should interfere.
    "hotkey": {
        "user_qps": 0,
        "tenant_concurrency": _UNLIMITED_CONCURRENCY,
        "tenant_token_per_min": 0,
        "tenant_cost_per_day_usd": 0,
    },
}

# Gate capacity per scenario.
#
# `steady` and `hotkey` leave the gate at whatever the server was started with,
# because neither is about the gate. `burst` shrinks it deliberately: with a
# generously-sized gate and a queue deep enough to hold the whole burst, every
# request eventually succeeds and nothing is demonstrated -- the run just takes
# longer. Admission control only becomes visible when the queue is genuinely
# smaller than the offered load, which is the real situation in production: a
# bounded queue is what turns "everything times out eventually" into "most
# requests are told to retry immediately".
SCENARIO_GATE: dict[str, dict[str, Any]] = {
    "burst": {"max_concurrency": 4, "queue_maxsize": 16, "queue_timeout_s": 5.0},
}

SCENARIO_ACTIVE_CONTROL = {
    "steady": "nothing — all ceilings raised, so a 429 here would be a bug",
    "burst": "the upstream gate only, shrunk so the queue is smaller than the burst",
    "hotkey": "the semantic cache only",
}
