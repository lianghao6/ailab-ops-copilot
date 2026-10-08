# AILab Ops Copilot V2

面向训练与评测故障的证据调查平台。模型通过只读工具读取独立案例并检索 V2 Runbook，维护计划和假设，输出带证据引用的报告。动作必须先提出申请、人工批准，再显式执行；当前执行全部为模拟。

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

打开 `http://127.0.0.1:8080/` 进入 V2 调查工作台。顶栏由服务端健康信息确认在线模式；填写租户、案例标识（例如 `case-gpu-assert`）及可选问题，点击“开始调查”。模型密钥仅由服务进程配置，页面不接收密钥。

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

默认 `data/v2/replays/investigations.jsonl` 包含三个案例的完整计划、观察后检索、假设和引用报告，共 15 条录制。它们是**人工编写的模拟响应轨迹，不是实际模型采样**，每条记录注明来源。`scripts/build_v2_replays.py` 是显式维护这些记录的离线工具，运行时不会调用它。其他同目录小文件仅用于底层 replay 协议测试。

回放服务同样在 `/` 打开工作台，顶栏明确标记“回放”。选择内置案例并留空问题即可复现录制；“证据不足”案例展示保留未知的报告。运行模式由服务配置决定，浏览器不切换模式。

## 工作台阅读与操作

证据记录保留来源工具、观察时间范围、原始片段和来源元信息；假设对照并列显示支持与反证引用。报告中的引用可跳回对应证据，报告同时保留“已排除”“仍然未知”和建议。事件时间线展示服务端返回的模型、工具、策略、证据与状态事件；截断窗口会明确提示。

完成有引用的报告后，在“行动与审批”填写工具、JSON 参数、当前证据 ID、理由、风险和回滚方案，提交模拟提案。例如工具 `annotate_incident`，参数 `{"case_id":"case-gpu-assert","note":"已核查引用报告"}`，其余字段按实际审阅填写。提案默认展开参数，显示有效期。填写审阅人并勾选确认后才能批准；批准后必须再次单独确认“执行模拟”，拒绝必须填写原因。结果标明模拟，审计序列保留批准、拒绝和执行事件。期限已过的操作会禁用并提示刷新核验。

“恢复已有调查”按当前租户读取 Session ID；浏览器仅保存租户和当前 Session ID，重新载入从服务端恢复，不记住执行确认。服务进程重启会丢失调查、证据和审批。租户和操作者字段是模拟身份，没有生产认证。“取消本次等待”仅中止浏览器等待，服务端结果须刷新确认；写入后的连接异常也须先刷新核验。

页面采用原生目录链接、表单、按钮和可展开明细，可通过 Tab、Enter/Space 操作；窄屏转为单列。实现与 QA 边界、教材截图流程见 [V2 工作台说明](docs/ui/v2-investigation-workspace.md)。

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
| `GET /v2/investigations/{id}/evidence` | 读取证据展示资源 |
| `GET /v2/investigations/{id}/timeline` | 读取有界事件窗口，limit 最大 500 |
| `GET /v2/investigations/{id}/approvals` | 读取审批展示资源 |
| `POST /v2/investigations/{id}/approvals` | 提交 `proposal`，包含 tool、arguments、reason、risk、rollback、evidence_ids |
| `GET /v2/approvals/{id}` | 查看审批申请 |
| `POST /v2/approvals/{id}/approve` | 提供 actor，明确批准 |
| `POST /v2/approvals/{id}/reject` | 提供 actor 和 reason，拒绝 |
| `POST /v2/approvals/{id}/execute` | 提供 actor，仅执行已批准、未过期的模拟动作 |
| `GET /v2/health` | 显示 model_mode、模型、闸门和熔断状态 |

健康接口还提供 `/health` 和 `/v1/health` 别名。调查输出包含 mode、phase、evidence、hypotheses、report、stop_reason、trace_id、approval_state。调查请求可设置 max_steps、max_tokens 和 deadline_s。预算耗尽是 200 的 stopped 结果；replay miss 为 422；可重试上游错误为 503 并带 Retry-After；租户限制为 429。

动作参数 schema 声明当前 case_id 和 note；只允许该案例声明的动作。note 必须为 1–4000 个字符，提出审批和执行前重授权都会校验字符串边界。未批准执行返回 409。审批身份来自请求体；GET 的租户身份来自 tenant_id 查询参数。这里没有认证，真实部署必须在可信网关验证身份。所有动作始终返回 simulated=true，不会修改真实平台。

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

日志和指标结果各自携带来源的 telemetry、保留范围与缺失说明；Evidence 和终态归档保留相同信息。`truncated` 仅表示工具是否裁剪了输出，不能替代来源完整性或采集可用性。无需固定先读取 snapshot。

只读工具 `search_runbooks(query, top_k=3)` 仅检索 `data/v2/knowledge/runbooks/*.md`，按观察到的日志或指标信号做词法匹配；最多返回 5 篇，每篇摘录最多 4000 字符。结果包含稳定文档 ID、逻辑文件路径与行范围，并进入可引用的 Evidence。当前未接向量或混合检索实验；不读取 V1 派生知识库或评测标签。

调查证据、审批及已脱敏 trace 保存在进程内，重启即丢，不跨副本。Trace ID 用于关联会话的事件记录。当前没有状态容量/TTL 淘汰；持续运行部署需补持久化与清理策略。V2 禁用语义答案缓存，因为引用 ID 与调查会话绑定；旧缓存组件保留于 legacy 服务。

预算在同步调用前后检查，闸门等待时间取队列超时与调查剩余时间的较小值。OpenAI 每次 HTTP attempt 与退避都会检查取消和绝对截止时间；退避可被取消打断且不超过剩余时间，deadline 停止原因是 `budget_exhausted:deadline`。在途 HTTP 仍由客户端超时控制，每次请求的各阶段超时不超过调查剩余时间。Retry-After 会归一为有限非负值，默认最多 30 秒，可用 `AILAB_LLM_RETRY_MAX_DELAY_S` 配置；非法提示回退为有界 backoff，等待与 API JSON 使用归一后的值。Token 和成本按调用后实际 usage 记账；并发已准入调用可能造成临时超额，尚未实现 token 预留与结算。

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

默认 `/` 提供 V2 调查工作台，与 V2 API 和 CLI 共用运行时；V1 界面仅保留在 legacy 入口。非 editable 安装需部署仓库的 data/v2 资产并适配数据根路径，目前推荐源代码或 editable 安装。

MIT，见 LICENSE。
