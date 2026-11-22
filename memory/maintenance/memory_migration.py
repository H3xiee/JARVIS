"""One-time import of the legacy JSON memory stores into the unified database.

Sources (all read-only here; the legacy files keep working as before):
  data/jarvis_memory.json     rules, speech habits, lessons, skills, history log, salutation
  data/session_history.json   current + past conversation sessions
  cases/.lessons/lessons.json false-positive lessons

Every migrated record gets a deterministic id derived from where it came
from, so running the migration again (or after the legacy file grew) only adds
what is new. Schema upgrades themselves live in memory_schema.MIGRATIONS.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from memory.core import memory_config
from memory.core.memory_manager import MemoryManager
from memory.core.memory_types import MemoryType, SourceType, to_utc_iso

logger = logging.getLogger(__name__)

ROOT = memory_config.ROOT
LEGACY_MEMORY = ROOT / "data" / "jarvis_memory.json"
LEGACY_SESSIONS = ROOT / "data" / "session_history.json"
LEGACY_LESSONS = ROOT / "cases" / ".lessons" / "lessons.json"

OPERATOR = "operator"
_SYSTEM_DEFAULT_PATTERNS = {
    "maintain understated british elegance, razor wit, and mathematical precision.",
    "stt acoustic tuning for jarvis wake recognition.",
}


def legacy_id(*parts: Any) -> str:
    return "mig_" + hashlib.sha256("\x1f".join(map(str, parts)).encode("utf-8")).hexdigest()[:20]


def legacy_session_id(started_at: Any) -> str:
    return "ses_" + hashlib.sha256(str(started_at).encode("utf-8")).hexdigest()[:16]


def _read(path: Path) -> Optional[Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _put(mm: MemoryManager, counts: Dict[str, int], key: str, mid: str, **kw) -> None:
    if mm.repo.exists(mid):
        return
    before = mm.repo.count(status=None)
    mm.remember(memory_id=mid, source_type=kw.pop("source_type", SourceType.IMPORT.value), actor="migration", **kw)
    if mm.repo.count(status=None) > before:
        counts[key] = counts.get(key, 0) + 1


def migrate_jarvis_memory(mm: MemoryManager, path: Path = LEGACY_MEMORY) -> Dict[str, int]:
    data, counts = _read(path), {}
    if not isinstance(data, dict):
        return counts
    ref = f"legacy:{Path(path).name}"
    for rule in data.get("custom_rules", []) or []:
        _put(mm, counts, "rules", legacy_id(ref, "rule", rule), content=str(rule), type=MemoryType.SEMANTIC,
             category="operator_rule", entity=OPERATOR, entity_kind="person", source_type=SourceType.USER_STATED.value,
             verified=True, importance=0.9, reference=ref, tags=["rule"])
    for pat in data.get("user_speech_patterns", []) or []:
        text = str(pat)
        if text.lower().startswith(("operator rule / phrase definition:", "always address the operator as")):
            continue  # mirrors of custom_rules / the salutation, which are migrated as themselves
        system = text.lower() in _SYSTEM_DEFAULT_PATTERNS
        _put(mm, counts, "habits", legacy_id(ref, "habit", text), content=text, type=MemoryType.SEMANTIC,
             category="persona" if system else "speech_pattern", entity=None if system else OPERATOR,
             entity_kind="person", source_type=SourceType.SYSTEM.value if system else SourceType.IMPORT.value,
             verified=system, importance=0.4, reference=ref)
    sal = data.get("operator_salutation")
    if sal:
        _put(mm, counts, "preferences", legacy_id(ref, "salutation"), content=f"Address the operator as '{sal}'.",
             type=MemoryType.SEMANTIC, category="preference", entity=OPERATOR, entity_kind="person",
             source_type=SourceType.USER_CONFIRMED.value, importance=0.9, reference=ref,
             data={"key": "salutation", "value": sal})
    for lesson in data.get("failures_and_lessons", []) or []:
        _put(mm, counts, "lessons", legacy_id(ref, "lesson", lesson), content=str(lesson), type=MemoryType.REFLECTIVE,
             category="lesson", reference=ref, importance=0.6, data={"context": "general"})
    for skill in data.get("learned_skills", []) or []:
        if not isinstance(skill, dict) or not skill.get("name"):
            continue
        _put(mm, counts, "procedures", legacy_id(ref, "skill", skill["name"]),
             content=f"{skill['name']}: {skill.get('description', '')}".strip(": "), type=MemoryType.PROCEDURAL,
             category="skill", source_type=SourceType.SYSTEM.value, verified=True, importance=0.3, reference=ref,
             data={"name": skill["name"], "trigger": skill.get("trigger", ""), "steps": []},
             created_at=skill.get("created_at"))
    for i, h in enumerate(data.get("history_log", []) or []):
        if not isinstance(h, dict) or not h.get("entry"):
            continue
        _put(mm, counts, "episodes", legacy_id(ref, "history", h.get("timestamp"), h.get("entry")),
             content=str(h["entry"]), type=MemoryType.EPISODIC, category=str(h.get("category") or "note"),
             reference=ref, importance=0.3, created_at=h.get("timestamp"), dedupe=False,
             data={"started_at": to_utc_iso(h.get("timestamp"))})
    return counts


def migrate_session_history(mm: MemoryManager, path: Path = LEGACY_SESSIONS) -> Dict[str, int]:
    from narrative.session_memory import summarise_messages  # structured summary, no LLM

    data, counts = _read(path), {"sessions": 0, "messages": 0}
    if not isinstance(data, dict):
        return counts
    sessions = list(data.get("past_sessions", []) or [])
    current = data.get("current_session") or {}
    for s in sessions + ([current] if current.get("messages") else []):
        msgs = s.get("messages", []) or []
        started = s.get("started_at") or (msgs[0].get("at") if msgs else None)
        if not started:
            continue
        sid = s.get("session_id") or legacy_session_id(started)
        if mm.repo.get_session(sid) is None:
            counts["sessions"] += 1
        mm.repo.start_session(sid, started_at=started, channel="voice")
        for m in msgs:
            if m.get("content"):
                counts["messages"] += mm.repo.add_message(sid, m.get("role", "user"), m["content"], at=m.get("at"),
                                                          target=m.get("target"))
        if s is not current:
            summary = summarise_messages(msgs)
            mm.repo.end_session(sid, ended_at=s.get("ended_at"), summary=summary["text"], data=summary)
    return counts


def migrate_lessons(mm: MemoryManager, path: Path = LEGACY_LESSONS) -> Dict[str, int]:
    data, counts = _read(path), {}
    if not isinstance(data, list):
        return counts
    for e in data:
        if not isinstance(e, dict) or not e.get("lesson"):
            continue
        platform = str(e.get("platform") or "general").lower()
        _put(mm, counts, "lessons", lesson_memory_id(e.get("trigger", ""), platform), content=str(e["lesson"]),
             type=MemoryType.REFLECTIVE, category="lesson", reference=f"legacy:{Path(path).name}", importance=0.6,
             created_at=e.get("timestamp"), data={"trigger": e.get("trigger", ""), "platform": platform,
                                                  "context": e.get("context", "general")})
    return counts


def lesson_memory_id(trigger: str, platform: str) -> str:
    """Stable id for a (trigger, platform) lesson, shared with LessonsStore's vector index."""
    from memory.lessons_store import lesson_id
    return lesson_id(trigger, platform)


