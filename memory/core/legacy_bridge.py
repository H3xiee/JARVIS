"""Compatibility adapters: the legacy stores (JarvisMemory, SessionMemory,
LessonsStore) keep their JSON files and APIs, and mirror every write into the
unified database through here.

Mirroring only happens for the real, default legacy files; a legacy store
pointed at a temp file (tests) never touches the memory database. Failures
are swallowed: the legacy path must keep working even if the database can't.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)
_warned = False


def _run(fn: Callable[[Any], Any]) -> Any:
    global _warned
    try:
        from memory.core.memory_manager import get_memory
        from memory.maintenance.memory_migration import ensure_migrated
        mm = get_memory()
        ensure_migrated(mm)
        return fn(mm)
    except Exception as exc:
        if not _warned:
            logger.warning("[memory] mirror to the memory database failed: %s", exc)
            _warned = True
        return None


def remember(**kw):
    return _run(lambda mm: mm.remember(**kw))


def custom_rule(text: str):
    return remember(content=text, type="semantic", category="operator_rule", entity="operator", entity_kind="person",
                    source_type="user_stated", verified=True, importance=0.9, reference="jarvis_memory.custom_rules",
                    tags=["rule"])


def speech_pattern(text: str):
    return remember(content=text, type="semantic", category="speech_pattern", entity="operator",
                    entity_kind="person", source_type="user_stated", importance=0.4,
                    reference="jarvis_memory.user_speech_patterns")


def failure_lesson(text: str):
    return _run(lambda mm: mm.reflective.learn(text, context="general", source="agent",
                                               reference="jarvis_memory.failures_and_lessons"))


def learned_skill(name: str, description: str, trigger: str):
    def go(mm):
        mid = "skl_" + __import__("hashlib").sha256(name.lower().encode()).hexdigest()[:20]
        content = f"{name}: {description}".strip(": ")
        cur = mm.get(mid)
        if cur is None:
            return mm.remember(content=content, type="procedural", category="skill", source_type="system",
                               verified=True, importance=0.3, reference="jarvis_memory.learned_skills",
                               memory_id=mid, data={"name": name, "trigger": trigger, "steps": []})
        if cur.content != content or cur.data.get("trigger") != trigger:
            return mm.repo.update(mid, reason="skill updated", content=content, data={"trigger": trigger})
        return cur
    return _run(go)


def history_entry(category: str, text: str, at: Optional[str] = None):
    return remember(content=text, type="episodic", category=category or "note", source_type="agent",
                    importance=0.3, reference="jarvis_memory.history_log", created_at=at, dedupe=False,
                    data={"started_at": at})


def salutation(value: str):
    """The operator changed how they want to be addressed: supersede the old preference."""
    return _run(lambda mm: mm.semantic.set_preference("salutation", value, text=f"Address the operator as '{value}'.",
                                                      reference="settings.operator_salutation"))


def lesson(trigger: str, lesson_text: str, platform: str, context: str):
    """A LessonsStore lesson: one memory per (trigger, platform), updated in place."""
    def go(mm):
        from memory.maintenance.memory_migration import lesson_memory_id
        mid = lesson_memory_id(trigger, platform)
        data = {"trigger": trigger, "platform": platform, "context": context}
        if mm.get(mid) is None:
            return mm.remember(content=lesson_text, type="reflective", category="lesson", source_type="agent",
                               importance=0.6, reference="lessons_store", memory_id=mid, data=data, dedupe=False)
        return mm.repo.update(mid, reason="lesson updated", content=lesson_text, data=data)
    return _run(go)


def session_message(session_id: str, started_at: str, role: str, content: str, at: str, target: Optional[str]):
    def go(mm):
        mm.repo.start_session(session_id, started_at=started_at)
        return mm.repo.add_message(session_id, role, content, at=at, target=target)
    return _run(go)


def session_end(session_id: str, ended_at: str, summary: dict):
    """A conversation ended: archive it, then consolidate it into long-term memory in the background."""
    def go(mm):
        mm.repo.end_session(session_id, ended_at=ended_at, summary=summary.get("text"), data=summary)
        from memory.intelligence.memory_consolidation import consolidate_in_background
        consolidate_in_background(mm, session_id)
    return _run(go)
