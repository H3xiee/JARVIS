"""Shared plumbing for the six typed memory stores.

Each store is a thin, typed view over MemoryManager / MemoryRepository: it
decides how its kind of memory is written (ids, categories, structured
fields, supersession) and offers the reads that kind needs. All storage still
goes through the repository, so versioning, provenance, dedupe and deletion
behave the same for every type.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


def stable_id(prefix: str, *parts: Any) -> str:
    key = "\x1f".join(str(p).strip().lower() for p in parts)
    return f"{prefix}_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def parse(value) -> Optional[datetime]:
    from memory.core.memory_types import to_utc_iso
    s = to_utc_iso(value)
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def plus(delta: timedelta, start: Optional[datetime] = None) -> str:
    return iso((start or now_dt()) + delta)


class Store:
    TYPE = ""

    def __init__(self, mm):
        self.mm = mm
        self.repo = mm.repo

    def all(self, limit: int = 100, **filters):
        filters.setdefault("status", "active")
        return self.repo.find(type=self.TYPE, limit=limit, **filters)

    def search(self, query: str, limit: int = 5, **filters):
        return self.mm.search(query, limit=limit, type=self.TYPE, **filters)
