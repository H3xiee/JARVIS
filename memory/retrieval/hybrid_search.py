"""Step 2 of retrieval: gather candidates from every method and merge their signals.

Exact phrase · keyword (BM25) · vector (meaning) · metadata (the entities
asked about) · graph (one hop through stored relationships) · temporal (the
time window asked about) · kind (the memory type the question is about).
"""

from __future__ import annotations

from typing import Dict, Optional

from memory.retrieval import keyword_search
from memory.retrieval.memory_ranker import Signals, rescale_keyword, rescale_vector

# (floor, ceiling) cosine per embedder: below floor = unrelated, at ceiling = a clear match
VECTOR_SCALE = {"minilm-l6-v2": (0.22, 0.52), "hash-v2": (0.15, 0.55)}


def gather(repo, vindex, qa, k: int = 8, filters: Optional[dict] = None, lexical=None) -> Dict[str, Signals]:
    filters = dict(filters or {})
    status = ["active", "superseded"] if qa.historical else "active"
    filters.setdefault("status", status)
    pool = max(20, k * 4)
    cands: Dict[str, Signals] = {}

    def sig(mid: str) -> Signals:
        return cands.setdefault(mid, Signals())

    # exact phrases (quoted text, and the names of the entities asked about)
    for mid in keyword_search.exact(repo, qa.phrases, limit=pool, **filters):
        sig(mid).exact = True

    # keyword
    kw = dict(keyword_search.keyword(repo, qa.text, limit=pool, **filters))
    for mid, v in rescale_keyword(kw).items():
        sig(mid).keyword = v

    # vector
    if vindex is not None:
        floor, ceil = VECTOR_SCALE.get(vindex.model, (0.2, 0.6))
        for mid, cos in vindex.search(qa.text, limit=pool, min_score=floor, **filters):
            sig(mid).vector = rescale_vector(cos, floor, ceil)

    # spelling-tolerant (character n-grams): finds "reconect gogle" when keywords can't
    if lexical is not None:
        floor, ceil = VECTOR_SCALE.get(lexical.model, (0.15, 0.55))
        for mid, cos in lexical.search(qa.text, limit=pool, min_score=floor, **filters):
            sig(mid).fuzzy = rescale_vector(cos, floor, ceil)

    # metadata: memories filed under the entities mentioned
    entity_ids = [e["entity_id"] for e in qa.entities]
    for eid in entity_ids:
        for rec in repo.find(entity_id=eid, limit=pool, valid_at=qa.valid_at, **filters):
            sig(rec.memory_id).entity = True

    # graph: one hop through relationships backed by stored facts
    for eid in entity_ids:
        for rel in repo.relations(eid, valid_at=qa.valid_at):
            if rel.get("memory_id"):
                sig(rel["memory_id"]).graph = True
            other = rel["dst_entity"] if rel["src_entity"] == eid else rel["src_entity"]
            for rec in repo.find(entity_id=other, limit=5, **filters):
                s = sig(rec.memory_id)
                s.graph = True

    # temporal: what happened in the window asked about
    if qa.since or qa.until:
        for rec in repo.find(since=qa.since, until=qa.until, limit=pool, order="created",
                             **{k2: v for k2, v in filters.items() if k2 != "status"}):
            if rec.type in ("episodic", "prospective") or (qa.since and rec.created_at >= qa.since):
                sig(rec.memory_id).temporal = True

    # entity-only candidates must also relate to the rest of the request when there is more to it
    return cands
