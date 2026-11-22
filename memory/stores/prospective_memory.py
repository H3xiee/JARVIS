"""Prospective memory: future commitments.

"I'll send Ravi the sheet by Friday", "remind me to renew the domain",
"follow up if Priya hasn't replied". An intention is pending until it is
completed or cancelled; JARVIS can list what is due, overdue or coming up.
Completed and cancelled intentions stay (status archived) as a record.
"""

from __future__ import annotations

from datetime import timedelta
from typing import List, Optional

from memory.core.memory_types import MemoryRecord, to_utc_iso
from memory.stores.base import Store, iso, now_dt, parse

STATES = ("pending", "done", "cancelled")


class ProspectiveMemory(Store):
    TYPE = "prospective"

    def commit(self, text: str, due_at=None, owner: str = "operator", counterpart: Optional[str] = None,
               source: str = "user_stated", reference: Optional[str] = None, agent: Optional[str] = None,
               remind_before: Optional[timedelta] = None) -> MemoryRecord:
        """owner: who must act ('operator' or 'jarvis'); counterpart: who it is for (a contact)."""
        due = to_utc_iso(due_at)
        data = {"due_at": due, "owner": owner, "state": "pending", "counterpart": counterpart, "agent": agent,
                "remind_at": iso(parse(due) - remind_before) if due and remind_before else due}
        return self.mm.remember(text, type=self.TYPE, category="commitment", entity=counterpart, entity_kind="person",
                                source_type=source, reference=reference, importance=0.8,
                                data={k: v for k, v in data.items() if v is not None},
                                tags=["state:pending", f"owner:{owner}"])

    def _set_state(self, memory_id: str, state: str, note: Optional[str]) -> MemoryRecord:
        rec = self.repo.get(memory_id)
        if rec is None or rec.type != self.TYPE:
            raise KeyError(memory_id)
        tags = [t for t in rec.tags if not t.startswith("state:")] + [f"state:{state}"]
        data = {"state": state, f"{state}_at": iso(now_dt())}
        if note:
            data["note"] = note[:300]
        return self.repo.update(memory_id, reason=state, data=data, tags=tags, status="archived",
                                valid_to=iso(now_dt()))

    def complete(self, memory_id: str, note: Optional[str] = None) -> MemoryRecord:
        return self._set_state(memory_id, "done", note)

    def cancel(self, memory_id: str, note: Optional[str] = None) -> MemoryRecord:
        return self._set_state(memory_id, "cancelled", note)

    def reschedule(self, memory_id: str, due_at) -> MemoryRecord:
        due = to_utc_iso(due_at)
        return self.repo.update(memory_id, reason="rescheduled", data={"due_at": due, "remind_at": due})

    def pending(self, owner: Optional[str] = None, counterpart: Optional[str] = None) -> List[MemoryRecord]:
        """Open commitments, soonest due first (undated ones last)."""
        rows = self.repo.find(type=self.TYPE, tag="state:pending", limit=1000)
        if owner:
            rows = [r for r in rows if r.data.get("owner") == owner]
        if counterpart:
            ent = self.repo.find_entity(counterpart)
            rows = [r for r in rows if ent and r.entity_id == ent["entity_id"]]
        return sorted(rows, key=lambda r: (r.data.get("due_at") is None, r.data.get("due_at") or ""))

    def due(self, now=None, within: timedelta = timedelta(0)) -> List[MemoryRecord]:
        """Pending intentions whose reminder time has come (by now + within)."""
        limit = iso((parse(now) if now else now_dt()) + within)
        return [r for r in self.pending() if r.data.get("remind_at") and r.data["remind_at"] <= limit]

    def overdue(self, now=None) -> List[MemoryRecord]:
        t = iso(parse(now) if now else now_dt())
        return [r for r in self.pending() if r.data.get("due_at") and r.data["due_at"] < t]
