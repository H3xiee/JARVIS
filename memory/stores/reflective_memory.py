"""Reflective memory: lessons and insights drawn from experience.

A lesson says what to do (or avoid) next time, in a context, backed by the
episodes it was learned from. Re-learning the same lesson reinforces it
(more evidence, higher confidence) instead of duplicating it. Lessons the
false-positive LessonsStore writes land here too (same ids).
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from memory.core.memory_types import MemoryRecord
from memory.stores.base import Store, stable_id


class ReflectiveMemory(Store):
    TYPE = "reflective"

    def learn(self, lesson: str, context: str = "general", trigger: str = "", platform: str = "general",
              evidence: Iterable[str] = (), agent: Optional[str] = None, source: str = "agent",
              reference: Optional[str] = None, kind: str = "lesson", confidence: float = 0.6) -> MemoryRecord:
        """kind: 'lesson' (do / avoid this) or 'insight' (a pattern noticed)."""
        evidence = [e for e in evidence if e]
        mid = stable_id("rfl", kind, context, lesson.lower().strip().rstrip("."))
        existing = self.repo.get(mid)
        if existing is not None and existing.status == "active":
            ev = sorted(set(existing.data.get("evidence", [])) | set(evidence))
            fresh = set(ev) - set(existing.data.get("evidence", []))
            if evidence and not fresh:
                return existing  # same evidence seen again (e.g. a re-run): nothing new learned
            gain = 0.1 * max(1, len(fresh))
            return self.repo.update(mid, reason="reinforced", data={"evidence": ev,
                                                                    "times_learned": int(existing.data.get(
                                                                        "times_learned", 1)) + 1},
                                    confidence=min(0.95, existing.confidence + gain))
        return self.mm.remember(lesson, type=self.TYPE, category=kind, source_type=source, reference=reference,
                                memory_id=mid, dedupe=False, importance=0.7, confidence=confidence, verified=False,
                                tags=[f"context:{context}"] + ([f"agent:{agent}"] if agent else []),
                                data={"context": context, "trigger": trigger, "platform": platform,
                                      "evidence": evidence, "agent": agent, "times_learned": 1})

    def confirm(self, memory_id: str) -> MemoryRecord:
        """The operator agreed with the lesson."""
        return self.repo.update(memory_id, reason="operator confirmed", verification_status="verified",
                                confidence=1.0)

    def lessons_for(self, query: str = "", context: Optional[str] = None, agent: Optional[str] = None,
                    limit: int = 5) -> List[MemoryRecord]:
        """Lessons relevant to a situation: matching words first, then by confidence."""
        tag = f"context:{context}" if context else (f"agent:{agent}" if agent else None)
        if query.strip():
            hits = self.mm.search(query, limit=limit * 3, type=self.TYPE, tag=tag)
        else:
            hits = self.repo.find(type=self.TYPE, tag=tag, limit=limit * 3, order="importance")
        if agent and context:
            hits = [h for h in hits if h.data.get("agent") in (agent, None)]
        hits.sort(key=lambda r: (-round(r.data.get("_score", 0), 1), -r.confidence))
        return hits[:limit]

    def insights(self, limit: int = 10) -> List[MemoryRecord]:
        return self.repo.find(type=self.TYPE, category="insight", limit=limit, order="importance")
