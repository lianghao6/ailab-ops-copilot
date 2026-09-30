# AILab Ops Copilot · 六节课课件

面向「大模型应用 / Agent 工程师」方向的六节课，配套仓库里的项目。
每节课一份 PDF，**按讲解稿写成——上课照着念即可**。

## 课表

| 课次 | 主题 | 时长 | PDF |
|---|---|---|---|
| 1 | 行业地图与问题定位 | 120 分钟 | [lesson-1.pdf](pdf/lesson-1.pdf) |
| 2 | Agent 原理与上下文工程 | 120 分钟 | [lesson-2.pdf](pdf/lesson-2.pdf) |
| 3 | 检索与知识底座 | 120 分钟 | [lesson-3.pdf](pdf/lesson-3.pdf) |
| 4 | 高并发与稳定性 | 120 分钟 | [lesson-4.pdf](pdf/lesson-4.pdf) |
| 5 | 评测与可观测 | 120 分钟 | [lesson-5.pdf](pdf/lesson-5.pdf) |
| 6 | 答辩与模拟面试 | 120 分钟 | [lesson-6.pdf](pdf/lesson-6.pdf) |

## 每节课的结构

固定的五段式，学员形成预期：

1. **这节课的目标** —— 三件事，讲清楚今天要拿到什么
2. **口语化讲稿** —— 主体。分小节，`口语化讲稿` 段落就是可以直接念的话
3. **动手** —— 三到四个实操，都有可复制的命令
4. **小面试（10 分钟）** —— 三到四道高频面试题 + 答题框架
5. **作业** —— 三道题，前两道动手，第三道是思考题

## 设计取舍

**为什么不用 Markdown + pandoc？**
本机没有 pandoc / LaTeX / wkhtmltopdf，而且都在受限网络下。用 reportlab
直接排版，只依赖一个库，且中文可控。

**为什么字体文件进仓库（17MB）？**
reportlab 内置的 CJK CID 字体**不内嵌、也没有 ToUnicode 映射**，
换一台机器打开就可能全变方框——而这份材料是要拿去讲课的。
内嵌字体换来「在任何机器上都能正常显示」，这个交换值得。

**为什么字号偏大、行距偏宽？**
因为这是要念的稿子，不是要读的技术文档。念的时候眼睛需要能快速定位。

## 重新生成

```bash
pip install reportlab
python3 docs/course/build.py        # 全部
python3 docs/course/build.py 3      # 只生成第 3 课
```

内容定义在 `lessons/l*.py`，排版引擎在 `deck.py`。
改内容不需要动排版代码。

## 课件里的数字都是实测的

第 3 课的检索对比（纯词法 82.5% / 纯向量 29% / 等权融合 57%）、
第 5 课的评测指标和「禁用 7 条守卫」实验，都是在本仓库上实际跑出来的，
可以复现：

```bash
PYTHONPATH=src python3 -m ailab_ops.cli compare     # 第 3 课的数字
PYTHONPATH=src python3 -m ailab_ops.cli eval --limit 200   # 第 5 课的数字
```
