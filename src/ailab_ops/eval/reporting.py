"""评测产物的落盘。

把用例清单和报告一起写出去。没有用例清单，报告就不可复跑、不可比对，
因为计算它所用的样本已经没了。
"""

from __future__ import annotations

from .models import EvalReport
from pathlib import Path
from typing import Any
from typing import Sequence
import json

def write_report(report: EvalReport, path: str | Path) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)

def write_cases(results_path: Path, cases: Sequence[dict[str, Any]]) -> None:
    """Persist the case list next to the report.

    Without this, a report cannot be re-run or compared, because the sample it
    was computed over is gone.
    """
    with (results_path.parent / "eval_cases.jsonl").open("w", encoding="utf-8") as fh:
        for c in cases:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
