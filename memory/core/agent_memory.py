"""The shared memory interface every agent uses.

    mem = agent_memory("email")
    pkg = mem.context("Reply to Ravi about the budget")    # what this agent should know
    mem.record_run(goal, outcome="success", tools=[...])   # execution history
    mem.remember("Ravi prefers email over calls", category="contact_fact", entity="Ravi")

One database, many views: each agent has a profile saying which kinds of
memory it may read (an agent that frees disk space has no business reading
your contacts' birthdays) and which it cares about most. Whatever an agent
writes is shared: the email agent's commitment shows up for the calendar
agent, a system-ops failure shows up when system ops plans its next run.
Agents never touch SQLite or the vector index; they go through here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from memory.retrieval.context_builder import ContextPackage, build

OPERATOR_SOURCES = frozenset({"user_confirmed", "user_stated", "system"})
AGENT_SOURCES = frozenset({"tool_result", "agent"})  # what an agent may claim as the source of what it writes
PRIVATE = "private"  # tag: the operator asked to keep it private; never put into an agent's context
ALL_TYPES = ("working", "episodic", "semantic", "procedural", "prospective", "reflective")


@dataclass(frozen=True)
class AgentProfile:
    name: str
    types: Tuple[str, ...] = ALL_TYPES
    deny_categories: FrozenSet[str] = frozenset()
    boost_categories: FrozenSet[str] = frozenset()
    own_history: bool = True          # boost this agent's own past runs
    standing: FrozenSet[str] = frozenset({"operator_rule"})  # always relevant to this agent (a few, best first)
    budget_tokens: int = 350
    description: str = ""


_PERSONAL = frozenset({"contact_fact", "personal", "conversation"})

PROFILES: Dict[str, AgentProfile] = {
    "assistant": AgentProfile("assistant", ("semantic", "episodic", "reflective", "prospective", "procedural"),
                              boost_categories=frozenset({"preference", "operator_rule"}),
                              description="preferences, facts, past conversations"),
    "local_research": AgentProfile("local_research", ("semantic", "episodic", "reflective", "procedural"),
                                   boost_categories=frozenset({"project_fact", "project_state", "decision", "fact"}),
                                   description="project knowledge, document history, research episodes"),
    "email": AgentProfile("email", ("semantic", "episodic", "prospective", "reflective"),
                          boost_categories=frozenset({"contact_fact", "preference", "commitment", "operator_rule",
                                                      "communication"}),
                          standing=frozenset({"operator_rule", "email_style"}),
                          description="communication history, contact facts, commitments"),
    "calendar": AgentProfile("calendar", ("semantic", "episodic", "prospective", "reflective"),
                             boost_categories=frozenset({"preference", "schedule", "commitment"}),
                             standing=frozenset({"operator_rule", "schedule"}),
                             description="scheduling preferences, commitments, previous events"),
    "system_ops": AgentProfile("system_ops", ("semantic", "episodic", "procedural", "reflective"),
                               deny_categories=_PERSONAL,
                               boost_categories=frozenset({"system_config", "system_operation", "workflow",
                                                           "procedures", "lesson"}),
                               standing=frozenset({"operator_rule", "system_config"}),
                               description="system configuration, previous operations, recovery procedures"),
    "supervisor": AgentProfile("supervisor", ALL_TYPES,
                               boost_categories=frozenset({"objective", "project_state", "decision", "commitment",
                                                           "preference", "operator_rule"}),
                               budget_tokens=400, description="objectives, task state, agent history"),
}


def enabled() -> bool:
    flag = os.environ.get("JARVIS_AGENT_MEMORY")
    return flag is None or flag.strip() not in ("0", "false", "no", "")


class AgentMemory:
    def __init__(self, mm, agent: str):
        self.mm = mm
        self.agent = agent
        self.profile = PROFILES.get(agent, AgentProfile(agent, ("semantic", "episodic", "reflective"),
                                                        deny_categories=_PERSONAL))

    # ── read ───────────────────────────────────────────────────
    def allowed(self, rec, explicit: bool = False) -> bool:
        """May this agent see this memory? Private memories only when the operator explicitly asks
        the assistant for them, never as background context."""
        if PRIVATE in (rec.tags or []) and not (explicit and self.agent == "assistant"):
            return False
        return rec.type in self.profile.types and rec.category not in self.profile.deny_categories

    def retrieve(self, goal: str, k: int = 8, standing: bool = False):
        ranked = self.mm.retriever.retrieve(goal, k=k * 2, filters={"type": list(self.profile.types)},
                                            agent=self.agent)
        out = []
        for r in ranked:
            if not self.allowed(r.record):
                continue
            boost = 1.0
            if r.record.category in self.profile.boost_categories:
                boost += 0.2
            if self.profile.own_history and r.record.type == "episodic" and r.record.data.get("agent") == self.agent:
                boost += 0.15
            r.score *= boost
            out.append(r)
        out.sort(key=lambda r: -r.score)
        out = out[:k]
        if not standing:
            return out
        return out + self._standing(exclude={r.record.memory_id for r in out}, room=max(0, k + 3 - len(out)))

    STANDING_MAX = 3

    def _standing(self, exclude, room: int):
        """The few memories this agent should always have in mind (its rules, the machine's setup...),
        ranked after the request's own matches."""
        if not self.profile.standing or room <= 0:
            return []
        from memory.retrieval.memory_ranker import RankedMemory
        recs = [r for r in self.mm.repo.find(category=sorted(self.profile.standing), limit=20, order="importance")
                if r.memory_id not in exclude and self.allowed(r)]
        recs.sort(key=lambda r: (-r.importance, -r.confidence))
        return [RankedMemory(r, 0.01, 0.0, r.confidence, ["standing context"]) for r in
                recs[:min(room, self.STANDING_MAX)]]

    def context(self, goal: str, k: int = 8, budget_tokens: Optional[int] = None,
                include_ids: bool = False) -> ContextPackage:
        qa = self.mm.retriever.analyse(goal)
        return build(goal, self.retrieve(goal, k, standing=True), budget_tokens=budget_tokens or self.profile.budget_tokens,
                     include_ids=include_ids, analysis=qa.to_dict(), header=False)

    def context_split(self, goal: str, k: int = 8, budget_tokens: Optional[int] = None) -> Tuple[List[str], str]:
        """(operator-sourced lines, everything else) for a tool loop: what the operator said can ground
        actions; what tools or JARVIS concluded is shown as data only."""
        pkg = self.context(goal, k, budget_tokens)
        if not pkg:
            return [], ""
        trusted, other = [], []
        for item, line in zip(pkg.items, _item_lines(self.mm, pkg.items)):
            (trusted if item["source"] in OPERATOR_SOURCES and item["verification"] != "disputed" else other).append(line)
        return trusted, "\n".join(other)

    def recall(self, query: str, k: int = 6, explicit: bool = False):
        """explicit=True: the operator asked what JARVIS remembers (private memories included for the assistant)."""
        if not explicit:
            return [r.record for r in self.retrieve(query, k)]
        ranked = self.mm.retriever.retrieve(query, k=k * 2, filters={"type": list(self.profile.types)}, agent=self.agent)
        return [r.record for r in ranked if self.allowed(r.record, explicit=True)][:k]

    # ── write ──────────────────────────────────────────────────
    def remember(self, content: str, category: str = "fact", type: str = "semantic", entity: Optional[str] = None,
                 source_type: str = "tool_result", **kw):
        """A fact this agent learned. Shared with every agent allowed to read its kind."""
        from memory.intelligence.memory_extractor import contains_secret
        if contains_secret(content):
            return None
        if source_type not in AGENT_SOURCES:
            source_type = "tool_result"  # an agent can't record something as the operator's own word
        kw.pop("verified", None)
        data = dict(kw.pop("data", {}) or {})
        data.setdefault("agent", self.agent)
        return self.mm.remember(content, type=type, category=category, entity=entity, source_type=source_type,
                                reference=kw.pop("reference", f"agent:{self.agent}"), actor=self.agent, data=data,
                                **kw)

    def record_run(self, goal: str, outcome: str, summary: str = "", tools: Iterable[str] = (),
                   failed_tools: Optional[Dict[str, str]] = None, task_id: Optional[str] = None,
                   origin: str = "user", session_id: Optional[str] = None):
        """Execution history: one episode per agent run, linked to the task and to what it was about."""
        from memory.intelligence.memory_extractor import redact
        goal_text, summary_text = redact(goal)[:300], redact(summary)[:400]
        mentioned = self.mm.retriever.resolver.mentions(goal_text)
        error = None
        if outcome == "failure":
            error = summary_text or "failed"
        elif failed_tools:
            error = "; ".join(f"{t}: {why}" for t, why in list(failed_tools.items())[:3])[:300]
        text = f"{self.agent}: {goal_text}" + (f" -> {summary_text}" if summary_text else "")
        if outcome == "success":
            self._resolve_earlier_failures(goal_text, summary_text, task_id)
        return self.mm.episodic.record(
            text, outcome=outcome, agent=self.agent, goal=goal_text, session_id=session_id,
            entity=mentioned[0]["name"] if mentioned else None,
            category="agent_run" if origin == "user" else "proactive_run",
            details={"task_id": task_id, "tools": list(dict.fromkeys(tools))[:20], "origin": origin,
                     "failed_tools": failed_tools or {}},
            error=error, source="tool_result", reference=f"task:{task_id}" if task_id else f"agent:{self.agent}")

    RESOLVE_WITHIN_DAYS = 3
    SAME_GOAL = 0.6

    def _resolve_earlier_failures(self, goal: str, summary: str, task_id: Optional[str]):
        """The same goal failed recently and now worked: record the success as that failure's fix, so the
        reflector can turn it into a lesson ("When X failed: Y worked")."""
        from datetime import datetime, timedelta, timezone
        from memory.retrieval.semantic_search import HashEmbedder
        since = (datetime.now(timezone.utc) - timedelta(days=self.RESOLVE_WITHIN_DAYS)).isoformat()
        open_failures = [f for f in self.mm.episodic.failures(agent=self.agent, limit=20)
                         if not f.data.get("resolution") and f.created_at >= since[:19]]
        if not open_failures:
            return
        emb = HashEmbedder()
        vecs = emb.embed([goal] + [f.data.get("goal") or f.content for f in open_failures])
        for f, v in zip(open_failures, vecs[1:]):
            if float(v @ vecs[0]) >= self.SAME_GOAL:
                self.mm.episodic.resolve(f.memory_id, f"worked on a later try{f' ({summary[:160]})' if summary else ''}"
                                         + (f" [task:{task_id}]" if task_id else ""))

    def task_note(self, task_id: str, key: str, value: Any):
        """Working memory for one task (draft being edited, folder chosen...), shared by the agents on it."""
        return self.mm.working.set(key, value, scope=f"task:{task_id}", source="agent")

    def task_notes(self, task_id: str) -> Dict[str, Any]:
        return self.mm.working.items(f"task:{task_id}")

    def history(self, limit: int = 10, failures_only: bool = False):
        return self.mm.episodic.failures(agent=self.agent, limit=limit) if failures_only else \
            self.mm.episodic.recent(limit=limit, agent=self.agent)


def _item_lines(mm, items: List[Dict[str, Any]]) -> List[str]:
    from memory.retrieval.context_builder import render_line
    return [render_line(mm.get(i["memory_id"]), include_ids=False) for i in items]


def agent_memory(agent: str, mm=None) -> AgentMemory:
    if mm is None:
        from memory.core.memory_manager import get_memory
        mm = get_memory()
    return AgentMemory(mm, agent)
