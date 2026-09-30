"""The agent's tool surface.

Design rules, each of which is a teachable point and several of which are
enforced by tests:

1. **Read-only.** No tool mutates the world. A diagnostic assistant that can
   restart jobs will eventually restart the wrong job; the KB has a document
   saying so and `test_tools.py` asserts it structurally.
2. **Bounded output.** Every tool returns a size-capped payload. An unbounded
   tool is how an agent blows its context window on a 40MB log, and the cap
   belongs in the tool, not in the model's good manners.
3. **Explicit truncation.** When a tool caps its output it says so, in the
   payload. Silent truncation is the worst failure mode in an agent system:
   the model cannot tell "there is nothing more" from "you were not shown
   the rest", and will confidently diagnose from a partial log.
4. **Typed errors, not exceptions.** A tool that raises kills the run; a tool
   that returns `{"error": ..., "hint": ...}` lets the model correct itself,
   which is the entire reason tool-calling beats a fixed pipeline.
5. **Latency is injected, not real.** Each tool has a configured simulated
   latency. That is what makes the concurrency lesson concrete: the gate in
   `serving` exists because a job's tools take hundreds of milliseconds each,
   and the numbers in the benchmark are honest about being simulated.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..datagen.world import Job, LogRecord, World
from ..llm.base import ToolSpec
from ..signals import classify_series

# Tool output caps. Chosen so a full diagnosis fits comfortably in a modest
# context window with room for the retrieved runbooks.
MAX_LOG_LINES = 60
MAX_JOBS = 25
MAX_SERIES_POINTS = 60
MAX_METRICS = 6


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    hint: str | None = None
    latency_ms: float = 0.0
    truncated: bool = False

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            d["data"] = self.data
        else:
            d["error"] = self.error
            if self.hint:
                d["hint"] = self.hint
        if self.truncated:
            d["truncated"] = True
        d["latency_ms"] = round(self.latency_ms, 1)
        return d

    def as_text(self) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., ToolResult]
    simulated_latency_ms: float = 40.0
    read_only: bool = True
    calls: int = 0
    errors: int = 0
    total_latency_ms: float = 0.0

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)

    def __call__(self, **kwargs: Any) -> ToolResult:
        t0 = time.perf_counter()
        self.calls += 1
        try:
            res = self.fn(**kwargs)
        except TypeError as exc:
            self.errors += 1
            res = ToolResult(
                ok=False,
                error=f"invalid arguments: {exc}",
                hint=f"expected parameters: {sorted(self.parameters.get('properties', {}))}",
            )
        except Exception as exc:  # a tool must never crash the loop
            self.errors += 1
            res = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        # Latency is simulated rather than slept: the benchmark measures
        # throughput of the *architecture*, and sleeping would make the test
        # suite slow for no gain. The number is still reported so
        # the accounting is honest.
        res.latency_ms = self.simulated_latency_ms + (time.perf_counter() - t0) * 1000.0
        self.total_latency_ms += res.latency_ms
        return res


class ToolRegistry:
    """Holds the tools, dispatches calls, and records the call log.

    The call log is not just telemetry: replaying it is how you answer "why did
    the answer change between two runs?", because the only thing that can make
    the same question produce a different answer is a different evidence set.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.call_log: list[dict[str, Any]] = []

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    def call(self, name: str, arguments: dict[str, Any], step: int = 0) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            res = ToolResult(
                ok=False,
                error=f"unknown tool {name!r}",
                hint=f"available tools: {self.names}",
            )
            self.call_log.append({"step": step, "tool": name, "arguments": arguments, "ok": False, "error": res.error})
            return res
        res = tool(**arguments)
        self.call_log.append(
            {
                "step": step,
                "tool": name,
                "arguments": arguments,
                "ok": res.ok,
                "error": res.error,
                "latency_ms": round(res.latency_ms, 1),
                "truncated": res.truncated,
            }
        )
        return res

    def stats(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "calls": t.calls,
                "errors": t.errors,
                "avg_latency_ms": round(t.total_latency_ms / t.calls, 1) if t.calls else 0.0,
                "read_only": t.read_only,
            }
            for name, t in self._tools.items()
        }


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------


