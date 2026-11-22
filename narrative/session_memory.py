# Manages full conversation history & persistent cross-session recall
"""Conversation sessions.

The JSON file keeps a small working copy (current session + recent session
summaries) for fast prompt building. Every message is also archived durably in
the unified memory database (memory/), where nothing is capped. Timestamps are
UTC internally ("...Z"); legacy naive timestamps are read as local time.
"""
import json
import re
import weakref
from collections import Counter
from typing import List, Dict, Optional
from datetime import datetime, timezone
from pathlib import Path

HISTORY_FILE = Path(__file__).parent.parent / "data" / "session_history.json"


SESSION_TIMEOUT_SECONDS = 7200  # 2 hours of inactivity demarcates a new conversation session
PAST_SESSIONS_KEPT = 20         # summaries kept in the JSON working copy (full history lives in the database)
CURRENT_MESSAGES_KEPT = 60
PAST_MESSAGES_KEPT = 15

_STOP = set("""a an the and or but if then so to of in on at by for with from about into over is are was were be been
being do does did have has had i you he she it we they me my your our their this that these those what which who whom
how why when where can could would should will shall may might must just please tell show give find check open get
let make jarvis sir maam ma'am hey hi hello thanks thank okay ok yes no not any some all more most very also again
now today tomorrow""".split())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse(value) -> Optional[datetime]:
    """Aware UTC datetime from an ISO string; naive legacy values are local time."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def _session_id(started_at: str) -> str:
    from memory.maintenance.memory_migration import legacy_session_id
    return legacy_session_id(started_at)


def summarise_messages(messages: List[Dict]) -> Dict:
    """Structured, model-free summary of a session: what was asked, about what, and the outcome."""
    users = [m for m in messages if m.get("role") == "user" and len(str(m.get("content", "")).strip()) > 3]
    replies = [m for m in messages if m.get("role") != "user" and str(m.get("content", "")).strip()]
    words = Counter()
    for m in users:
        for w in re.findall(r"[a-z][a-z0-9'+.-]{2,}", str(m["content"]).lower()):
            w = w.strip(".'-")
            if w and w not in _STOP:
                words[w] += 1
    topics = [w for w, _ in words.most_common(6)]
    targets = list(dict.fromkeys(m["target"] for m in messages if m.get("target")))
    requests = [str(m["content"]).strip()[:140] for m in users]
    last_reply = str(replies[-1]["content"]).strip()[:200] if replies else ""
    first, last = (messages[0].get("at"), messages[-1].get("at")) if messages else (None, None)
    parts = []
    if requests:
        parts.append("Asked: " + "; ".join(f"'{r}'" for r in (requests[:2] + requests[-2:] if len(requests) > 4
                                                                  else requests)))
    if targets:
        parts.append("Targets: " + ", ".join(targets[:5]))
    if topics:
        parts.append("Topics: " + ", ".join(topics))
    if last_reply:
        parts.append(f"Last reply: '{last_reply}'")
    return {"text": " | ".join(parts), "topics": topics, "targets": targets, "requests": requests[-10:],
            "user_turns": len(users), "messages": len(messages), "first_at": first, "last_at": last,
            "last_reply": last_reply}


_LIVE = weakref.WeakSet()


def live_sessions():
    """Running SessionMemory objects (so "forget" can reach what they hold in memory, not just on disk)."""
    return list(_LIVE)


class SessionMemory:
    def __init__(self, filepath: Optional[Path] = None):
        self.filepath = filepath or HISTORY_FILE
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._mirror = self.filepath.resolve() == HISTORY_FILE.resolve()
        self.history: List[Dict] = []
        self.past_sessions: List[Dict] = []
        self.started_at = _iso(_now())
        self.session_id = _session_id(self.started_at)
        self._load_from_disk()
        _LIVE.add(self)

    def forget(self, needle: str) -> int:
        """Drop every message (current and past sessions) mentioning needle; returns how many."""
        needle = str(needle or "").lower().strip()
        if len(needle) < 3:
            return 0
        hit = lambda m: needle in str(m.get("content", "")).lower() or needle in str(m.get("target", "")).lower()  # noqa: E731
        before = len(self.history) + sum(len(p.get("messages", [])) for p in self.past_sessions)
        self.history = [m for m in self.history if not hit(m)]
        for p in self.past_sessions:
            p["messages"] = [m for m in p.get("messages", []) if not hit(m)]
            if needle in json.dumps(p.get("summary", {})).lower():
                p["summary"] = summarise_messages(p["messages"])
        after = len(self.history) + sum(len(p.get("messages", [])) for p in self.past_sessions)
        if after != before:
            self._save_to_disk()
        return before - after

    # ── persistence ────────────────────────────────────────────
    def _load_from_disk(self):
        if not self.filepath.exists():
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[SessionMemory] Warning loading persistent history: {e}")
            return
        self.past_sessions = data.get("past_sessions", []) or []
        current = data.get("current_session", {}) or {}
        msgs = current.get("messages", []) or []
        if not msgs:
            return
        last_dt = _parse(msgs[-1].get("at"))
        started = current.get("started_at") or msgs[0].get("at") or self.started_at
        if last_dt and (_now() - last_dt).total_seconds() > SESSION_TIMEOUT_SECONDS:
            self._archive(started, msgs, current.get("session_id"))
            self.history = []
        else:
            self.history = msgs
            self.started_at = started  # resume the same session (was: always a fresh start time)
            self.session_id = current.get("session_id") or _session_id(started)

    def _save_to_disk(self):
        try:
            payload = {
                "updated_at": _iso(_now()),
                "current_session": {
                    "session_id": self.session_id,
                    "started_at": self.started_at,
                    "messages": self.history[-CURRENT_MESSAGES_KEPT:]
                },
                "past_sessions": self.past_sessions[-PAST_SESSIONS_KEPT:]
            }
            tmp = self.filepath.with_suffix(self.filepath.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            tmp.replace(self.filepath)
        except Exception as e:
            print(f"[SessionMemory] Error saving persistent history: {e}")

    def _archive(self, started_at: str, msgs: List[Dict], session_id: Optional[str] = None):
        """Close a session: keep a structured summary + its last messages in JSON; the database keeps it all."""
        if not msgs:
            return
        sid = session_id or _session_id(started_at)
        summary = summarise_messages(msgs)
        ended = msgs[-1].get("at") or _iso(_now())
        self.past_sessions.append({
            "session_id": sid,
            "started_at": started_at,
            "ended_at": ended,
            "summary": summary,
            "messages": msgs[-PAST_MESSAGES_KEPT:]
        })
        if self._mirror:
            from memory.core import legacy_bridge
            for m in msgs:  # messages already archived are skipped by the database
                legacy_bridge.session_message(sid, started_at, m.get("role", "user"), m.get("content", ""),
                                              m.get("at"), m.get("target"))
            legacy_bridge.session_end(sid, ended, summary)

    def _roll(self):
        self._archive(self.started_at, self.history, self.session_id)
        self.history = []
        self.started_at = _iso(_now())
        self.session_id = _session_id(self.started_at)

    # ── API ────────────────────────────────────────────────────
    def add(self, role: str, content: str, target: Optional[str] = None):
        """Append a message. role = 'user' | 'jarvis'"""
        if not content or not str(content).strip():
            return
        # Roll to new session if previous interaction was over 2 hours ago
        if self.history:
            last_dt = _parse(self.history[-1].get("at"))
            if last_dt and (_now() - last_dt).total_seconds() > SESSION_TIMEOUT_SECONDS:
                self._roll()

        # Never pollute conversation history with raw internal prompts, debriefs, or skill contexts
        if role == "user":
            stripped = str(content).lstrip()
            if stripped.startswith(("[COMMAND_DEBRIEF]", "[SKILL_CONTEXT]", "[GEOSPATIAL TELEMETRY]")):
                m = re.search(r'User (?:Prompt|Question):\s*([^\n]+)', stripped, re.IGNORECASE)
                if m:
                    content = m.group(1).strip()
                else:
                    return
        elif role == "jarvis" and content:
            content = re.sub(r'^(?:ASSISTANT|AI)\s*:\s*', '', content, flags=re.IGNORECASE).strip()

        entry = {
            "role": role,
            "content": content,
            "at": _iso(_now()),
        }
        if target:
            entry["target"] = target
        self.history.append(entry)
        self._save_to_disk()
        if self._mirror:
            from memory.core import legacy_bridge
            legacy_bridge.session_message(self.session_id, self.started_at, role, content, entry["at"], target)

    def last_n(self, n: int = 12) -> List[Dict]:
        return self.history[-n:]

    def clear(self):
        if self.history:
            self._roll()
        else:
            self.started_at = _iso(_now())
            self.session_id = _session_id(self.started_at)
        self._save_to_disk()

    def to_text(self, n: int = 12, max_age_seconds: int = SESSION_TIMEOUT_SECONDS) -> str:
        lines = []
        now = _now()
        for m in self.last_n(n):
            dt = _parse(m.get("at"))
            if dt and (now - dt).total_seconds() > max_age_seconds:
                continue
            role_label = "User" if m.get("role") == "user" else "J.A.R.V.I.S."
            clean_content = re.sub(r'^(?:ASSISTANT|AI)\s*:\s*', '', m.get("content", ""), flags=re.IGNORECASE).strip()
            lines.append(f"{role_label}: {clean_content}")
        return "\n".join(lines)

    def get_last_session_summary(self) -> Optional[str]:
        """A human-friendly summary of the previous session: requests, targets, topics, outcome."""
        if self.past_sessions:
            last = self.past_sessions[-1]
            summary = last.get("summary") or summarise_messages(last.get("messages", []))
        elif len(self.history) > 2:
            summary = summarise_messages(self.history[:-2])
        else:
            return None
        if not summary.get("requests"):
            return None
        when = _parse(summary.get("last_at"))
        prefix = f"Previous session ({when.astimezone().strftime('%d %b %H:%M')})" if when else "Previous session"
        return f"{prefix}: {summary['text']}"

    def get_cross_session_context(self) -> str:
        """Build context summary for prompt injection."""
        summary = self.get_last_session_summary()
        if summary:
            return (
                f"[PAST SESSIONS REFERENCE ONLY]: {summary}\n"
                f"Directive: The above past session topics are strictly for historical reference if the operator asks what was discussed earlier. Do NOT answer or merge these past topics into new, unrelated conversations."
            )
        return ""
