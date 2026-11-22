"""MemoryManager: the shared memory interface for JARVIS and its agents.

    from memory.core.memory_manager import get_memory
    mem = get_memory()
    mem.remember("Prefers short answers", type="semantic", category="preference",
                 source_type="user_confirmed", verified=True)
    mem.inspect(memory_id)       # record + where it came from + its earlier versions

Retrieval ranking, consolidation and per-agent context arrive in later
milestones; they plug in here without callers changing.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from memory.core import memory_config
from memory.core.memory_repository import MemoryRepository
from memory.core.memory_types import (MemoryRecord, MemorySource, MemoryType, SourceType, Status, Verification,
                                      to_utc_iso, utc_now)

_EXPORT_FORMAT = "jarvis-memory-export"


class MemoryManager:
    def __init__(self, repository: Optional[MemoryRepository] = None, path: Optional[Path | str] = None,
                 detect_conflicts: bool = True):
        self.repo = repository or MemoryRepository(path)
        self._stores: Dict[str, Any] = {}
        if detect_conflicts:  # every new fact is checked against what is already remembered
            from memory.intelligence import contradiction_detector
            contradiction_detector.attach(self)

    # ── the six typed stores ───────────────────────────────────
    def _store(self, name: str):
        if name not in self._stores:
            import memory.stores as st
            cls = {"working": st.WorkingMemory, "episodic": st.EpisodicMemory, "semantic": st.SemanticMemory,
                   "procedural": st.ProceduralMemory, "prospective": st.ProspectiveMemory,
                   "reflective": st.ReflectiveMemory}[name]
            self._stores[name] = cls(self)
        return self._stores[name]

    @property
    def working(self):
        return self._store("working")

    @property
    def episodic(self):
        return self._store("episodic")

    @property
    def semantic(self):
        return self._store("semantic")

    @property
    def procedural(self):
        return self._store("procedural")

    @property
    def prospective(self):
        return self._store("prospective")

    @property
    def reflective(self):
        return self._store("reflective")

    @property
    def consolidator(self):
        """Turns conversations and experiences into structured memories (milestone 4)."""
        if "consolidator" not in self._stores:
            from memory.intelligence.memory_consolidation import MemoryConsolidator
            self._stores["consolidator"] = MemoryConsolidator(self)
        return self._stores["consolidator"]

    def consolidate(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        return self.consolidator.run(session_id)

    @property
    def retriever(self):
        """The hybrid retrieval engine (keyword + vector + entities + graph + time), built on first use."""
        if "retriever" not in self._stores:
            from memory.retrieval.memory_retrieval import MemoryRetriever
            self._stores["retriever"] = MemoryRetriever(self)
        return self._stores["retriever"]

    # ── write ──────────────────────────────────────────────────
    def remember(self, content: str, type: str | MemoryType = MemoryType.SEMANTIC, category: str = "general",
                 entity: Optional[str] = None, entity_kind: str = "other", source_type: str = SourceType.AGENT.value,
                 reference: Optional[str] = None, excerpt: Optional[str] = None, confidence: float = 0.8,
                 verified: Optional[bool] = None, importance: float = 0.5, tags: Iterable[str] = (),
                 data: Optional[Dict[str, Any]] = None, valid_from=None, valid_to=None,
                 expires_in: Optional[timedelta] = None, actor: str = "system", created_at=None,
                 memory_id: Optional[str] = None, dedupe: bool = True) -> MemoryRecord:
        """Store (or reinforce) one memory. `entity` is a name ("JARVIS project")
        resolved to a stable entity id. `verified` defaults from the source:
        the operator's own confirmation is verified, everything else is not."""
        if verified is None:
            verified = source_type == SourceType.USER_CONFIRMED.value
        status = Verification.VERIFIED if verified else (
            Verification.INFERRED if source_type == SourceType.AGENT.value else Verification.UNVERIFIED)
        entity_id = self.repo.upsert_entity(entity, entity_kind) if entity else None
        now = utc_now()
        rec = MemoryRecord(
            content=content, type=type, category=category, entity_id=entity_id,
            source=MemorySource(source_type, reference, excerpt), confidence=1.0 if verified else confidence,
            verification_status=status, importance=importance, tags=sorted(set(tags)), data=dict(data or {}),
            valid_from=to_utc_iso(valid_from), valid_to=to_utc_iso(valid_to),
            created_at=to_utc_iso(created_at) or now, updated_at=to_utc_iso(created_at) or now,
            expires_at=(datetime.now(timezone.utc) + expires_in).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            if expires_in else None)
        if memory_id:
            rec.memory_id = memory_id
        return self.repo.add(rec, actor=actor, dedupe=dedupe)

    def correct(self, memory_id: str, new_content: str, reference: Optional[str] = None) -> MemoryRecord:
        return self.repo.correct(memory_id, new_content, reference=reference)

    def forget(self, memory_id: str, actor: str = "user") -> bool:
        return self.repo.delete(memory_id, actor=actor, reason="operator asked to forget")

    def forget_about(self, entity: str, actor: str = "user") -> int:
        """Delete everything remembered about an entity, and the entity itself."""
        ent = self.repo.find_entity(entity)
        if not ent:
            return 0
        n = self.repo.delete_where(actor=actor, reason="operator asked to forget", entity_id=ent["entity_id"])
        with self.repo._tx() as conn:
            conn.execute("DELETE FROM entities WHERE entity_id=?", (ent["entity_id"],))
        return n

    # ── read ───────────────────────────────────────────────────
    def get(self, memory_id: str) -> Optional[MemoryRecord]:
        return self.repo.get(memory_id)

    def about(self, entity: str, valid_at=None, **filters) -> List[MemoryRecord]:
        ent = self.repo.find_entity(entity)
        return self.repo.find(entity_id=ent["entity_id"], valid_at=valid_at, **filters) if ent else []

    def search(self, query: str, limit: int = 10, actor: Optional[str] = None, **filters) -> List[MemoryRecord]:
        """Keyword search over active memories (the full hybrid retriever replaces this in milestone 3)."""
        filters.setdefault("status", Status.ACTIVE.value)
        return self.repo.search_text(query, limit=limit, actor=actor, **filters)

    def inspect(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """What JARVIS remembers, where it came from, and how it changed."""
        rec = self.repo.get(memory_id)
        if rec is None:
            return None
        ent = self.repo.get_entity(rec.entity_id) if rec.entity_id else None
        return {"memory": rec.to_dict(), "entity": ent, "sources": self.repo.sources(memory_id),
                "versions": self.repo.history(memory_id),
                "lineage": [r.memory_id for r in self.repo.lineage(memory_id)]}

    def stats(self) -> Dict[str, Any]:
        return self.repo.stats()

    # ── export / import ────────────────────────────────────────
    def export(self, include_sessions: bool = True) -> Dict[str, Any]:
        conn = self.repo.raw()
        with self.repo._lock:
            dump = lambda sql: [dict(r) for r in conn.execute(sql).fetchall()]  # noqa: E731
            out = {"format": _EXPORT_FORMAT, "schema_version": self.repo.schema_version, "exported_at": utc_now(),
                   "memories": [r.to_dict() | {"superseded_by": r.data.pop("_superseded_by", None)}
                                for r in self.repo.find(limit=10 ** 7, status=None, order="oldest")],
                   "sources": dump("SELECT memory_id, source_type, reference, excerpt, added_at FROM memory_sources "
                                   "ORDER BY source_id"),
                   "versions": dump("SELECT memory_id, version, snapshot, reason, changed_at FROM memory_versions "
                                    "ORDER BY version_id"),
                   "entities": dump("SELECT * FROM entities"),
                   "relationships": dump("SELECT * FROM relationships")}
            if include_sessions:
                out["sessions"] = dump("SELECT * FROM sessions")
                out["messages"] = dump("SELECT session_id, role, content, at, target FROM session_messages "
                                       "ORDER BY message_id")
        return out

    def export_to(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.export(), ensure_ascii=False, indent=1), encoding="utf-8")
        return path

    def import_from(self, payload: Dict[str, Any] | Path | str) -> Dict[str, int]:
        """Load an export into this database. Records already present (same id) are kept as they are."""
        if not isinstance(payload, dict):
            payload = json.loads(Path(payload).read_text(encoding="utf-8"))
        if payload.get("format") != _EXPORT_FORMAT:
            raise ValueError("not a JARVIS memory export")
        counts = {"memories": 0, "entities": 0, "sessions": 0, "messages": 0}
        conn = self.repo.raw()
        with self.repo._tx():
            for e in payload.get("entities", []):
                counts["entities"] += conn.execute(
                    "INSERT OR IGNORE INTO entities(entity_id, kind, name, aliases, data, created_at, updated_at) "
                    "VALUES (:entity_id,:kind,:name,:aliases,:data,:created_at,:updated_at)", e).rowcount
            known = {r[0] for r in conn.execute("SELECT memory_id FROM memories")}
            fresh = set()
            for m in payload.get("memories", []):
                if m["memory_id"] in known:
                    continue
                sup = m.pop("superseded_by", None)
                rec = MemoryRecord.from_dict(m)
                if rec.status == Status.ACTIVE.value and conn.execute(
                        "SELECT 1 FROM memories WHERE content_hash=? AND status='active'", (rec.hash,)).fetchone():
                    continue  # same statement already remembered here
                self.repo._write_row(conn, rec, superseded_by=sup)
                fresh.add(rec.memory_id)
                counts["memories"] += 1
            for s in payload.get("sources", []):
                if s["memory_id"] in fresh:
                    conn.execute("INSERT INTO memory_sources(memory_id, source_type, reference, excerpt, added_at) "
                                 "VALUES (:memory_id,:source_type,:reference,:excerpt,:added_at)", s)
            for v in payload.get("versions", []):
                if v["memory_id"] in fresh:
                    conn.execute("INSERT INTO memory_versions(memory_id, version, snapshot, reason, changed_at) "
                                 "VALUES (:memory_id,:version,:snapshot,:reason,:changed_at)", v)
            for r in payload.get("relationships", []):
                conn.execute("INSERT OR IGNORE INTO relationships(rel_id, src_entity, relation, dst_entity, memory_id, "
                             "confidence, valid_from, valid_to, created_at) VALUES (:rel_id,:src_entity,:relation,"
                             ":dst_entity,:memory_id,:confidence,:valid_from,:valid_to,:created_at)", r)
            for s in payload.get("sessions", []):
                counts["sessions"] += conn.execute(
                    "INSERT OR IGNORE INTO sessions(session_id, started_at, ended_at, channel, summary, data) "
                    "VALUES (:session_id,:started_at,:ended_at,:channel,:summary,:data)", s).rowcount
            for msg in payload.get("messages", []):
                counts["messages"] += conn.execute(
                    "INSERT OR IGNORE INTO session_messages(session_id, role, content, at, target) "
                    "VALUES (:session_id,:role,:content,:at,:target)", msg).rowcount
            self.repo._log(conn, "import", None, "system", counts)
        return counts


_instances: Dict[str, MemoryManager] = {}
_instances_lock = threading.Lock()


def get_memory(path: Optional[Path | str] = None) -> MemoryManager:
    """The process-wide manager for a database (default: data/memory/jarvis_memory.db,
    or JARVIS_MEMORY_DB)."""
    key = str(Path(path or memory_config.db_path()).resolve())
    with _instances_lock:
        if key not in _instances:
            from memory.maintenance.memory_recovery import open_safely
            _instances[key] = open_safely(Path(key))  # a damaged file is set aside and restored from backup
        return _instances[key]


def reset_instances():
    """Close cached managers (tests)."""
    with _instances_lock:
        for m in _instances.values():
            try:
                m.repo.close()
            except Exception:
                pass
        _instances.clear()
