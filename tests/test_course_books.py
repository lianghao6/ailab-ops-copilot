"""Publication contracts checked on real PDF artifacts, not source strings."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from pypdf import PdfReader

COURSE = Path(__file__).resolve().parents[1] / "docs" / "course"
sys.path.insert(0, str(COURSE))
deck = importlib.import_module("deck")


def engine():
    # A useful assertion on the old engine, rather than a collection import error.
    assert hasattr(deck, "TeacherNote"), "student books need an explicit teacher-only block"
    return deck


def sample_lessons():
    d = engine()
    return [d.Lesson(n, f"案例主题{n}", "可独立阅读的项目材料", blocks=[
        d.H1("观察与假设"), d.P(f"学生正文标记{n}。" * 50),
        d.H2("引用与控制"), d.Bullets(["工具结果是观察", "引用需要校验"]),
        d.Numbered(["读取任务", "验证证据"]),
        d.Code("def inspect(job_id):\n    return {'状态': job_id}", caption="代码示例"),
        d.Grid(["字段", "含义"], [["evidence_id", "稳定引用"]], widths=[1, 2]),
        d.Term("证据", "来自可追溯工具结果的事实。"), d.Note("边界提醒", "warn"),
        d.Source("src/ailab_ops/cli.py", "main"), d.ChapterRef(2, "控制流程"),
        d.Diagram(["输入", "模型", "工具"], [(0, 1, "消息"), (1, 2, "调用")], caption="调查流程"),
        d.SequenceDiagram(["用户", "Agent", "工具"], [(0, 1, "调查"), (1, 2, "只读查询"), (2, 1, "证据")], caption="调用时序"),
        d.TeacherNote("教师专用不可泄露标记"),
        d.PageBreakBlock(), d.P("章节结尾标记"),
    ]) for n in range(1, 7)]


@pytest.fixture(scope="module")
def publications(tmp_path_factory):
    engine()
    builder = importlib.import_module("build")
    root = tmp_path_factory.mktemp("books")
    artifacts = builder.build_all(sample_lessons(), root)
    return root, artifacts


def test_builds_six_chapters_and_one_combined_book(publications):
    root, artifacts = publications
    assert {p.name for p in root.glob("*.pdf")} == {
        "lesson-1.pdf", "lesson-2.pdf", "lesson-3.pdf", "lesson-4.pdf",
        "lesson-5.pdf", "lesson-6.pdf", "enterprise-incident-agent-v2.pdf",
    }
    assert len(artifacts) == 7
    assert all(a.pages >= 3 and a.path.is_file() for a in artifacts)


def test_student_books_have_searchable_chinese_without_teacher_notes(publications):
    root, _ = publications
    for filename in ("lesson-1.pdf", "enterprise-incident-agent-v2.pdf"):
        reader = PdfReader(root / filename)
        text = "\n".join(p.extract_text() for p in reader.pages)
        for expected in ("学生正文标记1", "观察与假设", "代码示例", "调查流程", "调用时序", "src/ailab_ops/cli.py", "第 2 课", "章节结尾标记"):
            assert expected in text
        assert "教师专用不可泄露标记" not in text


def test_a4_metadata_running_pages_toc_and_outline(publications):
    root, _ = publications
    reader = PdfReader(root / "enterprise-incident-agent-v2.pdf")
    assert "企业级故障响应 Agent" in reader.metadata.title
    assert reader.metadata.author
    assert reader.metadata.subject
    for n, page in enumerate(reader.pages, 1):
        assert float(page.mediabox.width) == pytest.approx(595.276, abs=0.02)
        assert float(page.mediabox.height) == pytest.approx(841.89, abs=0.02)
        assert f"第 {n} 页" in page.extract_text()
        assert "Enterprise Incident Agent V2" in page.extract_text()
    toc = "\n".join(p.extract_text() for p in reader.pages[:3])
    assert "目录" in toc and "案例主题6" in toc
    outline_titles = []
    def visit(items):
        for item in items:
            if isinstance(item, list):
                visit(item)
            else:
                outline_titles.append(item.title)
    visit(reader.outline)
    assert any("案例主题6" in title for title in outline_titles)
    assert "观察与假设" in outline_titles


def test_fonts_are_embedded_and_have_unicode_maps(publications):
    root, _ = publications
    reader = PdfReader(root / "lesson-1.pdf")
    embedded_names = set()
    for page in reader.pages:
        for ref in page["/Resources"]["/Font"].values():
            font = ref.get_object()
            descriptor = font.get("/FontDescriptor")
            if descriptor and "/FontFile2" in descriptor.get_object():
                assert "/ToUnicode" in font
                embedded_names.add(str(font["/BaseFont"]))
    assert any("NotoSansSC" in name for name in embedded_names)
    assert any("JetBrainsMono" in name for name in embedded_names)


def test_repeat_build_stable_properties(publications, tmp_path):
    root, _ = publications
    importlib.import_module("build").build_all(sample_lessons(), tmp_path)
    for source in root.glob("*.pdf"):
        first, second = PdfReader(source), PdfReader(tmp_path / source.name)
        assert len(first.pages) == len(second.pages)
        assert [p.extract_text() for p in first.pages] == [p.extract_text() for p in second.pages]


def test_cli_single_combined_and_invalid_selection(tmp_path):
    engine()
    script = str(COURSE / "build.py")
    result = subprocess.run([sys.executable, script, "2", "--output", str(tmp_path), "--json"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    metadata = json.loads(result.stdout)
    assert [Path(a["path"]).name for a in metadata] == ["lesson-2.pdf"]
    assert metadata[0]["pages"] > 0
    result = subprocess.run([sys.executable, script, "--combined", "--output", str(tmp_path), "--json"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)[0]["path"]).name == "enterprise-incident-agent-v2.pdf"
    invalid = subprocess.run([sys.executable, script, "7", "--output", str(tmp_path)], capture_output=True, text=True)
    assert invalid.returncode != 0


def test_long_table_code_and_diagram_labels_preserve_content(tmp_path):
    d = engine()
    lesson = d.Lesson(1, "压力版式", "", blocks=[
        d.H1("连续阅读"),
        d.Grid(["项目", "事实"], [[f"行{i}", "证据摘录" * 15] for i in range(70)]),
        d.Code("\n".join(f"line_{i} = '" + "long_identifier_" * 12 + "'" for i in range(80))),
        d.Diagram(["很长的中文节点说明" * 3, "可控调查", "最终报告"], [(0, 1, "事实"), (1, 2, "校验")]),
        d.P("最终正文保存标记"),
    ])
    path = d.build(lesson, tmp_path / "stress.pdf")
    reader = PdfReader(path)
    text = "\n".join(p.extract_text() for p in reader.pages)
    assert "行69" in text and "line_79" in text and "最终正文保存标记" in text
    assert all(p.extract_text().strip() for p in reader.pages)


def test_oversize_table_cell_can_continue_across_pages(tmp_path):
    d = engine()
    lesson = d.Lesson(1, "跨页证据", blocks=[d.H1("原文"),
        d.Grid(["来源", "长摘录"], [["日志", "日志观察内容。" * 1200 + "长单元格结束标记"]], widths=[1, 3]),
    ])
    reader = PdfReader(d.build(lesson, tmp_path / "cell.pdf"))
    text = "\n".join(p.extract_text() for p in reader.pages)
    assert "长单元格结束标记" in text
    assert sum("长摘录" in p.extract_text() for p in reader.pages) >= 2


def test_duplicate_author_anchors_fail_before_publication(tmp_path):
    d = engine()
    lesson = d.Lesson(1, "稳定定位", blocks=[d.H1("开篇", anchor="entry"),
        d.P("正文"), d.H1("其他", anchor="entry"), d.P("正文")])
    with pytest.raises(ValueError, match="anchor"):
        d.build(lesson, tmp_path / "duplicate.pdf")


def test_figure_caption_stays_on_the_figure_page(tmp_path):
    d = engine()
    # Local image fixture: a one-pixel PNG, no network or browser rendering.
    import base64
    image = tmp_path / "pixel.png"
    image.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j8l0AAAAASUVORK5CYII="))
    lesson = d.Lesson(1, "图片", blocks=[d.H1("当前界面"), d.Figure(image, "图注唯一标记")])
    reader = PdfReader(d.build(lesson, tmp_path / "figure.pdf"))
    figure_pages = [p for p in reader.pages if "/XObject" in p["/Resources"]]
    assert len(figure_pages) == 1
    assert "图注唯一标记" in figure_pages[0].extract_text()


def test_chapter_boundary_collapses_redundant_page_breaks(tmp_path):
    d = engine()
    chapters = [d.Lesson(n, f"第{n}章", blocks=[d.H1("正文标题"), d.P("章节正文"),
        d.PageBreakBlock(), d.TeacherNote("隐藏备课提示"), d.PageBreakBlock()]) for n in (1, 2)]
    builder = importlib.import_module("build")
    builder.build_all(chapters, tmp_path, chapters=False)
    reader = PdfReader(tmp_path / "enterprise-incident-agent-v2.pdf")
    # One cover, one TOC, one actual reading page per short chapter.
    assert len(reader.pages) == 4
    assert "章节正文" in reader.pages[2].extract_text()
    assert "章节正文" in reader.pages[3].extract_text()
