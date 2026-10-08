# V2 调查工作台：阅读、复现与 QA 边界

默认服务根路径 `/` 提供中文调查工作台，样式和脚本来自同源 `/static/v2/`，无需前端构建链。页面按入口、计划、证据、假设、报告、时间线、行动与审批展开。所有动态内容按文本渲染，引用只链接本页已有证据锚点。

## 在线入口与回放复现

在线模式先在服务进程设置 `AILAB_MODEL_MODE=online`、`AILAB_LLM_BASE_URL`、`AILAB_LLM_MODEL` 和 `AILAB_LLM_API_KEY`，再启动：

```bash
PYTHONPATH=src python3 -m ailab_ops.cli serve --host 127.0.0.1 --port 8080
```

打开 `http://127.0.0.1:8080/`，确认顶栏显示在线模式，填写租户、案例标识和问题。密钥不进入浏览器；当前在线工具仍读取三份策划案例，未连接实际训练平台。

无密钥的教材复现显式采用回放：

```bash
AILAB_MODEL_MODE=replay PYTHONPATH=src python3 -m ailab_ops.cli serve --host 127.0.0.1 --port 8080
```

确认顶栏显示回放，选择 `case-gpu-assert`、`case-collective-timeout` 或 `case-insufficient-evidence`，问题留空。录制是人工编写的模拟轨迹；自定义未录制问题会显示 `replay_miss`，不是模型能力评测。模式由服务配置和 `/v2/health` 确认，页面不自行选择后端。

## 如何核查一份调查

证据面板保留 Evidence ID、来源工具、观察时间范围、原始片段、来源参数和元信息；来源完整性与片段截断分别展示。假设对照显示支持、反证和置信度，缺失引用明确显示尚无引用。报告包含根因、摘要、实质判断引用、已排除、未知和建议。引用跳回证据记录；“证据不足”也可以是已完成报告，不能把阶段完成理解为找到了确定根因。

时间线使用服务端事件，不按浏览器推断重建。含审批的会话会另外读取最大 500 条事件窗口，超出窗口时提示截断。浏览器不声称拿到了窗口以外的完整审计历史。

已完成且有报告时可提交行动提案。GPU 案例可使用工具 `annotate_incident`，参数 `{"case_id":"case-gpu-assert","note":"已核查引用报告"}`，引用从当前证据记录复制；理由、风险、回滚都要填写。工具和参数仍由服务端策略校验。参数默认展开，pending 只显示批准和拒绝；填写操作者并确认批准后，approved 才显示单独的执行确认。拒绝须填写原因。模拟执行结果和审计事件保留在面板中，所有行动都是模拟。

过期或无法核验期限时，按钮禁用并在卡片附近提示刷新。即使收到内容完全相同的快照，提示也会更新，确认框和拒绝原因输入节点保持原位。浏览器时钟只限制操作，不把服务器的 pending/approved 状态改写成 expired。

## 会话与错误

浏览器仅在 localStorage 保存租户和当前 Session ID。重新载入会读取服务端详情和审计，不恢复审批确认、执行确认或操作者。恢复入口也可粘贴当前租户的 Session ID。租户变更会中止旧请求并清空当前调查和行动草稿。

取消按钮中止浏览器等待，不能保证服务端停止。POST 传输异常或写入后读取失败时，保留最后确认快照并禁用继续写入，直到完整刷新详情和审计成功。调查、证据、审批和 trace 均在服务进程内存中；重启即丢，不跨副本。租户和 actor 是模拟身份字段，没有生产认证或持久化承诺。

## 键盘与响应式合同

页面只有一个 H1；静态输入有显式 label，动态确认框包在 label 内，拒绝原因有关联 label。跳过入口链接的调查目标可接收焦点。目录是原生锚点，明细使用原生 details/summary，没有需要自定义键盘协议的 ARIA tabs。状态区域采用 polite live region，加载时调查区域标记 aria-busy。操作按钮通过 aria-describedby 关联到期解释。

