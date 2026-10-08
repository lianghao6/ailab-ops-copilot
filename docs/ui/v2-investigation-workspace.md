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

时间线使用服务端事件，不按浏览器推断重建。每条调用显示工具、模型和可跳转的关联证据；原生明细保留 usage、latency_ms、retry、error 以及服务端已脱敏的公开事件数据，按安全文本展示，不展示私密推理。含审批的会话会另外读取最大 500 条事件窗口，超出窗口时提示截断。浏览器不声称拿到了窗口以外的完整审计历史。

已完成且有报告时可提交行动提案。GPU 案例可使用工具 `annotate_incident`，参数 `{"case_id":"case-gpu-assert","note":"已核查引用报告"}`，引用从当前证据记录复制；理由、风险、回滚都要填写。工具和参数仍由服务端策略校验。参数默认展开，pending 只显示批准和拒绝；填写操作者并确认批准后，approved 才显示单独的执行确认。拒绝须填写原因。模拟执行结果和审计事件保留在面板中，所有行动都是模拟。

过期或无法核验期限时，按钮禁用并在卡片附近提示刷新。即使收到内容完全相同的快照，提示也会更新，确认框和拒绝原因输入节点保持原位。浏览器时钟只限制操作，不把服务器的 pending/approved 状态改写成 expired。

## 会话与错误

浏览器仅在 localStorage 保存租户和当前 Session ID。重新载入会读取服务端详情和审计，不恢复审批确认、执行确认或操作者。恢复入口也可粘贴当前租户的 Session ID。租户变更会中止旧请求并清空当前调查和行动草稿。

非终态每 2.5 秒发起的自动轮询是后台读取，不进入前台 busy、不禁用现有审批或抢走焦点。未变化的计划、证据、假设与报告保留节点，新增证据与时间线/审计事件保留既有未变行和 details 展开状态；相同状态消息不重复改写 live region。读请求在下一次前台恢复、创建或审批时中止，迟到响应由 generation 校验丢弃。真正变更的提案仍要求重新确认，不能为保留焦点而继续展示已失效的操作；服务端移除的记录或截断窗口外事件也不会凭空保留。

取消按钮中止浏览器等待，不能保证服务端停止。POST 传输异常或写入后读取失败时，保留最后确认快照并禁用继续写入，直到完整刷新详情和审计成功。调查、证据、审批和 trace 均在服务进程内存中；重启即丢，不跨副本。租户和 actor 是模拟身份字段，没有生产认证或持久化承诺。

## 键盘与响应式合同

页面只有一个 H1；静态输入有显式 label，动态确认框包在 label 内，拒绝原因有关联 label。跳过入口链接的调查目标可接收焦点。目录是原生锚点，明细使用原生 details/summary，没有需要自定义键盘协议的 ARIA tabs。状态区域采用 polite live region，加载时调查区域标记 aria-busy。操作按钮通过 aria-describedby 关联到期解释。

`:focus-visible` 提供 3px 轮廓；系统 reduced motion 会关闭平滑滚动、动画和过渡。桌面为目录与正文两列；1000px 收窄目录，720px 及以下转单列，阶段列表换行、证据卡转单列。正文列可收缩，普通动态文本继承 `overflow-wrap:anywhere`，pre 保留格式并允许换行；没有强制正文采用桌面固定宽度。

## 2026-10-08 确定性 QA 记录

初次检查时，本机有 Node 16.20.2；PATH 和 `/opt`、`/usr/bin`、用户 cache 扫描未发现 Chromium、Chrome、Firefox 或 Playwright 浏览器，Python 也没有 Playwright/Selenium。初轮运行本地 replay 服务于 `127.0.0.1:8098`，通过真实 HTTP 检查根页面、CSS/JS、健康接口、三份报告、提案、拒绝、批准及模拟执行；随后关闭服务。localhost 请求显式绕过环境代理，否则 urllib 会收到代理的 503。后续 fix round1 成功安装临时 headless Chromium 并补做真实浏览器 QA，记录如下。

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

## 真实浏览器 QA：fix round1

主机 glibc 为 2.17。通过 CodeLab 内网 npm 源，在 `/tmp/v2-ui-browser-qa.8ecfAB` 安装 `chrome-aws-lambda@10.1.0` 与 `puppeteer-core@10.1.0`，得到可运行的 `HeadlessChrome/92.0.4512.0`。依赖、浏览器、字体缓存和截图仅在临时目录，不加入项目运行依赖或版本库。中文字体通过临时 Fontconfig 配置使用已有课程字体 `/home/hadoop-aipnlp/ailab-ops-copilot/docs/course/fonts/NotoSansSC.ttf`，截图中文正常显示。

