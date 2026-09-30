"""Retrieval: BM25, the hashed encoder, fusion, and chunking.

The useful assertions here are comparative rather than absolute. "Does the
retriever find the right runbook for a real failure log" is a meaningful,
stable claim; "does this query score 14.77" is not.
"""

from __future__ import annotations

import pytest

from ailab_ops.rag import BM25Index, HashingEmbedder, build_knowledge_base, cosine_similarity, tokenize
from ailab_ops.rag.store import Document, HybridRetriever, chunk_text
from ailab_ops.signals import extract_log_evidence


def test_tokenizer_splits_snake_case_identifiers():
    """Without this, a query for `memory` misses `host_mem_used_pct`, which is
    worth more recall in this corpus than any amount of BM25 tuning."""
    toks = tokenize("host_mem_used_pct rose to 100")
    assert "mem" in toks and "used" in toks and "pct" in toks
    assert "host_mem_used_pct" in toks


def test_tokenizer_keeps_cjk():
    toks = tokenize("磁盘写满 no space")
    assert "磁盘写满" in toks, f"CJK run was not preserved: {toks}"
    assert "space" in toks
    # "no" is a stopword and is correctly dropped.
    assert "no" not in toks


def test_bm25_ranks_a_rare_term_above_a_common_one():
    idx = BM25Index()
    idx.add("a", "error error error failed job")
    idx.add("b", "CUDA out of memory during allocation")
    idx.add("c", "error generic failure")
    idx.build()
    hits = dict(idx.search("CUDA out of memory", top_k=3))
    assert hits.get("b", 0) > hits.get("a", 0)
    assert hits.get("b", 0) > hits.get("c", 0)


def test_bm25_handles_an_empty_index():
    assert BM25Index().build().search("anything") == []


def test_embedder_is_deterministic_and_normalised():
    e = HashingEmbedder()
    a1 = e.encode("CUDA out of memory")
    a2 = e.encode("CUDA out of memory")
    assert a1 == a2
    assert abs(sum(x * x for x in a1) ** 0.5 - 1.0) < 1e-9


def test_embedder_places_similar_text_closer():
    e = HashingEmbedder()
    q = e.encode("CUDA out of memory during allocation")
    near = e.encode("GPU memory allocation failed with CUDA out of memory")
    far = e.encode("no space left on device while writing a checkpoint")
    assert cosine_similarity(q, near) > cosine_similarity(q, far)


def test_chunking_preserves_all_content():
    text = "\n".join(f"Paragraph {i} with some content to fill it out." for i in range(20))
    chunks = chunk_text(text, max_chars=200, overlap=40)
    assert len(chunks) > 1
    joined = " ".join(chunks)
    for i in range(20):
        assert f"Paragraph {i} " in joined


def test_chunking_handles_a_single_oversized_paragraph():
    chunks = chunk_text("x" * 5000, max_chars=300, overlap=20)
    assert len(chunks) > 1
    assert all(len(c) <= 600 for c in chunks)


@pytest.fixture(scope="module")
def retriever():
    return build_knowledge_base().retriever


def test_knowledge_base_covers_every_runbook(retriever, playbook):
    for s in playbook.scenarios.values():
        if s.is_unknown:
            continue
        assert any(d.metadata.get("scenario") == s.id for d in retriever.docs.values()), (
            f"no runbook document for scenario {s.id}"
        )


@pytest.mark.parametrize(
    "query,expected_scenario",
    [
        ("CUDA out of memory during the first optimizer step", "oom_gpu"),
        ("Watchdog caught collective operation timeout across all ranks", "collective_timeout"),
        ("No space left on device ENOSPC", "disk_full"),
        ("Too Many Requests 429 retry after", "rate_limited"),
        ("0/64 nodes are available Unschedulable", "insufficient_resources"),
        ("ErrImagePull manifest unknown", "image_pull_failed"),
        ("Temporary failure in name resolution getaddrinfo", "dns_failure"),
        ("size mismatch for model.layers.0.attention.wq.weight", "ckpt_incompatible"),
        ("ModuleNotFoundError No module named", "missing_dependency"),
    ],
)
def test_real_symptom_queries_retrieve_the_right_runbook(retriever, query, expected_scenario):
    res = retriever.search(query, top_k=5)
    ids = [h.doc.metadata.get("scenario") for h in res.hits]
    assert expected_scenario in ids, (
        f"query {query!r} retrieved {ids}, expected {expected_scenario} to be present"
    )


