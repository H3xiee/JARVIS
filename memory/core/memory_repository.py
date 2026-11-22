"""The one place that reads and writes the memory database.

Agents and stores never touch SQLite (or a vector index) directly: they go
through MemoryRepository, which keeps every change versioned, every memory
attributed to a source, and every add / change / delete in the access log.

Thread-safe: one connection guarded by a re-entrant lock (SQLite in WAL mode).
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from memory.core import memory_config
from memory.core.memory_schema import MIGRATIONS, SCHEMA_VERSION, TYPE_TABLES
from memory.core.memory_types import (MemoryRecord, MemorySource, SourceType, Status, Verification,
                                      new_id, to_utc_iso, utc_now)

_MEM_COLS = ("memory_id", "type", "category", "content", "content_hash", "entity_id", "source_type", "source_ref",
             "confidence", "verification_status", "importance", "created_at", "updated_at", "valid_from",
             "valid_to", "expires_at", "status", "version", "superseded_by", "tags", "data")
_ORDER = {"updated": "updated_at DESC", "created": "created_at DESC", "importance": "importance DESC, updated_at DESC",
          "oldest": "created_at ASC"}
_FTS_TOKEN = re.compile(r"[\w']+", re.UNICODE)

ChangeListener = Callable[[str, MemoryRecord], None]


class MemoryRepository:
    def __init__(self, path: Optional[Path | str] = None):
        self.path = Path(path) if path else memory_config.db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if str(self.path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._listeners: List[ChangeListener] = []
        self._migrate()

    # ── plumbing ───────────────────────────────────────────────
    def _migrate(self):
        with self._lock:
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for version in range(current + 1, SCHEMA_VERSION + 1):
                self._conn.executescript("BEGIN;" + MIGRATIONS[version] + f"PRAGMA user_version = {version}; COMMIT;")

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    @contextmanager
    def _tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self):
        with self._lock:
            self._conn.close()

    def on_change(self, listener: ChangeListener):
        """Called with (action, record) after add / update / correct / supersede / delete.
        The vector index uses this to stay in step with the database."""
        self._listeners.append(listener)

    def _notify(self, action: str, record: MemoryRecord):
        for cb in list(self._listeners):
            try:
                cb(action, record)
            except Exception:
                pass

    def _log(self, conn, action: str, memory_id: Optional[str], actor: Optional[str], detail: Any = None):
        conn.execute("INSERT INTO memory_access_log(at, action, memory_id, actor, detail) VALUES (?,?,?,?,?)",
                     (utc_now(), action, memory_id, actor or "system",
                      json.dumps(detail, ensure_ascii=False) if detail is not None else None))

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
        rec = MemoryRecord(
            memory_id=row["memory_id"], type=row["type"], category=row["category"], content=row["content"],
            entity_id=row["entity_id"], source=MemorySource(type=row["source_type"], reference=row["source_ref"]),
            confidence=row["confidence"], verification_status=row["verification_status"],
            importance=row["importance"], created_at=row["created_at"], updated_at=row["updated_at"],
            valid_from=row["valid_from"], valid_to=row["valid_to"], expires_at=row["expires_at"],
            status=row["status"], version=row["version"], tags=json.loads(row["tags"] or "[]"),
            data=json.loads(row["data"] or "{}"))
        if row["superseded_by"]:
            rec.data["_superseded_by"] = row["superseded_by"]
        return rec

    def _write_row(self, conn, rec: MemoryRecord, superseded_by: Optional[str] = None, insert: bool = True):
        data = {k: v for k, v in rec.data.items() if not k.startswith("_")}
        values = (rec.memory_id, rec.type, rec.category, rec.content, rec.hash, rec.entity_id, rec.source.type,
                  rec.source.reference, rec.confidence, rec.verification_status, rec.importance, rec.created_at,
                  rec.updated_at, to_utc_iso(rec.valid_from), to_utc_iso(rec.valid_to), to_utc_iso(rec.expires_at),
                  rec.status, rec.version, superseded_by, json.dumps(list(rec.tags), ensure_ascii=False),
                  json.dumps(data, ensure_ascii=False, default=str))
        if insert:
            conn.execute(f"INSERT INTO memories({','.join(_MEM_COLS)}) VALUES ({','.join('?' * len(_MEM_COLS))})", values)
        else:
            sets = ",".join(f"{c}=?" for c in _MEM_COLS[1:])
            conn.execute(f"UPDATE memories SET {sets} WHERE memory_id=?", values[1:] + (rec.memory_id,))
        table = TYPE_TABLES.get(rec.type)
        if table:
            name, cols = table
            vals = [json.dumps(data[c]) if isinstance(data.get(c), (list, dict)) else data.get(c) for c in cols]
            conn.execute(f"INSERT OR REPLACE INTO {name}(memory_id,{','.join(cols)}) VALUES (?{',?' * len(cols)})",
                         [rec.memory_id] + vals)

    def _add_source(self, conn, memory_id: str, src: MemorySource):
        conn.execute("INSERT INTO memory_sources(memory_id, source_type, reference, excerpt, added_at) VALUES (?,?,?,?,?)",
                     (memory_id, src.type, src.reference, (src.excerpt or None) and str(src.excerpt)[:500], utc_now()))

    def _snapshot(self, conn, rec: MemoryRecord, reason: str):
        conn.execute("INSERT INTO memory_versions(memory_id, version, snapshot, reason, changed_at) VALUES (?,?,?,?,?)",
                     (rec.memory_id, rec.version, json.dumps(rec.to_dict(), ensure_ascii=False, default=str),
                      reason, utc_now()))

    # ── writes ─────────────────────────────────────────────────
    def add(self, record: MemoryRecord, actor: str = "system", dedupe: bool = True) -> MemoryRecord:
        """Store a memory. If the same statement is already active, the existing
        memory is reinforced instead (new source added, confidence / importance
        raised) and returned, so callers never create duplicates."""
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM memories WHERE content_hash=? AND status='active'",
                               (record.hash,)).fetchone() if dedupe else None
            if row:
                existing = self._row_to_record(row)
                existing.confidence = max(existing.confidence, record.confidence)
                existing.importance = max(existing.importance, record.importance)
                if record.verification_status == Verification.VERIFIED.value:
                    existing.verification_status = Verification.VERIFIED.value
                existing.tags = sorted(set(existing.tags) | set(record.tags))
                existing.data = {**record.data, **{k: v for k, v in existing.data.items() if not k.startswith("_")}}
                existing.updated_at = utc_now()
                self._write_row(conn, existing, insert=False)
                self._add_source(conn, existing.memory_id, record.source)
                self._log(conn, "reinforce", existing.memory_id, actor)
                result, action = existing, "reinforce"
            else:
                self._write_row(conn, record)
                self._add_source(conn, record.memory_id, record.source)
                self._log(conn, "add", record.memory_id, actor, {"type": record.type, "category": record.category})
                result, action = record, "add"
        self._notify(action, result)
        return result

    def update(self, memory_id: str, actor: str = "system", reason: str = "update", **changes) -> MemoryRecord:
        """Change fields of a memory in place, keeping the previous version."""
        with self._tx() as conn:
            rec = self._get(conn, memory_id)
            if rec is None:
                raise KeyError(memory_id)
            self._snapshot(conn, rec, reason)
            for key, value in changes.items():
                if key == "data":
                    rec.data = {**rec.data, **value}
                elif key in ("memory_id", "created_at", "version"):
                    continue
                elif hasattr(rec, key):
                    setattr(rec, key, value)
            rec.version += 1
            rec.updated_at = utc_now()
            rec.__post_init__()
            self._write_row(conn, rec, insert=False)
            self._log(conn, "update", memory_id, actor, {"reason": reason, "fields": sorted(changes)})
        self._notify("update", rec)
        return rec

    def supersede(self, old_id: str, new_record: MemoryRecord, actor: str = "system",
                  reason: str = "superseded", retract: bool = False) -> MemoryRecord:
        """Replace a memory with a newer one. The old one stays (status superseded,
        valid_to = now) so JARVIS can say what used to be true and when it changed.
        retract=True means the old statement was never true (a correction): its
        validity window is emptied so "what was true then" never returns it."""
        now = utc_now()
        with self._tx() as conn:
            old = self._get(conn, old_id)
            if old is None:
                raise KeyError(old_id)
            self._snapshot(conn, old, reason)
            old.status = Status.SUPERSEDED.value
            if retract:
                old.valid_from = old.valid_from or old.created_at
                old.valid_to = old.valid_from
                old.data["retracted"] = True
            else:
                old.valid_to = old.valid_to or new_record.valid_from or now
            old.updated_at = now
            self._write_row(conn, old, superseded_by=new_record.memory_id, insert=False)
            new_record.entity_id = new_record.entity_id or old.entity_id
            new_record.valid_from = new_record.valid_from or now
            new_record.data.setdefault("supersedes", old_id)
            dup = conn.execute("SELECT memory_id FROM memories WHERE content_hash=? AND status='active'",
                               (new_record.hash,)).fetchone()
            if dup:  # the corrected text is already remembered: point at it instead of duplicating
                conn.execute("UPDATE memories SET superseded_by=? WHERE memory_id=?", (dup[0], old_id))
                self._log(conn, "supersede", old_id, actor, {"by": dup[0], "reason": reason})
                result = self._get(conn, dup[0])
            else:
                self._write_row(conn, new_record)
                self._add_source(conn, new_record.memory_id, new_record.source)
                self._log(conn, "supersede", old_id, actor, {"by": new_record.memory_id, "reason": reason})
                result = new_record
        self._notify("supersede", old)
        self._notify("add", result)
        return result

    def correct(self, memory_id: str, new_content: str, actor: str = "user",
                reference: Optional[str] = None) -> MemoryRecord:
        """The operator corrected something JARVIS remembered."""
        old = self.get(memory_id)
        if old is None:
            raise KeyError(memory_id)
        fixed = MemoryRecord(content=new_content, type=old.type, category=old.category, entity_id=old.entity_id,
                             source=MemorySource(SourceType.USER_CONFIRMED.value, reference or "user_correction"),
                             confidence=1.0, verification_status=Verification.VERIFIED.value,
                             importance=max(old.importance, 0.6), tags=list(old.tags),
                             data={k: v for k, v in old.data.items() if not k.startswith("_") and k != "supersedes"})
        return self.supersede(memory_id, fixed, actor=actor, reason="user correction", retract=True)

    def delete(self, memory_id: str, actor: str = "user", reason: str = "deleted") -> bool:
        """Permanently remove a memory with its versions, sources and links.
        The access log keeps only that a deletion happened, never the content."""
        with self._tx() as conn:
            rec = self._get(conn, memory_id)
            if rec is None:
                return False
            conn.execute("DELETE FROM memories WHERE memory_id=?", (memory_id,))
            conn.execute("UPDATE memories SET superseded_by=NULL WHERE superseded_by=?", (memory_id,))
            conn.execute("UPDATE memory_access_log SET detail=NULL WHERE memory_id=?", (memory_id,))
            self._log(conn, "delete", memory_id, actor, {"reason": reason})
        self._notify("delete", rec)
        return True

    def delete_where(self, actor: str = "user", reason: str = "deleted", **filters) -> int:
        """Delete every memory matching find() filters (all statuses). Returns the count."""
        filters.setdefault("status", None)
        ids = [r.memory_id for r in self.find(limit=100000, **filters)]
        return sum(self.delete(i, actor=actor, reason=reason) for i in ids)

    def expire(self, now: Optional[str] = None) -> int:
        """Mark memories past expires_at as expired. Returns how many changed."""
        now = now or utc_now()
        with self._tx() as conn:
            rows = conn.execute("SELECT memory_id FROM memories WHERE status='active' AND expires_at IS NOT NULL "
                                "AND expires_at <= ?", (now,)).fetchall()
            for (mid,) in rows:
                conn.execute("UPDATE memories SET status='expired', updated_at=? WHERE memory_id=?", (now, mid))
                self._log(conn, "expire", mid, "system")
        return len(rows)

    # ── reads ──────────────────────────────────────────────────
    def _get(self, conn, memory_id: str) -> Optional[MemoryRecord]:
        row = conn.execute("SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def get(self, memory_id: str) -> Optional[MemoryRecord]:
        with self._lock:
            return self._get(self._conn, memory_id)

    def _where(self, type=None, category=None, entity_id=None, status: Optional[str] = "active", source_type=None,
               verification_status=None, since=None, until=None, valid_at=None, tag=None,
               min_confidence=None, ids: Optional[Iterable[str]] = None, data: Optional[Dict[str, Any]] = None):
        clauses, params = [], []

        def add(col, value):
            if value is None:
                return
            if isinstance(value, (list, tuple, set)):
                clauses.append(f"m.{col} IN ({','.join('?' * len(value))})")
                params.extend(str(getattr(v, 'value', v)) for v in value)
            else:
                clauses.append(f"m.{col} = ?")
                params.append(str(getattr(value, 'value', value)))

        add("type", type), add("category", category), add("entity_id", entity_id), add("status", status)
        add("source_type", source_type), add("verification_status", verification_status)
        if since:
            clauses.append("m.updated_at >= ?"), params.append(to_utc_iso(since))
        if until:
            clauses.append("m.created_at <= ?"), params.append(to_utc_iso(until))
        if valid_at:
            t = to_utc_iso(valid_at)
            clauses.append("(m.valid_from IS NULL OR m.valid_from <= ?) AND (m.valid_to IS NULL OR m.valid_to > ?)")
            params += [t, t]
        if tag:
            clauses.append("EXISTS (SELECT 1 FROM json_each(m.tags) WHERE json_each.value = ?)"), params.append(tag)
        if min_confidence is not None:
            clauses.append("m.confidence >= ?"), params.append(float(min_confidence))
        if ids is not None:
            ids = list(ids)
            clauses.append(f"m.memory_id IN ({','.join('?' * len(ids)) or 'NULL'})"), params.extend(ids)
        for key, value in (data or {}).items():
            if not re.fullmatch(r"\w+", key):
                raise ValueError(f"bad data key {key!r}")
            clauses.append(f"json_extract(m.data, '$.{key}') = ?"), params.append(value)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def find(self, limit: int = 50, order: str = "updated", **filters) -> List[MemoryRecord]:
        """Metadata filtering: type, category, entity_id, status (None = any), source_type,
        verification_status, since / until, valid_at (what was true then), tag, min_confidence, data={...}.
        With valid_at, superseded facts whose window covers that time count too."""
        if filters.get("valid_at") and filters.get("status", "active") == "active":
            filters["status"] = ["active", "superseded"]
        where, params = self._where(**filters)
        sql = f"SELECT m.* FROM memories m{where} ORDER BY {_ORDER.get(order, _ORDER['updated'])} LIMIT ?"
        with self._lock:
            return [self._row_to_record(r) for r in self._conn.execute(sql, params + [int(limit)]).fetchall()]

    def count(self, **filters) -> int:
        where, params = self._where(**filters)
        with self._lock:
            return self._conn.execute(f"SELECT COUNT(*) FROM memories m{where}", params).fetchone()[0]

    def search_text(self, query: str, limit: int = 20, actor: Optional[str] = None, **filters) -> List[MemoryRecord]:
        """Keyword search (SQLite FTS5, BM25-ranked, stemmed). Any word may match;
        records matching more / rarer words rank first. Each result carries
        data['_score'] (higher = better)."""
        tokens = [t for t in _FTS_TOKEN.findall(str(query or "").lower()) if len(t) > 1]
        if not tokens:
            return []
        match = " OR ".join('"' + t.replace('"', '') + '"' for t in dict.fromkeys(tokens))
        where, params = self._where(**filters)
        where = (where + " AND " if where else " WHERE ") + "memories_fts MATCH ?"
        sql = (f"SELECT m.*, bm25(memories_fts) AS score FROM memories_fts JOIN memories m ON m.rowid = memories_fts.rowid"
               f"{where} ORDER BY score LIMIT ?")
        with self._lock:
            rows = self._conn.execute(sql, params + [match, int(limit)]).fetchall()
            if actor:
                with self._tx() as conn:
                    self._log(conn, "search", None, actor, {"results": len(rows)})
        out = []
        for r in rows:
            rec = self._row_to_record(r)
            rec.data["_score"] = -float(r["score"])
            out.append(rec)
        return out

    def find_exact(self, phrase: str, limit: int = 20, **filters) -> List[MemoryRecord]:
        """Memories containing this exact phrase (case-insensitive)."""
        where, params = self._where(**filters)
        where = (where + " AND " if where else " WHERE ") + "instr(lower(m.content), ?) > 0"
        with self._lock:
            rows = self._conn.execute(f"SELECT m.* FROM memories m{where} ORDER BY m.updated_at DESC LIMIT ?",
                                      params + [str(phrase).lower(), int(limit)]).fetchall()
        return [self._row_to_record(r) for r in rows]

    def sources(self, memory_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT source_type, reference, excerpt, added_at FROM memory_sources WHERE memory_id=? "
                "ORDER BY source_id", (memory_id,)).fetchall()]

    def history(self, memory_id: str) -> List[Dict[str, Any]]:
        """Earlier versions of a memory, oldest first, plus what superseded it."""
        with self._lock:
            rows = self._conn.execute("SELECT version, snapshot, reason, changed_at FROM memory_versions "
                                      "WHERE memory_id=? ORDER BY version_id", (memory_id,)).fetchall()
        return [{"version": r["version"], "reason": r["reason"], "changed_at": r["changed_at"],
                 "record": json.loads(r["snapshot"])} for r in rows]

    def lineage(self, memory_id: str) -> List[MemoryRecord]:
        """The chain of corrections from the first statement to the current one."""
        chain, seen = [], set()
        with self._lock:
            cur = memory_id
            while True:  # walk back to the root
                row = self._conn.execute("SELECT memory_id FROM memories WHERE superseded_by=?", (cur,)).fetchone()
                if not row or row[0] in seen:
                    break
                seen.add(row[0])
                cur = row[0]
            seen = set()
            while cur and cur not in seen:
                seen.add(cur)
                row = self._conn.execute("SELECT * FROM memories WHERE memory_id=?", (cur,)).fetchone()
                if not row:
                    break
                chain.append(self._row_to_record(row))
                cur = row["superseded_by"]
        return chain

    def access_log(self, limit: int = 100, memory_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql, params = "SELECT at, action, memory_id, actor, detail FROM memory_access_log", []
        if memory_id:
            sql, params = sql + " WHERE memory_id=?", [memory_id]
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql + " ORDER BY log_id DESC LIMIT ?",
                                                        params + [int(limit)]).fetchall()]

    # ── entities & relationships ───────────────────────────────
    def upsert_entity(self, name: str, kind: str = "other", aliases: Iterable[str] = (),
                      entity_id: Optional[str] = None, data: Optional[dict] = None) -> str:
        name = str(name).strip()
        existing = self.find_entity(name) if not entity_id else self.get_entity(entity_id)
        now = utc_now()
        with self._tx() as conn:
            if existing:
                merged = sorted(set(existing["aliases"]) | {a.strip() for a in aliases if a and a.strip()} - {existing["name"]})
                conn.execute("UPDATE entities SET aliases=?, data=?, updated_at=? WHERE entity_id=?",
                             (json.dumps(merged), json.dumps({**existing["data"], **(data or {})}), now,
                              existing["entity_id"]))
                return existing["entity_id"]
            eid = entity_id or new_id("ent")
            conn.execute("INSERT INTO entities(entity_id, kind, name, aliases, data, created_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?)", (eid, kind, name, json.dumps(sorted(set(aliases) - {name})),
                                                    json.dumps(data or {}), now, now))
            return eid

    @staticmethod
    def _entity_row(r) -> Dict[str, Any]:
        return {"entity_id": r["entity_id"], "kind": r["kind"], "name": r["name"],
                "aliases": json.loads(r["aliases"]), "data": json.loads(r["data"])}

    def get_entity(self, entity_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._conn.execute("SELECT * FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
        return self._entity_row(r) if r else None

    def find_entity(self, name: str) -> Optional[Dict[str, Any]]:
        """By name or alias, case-insensitive."""
        n = str(name).strip().lower()
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM entities WHERE lower(name)=? OR EXISTS "
                "(SELECT 1 FROM json_each(entities.aliases) WHERE lower(json_each.value)=?) LIMIT 1", (n, n)).fetchone()
        return self._entity_row(r) if r else None

    def entities(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        sql, params = "SELECT * FROM entities", []
        if kind:
            sql, params = sql + " WHERE kind=?", [kind]
        with self._lock:
            return [self._entity_row(r) for r in self._conn.execute(sql + " ORDER BY name", params).fetchall()]

    def relate(self, src_entity: str, relation: str, dst_entity: str, memory_id: Optional[str] = None,
               confidence: float = 1.0, valid_from=None, valid_to=None) -> str:
        """Link two entities. Relationships come from stored facts (memory_id), not guesses."""
        with self._tx() as conn:
            row = conn.execute("SELECT rel_id FROM relationships WHERE src_entity=? AND relation=? AND dst_entity=?",
                               (src_entity, relation, dst_entity)).fetchone()
            if row:
                conn.execute("UPDATE relationships SET memory_id=COALESCE(?, memory_id), confidence=?, valid_from=?, "
                             "valid_to=? WHERE rel_id=?", (memory_id, confidence, to_utc_iso(valid_from),
                                                           to_utc_iso(valid_to), row[0]))
                return row[0]
            rid = new_id("rel")
            conn.execute("INSERT INTO relationships(rel_id, src_entity, relation, dst_entity, memory_id, confidence, "
                         "valid_from, valid_to, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (rid, src_entity, relation, dst_entity, memory_id, confidence, to_utc_iso(valid_from),
                          to_utc_iso(valid_to), utc_now()))
            return rid

    def relations(self, entity_id: str, valid_at=None) -> List[Dict[str, Any]]:
        sql = ("SELECT r.*, s.name AS src_name, d.name AS dst_name FROM relationships r "
               "JOIN entities s ON s.entity_id=r.src_entity JOIN entities d ON d.entity_id=r.dst_entity "
               "WHERE (r.src_entity=? OR r.dst_entity=?)")
        params: list = [entity_id, entity_id]
        if valid_at:
            t = to_utc_iso(valid_at)
            sql += " AND (r.valid_from IS NULL OR r.valid_from <= ?) AND (r.valid_to IS NULL OR r.valid_to > ?)"
            params += [t, t]
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # ── sessions ───────────────────────────────────────────────
    def start_session(self, session_id: Optional[str] = None, started_at=None, channel: str = "voice") -> str:
        sid = session_id or new_id("ses")
        with self._tx() as conn:
            conn.execute("INSERT OR IGNORE INTO sessions(session_id, started_at, channel) VALUES (?,?,?)",
                         (sid, to_utc_iso(started_at) or utc_now(), channel))
        return sid

    def add_message(self, session_id: str, role: str, content: str, at=None, target: Optional[str] = None) -> bool:
        """Append a message to a session; the same message twice is stored once."""
        with self._tx() as conn:
            conn.execute("INSERT OR IGNORE INTO sessions(session_id, started_at) VALUES (?,?)",
                         (session_id, to_utc_iso(at) or utc_now()))
            cur = conn.execute("INSERT OR IGNORE INTO session_messages(session_id, role, content, at, target) "
                               "VALUES (?,?,?,?,?)", (session_id, role, str(content), to_utc_iso(at) or utc_now(), target))
            return cur.rowcount > 0

    def end_session(self, session_id: str, ended_at=None, summary: Optional[str] = None,
                    data: Optional[dict] = None):
        with self._tx() as conn:
            conn.execute("UPDATE sessions SET ended_at=COALESCE(?, ended_at), summary=COALESCE(?, summary), "
                         "data=? WHERE session_id=?", (to_utc_iso(ended_at) or utc_now(), summary,
                                                       json.dumps(data or {}, ensure_ascii=False), session_id))

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["data"] = json.loads(d.get("data") or "{}")
        return d

    def messages(self, session_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT role, content, at, target FROM session_messages WHERE session_id=? ORDER BY message_id"
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(sql, (session_id,)).fetchall()]
        return rows[-limit:] if limit else rows

    def recent_sessions(self, limit: int = 10, ended_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sessions" + (" WHERE ended_at IS NOT NULL" if ended_only else "")
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY started_at DESC LIMIT ?", (int(limit),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d.get("data") or "{}")
            out.append(d)
        return out

    def search_messages(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        words = [w for w in _FTS_TOKEN.findall(str(query).lower()) if len(w) > 2]
        if not words:
            return []
        clause = " OR ".join("instr(lower(content), ?) > 0" for _ in words)
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                f"SELECT session_id, role, content, at FROM session_messages WHERE {clause} "
                f"ORDER BY message_id DESC LIMIT ?", words + [int(limit)]).fetchall()]

    # ── maintenance helpers ────────────────────────────────────
    def get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            r = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_meta(self, key: str, value: str):
        with self._tx() as conn:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, str(value)))

    def exists(self, memory_id: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM memories WHERE memory_id=?", (memory_id,)).fetchone() is not None

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            by_type = dict(self._conn.execute(
                "SELECT type, COUNT(*) FROM memories WHERE status='active' GROUP BY type").fetchall())
            by_status = dict(self._conn.execute("SELECT status, COUNT(*) FROM memories GROUP BY status").fetchall())
            q = lambda t: self._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
            return {"active_by_type": by_type, "by_status": by_status, "entities": q("entities"),
                    "relationships": q("relationships"), "sessions": q("sessions"),
                    "messages": q("session_messages"), "schema_version": self.schema_version}

    def backup_to(self, dest: Path | str) -> Path:
        """Consistent online copy of the whole database (SQLite backup API)."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self._conn.backup(target)
            finally:
                target.close()
        return dest

    def raw(self) -> sqlite3.Connection:
        """For maintenance code in this package only (integrity checks, export)."""
        return self._conn
