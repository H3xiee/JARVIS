"""Exact and keyword retrieval (SQLite FTS5 / phrase match) as ranked candidate lists."""

from __future__ import annotations

from typing import List, Tuple


def keyword(repo, query: str, limit: int = 30, **filters) -> List[Tuple[str, float]]:
    """[(memory_id, bm25 score)] best first."""
    return [(r.memory_id, r.data.get("_score", 0.0)) for r in repo.search_text(query, limit=limit, **filters)]


def exact(repo, phrases, limit: int = 20, **filters) -> List[str]:
    out: List[str] = []
    for p in phrases:
        if p and len(p.strip()) >= 3:
            out += [r.memory_id for r in repo.find_exact(p.strip(), limit=limit, **filters) if r.memory_id not in out]
    return out
