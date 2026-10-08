"""Final publication checks with deliberately broken and real PDF artifacts."""
import importlib
from pathlib import Path
import re
import sys

import pytest
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docs/course"))
sys.path.insert(0, str(ROOT / "scripts"))
import deck


def qa():
    assert (ROOT / "scripts/qa_course_pdfs.py").exists(), "missing independent publication checker"
    return importlib.import_module("qa_course_pdfs")


def test_checker_rejects_a_blank_non_a4_pdf(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=400, height=400)
    path = tmp_path / "broken.pdf"
    writer.write(path)
    issues = qa().inspect_pdf(path)["issues"]
    assert any("A4" in item for item in issues)
    assert any("blank" in item for item in issues)
    assert any("font" in item for item in issues)


def test_checker_rejects_prohibited_prose_and_missing_source(tmp_path):
    lesson = deck.Lesson(1, "检查", blocks=[deck.H1("正文"),
        deck.P("下面请大家现场手写代码。"), deck.Source("src/no-such-file.py")])
    path = deck.build(lesson, tmp_path / "bad.pdf")
    issues = qa().inspect_pdf(path)["issues"]
    assert any("prohibited" in item for item in issues)
    assert any("source" in item for item in issues)


def test_checker_rejects_a_reference_only_tail_page(tmp_path):
    lesson = deck.Lesson(1, "尾页检查", blocks=[deck.H1("正文"), deck.P("独立正文。" * 30),
        deck.PageBreakBlock(), deck.ChapterRef(2, "下一章讨论调查控制与预算边界。")])
    path = deck.build(lesson, tmp_path / "tail.pdf")
    assert any("reference-only" in item for item in qa().inspect_pdf(path)["issues"])


def test_short_table_and_source_explanation_stay_attached(tmp_path):
    lesson = deck.Lesson(1, "分页", blocks=[deck.H1("正文")]
        + [deck.P("前置正文用于观察自然分页。" * 12) for _ in range(7)]
        + [deck.Grid(["短表标记", "值"], [[f"短表行{i}", "观察值"] for i in range(4)]),
           deck.Code("first_line = 1\nsecond_line = 2", caption="代码组标记"),
           deck.Source("src/ailab_ops/cli.py", "main"), deck.P("代码解释标记。" * 10)])
    pages = [p.extract_text() for p in PdfReader(deck.build(lesson, tmp_path / "groups.pdf")).pages]
    table_pages = [n for n, text in enumerate(pages) if "短表" in text]
    assert len(table_pages) == 1, "compact comparison table split"
    code_page = next(n for n, text in enumerate(pages) if "first_line" in text)
    assert "src/ailab_ops/cli.py" in pages[code_page], "source detached from listing"
    assert "代码解释标记" in pages[code_page], "explanation detached from listing"


def test_chinese_body_uses_embedded_regular_instance(tmp_path):
    lesson = deck.Lesson(1, "字重检查", blocks=[deck.H1("正文"), deck.P("中文应适合长时间阅读。")])
    reader = PdfReader(deck.build(lesson, tmp_path / "font.pdf"))
    fonts = [ref.get_object() for p in reader.pages for ref in p["/Resources"]["/Font"].values()]
    names = {str(f.get("/BaseFont", "")) for f in fonts}
    assert any("NotoSansSC-Regular" in name for name in names), names
    assert not any("Thin" in name for name in names)


def test_bullet_marks_use_embedded_font_and_grouped_heading_has_body(tmp_path):
    lesson = deck.Lesson(1, "标题粘连", blocks=[deck.H1("正文")]
        + [deck.P("前置正文用于观察自然分页。" * 12) for _ in range(6)]
        + [deck.H2("后续标题标记"), deck.Code("reading_code = 1\n" * 12),
           deck.Source("src/ailab_ops/cli.py"), deck.P("解释正文。" * 30),
           deck.Bullets(["中文项目符号也需要嵌入字体"])])
    path = deck.build(lesson, tmp_path / "headings.pdf")
    pages = [p.extract_text() for p in PdfReader(path).pages]
    heading_page = next(p for p in pages[2:] if "后续标题标记" in p)
    assert "reading_code" in heading_page, "heading detached from keep-together block"
    assert not qa().inspect_pdf(path)["issues"]


def test_source_after_listing_explanation_does_not_start_next_page(tmp_path):
    for count in range(2, 10):
        lesson = deck.Lesson(1, "尾部定位", blocks=[deck.H1("正文")]
            + [deck.P("前置正文用于观察自然分页。" * 12) for _ in range(count)]
            + [deck.Code("sample = 1\n" * 8), deck.P("代码解释。" * 20),
               deck.Source("src/ailab_ops/cli.py"), deck.P("下一段说明。" * 8)])
        path = deck.build(lesson, tmp_path / f"source-{count}.pdf")
        for page in PdfReader(path).pages:
            text = page.extract_text()
            text = re.sub(r"Enterprise Incident Agent V2|项目阅读材料|知识 · 代码 · 运行证据|第 \d+ 页", "", text).strip()
            assert not text.startswith("源码定位"), f"source detached with {count} preceding paragraphs"


def test_teacher_notes_have_six_routes_and_are_separate():
    path = ROOT / "docs/course/TEACHING_NOTES.md"
    assert path.exists(), "missing independent teacher reading routes"
    text = path.read_text()
    for n in range(1, 7):
        assert f"第 {n} 课" in text
    for phrase in ("带读路线", "停顿问题", "演示命令", "易错边界"):
        assert text.count(phrase) >= 6


