"""Memory consolidation: turn raw conversation and experience into structured knowledge.

    report = get_memory().consolidate()          # everything new since the last run
    report = get_memory().consolidate("ses_...") # one session (e.g. when it ends)

1. Collect      operator messages not consolidated yet, ended sessions without
                an episode, recent episodes and procedures.
2. Extract      candidate facts, preferences, decisions, commitments, project
                states, rules, lessons and corrections (memory_extractor).
3. Validate     grounded in the operator's words, no secrets, important enough
                (importance_scorer), not a duplicate, contradictions checked.
4. Consolidate  each candidate updates the right store: a changed preference or
                project status supersedes the old one, a correction corrects the
                memory it refers to, a repeated fact reinforces the existing one.
                Sessions become one episode each; fixed failures become lessons
                (memory_reflector); near-duplicates are merged.
5. Persist      with provenance (session:message + the exact words), versions,
                and a checkpoint so nothing is processed twice.
"""

from __future__ import annotations

import calendar
import logging
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from memory.intelligence import contradiction_detector, importance_scorer, memory_extractor, memory_reflector
from memory.intelligence.entity_resolver import EntityResolver
from memory.core.memory_types import MemoryRecord, MemorySource, to_utc_iso

logger = logging.getLogger(__name__)
CHECKPOINT = "consolidated_message_id"
NEAR_DUPLICATE = 0.9

_WEEKDAYS = {d.lower(): i for i, d in enumerate(calendar.day_name)}
_WEEKDAYS.update({d.lower(): i for i, d in enumerate(calendar.day_abbr)})
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})


