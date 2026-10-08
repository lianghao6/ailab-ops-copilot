# 企业级故障响应 Agent · 项目阅读

面向有 Python 基础、尚未做过 LLM/Agent 项目的读者。教师带领学生阅读教材、
分析代码与运行演示；学生课后自行复习和模仿。正文按知识内容组织，课时不固定。
六章讨论从 LLM 到 Agent、调查控制、RAG 与证据、安全与审批、Agent 评测和企业服务。
最终产物为六份分课 PDF、一份合订本和独立教师带读提示。

六章正文与构建管线已替换。学生阅读六分册或合订本；教师备课使用独立的
[教师带读提示](TEACHING_NOTES.md)，按问题、案例、概念、源码与运行证据安排进度。

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
python3 scripts/qa_course_pdfs.py         # 七份 PDF 的自动出版检查
```

默认输出 `pdf/lesson-1.pdf` … `pdf/lesson-6.pdf` 和 `pdf/enterprise-incident-agent-v2.pdf`；
`--output DIR` 用于临时检查。重复构建比较页数、正文、目录等稳定属性，不要求
PDF 时间戳或文件字节相同。

出版 QA 检查 A4、页数、实际使用字体的嵌入与 ToUnicode、可抽取文本、空白页、
关键事实、源码路径/符号、过时口播措辞、文字边界和孤立末尾标题。
它不能证明自然语言主张正确，也不能替代图表和代码的视觉抽查。
安装可选的 `pypdfium2` 与 Pillow 后，可在临时目录渲染每页及每二十页的联系表：

```bash
python3 scripts/qa_course_pdfs.py --render /tmp/course-pdf-review --json
```

渲染工具不是构建依赖。报告写入指定临时目录的 `qa.json`，检查独立 PDFium 字符
边界；每章和合订本都保留整页图像与联系表，供封面、正文、图、表、代码与末页复核。

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
高度不超过 350pt 的短表整体移动到下一页；源码定位与前文/代码及紧随解释在
总高不超过 600pt 时保持同页，长内容仍正常分页，避免整节大块空白。
流程图按节点顺序纵向布局，非相邻边使用外围连线。图节点和时序消息用零起始索引。
相邻节点间距和时序消息间距根据标签高度分配；self-call 标签使用参与者本列内宽度。
图高超过一页或外围标签超出图高时明确报错，应缩短标签或拆成小图并在正文展开说明。
`Figure` 路径应由 lesson 基于文件位置解析为绝对路径，避免依赖调用者工作目录。

教师完整带读提示维护在独立 `TEACHING_NOTES.md`。
`TeacherNote` 用于编辑期隔离。`Spoken` 与 `Title` 仅为旧模块导入过渡保留：
前者过滤，后者由 Lesson 元数据生成的章节标题替代。
旧 `duration` 参数兼容旧模块，但不进入正文、页眉或页脚。

真实 API 与人工编写回放（authored replay）必须明确区分；数字注明命令、数据和版本来源。
历史 V1 实验不得当作当前 V2 能力。

## 字体来源与长读字重

仓库原 `fonts/NotoSansSC.ttf` 是可变字体，ReportLab 不应用其变化轴，默认嵌入
Thin。出版使用同一字体生成的静态 `NotoSansSC-Regular.ttf`（wght=400）。
使用 fontTools 4.66.1 的 `varLib.instancer --update-name-table` 完整实例化，
并保留 Unicode 映射；不把文字变成图片，也不依赖浏览器合成描边。
构建不需要 fontTools；只有重新生成字体资产时才需要：

```bash
python3 -m fontTools.varLib.instancer docs/course/fonts/NotoSansSC.ttf wght=400 \
  --update-name-table --output docs/course/fonts/NotoSansSC-Regular.ttf
```
