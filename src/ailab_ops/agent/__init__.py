"""Agent：有界的工具调用循环。

项目里其它一切的存在，都是为了让这一层足够小。循环是「调用模型 → 派发工具 →
回喂观测」的受控重复，配套四件生产必需的东西：步数预算、单次调用超时与整体
截止时间、把工具报错回喂给模型（而不是抛异常终止）、以及全链路 span。

循环本身同步。并发属于上一层——放在 serving 里才能被限界和度量。
"""

from .loop import (
    DEFAULT_SYSTEM,
    Agent,
    AgentResult,
    AgentStep,
    AgentTimeout,
)
from .parsing import parse_answer

__all__ = [
    "Agent",
    "AgentResult",
    "AgentStep",
    "AgentTimeout",
    "DEFAULT_SYSTEM",
    "parse_answer",
]
