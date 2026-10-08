# V1 历史与临时回归入口

V2 是唯一默认产品路径。在线模型负责工具选择、计划、假设和带证据引用的报告；确定性代码负责预算、证据隔离、引用校验及审批。Replay 只匹配已有完整请求并返回人工编写的记录，不能代表在线模型能力。

最后一个 V1 版本为 Git commit `f4dabbe46dbc7dc46faebfe2049658e18aa9d0e9`（`Correct the refusal-guard count in lesson 5`）。可通过 Git 历史阅读或在单独目录检出：

```bash
git show f4dabbe46dbc7dc46faebfe2049658e18aa9d0e9:README.md
git worktree add ../ailab-ops-copilot-v1 f4dabbe46dbc7dc46faebfe2049658e18aa9d0e9
```

V1 的生成数据、MockLLMClient、signals.decide 及评分共享 `faults.yaml` 剧本。手写规则诊断不能作为大模型泛化或真实故障准确率的证据；旧版“离线推理器可以证明模型能力”的默认产品声明已移除。

当前为了已有回归 consumer 临时保留以下显式入口，全部不受支持，课程发布前应删除：

```bash
ailab-ops legacy gen-data
ailab-ops legacy demo
ailab-ops legacy ask 'Why did job-... fail?'
ailab-ops legacy eval --limit 20
ailab-ops legacy compare
ailab-ops legacy inspect job-...
ailab-ops legacy llm-stub
ailab-ops legacy serve --host 127.0.0.1 --port 8081
ailab-ops legacy bench --base-url http://127.0.0.1:8081
make legacy-demo
```

旧 bench 的 `/v1/jobs`、`/v1/diagnose` 请求仅适用于明确启动的 legacy 服务，不能对默认 V2 API 运行。旧 stub 仅是规则模拟器，不能冒充在线模型。`AILAB_LLM_BACKEND` 和模拟 latency 参数只用于 V1；V2 由 `AILAB_MODEL_MODE=online|replay` 控制。

保留 `llm/mock.py` 是因为旧 stub、显式 legacy factory 和 agent 回归仍调用它；保留 `signals.py` 是因为旧工具、规则测试、RAG/数据测试及旧课程仍依赖它。两者不再由默认 package、runtime 或 HTTP factory 导入。V1 runtime 已隔离到 `ailab_ops.legacy.runtime`，HTTP factory 已隔离到 `ailab_ops.serving.legacy_app`；旧 package 的少量显式兼容属性采用懒加载。

课程源文件、构建引擎、已生成 PDF 和 V1 静态 UI 属于待后续教材/UI 计划处理的历史资产，此次不删除。现有六课描述的是上述 V1 commit，其中旧 `make data`、`make bench` 和 V1 参数不能直接套用当前 V2 命令；复现请使用历史 worktree。V2 教材应针对稳定接口重新规划，不继续沿用旧规则系统的能力声明。
