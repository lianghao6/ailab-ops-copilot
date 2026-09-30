"""工具注册中心与调度。

`ToolRegistry` 保存工具、派发调用、记录调用日志。调用日志不只是遥测：回放它是
回答「两次运行的答案为什么不同」的唯一手段，因为能让同一个问题产生不同答案的，
只有读到过不同的证据集。
"""

from __future__ import annotations

from ..llm.base import ToolSpec
from dataclasses import dataclass
from typing import Any
from typing import Callable
import time

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
