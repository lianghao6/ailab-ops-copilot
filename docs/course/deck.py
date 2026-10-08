"""A4 project books with local embedded fonts and literal author text.

Historical module name retained for lesson imports. Teacher blocks never enter
student PDFs. Continuous paragraphs replace the former presenter layout.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import BaseDocTemplate, Flowable, Frame, Image, PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.platypus.tableofcontents import TableOfContents

FONT_DIR = Path(__file__).with_name("fonts")
SANS, MONO = "NotoSC", "Mono"
INK, MUTED, ACCENT, RULE = [colors.HexColor(c) for c in ("#202b36", "#586879", "#205777", "#cbd5df")]
WIDTH = A4[0] - 40 * mm
BODY_SIZE = 10.5


def register_fonts():
    for name, filename in ((SANS, "NotoSansSC.ttf"), (MONO, "JetBrainsMono.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / filename)))
        pdfmetrics.registerFontFamily(name, normal=name, bold=name, italic=name, boldItalic=name)


@dataclass
class Block:
    """Unknown blocks fail loudly at publication time."""


@dataclass
class Title(Block):
    text: str
    sub: str = ""


@dataclass
class H1(Block):
    text: str
    anchor: str = ""


@dataclass
class H2(Block):
    text: str
    anchor: str = ""


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
    kind: str = "note"


@dataclass
class Term(Block):
    name: str
    text: str


@dataclass
class Grid(Block):
    header: list[str]
    rows: list[list[str]]
    widths: list[float] | None = None


@dataclass
class TeacherNote(Block):
    text: str


# Temporary legacy bridge: presenter prose is excluded from student books.
Spoken = TeacherNote


@dataclass
class Source(Block):
    path: str
    symbol: str = ""


@dataclass
class ChapterRef(Block):
    number: int
    text: str = ""


@dataclass
class PageBreakBlock(Block):
    pass


@dataclass
class Diagram(Block):
    """Vertical flow/architecture; edges contain zero-based node indexes."""
    nodes: list[str]
    edges: list[tuple[int, int, str]]
    caption: str = ""


@dataclass
class SequenceDiagram(Block):
    participants: list[str]
    messages: list[tuple[int, int, str]]
    caption: str = ""


@dataclass
class Figure(Block):
    path: str | Path
    caption: str
    width: float = 1.0  # fraction of text width


@dataclass
class Lesson:
    number: int
    title: str
    subtitle: str = ""
    duration: str = ""  # compatibility only; never printed
    blocks: list[Block] = field(default_factory=list)


def _styles():
    def style(name, size, leading, **kwargs):
        return ParagraphStyle(name, fontName=SANS, fontSize=size, leading=leading,
                              textColor=INK, wordWrap="CJK", **kwargs)
    return {
        "title": style("title", 25, 36, spaceAfter=18),
        "subtitle": style("subtitle", 12, 20, spaceAfter=8),
        "h1": style("h1", 15, 23, spaceBefore=15, spaceAfter=8, keepWithNext=True),
        "h2": style("h2", 12, 19, spaceBefore=10, spaceAfter=5, keepWithNext=True),
        "body": style("body", BODY_SIZE, 17.5, spaceAfter=8),
        "bullet": style("bullet", BODY_SIZE, 17, leftIndent=15, bulletIndent=2, spaceAfter=4),
        "table": style("table", 9, 14),
        "caption": style("caption", 9, 14, spaceAfter=6),
        "code": ParagraphStyle("code", fontName=MONO, fontSize=8, leading=12, textColor=INK, splitLongWords=True),
        "note": style("note", 10, 16),
        "toc0": style("toc0", 12, 19, spaceBefore=9),
        "toc1": style("toc1", 10, 16, leftIndent=14, spaceBefore=3),
        "toc2": style("toc2", 9, 14, leftIndent=28),
    }


def _paragraph(text, style):
    return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)


def _heading(text, level, key, st):
    p = _paragraph(text, st["title" if level == 0 else f"h{level}"])
    p.book_heading = (level, str(text), key)
    return p


class BookDocument(BaseDocTemplate):
    def __init__(self, filename, title):
        super().__init__(str(filename), pagesize=A4, leftMargin=20 * mm,
                         rightMargin=20 * mm, topMargin=23 * mm, bottomMargin=21 * mm,
                         title=title, author="Enterprise Incident Agent V2 项目教材",
                         subject="Python 读者的 LLM、Agent 与企业故障调查项目教材")
        self.addPageTemplates(PageTemplate(id="book", frames=[Frame(
            self.leftMargin, self.bottomMargin, self.width, self.height,
            leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        )], onPage=self._decorate))

    def _decorate(self, canvas, doc):
        canvas.saveState()
        canvas.setFont(SANS, 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(self.leftMargin, A4[1] - 14 * mm, "Enterprise Incident Agent V2")
        canvas.drawRightString(A4[0] - self.rightMargin, A4[1] - 14 * mm, "项目阅读材料")
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(.4)
        canvas.line(self.leftMargin, A4[1] - 17 * mm, A4[0] - self.rightMargin, A4[1] - 17 * mm)
        canvas.line(self.leftMargin, 16 * mm, A4[0] - self.rightMargin, 16 * mm)
        canvas.drawString(self.leftMargin, 11 * mm, "知识 · 代码 · 运行证据")
        canvas.drawRightString(A4[0] - self.rightMargin, 11 * mm, f"第 {doc.page} 页")
        canvas.restoreState()

    def afterFlowable(self, flowable):
        if hasattr(flowable, "book_heading"):
            level, text, key = flowable.book_heading
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text, key, level=level, closed=False)
            self.notify("TOCEntry", (level, escape(text), self.page, key))


def _table(header, rows, widths, st):
    count = len(header)
    if not count or any(len(row) != count for row in rows):
        raise ValueError("tables need nonempty headers and equal-width rows")
    ratios = widths or [1] * count
    if len(ratios) != count or any(w <= 0 for w in ratios):
        raise ValueError("table weights must be positive and match headers")
    data = [[_paragraph(c, st["table"]) for c in header]]
    data.extend([[_paragraph(c, st["table"]) for c in row] for row in rows])
    result = Table(data, colWidths=[WIDTH * w / sum(ratios) for w in ratios], repeatRows=1, splitInRow=1)
    result.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf0f5")),
        ("GRID", (0, 0), (-1, -1), .4, RULE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return result


def _code(text, st):
    rows = []
    for line in text.rstrip("\n").expandtabs(4).split("\n"):
        style = st["code"]
        if any(ord(char) > 127 for char in line):
            style = ParagraphStyle("codeCJK", parent=style, fontName=SANS, wordWrap="CJK")
        p = Paragraph(escape(line).replace(" ", "&#160;") or "&#160;", style)
        rows.append([p])
    result = Table(rows, colWidths=[WIDTH], splitInRow=1)
    result.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f3f5f7")),
        ("BOX", (0, 0), (-1, -1), .4, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return result


def _note(text, kind, st):
    backgrounds = {"note": "#edf3f8", "warn": "#fff5df", "ok": "#edf6ef"}
    if kind not in backgrounds:
        raise ValueError(f"unknown note kind: {kind}")
    style = ParagraphStyle("callout", parent=st["note"], backColor=colors.HexColor(backgrounds[kind]),
                           borderColor=RULE, borderWidth=.4, borderPadding=9,
                           spaceBefore=9, spaceAfter=14)
    return _paragraph(text, style)


class VectorDiagram(Flowable):
    """Native PDF strokes and wrapped Unicode labels, no image tool needed."""
    def __init__(self, block, st):
        super().__init__()
        self.block, self.st = block, st
        self.width = WIDTH
        labels = block.nodes if isinstance(block, Diagram) else block.participants
        if not labels:
            raise ValueError("diagrams need nodes/participants")
        edges = block.edges if isinstance(block, Diagram) else block.messages
        if any(a < 0 or b < 0 or a >= len(labels) or b >= len(labels) for a, b, _ in edges):
            raise ValueError("diagram indexes are outside the node list")
        self.labels = [_paragraph(label, st["table"]) for label in labels]
        if isinstance(block, Diagram):
            self.box_width = WIDTH * .68
            self.heights = [max(38, p.wrap(self.box_width - 18, 1000)[1] + 16) for p in self.labels]
            self.height = sum(self.heights) + 34 * (len(labels) - 1)
        else:
            self.column = WIDTH / len(labels)
            self.top = max(40, max(p.wrap(self.column - 14, 1000)[1] for p in self.labels) + 14)
            self.height = self.top + 38 * len(block.messages) + 12
        if self.height > 650:
            raise ValueError("diagram exceeds one page; split into smaller figures")

    def wrap(self, availWidth, availHeight):
        return self.width, self.height

    def arrow(self, x1, y1, x2, y2):
        import math
        self.canv.line(x1, y1, x2, y2)
        angle = math.atan2(y2 - y1, x2 - x1)
        for offset in (-.5, .5):
            self.canv.line(x2, y2, x2 - 6 * math.cos(angle + offset), y2 - 6 * math.sin(angle + offset))

    def draw(self):
        c = self.canv
        c.saveState()
        c.setStrokeColor(ACCENT)
        if isinstance(self.block, Diagram):
            left = (WIDTH - self.box_width) / 2
            y, boxes = self.height, []
            for p, height in zip(self.labels, self.heights):
                bottom = y - height
                c.setFillColor(colors.HexColor("#edf3f8"))
                c.roundRect(left, bottom, self.box_width, height, 4, fill=1)
                ph = p.wrap(self.box_width - 18, height)[1]
                p.drawOn(c, left + 9, bottom + (height - ph) / 2)
                boxes.append((bottom, y))
                y = bottom - 34
            for a, b, label in self.block.edges:
                if a == b:
                    raise ValueError("flow self-loops need a separate explanatory figure")
                if abs(a - b) == 1:
                    y1 = boxes[a][0] if b > a else boxes[a][1]
                    y2 = boxes[b][1] if b > a else boxes[b][0]
                    self.arrow(WIDTH / 2, y1, WIDTH / 2, y2)
                    p = _paragraph(label, self.st["caption"])
                    ph = p.wrap(WIDTH / 2 - 15, 100)[1]
                    p.drawOn(c, WIDTH / 2 + 9, (y1 + y2 - ph) / 2)
                else:
                    side = left - 12
                    y1, y2 = sum(boxes[a]) / 2, sum(boxes[b]) / 2
                    c.line(left, y1, side, y1)
                    c.line(side, y1, side, y2)
                    self.arrow(side, y2, left, y2)
                    p = _paragraph(label, self.st["caption"])
                    ph = p.wrap(max(20, left - 20), 100)[1]
                    p.drawOn(c, 0, (y1 + y2 - ph) / 2)
        else:
            centers = [(i + .5) * self.column for i in range(len(self.labels))]
            for x, p in zip(centers, self.labels):
                ph = p.wrap(self.column - 14, self.top)[1]
                p.drawOn(c, x - self.column / 2 + 7, self.height - ph - 5)
                c.setDash(2, 3)
                c.line(x, self.height - self.top, x, 0)
                c.setDash()
            for i, (a, b, label) in enumerate(self.block.messages):
                y = self.height - self.top - 28 - i * 38
                if a == b:
                    x = centers[a]
                    c.line(x, y + 8, x + 12, y + 8)
                    c.line(x + 12, y + 8, x + 12, y)
                    self.arrow(x + 12, y, x, y)
                else:
                    self.arrow(centers[a], y, centers[b], y)
                p = _paragraph(label, self.st["caption"])
                span = max(self.column - 14, abs(centers[a] - centers[b]) - 12)
                ph = p.wrap(span, 100)[1]
                if ph > 28:
                    raise ValueError("sequence message too long; use prose for details")
                p.drawOn(c, min(centers[a], centers[b]) + 6, y + 4)
        c.restoreState()


def _render(block, st, chapter, index):
    if isinstance(block, (TeacherNote, Title)):
        return []
    if isinstance(block, (H1, H2)):
        level = 1 if isinstance(block, H1) else 2
        return [_heading(block.text, level, f"chapter-{chapter}-{block.anchor or index}", st)]
    if isinstance(block, P):
        return [_paragraph(block.text, st["body"])]
    if isinstance(block, (Bullets, Numbered)):
        return [Paragraph(escape(text), st["bullet"], bulletText=f"{n}." if isinstance(block, Numbered) else "•")
                for n, text in enumerate(block.items, 1)] + [Spacer(1, 5)]
    if isinstance(block, Code):
        caption = _paragraph(block.caption, st["caption"])
        caption.keepWithNext = True
        return ([caption] if block.caption else []) + [_code(block.text, st), Spacer(1, 10)]
    if isinstance(block, Term):
        return [_note(f"术语 · {block.name}\n{block.text}", "note", st)]
    if isinstance(block, Note):
        return [_note(block.text, block.kind, st)]
    if isinstance(block, Grid):
        return [_table(block.header, block.rows, block.widths, st), Spacer(1, 10)]
    if isinstance(block, Source):
        return [_paragraph(f"源码定位：{block.path}" + (f" · {block.symbol}" if block.symbol else ""), st["caption"])]
    if isinstance(block, ChapterRef):
        return [_paragraph(f"章节关联：第 {block.number} 课" + (f" · {block.text}" if block.text else ""), st["caption"])]
    if isinstance(block, PageBreakBlock):
        return [PageBreak()]
    if isinstance(block, (Diagram, SequenceDiagram)):
        figure = VectorDiagram(block, st)
        figure.keepWithNext = bool(block.caption)
        return [figure] + ([_paragraph(block.caption, st["caption"])] if block.caption else []) + [Spacer(1, 8)]
    if isinstance(block, Figure):
        if not 0 < block.width <= 1:
            raise ValueError("figure width must be a fraction in (0, 1]")
        image = Image(str(block.path))
        ratio = min(WIDTH * block.width / image.imageWidth, 580 / image.imageHeight)
        image.drawWidth, image.drawHeight = image.imageWidth * ratio, image.imageHeight * ratio
        image.keepWithNext = True
        return [image, _paragraph(block.caption, st["caption"])]
    raise TypeError(f"unsupported publication block: {type(block).__name__}")


def publish(lessons, out_path, *, title, subtitle="项目配套阅读 · 有 Python 基础即可开始"):
    register_fonts()
    lessons = list(lessons)
    if not lessons or len({lesson.number for lesson in lessons}) != len(lessons):
        raise ValueError("books need nonempty chapters with unique numbers")
    st = _styles()
    story = [Spacer(1, 65), _paragraph(title, st["title"]), _paragraph(subtitle, st["subtitle"]),
             Spacer(1, 30), _paragraph("从概念、项目代码到运行证据的连续阅读材料。", st["body"]),
             PageBreak(), _paragraph("目录", st["h1"])]
    toc = TableOfContents()
    toc.levelStyles = [st["toc0"], st["toc1"], st["toc2"]]
    toc.dotsMinLevel = 0
    story.extend([toc, PageBreak()])
    for position, lesson in enumerate(lessons):
        if position and not isinstance(story[-1], PageBreak):
            story.append(PageBreak())
        story.extend([_heading(f"第 {lesson.number} 课 · {lesson.title}", 0, f"chapter-{lesson.number}", st),
                      _paragraph(lesson.subtitle, st["subtitle"]), Spacer(1, 15)])
        has_h1, anchors = False, set()
        for index, block in enumerate(lesson.blocks):
            if isinstance(block, (H1, H2)):
                anchor = str(block.anchor or index)
                if anchor in anchors:
                    raise ValueError(f"duplicate chapter anchor: {anchor}")
                anchors.add(anchor)
            if isinstance(block, H2) and not has_h1:
                raise ValueError("H2 must follow an H1 within its chapter")
            if isinstance(block, H1):
                has_h1 = True
            for flow in _render(block, st, lesson.number, index):
                if isinstance(flow, PageBreak) and isinstance(story[-1], PageBreak):
                    continue
                story.append(flow)
    while isinstance(story[-1], PageBreak):
        story.pop()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = BookDocument(out, title)
    doc.multiBuild(story)
    return out, doc.page


def build(lesson: Lesson, out_path: str | Path) -> Path:
    return publish([lesson], out_path, title=f"第 {lesson.number} 课 · {lesson.title}", subtitle=lesson.subtitle)[0]
