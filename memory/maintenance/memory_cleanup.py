"""Retention, decay and deletion.

retention(mm)        apply the retention policy (memory_config.RetentionPolicy):
                     expired memories marked, expired working notes removed,
                     raw conversation messages older than the window deleted
                     once their session has a summary, old access-log rows
                     pruned, unconfirmed inferences that were never reinforced
                     lose confidence and are archived when they fade, and old
                     routine run records are archived.
forget_everywhere()  delete something from every place JARVIS keeps it: the
                     memory database (records, versions, sources, vectors,
                     search index, conversation messages, session summaries),
                     the legacy JSON stores, the lessons vector store, the
                     conversation state and history archive files, and on
                     request the backups.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from memory.core.memory_config import RETENTION, ROOT, RetentionPolicy

logger = logging.getLogger(__name__)

DECAY_AFTER_DAYS = 180
DECAY_STEP = 0.15
FADED = 0.3
ROUTINE_RUN_DAYS = 180


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def retention(mm, policy: RetentionPolicy = RETENTION, now: Optional[datetime] = None) -> Dict[str, int]:
    now = now or datetime.now(timezone.utc)
    repo, conn = mm.repo, mm.repo.raw()
    report = {"expired": repo.expire(_iso(now))}

    with repo._tx():
        # expired working notes are scratch: remove them outright
        rows = conn.execute("SELECT memory_id FROM memories WHERE type='working' AND status='expired'").fetchall()
        for (mid,) in rows:
            conn.execute("DELETE FROM memories WHERE memory_id=?", (mid,))
        report["working_removed"] = len(rows)

        # raw messages past the window, only for sessions that are summarised
        cutoff = _iso(now - timedelta(days=policy.session_messages_days))
        cur = conn.execute("DELETE FROM session_messages WHERE at < ? AND session_id IN "
                           "(SELECT session_id FROM sessions WHERE summary IS NOT NULL AND summary != '')", (cutoff,))
        report["messages_removed"] = cur.rowcount

        log_cut = _iso(now - timedelta(days=policy.access_log_days))
        report["log_pruned"] = conn.execute("DELETE FROM memory_access_log WHERE at < ?", (log_cut,)).rowcount

    # unconfirmed inferences fade unless something reinforces them
    stale = _iso(now - timedelta(days=DECAY_AFTER_DAYS))
    faded = decayed = 0
    for rec in repo.find(verification_status=["inferred", "unverified"], source_type=["agent", "tool_result"],
                         type=["semantic", "reflective"], limit=100000):
        if rec.updated_at >= stale:
            continue
        conf = round(rec.confidence - DECAY_STEP, 3)
        if conf < FADED:
            repo.update(rec.memory_id, actor="retention", reason="faded: never confirmed", status="archived",
                        confidence=max(conf, 0.0))
            faded += 1
        else:
            repo.update(rec.memory_id, actor="retention", reason="decay: not reinforced", confidence=conf)
            decayed += 1
    report["decayed"], report["faded"] = decayed, faded

    # old routine run records (successes nobody looked back at) are archived
    old = _iso(now - timedelta(days=ROUTINE_RUN_DAYS))
    archived = 0
    for rec in repo.find(type="episodic", category=["agent_run", "proactive_run"], tag="outcome:success",
                         until=old, limit=100000):
        if rec.importance < 0.5:
            repo.update(rec.memory_id, actor="retention", reason="old routine run", status="archived")
            archived += 1
    report["runs_archived"] = archived
    with repo._tx() as c:
        repo._log(c, "retention", None, "retention", report)
    return report


# ── forgetting ─────────────────────────────────────────────────
LEGACY_FILES = {
    "jarvis_memory": ROOT / "data" / "jarvis_memory.json",
    "jarvis_memory_archive": ROOT / "data" / "jarvis_memory.history_archive.jsonl",
    "session_history": ROOT / "data" / "session_history.json",
    "conversation_state": ROOT / "data" / "conversation_state.json",
    "lessons": ROOT / "cases" / ".lessons" / "lessons.json",
}


def _contains(value: Any, needle: str) -> bool:
    return needle in json.dumps(value, ensure_ascii=False, default=str).lower()


def _scrub(value: Any, needle: str):
    """value without the parts that mention needle (lists filtered; dicts dropped when they mention it)."""
    if isinstance(value, list):
        return [_scrub(v, needle) for v in value if not _contains(v, needle)]
    if isinstance(value, dict):
        return {k: _scrub(v, needle) for k, v in value.items()
                if not (isinstance(v, str) and needle in v.lower())}
    return value


def _rewrite_json(path: Path, needle: str) -> int:
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    if needle not in text.lower():
        return 0
    if path.suffix == ".jsonl":
        lines = text.splitlines()
        keep = [l for l in lines if needle not in l.lower()]
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
        tmp.replace(path)
        return len(lines) - len(keep)
    data = json.loads(text)
    cleaned = _scrub(data, needle)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cleaned, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return 1


def preview_forget(mm, text: Optional[str] = None, entity: Optional[str] = None) -> Dict[str, Any]:
    """What forget_everywhere would remove, without removing anything."""
    ids = _targets(mm, text, entity)
    msgs = _message_ids(mm, text, entity)
    return {"memories": [mm.get(i).content for i in ids][:20], "memory_count": len(ids), "messages": len(msgs)}


def _needles(mm, text: Optional[str], entity: Optional[str]) -> List[str]:
    out = []
    if text and len(text.strip()) >= 3:
        out.append(text.strip().lower())
    if entity:
        ent = mm.repo.find_entity(entity) or mm.retriever.resolver.resolve(entity)
        names = [entity] + ([ent["name"], *ent["aliases"]] if ent else [])
        out += [n.strip().lower() for n in names if n and len(n.strip()) >= 3]
    return list(dict.fromkeys(out))


def _targets(mm, text, entity) -> List[str]:
    ids: List[str] = []
    if entity:
        ent = mm.repo.find_entity(entity) or mm.retriever.resolver.resolve(entity)
        if ent:
            ids += [r.memory_id for r in mm.repo.find(entity_id=ent["entity_id"], status=None, limit=100000)]
    for n in _needles(mm, text, entity):
        ids += [r.memory_id for r in mm.repo.find_exact(n, limit=100000, status=None)]
    return list(dict.fromkeys(ids))


def _message_ids(mm, text, entity) -> List[int]:
    needles = _needles(mm, text, entity)
    if not needles:
        return []
    clause = " OR ".join("instr(lower(content), ?) > 0" for _ in needles)
    with mm.repo._lock:
        return [r[0] for r in mm.repo.raw().execute(f"SELECT message_id FROM session_messages WHERE {clause}",
                                                    needles).fetchall()]


def forget_everywhere(mm, text: Optional[str] = None, entity: Optional[str] = None,
                      memory_ids: Iterable[str] = (), include_legacy: Optional[bool] = None,
                      purge_backups: bool = False, actor: str = "user") -> Dict[str, Any]:
    """Delete a memory (by id), everything mentioning `text`, and/or everything about `entity`."""
    from memory.core.memory_config import is_default_db
    if include_legacy is None:  # only the real installation's legacy files, never from a test or copy
        include_legacy = is_default_db(mm.repo.path)
    resolved_entity = (mm.repo.find_entity(entity) or mm.retriever.resolver.resolve(entity)) if entity else None
    needles = _needles(mm, text, entity)
    for mid in memory_ids:
        rec = mm.get(mid)
        if rec:
            needles.append(rec.content.lower())
    ids = list(dict.fromkeys(list(memory_ids) + _targets(mm, text, entity)))
    report: Dict[str, Any] = {"memories": 0, "messages": 0, "session_summaries": 0, "entities": 0, "legacy": {},
                              "backups_removed": 0}
    for mid in ids:
        report["memories"] += mm.repo.delete(mid, actor=actor, reason="operator asked to forget")
    conn = mm.repo.raw()
    with mm.repo._tx():
        msg_ids = _message_ids(mm, text, entity)
        for m in msg_ids:
            conn.execute("DELETE FROM session_messages WHERE message_id=?", (m,))
        report["messages"] = len(msg_ids)
        for n in needles:
            rows = conn.execute("SELECT session_id, summary, data FROM sessions WHERE instr(lower(COALESCE(summary,'')),?)>0 "
                                "OR instr(lower(data),?)>0", (n, n)).fetchall()
            for sid, _, data in rows:
                conn.execute("UPDATE sessions SET summary='[removed at the operator''s request]', data=? "
                             "WHERE session_id=?", (json.dumps(_scrub(json.loads(data or "{}"), n)), sid))
                report["session_summaries"] += 1
        if entity:
            ent = resolved_entity
            if ent:
                conn.execute("DELETE FROM entities WHERE entity_id=?", (ent["entity_id"],))
                report["entities"] = 1
        # the access log keeps that something was forgotten, never what
        for n in needles:
            conn.execute("UPDATE memory_access_log SET detail=NULL WHERE instr(lower(COALESCE(detail,'')), ?) > 0", (n,))
        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('optimize')")
        mm.repo._log(conn, "forget_everywhere", None, actor, {"memories": report["memories"],
                                                              "messages": report["messages"]})
    if include_legacy:
        report["legacy"]["live"] = _forget_live(needles)  # running stores rewrite their files from memory
        for name, path in LEGACY_FILES.items():
            n = sum(_rewrite_json(path, needle) for needle in needles)
            if n:
                report["legacy"][name] = n
        report["legacy"]["lessons_vectors"] = _forget_lesson_vectors(needles)
    if purge_backups:
        report["backups_removed"] = _purge_backups(mm)
    try:  # free pages so deleted text doesn't linger in the file
        with mm.repo._lock:
            conn.execute("VACUUM")
    except Exception:
        pass
    return report


def _forget_live(needles: List[str]) -> int:
    """Conversation history and context held by running JARVIS components."""
    n = 0
    try:
        from narrative.session_memory import live_sessions
        from core.conversation_context import live_contexts
        for obj in list(live_sessions()) + list(live_contexts()):
            for needle in needles:
                n += obj.forget(needle)
    except Exception:
        pass
    return n


def _forget_lesson_vectors(needles: List[str]) -> int:
    try:
        from memory.lessons_store import LessonsStore
        store = LessonsStore()
        if not (store._available and store._collection is not None):
            return 0
        got = store._collection.get(include=["documents"])
        drop = [i for i, d in zip(got["ids"], got["documents"]) if any(n in (d or "").lower() for n in needles)]
        if drop:
            store._collection.delete(ids=drop)
        return len(drop)
    except Exception:
        return 0


def _purge_backups(mm) -> int:
    """Backups hold the old data: replace them all with one fresh backup taken after the deletion."""
    from memory.maintenance.memory_backup import backup, backup_dir, list_backups
    old = list_backups(backup_dir(mm))
    fresh = backup(mm, label="after-forget", keep=len(old) + 1)
    removed = 0
    for p in old:
        if p != fresh:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed
