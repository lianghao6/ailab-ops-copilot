"""Caching and the degradation chain.

Two mechanisms that both exist to protect the upstream, and that are worth
distinguishing clearly:

**Semantic cache.** An exact-match cache is nearly useless for an agent,
because two people investigating the same incident ask differently ("why did
job-X die" vs "what happened to job-X"). The cache therefore keys on the
*normalised question* plus the *evidence set the answer depended on*. The
second half is the part that is usually missing and the part that causes
correctness bugs: a cached diagnosis must be invalidated when the underlying
diagnosis changed. In this system the invalidation key is deliberately coarse
-- the answer is re-used only within a TTL and only if the cached answer was
not a refusal -- because a stale wrong answer is worse than a cache miss.

**Degradation chain.** When the upstream is unhealthy, the correct behaviour is
not to fail hard. It is to descend a ladder, announcing the descent:

    1. full tool-using diagnosis
    2. cached or historical answer for the same job or a twin of it
    3. an evidence-only summary produced without any model call
    4. an honest error with a retry hint

Step 3 is the interesting one and is what most implementations skip. It costs
nothing to run, needs no model, and still answers the actual question ("what is
wrong with this job") for the majority of failures -- because the evidence was
already gathered and the diagnosis was already computable from it.

**Circuit breaker.** Repeated upstream failures should stop being retried. The
breaker is deliberately simple: it opens after N consecutive failures, stays
open for a cooldown, then allows a single probe. Per-tenant breakers are
supported because one tenant's malformed requests should not open a breaker for
everyone -- that is a real outage amplifier.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# Words that carry no discriminating information in a question about a job.
# Removing them is what makes "why did job-X fail" and "what happened to job-X"
# land on the same cache key.
_QUESTION_NOISE = re.compile(
    r"\b(why|what|how|did|does|do|is|are|was|were|the|a|an|of|to|for|in|on|at|and|or|"
    r"please|tell|me|give|show|explain|happened|happen|went|wrong|fail|failed|failure|"
    r"root|cause|reason|help|about|with|this|that|it|job|please)\b",
    re.IGNORECASE,
)
_JOB_ID_RE = re.compile(r"\bjob-[0-9a-f]{6,12}-\d{3,6}\b")


def normalise_question(text: str) -> str:
    """Strip question words, keep the identifiers and the nouns that matter."""
    t = text.lower()
    t = _JOB_ID_RE.sub(" ", t)
    t = _QUESTION_NOISE.sub(" ", t)
    t = re.sub(r"[^a-z0-9_\s]", " ", t)
    return " ".join(t.split())


def cache_key(question: str, extra: str = "") -> str:
    """Key on the normalised question plus a version tag.

    The version tag is where evidence freshness would go: a real deployment
    puts a data-version or a log-offset in here so that a diagnosis is not
    served after its evidence changed. This implementation uses the TTL instead
    and says so in the README, because a coarse invalidation that is understood
    beats a fine one that is wrong.
    """
    base = normalise_question(question) + "||" + extra
    return hashlib.blake2b(base.encode("utf-8"), digest_size=12).hexdigest()


def tenant_scoped(tenant_id: str, key: str) -> str:
    """Namespace a cache key by tenant.

    This is not an optimisation, it is a correctness and security boundary. A
    cache shared across tenants leaks one organisation's incident content into
    another organisation's answers -- the classic cross-tenant cache-poisoning
    bug, and a genuinely serious one in an operations assistant, where the
    cached text contains job names, workdirs and log excerpts.

    It is deliberately implemented as a key-namespacing function rather than as
    "remember to pass the tenant to `get`" so that the boundary is impossible to
    forget: `SemanticCache.get` takes the tenant as a required argument.
    """
    return f"{tenant_id}:{key}"


@dataclass
class CacheEntry:
    key: str
    value: Any
    created_at: float
    ttl_s: float
    hits: int = 0
    question: str = ""
    tenant_id: str = ""

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl_s


class SemanticCache:
    """A TTL cache with near-duplicate question matching, scoped per tenant.

    The similarity check is a token-set Jaccard over normalised questions. It
    is deliberately not an embedding lookup: the number of distinct question
    phrasings against a fixed world is small, the cost of a miss is one
    diagnosis, and a token overlap is auditable in a way that a cosine
    threshold is not. A real deployment would swap this for a vector index and
    the interface would not change.

    Tenant scoping is enforced structurally: both `get` and `put` require a
    tenant id, keys are namespaced by it, and the near-match scan never looks
    outside the caller's own tenant. A shared cache in an operations assistant
    would leak job names, workdirs and log excerpts between organisations.
    """

    def __init__(self, ttl_s: float = 900.0, threshold: float = 0.86, max_entries: int = 512) -> None:
        self.ttl_s = ttl_s
        self.threshold = threshold
        self.max_entries = max_entries
        self._entries: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.near_hits = 0
        self.evictions = 0
        self.cross_tenant_rejections = 0

    def _sweep(self) -> None:
        dead = [k for k, e in self._entries.items() if e.expired]
        for k in dead:
            del self._entries[k]
            self.evictions += 1
        # Evict down to max_entries - 1, not max_entries: this runs *before* the
        # insertion that follows it, so leaving room for exactly one more entry
        # is what keeps the cache at its stated bound rather than one over it.
        target = max(self.max_entries - 1, 0)
        if len(self._entries) > target:
            for k, _ in sorted(self._entries.items(), key=lambda kv: kv[1].created_at)[
                : len(self._entries) - target
            ]:
                del self._entries[k]
                self.evictions += 1

    def get(self, tenant_id: str, question: str) -> tuple[Any, str] | None:
        """Return (value, how) where `how` is 'exact' or 'near'."""
        with self._lock:
            self._sweep()
            k = tenant_scoped(tenant_id, cache_key(question))
            e = self._entries.get(k)
            if e and not e.expired:
                e.hits += 1
                self.hits += 1
                return e.value, "exact"

            if self.threshold >= 1.0:
                self.misses += 1
                return None

            target = set(normalise_question(question).split())
            if not target:
                self.misses += 1
                return None
            best: tuple[float, CacheEntry] | None = None
            for cand in self._entries.values():
                if cand.expired or cand.tenant_id != tenant_id:
                    continue
                toks = set(normalise_question(cand.question).split())
                if not toks:
                    continue
                inter = len(target & toks)
                union = len(target | toks)
                sim = inter / union if union else 0.0
                if sim >= self.threshold and (best is None or sim > best[0]):
                    best = (sim, cand)
            if best is not None:
                best[1].hits += 1
                self.hits += 1
                self.near_hits += 1
                return best[1].value, "near"
            self.misses += 1
            return None

    def put(self, tenant_id: str, question: str, value: Any, ttl_s: float | None = None) -> None:
        with self._lock:
            self._sweep()
            k = tenant_scoped(tenant_id, cache_key(question))
            self._entries[k] = CacheEntry(
                key=k, value=value, created_at=time.time(),
                ttl_s=ttl_s or self.ttl_s, question=question, tenant_id=tenant_id,
            )

    def invalidate(self, tenant_id: str, question: str) -> bool:
        with self._lock:
            return self._entries.pop(tenant_scoped(tenant_id, cache_key(question)), None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            n = len(self._entries)
            per_tenant: dict[str, int] = {}
            for e in self._entries.values():
                per_tenant[e.tenant_id] = per_tenant.get(e.tenant_id, 0) + 1
        total = self.hits + self.misses
        return {
            "entries": n,
            "entries_by_tenant": per_tenant,
            "hits": self.hits,
            "misses": self.misses,
            "near_hits": self.near_hits,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
            "evictions": self.evictions,
            "ttl_s": self.ttl_s,
            "threshold": self.threshold,
            "scoped_per_tenant": True,
        }


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------


class BreakerState(str):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Consecutive-failure breaker with a single-probe half-open state."""

    name: str = "upstream"
    failure_threshold: int = 5
    cooldown_s: float = 20.0
    half_open_max: int = 1

    failures: int = 0
    successes: int = 0
    state: str = BreakerState.CLOSED
    opened_at: float = 0.0
    _half_open_in_flight: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    trips: int = 0

    def allow(self) -> bool:
        with self._lock:
            if self.state == BreakerState.CLOSED:
                return True
            if self.state == BreakerState.OPEN:
                if (time.time() - self.opened_at) >= self.cooldown_s:
                    self.state = BreakerState.HALF_OPEN
                    self._half_open_in_flight = 1
                    return True
                return False
            # HALF_OPEN: allow a bounded number of probes.
            if self._half_open_in_flight < self.half_open_max:
                self._half_open_in_flight += 1
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.successes += 1
            if self.state == BreakerState.HALF_OPEN:
                self.state = BreakerState.CLOSED
                self._half_open_in_flight = 0

    def release_probe(self) -> None:
        """Release a cooperatively interrupted probe without judging the backend."""
        with self._lock:
            if self.state == BreakerState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.state == BreakerState.HALF_OPEN:
                self.state = BreakerState.OPEN
                self.opened_at = time.time()
                self._half_open_in_flight = 0
                self.trips += 1
            elif self.failures >= self.failure_threshold:
                self.state = BreakerState.OPEN
                self.opened_at = time.time()
                self.trips += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            remaining = 0.0
            if self.state == BreakerState.OPEN:
                remaining = max(self.cooldown_s - (time.time() - self.opened_at), 0.0)
            return {
                "name": self.name,
                "state": self.state,
                "failures": self.failures,
                "successes": self.successes,
                "trips": self.trips,
                "retry_in_s": round(remaining, 2),
            }


