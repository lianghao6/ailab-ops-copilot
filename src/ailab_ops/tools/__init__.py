"""工具层：agent 的只读工具面。

约定：只读、输出有上限、截断时明确声明、错误以类型化结构返回而不是抛异常。
上限放在工具里而不是指望模型自觉，是因为一个无上限的工具会让模型把上下文
窗口耗在某个 40MB 的日志上。
"""

from .registry import Tool, ToolRegistry, ToolResult


def __getattr__(name):
    # Historical generated-world tools are loaded only by explicit consumers.
    if name in {"MAX_JOBS", "MAX_LOG_LINES", "MAX_METRICS", "MAX_SERIES_POINTS", "build_registry"}:
        from . import builtin
        return getattr(builtin, name)
    raise AttributeError(name)

__all__ = [
    "MAX_JOBS",
    "MAX_LOG_LINES",
    "MAX_METRICS",
    "MAX_SERIES_POINTS",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "build_registry",
]
