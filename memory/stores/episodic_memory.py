"""Episodic memory: things that happened.

An episode is one experience with its outcome: an agent run, a conversation
milestone, an error and how it was fixed. Episodes repeat legitimately (the
same cleanup run twice is two episodes), so they are never deduplicated.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from memory.core.memory_types import MemoryRecord
from memory.stores.base import Store, iso, now_dt

OUTCOMES = ("success", "partial", "failure", "cancelled", "unknown")


class EpisodicMemory(Store):
    TYPE = "episodic"

    def record(self, summary: str, outcome: str = "unknown", agent: Optional[str] = None, goal: Optional[str] = None,
               session_id: Optional[str] = None, started_at=None, ended_at=None, entity: Optional[str] = None,
               category: str = "event", details: Optional[Dict[str, Any]] = None, error: Optional[str] = None,
               resolution: Optional[str] = None, source: str = "tool_result", reference: Optional[str] = None,
               importance: Optional[float] = None, tags=(), entity_kind: str = "other") -> MemoryRecord:
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}")
        if importance is None:  # failures and fixes teach more than routine successes
            importance = 0.7 if outcome == "failure" or error else 0.4
        ended = ended_at or iso(now_dt())
        data = {"agent": agent, "goal": goal, "outcome": outcome, "session_id": session_id,
                "started_at": started_at or ended, "ended_at": ended, "details": details or {},
                "error": error, "resolution": resolution}
        return self.mm.remember(summary, type=self.TYPE, category=category, entity=entity, entity_kind=entity_kind,
                                source_type=source,
                                reference=reference or (f"agent:{agent}" if agent else None), importance=importance,
                                data={k: v for k, v in data.items() if v not in (None, {}, "")}, dedupe=False,
                                tags=list(tags) + ([f"agent:{agent}"] if agent else []) + [f"outcome:{outcome}"],
                                valid_from=data["started_at"], created_at=ended)

    def recent(self, limit: int = 10, agent: Optional[str] = None, since=None, session_id: Optional[str] = None,
               outcome: Optional[str] = None) -> List[MemoryRecord]:
        data = {}
        if session_id:
            data["session_id"] = session_id
        return self.repo.find(type=self.TYPE, limit=limit, since=since, order="created",
                              tag=f"agent:{agent}" if agent else (f"outcome:{outcome}" if outcome else None),
                              data=data or None)

    def failures(self, agent: Optional[str] = None, limit: int = 10) -> List[MemoryRecord]:
        rows = self.repo.find(type=self.TYPE, tag="outcome:failure", limit=limit * 4, order="created")
        return [r for r in rows if not agent or r.data.get("agent") == agent][:limit]

    def similar(self, query: str, limit: int = 5, agent: Optional[str] = None) -> List[MemoryRecord]:
        return self.mm.search(query, limit=limit, type=self.TYPE, tag=f"agent:{agent}" if agent else None)

    def resolve(self, memory_id: str, resolution: str) -> MemoryRecord:
        """Record how a failed episode was eventually fixed."""
        return self.repo.update(memory_id, reason="resolution recorded", data={"resolution": resolution})
