"""Contradiction detection: notice when two memories can't both be true.

Two statements conflict when they say the same thing about the same entity
except for a value: "Ravi's birthday is on 3 March" / "Ravi's birthday is on
13 March". Structured facts compare (subject, predicate) directly.

Resolution policy (never a coin toss):
  * the operator's own word beats an inference or a tool result
    (higher-trust source supersedes the lower one);
  * a changeable attribute (works at, lives in, status...) said later by the
    operator is an update: the newer statement supersedes the older one;
  * otherwise (e.g. two different birthdays, both from the operator) both are
    marked DISPUTED, linked to each other, and kept out of "verified"
    answers until the operator settles it (resolve()).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from memory.core.memory_types import MemoryRecord, MemorySource, normalise_text

TRUST = {"user_confirmed": 4, "user_stated": 3, "tool_result": 2, "import": 1, "agent": 1, "system": 0}
IMMUTABLE = re.compile(r"\b(?:birthday|born|birth ?date|date of birth|anniversary|blood (?:type|group)|maiden name|"
                       r"nationality|middle name|first name|last name)\b", re.I)
_MONTHS = ("january february march april may june july august september october november december "
           "jan feb mar apr jun jul aug sep sept oct nov dec").split()
_VALUE = re.compile(r"\b(?:\d[\d:.,/-]*(?:am|pm|st|nd|rd|th)?|" + "|".join(_MONTHS) + r")\b|[\w.+-]+@[\w-]+\.[\w.]+",
                    re.I)
_FILLER = re.compile(r"\b(?:now|currently|actually|still|really|also|today)\b", re.I)


@dataclass
class Conflict:
    new: MemoryRecord
    old: MemoryRecord
    action: str          # "supersede_old" | "supersede_new" | "dispute"
    reason: str


def _proper_nouns(text: str) -> List[str]:
    words = re.findall(r"\b[A-Z][\w-]+\b", text)
    return [w.lower() for w in words[1:]]  # skip the sentence's first word


def signature(text: str) -> (str, set):
    """The statement with its values blanked out, and the set of values."""
    t = _FILLER.sub(" ", text)
    values = {v.lower() for v in _VALUE.findall(t)}
    nouns = set(_proper_nouns(t))
    sig = _VALUE.sub(" _ ", t)
    for n in nouns:
        sig = re.sub(rf"\b{re.escape(n)}\b", " _ ", sig, flags=re.I)
    return normalise_text(re.sub(r"[^\w\s]", " ", sig)), values | nouns


def _overlap(a: MemoryRecord, b: MemoryRecord) -> bool:
    """Whether the two were (claimed to be) true at the same time."""
    a0, a1 = a.valid_from or "", a.valid_to or "9999"
    b0, b1 = b.valid_from or "", b.valid_to or "9999"
    return a0 < b1 and b0 < a1


def conflicts(a: MemoryRecord, b: MemoryRecord) -> bool:
    if a.memory_id == b.memory_id or a.type != "semantic" or b.type != "semantic":
        return False
    if not _overlap(a, b):
        return False  # Project Atlas in October, Borealis in November: a change, not a contradiction
    da, db = a.data, b.data
    if da.get("subject_key") and db.get("subject_key"):
        return (da["subject_key"] == db["subject_key"] and da.get("predicate_key") == db.get("predicate_key")
                and not da.get("multi") and da.get("object_key") != db.get("object_key"))
    if not a.entity_id or a.entity_id != b.entity_id:
        return False  # free text only conflicts when filed about the same known entity
    sa, va = signature(a.content)
    sb, vb = signature(b.content)
    return bool(sa) and sa == sb and bool(va) and bool(vb) and va != vb and "_" in sa


def decide(new: MemoryRecord, old: MemoryRecord) -> Conflict:
    tn, to = TRUST.get(new.source.type, 1), TRUST.get(old.source.type, 1)
    if tn > to:
        return Conflict(new, old, "supersede_old", f"{new.source.type} outranks {old.source.type}")
    if to > tn:
        return Conflict(new, old, "supersede_new", f"{old.source.type} outranks {new.source.type}")
    if not IMMUTABLE.search(new.content) and new.created_at >= old.created_at:
        return Conflict(new, old, "supersede_old", "newer statement of a changeable fact")
    return Conflict(new, old, "dispute", "both equally trusted and it can't change")


def find_conflicts(mm, rec: MemoryRecord) -> List[MemoryRecord]:
    if rec.type != "semantic" or rec.status != "active":
        return []
    if rec.data.get("subject_key"):
        pool = mm.repo.find(type="semantic", data={"subject_key": rec.data["subject_key"],
                                                   "predicate_key": rec.data.get("predicate_key")}, limit=50)
    else:
        pool = mm.repo.find(type="semantic", entity_id=rec.entity_id, category=rec.category, limit=200)
    return [o for o in pool if conflicts(rec, o)]


def check(mm, rec: MemoryRecord, apply: bool = True) -> List[Conflict]:
    """Detect (and by default act on) conflicts between `rec` and what is already remembered."""
    out = []
    for old in find_conflicts(mm, rec):
        c = decide(rec, old)
        out.append(c)
        if apply:
            _apply(mm, c)
    return out


def _apply(mm, c: Conflict):
    repo = mm.repo
    if c.action == "dispute":
        for a, b in ((c.new, c.old), (c.old, c.new)):
            cur = repo.get(a.memory_id)
            others = sorted(set(cur.data.get("conflicts_with", [])) | {b.memory_id})
            repo.update(a.memory_id, actor="contradiction_detector", reason=c.reason,
                        verification_status="disputed", data={"conflicts_with": others})
        return
    loser, winner = (c.old, c.new) if c.action == "supersede_old" else (c.new, c.old)
    _supersede_by(repo, loser, winner, c.reason)


def _supersede_by(repo, loser: MemoryRecord, winner: MemoryRecord, reason: str):
    now = winner.valid_from or winner.created_at
    with repo._tx() as conn:
        cur = repo._get(conn, loser.memory_id)
        if cur is None or cur.status != "active":
            return
        repo._snapshot(conn, cur, reason)
        cur.status, cur.valid_to = "superseded", cur.valid_to or now
        cur.version += 1
        repo._write_row(conn, cur, superseded_by=winner.memory_id, insert=False)
        repo._log(conn, "supersede", loser.memory_id, "contradiction_detector", {"by": winner.memory_id,
                                                                                "reason": reason})


def disputes(mm) -> List[MemoryRecord]:
    return mm.repo.find(verification_status="disputed", limit=200)


def resolve(mm, keep_id: str, reference: Optional[str] = None) -> MemoryRecord:
    """The operator settled a dispute: keep one, retract the ones it conflicted with."""
    keep = mm.repo.get(keep_id)
    if keep is None:
        raise KeyError(keep_id)
    for other_id in keep.data.get("conflicts_with", []):
        other = mm.repo.get(other_id)
        if other and other.status == "active":
            mm.repo.update(other_id, actor="user", reason="dispute resolved", status="superseded",
                           valid_to=other.valid_from or other.created_at,
                           valid_from=other.valid_from or other.created_at, data={"retracted": True})
    keep = mm.repo.update(keep_id, actor="user", reason="dispute resolved", verification_status="verified",
                          confidence=1.0, data={"conflicts_with": [], "resolved_by": reference or "operator"})
    return keep


def attach(mm):
    """Check every new semantic memory as it is written."""
    def on_change(action, rec):
        if action == "add" and rec.type == "semantic" and not getattr(mm, "_checking_conflicts", False):
            mm._checking_conflicts = True
            try:
                check(mm, rec)
            except Exception:
                pass
            finally:
                mm._checking_conflicts = False
    mm.repo.on_change(on_change)