启动本地 replay 服务后，使用 [真实浏览器 QA 脚本](../../scripts/qa_v2_workspace.cjs) 操作交付页面：每个视口都清空当前浏览器上下文，再以原生键盘事件提交调查、提案、拒绝、批准和模拟执行。最终运行 exit 0，在 **1440×900 与 390×844** 各捕获七种状态的全页面截图和视口截图，另捕获报告与行动面板细节；已查看两组七状态截图。所有状态的 `document.documentElement.scrollWidth` 分别为 1440 和 390，无页面横向溢出，main 内可见元素未越过右侧边界，console error 和 pageerror 均为 0。

| 状态 | 真实浏览器视觉与控件检查 | 截图名称（两个宽度均有） |
|---|---|---|
| intake | 桌面两列、窄屏单列，入口标签与中文无缺字；修复后的左右留白正常 | `{width}-intake.png` |
| investigating/loading | 真实 POST 暂挂时“正在调查”提示、disabled 开始按钮、取消等待及 aria-busy；未伪造服务端 phase | `{width}-investigating.png` |
| completed | GPU 根因、摘要、引用、未知项布局完整；长 Evidence ID 在窄屏换行 | `{width}-completed.png`、`{width}-completed-report.png` |
| insufficient evidence | 根因为 insufficient_evidence，4 项未知可读，窄屏报告与引用正常换行 | `{width}-insufficient.png`、`{width}-insufficient-report.png` |
| pending approval | 参数默认展开；确认框、拒绝原因和审批按钮完整；没有执行按钮 | `{width}-pending.png`、`{width}-pending-action.png` |
| rejected | 拒绝原因和审计可读；没有执行按钮 | `{width}-rejected.png`、`{width}-rejected-action.png` |
| simulated executed | 批准后需单独勾选执行；已展开结果显示 simulated=true；无再次执行按钮，审计有 executed | `{width}-executed.png`、`{width}-executed-action.png` |

`{width}` 为 `1440` 或 `390`。上述截图及结构化结果位于 `/tmp/v2-ui-browser-qa.8ecfAB/`（`findings.json`），视口截图加 `-viewport` 后缀；它们是临时 QA 产物，可能随环境清理失效，未提交。截图保留回放和模拟标记，部分面板细节截图只用于查看排版，不作为独立教材模式声明图。

浏览器验证 Tab 后首先到 skip link，focus-visible 轮廓为 solid；Enter 跳到 investigation 焦点目标；Space 可开关恢复明细。键盘 Enter 提交、拒绝、批准与执行，Space 切换两个独立确认框均成功；pending 没有执行按钮，批准不自动执行，执行完成后没有再次执行按钮。媒体仿真 reduced motion 下计算后的 scroll-behavior 为 auto。额外插入 80 次连续的不可分割长标识至问题、计划、摘要、未知项和证据片段，两个视口的页面仍不横向溢出；此压力输入仅是浏览器 QA fixture。

视觉发现与修复：原 `min-width:1400px` 规则将入口栏 `padding-left` 清零，1440px 时标题和表单贴到页面左边。新增静态合同及真实浏览器 `h1.left>=24` 断言先分别失败；删除该覆盖规则，保留基础内边距后两项通过，重新捕获两组七状态。此次没有其他视觉问题需要修改。

同步 replay 的 investigating 截图采用请求拦截暂挂真实 POST，捕获浏览器的 loading 状态；当时服务端尚未返回阶段快照，阶段轨道仍为空。它不代表轮询捕获到了服务端正在 investigating 的存档状态。

可在兼容环境重跑，服务与 QA 命令分别在两个终端执行：

```bash
AILAB_MODEL_MODE=replay AILAB_USER_QPS=0 PYTHONPATH=src python3 -m ailab_ops.cli serve --host 127.0.0.1 --port 8098
```

```bash
qa_dir=$(mktemp -d /tmp/v2-ui-browser-qa.XXXXXX)
npm install --prefix "$qa_dir" chrome-aws-lambda@10.1.0 puppeteer-core@10.1.0 \
  --cache "$qa_dir/npm-cache" --no-audit --no-fund
qa_browser=$(AWS_LAMBDA_FUNCTION_NAME=v2-ui-qa node -e \
  'require(process.argv[1]+"/node_modules/chrome-aws-lambda").executablePath.then(console.log)' "$qa_dir")
NODE_PATH="$qa_dir/node_modules" node scripts/qa_v2_workspace.cjs \
  http://127.0.0.1:8098 "$qa_browser" "$qa_dir"
```

