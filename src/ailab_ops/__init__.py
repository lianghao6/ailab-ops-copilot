"""AILab Ops Copilot V2：模型驱动的证据调查与模拟动作审批。

cases 提供独立观察数据；models 提供 online / strict replay；investigation
维护计划、假设及引用报告；approvals 约束动作；observability 记录脱敏轨迹；
serving 提供 V2 API 与并发保护；evals 进行独立标签分层评测。
旧规则模拟器仅供显式 legacy 回归，历史说明见 docs/legacy-v1.md。
"""

__version__ = "0.1.0"
