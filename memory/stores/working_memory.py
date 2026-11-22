"""Working memory: the scratchpad of the task in hand.

Short-lived key/value notes scoped to a session, task or agent ("the draft
we're editing", "the folder the operator picked", "current focus"). Setting a
key again replaces it; everything expires (default 24 h) and is never used as
long-term knowledge. Consolidation (milestone 4) decides what, if anything,
moves on to the other stores.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Dict, List, Optional

from memory.core.memory_config import RETENTION
from memory.stores.base import Store, plus, stable_id

GLOBAL = "global"


class WorkingMemory(Store):
    TYPE = "working"

    def _id(self, scope: str, key: str) -> str:
        return stable_id("wrk", scope, key)

    def set(self, key: str, value: Any, scope: str = GLOBAL, ttl: Optional[timedelta] = None,
            source: str = "agent") -> str:
        ttl = ttl or timedelta(hours=RETENTION.working_memory_hours)
        mid = self._id(scope, key)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        content = f"{key}: {text}"[:4000]
        data = {"key": key, "scope": scope, "value": value}
        existing = self.repo.get(mid)
        if existing is None:
            self.mm.remember(content, type=self.TYPE, category="scratch", source_type=source, importance=0.2,
                             data=data, memory_id=mid, dedupe=False, expires_in=ttl, tags=[f"scope:{scope}"])
        else:  # replace in place; a working note has no history worth keeping
            self.repo.update(mid, reason="working update", content=content, data=data, status="active",
                             expires_at=plus(ttl))
        return mid

    def _live(self, mid: str):
        self.repo.expire()
        rec = self.repo.get(mid)
        return rec if rec and rec.status == "active" else None

    def get(self, key: str, scope: str = GLOBAL, default: Any = None) -> Any:
        rec = self._live(self._id(scope, key))
        return rec.data.get("value", default) if rec else default

    def items(self, scope: str = GLOBAL) -> Dict[str, Any]:
        self.repo.expire()
        return {r.data["key"]: r.data.get("value")
                for r in self.repo.find(type=self.TYPE, tag=f"scope:{scope}", order="oldest", limit=500)}

    def delete(self, key: str, scope: str = GLOBAL) -> bool:
        return self.repo.delete(self._id(scope, key), actor="system", reason="working note cleared")

    def clear(self, scope: str = GLOBAL) -> int:
        return self.repo.delete_where(actor="system", reason="working scope cleared", type=self.TYPE,
                                      tag=f"scope:{scope}")

    # focus: what the operator is working on right now
    def set_focus(self, what: str, kind: str = "project", ttl: Optional[timedelta] = None) -> str:
        return self.set("focus", {"what": what, "kind": kind}, scope=GLOBAL, ttl=ttl or timedelta(hours=12),
                        source="user_stated")

    def focus(self) -> Optional[Dict[str, Any]]:
        return self.get("focus")

    def scopes(self) -> List[str]:
        return sorted({r.data.get("scope", GLOBAL) for r in self.all(limit=1000)})
