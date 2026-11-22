"""Procedural memory: reusable workflows.

A procedure is a named, ordered list of steps with a trigger phrase. Each use
records success or failure; confidence follows the track record (Laplace
smoothed), and a procedure counts as *validated* once it has worked at least
twice and more often than not. Editing the steps keeps the old version.
"""

from __future__ import annotations

from typing import List, Optional

from memory.core.memory_types import MemoryRecord
from memory.stores.base import Store, iso, now_dt, stable_id


def _confidence(success: int, failure: int) -> float:
    return round((success + 1) / (success + failure + 2), 4)


class ProceduralMemory(Store):
    TYPE = "procedural"
    MIN_VALIDATED_SUCCESSES = 2

    def _id(self, name: str) -> str:
        return stable_id("prc", name)

    def learn(self, name: str, steps: List[str], trigger: str = "", description: str = "",
              agent: Optional[str] = None, source: str = "agent", reference: Optional[str] = None,
              tags=()) -> MemoryRecord:
        steps = [str(s).strip() for s in steps if str(s).strip()]
        if not steps:
            raise ValueError("a procedure needs at least one step")
        content = f"{description or name}. Steps: " + "; ".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
        if trigger:
            content = f"{content} (when: {trigger})"
        mid = self._id(name)
        existing = self.repo.get(mid)
        if existing is not None and existing.status == "active":
            if existing.data.get("steps") == steps and existing.data.get("trigger") == trigger:
                return existing
            return self.repo.update(mid, reason="steps changed", content=content,
                                    data={"steps": steps, "trigger": trigger, "description": description})
        return self.mm.remember(content, type=self.TYPE, category="workflow", source_type=source, reference=reference,
                                memory_id=mid, dedupe=False, importance=0.6, confidence=0.5, verified=False,
                                tags=list(tags) + ([f"agent:{agent}"] if agent else []),
                                data={"name": name, "steps": steps, "trigger": trigger, "description": description,
                                      "agent": agent, "success_count": 0, "failure_count": 0})

    def get(self, name: str) -> Optional[MemoryRecord]:
        rec = self.repo.get(self._id(name))
        if rec is None:  # skills migrated from the legacy store have other ids
            found = self.repo.find(type=self.TYPE, data={"name": name}, limit=1)
            rec = found[0] if found else None
        return rec if rec and rec.status == "active" else None

    def record_outcome(self, name: str, success: bool, note: Optional[str] = None) -> MemoryRecord:
        rec = self.get(name)
        if rec is None:
            raise KeyError(name)
        s = int(rec.data.get("success_count") or 0) + (1 if success else 0)
        f = int(rec.data.get("failure_count") or 0) + (0 if success else 1)
        validated = s >= self.MIN_VALIDATED_SUCCESSES and s > f
        data = {"success_count": s, "failure_count": f, "last_used_at": iso(now_dt()),
                "last_outcome": "success" if success else "failure"}
        if note:
            data["last_note"] = note[:300]
        return self.repo.update(rec.memory_id, reason="outcome", data=data, confidence=_confidence(s, f),
                                verification_status="verified" if validated else rec.verification_status)

    def find(self, query: str, limit: int = 3, validated_only: bool = False) -> List[MemoryRecord]:
        """Procedures for a request, best track record first among equally relevant ones."""
        hits = self.mm.search(query, limit=limit * 3, type=self.TYPE)
        if validated_only:
            hits = [h for h in hits if self.is_validated(h)]
        hits.sort(key=lambda r: (-round(r.data.get("_score", 0), 1), -r.confidence))
        return hits[:limit]

    def is_validated(self, rec: MemoryRecord) -> bool:
        s, f = int(rec.data.get("success_count") or 0), int(rec.data.get("failure_count") or 0)
        return s >= self.MIN_VALIDATED_SUCCESSES and s > f

    def validated(self) -> List[MemoryRecord]:
        return [r for r in self.all(limit=1000) if self.is_validated(r)]