上面的通用命令使用 npm 当前配置的源，不要求内部网络。仅在需要内网镜像的 CodeLab 环境，可在安装命令前额外设置 `npm_config_registry=http://r.npm.sankuai.com`；这不是项目或默认 QA 配置。所固定的旧 Chromium 适用于本机 glibc 2.17 的兼容性验证，不是推荐的生产浏览器版本；其他环境可自行提供兼容的 Puppeteer/浏览器路径。

需要中文字体的环境先检查 `fc-list :lang=zh`；本机运行额外传入临时 `FONTCONFIG_FILE` 与 `FONTCONFIG_PATH` 指向含课程字体的配置，配置和缓存均在 QA 目录。脚本只接受 localhost 服务，创建的调查与模拟审批在该测试服务内存中。应用配置与项目依赖未变化。

最终专项 25 passed；全套 489 passed、1 个上述 legacy skip。真实浏览器脚本 exit 0，JS 语法和 diff whitespace 检查通过。

## Tab/Shift+Tab 与 200% 等价重排：fix round2

沿用 `HeadlessChrome/92.0.4512.0` 和相同临时依赖，扩展 QA 脚本至桌面/窄屏的 100% 与 200% 等价重排。200% 采用 Puppeteer 的 CDP device metrics：把 **CSS layout viewport 的宽、高各缩为原来一半**，再设置 `deviceScaleFactor=2` 保持截图输出的物理像素尺寸。桌面由 1440×900 改为 720×450 CSS px，窄屏由 390×844 改为 195×422 CSS px；读取的 `innerWidth` 确实变为 720 和 195。这是等价的页面放大/reflow 验证，不是操作浏览器菜单缩放；单独改变 deviceScaleFactor 而不减小 CSS 视口不会产生这项重排。

代表状态 intake、completed 和 pending 在每种组合执行完整键盘循环：仅在开头设置 skip link 焦点，之后逐次真实发送 Tab，遍历当前可用控件和链接直至回到 skip link；再用 Shift keyDown + Tab + Shift keyUp 逐次反向遍历。每步检查 activeElement、可见/未禁用及 focus-visible 计算轮廓 solid，反向序列与正向序列逆序严格一致；未发现焦点陷阱。统计不含浏览器边界 body 焦点，包含 skip link；展开明细中的链接和控件按当时可见状态参与循环。

| 物理输出视口 / 等价缩放 | CSS 视口 | intake 焦点数 | completed 焦点数 | pending 焦点数 |
|---|---|---:|---:|---:|
| 1440×900 / 100% | 1440×900 | 12 | 69 | 74 |
| 1440×900 / 200% | 720×450 | 7 | 64 | 69 |
| 390×844 / 100% | 390×844 | 7 | 64 | 69 |
| 390×844 / 200% | 195×422 | 7 | 64 | 69 |

主要顺序已核对：skip link → wordmark → tenant-id → case-select → question → start-button → 恢复明细 summary。桌面 100% 另有 5 个可见目录链接；重排为窄屏后它们隐藏，焦点数相应减少。completed 的行动表单按操作者、工具、参数、引用、理由、风险、回滚、提交排列。pending 审批卡按批准确认框、批准按钮、拒绝原因、拒绝按钮排列，之后可到操作者输入与审计明细。Shift+Tab 可从末尾逐项返回入口。200% 下同样以 Enter/Space 实际提出、拒绝、批准、单独执行提案，控件没有被隐藏或挤出页面。

四种组合均重新捕获全部七状态，共 28 次主状态检查；每次 console/pageerror=0，页面 scrollWidth=innerWidth，body 内可见元素的右侧越界列表为空。200% 桌面七状态为 720/720，200% 窄屏七状态为 195/195。图片和结果在 `/tmp/v2-ui-browser-qa.8ecfAB/round2/`，缩放截图名为 `1440-200pct-{state}.png` 与 `390-200pct-{state}.png`，其余后缀与 round1 相同；`findings.json` 包含 12 组正反序列。已查看修复后的 200% 窄屏 intake、报告和 pending 行动面板截图。临时文件未提交。

发现并修复：195 CSS px 时页头的版本标记沿不换行的 flex 行溢出，页面 scrollWidth=256。实际浏览器宽度断言先失败，新增窄屏页头静态合同也先失败。补 `max-width:360px` 页头 flex-wrap 和品牌 white-space:normal 后，版本标记正常换到下一行，完整四组合复测通过。该规则也改善正常极窄视口；390px 正常截图保持原布局。

本轮可重跑命令（先按 round1 启动本地 replay 服务）：

