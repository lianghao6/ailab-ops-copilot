#!/usr/bin/env python3
"""Audit searchable A4 project books; optionally render every page with PDFium.

Default audit uses only course dependencies. --render additionally needs
pypdfium2 and Pillow (they may be installed in a temporary PYTHONPATH).
Geometric checks are conservative text-area checks, not a replacement for
visual review of table cells, vector arrows or whitespace.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import json
from pathlib import Path
import re
import sys

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
COURSE = ROOT / "docs/course"
sys.path.insert(0, str(COURSE))
import deck

FILENAMES = [f"lesson-{n}.pdf" for n in range(1, 7)] + ["enterprise-incident-agent-v2.pdf"]
PROHIBITED = ("下面请大家", "现场手写代码", "口语化讲稿", "小面试", "下节课开始写代码",
              "手写一个agentloop", "不调模型一样能出诊断", "get_case_job")
FACTS = {
    1: ("从LLM到Agent", "case-gpu-assert", "get_case_snapshot", "人工编写回放", "结构化输出"),
    2: ("让调查过程可控", "budget_exhausted", "duplicate_call", "自然结束", "transport"),
    3: ("RAG、证据链与可信回答", "4000", "truncated", "V1", "PYTHONHASHSEED=0"),
    4: ("安全边界与人工审批", "get_case_snapshot", "recommendations", "simulated", "execution_replayed", "未经认证"),
    5: ("如何评测一个Agent", "null", "置信度分段", "九次", "语义"),
    6: ("从Demo到企业级服务", "每秒请求数", "令牌桶", "突发容量", "Retry-After", "进程内"),
}

# Complete authoritative OFL snapshots retrieved on 2026-10-08; normalization
# removes only line-ending/trailing whitespace. Attribution lives beside files.
FONT_LICENSE_DIGESTS = {
    "OFL-NotoSansSC.txt": "babcfe66c8a098b2fa279bc724a3a342f8124f77ce18941fbcc1bbb39823cded",
    "OFL-JetBrainsMono.txt": "c1ab7c666206842a02b35b30770dac0d7a10156ed401c9defc3f02a754d89e90",
}


def inspect_font_licenses(directory: Path):
    """Reject missing/truncated/changed distributed copyright and OFL text."""
    issues = []
    for name, digest in FONT_LICENSE_DIGESTS.items():
        path = directory / name
        if not path.is_file():
            issues.append(f"missing font distribution license: {name}")
            continue
        text = "\n".join(line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()) + "\n"
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
            issues.append(f"font license {name}: incomplete or changed authoritative text")
    return issues


def compact(value):
    return re.sub(r"\s+", "", value)


def _body(text, number):
    for fragment in ("Enterprise Incident Agent V2", "项目阅读材料", "知识 · 代码 · 运行证据", f"第 {number} 页"):
        text = text.replace(fragment, "")
    return text.strip()


def inspect_pdf(path: Path, *, chapter: int | None = None) -> dict:
    path = Path(path)
    issues, warnings, pages, fonts = [], [], [], {}
    if not path.is_file():
        return {"path": str(path), "pages": 0, "issues": ["missing artifact"], "warnings": []}
    try:
        reader = PdfReader(path)
    except Exception as exc:
        return {"path": str(path), "pages": 0, "issues": [f"unreadable PDF: {type(exc).__name__}"], "warnings": []}
    deck.register_fonts()
    from reportlab.pdfbase import pdfmetrics
    for number, page in enumerate(reader.pages, 1):
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        if abs(width - 595.276) > .05 or abs(height - 841.89) > .05:
            issues.append(f"page {number}: not A4 ({width:g} × {height:g})")
        used_fonts, segments, out = {}, [], []
        def capture(text, cm, tm, font, size):
            if not text.strip():
                return
            font = font or {}
            name = str(font.get("/BaseFont", "unknown"))
            used_fonts[name] = font
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            segments.append((text, size, x, y))
            family = deck.MONO if "JetBrainsMono" in name else deck.SANS
            # Generated books have no rotated text. ReportLab may emit several
            # lines as one segment; width uses the widest actual line.
            length = max(pdfmetrics.stringWidth(line, family, size) for line in text.splitlines())
            if x < 18 * 72 / 25.4 - 1 or x + length > width - 18 * 72 / 25.4 + 1 or y < 20 or y > height - 20:
                out.append(text.strip()[:80])
        text = page.extract_text(visitor_text=capture) or ""
        body = _body(text, number)
        if len(compact(body)) < 20:
            issues.append(f"page {number}: blank or decoration-only")
        if body.startswith("章节关联：") and len(body.splitlines()) <= 2:
            issues.append(f"page {number}: reference-only tail page")
        if "\ufffd" in text or "\x00" in text:
            issues.append(f"page {number}: broken text extraction")
        for name, font in used_fonts.items():
            descriptor = font.get("/FontDescriptor")
            descriptor = descriptor.get_object() if descriptor else {}
            embedded = any(k in descriptor for k in ("/FontFile", "/FontFile2", "/FontFile3"))
            unicode = "/ToUnicode" in font
            fonts[name] = {"embedded": embedded, "unicode": unicode}
            if not embedded or not unicode:
                issues.append(f"page {number}: used font {name} lacks embedding/ToUnicode")
        if out:
            issues.append(f"page {number}: text outside reading/page area: {out}")
        # Paragraph headings are >=12 pt. Decorations precede story content in
        # drawing order; only flag a last story segment that is itself a heading.
        story_segments = [s for s in segments if _body(s[0], number)]
        if story_segments and story_segments[-1][1] >= 12:
            issues.append(f"page {number}: isolated final heading: {story_segments[-1][0].strip()}")
        pages.append({"page": number, "text_chars": len(body), "width": width, "height": height})
    if not fonts:
        issues.append("no used embedded fonts")
    all_text = "\n".join(p.extract_text() or "" for p in reader.pages)
    flattened = compact(all_text)
    for phrase in PROHIBITED:
        if compact(phrase) in flattened:
            issues.append(f"prohibited prose/tool: {phrase}")
    for match in re.finditer(r"源码定位[：:]([A-Za-z0-9_./-]+\.(?:jsonl|json|py|md))", flattened):
        if not (ROOT / match.group(1)).is_file():
            issues.append(f"missing source path: {match.group(1)}")
    numbers = [chapter] if chapter else (range(1, 7) if path.name == FILENAMES[-1] else [])
    for number in numbers:
        for phrase in FACTS[number]:
            if compact(phrase) not in flattened:
                issues.append(f"chapter {number}: missing key fact {phrase}")
    if not reader.outline:
        issues.append("missing PDF outline")
    if "目录" not in all_text:
        issues.append("missing table of contents")
    return {"path": str(path), "pages": len(reader.pages), "fonts": fonts,
            "page_checks": pages, "issues": issues, "warnings": warnings}


def inspect_sources():
    issues = []
    from lessons import ALL
    for lesson in ALL:
        for source in (b for b in lesson.blocks if isinstance(b, deck.Source)):
            path = ROOT / source.path
            if not path.is_file():
                issues.append(f"chapter {lesson.number}: missing source {source.path}")
            elif path.suffix == ".py" and source.symbol:
                names = {node.name for node in ast.walk(ast.parse(path.read_text()))
                         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
                if source.symbol.split(".")[-1] not in names:
                    issues.append(f"chapter {lesson.number}: missing symbol {source.symbol}")
    return issues


def inspect_publication(directory: Path) -> dict:
    artifacts = [inspect_pdf(directory / name, chapter=n if n < 7 else None)
                 for n, name in enumerate(FILENAMES, 1)]
    issues = [f"{Path(a['path']).name}: {issue}" for a in artifacts for issue in a["issues"]]
    issues.extend(inspect_sources())
    issues.extend(inspect_font_licenses(COURSE / "fonts"))
    for artifact in artifacts:
        if artifact["pages"] < 3:
            issues.append(f"{Path(artifact['path']).name}: implausibly short publication")
    return {"ok": not issues, "artifacts": artifacts, "issues": issues}


def render_publication(directory: Path, output: Path, *, scale=1.3):
    import pypdfium2 as pdfium
    from PIL import Image, ImageDraw
    output.mkdir(parents=True, exist_ok=True)
    report = {"renderer": str(pdfium.PYPDFIUM_INFO), "scale": scale, "artifacts": []}
    for name in FILENAMES:
        doc = pdfium.PdfDocument(directory / name)
        target = output / Path(name).stem
        target.mkdir(exist_ok=True)
        sheets, outside = [], []
        for first in range(0, len(doc), 20):
            count = min(20, len(doc) - first)
            sheet = Image.new("RGB", (1200, 440 * ((count + 3) // 4)), "#dce2e8")
            draw = ImageDraw.Draw(sheet)
            for index in range(first, first + count):
                page = doc[index]
                image = page.render(scale=scale).to_pil().convert("RGB")
                image.save(target / f"page-{index + 1:03}.png")
                image.thumbnail((290, 410))
                x, y = (index - first) % 4 * 300 + 5, (index - first) // 4 * 440 + 20
                sheet.paste(image, (x, y))
                draw.text((x, y - 16), f"{Path(name).stem} / {index + 1}", fill="black")
                textpage = page.get_textpage()
                width, height = page.get_size()
                for char in range(textpage.count_chars()):
                    x1, y1, x2, y2 = textpage.get_charbox(char)
                    if x1 < -.5 or y1 < -.5 or x2 > width + .5 or y2 > height + .5:
                        outside.append({"page": index + 1, "char": char, "box": [x1, y1, x2, y2]})
                textpage.close()
                page.close()
            contact = target / f"contact-{first + 1:03}.png"
            sheet.save(contact)
            sheets.append(str(contact))
        report["artifacts"].append({"name": name, "pages": len(doc), "contacts": sheets,
                                    "outside_page_charboxes": outside})
        doc.close()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=COURSE / "pdf")
    parser.add_argument("--render", type=Path, help="render pages/contact sheets into this local directory")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = inspect_publication(args.directory)
    if args.render:
        result["render"] = render_publication(args.directory, args.render)
        for artifact in result["render"]["artifacts"]:
            if artifact["outside_page_charboxes"]:
                result["issues"].append(f"{artifact['name']}: rendered glyphs outside page")
        result["ok"] = not result["issues"]
        (args.render / "qa.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for artifact in result["artifacts"]:
            print(f"{Path(artifact['path']).name}: {artifact['pages']} pages / {len(artifact['issues'])} issues")
        for issue in result["issues"]:
            print(issue)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
