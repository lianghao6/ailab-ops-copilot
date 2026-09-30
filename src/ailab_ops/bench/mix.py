"""构造压测问题集。

`hotkey_ratio` 模拟运维助手真实的流量形状：发生事故时，所有人都在问同一个 job。
`unique=True` 则故意让缓存失效，使被测量的对象是系统本身而不是缓存。
"""

from __future__ import annotations

from typing import Sequence

def build_question_mix(
    job_ids: Sequence[str], hotkey_ratio: float = 0.0, unique: bool = False, n: int = 0
) -> list[str]:
    """Question templates over the given job ids.

    `hotkey_ratio` models the real traffic shape of an operations assistant:
    during an incident, everybody asks about the same job. That is why the
    semantic cache matters more here than in a general-purpose chat product.

    `unique=True` defeats the cache on purpose by appending a per-request
    nonce. Without it, any scenario that repeats its question list is measuring
    the cache, not the component it claims to be measuring -- a mistake that is
    easy to make and produces a very flattering, entirely meaningless result.
    """
    if not job_ids:
        return ["Why did the last job fail?"]
    qs: list[str] = []
    for jid in job_ids:
        qs.append(f"Why did {jid} fail? Give me the root cause and what to do about it.")
        qs.append(f"Diagnose {jid}. What is the root cause?")
    if hotkey_ratio > 0:
        hot = job_ids[0]
        n_hot = int(len(qs) * hotkey_ratio / max(1 - hotkey_ratio, 1e-9))
        qs.extend([f"Diagnose {hot}. What is the root cause?"] * max(n_hot, 0))
    if unique:
        want = max(n, len(qs))
        out: list[str] = []
        for i in range(want):
            base = qs[i % len(qs)]
            out.append(f"{base} (investigation {i})")
        return out
    return qs
