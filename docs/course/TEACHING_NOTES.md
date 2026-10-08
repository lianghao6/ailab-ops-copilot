# 教师带读提示

本文件独立于七份学生 PDF。教材正文可以连续阅读；这些提示帮助教师选择停顿、
展开代码和运行预先写好的演示，不要求学生课堂编码，不限定每课时长。
按主题完整性安排进度，可以省略部分源码细节，保留案例、责任关系和运行证据。
所有命令从仓库根目录执行。先按 README 安装依赖；课堂前运行一次演示，
确认所用版本。在线模式配置真实 OpenAI 兼容 API；固定数字演示使用明确标记的
人工编写回放。改变回放问题可能产生 `replay_miss`，可把它作为输入边界的说明。

## 第 1 课 · 从 LLM 到 Agent

带读路线：先读 1.2 时间线，请学生找到首个已观察错误，而不是读末尾 watchdog
直接猜原因。再读消息角色、上下文和工具说明，按“当前输入 → 模型请求 →
实际观察 → 状态变化”四列追踪图 1-1。最后沿 1.7 项目调用链打开
`cli.py`、`v2_runtime.py`、`investigation/orchestrator.py`，只展开 start、
advance、_tool_feedback 和 _report 的关键分支。用 1.8 五轮表收束整个循环。

停顿问题：第一轮能引用 rank 2 的具体断言吗？答案是不能，日志尚未回到上下文。
五轮为什么只有四次工具调用？计划、假设、报告各一轮，第二轮同时请求三个读取。
哪些观察允许“首个已观察故障是断言”，哪些证据还不足以“某条样本已被确认”？
回到 source_note、异步 CUDA 和未保留输入，不仅背根因字符串。

演示命令：

```bash
PYTHONPATH=src python3 -m ailab_ops.cli investigate --case case-gpu-assert --mode replay
AILAB_MODEL_MODE=replay PYTHONPATH=src python3 -m ailab_ops.cli serve --host 127.0.0.1 --port 8080
```

CLI 核对五轮、四证据和引用；服务启动后打开根页面，定位计划、证据和未知项。
两条命令分别演示，serve 持续运行，结束后正常 Ctrl+C。

易错边界：online 只表示模型 API 在线，当前工具读取教学案例。confidence=0.9
和 1000 token 都是回放预置值，不能转述成准确率或供应商账单。合法 JSON 与
合法引用仍不足以证明语义正确；completed 不是故障修复。

## 第 2 课 · 让调查过程可控

带读路线：从 2.2 同一观察、三种结局进入问题，先让学生预测预算停止的位置。
读 InvestigationState 与 InvestigationSession 的职责差别，再读六个 phase、
Budget.exhausted 和 advance。重点停在调用前、响应后、每个工具前后检查的位置，
解释最后一轮批量工具为什么能处理。读 duplicate_call 和 report_failures 两个
小分支，将“可修正错误”与“一次报告修正”分别放回总预算。最后比较页面恢复与执行恢复。

停顿问题：max_steps=2 是否意味着两次工具调用？不，step 是模型决策。
为什么只存 state 的 JSON 不能重启续跑？messages、证据、去重表、修正计数、版本与
身份关系尚未完整恢复。参数错误能否只靠提示词解决？应找到 schema 拒绝与错误回喂。

演示命令：

```bash
PYTHONPATH=src python3 -m ailab_ops.cli investigate --case case-gpu-assert --mode replay --max-steps 2
PYTHONPATH=src python3 -m ailab_ops.cli investigate --case case-gpu-assert --mode replay --max-tokens 200
PYTHONPATH=src python3 -m pytest tests/test_orchestrator.py tests/test_model_controls.py -q
```

前两条预期退出码为 2，属于已记录的停止结果，别在 shell 中把它们连成要求全成功的链。
分别核对三证据和零证据；第三条解释测试替身边界，控制层与证据层仍是真实代码。

易错边界：token 预算是返回后累计检查，不是供应商绝不超额保证；同步在途线程
不能任意杀掉。自然结束且缺少 finish_reason 是 protocol，读取传输异常是 transport，
超时是 timeout，不能把所有 SSE 失败都归成坏协议。保存 stopped 没有自动恢复 API。

## 第 3 课 · RAG、证据链与可信回答

带读路线：让学生先读本次错误，随后比较“为什么失败了”和具体错误串的查询。
用资产表区分 cases、knowledge、replays、evals；读当前词法检索器的集合交集和
标题加权，再读一般向量、融合与重排直觉。历史权重表讲清数据来源之后才解释
弱组件可能拖累排序。后半章以 Evidence、telemetry 和 Claim 连接知识、事实与结论，
用证据不足案例说明有帮助的拒答。

停顿问题：score=4 是否意味着四成准确率？不，是词项加权分数。
truncated=False 能否证明资料完整？不能，源头可能缺失早期 stderr。
有效日志 ID 能否支持“坏网线已确认”？程序可验证 ID，语义必须另行核查。

演示命令：

```bash
PYTHONPATH=src python3 -m ailab_ops.cli investigate --case case-insufficient-evidence --mode replay
PYTHONPATH=src python3 -m pytest tests/test_course_content.py -k chapter_three -q
```

第一条核对 completed、insufficient_evidence、unknowns；第二条复算实际检索、
两次回放及历史六档权重。历史实验在新子进程固定 PYTHONHASHSEED=0，课堂前缓存
结果便于带读，仍保留完整来源和命令。

