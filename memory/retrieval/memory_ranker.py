"""Step 3 of retrieval: rank and filter candidates.

score = relevance x quality, where
  relevance  how well the memory answers *this* request: keyword (BM25) and
             vector similarity, exact phrase, the entity asked about, a graph
             neighbour, the time window, and the kind of memory asked for;
  quality    how much to trust and prefer it: confidence, source, recency,
             importance and validity (superseded / disputed / expired).
Relevance gates: a very trustworthy memory about something else still ranks
low. Every result carries the reasons it scored as it did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SOURCE_TRUST = {"user_confirmed": 1.0, "user_stated": 0.85, "tool_result": 0.75, "import": 0.6, "agent": 0.55,
                "system": 0.5}
HALF_LIFE_DAYS = {"working": 1, "episodic": 45, "prospective": 30, "semantic": 365, "procedural": 180,
                  "reflective": 180}
VERIFICATION = {"verified": 1.0, "unverified": 0.8, "inferred": 0.7, "disputed": 0.45}
WEIGHTS = {"keyword": 0.45, "vector": 0.45, "fuzzy": 0.35, "exact": 0.3, "entity": 0.35, "graph": 0.15, "temporal": 0.2,
           "type": 0.25, "category": 0.25, "forced": 0.5}


@dataclass
class Signals:
    keyword: float = 0.0       # normalised BM25, 0..1
    vector: float = 0.0        # rescaled cosine from the meaning embedder, 0..1
    fuzzy: float = 0.0         # rescaled cosine from the spelling (character n-gram) embedder, 0..1
    exact: bool = False
    entity: bool = False
    graph: bool = False
    temporal: bool = False
    forced: bool = False       # pulled in by a structured lookup (e.g. project state) rather than a match


@dataclass
class RankedMemory:
    record: Any
    score: float
    relevance: float
    quality: float
    reasons: List[str] = field(default_factory=list)
    section: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        r = self.record
        return {"memory_id": r.memory_id, "content": r.content, "type": r.type, "category": r.category,
                "score": round(self.score, 4), "relevance": round(self.relevance, 4),
                "quality": round(self.quality, 4), "reasons": self.reasons, "section": self.section,
                "source": r.source.type, "verification": r.verification_status, "updated_at": r.updated_at}


def _age_days(ts: Optional[str], now: datetime) -> float:
    if not ts:
        return 365.0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return 365.0
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def quality(rec, now: datetime, historical: bool = False) -> (float, List[str]):
    reasons = []
    trust = SOURCE_TRUST.get(rec.source.type, 0.5)
    recency = 0.5 ** (_age_days(rec.updated_at, now) / HALF_LIFE_DAYS.get(rec.type, 180))
    q = 0.3 * rec.confidence + 0.2 * trust + 0.2 * recency + 0.15 * rec.importance + 0.15
    q *= VERIFICATION.get(rec.verification_status, 0.8)
    if rec.verification_status == "disputed":
        reasons.append("disputed")
    if rec.status == "superseded":
        q *= 0.75 if historical else 0.2
        reasons.append("superseded")
    elif rec.status in ("expired", "archived"):
        q *= 0.3
        reasons.append(rec.status)
    if rec.type == "prospective" and rec.data.get("due_at"):
        days = (datetime.fromisoformat(rec.data["due_at"].replace("Z", "+00:00")) - now).total_seconds() / 86400
        if days < 7:
            q = min(1.0, q + 0.1)
            reasons.append("due soon" if days >= 0 else "overdue")
    return q, reasons


def rank(candidates: Dict[str, Signals], records: Dict[str, Any], type_boost: Dict[str, float],
         category_boost: Dict[str, float], historical: bool = False, min_relevance: float = 0.12,
         now: Optional[datetime] = None) -> List[RankedMemory]:
    now = now or datetime.now(timezone.utc)
    out = []
    for mid, s in candidates.items():
        rec = records.get(mid)
        if rec is None:
            continue
        reasons = []
        rel = WEIGHTS["keyword"] * s.keyword + WEIGHTS["vector"] * s.vector + WEIGHTS["fuzzy"] * s.fuzzy
        if s.fuzzy:
            reasons.append(f"spelling {s.fuzzy:.2f}")
        if s.keyword:
            reasons.append(f"keywords {s.keyword:.2f}")
        if s.vector:
            reasons.append(f"meaning {s.vector:.2f}")
        for name in ("exact", "entity", "graph", "temporal", "forced"):
            if getattr(s, name):
                rel += WEIGHTS[name]
                reasons.append(name)
        matched = rel > 0
        tb = type_boost.get(rec.type, 0.0)
        cb = category_boost.get(rec.category, 0.0)
        if matched and (tb or cb):  # a boost lifts a match; it never makes an unrelated memory relevant
            rel += WEIGHTS["type"] * tb + WEIGHTS["category"] * cb
            reasons.append(f"kind {rec.type}/{rec.category}")
        rel = min(rel, 1.5)
        if rel < min_relevance:
            continue
        q, qreasons = quality(rec, now, historical)
        out.append(RankedMemory(rec, rel * q, rel, q, reasons + qreasons))
    out.sort(key=lambda r: -r.score)
    return out


def rescale_vector(cos: float, floor: float, ceil: float = 1.0) -> float:
    """Map an embedder's cosine onto 0..1: below `floor` is unrelated, `ceil` and up is a clear match."""
    return max(0.0, min(1.0, (cos - floor) / max(1e-6, ceil - floor)))


def rescale_keyword(scores: Dict[str, float]) -> Dict[str, float]:
    if not scores:
        return {}
    top = max(scores.values()) or 1.0
    return {k: max(0.0, v) / top if top > 0 else 0.0 for k, v in scores.items()}


def softmax_gap(ranked: List[RankedMemory], ratio: float = 0.25) -> List[RankedMemory]:
    """Drop the long tail: results scoring under `ratio` of the best one."""
    if not ranked:
        return ranked
    best = ranked[0].score
    return [r for r in ranked if r.score >= best * ratio or r.reasons and "forced" in r.reasons]

