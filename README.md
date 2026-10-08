# AILab Ops Copilot V2

面向训练与评测故障的证据调查平台。模型通过只读工具读取独立案例，维护计划和假设，输出带证据引用的报告。动作必须先提出申请、人工批准，再显式执行；当前执行全部为模拟。

## 在线快速开始

需要 Python 3.10+。默认模型模式为 `online`，启动时必须配置模型和密钥：

```bash
pip install -e ".[dev]"
export AILAB_MODEL_MODE=online
export AILAB_LLM_BASE_URL=https://your-gateway.example/v1
export AILAB_LLM_MODEL=your-tool-calling-model
export AILAB_LLM_API_KEY=your-key

ailab-ops investigate --case case-gpu-assert
ailab-ops serve --host 127.0.0.1 --port 8080
```

使用 OpenAI 兼容的 Chat Completions 接口，支持完整响应和流式工具参数拼接。在线模型做真实工具选择与报告生成；本版本的工具数据仍是仓库里的三份独立策划案例，不连接真实训练平台。API 文档位于 `http://127.0.0.1:8080/docs`。

## 离线 replay

无需 API key、模型下载或网络：

```bash
ailab-ops replay --case case-gpu-assert
ailab-ops replay --case case-collective-timeout
ailab-ops replay --case case-insufficient-evidence
ailab-ops eval --mode replay
make demo                    # 同一 V2 replay 路径
make eval MODE=replay

AILAB_MODEL_MODE=replay ailab-ops serve --host 127.0.0.1
```

Replay 对完整消息历史和可用工具名称做精确哈希匹配，返回已有记录，不生成新回答。更换问题、工具输出或提示词后，未匹配输入会得到 `replay_miss`，API 返回 typed 422；不会悄悄切换到规则推理或在线服务。

默认 `data/v2/replays/investigations.jsonl` 包含三个案例的完整计划、工具调用、假设和报告。它们是**人工编写的模拟响应轨迹，不是实际模型采样**，每条记录注明来源。`scripts/build_v2_replays.py` 是显式维护这些记录的离线工具，运行时不会调用它。其他同目录小文件仅用于底层 replay 协议测试。

## 调查与审批 API

```bash
curl -s http://127.0.0.1:8080/v2/health
curl -s http://127.0.0.1:8080/v2/investigations \
  -H 'Content-Type: application/json' \
  -d '{"case_id":"case-gpu-assert","tenant_id":"tenant-01"}'
```

| 接口 | 用途 |
|---|---|
| `POST /v2/investigations` | 同步完成调查或返回明确停止状态 |
| `GET /v2/investigations/{id}` | 读取报告、证据、假设与审批状态 |
| `POST /v2/investigations/{id}/approvals` | 提交 `proposal`，包含 tool、arguments、reason、risk、rollback、evidence_ids |
| `GET /v2/approvals/{id}` | 查看审批申请 |
| `POST /v2/approvals/{id}/approve` | 提供 actor，明确批准 |
| `POST /v2/approvals/{id}/reject` | 提供 actor 和 reason，拒绝 |
| `POST /v2/approvals/{id}/execute` | 提供 actor，仅执行已批准、未过期的模拟动作 |
| `GET /v2/health` | 显示 model_mode、模型、闸门和熔断状态 |

健康接口还提供 `/health` 和 `/v1/health` 别名。调查输出包含 mode、phase、evidence、hypotheses、report、stop_reason、trace_id、approval_state。调查请求可设置 max_steps、max_tokens 和 deadline_s。预算耗尽是 200 的 stopped 结果；replay miss 为 422；可重试上游错误为 503 并带 Retry-After；租户限制为 429。

动作参数 schema 声明当前 case_id 和 note；只允许该案例声明的动作。当前策略校验尚未执行字符串 minLength/maxLength，属于已知限制。未批准执行返回 409。审批身份来自请求体；GET 的租户身份来自 tenant_id 查询参数。这里没有认证，真实部署必须在可信网关验证身份。所有动作始终返回 simulated=true，不会修改真实平台。

## 运行链路与生产保护

`build_runtime()` 默认构造 V2；CLI 和 API 共用对象图：

```text
案件观察数据 → case-scoped read tools → InvestigationOrchestrator
                                         ↓
                              gated ModelGateway (online/replay)
                                         ↓
                          Evidence → cited report → approval → simulation
```

模型调用使用已有有界并发闸门、排队超时、用户 QPS、租户并发、token/分钟、日成本限制和熔断器。调查由异步循环逐步推进，每一步先取得全局闸门，再提交专用线程池；没有默认线程池中的整轮调查等待队列。出队后重新检查最新已结算成本与截止时间。取消会立即移除排队请求；在运行的调用完成结算后停止，后续模型和工具不会继续执行。上游失败保持错误类型和重试提示，不借助旧规则推理补答案。

调查证据、审批及已脱敏 trace 保存在进程内，重启即丢，不跨副本。Trace ID 用于关联会话的事件记录。当前没有状态容量/TTL 淘汰；持续运行部署需补持久化与清理策略。V2 禁用语义答案缓存，因为引用 ID 与调查会话绑定；旧缓存组件保留于 legacy 服务。

预算在同步调用前后检查，闸门等待时间取队列超时与调查剩余时间的较小值，并在真正调用模型前再次检查绝对截止时间。在途 HTTP 由模型客户端超时控制。Token 和成本按调用后实际 usage 记账；并发已准入调用可能造成临时超额，尚未实现 token 预留与结算。

## 分层评测与验证

```bash
ailab-ops eval --mode online --repeats 3
ailab-ops eval --mode replay --repeats 2
PYTHONPATH=src python3 -m pytest tests/test_v2_runtime.py tests/test_v2_api.py -q
PYTHONPATH=src python3 -m pytest -q
```

评测单独读取 `data/v2/evals/labels.jsonl`；runtime、模型输入和工具不会读取隐藏标签。分别汇总工具选择、必要证据、引用有效性、根因、拒答、策略合规、延迟和 token 指标，并给出重复运行均值与离散度。未配置评分规则的维度返回 null，不冒充已测量分数。

Replay 得分检查录制轨迹及工程契约，**不能说明模型能力、泛化或真实故障诊断准确率**。在线测试使用 httpx MockTransport 或脚本化模型边界，测试套件不需要真实密钥或外网。

## 配置与兼容性

复制 `.env.example` 后填入在线配置，或显式切换 `AILAB_MODEL_MODE=replay`。完整 replay 文件可通过 `AILAB_MODEL_REPLAY_PATH` 指定。环境变量优先于 .env。

`investigate`、`demo`、`ask`、`eval` 和 `serve` 都使用 V2；`eval-v2` 保留为 `eval` 的兼容别名。直接 CLI 默认 online，必须配置模型和密钥；显式 `replay` 或 `--mode replay` 是默认产品唯一无需 key 的推理模式。`make demo` 明确调用 replay，`make serve` / `make eval` 默认 online，可传 `MODE=replay`。

V1 工具统一改为 `ailab-ops legacy <command>`，Make 对应 `legacy-*`，标为不支持的临时回归入口，课程发布前移除。历史版本、保留模块与旧课程适用范围见 [V1 历史说明](docs/legacy-v1.md)。

本任务只提供 API 与 CLI；已有 V1 静态 UI 未迁移至 V2。非 editable 安装需部署仓库的 data/v2 资产并适配数据根路径，目前推荐源代码或 editable 安装。

MIT，见 LICENSE。
