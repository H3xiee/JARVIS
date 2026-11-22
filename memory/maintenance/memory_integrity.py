"""Consistency checks for the memory database. `check()` reports, `repair()` fixes what is safe to fix."""

from __future__ import annotations

from typing import Any, Dict, List

from memory.core.memory_types import content_hash


def check(mm) -> Dict[str, Any]:
    conn = mm.repo.raw()
    problems: List[str] = []
    with mm.repo._lock:
        sqlite_ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if sqlite_ok != "ok":
            problems.append(f"sqlite: {sqlite_ok}")
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            problems.append(f"{len(fk)} rows point at missing parents")
        dangling = conn.execute("SELECT COUNT(*) FROM memories m WHERE superseded_by IS NOT NULL AND NOT EXISTS "
                                "(SELECT 1 FROM memories n WHERE n.memory_id = m.superseded_by)").fetchone()[0]
        if dangling:
            problems.append(f"{dangling} memories superseded by a missing memory")
        orphans = conn.execute("SELECT COUNT(*) FROM memories m WHERE entity_id IS NOT NULL AND NOT EXISTS "
                               "(SELECT 1 FROM entities e WHERE e.entity_id = m.entity_id)").fetchone()[0]
        if orphans:
            problems.append(f"{orphans} memories about a missing entity")
        bad_hash = [r["memory_id"] for r in conn.execute(
            "SELECT memory_id, type, category, entity_id, content, content_hash FROM memories")
            if content_hash(r["type"], r["category"], r["entity_id"], r["content"]) != r["content_hash"]]
        if bad_hash:
            problems.append(f"{len(bad_hash)} memories with a stale duplicate key")
        fts = conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        if fts != rows:
            problems.append(f"search index has {fts} rows for {rows} memories")
    return {"ok": not problems, "problems": problems}


def repair(mm) -> Dict[str, Any]:
    conn = mm.repo.raw()
    with mm.repo._tx():
        conn.execute("UPDATE memories SET superseded_by = NULL WHERE superseded_by IS NOT NULL AND NOT EXISTS "
                     "(SELECT 1 FROM memories n WHERE n.memory_id = memories.superseded_by)")
        conn.execute("UPDATE memories SET entity_id = NULL WHERE entity_id IS NOT NULL AND NOT EXISTS "
                     "(SELECT 1 FROM entities e WHERE e.entity_id = memories.entity_id)")
        for r in conn.execute("SELECT memory_id, type, category, entity_id, content FROM memories").fetchall():
            conn.execute("UPDATE memories SET content_hash=? WHERE memory_id=?",
                         (content_hash(r["type"], r["category"], r["entity_id"], r["content"]), r["memory_id"]))
        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
    return check(mm)