`:focus-visible` 提供 3px 轮廓；系统 reduced motion 会关闭平滑滚动、动画和过渡。桌面为目录与正文两列；1000px 收窄目录，720px 及以下转单列，阶段列表换行、证据卡转单列。正文列可收缩，普通动态文本继承 `overflow-wrap:anywhere`，pre 保留格式并允许换行；没有强制正文采用桌面固定宽度。

## 2026-10-08 确定性 QA 记录

本机有 Node 16.20.2；PATH 和 `/opt`、`/usr/bin`、用户 cache 扫描未发现 Chromium、Chrome、Firefox 或 Playwright 浏览器，Python 也没有 Playwright/Selenium。此次运行本地 replay 服务于 `127.0.0.1:8098`，通过真实 HTTP 检查根页面、CSS/JS、健康接口、三份报告、提案、拒绝、批准及模拟执行；随后关闭服务。localhost 请求显式绕过环境代理，否则 urllib 会收到代理的 503。

测试 harness 执行交付 app.js，使用真实 replay API 返回的快照和最小确定性 DOM、传输与时钟。以下是状态检查记录，不是浏览器截图：

| 页面状态 | 快照来源 | 检查结果 |
|---|---|---|
| intake | 空快照 | 无报告，无行动控件；静态入口有标签、状态区域和键盘目录 |
| investigating | 已有快照的阶段改为 investigating、report=null；受控 fixture | 当前阶段正确、无终态报告；加载 aria-busy 和轮询至终态通过 |
| completed report | GPU replay HTTP / TestClient | 4 份证据，含独立 Runbook；判断引用均可定位，原始片段按文本保留 |
| insufficient evidence | 证据不足 replay HTTP / TestClient | root_cause=insufficient_evidence，4 份证据、4 项未知；未知项显示在报告中 |
| pending approval | 真实提案响应后 GET | awaiting_approval；参数默认展开；确认和拒绝原因都有标签，没有执行按钮 |
| rejected | 真实 reject 后 GET | 保留原因和审计，不提供执行按钮；HTTP 阶段恢复 completed |
| simulated execution | 真实 approve、execute 后 GET | simulated=true；单独执行确认、结果及执行审计显示；没有再次执行按钮 |

Collective replay 的真实 HTTP 报告为 `collective_transport_failure`，同样有 4 份证据及 1 项未知。GPU 报告为 `gpu_device_assert`，有 1 项未知。

此次修复普通动态长文本通用换行、skip link 焦点目标，以及相同快照跨过期限后按钮缺少附近解释。回归覆盖 pending 和 approved 两种跨期状态，保留真实输入节点和确认，不伪造服务器过期状态。此外固定覆盖 reload 仅 GET 恢复与 POST 抛 TypeError 后必须刷新才能继续写入。

```bash
PYTHONPATH=src python3 -m pytest tests/test_v2_ui_accessibility.py tests/test_v2_ui_flows.py -q
PYTHONPATH=src python3 -m pytest -q
PYTHONPATH=src python3 -m ailab_ops.cli replay --case case-gpu-assert
```

专项合同与 DOM 交互：24 passed。全套：488 passed、1 skipped（`tests/test_tools.py:121`，legacy 样本中没有无日志作业：`no log-less job in this sample`）。CLI smoke 完成有引用报告，包含 `search_runbooks` 的独立证据。

## 尚未验证及教材截图要求

此次没有运行真实浏览器，未进行桌面或窄屏像素视觉验收，也未生成教材截图。静态断点/换行合同不证明实际布局零溢出；最小 DOM 不能证明原生焦点移动、Tab 顺序、屏幕阅读器播报、色彩对比度、字体加载、缩放或所有浏览器兼容性。此次也未调用在线模型。

具备浏览器的环境应在 1440×1000 与 390×844 检查上述七种状态，使用 Tab、Shift+Tab、Enter/Space 和跳过入口链接核查焦点，并在 200% 缩放、长不可分割标识和 reduced motion 下检查。同步 replay 接口很快返回终态，investigating/loading 截图须用受控延迟传输或测试 fixture，清楚标注。截图必须可见回放及模拟标记，不展示密钥；会话 ID、时间和延迟每次运行不同，不能把它们当作像素确定性基准。保存截图前仍需完成这组真实浏览器检查。
