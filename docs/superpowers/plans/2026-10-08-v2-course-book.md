# V2 六课项目教材实施计划

> 执行方式：Subagent-driven development。每项任务由独立实现代理完成，再由独立审查代理核验；最终进行全书审查与 PDF 视觉验收。

**目标：** 将旧的逐字讲稿式课件替换为面向“有 Python 基础、未做过 LLM/Agent”的六章 A4 知识型项目教材，生成六份分课 PDF、一份合订本和独立教师带读提示。

**内容原则：** 课堂不现场写代码；正文可以独立阅读；每章围绕一个连续故障案例解释概念、架构、代码与运行证据。真实 OpenAI 兼容 API 是主路径，离线模式必须始终称为 authored replay / 人工编写回放，不冒充模型能力。

**技术方案：** 保留 reportlab 与仓库内嵌中文字体，重写 `docs/course/` 的书籍式排版模型。教材源文件保持 Python 数据结构，图表使用可维护的矢量绘制或仓库生成资产。构建脚本同时生成分册和合订本，并由结构测试、文本抽取、字体检查、页面渲染和人工截图抽查验证。

---

## Task 1：重建书籍式 PDF 排版与构建管线

**Files:**
- Replace: `docs/course/deck.py`
- Modify: `docs/course/build.py`
- Replace: `docs/course/README.md`
- Add: `docs/course/book.py`
- Add: `tests/test_course_books.py`

**要求：**
- 先写失败测试，覆盖六分册、合订本、书签/目录、页眉页脚、元数据、教师提示与学生正文隔离。
- 支持标题层级、连续正文、列表、表格、代码框、术语框、警告框、图注、源码定位、章节引用、矢量架构图/流程图/时序图和显式分页。
- 正文字号与留白适合连续阅读，不使用幻灯片式“一页一个观点”。
- 支持 `python3 docs/course/build.py`、单章构建和合订本构建；输出稳定写入 `docs/course/pdf/`。
- 中文字体嵌入，文本可复制/搜索；构建不依赖网络、pandoc、LaTeX 或浏览器。
- 旧 PDF 可由新构建覆盖；替代内容存在后删除逐字稿专用构件和过时说明。

## Task 2：第 1 课《从 LLM 到 Agent》

**Files:** `docs/course/lessons/l1.py`, `tests/test_course_content.py`

**要求：** 围绕 GPU assert 调查建立人工排障与 Agent 排障对照；讲消息、上下文、结构化输出、工具调用、最小循环和真实 API/回放边界。阅读从 CLI/API 到 orchestrator、模型网关、工具和报告的最短真实调用链。包含概念图、一次完整回放数据、代码定位、反例、总结和课后模仿方向。

## Task 3：第 2 课《让调查过程可控》

**Files:** `docs/course/lessons/l2.py`, `tests/test_course_content.py`

**要求：** 讲显式状态机、动态计划、预算、停止条件、错误回喂、幂等、重试、上下文约束与恢复；以重复工具调用、编造参数、无限循环、超时/限流为反例。所有状态名和预算字段必须与当前实现一致。

## Task 4：第 3 课《RAG、证据链与可信回答》

**Files:** `docs/course/lessons/l3.py`, `tests/test_course_content.py`, optional generated chart asset

**要求：** 讲观察后检索、Runbook 与评测标签隔离、词法/向量/混合、证据对象、截断与来源、主张—证据校验、证据不足拒答。保留“弱检索器拖累融合”的历史实验，但明确它属于 V1 基线及适用边界；V2 演示数字必须由当前案例/回放生成并标注 provenance。

## Task 5：第 4 课《安全边界与人工审批》

**Files:** `docs/course/lessons/l4.py`, `tests/test_course_content.py`

**要求：** 讲只读/动作工具、最小权限、策略强制、敏感字段与脱敏、提示注入边界、待审批动作、批准/拒绝/过期/模拟执行与审计。用 UI 和 API 的真实审批闭环说明“建议、批准、执行”三步分离。

## Task 6：第 5 课《如何评测一个 Agent》

**Files:** `docs/course/lessons/l5.py`, `tests/test_course_content.py`, optional generated chart asset

**要求：** 讲工具、参数、必需证据、引用、根因、拒答、策略、延迟和 token 的分层指标；解释独立标签、重复运行、波动和失败归因。使用当前 `eval --mode replay` 的可复跑结果并明确 replay 只验证确定性集成，不代表模型能力；对比旧闭环 100% 的局限。

## Task 7：第 6 课《从 Demo 到企业级服务》

**Files:** `docs/course/lessons/l6.py`, `tests/test_course_content.py`

**要求：** 讲 API/UI、并发闸门、租户边界、限流、超时、有限重试、`Retry-After`、缓存/降级边界、trace 和部署责任；用一次完整调查、一次上游失败、一次审批和一次评测回归收束全书，并给出学生课后复现路径。

## Task 8：教师提示、合订本与最终出版验收

**Files:**
- Add: `docs/course/TEACHING_NOTES.md`
- Add/Modify: `docs/course/book.py`, `docs/course/build.py`, `docs/course/README.md`
- Generate: `docs/course/pdf/lesson-1.pdf` … `lesson-6.pdf`, `docs/course/pdf/enterprise-incident-agent-v2.pdf`
- Add: `scripts/qa_course_pdfs.py`
- Modify: `tests/test_course_books.py`, `tests/test_course_content.py`

**要求：**
- 教师提示按章给出带读路线、可停顿提问、演示命令和容易讲错的边界，不混入学生 PDF 正文。
- 六章结构均包含：问题、案例、概念、项目位置、图、代码、反例、运行证据、总结、课后阅读/思考。
- 分册与合订本可重复构建；所有 PDF 页面尺寸为 A4，无空白页、溢出/裁切、孤立标题或不可读表格。
- 自动检查页数、字体嵌入、文本抽取、禁用旧口播/实操措辞、路径存在性和关键事实。
- 将所有 PDF 渲染为页面缩略图/联系表，逐章抽查封面、正文、表格、代码、图示和末页；记录工具版本、页数和发现。
- 运行全仓测试、教材构建两次并比较稳定属性；最终整体审查不得遗留 Critical/Important。

## 完成标准

- 六份知识型 A4 PDF 与一份合订本均存在并通过自动/视觉检查。
- 内容与当前 V2 实现一致，不再把 V1 确定性推理器或旧数字当成当前主路径。
- 每个关键数字有 provenance；回放、在线模型和模拟执行边界清晰。
- 学生只阅读 PDF 和仓库即可理解项目；教师可按独立提示带读，不需要课堂手写代码。
