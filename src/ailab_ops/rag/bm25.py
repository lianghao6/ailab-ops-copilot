"""BM25 with field weighting (BM25F-lite).

这里手写而不用现成库，是为了让 RAG 流水线里每一处会悄悄丢召回的地方都摆在
明面上。具体来说：

* tokenisation decides whether `gpu_mem_used_pct` matches the query term
  `gpu mem used pct`;
* the IDF term is what makes a rare signal (an exact error string) outrank a
  common one (the word "error");
* field weighting is what makes a document findable by its *headings and exact
  strings* rather than only by the volume of prose that happens to mention the
  same words. Without it, a long runbook that discusses a subject several times
  outranks a short one whose title is the exact symptom, which is backwards;
* `k1` and `b` control how much a repeated term helps and how much long
  documents are penalised.

The field weights are a judgement, not a tuning result, and they are declared
as a constant so a class can change them and watch retrieval move.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping

# Split on anything that is not a letter, digit or CJK char; keep CJK runs
# intact since the docs may contain Chinese section titles.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[一-鿿]+")

# Tokens that carry no discrimination in this domain. Kept short on purpose:
# aggressive stopword lists hurt more than they help in a technical corpus.
_STOPWORDS = frozenset(
    """
    a an the and or but if then than that this these those is are was were be been being
    of in on at to for with from by as it its into over after before during not no
    do does did done can could should would will shall may might must have has had
    """.split()
)

# Per-field multipliers. `signature` is the "typical log evidence" line -- the
# exact strings the failure prints -- and it carries the most weight because a
# query in this domain is usually a verbatim log fragment. `title` comes second
# because runbook titles are written as symptoms. `body` is the explanation and
# is deliberately the weakest: it is the longest field and the most likely to
# mention a subject in passing.
DEFAULT_FIELD_WEIGHTS: Mapping[str, float] = {
    "title": 3.0,
    "signature": 4.0,
    "body": 1.0,
}


def tokenize(text: str) -> list[str]:
    """Lowercase, then split into word and CJK tokens.

    Also emits the sub-parts of snake_case identifiers, so a query for
    `memory` matches a document containing `host_mem_used_pct`. This single
    decision is worth more recall in this corpus than any amount of BM25
    parameter tuning.
    """
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        out.append(raw)
        if "_" in raw:
            out.extend(p for p in raw.split("_") if p)
    return [t for t in out if t not in _STOPWORDS and len(t) > 0]


@dataclass
class BM25Index:
    """Okapi BM25 over a fixed collection, with optional per-field weighting.

    Fields are scored separately and summed, which is the standard
    approximation to BM25F: it ignores that the same term appearing in two
    fields of one document is one piece of evidence rather than two, and that
    approximation is more than good enough here. Being explicit about the
    approximation is the point -- it is exactly the kind of simplification a
    practitioner should be able to name.
    """

    k1: float = 1.5
    b: float = 0.75
    field_weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_FIELD_WEIGHTS))

    _doc_ids: list[str] = field(default_factory=list)
    _fields: list[dict[str, Counter]] = field(default_factory=list)
    _doc_len: list[int] = field(default_factory=list)
    _df: Counter = field(default_factory=Counter)
    _avgdl: float = 0.0

    def add(self, doc_id: str, text: str, **fields: str) -> None:
        """Index a document.

        `text` is the combined form used for length normalisation; named fields
        (`title=`, `signature=`) are scored with their own weights.
        """
        per_field: dict[str, Counter] = {}
        combined: Counter = Counter()
        total_len = 0
        for name, value in {"body": text, **fields}.items():
            if not value:
                continue
            toks = tokenize(value)
            if not toks:
                continue
            per_field[name] = Counter(toks)
            combined.update(per_field[name])
            total_len += len(toks)
        if not per_field:
            # An empty document still needs a slot, or the indices desynchronise.
            per_field["body"] = Counter()
        self._doc_ids.append(doc_id)
        self._fields.append(per_field)
        self._doc_len.append(total_len or 1)
        for term in combined:
            self._df[term] += 1

    def build(self) -> "BM25Index":
        n = len(self._doc_ids)
        self._avgdl = (sum(self._doc_len) / n) if n else 0.0
        return self

    def __len__(self) -> int:
        return len(self._doc_ids)

    def _idf(self, term: str) -> float:
        n = len(self._doc_ids)
        df = self._df.get(term, 0)
        if df == 0:
            return 0.0
        # BM25+ style floor: a term present in every document still gets a
        # small positive weight instead of going negative.
        return max(math.log((n - df + 0.5) / (df + 0.5) + 1.0), 0.0)

    def _term_score(self, term: str, i: int) -> float:
        fields = self._fields[i]
        dl = self._doc_len[i] or 1
        norm = (1.0 - self.b + self.b * dl / (self._avgdl or 1.0))
        score = 0.0
        for field_name, tf in fields.items():
            f = tf.get(term, 0)
            if not f:
                continue
            w = self.field_weights.get(field_name, 1.0)
            score += w * (f * (self.k1 + 1.0)) / (f + self.k1 * norm)
        return score

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """Return (doc_id, score) sorted by descending score."""
        if not self._doc_ids:
            return []
        q_terms = tokenize(query)
        if not q_terms:
            return []

        scores = [0.0] * len(self._doc_ids)
        for term in set(q_terms):
            idf = self._idf(term)
            if idf <= 0.0:
                continue
            for i in range(len(self._doc_ids)):
                s = self._term_score(term, i)
                if s:
                    scores[i] += idf * s

        ranked = sorted(
            ((self._doc_ids[i], s) for i, s in enumerate(scores) if s > 0.0),
            key=lambda kv: kv[1],
            reverse=True,
        )
        return ranked[:top_k]

    def explain(self, query: str, top_k: int = 5) -> list[dict]:
        """Per-term contribution for the top documents.

        用来回答"这篇文档凭什么排第一"——通常是"因为查询里的稀有词在它标题里
        出现了三次"。把数字摆出来，比一句结论有说服力得多。
        """
        q_terms = sorted(set(tokenize(query)))
        out = []
        for doc_id, score in self.search(query, top_k=top_k):
            i = self._doc_ids.index(doc_id)
            contrib = {}
            for t in q_terms:
                idf = self._idf(t)
                if idf > 0:
                    s = self._term_score(t, i)
                    if s:
                        contrib[t] = round(idf * s, 3)
            out.append({"doc_id": doc_id, "score": round(score, 3), "terms": contrib})
        return out