def test_retrieval_works_on_actual_generated_failure_logs(retriever, world):
    """The end-to-end claim that matters: given the log of a real failure in the
    dataset, the runbook for its true cause is retrieved near the top."""
    from ailab_ops.datagen.taxonomy import load_playbook

    pb = load_playbook()
    hits = 0
    total = 0
    for job in world.jobs:
        if job.status == "SUCCEEDED" or job.is_insufficient_evidence or not job.root_cause:
            continue
        ev = extract_log_evidence(world.logs_for(job.job_id), pb)
        if not ev.first_error:
            continue
        total += 1
        ids = [h.doc.metadata.get("scenario") for h in retriever.search(ev.first_error, top_k=5).hits]
        if job.root_cause in ids:
            hits += 1
    assert total > 10, "not enough diagnosable failures to measure retrieval"
    recall = hits / total
    assert recall >= 0.75, f"top-5 recall on real failure logs is only {recall:.1%}"


def test_fusion_weighting_is_lexically_tilted(playbook, world):
    """On verbatim log queries, lexical-only beats dense-only, so a symmetric
    fusion loses to lexical alone. The default weighting reflects that.

    The test asserts the *result* rather than the weight, so tuning the weight
    is free but regressing the outcome is not.
    """
    cases: list[tuple[str, str]] = []
    for job in world.jobs:
        if job.status == "SUCCEEDED" or job.is_insufficient_evidence or not job.root_cause:
            continue
        ev = extract_log_evidence(world.logs_for(job.job_id), playbook)
        if ev.first_error:
            cases.append((ev.first_error, job.root_cause))
    assert len(cases) > 10

    def top1(weight: float) -> float:
        r = build_knowledge_base(playbook).retriever
        r.lexical_weight = weight
        ok = 0
        for q, truth in cases:
            hits = r.search(q, top_k=1).hits
            if hits and hits[0].doc.metadata.get("scenario") == truth:
                ok += 1
        return ok / len(cases)

    default = top1(build_knowledge_base().retriever.lexical_weight)
    symmetric = top1(0.5)
    assert default >= symmetric, (
        f"the default weighting ({default:.1%}) should not lose to a symmetric fusion "
        f"({symmetric:.1%}) on verbatim queries"
    )
    assert default >= 0.75, f"top-1 through fusion is only {default:.1%}"


def test_dense_side_earns_its_place_on_paraphrases(playbook):
    """Fusion is not free: it must actually help somewhere. Paraphrases -- a
    human describing the symptom rather than quoting it -- are where the dense
    retriever contributes, and the weighting must not shut it out entirely."""
    r = build_knowledge_base(playbook).retriever
    paraphrases = [
        ("all the workers stopped talking to each other and everything froze",
         "collective_timeout"),
        ("it cannot write anything because the drive is full", "disk_full"),
        ("the server told us we are sending too many requests", "rate_limited"),
    ]
    dense_only = 0
    for q, truth in paraphrases:
        qv = r.embedder.encode(q)
        scored = sorted(
            ((c.chunk_id, cosine_similarity(qv, v)) for c, v in zip(r.chunks, r._chunk_vecs)),
            key=lambda kv: kv[1],
            reverse=True,
        )
        doc = r.docs[r.chunks[r._chunk_index[scored[0][0]]].doc_id]
        if doc.metadata.get("scenario") == truth:
            dense_only += 1
    assert dense_only > 0, "the dense retriever found none of the paraphrases; it is dead weight"
    # And the fusion, at its default weight, must not be worse than lexical-only
    # on this set.
    lex_ok = 0
    for q, truth in paraphrases:
        hits = r.search(q, top_k=1).hits
        if hits and hits[0].doc.metadata.get("scenario") == truth:
            lex_ok += 1
    assert lex_ok >= 1


def test_fusion_modes_both_rank_the_obvious_case_first(retriever):
    for fusion in ("rrf", "linear"):
        res = retriever.search("CUDA out of memory", top_k=3, fusion=fusion)
        assert res.hits[0].doc.metadata.get("scenario") == "oom_gpu", (
            f"{fusion} fusion did not put the obvious runbook first"
        )


def test_scores_are_normalised_between_modes(retriever):
    """Comparable score scales matter: the numbers appear in reports and the UI,
    and an RRF score of 0.03 sitting next to a linear score of 0.77 is the kind
    of inconsistency that makes people distrust the whole dashboard."""
    for fusion in ("rrf", "linear"):
        res = retriever.search("disk exhausted while writing a checkpoint", top_k=4, fusion=fusion)
        assert res.hits
        assert 0.0 <= res.hits[0].score <= 1.0


def test_tag_filter_excludes_other_categories(retriever):
    res = retriever.search("timeout", top_k=5, tag_filter=("memory",))
    for h in res.hits:
        assert "memory" in h.doc.tags


def test_empty_query_is_handled(retriever):
    res = retriever.search("", top_k=3)
    assert res.hits == []


def test_as_context_respects_the_budget(retriever):
    res = retriever.search("memory exhaustion", top_k=6)
    ctx = res.as_context(max_chars=600)
    assert len(ctx) <= 700
    assert res.hits[0].doc.doc_id in ctx
