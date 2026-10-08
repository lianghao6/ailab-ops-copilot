#!/usr/bin/env python3
"""Build A4 chapter books and the collected edition from local assets."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from book import BOOK_FILENAME, Book
from deck import Lesson, publish

OUT = HERE / "pdf"


@dataclass(frozen=True)
class PDFArtifact:
    path: Path
    title: str
    pages: int


def build_all(lessons: list[Lesson], output: Path = OUT, *, numbers=None,
              combined: bool = True, chapters: bool = True) -> list[PDFArtifact]:
    lessons = sorted(lessons, key=lambda lesson: lesson.number)
    if numbers and set(numbers) - {lesson.number for lesson in lessons}:
        raise ValueError("requested chapter does not exist")
    artifacts = []
    if chapters:
        for lesson in lessons:
            if numbers and lesson.number not in numbers:
                continue
            title = f"第 {lesson.number} 课 · {lesson.title}"
            path, pages = publish([lesson], output / f"lesson-{lesson.number}.pdf", title=title, subtitle=lesson.subtitle)
            artifacts.append(PDFArtifact(path, title, pages))
    if combined:
        book = Book(lessons)
        path, pages = publish(book.chapters, output / BOOK_FILENAME, title=book.title, subtitle=book.subtitle)
        artifacts.append(PDFArtifact(path, book.title, pages))
    return artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lessons", nargs="*", type=int, choices=range(1, 7), metavar="N")
    parser.add_argument("--combined", action="store_true", help="build only the complete six-chapter edition")
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--json", action="store_true", help="emit artifact paths and page counts as JSON")
    args = parser.parse_args(argv)
    if args.combined and args.lessons:
        parser.error("--combined cannot be combined with chapter numbers")
    from lessons import ALL
    artifacts = build_all(ALL, args.output, numbers=args.lessons or None,
                          combined=args.combined or not args.lessons, chapters=not args.combined)
    if args.json:
        print(json.dumps([{"path": str(a.path), "title": a.title, "pages": a.pages} for a in artifacts], ensure_ascii=False))
    else:
        for artifact in artifacts:
            print(f"{artifact.path.name}: {artifact.pages} 页 · {artifact.title}")
        print(f"输出目录: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