def migrate_all(mm: MemoryManager, memory_path: Path = LEGACY_MEMORY, sessions_path: Path = LEGACY_SESSIONS,
                lessons_path: Path = LEGACY_LESSONS) -> Dict[str, Dict[str, int]]:
    result = {"jarvis_memory": migrate_jarvis_memory(mm, memory_path),
              "session_history": migrate_session_history(mm, sessions_path),
              "lessons": migrate_lessons(mm, lessons_path)}
    mm.repo.set_meta("legacy_migrated_at", json.dumps(result))
    return result


_done = set()


def ensure_migrated(mm: MemoryManager) -> None:
    """Run the legacy import once per database (and once per process)."""
    key = str(mm.repo.path)
    if key in _done:
        return
    _done.add(key)
    if memory_config.is_default_db(mm.repo.path):  # the real installation: daily upkeep in the background
        from memory.maintenance.memory_recovery import maintain_in_background
        maintain_in_background(mm)
    if mm.repo.get_meta("legacy_migrated_at"):
        return
    try:
        from memory.maintenance.memory_backup import backup
        if memory_config.is_default_db(mm.repo.path):
            backup(mm, label="pre-migration")
        migrate_all(mm)
    except Exception as exc:  # never break JARVIS over the migration; it retries next start
        _done.discard(key)
        logger.warning("[memory] legacy migration failed: %s", exc)
