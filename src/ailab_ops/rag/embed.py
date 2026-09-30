"""零依赖的"向量"编码器。

为什么不用真实 embedding 模型？两个理由，都是刻意的：

1. **离线可复现。** 一个必须先下载模型才能启动的项目，在受限网络环境下直接跑不
   起来，而且模型一升级输出就变。本编码器是文本与种子的纯函数。
2. **这一层要讲的是混合检索本身。** 重点在于如何把词法分数与向量分数结合、如何
   调融合权重、以及向量检索器为什么会漏掉 BM25 一下就能命中的精确错误串。用哈希
   编码器完全能承载这堂课；下面留出的可插拔接口，就是换成真实模型的扩展点。

具体做法是特征哈希（hashing trick）：用稳定哈希把每个 token 映射到固定宽度的
稠密向量上，按次线性词频加权，再做 L2 归一化。它的行为等价于一个固定占用空间的
词袋向量空间模型，对融合这一课来说正好够用。
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Protocol, Sequence

from .bm25 import tokenize

# Character n-grams complement word tokens: they give partial credit for
# morphological variants ("timeout" / "timeouts" / "timed out") without any
# stemming logic, and they are what lets the dense side recover some of the
# near-miss cases where BM25 is brittle about exact wording.
_NGRAM_N = 4


class Embedder(Protocol):
    """Interface a real embedding model would implement."""

    dim: int

    def encode(self, text: str) -> list[float]: ...

    def encode_batch(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass
class HashingEmbedder:
    """Deterministic feature-hashing encoder.

    `dim` controls the collision rate: at 256 dimensions with this vocabulary
    size, collisions are frequent enough to be visible in tests (which is
    useful -- it motivates the hybrid design), yet the ranking stays stable.
    """

    dim: int = 256
    seed: int = 17
    use_ngrams: bool = True
    word_weight: float = 1.0
    ngram_weight: float = 0.45

    def _features(self, text: str) -> Counter:
        toks = tokenize(text)
        feats: Counter = Counter()
        if self.use_ngrams:
            # n-grams over the *token stream* rather than raw characters: it is
            # cheaper and keeps the features interpretable.
            joined = "\x00".join(toks)
            for i in range(len(joined) - _NGRAM_N + 1):
                feats["g:" + joined[i : i + _NGRAM_N]] += self.ngram_weight
        for t in toks:
            feats["w:" + t] += self.word_weight
        return feats

    def _bucket(self, feature: str) -> tuple[int, float]:
        """Stable hash -> (index, sign). The sign keeps collisions from always
        inflating a coordinate, which is standard practice in hashing encoders."""
        h = hashlib.blake2b(
            feature.encode("utf-8"), digest_size=8, key=str(self.seed).encode("ascii")
        ).digest()
        raw = int.from_bytes(h, "big")
        return raw % self.dim, (1.0 if (raw >> 63) & 1 else -1.0)

    def encode(self, text: str) -> list[float]:
        feats = self._features(text)
        vec = [0.0] * self.dim
        for feature, count in feats.items():
            idx, sign = self._bucket(feature)
            # Sublinear tf: a term repeated 50 times is not 50x as important.
            vec[idx] += sign * (1.0 + math.log(count)) if count > 0 else 0.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def encode_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.encode(t) for t in texts]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity. Inputs from `HashingEmbedder` are already unit-norm,
    so this is usually just a dot product, but the general form is kept since
    a real embedder may not normalise."""
    if not a or not b:
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


def l2_normalise(vec: Iterable[float]) -> list[float]:
    v = list(vec)
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v] if norm > 0 else v