```bash
NODE_PATH=/tmp/v2-ui-browser-qa.8ecfAB/node_modules \
FONTCONFIG_FILE=/tmp/v2-ui-browser-qa.8ecfAB/fonts.conf \
FONTCONFIG_PATH=/tmp/v2-ui-browser-qa.8ecfAB \
node scripts/qa_v2_workspace.cjs http://127.0.0.1:8098 /tmp/chromium /tmp/v2-ui-browser-qa.8ecfAB/round2
```

真实浏览器脚本 exit 0；专项 26 passed；全套 490 passed、1 个原有 legacy skip。JS 语法与 diff whitespace 检查通过。

## 整体审查修复：后台轮询与调用信息

四项新回归先分别失败于阅读节点被替换、后台审批禁用、前台决策被后台 GET 阻挡和工具/模型调用字段缺失；补充的相同 live 状态写入与新增证据回归也先失败，随后通过。确定性 harness 直接执行交付脚本，触发真实注册的轮询回调并暂挂 GET，覆盖同快照、新事件/证据追加，以及前台审批中止旧读后迟到响应不能覆盖 approved。服务端 TestClient 产生含 `<tool-call>`、`<model-call>`、usage、latency_ms、retry、error 和关联 Evidence ID 的真实 TraceEvent；UI 文本与引用合同通过，unsafe HTML sink 会抛错。api_key 与 reasoning_content 在 presentation API 脱敏后不出现在 UI 文本；客户端仍以服务端脱敏响应为隐私边界，不直接访问原始 trace 或推理。

沿用 `HeadlessChrome/92.0.4512.0`，以实际 2500ms 定时器跨越轮询周期，没有改短定时器或直接调用 refresh。四种桌面/窄屏、100%/200% 等价重排组合中，各在时间线 SUMMARY 与批准按钮上保持焦点，暂挂实际 GET 后分别检查读取中与完成后：activeElement 相同、审批可用、busy=false、证据/报告/时间线/审计阅读节点相同，所有打开的相关 details 均保持展开。共 8 次检查通过；每组真实 replay 时间线有 12 条带工具、10 条带模型的事件，其字段和关联证据链接均通过浏览器断言。

本轮再次执行 28 次七状态检查、12 组 Tab/Shift+Tab 完整循环和原生键盘拒绝/批准/单独模拟执行，全部 exit 0；console/pageerror=0，scrollWidth=innerWidth（1440、720、390、195），无横向页面溢出。由于新增调用明细和时间线证据链接，completed/pending 焦点数较 round2 增多，正反顺序仍严格一致；结构化结果保留实际序列。截图和 `findings.json` 位于 `/tmp/v2-ui-browser-qa.8ecfAB/overall-fix-final/`，未提交。

命令（服务启动方式同上）：

```bash
PYTHONPATH=src python3 -m pytest tests/test_v2_ui.py tests/test_v2_ui_flows.py tests/test_v2_ui_accessibility.py tests/test_v2_presentation_api.py -q
PYTHONPATH=src python3 -m pytest -q
PYTHONPATH=src python3 -m ailab_ops.cli replay --case case-gpu-assert
NODE_PATH=/tmp/v2-ui-browser-qa.8ecfAB/node_modules \
FONTCONFIG_FILE=/tmp/v2-ui-browser-qa.8ecfAB/fonts.conf \
FONTCONFIG_PATH=/tmp/v2-ui-browser-qa.8ecfAB \
node scripts/qa_v2_workspace.cjs http://127.0.0.1:8098 /tmp/chromium /tmp/v2-ui-browser-qa.8ecfAB/overall-fix-final
```

专项 59 passed；全套 494 passed、1 个原有 legacy skip。CLI smoke 完成有引用的 gpu_device_assert 报告，保留 search_runbooks 独立证据。JS 语法与 diff whitespace 检查通过。QA 安装命令已取消强制内部 registry，内部镜像仅为可选配置；没有新增 runtime 依赖、browser/cache 或截图提交。

## 剩余验证边界与教材截图

真实浏览器验证范围限于上述 Chromium 92、两种输出视口（含 200% 等价重排）和本地 replay；尚未验证其他浏览器、屏幕阅读器播报、对比度测量或在线模型。Tab/Shift+Tab 完整循环覆盖 intake、completed、pending 的当前展开状态，不声称覆盖每个可能的明细展开组合；缩放检查未操作浏览器菜单。此次截图是 QA 证据，教材裁图与最终版式仍应在排版阶段确认。会话 ID、时间和延迟每次运行不同，不能把它们当作像素确定性基准；教材使用 investigating/loading 图时应标明受控等待。