def test_deferred_terms_and_wrapping_are_resolved():
    from lessons import ALL
    texts = ["\n".join(getattr(b, "text", "") for b in l.blocks) for l in ALL]
    assert "自然结束" in texts[1] and "transport" in texts[1]
    assert "置信度分段" in texts[4]
    assert "每秒请求数" in texts[5] and "令牌桶" in texts[5] and "突发容量" in texts[5]
    assert all("get_case_job" not in str(b) for b in ALL[3].blocks)
    snippets = [b for b in ALL[0].blocks if isinstance(b, deck.Code) and "json.dumps(payload" in b.text]
    assert snippets and max(map(len, snippets[0].text.splitlines())) < 85


def test_seven_final_books_pass_independent_publication_audit():
    result = qa().inspect_publication(ROOT / "docs/course/pdf")
    assert len(result["artifacts"]) == 7
    assert result["ok"], result["issues"]


def test_publication_audit_rejects_missing_font_distribution_licenses(tmp_path):
    assert callable(getattr(qa(), "inspect_font_licenses", None)), "missing distribution-license audit"
    issues = qa().inspect_font_licenses(tmp_path)
    assert any("OFL-NotoSansSC.txt" in issue and "missing" in issue for issue in issues)
    assert any("OFL-JetBrainsMono.txt" in issue and "missing" in issue for issue in issues)


def test_publication_audit_rejects_truncated_font_license(tmp_path):
    assert callable(getattr(qa(), "inspect_font_licenses", None)), "missing distribution-license audit"
    for name in ("OFL-NotoSansSC.txt", "OFL-JetBrainsMono.txt"):
        (tmp_path / name).write_text("SIL OPEN FONT LICENSE Version 1.1\n")
    issues = qa().inspect_font_licenses(tmp_path)
    assert len(issues) == 2
    assert all("incomplete or changed" in issue for issue in issues)


def test_repository_ships_verified_complete_font_licenses():
    assert callable(getattr(qa(), "inspect_font_licenses", None)), "missing distribution-license audit"
    assert qa().inspect_font_licenses(ROOT / "docs/course/fonts") == []


def code_starts(reader, tokens):
    """Measure first nonspace glyph x on actual rendered PDF code baselines."""
    from reportlab.pdfbase import pdfmetrics
    starts = []
    for number, page in enumerate(reader.pages):
        def capture(text, cm, tm, font, size):
            if size != 8 or not text.lstrip().startswith(tokens):
                return
            name = str(font.get("/BaseFont", ""))
            family = deck.MONO if "JetBrainsMono" in name else deck.SANS
            prefix = text[:len(text) - len(text.lstrip())]
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            starts.append((number, text.lstrip(), x + pdfmetrics.stringWidth(prefix, family, size), name))
        page.extract_text(visitor_text=capture)
    return starts


def test_mixed_code_keeps_fixed_indent_and_ascii_monospace_in_real_pdf(tmp_path):
    lesson = deck.Lesson(1, "混合代码缩进", blocks=[deck.H1("代码"), deck.Code(
        'root_call = 1\n'
        '    mono_call = "ascii"\n'
        '    cjk_call = "中文状态"\n'
        '\ttab_call = "中文"\n'
        '        deep_call = "中文"')])
    reader = PdfReader(deck.build(lesson, tmp_path / "indent.pdf"))
    starts = code_starts(reader, ("root_call", "mono_call", "cjk_call", "tab_call", "deep_call"))
    assert len(starts) == 5
    base = starts[0][2]
    for (_, text, x, font), offset in zip(starts, (0, 19.2, 19.2, 19.2, 38.4)):
        assert x == pytest.approx(base + offset, abs=.01), (text, x, base + offset)
        assert "JetBrainsMono" in font, "ASCII code must stay monospace even on CJK lines"
    assert "中文状态" in "".join(p.extract_text() for p in reader.pages)


def test_chapter_four_chinese_print_aligns_with_actual_api_client_block(tmp_path):
    from lessons.l4 import LESSON
    reader = PdfReader(deck.build(LESSON, tmp_path / "actual-chapter-four.pdf"))
    starts = code_starts(reader, ('print("', "approved = client.post", "print(done.json"))
    assert len(starts) == 4
    assert all(x == pytest.approx(starts[-1][2], abs=.01) for _, _, x, _ in starts)


def test_mixed_code_still_wraps_and_splits_without_losing_searchable_text(tmp_path):
    text = "\n".join(f'    marker_{n} = "' + "中文_readable_value_" * 25 + '"' for n in range(45))
    lesson = deck.Lesson(1, "混合代码跨页", blocks=[deck.H1("代码"),
        deck.P("混排代码应当保留缩进、中文文本和跨页内容。"), deck.Code(text)])
    path = deck.build(lesson, tmp_path / "mixed-long.pdf")
    reader = PdfReader(path)
    assert len(reader.pages) > 4
    bodies = [re.sub(r"Enterprise Incident Agent V2|项目阅读材料|知识 · 代码 · 运行证据|第 \d+ 页", "", p.extract_text())
              for p in reader.pages]
    extracted = re.sub(r"\s+", "", "".join(bodies))
    for n in range(45):
        assert f'marker_{n}="' + "中文_readable_value_" * 25 + '"' in extracted
    assert qa().inspect_pdf(path)["issues"] == []
