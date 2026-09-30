# AILab Ops Copilot

面向训练与评测平台的企业级 AIOps Agent。你给它一个 job id，它去读 job 记录、日志和指标，然后告诉你这个 job 为什么挂了、该怎么处理。

Agent 系统真正的难点不在 agent loop——那部分两百行就写完了——而在它外面那圈工程：昂贵模型服务前面的有界并发、多维限流与成本预算、语义缓存、降级阶梯、链路追踪，以及一套能证明这些到底有没有用的评测。这些东西项目里都有，而且全部离线可跑，不依赖网络、GPU 或 API key。

---

## 目录

- [它能做什么](#它能做什么)
- [快速开始](#快速开始)
- [架构](#架构)
- [四个值得细看的点](#四个值得细看的点)
- [数据生成](#数据生成)
- [故障剧本](#故障剧本)
- [接入真实模型](#接入真实模型)
- [并发、限流与降级](#并发限流与降级)
- [评测](#评测)
- [项目结构](#项目结构)
- [配置](#配置)
- [已知局限](#已知局限)
- [上手练习](#上手练习)

---

## 它能做什么

给一个 job id，它查证后给出结构化诊断。

```
$ ailab-ops demo

world      loaded:./data/generated  (boot 139ms)
backend    mock-diagnoser-v1
question   Why did job-68c151b5-0272 (pretrain-speech-990) fail?
           Give me the root cause and what to do about it.

agent steps:
   1. [ok ] get_job({"job_id": "job-68c151b5-0272"})                0ms
   2. [ok ] search_logs({"job_id": "...", "level": "ERROR"})       1ms
   3. [ok ] get_metrics({"job_id": "..."})                        1ms
   4. [ok ] search_runbooks({"query": "AssertionError gpu_util_pct cliff_to_zero"})  2ms
   5. [ok ] get_exit_code_meaning({"code": 134})                  0ms
   6. [llm] final answer                                          2ms
==============================================================================
ROOT CAUSE   rank_crash_assert   Rank crashed on a device-side assert
confidence   0.97

Job job-68c151b5-0272 (FAILED, exit 134) — rank_crash_assert: Rank crashed on a
device-side assert. Decided by unique signature under cascade.

EVIDENCE
  · status=FAILED exit=134 duration=7239s cluster=ailab-prod-b queue=gpu-preemptible
  · first non-cascade error (rank 0, 2025-09-10T12:17:44Z): AssertionError
  · cascade signature: 9 timeout lines across ranks [2,5,6,7,4,1,3,0];
    treated as a symptom, not the cause
  · metric gpu_util_pct: cliff_to_zero (ran near 52.53 then collapsed to zero)

RULED OUT
  · collective_timeout — scored lower: cascade-shaped logs but no unique signature
  · watchdog_hang — scored lower

REMEDIATION
  · Guard the numerics that fed the assert (clamp, nan_to_num, fp32 for the
    reduction). Because the assert kills one rank, expect a collective timeout
    cascade afterwards — fix the assert, the cascade disappears.

GROUND TRUTH（agent 看不到，此处仅用于对照）
  root cause   rank_crash_assert   difficulty hard   confounders ['port_conflict']
  agent said   rank_crash_assert   -> AGREES
```

这类故障十有八九是这个形状：一张卡先死，幸存的其他卡随后全部报 collective timeout，而这个 timeout 恰恰是**出现次数最多**的错误。把它当成根因，是分布式训练里最常见的误诊。能不能判对，就是检验这套系统的关键。

---

## 快速开始

需要 Python 3.10+。

```bash
git clone https://github.com/lianghao6/ailab-ops-copilot.git
cd ailab-ops-copilot
pip install -e ".[dev]"

make data          # 生成平台数据集（约 2 秒）
make demo          # 离线跑完一次完整诊断
make test          # 159 个单元测试，约 3.5 秒

make serve         # HTTP API + 单文件 Web UI，监听 :8080
make eval          # 对准 ground truth 跑评测
make bench         # 并发压测（需要先跑着 make serve）
```

也可以直接用 CLI：

```bash
ailab-ops gen-data --seed 1234 --jobs 500
ailab-ops inspect job-68c151b5-0272      # 打印 job 全部信息 + ground truth + 假设打分
ailab-ops ask "why did job-68c151b5-0272 fail?"
ailab-ops compare                        # 对比检索融合模式
```

### Web UI

跑 `make serve`，打开 <http://127.0.0.1:8080>。页面上能实时看到：并发闸门、限流与预算状态、缓存命中率、熔断器、agent 的步骤序列。右下角有一个**故障注入按钮**，按下后系统会认为模型服务已经挂掉，你可以直接观察它如何降到确定性的「仅证据」通道，以及恢复后如何自愈。

---

## 架构

```
                       ┌──────────────────────────────────────────┐
   client ── HTTP ──▶  │  serving/                                │
                       │    limits.py    单用户 QPS、租户并发、      │
                       │                 token 速率、成本预算        │
                       │    cache.py     语义缓存（租户隔离）        │
                       │                 熔断器 + 降级阶梯           │
                       │    gate.py      上游准入控制（并发闸门）    │
                       │    service.py   请求流水线                 │
                       │    app.py       FastAPI + SSE              │
                       └───────────────┬──────────────────────────┘
                                       │  （每次模型调用占一个槽位）
                       ┌───────────────▼──────────────────────────┐
                       │  agent/    有界的工具调用循环               │
                       │    ↕ llm/     离线推理器 | OpenAI 兼容后端  │
                       └───────────────┬──────────────────────────┘
                                       │
              ┌────────────────────────┼────────────────────────┐
              ▼                        ▼                        ▼
      ┌───────────────┐        ┌───────────────┐       ┌───────────────┐
      │ tools/        │        │ rag/          │       │ signals.py    │
      │ 7 个只读工具   │        │ BM25F + 向量   │       │ 形态分类       │
      │               │        │ RRF 融合       │       │ 级联规则       │
      └───────┬───────┘        └───────┬───────┘       │ 假设打分       │
              │                        │               └───────────────┘
              ▼                        ▼
      ┌───────────────────────────────────────┐
      │  datagen/  平台数据                    │
      │   faults.yaml ─▶ 生成器 ─▶ world       │
      └───────────────────────────────────────┘
```

建议按这个顺序读代码：`datagen/`（数据从哪来）→ `signals.py`（推理长什么样）→ `tools/`（agent 能看到什么）→ `agent/`（那个循环）→ `serving/`（所有让它在生产里活得下来的东西）。

---

## 四个值得细看的点

### 1. 级联规则是代码，不是文档

`signals.py::extract_log_evidence` 把一份多卡日志压缩成人类真正会用的证据：按时间排序、把级联行和因果行分开、返回第一条**非级联**错误。随后 `decide()` 在「唯一的支撑证据是一个被多个场景共用的 timeout 字符串」时拒绝作答。

知识库里的每份 runbook 都写着这条规则，这里是执行它的代码。`tests/test_signals.py` 为每个分支都留了用例——包括一个真实的坑：早先版本把 `NCCL timeout`、`some ranks did not join` 也划进了级联特征，结果 `collective_timeout` 这个场景永远无法被识别，因为它自己的签名全被判成了它要区分的那种症状。

### 2. 置信度校准是一等指标

`eval/` 在 accuracy 之外，同时报告 **abstention precision**（拒答精确率）和 **false-confidence rate**（假自信率）。原因是：一个从不拒答的系统，accuracy 反而更高，但这两项都更差。只看 accuracy 会把这个问题藏起来。

数据集里故意放了「正确答案就是证据不足」的案例——遥测没采集、详细日志被轮转掉了——此时拒答算正确。想直观感受这个权衡，改一下 `signals.py` 里的 `MIN_MARGIN` 再跑 `make eval`。

### 3. 上游闸门

`serving/gate.py` 限制同时在飞的模型调用数，配套有界队列、等待超时和优先级。

它是把「模型服务被打挂、所有请求全失败」变成「大部分请求成功、超出的立刻被告知重试」的那个组件。理由很实在：典型推理服务同时只持有少量序列，超过之后延迟超线性上升，最终开始拒绝或重启——过载时的失败模式不是「请求变慢」，而是「**每一个**请求都失败，包括那些本来能成功的」。闸门也是最容易被漏掉、且漏掉代价最大的一个组件。

### 4. 降级阶梯

模型不可用时，系统不是失败，而是逐级下降：`full` → `evidence_only` → `unavailable`。

中间那一级最有意思。工具是对本地数据的廉价读取，打分是确定性代码，所以**同一条证据流水线在完全不调用模型的情况下仍然能出答案**。很多实现会漏掉这一级，直接从「有模型」跳到「报错」——而实际上降级并不等于无话可说。按一下 UI 里的故障注入按钮就能看到。

---

## 数据生成

平台数据由 `datagen/` 生成，核心事实如下：

- **可复现。** `--seed` 固定整个数据集。同一个种子给出逐字节相同的结果（`tests/test_world.py::test_generation_is_deterministic`）。
- **互相独立。** 每个人用不同种子跑 `make data`，得到一份自洽、答案独立的数据集，不会互相抄到答案。
- **防泄漏。** `tests/test_world.py` 和 `tests/test_tools.py` 断言：任何 ground-truth 字段都无法通过任何工具拿到。

### Ground truth 是独立产物

`data/generated/ground_truth.jsonl` 保存了每个失败 job 的答案，写在独立文件里，工具永不读取。

「agent 看不到答案」因此是**数据布局的属性**——可被测试检验；而不是依赖 agent 的自律——那不可检验。

---

## 故障剧本

`src/ailab_ops/datagen/faults.yaml` 是整个项目唯一的真相来源。**29 种故障场景**，分 8 大类：

| 类别 | 场景数 | 例子 |
|---|---|---|
| `memory` | 3 | GPU OOM、宿主内存 OOM-kill、缓慢泄漏 |
| `network` | 4 | 依赖下载超时、路由不可达、DNS 失败、集合通信超时 |
| `runtime` | 5 | device-side assert、watchdog 卡死、死锁、端口冲突、无法归因的 SIGKILL |
| `storage` | 4 | 本地磁盘写满、远端配额、对象存储读失败、NFS stale handle |
| `scheduling` | 4 | 抢占、驱逐、无法调度、镜像拉取失败 |
| `code` | 5 | 编译错误、缺依赖、配置错误、schema 漂移、checkpoint 不兼容 |
| `external` | 3 | 429 限流、上游 5xx、硬配额 |
| `unknown` | 1 | 没有决定性证据可用 |

下游一切都从这一份文件派生：生成器（有哪些故障、多频繁、长什么样）、知识库（每个场景的 runbook）、推理打分（证据匹配假设）、评测（ground-truth 标签）。**在那一份文件里加一个场景，它会出现在所有地方**，包括围绕整本剧本展开的测试。

每个场景声明这些字段：

```yaml
- id: collective_timeout
  category: network
  difficulty: hard          # easy | medium | hard —— 决定评测分层
  prevalence: 5             # 在其难度层内的相对采样权重
  distractors: [...]        # 证据与之重叠的干扰场景
  signals:
    log_patterns: [{ pattern: "Watchdog caught collective operation timeout", weight: 1.00 }]
    metric_shapes: [{ name: gpu_util_pct, shape: staircase, weight: 0.70 }]
    exit_codes: [1, 137, 143]
  remediation: >
    Find the rank that stopped first — it is usually not rank 0 ...
  runbook:
    title: "Runbook: collective timeout is usually a cascade, not a cause"
    body: > ...    # 会成为一篇可被检索的知识库文档
```

### 难度来自数据，不来自模型

- **easy** —— 一个清晰的签名。
- **medium** —— 同时存在一个看似合理的对手。
- **hard** —— 两个重叠的签名，**外加**一个真实干扰项，逼 agent 去辨别而不是模式匹配。由 `tests/test_signals.py` 里的断言守着。
- **unknown** —— 故意不给任何遥测。拒答就是正确答案，且按正确计分。

---

## 接入真实模型

默认后端是离线推理器（`llm/mock.py`）。它做的是和一个真实模型完全相同的工作：规划下一步调哪个工具、读结果、给竞争假设打分、写出结构化答案。它不是桩——它走的是同一套工具接口，所以换成真实模型时，走的管线完全一致；而它的准确率本身就是这套架构的真实上限。

指向真实端点：

```bash
export AILAB_LLM_BACKEND=openai
export AILAB_LLM_BASE_URL=http://your-gateway/v1
export AILAB_LLM_MODEL=your-model
export AILAB_LLM_API_KEY=...

ailab-ops ask "why did job-... fail?"
```

任何 OpenAI 兼容的 `/chat/completions` 端点都能用：vLLM、SGLang、TGI、lmdeploy、LiteLLM、one-api，或内部网关。客户端（`llm/openai_compat.py`）处理了 connect/read 分离超时、带抖动的重试退避，以及参数以 JSON 字符串分片到达的流式工具调用。

**想不依赖任何基础设施就试一下**，项目自带一个本地 stub：

```bash
ailab-ops llm-stub --port 8001 &          # 一个真实的 HTTP OpenAI 兼容服务
export AILAB_LLM_BACKEND=openai
export AILAB_LLM_BASE_URL=http://127.0.0.1:8001/v1
export AILAB_LLM_MODEL=stub-reasoner-v1
ailab-ops serve
```

它还能注入故障，用来验证重试和熔断逻辑确实在工作：

```bash
curl -X POST "http://127.0.0.1:8001/v1/admin/fail-rate?rate=0.3"
```

---

## 并发、限流与降级

项目里设了四种彼此独立的「天花板」。把它们混为一谈是这套系统最容易犯的错：

| 限制器 | 它回答的问题 | 没有它会怎样 |
|---|---|---|
| 单用户 QPS | 是不是某个调用方在滥用？ | 一个客户端拖垮所有人 |
| 租户并发 | 一个组织的模型用量占了多少？ | 一个吵闹的租户打满整个集群 |
| 租户 token/分钟 | 持续生成量是多少？ | 一直无法缓解的过载 |
| 租户 美元/天 | 这要花我们多少钱？ | 一张意外的账单 |
| **上游闸门** | 允许多少个调用同时在飞？ | **模型服务被打挂** |

闸门最重要。原因在上一节说过：过载的失败模式是全面失败，而不是普遍变慢。闸门限制在飞调用数、限制队列长度、给出等待超时。它刻意不跟限流是同一套机制，下面的压测也会单独测它。

### 压测

```bash
make serve                       # 一个终端
PYTHONPATH=src python -m ailab_ops.cli serve --llm-latency-ms 400   # 想让闸门真正咬住就加这个
make bench                       # 另一个终端
```

三个场景，各自隔离一个机制：

- **steady** —— 问题各不相同、每个用户一个 worker、所有天花板抬起。预期 120/120 成功、零拒绝；这里出现拒绝就是 bug。
- **burst** —— 闭环客户端、绕过单用户限流、闸门缩到 4 槽队列，使得实际施压大于队列容量。预期约 100 次 queue-full、8 次 queue-timeout，且 `in_flight` 稳定停在配置上限，不会超过。
- **hotkey** —— 85% 的问题描述同一起事故。预期大部分流量由缓存承接，`gate.admitted` 几乎不动。

压测在运行时配置这些限制（`/v1/admin/limits`、`/v1/admin/gate`），而不是准备三份配置。原因是一次同时撞上四个限制的压测，只会给你一个拒绝计数和零理解；隔离出单个机制，数字才有归因。

---

## 评测

```bash
make eval                                    # 200 条用例，分层采样
ailab-ops eval --limit 500 --out runs/r1.json
ailab-ops eval --difficulty hard             # 只跑 hard
```

报告围绕那些真正对应决策的区分来组织：

```
cases evaluated      150 / 150
exact accuracy       100.0%  (150 correct)
  by category        code:100%  external:100%  memory:100%  network:100% ...
  by difficulty      easy:100%  medium:100%  hard:100%

abstentions          25
  abstention precision 100.0%  (25/25 refusals were correct refusals)
  unknown-case recall  100.0%  (25/25 unknowable cases correctly refused)
false-confidence rate 0.0%   (0 wrong answers given at >= 70% confidence)
answer parse rate    100.0%  (150/150 parseable)
cost                 $0.2347 total, $0.001565 per diagnosis
latency              p50 4ms  p90 5ms  p99 7ms
```

数据生成和评分逻辑出自同一份剧本，所以指标在闭环意义上被评分，100% 这个数字不能直接外推到「真实模型在真实数据上会怎样」。想验证这一点，把 `AILAB_LLM_BACKEND` 指向一个真实服务再跑一遍 `make eval`。

即便如此，有两点让报告依然可信：

* **语料是故意带噪声的。** 约 28% 的失败 job 带着**属于其他故障**的告警行——一次短暂的 allreduce 重试、一次短暂的 DNS 抖动、一次预取队列触顶。它们会匹配到对手场景的模式，所以分类是真正的辨别，而不是一次子串匹配。让噪声决定不了结果的，是模式**权重**起了类似 IDF 的作用：通用告警权重 0.2，因果行权重 1.0。和 BM25 是同一个洞见，只是用在了手写的模式列表上。
* **校准指标不会被闭环掩盖。** 一个调成从不拒答的系统，与一个随意拒答的系统，在这里的得分不同，而且这个权衡在报告里是可见的。这是**度量方式**的性质，不是数据的性质。

报告里两项不太常见，但最有价值：

- **Abstention precision（拒答精确率）。** 全部拒答的系统 accuracy 6%、拒答精确率 100%；从不拒答的系统 94% 和 0%。两项都报，权衡就是可见的，而不是被藏进一个数字里。
- **False-confidence rate（假自信率）。** 一个以 0.97 置信度给出的错误答案，会让工程师付出真实的时间成本。accuracy 掩盖这一点，它不掩盖。

另外，`ailab-ops compare` 在真实失败日志上单独度量检索器，让你在开始调 prompt 之前，就能分清是检索问题还是推理问题。

---

## 项目结构

每个包的 `__init__.py` 只做 re-export 并声明 `__all__`，实现都在具名模块里。

```
src/ailab_ops/
  config.py        配置，全部走环境变量，带离线默认值
  signals.py       ★ 形态分类、级联规则、假设打分（880 行）
  runtime.py       唯一的对象图，被 server / CLI / eval / bench 共用
  cli.py           所有入口

  datagen/         平台数据生成
    faults.yaml    ★ 真相来源：29 个场景（932 行）
    taxonomy.py    加载与校验
    world.py       实体建模 + 遥测合成 + 序列化（1225 行）

  rag/             BM25F + 哈希向量检索、RRF 融合
    bm25.py           词法检索（带字段权重）
    embed.py          零依赖的哈希编码器
    store.py          文档存储、分块、排序融合
    kb.py             知识库构建（runbook + 平台文档）

  tools/           只读工具层
    registry.py      Tool / ToolRegistry / ToolResult
    builtin.py       7 个内置工具 + build_registry

  llm/             模型适配层
    base.py          后端协议与消息类型
    mock.py          离线推理器（确定性）
    openai_compat.py 任何 OpenAI 兼容端点
    stub_server.py   本地 HTTP stub，可注入故障
    registry.py      按配置选后端

  agent/           ★ 有界的工具调用循环
    loop.py          Agent / AgentStep / AgentResult（346 行）
    parsing.py       把模型输出解析成结构化诊断

  serving/         HTTP 与稳定性
    app.py           FastAPI + SSE
    service.py       请求流水线（身份 → 限流 → 缓存 → 降级 → 闸门 → agent）
    gate.py          上游准入控制
    limits.py        四种独立的限流与预算
    cache.py         语义缓存 + 熔断器 + 降级阶梯
    static/          单文件 Web UI

  obs/             可观测性
    tracing.py       Span / Trace / Tracer
    metrics.py       指标、成本核算、METRICS

  eval/            评测
    models.py        CaseResult / EvalReport
    cases.py         分层采样
    runner.py        串行执行与聚合
    reporting.py     落盘

  bench/           压测
    models.py        ClientResult / BenchReport
    mix.py           问题集构造
    scenarios.py     场景配置（限制画像 / 闸门容量）
    runner.py        施压与观测
    reporting.py     报告落盘

tests/             159 个测试：playbook、world、rag、signals、tools、agent、
                   serving、eval —— 外加一条端到端准确率下限
```

★ = 建议先读。

---

## 配置

每一项都有可用的默认值，`.env.example` 里全都写了。对行为影响最大的几个：

| 变量 | 默认值 | 作用 |
|---|---|---|
| `AILAB_LLM_BACKEND` | `mock` | `mock`（离线）或 `openai` |
| `AILAB_LLM_LATENCY_MS` | `0` | 每次调用模拟的思考耗时；**做有意义的压测要设成 ~400** |
| `AILAB_UPSTREAM_CONCURRENCY` | `8` | 在飞的模型调用数，这里最重要的数字 |
| `AILAB_QUEUE_MAXSIZE` | `64` | 超出负载中有多少排队而不是被拒 |
| `AILAB_TENANT_CONCURRENCY` | `16` | 每个组织的在飞上限 |
| `AILAB_TENANT_COST_PER_DAY_USD` | `5.0` | 每日预算，且这个拒绝不会因为重试而解除 |
| `AILAB_SEMANTIC_CACHE_THRESHOLD` | `0.86` | 近似重复问题的阈值 |
| `AILAB_AGENT_MAX_STEPS` | `8` | 循环的步数预算 |
| `AILAB_SEED` / `AILAB_N_JOBS` | `20260929` / `400` | 数据集规模与随机种子 |

---

## 已知局限

直接说清楚，比含糊过去有用：

- **数据规模偏小。** 内存里 400 个 job。并发数字度量的是架构行为，不是规模化吞吐。
- **数据生成与分类器出自同一份剧本。** 形态往返准确率接近 100%，这类指标会比接真实数据时乐观。这正是 `ailab-ops compare` 单独报检索、以及值得接一个真实模型后端的原因。
- **会话存储是进程内的。** 重启即丢，也不跨副本。这个局限是刻意的——它正是「生产答案是共享存储而不是粘性会话」的理由——但它仍是个局限。
- **缓存按 TTL 失效，而不是按证据变化失效。** 更细的 key 会更好，也更难做对。
- **哈希编码器不是真实 embedding 模型。** 它存在的意义是让混合检索不需要下载任何模型；换成真实模型只改一个类。
- **没有鉴权。** 身份来自请求体，代码在真实部署应该校验网关请求头的地方明确标注了这一点。

---

## 上手练习

项目按「每个组件都能独立理解、且各自的失效模式在测试里可见」来组织：

| 想理解什么 | 看哪里 | 怎么改能看到 |
|---|---|---|
| 检索融合不会自动胜过它的组件 | `rag/store.py` | 把 `lexical_weight` 设成 `0.5`，跑检索测试 |
| 置信度校准 vs 准确率 | `signals.py` | 挪 `MIN_MARGIN`，重跑 `make eval` |
| 级联规则 | `signals.py::extract_log_evidence` | 注释掉级联过滤，看准确率掉下去 |
| 准入控制 | `serving/gate.py` | 压测时调低 `AILAB_UPSTREAM_CONCURRENCY` |
| 优雅降级 | `serving/cache.py` | 按 UI 里的故障注入按钮 |
| 为什么输出上限该放在工具里 | `tools/__init__.py` | 调大 `MAX_LOG_LINES`，看上下文和成本增长 |
| 工具报错不能杀死整轮运行 | `agent/__init__.py` | 循环的错误处理，每条路径都有测试 |

最有用的一个练习：跑 `make eval`，读混淆对，修掉排第一的那个。混淆矩阵告诉你**具体是哪两个原因**在被弄混；在你不知道这一点之前，其他一切都是猜。

---

## 许可证

MIT。见 `LICENSE`。
