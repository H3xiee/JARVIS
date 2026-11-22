"""Step 4 of retrieval: a compact, source-attributed memory package for a model.

Memories are grouped into sections, each line says where it came from and how
sure JARVIS is, unconfirmed items are marked, near-duplicates are dropped, and
the whole package stays inside a token budget (best-ranked lines kept first).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from memory.core.memory_types import normalise_text
from memory.retrieval.memory_ranker import RankedMemory

SECTION_ORDER = ["Project state", "Preferences", "Facts", "Decisions", "Open commitments", "Procedures",
                 "Past errors and fixes", "Lessons", "Recent activity", "Notes"]
SOURCE_LABEL = {"user_confirmed": "you confirmed", "user_stated": "you said", "tool_result": "tool result",
                "agent": "JARVIS inferred", "import": "imported", "system": "default"}


def section_for(rec) -> str:
    t, c = rec.type, rec.category
    if t == "working":
        return "Project state" if rec.data.get("key") == "focus" else "Notes"
    if t == "semantic":
        if c == "preference":
            return "Preferences"
        if c in ("project_state", "status", "active_project"):
            return "Project state"
        if c == "decision":
            return "Decisions"
        if c == "objective":
            return "Open commitments"
        return "Facts"
    if t == "prospective":
        return "Open commitments"
    if t == "procedural":
        return "Procedures"
    if t == "reflective":
        return "Lessons"
    if t == "episodic":
        return "Past errors and fixes" if rec.data.get("outcome") == "failure" or rec.data.get("error") else \
            "Recent activity"
    return "Notes"


def _date(ts: Optional[str]) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%d %b %Y")
    except ValueError:
        return ""


def render_line(rec, include_ids: bool = True) -> str:
    text = rec.content
    if rec.type == "episodic" and rec.data.get("resolution"):
        text += f" — fixed by: {rec.data['resolution']}"
    if rec.type == "prospective" and rec.data.get("due_at"):
        text += f" (due {_date(rec.data['due_at'])})"
    if rec.type == "procedural" and rec.data.get("success_count"):
        text += f" (worked {rec.data['success_count']}x, failed {rec.data.get('failure_count', 0)}x)"
    attrs = [SOURCE_LABEL.get(rec.source.type, rec.source.type)]
    d = _date(rec.valid_from if rec.type == "semantic" and rec.valid_from else rec.updated_at)
    if d:
        attrs.append(d)
    if rec.verification_status in ("inferred", "unverified"):
        attrs.append("unconfirmed")
    elif rec.verification_status == "disputed":
        attrs.append("DISPUTED: conflicts with another memory")
    if rec.status == "superseded":
        attrs.append(f"no longer true since {_date(rec.valid_to)}" if rec.valid_to else "outdated")
    if include_ids:
        attrs.append(rec.memory_id)
    return f"- {text} [{'; '.join(attrs)}]"


@dataclass
class ContextPackage:
    query: str
    text: str
    items: List[Dict[str, Any]] = field(default_factory=list)
    tokens: int = 0
    sections: Dict[str, int] = field(default_factory=dict)
    analysis: Dict[str, Any] = field(default_factory=dict)
    dropped_for_budget: int = 0

    def __bool__(self) -> bool:
        return bool(self.items)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def build(query: str, ranked: List[RankedMemory], budget_tokens: int = 600, include_ids: bool = True,
          analysis: Optional[Dict[str, Any]] = None, header: bool = True) -> ContextPackage:
    seen: List[str] = []
    chosen: List[RankedMemory] = []
    for r in ranked:  # best first; drop near-duplicates
        n = normalise_text(r.record.content)
        if any(n == s or (len(n) > 25 and (n in s or s in n)) for s in seen):
            continue
        seen.append(n)
        chosen.append(r)
    head = (f"[MEMORY CONTEXT for: \"{query[:120]}\". From JARVIS's memory; items marked unconfirmed or "
            f"disputed may be wrong — check before acting on them.]") if header else ""
    used = estimate_tokens(head)
    kept: List[RankedMemory] = []
    dropped = 0
    for r in chosen:
        r.section = r.section or section_for(r.record)
        cost = estimate_tokens(render_line(r.record, include_ids)) + 1
        if used + cost > budget_tokens:
            dropped += 1
            continue
        used += cost
        kept.append(r)
    if not kept:
        return ContextPackage(query=query, text="", analysis=analysis or {}, dropped_for_budget=dropped)
    lines = [head] if head else []
    sections: Dict[str, int] = {}
    for name in SECTION_ORDER:
        group = [r for r in kept if r.section == name]
        if not group:
            continue
        lines.append(f"{name}:")
        lines += [render_line(r.record, include_ids) for r in group]
        sections[name] = len(group)
    text = "\n".join(lines)
    return ContextPackage(query=query, text=text, items=[r.to_dict() for r in kept], tokens=estimate_tokens(text),
                          sections=sections, analysis=analysis or {}, dropped_for_budget=dropped)
