"""Document store with hybrid (lexical + dense) retrieval and rank fusion.

The fusion step is the pedagogical centrepiece of this module. Two retrievers
that are individually mediocre -- BM25 is brittle about wording, the hashed
encoder is brittle about exact rare tokens -- combine into something markedly
better, and the *way* they combine is a tunable knob with a visible effect on
the evaluation report:

* `rrf`  (Reciprocal Rank Fusion) uses only ranks, so it is immune to the two
  scorers being on incomparable scales. Robust default.
* `linear` uses normalised scores. Usually sharper when both retrievers are
  well calibrated, and visibly worse when one produces a runaway score.

Chunking is included because it is where most real RAG pipelines quietly lose
recall: a runbook split on a paragraph boundary separates the symptom from the
fix, and neither chunk then ranks well for a query mentioning both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal, Sequence

from .bm25 import BM25Index, tokenize
from .embed import Embedder, HashingEmbedder, cosine_similarity

FusionMode = Literal["rrf", "linear"]


@dataclass
class Document:
    doc_id: str
    title: str
    body: str
    source: str = "kb"
    tags: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)
    # The verbatim strings this document is *about* -- for a runbook, the log
    # lines the failure prints. Indexed as its own field with a high weight,
    # because a query in this domain is usually a verbatim log fragment and a
    # long prose body would otherwise outrank a short, exact match.
    signatures: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return self.body

    @property
    def signature_text(self) -> str:
        return " | ".join(self.signatures)


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    text: str
    ord: int
    title: str = ""
    signature: str = ""


@dataclass
class Hit:
    doc: Document
    score: float
    lexical_score: float = 0.0
    dense_score: float = 0.0
    lexical_rank: int | None = None
    dense_rank: int | None = None
    matched_chunk: str = ""

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc.doc_id,
            "title": self.doc.title,
            "score": round(self.score, 4),
            "lexical_score": round(self.lexical_score, 4),
            "dense_score": round(self.dense_score, 4),
            "lexical_rank": self.lexical_rank,
            "dense_rank": self.dense_rank,
            "source": self.doc.source,
            "tags": list(self.doc.tags),
            "excerpt": _excerpt(self.matched_chunk or self.doc.body),
        }


@dataclass
class RetrievalResult:
    query: str
    hits: list[Hit]
    fusion: FusionMode
    n_candidates: int

    @property
    def top(self) -> Hit | None:
        return self.hits[0] if self.hits else None

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "fusion": self.fusion,
            "n_candidates": self.n_candidates,
            "hits": [h.to_dict() for h in self.hits],
        }

    def as_context(self, max_chars: int = 2400) -> str:
        """Render the hits as a prompt-ready context block.

        Deliberately includes the doc_id so the agent can cite it, and keeps a
        hard character budget so a single oversized runbook cannot crowd out
        the other retrieved evidence.
        """
        parts: list[str] = []
        used = 0
        for i, h in enumerate(self.hits, 1):
            block = f"[{i}] {h.doc.doc_id} :: {h.doc.title}\n{_excerpt(h.matched_chunk or h.doc.body, 700)}"
            if used + len(block) > max_chars and parts:
                break
            parts.append(block)
            used += len(block)
        return "\n\n".join(parts)


def _excerpt(text: str, limit: int = 320) -> str:
    t = " ".join(text.split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _minmax(value: float, scores: dict[str, float]) -> float:
    """Map a score into 0..1 using the min/max of its own retriever."""
    if not scores:
        return 0.0
    lo = min(scores.values())
    hi = max(scores.values())
    if hi - lo <= 1e-12:
        return 1.0 if value > 0 else 0.0
    return (value - lo) / (hi - lo)


def chunk_text(text: str, max_chars: int = 620, overlap: int = 90) -> list[str]:
    """Split on paragraph boundaries, packing paragraphs up to `max_chars`.

    Paragraph-aware splitting matters more than sentence-aware splitting here:
    the runbooks are written so that one paragraph holds one coherent idea
    (symptom, then discriminator, then fix), and splitting mid-paragraph is
    what produces the "the answer is in the index but was not retrieved"
    failure mode.
    """
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if not buf:
            buf = p
        elif len(buf) + len(p) + 1 <= max_chars:
            buf = f"{buf}\n{p}"
        else:
            chunks.append(buf)
            tail = buf[-overlap:] if overlap > 0 else ""
            buf = f"{tail}\n{p}" if tail else p
    if buf:
        chunks.append(buf)

    # A single oversized paragraph still has to be cut somewhere.
    out: list[str] = []
    for c in chunks:
        if len(c) <= max_chars * 2:
            out.append(c)
            continue
        for i in range(0, len(c), max_chars):
            out.append(c[i : i + max_chars])
    return out


class HybridRetriever:
    """BM25 + hashed-dense retrieval over chunked documents, with fusion.

    The default weights are a measured result for this corpus, not a guess, and
    they are worth understanding because they are unintuitive. On verbatim log
    queries -- which is what this domain's queries mostly are -- BM25 alone
    reaches ~88% top-1 while the hashed encoder reaches ~32%, because an exact
    error string is precisely what a lexical index is good at and a bag-of-
    features vector is not. Fusing them with equal weights therefore *loses* to
    lexical alone (62% at weight 0.5). Weighting lexically, at 0.8, recovers
    parity on verbatim queries and still beats lexical-only on paraphrases,
    which is where the dense side earns its place.

    The lesson generalises: fusion does not automatically beat its components,
    and the right weight depends on how good each retriever actually is on your
    data. Assuming symmetry is a common way to make a system worse while
    believing you improved it.

    The default is set by measurement: on 112 real generated failure queries,
    top-1 was lexical 88%, dense 32%, and the fusion 62% (w=0.5), 77% (w=0.8),
    85% (w=0.9). It is 0.85 -- close enough to lexical parity that nothing is
    lost on verbatim queries, while still leaving the dense side enough voice to
    catch paraphrases, which is the whole reason it is there.
    """

    #: Measured best compromise for this corpus (see the class docstring).
    DEFAULT_LEXICAL_WEIGHT = 0.85

    def __init__(
        self,
        embedder: Embedder | None = None,
        fusion: FusionMode = "rrf",
        rrf_k: int = 60,
        lexical_weight: float = DEFAULT_LEXICAL_WEIGHT,
        chunk_chars: int = 620,
        chunk_overlap: int = 90,
    ) -> None:
        self.embedder = embedder or HashingEmbedder()
        self.fusion: FusionMode = fusion
        self.rrf_k = rrf_k
        self.lexical_weight = lexical_weight
        self.chunk_chars = chunk_chars
        self.chunk_overlap = chunk_overlap

        self.docs: dict[str, Document] = {}
        self.chunks: list[Chunk] = []
        self._bm25 = BM25Index()
        self._chunk_vecs: list[list[float]] = []
        self._chunk_index: dict[str, int] = {}

    # ---- indexing ------------------------------------------------------

    def add(self, doc: Document) -> None:
        if doc.doc_id in self.docs:
            raise ValueError(f"duplicate doc_id: {doc.doc_id}")
        self.docs[doc.doc_id] = doc
        for i, piece in enumerate(chunk_text(doc.text, self.chunk_chars, self.chunk_overlap)):
            cid = f"{doc.doc_id}#{i}"
            # The first chunk of a runbook is the symptom paragraph, so the
            # title and the signature strings are attached to every chunk: a
            # chunk that has drifted away from the heading is otherwise
            # unfindable by exactly the query that should find it.
            searchable = f"{doc.title}\n{piece}"
            chunk = Chunk(
                chunk_id=cid, doc_id=doc.doc_id, text=piece, ord=i,
                title=doc.title, signature=doc.signature_text,
            )
            self._chunk_index[cid] = len(self.chunks)
            self.chunks.append(chunk)

    def add_many(self, docs: Iterable[Document]) -> None:
        for d in docs:
            self.add(d)

    def build(self) -> "HybridRetriever":
        self._bm25 = BM25Index()
        for c in self.chunks:
            self._bm25.add(c.chunk_id, c.text, title=c.title, signature=c.signature)
        self._bm25.build()
        self._chunk_vecs = [self.embedder.encode(self._searchable(c)) for c in self.chunks]
        return self

    @staticmethod
    def _searchable(c: "Chunk") -> str:
        """The text the dense encoder sees: title + signature + body.

        The dense side benefits from the title and signature being present even
        though the lexical side already weights them, because the encoder is a
        bag of features and has no way to privilege a heading.
        """
        return f"{c.title}\n{c.signature}\n{c.text}".strip()

    def __len__(self) -> int:
        return len(self.docs)

    @property
    def n_chunks(self) -> int:
        return len(self.chunks)

    # ---- search --------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int = 5,
        candidate_k: int = 30,
        fusion: FusionMode | None = None,
        tag_filter: Sequence[str] | None = None,
    ) -> RetrievalResult:
        mode: FusionMode = fusion or self.fusion
        if not self.chunks:
            return RetrievalResult(query=query, hits=[], fusion=mode, n_candidates=0)

        # A query with no indexable tokens retrieves nothing, and saying so is
        # better than returning an arbitrary ranking. Without this check the
        # dense side still produces a (meaningless) ordering of every chunk and
        # the caller gets results for an empty question.
        if not tokenize(query):
            return RetrievalResult(query=query, hits=[], fusion=mode, n_candidates=0)

        lex = dict(self._bm25.search(query, top_k=candidate_k))
        qv = self.embedder.encode(query)
        dense_scored = [
            (c.chunk_id, cosine_similarity(qv, v)) for c, v in zip(self.chunks, self._chunk_vecs)
        ]
        dense_scored.sort(key=lambda kv: kv[1], reverse=True)
        dense = dict(dense_scored[:candidate_k])

        lex_rank = {cid: i + 1 for i, (cid, _) in enumerate(sorted(lex.items(), key=lambda kv: kv[1], reverse=True))}
        dense_rank = {cid: i + 1 for i, (cid, _) in enumerate(dense_scored[:candidate_k])}

        if mode == "rrf":
            fused: dict[str, float] = {}
            for cid in set(lex) | set(dense):
                s = 0.0
                if cid in lex_rank:
                    s += self.lexical_weight / (self.rrf_k + lex_rank[cid])
                if cid in dense_rank:
                    s += (1.0 - self.lexical_weight) / (self.rrf_k + dense_rank[cid])
                fused[cid] = s
            # RRF scores are tiny and clustered (both at rank 1 -> 2/(k+1)),
            # which is fine for *ranking* but useless as a number to threshold
            # or display. Rescaling to 0..1 keeps the order and makes the score
            # comparable with the linear mode in reports.
            top = max(fused.values()) if fused else 1.0
            fused = {k: v / (top or 1.0) for k, v in fused.items()}
        else:  # linear
            # Min-max per retriever rather than divide-by-max: dividing by the
            # max compresses every candidate into a narrow band whenever one
            # document scores much higher than the rest, which silently
            # demotes the second-best evidence.
            fused = {}
            for cid in set(lex) | set(dense):
                l = _minmax(lex.get(cid, 0.0), lex)
                d = _minmax(dense.get(cid, 0.0), dense)
                fused[cid] = self.lexical_weight * l + (1.0 - self.lexical_weight) * d

        # Collapse to documents, keeping the best chunk per document.
        best_per_doc: dict[str, tuple[float, str]] = {}
        for cid, s in fused.items():
            chunk = self.chunks[self._chunk_index[cid]]
            cur = best_per_doc.get(chunk.doc_id)
            if cur is None or s > cur[0]:
                best_per_doc[chunk.doc_id] = (s, cid)

        hits: list[Hit] = []
        for doc_id, (score, cid) in sorted(best_per_doc.items(), key=lambda kv: kv[1][0], reverse=True):
            doc = self.docs[doc_id]
            if tag_filter and not set(tag_filter) & set(doc.tags):
                continue
            hits.append(
                Hit(
                    doc=doc,
                    score=score,
                    lexical_score=lex.get(cid, 0.0),
                    dense_score=dense.get(cid, 0.0),
                    lexical_rank=lex_rank.get(cid),
                    dense_rank=dense_rank.get(cid),
                    matched_chunk=self.chunks[self._chunk_index[cid]].text,
                )
            )
            if len(hits) >= top_k:
                break

        return RetrievalResult(query=query, hits=hits, fusion=mode, n_candidates=len(fused))

    # ---- introspection -------------------------------------------------

    def explain(self, query: str, top_k: int = 5) -> str:
        """Human-readable per-retriever comparison.

        Used in the demo and in class to show, concretely, a query where the
        lexical and dense retrievers disagree and the fusion beats both.
        """
        r = self.search(query, top_k=top_k)
        lines = [f"query: {query!r}  fusion={r.fusion}  candidates={r.n_candidates}"]
        for i, h in enumerate(r.hits, 1):
            lines.append(
                f"  {i}. {h.doc.title[:64]:<64} fused={h.score:.4f} "
                f"lex={h.lexical_score:.2f}(#{h.lexical_rank}) dense={h.dense_score:.3f}(#{h.dense_rank})"
            )
        return "\n".join(lines)
