"""评测：对准 ground truth 度量准确率、校准与成本。

报告围绕那些真正对应决策的区分来组织——准确率、拒答精确率、假自信率、混淆对、
成本。只报一个准确率，只能告诉你系统错了；它不会告诉你该改检索、改提示、改工具
还是改数据。
"""

from .cases import build_cases
from .models import (
    HIGH_CONFIDENCE,
    CaseResult,
    EvalReport,
)
from .reporting import write_cases, write_report
from .runner import run_eval

__all__ = [
    "HIGH_CONFIDENCE",
    "CaseResult",
    "EvalReport",
    "build_cases",
    "run_eval",
    "write_cases",
    "write_report",
]
