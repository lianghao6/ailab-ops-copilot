"""Bounded lexical access to independently authored V2 Runbooks only."""

from pathlib import Path
import re

from ailab_ops.config import PROJECT_ROOT
from ailab_ops.evidence import Evidence
from .registry import Tool, ToolResult


def _terms(text):
    stopwords = {"a", "an", "the", "in", "on", "at", "to", "for", "and", "or", "of", "is", "with", "from"}
    return set(re.findall(r"[a-z0-9]+|[一-鿿]+", text.casefold())) - stopwords


def build_runbook_tool(root: Path | None = None) -> Tool:
    directory = Path(root) if root is not None else PROJECT_ROOT / "data/v2/knowledge/runbooks"
    documents = []
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ") if text else path.stem
        documents.append((path.name, title, text, _terms(title), _terms(text)))

    def search(query: str, top_k: int = 3):
        terms = _terms(query)
        ranked = []
        for filename, title, text, title_terms, body_terms in documents:
            score = len(terms & body_terms) + 2 * len(terms & title_terms)
            if score:
                ranked.append((score, filename, title, text))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        matches, evidence = [], []
        for score, filename, title, text in ranked[:top_k]:
            excerpt = text[:4000]
            metadata = {"doc_id": "runbook:" + Path(filename).stem,
                        "source": "data/v2/knowledge/runbooks/" + filename,
                        "line_start": 1, "line_end": len(excerpt.splitlines())}
            truncated = len(excerpt) < len(text)
            matches.append({**metadata, "title": title, "excerpt": excerpt,
                            "score": score, "truncated": truncated})
            evidence.append(Evidence("search_runbooks", {"query": query, "top_k": top_k},
                "Runbook: " + title, excerpt, metadata=metadata, truncated=truncated))
        return ToolResult(True, data={"query": query, "matches": matches, "total_matches": len(ranked)},
                          evidence_items=evidence, truncated=any(item.truncated for item in evidence))

    return Tool("search_runbooks", "Search independent V2 Runbooks using observed errors or metric signals; returns citable sources",
        {"type": "object", "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 1000},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 5}},
         "required": ["query"], "additionalProperties": False}, search, simulated_latency_ms=0)
