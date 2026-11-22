"""Step 1 of retrieval: what is being asked for, about whom / what, and when."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

INTENTS = ("continuation", "how_to", "commitment", "preference", "history", "lesson", "recall")

# order = priority when several match (the first one is the request's main intent)
_PATTERNS = [
    ("continuation", r"\b(continue|resume|pick (?:it |this )?(?:back )?up|carry on|keep working|get back to|back to work on|"
                     r"where (?:were|did) (?:we|i) (?:leave|stop|get)|left off)\b"),
    ("how_to", r"\bhow (?:do|did|can|should|to) (?:i |we |you )?\b|\b(?:steps|workflow|procedure|instructions)\b"),
    ("commitment", r"\b(?:remind|promised?|due|deadline|to-?do|follow(?:ing)? up|owe|commit(?:ted|ment)s?|"
                   r"pending|need to|supposed to|have to do)\b"),
    ("lesson", r"\b(?:lessons?|mistakes?|learn(?:ed|t)|avoid|went wrong|errors?|fail(?:ed|ure|ures)?|fix(?:ed)?)\b"),
    ("preference", r"\b(?:prefer(?:s|red|ence|ences)?|favou?rite|how (?:should|do) you (?:talk|address|answer)|"
                   r"my style|like it when)\b"),
    ("history", r"\b(?:what happened|last time|did (?:i|we|you)|when did|previous(?:ly)?|history|earlier|"
                r"yesterday|last (?:week|month)|ran|tried)\b"),
]
_COMPILED = [(k, re.compile(p, re.I)) for k, p in _PATTERNS]

# which memory types each intent is mostly about (boost, not filter)
TYPE_BOOST: Dict[str, Dict[str, float]] = {
    "how_to": {"procedural": 1.0, "reflective": 0.3},
    "commitment": {"prospective": 1.0},
    "preference": {"semantic": 0.6},
    "history": {"episodic": 1.0},
    "lesson": {"reflective": 1.0, "episodic": 0.4},
    "continuation": {"semantic": 0.3, "prospective": 0.3, "episodic": 0.3},
}
CATEGORY_BOOST = {"preference": {"preference": 1.0}}

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_QUOTED = re.compile(r"[\"“]([^\"”]{3,80})[\"”]")


@dataclass
class QueryAnalysis:
    text: str
    intent: str = "recall"
    intents: List[str] = field(default_factory=list)
    entities: List[Dict[str, Any]] = field(default_factory=list)
    phrases: List[str] = field(default_factory=list)
    since: Optional[str] = None
    until: Optional[str] = None
    valid_at: Optional[str] = None      # "what was true then"
    historical: bool = False            # past-tense / dated question: superseded facts may answer it

    @property
    def type_boost(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for i in self.intents:
            for t, b in TYPE_BOOST.get(i, {}).items():
                out[t] = max(out.get(t, 0.0), b)
        return out

    @property
    def category_boost(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for i in self.intents:
            out.update(CATEGORY_BOOST.get(i, {}))
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {"intent": self.intent, "intents": self.intents, "entities": [e["name"] for e in self.entities],
                "phrases": self.phrases, "since": self.since, "until": self.until, "valid_at": self.valid_at}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def time_window(text: str, now: Optional[datetime] = None):
    """(since, until, valid_at) for phrases like 'yesterday', 'last week', 'in October'."""
    now = now or datetime.now(timezone.utc)
    t = text.lower()
    local = now.astimezone()
    day0 = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if re.search(r"\byesterday\b", t):
        return _iso(day0 - timedelta(days=1)), _iso(day0), None
    if re.search(r"\btoday\b|\bthis morning\b", t):
        return _iso(day0), _iso(now), None
    m = re.search(r"\b(?:last|past) (\d{1,3}) days\b", t)
    if m:
        return _iso(now - timedelta(days=int(m.group(1)))), _iso(now), None
    if re.search(r"\blast week\b", t):
        start = day0 - timedelta(days=day0.weekday() + 7)
        return _iso(start), _iso(start + timedelta(days=7)), None
    if re.search(r"\bthis week\b", t):
        start = day0 - timedelta(days=day0.weekday())
        return _iso(start), _iso(now), None
    if re.search(r"\blast month\b", t):
        first = day0.replace(day=1)
        prev = (first - timedelta(days=1)).replace(day=1)
        return _iso(prev), _iso(first), _iso(prev + (first - prev) / 2)
    if re.search(r"\brecent(?:ly)?\b|\blately\b", t):
        return _iso(now - timedelta(days=14)), _iso(now), None
    m = re.search(r"\b(?:in|during|back in|since) (" + "|".join(sorted(_MONTHS, key=len, reverse=True)) +
                  r")\b(?: (\d{4}))?", t)
    if m:
        month = _MONTHS[m.group(1)]
        year = int(m.group(2)) if m.group(2) else (local.year if month <= local.month else local.year - 1)
        start = local.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=32)).replace(day=1)
        return _iso(start), _iso(end), _iso(start + (end - start) / 2)
    return None, None, None


def analyse(text: str, resolver=None, now: Optional[datetime] = None) -> QueryAnalysis:
    text = str(text or "").strip()
    intents = [k for k, rx in _COMPILED if rx.search(text)]
    since, until, valid_at = time_window(text, now)
    qa = QueryAnalysis(text=text, intents=intents, intent=intents[0] if intents else "recall",
                       phrases=[p.strip() for p in _QUOTED.findall(text)], since=since, until=until,
                       valid_at=valid_at)
    qa.historical = bool(valid_at) or bool(re.search(r"\b(?:used to|was|were|did|back then|previously)\b", text, re.I))
    if resolver is not None:
        qa.entities = resolver.mentions(text)
    return qa