def _job_view(j: Job) -> dict[str, Any]:
    """What a caller may see of a job. Ground-truth fields are excluded by
    construction, not by convention -- a test asserts their absence."""
    return {
        "job_id": j.job_id,
        "name": j.name,
        "team_id": j.team_id,
        "submitter": j.submitter,
        "kind": j.kind,
        "cluster": j.cluster,
        "queue": j.queue,
        "node_ids": j.node_ids,
        "world_size": j.world_size,
        "status": j.status,
        "exit_code": j.exit_code,
        "submitted_at": j.submitted_at,
        "started_at": j.started_at,
        "finished_at": j.finished_at,
        "duration_s": j.duration_s,
        "image": j.image,
        "workdir": j.workdir,
        "summary": j.summary,
    }


def build_registry(
    world: World,
    retriever: Any = None,
    simulated_latency_ms: dict[str, float] | None = None,
) -> ToolRegistry:
    """Build the standard tool set over a world.

    `simulated_latency_ms` lets a benchmark show what happens when one tool
    becomes slow -- which is the realistic scenario, and the one that exposes
    whether the agent has a timeout budget or just blocks forever.
    """
    lat = {"get_job": 30.0, "search_logs": 120.0, "get_metrics": 90.0,
           "list_jobs": 60.0, "search_runbooks": 45.0, "get_incident_history": 70.0,
           "get_exit_code_meaning": 20.0}
    lat.update(simulated_latency_ms or {})

    reg = ToolRegistry()

    # ---- get_job -------------------------------------------------------
    def get_job(job_id: str) -> ToolResult:
        j = world.job(job_id)
        if j is None:
            similar = [x.job_id for x in world.jobs if job_id[:8] in x.job_id][:5]
            return ToolResult(
                ok=False,
                error=f"job {job_id!r} not found",
                hint=("did you mean one of: " + ", ".join(similar)) if similar else "check the job id",
            )
        return ToolResult(ok=True, data=_job_view(j))

    reg.register(
        Tool(
            name="get_job",
            description=(
                "Fetch one job's record: status, exit code, timestamps, duration, cluster, queue, "
                "nodes, image and workdir. Call this FIRST for any diagnosis. Status NEVER_STARTED "
                "or a duration of only a few seconds means no runtime telemetry will exist, and "
                "that fact is itself diagnostic."
            ),
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "string", "description": "the job id, e.g. job-68bdf963-0000"}},
                "required": ["job_id"],
            },
            fn=get_job,
            simulated_latency_ms=lat["get_job"],
        )
    )

    # ---- search_logs ---------------------------------------------------
    def search_logs(
        job_id: str,
        query: str | None = None,
        level: str | None = None,
        rank: int | None = None,
        tail: bool = False,
        limit: int = 30,
    ) -> ToolResult:
        j = world.job(job_id)
        if j is None:
            return ToolResult(ok=False, error=f"job {job_id!r} not found", hint="call get_job to confirm the id")
        records = world.logs_for(job_id)
        if not records:
            return ToolResult(
                ok=True,
                data={
                    "job_id": job_id,
                    "lines": [],
                    "note": (
                        "no log records retained for this job. For a job that never started this is "
                        "expected: there was no container and therefore no application log."
                    ),
                },
            )

        filtered: list[LogRecord] = records
        if level:
            want = level.upper()
            filtered = [r for r in filtered if r.level.upper() == want]
        if rank is not None:
            filtered = [r for r in filtered if r.rank == rank]
        if query:
            rx = _safe_regex(query)
            if rx is not None:
                filtered = [r for r in filtered if rx.search(r.message)]
            else:
                needle = query.lower()
                filtered = [r for r in filtered if needle in r.message.lower()]

        if tail or not (query or level or rank is not None):
            # Default to the tail: the last lines are what a human reads first,
            # and they are where the terminal error usually sits.
            selected = filtered[-limit:]
            mode = "tail"
        else:
            selected = filtered[:limit]
            mode = "head"

        truncated = len(filtered) > len(selected)
        data = {
            "job_id": job_id,
            "mode": mode,
            "filter": {"query": query, "level": level, "rank": rank},
            "returned": len(selected),
            "total_matching": len(filtered),
            "total_retained": len(records),
            "lines": [
                {"ts": r.ts, "level": r.level, "rank": r.rank, "source": r.source, "message": r.message}
                for r in selected
            ],
        }
        if truncated:
            data["truncation_note"] = (
                f"showing {len(selected)} of {len(filtered)} matching lines; "
                "narrow the query or raise the limit"
            )
        if len(records) < 12 and j.duration_s > 1800:
            data["retention_note"] = (
                f"only {len(records)} lines retained for a job that ran {j.duration_s}s; "
                "verbose output has probably rotated away, so absence of an error here is not "
                "evidence that no error occurred"
            )
        return ToolResult(ok=True, data=data, truncated=truncated)

    reg.register(
        Tool(
            name="search_logs",
            description=(
                "Search a job's log. With no filter it returns the TAIL, which is where the terminal "
                "error usually is. Pass `level=\"ERROR\"` to see error lines only, or `rank` to "
                "isolate one rank: in multi-rank failures several ranks log the same cascade message "
                "and the causal error usually belongs to exactly one of them."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "query": {"type": "string", "description": "substring or regex to match against the message"},
                    "level": {"type": "string", "enum": ["INFO", "WARNING", "ERROR", "CRITICAL"]},
                    "rank": {"type": "integer", "description": "restrict to one rank"},
                    "tail": {"type": "boolean", "description": "force tail instead of head"},
                    "limit": {"type": "integer", "description": f"max lines to return (cap {MAX_LOG_LINES})"},
                },
                "required": ["job_id"],
            },
            fn=search_logs,
            simulated_latency_ms=lat["search_logs"],
        )
    )

    # ---- get_metrics ---------------------------------------------------
    def get_metrics(job_id: str, metric: str | None = None, max_points: int = 40) -> ToolResult:
        j = world.job(job_id)
        if j is None:
            return ToolResult(ok=False, error=f"job {job_id!r} not found", hint="call get_job to confirm the id")
        series = world.metrics_for(job_id)
        if not series:
            return ToolResult(
                ok=True,
                data={
                    "job_id": job_id,
                    "series": {},
                    "note": (
                        "no metric series available for this job. If the job never started, or died "
                        "during startup, no metrics are expected. Their absence is not a data error "
                        "and should not by itself be read as a diagnosis."
                    ),
                },
            )
        if metric:
            series = [s for s in series if s.metric == metric]
            if not series:
                have = sorted({s.metric for s in world.metrics_for(job_id)})
                return ToolResult(
                    ok=False,
                    error=f"no series named {metric!r} for this job",
                    hint=f"available: {have}",
                )
        series = series[:MAX_METRICS]
        out: dict[str, Any] = {}
        for s in series:
            values = s.values
            step = max(len(values) // max_points, 1)
            sampled = values[::step][:max_points]
            verdict = classify_series(values, s.metric)
            out[s.metric] = {
                "unit": s.unit,
                "n_points": len(values),
                "sampled": sampled,
                "first": values[0] if values else None,
                "last": values[-1] if values else None,
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "shape": verdict.shape,
                "shape_detail": verdict.detail,
            }
        return ToolResult(ok=True, data={"job_id": job_id, "ts_start": series[0].ts_start, "series": out})

    reg.register(
        Tool(
            name="get_metrics",
            description=(
                "Fetch a job's metric series with each series already classified into a shape "
                "(flat, noisy_high, cliff_to_zero, spike_then_zero, ramp_to_ceiling, "
                "sawtooth_rising, staircase, periodic_gap). The classification is the useful part: "
                "'cliff_to_zero' means a crash, 'flat' means a hang, 'ramp_to_ceiling' means "
                "resource exhaustion, 'staircase' means retry backoff rather than a slow link."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "metric": {"type": "string", "description": "restrict to one metric name"},
                    "max_points": {"type": "integer", "description": "max sampled points per series"},
                },
                "required": ["job_id"],
            },
            fn=get_metrics,
            simulated_latency_ms=lat["get_metrics"],
        )
    )

    # ---- list_jobs -----------------------------------------------------
    def list_jobs(
        team_id: str | None = None,
        status: str | None = None,
        cluster: str | None = None,
        queue: str | None = None,
        kind: str | None = None,
        limit: int = 20,
    ) -> ToolResult:
        limit = min(limit, MAX_JOBS)
        rows = world.jobs
        for fieldname, value in (("team_id", team_id), ("status", status), ("cluster", cluster),
                                 ("queue", queue), ("kind", kind)):
            if value:
                rows = [j for j in rows if str(getattr(j, fieldname, "")).upper() == str(value).upper()]
        # Newest first: an engineer investigating an incident wants recent work.
        rows = sorted(rows, key=lambda j: j.submitted_at, reverse=True)
        selected = rows[:limit]

        # Correlation is where the value is: if several jobs share a cluster or
        # a queue, that grouping is usually the actual finding.
        by_cluster: dict[str, int] = {}
        by_queue: dict[str, int] = {}
        by_team: dict[str, int] = {}
        for j in rows:
            if j.status == "SUCCEEDED":
                continue
            by_cluster[j.cluster] = by_cluster.get(j.cluster, 0) + 1
            by_queue[j.queue] = by_queue.get(j.queue, 0) + 1
            by_team[j.team_id] = by_team.get(j.team_id, 0) + 1

        return ToolResult(
            ok=True,
            data={
                "filter": {"team_id": team_id, "status": status, "cluster": cluster, "queue": queue, "kind": kind},
                "matched": len(rows),
                "returned": len(selected),
                "jobs": [_job_view(j) for j in selected],
                "failure_correlation": {
                    "by_cluster": dict(sorted(by_cluster.items(), key=lambda kv: -kv[1])[:6]),
                    "by_queue": dict(sorted(by_queue.items(), key=lambda kv: -kv[1])[:6]),
                    "by_team": dict(sorted(by_team.items(), key=lambda kv: -kv[1])[:6]),
                    "note": (
                        "failures concentrated in one cluster or pool suggest infrastructure; in one "
                        "queue, policy or quota; in one team, a shared dependency or config change"
                    ),
                },
            },
            truncated=len(rows) > len(selected),
        )

    reg.register(
        Tool(
            name="list_jobs",
            description=(
                "List or filter jobs by team, status, cluster, queue or kind. Use this to test "
                "whether a failure is isolated or part of a correlated group: the response includes "
                "a failure-correlation breakdown by cluster, queue and team, which is usually the "
                "actual finding when many jobs fail at once."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "team_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["SUCCEEDED", "FAILED", "NEVER_STARTED"]},
                    "cluster": {"type": "string"},
                    "queue": {"type": "string"},
                    "kind": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
            fn=list_jobs,
            simulated_latency_ms=lat["list_jobs"],
        )
    )

    # ---- search_runbooks ----------------------------------------------
    def search_runbooks(query: str, top_k: int = 4, category: str | None = None) -> ToolResult:
        if retriever is None:
            return ToolResult(ok=False, error="knowledge base unavailable", hint="run `make data` to build it")
        tags = (category,) if category else None
        res = retriever.search(query, top_k=min(top_k, 6), tag_filter=tags)
        return ToolResult(ok=True, data=res.to_dict())

    reg.register(
        Tool(
            name="search_runbooks",
            description=(
                "Hybrid (lexical + dense) search over runbooks and platform procedure documents. Use "
                "it with the SIGNALS you have observed, not with the question -- searching "
                "'Watchdog caught collective operation timeout' finds the cascade rule, whereas "
                "searching 'why did my job fail' finds nothing useful."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "observed symptoms, verbatim log text works best"},
                    "top_k": {"type": "integer"},
                    "category": {
                        "type": "string",
                        "enum": ["memory", "network", "storage", "scheduling", "code", "runtime", "external"],
                    },
                },
                "required": ["query"],
            },
            fn=search_runbooks,
            simulated_latency_ms=lat["search_runbooks"],
        )
    )

    # ---- get_incident_history -----------------------------------------
    def get_incident_history(
        team_id: str | None = None,
        category: str | None = None,
        scenario: str | None = None,
        limit: int = 8,
    ) -> ToolResult:
        limit = min(limit, 15)
        rows = world.incidents
        if team_id:
            rows = [i for i in rows if i.team_id == team_id]
        if category:
            rows = [i for i in rows if i.category == category]
        if scenario:
            rows = [i for i in rows if i.root_cause == scenario]
        rows = sorted(rows, key=lambda i: i.opened_at, reverse=True)[:limit]
        resolved = [i for i in rows if i.closed_at]
        return ToolResult(
            ok=True,
            data={
                "matched": len(rows),
                "incidents": [
                    {
                        "incident_id": i.incident_id,
                        "job_id": i.job_id,
                        "opened_at": i.opened_at,
                        "closed_at": i.closed_at,
                        "severity": i.severity,
                        "affected": i.root_cause_name,
                        "resolution": i.resolution,
                        "timeline": i.timeline,
                    }
                    for i in rows
                ],
                "stats": {
                    "n": len(rows),
                    "n_closed": len(resolved),
                    "note": "prior incidents are context, not proof: the same symptom can have a different cause",
                },
            },
        )

    reg.register(
        Tool(
            name="get_incident_history",
            description=(
                "Look up prior incidents, optionally filtered by team, category or cause. Useful for "
                "saying 'this team has hit this three times in two weeks', and for seeing which rival "
                "hypothesis was ruled out last time. History is context, not proof."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "team_id": {"type": "string"},
                    "category": {"type": "string"},
                    "scenario": {"type": "string", "description": "a cause id, e.g. collective_timeout"},
                    "limit": {"type": "integer"},
                },
            },
            fn=get_incident_history,
            simulated_latency_ms=lat["get_incident_history"],
        )
    )

    # ---- get_exit_code_meaning ----------------------------------------
    def get_exit_code_meaning(code: int) -> ToolResult:
        table = {
            0: ("success", "no failure"),
            1: ("generic application error", "read the last log lines; the code itself says nothing"),
            2: ("commonly a CLI or argument-parsing failure", "check how the entrypoint was invoked"),
            28: ("often ENOSPC from a write path", "check local disk and the write target"),
            134: ("abort, frequently a failed assertion", "look for an assertion or device-side assert"),
            137: (
                "SIGKILL — a symptom shared by several unrelated causes",
                "check, in order: a kernel OOM-kill line, an eviction event, a preemption event. "
                "If none is present the cause is genuinely undetermined",
            ),
            143: ("SIGTERM — most often a graceful scheduler shutdown", "preemption or eviction, not a code defect"),
        }
        if code in table:
            meaning, action = table[code]
            return ToolResult(ok=True, data={"exit_code": code, "meaning": meaning, "next_step": action})
        return ToolResult(
            ok=True,
            data={"exit_code": code, "meaning": "not in the reference table", "next_step": "read the log tail"},
        )

    reg.register(
        Tool(
            name="get_exit_code_meaning",
            description=(
                "Translate an exit code into its meaning and the next check to perform. Use this "
                "before treating an exit code as a diagnosis: 137 and 143 are shared by several "
                "different causes and are never sufficient on their own."
            ),
            parameters={
                "type": "object",
                "properties": {"code": {"type": "integer"}},
                "required": ["code"],
            },
            fn=get_exit_code_meaning,
            simulated_latency_ms=lat["get_exit_code_meaning"],
        )
    )

    return reg


def _safe_regex(pattern: str) -> re.Pattern | None:
    """Compile a user-supplied pattern, tolerating plain text.

    ReDoS is a real concern when a model supplies the pattern, so the pattern
    length is capped and a catastrophic-backtracking heuristic is applied.
    A plain substring is not valid regex and returns None, and the caller falls
    back to a substring search.
    """
    if len(pattern) > 200:
        return None
    if re.search(r"\([^)]*[+*]\)[+*]", pattern):  # (a+)+ style
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None
