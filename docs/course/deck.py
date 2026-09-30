"""课件 PDF 的排版引擎。

设计目标只有一个：**讲师照着念就能讲**。所以正文用较大字号、行距宽、
段落短；代码块单独排版并自动缩进换行；每节课都带"照着念"的口播稿。

不依赖外部工具链（本机没有 pandoc / LaTeX / wkhtmltopdf），只用 reportlab，
并且把中文字体作为文件放进仓库，保证在任何机器上打开都不会变方框。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table as RTable,
    TableStyle,
)

FONT_DIR = Path(__file__).with_name("fonts")
SANS = "NotoSC"
MONO = "Mono"
MONO_BOLD = "MonoBold"

# 字号偏大，因为这是要念的稿子，不是要读的技术文档
BODY_SIZE = 12.5
CODE_SIZE = 10
LEAD = 8

INK = colors.HexColor("#1a1d23")
MUTED = colors.HexColor("#5b6472")
ACCENT = colors.HexColor("#2f6fdb")
RULE = colors.HexColor("#d7dbe2")
CODE_BG = colors.HexColor("#f6f7f9")
NOTE_BG = colors.HexColor("#eef4ff")
WARN_BG = colors.HexColor("#fff8e6")
OK_BG = colors.HexColor("#eefaf0")


def register_fonts() -> None:
    """把仓库内的字体注册进 reportlab。

    中文字体随仓库一起走，而不是用 reportlab 内置的 CID 字体：内置字体
    不内嵌、也没有 ToUnicode 映射，换一台机器打开就可能变成方框，
    而这份材料是要拿去讲课的。内嵌一个 17MB 的字体换取"在哪都能看"，
    这个交换是值得的。
    """
    if SANS in pdfmetrics.getRegisteredFontNames():
        return
    pdfmetrics.registerFont(TTFont(SANS, str(FONT_DIR / "NotoSansSC.ttf")))
    pdfmetrics.registerFont(TTFont(MONO, str(FONT_DIR / "JetBrainsMono.ttf")))
    pdfmetrics.registerFont(TTFont(MONO_BOLD, str(FONT_DIR / "JetBrainsMono-Bold.ttf")))
    # 用同一个中文字体冒充粗体，靠描边加粗。可变字体只有一个字重，
    # 与其再塞一个 17MB 的文件，不如让渲染器把笔画加粗一点点。
    from reportlab.pdfbase.pdfmetrics import registerFontFamily

    registerFontFamily(SANS, normal=SANS, bold=SANS, italic=SANS, boldItalic=SANS)


# --------------------------------------------------------------------------
# 块级元素：用一组小构件拼版面，而不是写一大坨 reportlab 调用
# --------------------------------------------------------------------------


@dataclass
class Block:
    pass


@dataclass
class Title(Block):
    text: str
    sub: str = ""


@dataclass
class H1(Block):
    text: str


@dataclass
class H2(Block):
    text: str


@dataclass
class P(Block):
    text: str


@dataclass
class Bullets(Block):
    items: list[str]


@dataclass
class Numbered(Block):
    items: list[str]


@dataclass
class Code(Block):
    text: str
    caption: str = ""


@dataclass
class Note(Block):
    text: str
    kind: str = "note"  # note | warn | ok


@dataclass
class Grid(Block):
    header: list[str]
    rows: list[list[str]]
    widths: list[float] | None = None


@dataclass
class Spoken(Block):
    """口播稿。渲染上与正文无异，单独成块是为了将来能换样式。"""
    text: str


@dataclass
class PageBreakBlock(Block):
    pass


@dataclass
class Lesson:
    number: int
    title: str
    subtitle: str
    duration: str
    blocks: list[Block] = field(default_factory=list)


# --------------------------------------------------------------------------
# 样式
# --------------------------------------------------------------------------

def _styles() -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle(
            "title", fontName=SANS, fontSize=26, leading=34, textColor=INK, spaceAfter=4
        ),
        "subtitle": ParagraphStyle(
            "subtitle", fontName=SANS, fontSize=13, leading=20, textColor=MUTED, spaceAfter=2
        ),
        "meta": ParagraphStyle(
            "meta", fontName=SANS, fontSize=10.5, leading=15, textColor=MUTED
        ),
        "h1": ParagraphStyle(
            "h1", fontName=SANS, fontSize=17, leading=24, textColor=ACCENT,
            spaceBefore=10, spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "h2", fontName=SANS, fontSize=14, leading=20, textColor=INK,
            spaceBefore=8, spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "body", fontName=SANS, fontSize=BODY_SIZE, leading=BODY_SIZE + LEAD,
            textColor=INK, alignment=TA_LEFT, spaceAfter=5,
        ),
        "bullet": ParagraphStyle(
            "bullet", fontName=SANS, fontSize=BODY_SIZE, leading=BODY_SIZE + LEAD,
            textColor=INK, leftIndent=14, bulletIndent=3, spaceAfter=3,
        ),
        "code": ParagraphStyle(
            "code", fontName=MONO, fontSize=CODE_SIZE, leading=CODE_SIZE + 4.5,
            textColor=INK,
        ),
        "caption": ParagraphStyle(
            "caption", fontName=SANS, fontSize=10, leading=14, textColor=MUTED, spaceAfter=3
        ),
        "note": ParagraphStyle(
            "note", fontName=SANS, fontSize=11.5, leading=17.5, textColor=INK
        ),
    }


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------
# 文档模板：页眉页脚 + 页码
# --------------------------------------------------------------------------


class Deck(BaseDocTemplate):
    def __init__(self, filename: str, lesson: Lesson, **kw):
        super().__init__(
            filename, pagesize=A4,
            leftMargin=20 * mm, rightMargin=20 * mm,
            topMargin=18 * mm, bottomMargin=18 * mm,
            title=f"第 {lesson.number} 课 · {lesson.title}",
            author="AILab Ops Copilot 课程",
            **kw,
        )
        self.lesson = lesson
        frame = Frame(
            self.leftMargin, self.bottomMargin,
            self.width, self.height, id="body",
        )
        self.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=self._decorate)])

    def _decorate(self, canvas, doc) -> None:
        canvas.saveState()
        w, h = A4
        # 页眉
        canvas.setFont(SANS, 9)
        canvas.setFillColor(MUTED)
        canvas.drawString(20 * mm, h - 12 * mm, f"AILab Ops Copilot 课程 · 第 {self.lesson.number} 课")
        canvas.drawRightString(w - 20 * mm, h - 12 * mm, self.lesson.title)
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(20 * mm, h - 14 * mm, w - 20 * mm, h - 14 * mm)
        # 页脚
        canvas.line(20 * mm, 14 * mm, w - 20 * mm, 14 * mm)
        canvas.setFont(SANS, 9)
        canvas.drawString(20 * mm, 10 * mm, f"{self.lesson.duration}")
        canvas.drawRightString(w - 20 * mm, 10 * mm, f"第 {doc.page} 页")
        canvas.restoreState()


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def _code_flowable(text: str, st: dict) -> Table:
    """代码块：每行一个 Paragraph，放进带底色的单元格，自动换行不截断。"""
    lines = text.rstrip("\n").split("\n")
    rows = [[Paragraph(_esc(ln).replace(" ", "&nbsp;") or "&nbsp;", st["code"])] for ln in lines]
    t = RTable(rows, colWidths=[170 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
        ("BOX", (0, 0), (-1, -1), 0.4, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


def _note_flowable(text: str, kind: str, st: dict) -> Table:
    bg = {"note": NOTE_BG, "warn": WARN_BG, "ok": OK_BG}.get(kind, NOTE_BG)
    bar = {"note": ACCENT, "warn": colors.HexColor("#c98a00"), "ok": colors.HexColor("#2f9e44")}[kind]
    t = RTable([[Paragraph(_esc(text), st["note"])]], colWidths=[170 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEBEFORE", (0, 0), (0, -1), 3, bar),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return t


def _table_flowable(header: Sequence[str], rows: Sequence[Sequence[str]], widths, st: dict):
    total = 170.0
    if widths is None:
        widths = [total / len(header)] * len(header)
    data = [[Paragraph(f"<b>{_esc(h)}</b>", st["body"]) for h in header]]
    for r in rows:
        data.append([Paragraph(_esc(str(c)), st["body"]) for c in r])
    t = RTable(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f5")),
        ("GRID", (0, 0), (-1, -1), 0.4, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def build(lesson: Lesson, out_path: str | Path) -> Path:
    register_fonts()
    st = _styles()
    story: list = []

    def add(flow, space=6):
        story.append(flow)
        if space:
            story.append(Spacer(1, space))

    for i, b in enumerate(lesson.blocks):
        if isinstance(b, Title):
            add(Paragraph(_esc(b.text), st["title"]), 2)
            if b.sub:
                add(Paragraph(_esc(b.sub), st["subtitle"]), 2)
            add(Paragraph(
                f"第 {lesson.number} 课 &nbsp;·&nbsp; {lesson.duration}", st["meta"]), 10)
            rule = RTable([[""]], colWidths=[170 * mm], rowHeights=[1.2])
            rule.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), ACCENT)]))
            add(rule, 14)
        elif isinstance(b, H1):
            add(Paragraph(_esc(b.text), st["h1"]), 4)
        elif isinstance(b, H2):
            add(Paragraph(_esc(b.text), st["h2"]), 3)
        elif isinstance(b, P):
            add(Paragraph(_esc(b.text), st["body"]))
        elif isinstance(b, Spoken):
            add(Paragraph(_esc(b.text), st["body"]))
        elif isinstance(b, Bullets):
            for it in b.items:
                story.append(Paragraph(_esc(it), st["bullet"], bulletText="•"))
            add(Spacer(1, 2))
        elif isinstance(b, Numbered):
            for n, it in enumerate(b.items, 1):
                story.append(Paragraph(_esc(it), st["bullet"], bulletText=f"{n}."))
            add(Spacer(1, 2))
        elif isinstance(b, Code):
            if b.caption:
                add(Paragraph(_esc(b.caption), st["caption"]), 1)
            add(_code_flowable(b.text, st), 10)
        elif isinstance(b, Note):
            add(_note_flowable(b.text, b.kind, st), 10)
        elif isinstance(b, Grid):
            add(_table_flowable(b.header, b.rows, b.widths, st), 10)
        elif isinstance(b, PageBreakBlock):
            story.append(PageBreak())

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    Deck(str(out), lesson).build(story)
    return out
