# 企业级故障响应 Agent · 项目阅读

面向有 Python 基础、尚未做过 LLM/Agent 项目的读者。教师带领学生阅读教材、
分析代码与运行演示；学生课后自行复习和模仿。正文按知识内容组织，课时不固定。
六章讨论从 LLM 到 Agent、调查控制、RAG 与证据、安全与审批、Agent 评测和企业服务。
最终产物为六份分课 PDF、一份合订本和独立教师带读提示。

当前排版管线已替换；旧 lesson 内容正在逐章迁移，临时构建不代表教材出版完成。

## 本地构建

构建仅依赖 Python 与 reportlab；字体来自仓库 `fonts/`，不访问网络。
若环境未安装排版依赖，可先运行 `pip install -e '.[course]'`。
测试和后续 PDF QA 使用 `pypdf`；开发依赖组也包含排版验证依赖。

```bash
python3 docs/course/build.py             # 六分册 + 合订本
python3 docs/course/build.py 3            # 单章
python3 docs/course/build.py 1 2          # 多章
python3 docs/course/build.py --combined   # 仅合订本
python3 docs/course/build.py --json       # 机器可读路径、标题、页数
PYTHONPATH=src python3 -m pytest tests/test_course_books.py -q
```

默认输出 `pdf/lesson-1.pdf` … `pdf/lesson-6.pdf` 和 `pdf/enterprise-incident-agent-v2.pdf`；
`--output DIR` 用于临时检查。重复构建比较页数、正文、目录等稳定属性，不要求
PDF 时间戳或文件字节相同。

## 作者接口

`lessons/l*.py` 各导出 `LESSON`；`book.py` 定义合订本身份，`deck.py` 负责排版。
文字按字面值处理，不能用 ReportLab XML 注入样式。

```python
from deck import Lesson, H1, H2, P, Code, Source, Diagram, TeacherNote

LESSON = Lesson(1, "从 LLM 到 Agent", "围绕一次 GPU assert 调查", blocks=[
    H1("本章解决的问题"), P("连续叙事正文。"),
    H2("项目代码"), Code("result = investigate(job_id)", caption="调用入口"),
    Source("src/ailab_ops/cli.py", "main"),
    Diagram(["请求", "调查", "报告"], [(0, 1, "创建"), (1, 2, "校验")], caption="调查流程"),
    TeacherNote("仅供教师备课；绝不写入学生 PDF。"),
])
```

可用块：`H1/H2`（自动进入目录和书签；H2 须在 H1 后；可选 `anchor` 为章内唯一稳定定位）、`P`、`Bullets/Numbered`、
`Code`、`Grid`、`Note(kind="note"/"warn"/"ok")`、`Term(name, text)`、
`Source(path, symbol)`、`ChapterRef(number, text)`、`Diagram`、`SequenceDiagram`、
`Figure(path, caption, width=1.0)` 和 `PageBreakBlock`。

`Grid.widths` 是相对列宽权重；省略时均分。表头跨页重复，代码按行跨页分割。
流程图按节点顺序纵向布局，非相邻边使用外围连线。图节点和时序消息用零起始索引。
图高超过一页或时序消息过长时明确报错，应拆成小图并在正文展开说明。
`Figure` 路径应由 lesson 基于文件位置解析为绝对路径，避免依赖调用者工作目录。

教师完整带读提示维护在独立 `TEACHING_NOTES.md`（出版验收任务产出）。
`TeacherNote` 用于编辑期隔离。`Spoken` 与 `Title` 仅为旧模块导入过渡保留：
前者过滤，后者由 Lesson 元数据生成的章节标题替代。
旧 `duration` 参数兼容旧模块，但不进入正文、页眉或页脚。

真实 API 与人工编写回放（authored replay）必须明确区分；数字注明命令、数据和版本来源。
历史 V1 实验不得当作当前 V2 能力。
