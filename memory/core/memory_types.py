"""Memory record types shared by every store, retriever and agent.

A memory is one remembered thing (a fact, an episode, a lesson...) with enough
metadata to say where it came from, how sure JARVIS is, and when it was true.
Times are UTC ISO-8601 strings ("2026-11-22T09:30:00Z") everywhere inside the
memory package; convert to local time only for display.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


class MemoryType(str, Enum):
    WORKING = "working"          # the current task / conversation scratchpad
    EPISODIC = "episodic"        # something that happened (a run, a conversation, an error)
    SEMANTIC = "semantic"        # a fact about the operator, a project, a contact...
    PROCEDURAL = "procedural"    # a reusable how-to / workflow
    PROSPECTIVE = "prospective"  # a future commitment ("remind me", "I'll send it Friday")
    REFLECTIVE = "reflective"    # a lesson / insight drawn from experience


class Verification(str, Enum):
    VERIFIED = "verified"        # confirmed by the operator or a trusted tool result
    INFERRED = "inferred"        # derived by JARVIS; may be wrong
    UNVERIFIED = "unverified"    # recorded but not checked
    DISPUTED = "disputed"        # contradicted by another memory, awaiting resolution


class Status(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"    # replaced by a newer version / correction (kept for history)
    ARCHIVED = "archived"        # retired by retention policy; not used in recall
    EXPIRED = "expired"          # past its expires_at


class SourceType(str, Enum):
    USER_CONFIRMED = "user_confirmed"  # the operator said it / approved it
    USER_STATED = "user_stated"        # the operator said it in passing
    TOOL_RESULT = "tool_result"        # an agent tool returned it
    AGENT = "agent"                    # an agent concluded it
    IMPORT = "import"                  # migrated from a legacy store
    SYSTEM = "system"                  # JARVIS defaults


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_utc_iso(value: Any) -> Optional[str]:
    """Normalise a datetime / ISO string / epoch to a UTC ISO string.

    Naive values are treated as *local* time, which is what the legacy JSON
    stores wrote (time.strftime / datetime.now())."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive = local
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id(prefix: str = "mem") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


_WS = re.compile(r"\s+")


def normalise_text(text: str) -> str:
    return _WS.sub(" ", str(text or "")).strip().lower().rstrip(".!")


def content_hash(mtype: str, category: str, entity_id: Optional[str], content: str) -> str:
    """Duplicate key: the same statement, of the same kind, about the same thing."""
    key = "\x1f".join([str(mtype), str(category or ""), str(entity_id or ""), normalise_text(content)])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


@dataclass
class MemorySource:
    type: str = SourceType.SYSTEM.value
    reference: Optional[str] = None   # conversation id, tool name, file path, legacy key...
    excerpt: Optional[str] = None     # short evidence text (never secrets)


@dataclass
class MemoryRecord:
    content: str
    type: str = MemoryType.SEMANTIC.value
    category: str = "general"
    entity_id: Optional[str] = None
    source: MemorySource = field(default_factory=MemorySource)
    confidence: float = 1.0
    verification_status: str = Verification.UNVERIFIED.value
    importance: float = 0.5
    memory_id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=utc_now)    # when JARVIS learned it
    updated_at: str = field(default_factory=utc_now)
    valid_from: Optional[str] = None                     # when it became true
    valid_to: Optional[str] = None                       # when it stopped being true
    expires_at: Optional[str] = None
    status: str = Status.ACTIVE.value
    version: int = 1
    tags: list = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)  # type-specific structured fields

    def __post_init__(self):
        for name, enum in (("type", MemoryType), ("verification_status", Verification), ("status", Status)):
            val = getattr(self, name)
            setattr(self, name, enum(val.value if isinstance(val, Enum) else val).value)
        if isinstance(self.source, dict):
            self.source = MemorySource(**{k: v for k, v in self.source.items() if k in ("type", "reference", "excerpt")})
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.importance = max(0.0, min(1.0, float(self.importance)))
        self.content = str(self.content or "").strip()
        if not self.content:
            raise ValueError("a memory needs content")

    @property
    def hash(self) -> str:
        return content_hash(self.type, self.category, self.entity_id, self.content)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MemoryRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})