class BreakerRegistry:
    """Per-name breakers, with the upstream sharing one global breaker."""

    def __init__(self, failure_threshold: int = 5, cooldown_s: float = 20.0) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> CircuitBreaker:
        with self._lock:
            b = self._breakers.get(name)
            if b is None:
                b = CircuitBreaker(
                    name=name, failure_threshold=self.failure_threshold, cooldown_s=self.cooldown_s
                )
                self._breakers[name] = b
            return b

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._breakers.items())
        return {name: b.snapshot() for name, b in items}


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------


class DegradeLevel(str):
    FULL = "full"           # complete tool-using diagnosis
    EVIDENCE_ONLY = "evidence_only"  # deterministic summary, no model call
    UNAVAILABLE = "unavailable"      # honest refusal with a retry hint


@dataclass
class DegradeDecision:
    level: str
    reason: str
    retry_after_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "reason": self.reason, "retry_after_s": self.retry_after_s}


def decide_degradation(
    breaker: CircuitBreaker,
    queued: int,
    queue_maxsize: int,
    upstream_failures_recent: int = 0,
) -> DegradeDecision:
    """Choose the rung of the ladder for this request.

    The order encodes the priority: a healthy system runs full, a moderately
    loaded one still runs full (queueing is not degradation), and only a system
    that is actually unhealthy descends. Descending too eagerly is its own
    failure mode -- users get second-class answers while the system is fine --
    so the trigger is upstream health, not load.
    """
    if breaker.state == BreakerState.OPEN:
        remaining = max(breaker.cooldown_s - (time.time() - breaker.opened_at), 0.0)
        return DegradeDecision(
            DegradeLevel.EVIDENCE_ONLY,
            f"upstream breaker open after {breaker.failures} consecutive failures "
            f"({breaker.trips} trips total); serving a deterministic evidence summary instead",
            retry_after_s=remaining,
        )
    if queued > queue_maxsize:
        return DegradeDecision(
            DegradeLevel.EVIDENCE_ONLY,
            f"{queued} requests queued (queue capacity {queue_maxsize}); shedding the model call",
            retry_after_s=2.0,
        )
    return DegradeDecision(DegradeLevel.FULL, "upstream healthy")