易错边界：当前 V2 不是 BM25、训练式 embedding、混合检索或重排；它只有有界词法搜索。
V1 的 82.5%、58.0% 属于合成共享剧本，不能称为 V2 在线准确率。
Evidence 哈希不是签名认证，引用有效性不是语义蕴含。观察后检索是提示与回放示范，
没有强制在线必须 logs-first 的门。

## 第 4 课 · 安全边界与人工审批

带读路线：从“诊断完成后能做什么”进入 annotate_incident，再看 Tool.kind 和双层
action 阻挡。沿 PolicyContext、ApprovalRequest、ApprovalService 看快照与证据归属。
按图 4-2 分开提出、批准、执行三个请求，随后读拒绝、过期、重复执行。
最后用恶意日志字符串讨论提示注入，并标出模型上下文、trace、presentation 三个出口。

停顿问题：reason 写“已批准”能否改变 pending？不能，批准是独立服务调用。
相同 actor 批准并执行是否被禁止？当前允许，不能宣称强制双人审批。
注释引用真实证据是否就该批准？结构归属不能替代行动内容、影响和回滚判断。

演示命令：

```bash
AILAB_MODEL_MODE=replay AILAB_USER_QPS=0 PYTHONPATH=src python3 -m ailab_ops.cli serve --host 127.0.0.1 --port 8098
PYTHONPATH=src python3 -m pytest tests/test_course_content.py -k chapter_four -q
```

在服务页面选择 GPU assert，提出预备注释提案；先拒绝，另起提案再批准并单独执行模拟。
需要 API 细节时运行教材已写好的客户端。自动测试覆盖未批准 409、拒绝、测试时钟过期、
重复执行与报告保留；无需课堂等待十五分钟。

易错边界：没有真实写操作；simulated=true 不表示事故记录已更新。
tenant/user/actor 都是未认证模拟字段。只追加内存接口不是可靠持久审计。
展示脱敏不表示发往真实模型的日志已过滤。注入可能污染建议，审批门不能保证人永远判断正确。

## 第 5 课 · 如何评测一个 Agent

带读路线：先比较三个根因字符串相同而可用性不同的报告，再找指标分母与配置。
沿 cli → runner → factory → scoring → summarize 打开源码，让学生看到标签只在评分边界
相遇。对照九次回放结果，解释 null、显式 0、异常失败和各维度 count。
用副本上的错误根因、伪造引用、虚假主张表检验盲区，最后讲重复运行与失败归因。

停顿问题：tool_choice=1、required_evidence=0.5 为什么能同时出现？工具被请求不等于
所需证据已到场。错误具体根因的 abstention=1 是否错误？该维度只判是否应回答。
同案例十次重复与十个独立事故是否等价？不，来源与分布仍有差别。

演示命令：

```bash
PYTHONPATH=src python3 -m ailab_ops.cli eval --mode replay --repeats 3
PYTHONPATH=src python3 -m pytest tests/test_course_content.py -k chapter_five -q
```

核对三案例九次运行，四项质量分数为 1，四项未配置为 null；不要只展示均值，
同时打开 issues 与 by_case。在线评测用同一入口改为 --mode online，需预先配置 API；
没有配置时带读方法，不编造在线结果。

易错边界：回放满分只验证确定性集成；默认标签没配置 tool/evidence/resource 门槛。
当前没有参数语义、忠实度和成本独立分数。stddev 是总体标准差，置信度分段分桶不是
统计置信区间。CLI 退出码 0 不保证根因分数 1。旧闭环 100% 作为历史声明讨论，非本次复测。

## 第 6 课 · 从 Demo 到企业级服务

带读路线：先追踪 API/UI 与 runtime 同一调用链，然后把五步准入与整条调查并发画在不同
层次。读 QPS、令牌桶、burst 与 token 费用，明确各控制的计量单位。
用三次 HTTP 尝试和五次完整失败解释重试与熔断，读 Retry-After 和截止时间的优先关系。
最后看四组观测与生产责任表，将当前本地实现、已验证行为和部署待补责任一一对应。

停顿问题：QPS=2 是否只允许两条慢调查并行？不，它限制到达速率。
槽位 8、队列 64 能否保证 64 请求/秒？队列不创造计算能力。
四进程部署时 POST 到 A、GET 到 B 会怎样？状态不共享，可能 404；
全局容量与配额也不会自动变成一套。取消浏览器等待能否承诺不再计费？不能。

演示命令：

```bash
PYTHONPATH=src python3 -m pytest tests/test_course_content.py -k chapter_six -q
PYTHONPATH=src python3 -m ailab_ops.cli eval --mode replay
```

测试以真实网关加固定 HTTP 503 传输触发十五次尝试、五次熔断失败、第六条无上游；
再看容量 1 队列 1 的移交统计。界面收束一次调查与独立模拟审批，评测检查回归。

易错边界：POST 等待快照，没有前端 token SSE；上游 SSE 与网页轮询不同。
进程内状态、配额、熔断和审计尚不能支持透明多实例、重启恢复或真实动作 exactly-once。
答案缓存关闭，没有自动回放降级。受控 503 不衡量供应商可用性，快速回放不适合推导容量。

## 课前与课后使用

课前构建教材并运行 `python3 scripts/qa_course_pdfs.py`，记录版本；准备案例、API
配置和回放备用演示。在线模型可能选不同合法工具顺序，也可能需要修正报告，
应沿 trace 解释实际行为，不要求它复制回放。
课后推荐学生先看本章源码路线与现成命令，再在自己的临时分支模仿一个小的只读业务场景。
以能解释观察、未知项、控制边界为目标，六章不附强制课堂实验或统一时间表。