def parse_due(text: Optional[str], now: Optional[datetime] = None) -> Optional[str]:
    """'by Friday', 'tomorrow at 5pm', 'on 5 December', 'next week', 'in 3 days' -> UTC ISO (local end of day)."""
    if not text:
        return None
    t = text.lower()
    local = (now or datetime.now().astimezone()).astimezone()
    day = None
    if "tomorrow" in t:
        day = local + timedelta(days=1)
    elif re.search(r"\b(?:today|tonight)\b", t):
        day = local
    elif re.search(r"\bnext week\b", t):
        day = local + timedelta(days=7 - local.weekday())
    else:
        m = re.search(r"\bin (\d{1,3}) (day|week)s?\b", t)
        if m:
            day = local + timedelta(days=int(m.group(1)) * (7 if m.group(2) == "week" else 1))
    if day is None:
        m = re.search(r"\b(" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")\b", t)
        if m:
            ahead = (_WEEKDAYS[m.group(1)] - local.weekday()) % 7 or 7
            day = local + timedelta(days=ahead + (7 if "next" in t[:m.start()] else 0))
    if day is None:
        names = "|".join(sorted(_MONTHS, key=len, reverse=True))
        m = (re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({names})\b", t)
             or re.search(rf"\b({names})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t))
        if m:
            a, b = m.groups()
            d, mon = (int(a), _MONTHS[b]) if a.isdigit() else (int(b), _MONTHS[a])
            try:
                day = local.replace(month=mon, day=d)
                if day.date() < local.date():
                    day = day.replace(year=local.year + 1)
            except ValueError:
                day = None
    if day is None:
        return None
    hour, minute = 18, 0
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        minute = int(m.group(2) or 0)
    return to_utc_iso(day.replace(hour=hour, minute=minute, second=0, microsecond=0))


class MemoryConsolidator:
    def __init__(self, mm, use_llm: Optional[bool] = None, llm=None):
        self.mm = mm
        self.repo = mm.repo
        self.resolver = EntityResolver(mm.repo)
        self.use_llm = use_llm
        self.llm = llm
        self._lock = threading.Lock()

    # ── 1. collect ─────────────────────────────────────────────
    def _new_messages(self, session_id: Optional[str]) -> List[Dict[str, Any]]:
        last = int(self.repo.get_meta(CHECKPOINT) or 0)
        sql = ("SELECT message_id, session_id, role, content, at FROM session_messages WHERE message_id > ? "
               "AND role = 'user'")
        params: list = [last]
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        with self.repo._lock:
            return [dict(r) for r in self.repo.raw().execute(sql + " ORDER BY message_id", params).fetchall()]

    # ── run ────────────────────────────────────────────────────
    def run(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"skipped": "already running"}
        try:
            return self._run(session_id)
        finally:
            self._lock.release()

    def _run(self, session_id: Optional[str]) -> Dict[str, Any]:
        report: Dict[str, Any] = {"messages": 0, "stored": [], "skipped": [], "corrected": [], "disputed": [],
                                  "sessions": [], "lessons": [], "insights": [], "merged": 0, "expired": 0}
        messages = self._new_messages(session_id)
        report["messages"] = len(messages)
        for msg in messages:
            ref = f"{msg['session_id']}:{msg['message_id']}"
            for cand in memory_extractor.extract(msg["content"], reference=ref, use_llm=self.use_llm, llm=self.llm):
                self._consolidate(cand, msg, report)
        if messages:
            top = max(m["message_id"] for m in messages)
            if top > int(self.repo.get_meta(CHECKPOINT) or 0):
                self.repo.set_meta(CHECKPOINT, str(top))
        report["sessions"] = self.summarise_sessions(session_id)
        refl = memory_reflector.reflect(self.mm)
        report["lessons"], report["insights"] = refl["lessons"], refl["insights"]
        report["merged"] = self.merge_near_duplicates()
        report["expired"] = self.repo.expire()
        report["disputed"] = [r.memory_id for r in contradiction_detector.disputes(self.mm)]
        with self.repo._tx() as conn:
            self.repo._log(conn, "consolidate", None, "consolidation",
                           {k: (len(v) if isinstance(v, list) else v) for k, v in report.items()})
        return report

    # ── 2-4. one candidate ─────────────────────────────────────
    def _consolidate(self, c, msg: Dict[str, Any], report: Dict[str, Any]):
        mentioned = self.resolver.mentions(c.content)
        if not importance_scorer.worth_storing(c, known_entity=bool(mentioned)):
            report["skipped"].append({"kind": c.kind, "content": c.content, "why": "not important enough"})
            return
        importance = importance_scorer.score(c, known_entity=bool(mentioned))
        when = msg.get("at")
        common = dict(source_type=c.source_type, reference=c.reference, excerpt=c.evidence[:300],
                      importance=importance, created_at=when, actor="consolidation")
        try:
            rec = self._apply(c, mentioned, common, report)
        except Exception as exc:
            logger.warning("[memory] consolidation of one item failed: %s", exc)
            report["skipped"].append({"kind": c.kind, "content": c.content, "why": f"error {type(exc).__name__}"})
            return
        if rec is not None:
            if c.meta.get("private") and "private" not in rec.tags:
                rec = self.repo.update(rec.memory_id, actor="consolidation", reason="operator asked to keep it private",
                                       tags=sorted(set(rec.tags) | {"private"}))
            report["stored"].append({"kind": c.kind, "memory_id": rec.memory_id, "content": rec.content,
                                     "private": "private" in rec.tags})

    def _entity_for(self, c, mentioned) -> Optional[str]:
        if c.entity:
            hit = self.resolver.resolve(c.entity)
            return hit["name"] if hit else c.entity
        if mentioned:
            return mentioned[0]["name"]
        focus = self.mm.working.focus()
        return focus["what"] if focus and c.kind in ("decision", "project_state", "lesson") else None

    def _apply(self, c, mentioned, common, report) -> Optional[MemoryRecord]:
        mm = self.mm
        kind = c.kind
        entity = self._entity_for(c, mentioned)
        if kind == "correction":
            return self._correct(c, common, report)
        if kind == "preference" and c.key:
            return mm.semantic.set_preference(c.key, c.value or c.content, text=c.content, source=c.source_type,
                                              reference=c.reference)
        if kind == "commitment":
            due = parse_due(c.due)
            pending = [p for p in mm.prospective.pending()
                       if re.sub(r"\W+", " ", p.content.lower()).strip() == re.sub(r"\W+", " ", c.content.lower()).strip()]
            if pending:
                return mm.prospective.reschedule(pending[0].memory_id, due) if due else pending[0]
            return mm.prospective.commit(c.content, due_at=due, owner=c.meta.get("owner", "operator"),
                                         counterpart=entity if entity and entity != "operator" else None,
                                         source=c.source_type, reference=c.reference)
        if kind == "project_state":
            return self._project_state(c, entity, common)
        if kind == "lesson":
            return mm.reflective.learn(c.content, context=entity or "general", source=c.source_type,
                                       reference=c.reference, confidence=c.confidence)
        if kind == "rule":
            return mm.remember(c.content, type="semantic", category="operator_rule", entity="operator",
                               entity_kind="person", verified=True, tags=["rule"], **common)
        if kind == "decision":
            return mm.remember(c.content, type="semantic", category="decision", entity=entity,
                               entity_kind="project", confidence=c.confidence, **common)
        if c.subject and c.predicate and c.object:
            return mm.semantic.assert_fact(c.subject, c.predicate, c.object, subject_kind=c.entity_kind,
                                           source=c.source_type, reference=c.reference, confidence=c.confidence,
                                           importance=common["importance"], text=c.content)
        return mm.remember(c.content, type="semantic", category="fact", entity=entity, entity_kind=c.entity_kind,
                           confidence=c.confidence, verified=c.source_type == "user_confirmed" or None, **common)

    def _project_state(self, c, entity, common) -> MemoryRecord:
        name = entity or "project"
        ent = self.repo.find_entity(name)
        prior = self.repo.find(type="semantic", category="project_state", entity_id=ent["entity_id"], limit=5) \
            if ent else []
        if prior and prior[0].content.lower() == c.content.lower():
            return prior[0]
        if prior:
            new = MemoryRecord(content=c.content, type="semantic", category="project_state",
                               entity_id=prior[0].entity_id, source=MemorySource(c.source_type, c.reference,
                                                                                 c.evidence[:300]),
                               importance=common["importance"], confidence=c.confidence)
            return self.repo.supersede(prior[0].memory_id, new, actor="consolidation", reason="project status changed")
        return self.mm.remember(c.content, type="semantic", category="project_state", entity=name,
                                entity_kind="project", confidence=c.confidence, **common)

    def _correct(self, c, common, report) -> Optional[MemoryRecord]:
        """Find the memory the operator is correcting and correct it; if none, remember the right version."""
        target = None
        if c.old_value:
            hits = self.repo.find_exact(c.old_value, limit=10, status="active")
            hits = [h for h in hits if h.type in ("semantic", "prospective", "procedural")]
            if hits:
                scored = self.mm.retriever.retrieve(c.content, k=5, filters={"ids": [h.memory_id for h in hits]})
                target = scored[0].record if scored else hits[0]
        if target is None:
            for r in self.mm.retriever.retrieve(c.content, k=5):
                rec = r.record
                if rec.type != "semantic" or rec.content.lower() == c.content.lower():
                    continue
                same_shape = contradiction_detector.signature(rec.content)[0] == \
                    contradiction_detector.signature(c.content)[0]
                if same_shape or (r.relevance >= 0.6 and rec.entity_id):
                    target = rec
                    break
        if target is None:
            return self.mm.remember(c.content, type="semantic", category="fact", verified=True,
                                    **{**common, "source_type": "user_confirmed"})
        fixed = self.repo.correct(target.memory_id, c.content, reference=c.reference)
        report["corrected"].append({"was": target.content, "now": fixed.content, "memory_id": fixed.memory_id})
        return fixed

    # ── sessions → episodes ────────────────────────────────────
    def summarise_sessions(self, session_id: Optional[str] = None) -> List[str]:
        """One episodic memory per ended conversation (its structured summary)."""
        from narrative.session_memory import summarise_messages
        out = []
        sessions = [self.repo.get_session(session_id)] if session_id else self.repo.recent_sessions(50, True)
        for s in sessions:
            if not s or not s.get("ended_at") or s["data"].get("episode_id"):
                continue
            msgs = self.repo.messages(s["session_id"])
            summary = s["data"] if s["data"].get("requests") else summarise_messages(msgs)
            if not summary.get("requests"):
                continue
            rec = self.mm.episodic.record(f"Conversation: {summary.get('text') or s.get('summary')}",
                                          outcome="unknown", category="conversation", session_id=s["session_id"],
                                          started_at=s["started_at"], ended_at=s["ended_at"], source="agent",
                                          reference=f"session:{s['session_id']}",
                                          details={"topics": summary.get("topics", []),
                                                   "targets": summary.get("targets", []),
                                                   "user_turns": summary.get("user_turns")})
            data = {**s["data"], **summary, "episode_id": rec.memory_id}
            self.repo.end_session(s["session_id"], ended_at=s["ended_at"], summary=summary.get("text"), data=data)
            out.append(rec.memory_id)
        return out

    # ── near-duplicates ────────────────────────────────────────
    def merge_near_duplicates(self, threshold: float = NEAR_DUPLICATE) -> int:
        """Same thing said in slightly different words about the same entity: keep the better one."""
        from memory.retrieval.semantic_search import HashEmbedder
        emb = HashEmbedder()
        merged = 0
        rows = self.repo.find(type="semantic", limit=5000)
        groups: Dict[tuple, List[MemoryRecord]] = {}
        for r in rows:
            groups.setdefault((r.entity_id, r.category), []).append(r)
        for group in groups.values():
            if len(group) < 2:
                continue
            vecs = emb.embed([g.content for g in group])
            gone = set()
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    if a.memory_id in gone or b.memory_id in gone:
                        continue
                    if float(vecs[i] @ vecs[j]) < threshold or contradiction_detector.conflicts(a, b):
                        continue
                    if contradiction_detector.signature(a.content)[1] != contradiction_detector.signature(b.content)[1]:
                        continue  # "standup at 9:30" vs "standup at 10:30": different values, not a duplicate
                    keep, drop = sorted((a, b), key=lambda r: (contradiction_detector.TRUST.get(r.source.type, 1),
                                                               r.confidence, r.updated_at), reverse=True)
                    self._merge(keep, drop)
                    gone.add(drop.memory_id)
                    merged += 1
        return merged

    def _merge(self, keep: MemoryRecord, drop: MemoryRecord):
        with self.repo._tx() as conn:
            for s in self.repo.sources(drop.memory_id):
                conn.execute("INSERT INTO memory_sources(memory_id, source_type, reference, excerpt, added_at) "
                             "VALUES (?,?,?,?,?)", (keep.memory_id, s["source_type"], s["reference"], s["excerpt"],
                                                    s["added_at"]))
            cur = self.repo._get(conn, drop.memory_id)
            self.repo._snapshot(conn, cur, "near-duplicate merged")
            cur.status, cur.version = "superseded", cur.version + 1
            cur.data["duplicate_of"] = keep.memory_id
            self.repo._write_row(conn, cur, superseded_by=keep.memory_id, insert=False)
            self.repo._log(conn, "merge", drop.memory_id, "consolidation", {"into": keep.memory_id})


def consolidate_in_background(mm, session_id: Optional[str] = None) -> threading.Thread:
    t = threading.Thread(target=lambda: _safe_run(mm, session_id), name="memory-consolidation", daemon=True)
    t.start()
    return t


def _safe_run(mm, session_id):
    try:
        mm.consolidator.run(session_id)
    except Exception as exc:
        logger.warning("[memory] consolidation failed: %s", exc)
