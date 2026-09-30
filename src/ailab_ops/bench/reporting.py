"""压测报告的精简与落盘。

只保留报告真正需要的字段。完整的服务端快照很大且在各场景之间几乎不变，
原样嵌进去会让报告不可读，还会盖掉真正有差异的那几个数字。
"""

from __future__ import annotations

from .models import BenchReport
from pathlib import Path
from typing import Any
from typing import Sequence
import json

def _slim(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Keep only what a report needs from a server snapshot.

    The full stats payload is large and mostly invariant between scenarios;
    embedding it verbatim would make the report unreadable and would hide the
    three or four numbers that actually differ.
    """
    if not snapshot:
        return {}
    return {
        "gate": snapshot.get("gate"),
        "cache": snapshot.get("cache"),
        "limits": {
            "rejections": (snapshot.get("limits") or {}).get("rejections"),
        },
        "sessions": snapshot.get("sessions"),
        "cost": (snapshot.get("metrics") or {}).get("cost"),
        "counters": {
            k: v
            for k, v in ((snapshot.get("metrics") or {}).get("counters") or {}).items()
            if k.startswith(("requests.", "cache.", "gate.", "degrade.", "rejected."))
        },
    }

def write_bench_report(reports: Sequence[BenchReport], path: str | Path) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([r.to_dict() for r in reports], ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)
