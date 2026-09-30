"""把模型的最终消息解析成结构化诊断。

解析刻意宽容：模型会把 JSON 包在散文里、包在代码块里、或者在 JSON 后面再补一段
评论。解析失败不是硬错误——调用方仍然拿得到原文——但解析不了的运行会在评测里
单独计数，因为「答案对了但输出不可用」是一种真实且可修的失败。
"""

from __future__ import annotations

from typing import Any
import json
import re

_PARSE_FAILED = object()

def parse_answer(answer: str) -> dict[str, Any] | None:
    """Extract the structured diagnosis from a model's final message.

    Tolerant by design: models wrap JSON in prose, in a fenced block, or emit a
    JSON object followed by commentary. Failing to parse is not a hard error --
    the caller still has the prose -- but a run that cannot be parsed is
    counted separately in the evaluation, because "the model was right but the
    output was unusable" is a real and fixable failure mode.

    Returned keys are normalised so a model that says `cause` and one that says
    `root_cause` are scored the same.
    """
    if not answer:
        return None
    text = answer.strip()

    candidates: list[str] = []
    for m in re.finditer(r"```(?:json)?\s*(.+?)```", text, re.DOTALL):
        candidates.append(m.group(1).strip())
    candidates.append(text)
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first : last + 1])

    for chunk in candidates:
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return _normalise(obj)

    # Last resort: a bare label, which models do emit when a schema is implied.
    m = re.search(r'"?(root_cause|cause|diagnosis)"?\s*[:=]\s*"?([a-z_0-9]+)"?', text)
    if m:
        return {"root_cause": m.group(2).lower(), "confidence": None, "summary": text[:400],
                "_parsed_from": "regex"}
    return None

def _normalise(obj: dict[str, Any]) -> dict[str, Any]:
    def pick(*keys: str) -> Any:
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        return None

    rc = pick("root_cause", "cause", "diagnosis", "label", "rootCause")
    if isinstance(rc, str):
        rc = rc.strip().lower().replace(" ", "_").replace("-", "_")
    conf = pick("confidence", "certainty", "score")
    try:
        conf = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf = None
    if conf is not None and conf > 1.0:  # some models answer 0-100
        conf = conf / 100.0

    def as_list(v: Any) -> list[Any]:
        if v is None:
            return []
        if isinstance(v, list):
            return v
        return [v]

    return {
        "root_cause": rc,
        "confidence": conf,
        "summary": pick("summary", "explanation", "analysis") or "",
        "evidence": as_list(pick("evidence", "evidence_lines", "signals")),
        "ruled_out": as_list(pick("ruled_out", "ruledOut", "alternatives", "differential")),
        "remediation": as_list(pick("remediation", "fix", "actions", "recommendation")),
        "citations": as_list(pick("citations", "sources", "references")),
        "job_id": pick("job_id", "jobId"),
        "_extra": {k: v for k, v in obj.items() if k not in {
            "root_cause", "cause", "diagnosis", "label", "rootCause", "confidence", "certainty",
            "score", "summary", "explanation", "analysis", "evidence", "evidence_lines", "signals",
            "ruled_out", "ruledOut", "alternatives", "differential", "remediation", "fix",
            "actions", "recommendation", "citations", "sources", "references", "job_id", "jobId",
        }},
    }
