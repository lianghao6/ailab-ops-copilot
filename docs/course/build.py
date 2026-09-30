#!/usr/bin/env python3
"""把六节课的 PDF 生成到 docs/course/pdf/ 下。

    python3 docs/course/build.py          # 全部
    python3 docs/course/build.py 3        # 只生成第 3 课
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from deck import build  # noqa: E402
from lessons import ALL  # noqa: E402

OUT = HERE / "pdf"


def main(argv: list[str]) -> int:
    wanted = [int(a) for a in argv if a.isdigit()] or None
    for lesson in ALL:
        if wanted and lesson.number not in wanted:
            continue
        name = f"lesson-{lesson.number}.pdf"
        path = build(lesson, OUT / name)
        size = path.stat().st_size / 1024
        print(f"  第 {lesson.number} 课  {lesson.title:<26} -> pdf/{name}  ({size:.0f} KB)")
    print(f"\n输出目录: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
