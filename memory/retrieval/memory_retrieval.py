"""The memory retrieval engine.

    from memory.core.memory_manager import get_memory
    pkg = get_memory().retriever.context("Continue working on the assistant project")
    pkg.text     # compact, source-attributed context for the reasoning loop / an agent

Pipeline:
  1. analyse   intent (continuation, how-to, commitment, preference, history,
               lesson, recall), entities mentioned, time window, quoted phrases
  2. gather    exact · keyword · vector · metadata · graph · temporal candidates
  3. rank      relevance x quality (confidence, source, recency, validity)
  4. build     sections, attribution, dedupe, token budget

A continuation request ("continue / resume / where did we leave ...") also
pulls the project's structured state: its latest status and decisions, open
commitments, past errors with their fixes, lessons and recent activity, so the
agent picks up where things stopped without replaying whole conversations.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from memory.intelligence.entity_resolver import EntityResolver
from memory.retrieval import context_builder, hybrid_search, memory_ranker
from memory.retrieval.memory_ranker import RankedMemory, Signals
from memory.retrieval.query_analysis import QueryAnalysis, analyse
from memory.retrieval.semantic_search import HashEmbedder, VectorIndex


class MemoryRetriever:
    def __init__(self, mm, embedder=None, use_vectors: bool = True):
        self.mm = mm
        self.repo = mm.repo
        self.resolver = EntityResolver(mm.repo)
        self.vectors: Optional[VectorIndex] = VectorIndex(mm.repo, embedder) if use_vectors else None
        # a second, spelling-level index when the main one is a meaning model (cheap, no model needed)
        self.lexical: Optional[VectorIndex] = None
        if self.vectors is not None and self.vectors.model != HashEmbedder.name:
            self.lexical = VectorIndex(mm.repo, HashEmbedder())

    # ── 1. analyse ─────────────────────────────────────────────
    def analyse(self, text: str) -> QueryAnalysis:
        qa = analyse(text, self.resolver)
        if qa.intent == "continuation" and not qa.entities:
            focus = self.mm.working.focus()
            ent = self.resolver.resolve(focus["what"]) if focus else None
            if ent:
                qa.entities = [ent]
        return qa

    # ── 2 + 3. gather and rank ─────────────────────────────────
    def retrieve(self, text: str, k: int = 8, filters: Optional[dict] = None, agent: Optional[str] = None,
                 qa: Optional[QueryAnalysis] = None, min_relevance: float = 0.12) -> List[RankedMemory]:
        qa = qa or self.analyse(text)
        cands = hybrid_search.gather(self.repo, self.vectors, qa, k=k, filters=filters, lexical=self.lexical)
        if qa.intent == "continuation":
            for mid in self._project_state_ids(qa):
                cands.setdefault(mid, Signals()).forced = True
        records = {r.memory_id: r for r in self.repo.find(ids=list(cands), status=None, limit=len(cands) or 1)}
        if filters and filters.get("type"):  # forced / graph candidates must respect an explicit type filter
            types = filters["type"] if isinstance(filters["type"], (list, tuple, set)) else [filters["type"]]
            records = {m: r for m, r in records.items() if r.type in types}
        if not qa.historical:
            records = {m: r for m, r in records.items() if r.status == "active"}
        records = {m: r for m, r in records.items() if not r.data.get("retracted")}  # corrected mistakes
        if not self._about_conversations(qa):
            # conversation summaries quote what was said, outdated parts included: they answer
            # "what did we talk about", not "what is true"
            records = {m: r for m, r in records.items() if not (r.type == "episodic" and r.category == "conversation")}
        ranked = memory_ranker.rank(cands, records, qa.type_boost, qa.category_boost, historical=qa.historical,
                                    min_relevance=min_relevance)
        ranked = memory_ranker.softmax_gap(ranked)
        if agent:
            with self.repo._tx() as conn:
                self.repo._log(conn, "retrieve", None, agent, {"results": len(ranked[:k]), "intent": qa.intent})
        return ranked[:k] if qa.intent != "continuation" else ranked[:max(k, 16)]

    def recall(self, text: str, k: int = 5, **filters):
        """Just the records, best first (what benchmarks and simple callers need)."""
        return [r.record for r in self.retrieve(text, k=k, filters=filters or None)]

    @staticmethod
    def _about_conversations(qa: QueryAnalysis) -> bool:
        import re
        return qa.intent in ("history", "continuation") or bool(
            re.search(r"\b(?:talk(?:ed)?|discuss(?:ed)?|conversation|chat(?:ted)?|session|said)\b", qa.text, re.I))

    # ── 4. build ───────────────────────────────────────────────
    def context(self, text: str, k: int = 10, budget_tokens: int = 600, agent: Optional[str] = None,
                filters: Optional[dict] = None, include_ids: bool = True) -> context_builder.ContextPackage:
        qa = self.analyse(text)
        ranked = self.retrieve(text, k=k, filters=filters, agent=agent, qa=qa)
        return context_builder.build(text, ranked, budget_tokens=budget_tokens, include_ids=include_ids,
                                     analysis=qa.to_dict())

    # ── project continuation ───────────────────────────────────
    def _project_state_ids(self, qa: QueryAnalysis) -> List[str]:
        ids: List[str] = []
        focus = self.repo.find(type="working", data={"key": "focus"}, limit=1)
        ids += [r.memory_id for r in focus]
        for ent in qa.entities:
            eid, name = ent["entity_id"], ent["name"]
            facts = self.repo.find(entity_id=eid, type="semantic", limit=50, order="updated")
            by_cat: Dict[str, int] = {}
            for f in facts:  # latest few of each kind: state, decisions, objectives, other facts
                n = by_cat.get(f.category, 0)
                if n < (3 if f.category in ("decision", "objective") else 2):
                    ids.append(f.memory_id)
                    by_cat[f.category] = n + 1
            ids += [r.memory_id for r in self.mm.prospective.pending() if r.entity_id == eid]
            eps = self.repo.find(entity_id=eid, type="episodic", limit=30, order="created")
            ids += [e.memory_id for e in eps if e.data.get("outcome") == "failure" or e.data.get("error")][:4]
            ids += [e.memory_id for e in eps if e.data.get("outcome") != "failure" and not e.data.get("error")][:3]
            lessons = self.repo.find(entity_id=eid, type="reflective", limit=5, order="importance")
            lessons += self.mm.search(name, limit=3, type="reflective")
            ids += [l.memory_id for l in lessons]
        return list(dict.fromkeys(ids))
