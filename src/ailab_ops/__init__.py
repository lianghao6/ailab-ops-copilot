"""AILab Ops Copilot — 企业级 AIOps Agent。

面向训练与评测平台的运维诊断助手：读取 job 记录、日志和指标，给出故障根因。

包内分层清晰，每一层都可以独立讲授和测试：

    datagen  平台数据生成（"世界"）
    rag      该世界知识库上的检索
    tools    agent 的只读工具面
    llm      模型适配层（离线推理器 + 任意 OpenAI 兼容端点）
    agent    把检索、工具和模型串起来的诊断循环
    obs      链路追踪与指标
    serving  FastAPI + SSE、并发闸门、限流、缓存
    eval     对准 ground truth 的准确率评测
    bench    并发压测
"""

__version__ = "0.1.0"
