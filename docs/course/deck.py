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
from reportlab.platypus import BaseDocTemplate, Flowable, Frame, Image, KeepTogether, PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.platypus.tableofcontents import TableOfContents

FONT_DIR = Path(__file__).with_name("fonts")
SANS, MONO = "NotoSC", "Mono"
INK, MUTED, ACCENT, RULE = [colors.HexColor(c) for c in ("#202b36", "#586879", "#205777", "#cbd5df")]
WIDTH = A4[0] - 40 * mm
BODY_SIZE = 10.5


def register_fonts():
    for name, filename in ((SANS, "NotoSansSC-Regular.ttf"), (MONO, "JetBrainsMono.ttf")):
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
        "bullet": style("bullet", BODY_SIZE, 17, leftIndent=15, bulletIndent=2,
                        bulletFontName=SANS, bulletFontSize=BODY_SIZE, spaceAfter=4),
        "table": style("table", 9, 14),
        "caption": style("caption", 9, 14, spaceAfter=6),
        "code": ParagraphStyle("code", fontName=MONO, fontSize=8, leading=12, textColor=INK, splitLongWords=True),
        "note": style("note", 10, 16),
        "toc0": style("toc0", 12, 18, spaceBefore=6),
        "toc1": style("toc1", 10, 15, leftIndent=14, spaceBefore=1.5),
        "toc2": style("toc2", 9, 13.5, leftIndent=28),
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
            self.gaps = [34] * (len(labels) - 1)
            self.edge_labels = []
            for a, b, text in block.edges:
                if a == b:
                    raise ValueError("flow self-loops need a separate explanatory figure")
                adjacent = abs(a - b) == 1
                width = WIDTH / 2 - 15 if adjacent else (WIDTH - self.box_width) / 2 - 20
                p = _paragraph(text, st["caption"])
                ph = p.wrap(width, 1000)[1]
                if adjacent:
                    self.gaps[min(a, b)] = max(self.gaps[min(a, b)], ph + 12)
                self.edge_labels.append((p, ph))
            self.height = sum(self.heights) + sum(self.gaps)
            self.boxes, y = [], self.height
            for i, height in enumerate(self.heights):
                self.boxes.append((y - height, y))
                y -= height + (self.gaps[i] if i < len(self.gaps) else 0)
            for (a, b, _), (_, ph) in zip(block.edges, self.edge_labels):
                if abs(a - b) != 1:
                    midpoint = (sum(self.boxes[a]) + sum(self.boxes[b])) / 4
                    if ph / 2 + 6 > min(midpoint, self.height - midpoint):
                        raise ValueError("exterior flow label does not fit; shorten label or split the figure")
        else:
            self.column = WIDTH / len(labels)
            self.top = max(40, max(p.wrap(self.column - 14, 1000)[1] for p in self.labels) + 14)
            self.message_labels = []
            for a, b, text in block.messages:
                if a == b:
                    left, span = a * self.column + 7, self.column - 14
                else:
                    left = (min(a, b) + .5) * self.column + 6
                    span = abs(a - b) * self.column - 12
                if span <= 0:
                    raise ValueError("sequence label has no room; use fewer participants")
                p = _paragraph(text, st["caption"])
                ph = p.wrap(span, 1000)[1]
                self.message_labels.append((p, left, ph, max(38, ph + 24)))
            self.height = self.top + sum(item[3] for item in self.message_labels) + 12
        if self.height > 650:
            raise ValueError("diagram and labels exceed one page; shorten labels or split into smaller figures")

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
            for p, height, (bottom, _) in zip(self.labels, self.heights, self.boxes):
                c.setFillColor(colors.HexColor("#edf3f8"))
                c.roundRect(left, bottom, self.box_width, height, 4, fill=1)
                ph = p.wrap(self.box_width - 18, height)[1]
                p.drawOn(c, left + 9, bottom + (height - ph) / 2)
            for (a, b, _), (p, ph) in zip(self.block.edges, self.edge_labels):
                if abs(a - b) == 1:
                    y1 = self.boxes[a][0] if b > a else self.boxes[a][1]
                    y2 = self.boxes[b][1] if b > a else self.boxes[b][0]
                    self.arrow(WIDTH / 2, y1, WIDTH / 2, y2)
                    p.drawOn(c, WIDTH / 2 + 9, (y1 + y2 - ph) / 2)
                else:
                    side = left - 12
                    y1, y2 = sum(self.boxes[a]) / 2, sum(self.boxes[b]) / 2
                    c.line(left, y1, side, y1)
                    c.line(side, y1, side, y2)
                    self.arrow(side, y2, left, y2)
                    p.drawOn(c, 0, (y1 + y2 - ph) / 2)
        else:
            centers = [(i + .5) * self.column for i in range(len(self.labels))]
            for x, p in zip(centers, self.labels):
                ph = p.wrap(self.column - 14, self.top)[1]
                p.drawOn(c, x - self.column / 2 + 7, self.height - ph - 5)
                c.setDash(2, 3)
                c.line(x, self.height - self.top, x, 0)
                c.setDash()
            offset = 0
            for (a, b, _), (p, left, ph, spacing) in zip(self.block.messages, self.message_labels):
                y = self.height - self.top - ph - 18 - offset
                if a == b:
                    x = centers[a]
                    c.line(x, y + 8, x + 12, y + 8)
                    c.line(x + 12, y + 8, x + 12, y)
                    self.arrow(x + 12, y, x, y)
                else:
                    self.arrow(centers[a], y, centers[b], y)
                p.drawOn(c, left, y + 12)
                offset += spacing
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
        table = _table(block.header, block.rows, block.widths, st)
        # Keep compact comparisons intact; long tables still repeat headers and
        # can split inside an oversized row.
        if table.wrap(WIDTH, 1000)[1] <= 350:
            table = KeepTogether([table])
        return [table, Spacer(1, 10)]
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
    toc.tableStyle = TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                ("TOPPADDING", (0, 0), (-1, -1), 0),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 1)])
    story.extend([toc, PageBreak()])
    for position, lesson in enumerate(lessons):
        if position and not isinstance(story[-1], PageBreak):
            story.append(PageBreak())
        story.extend([_heading(f"第 {lesson.number} 课 · {lesson.title}", 0, f"chapter-{lesson.number}", st),
                      _paragraph(lesson.subtitle, st["subtitle"]), Spacer(1, 15)])
        has_h1, anchors = False, set()
        rendered = []
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
            rendered.append((block, _render(block, st, lesson.number, index)))
        # Source locators belong to the preceding explanation/listing. Group a
        # modest related sequence, not a whole section: oversized material must
        # retain normal splitting rather than creating a blank page or overflow.
        index = 0
        while index < len(rendered):
            block, flows = rendered[index]
            group = list(flows)
            end = index + 1
            while end < len(rendered) and isinstance(rendered[end][0], Source):
                group.extend(rendered[end][1])
                end += 1
            if (end > index + 1 or isinstance(block, (Code, Source))) and end < len(rendered) and isinstance(rendered[end][0], P):
                group.extend(rendered[end][1])
                end += 1
                # A locator may follow the explanation instead of the listing.
                # Consuming that P must not leave its own Sources ungrouped.
                while end < len(rendered) and isinstance(rendered[end][0], Source):
                    group.extend(rendered[end][1])
                    end += 1
            # KeepTogether's wrap intentionally returns an enormous sentinel.
            # Flatten its compact table wrapper for the actual height estimate.
            flat = []
            for flow in group:
                flat.extend(flow._content if isinstance(flow, KeepTogether) else [flow])
            height = sum(flow.wrap(WIDTH, 10000)[1] + flow.getSpaceBefore() + flow.getSpaceAfter()
                         for flow in flat if not isinstance(flow, PageBreak))
            if end > index + 1 and height <= 600 and not any(isinstance(f, PageBreak) for f in flat):
                output = [KeepTogether(flat)]
            else:
                output = group
            if output and isinstance(output[0], KeepTogether):
                # ReportLab nests keepWithNext headings around KeepTogether,
                # then can split off the heading. Use one flat group instead.
                preceding = []
                while story and hasattr(story[-1], "book_heading"):
                    preceding.insert(0, story.pop())
                if preceding:
                    output = [KeepTogether(preceding + output[0]._content)] + output[1:]
            for flow in output:
                if isinstance(flow, PageBreak) and isinstance(story[-1], PageBreak):
                    continue
                story.append(flow)
            index = end
    while isinstance(story[-1], PageBreak):
        story.pop()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = BookDocument(out, title)
    doc.multiBuild(story)
    return out, doc.page


def build(lesson: Lesson, out_path: str | Path) -> Path:
    return publish([lesson], out_path, title=f"第 {lesson.number} 课 · {lesson.title}", subtitle=lesson.subtitle)[0]
