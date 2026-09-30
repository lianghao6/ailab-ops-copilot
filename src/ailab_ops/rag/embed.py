"""A dependency-free "dense" encoder.

Why not a real embedding model? Two reasons, both deliberate:

1. **Offline reproducibility.** A teaching project that requires downloading a
   model before it will start is a project that breaks in a locked-down
   environment, and whose output changes when the model is updated. This
   encoder is a pure function of the text and the seed.
2. **Honesty about what is being taught.** The lesson here is *hybrid
   retrieval* -- how to combine a lexical score with a vector score, how to
   tune the fusion, how a dense retriever can miss an exact error string that
   BM25 nails. That lesson is fully present with a hashed encoder, and the
   pluggable interface below is the extension point for a real model.

The technique is feature hashing (a.k.a. the hashing trick): map every token
to a fixed-width bag of dimensions via a stable hash, weight by sublinear term
frequency, then L2-normalise. It behaves like a bag-of-words vector space
model with a fixed footprint, which is exactly enough for the fusion lesson.
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
