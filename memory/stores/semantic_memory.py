"""Semantic memory: facts and preferences.

Facts are (subject, predicate, object) statements with provenance, e.g.
("Ravi", "works at", "Acme"). A predicate is single-valued by default: telling
JARVIS a new object for the same subject + predicate supersedes the old fact
(it stays in history with valid_to set), so "works at Globex" replaces "works
at Acme" instead of both being "true". Multi-valued predicates ("likes")
accumulate. When the object is itself a known entity, the two are linked in
the relationships table, backed by this fact.

Preferences are facts about the operator keyed by name ("salutation",
"answer_length"); setting one again replaces it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from memory.core.memory_types import MemoryRecord, MemorySource, Verification
from memory.stores.base import Store, iso, now_dt

OPERATOR = "operator"
MULTI_VALUED = {"likes", "dislikes", "knows", "uses", "has", "owns", "speaks", "member of", "works on", "attended",
                "is related to"}


def _norm(s: str) -> str:
    return " ".join(str(s or "").lower().split())


class SemanticMemory(Store):
    TYPE = "semantic"

    def assert_fact(self, subject: str, predicate: str, obj: str, *, subject_kind: str = "other",
                    object_kind: Optional[str] = None, multi: Optional[bool] = None, source: str = "user_stated",
                    reference: Optional[str] = None, verified: Optional[bool] = None, confidence: float = 0.8,
                    importance: float = 0.6, valid_from=None, category: str = "fact",
                    text: Optional[str] = None) -> MemoryRecord:
        content = text or f"{subject} {predicate} {obj}"
        multi = _norm(predicate) in MULTI_VALUED if multi is None else multi
        data = {"subject": subject, "predicate": predicate, "object": obj,
                "subject_key": _norm(subject), "predicate_key": _norm(predicate), "object_key": _norm(obj),
                "multi": bool(multi)}
        prior = [] if multi else [r for r in self._facts(subject, predicate) if r.data.get("object_key") != _norm(obj)]
        if prior:
            from memory.core.memory_types import SourceType
            if verified is None:
                verified = source == SourceType.USER_CONFIRMED.value
            new = MemoryRecord(content=content, type=self.TYPE, category=category,
                               entity_id=prior[0].entity_id, source=MemorySource(source, reference),
                               confidence=1.0 if verified else confidence, importance=importance, data=data,
                               verification_status=Verification.VERIFIED.value if verified else
                               Verification.UNVERIFIED.value, valid_from=valid_from or iso(now_dt()))
            rec = self.repo.supersede(prior[0].memory_id, new, actor=source, reason=f"new {predicate}")
            for extra in prior[1:]:
                self.repo.update(extra.memory_id, reason=f"new {predicate}", status="superseded",
                                 valid_to=rec.valid_from)
        else:
            rec = self.mm.remember(content, type=self.TYPE, category=category, entity=subject,
                                   entity_kind=subject_kind, source_type=source, reference=reference,
                                   verified=verified, confidence=confidence, importance=importance, data=data,
                                   valid_from=valid_from)
        self._link(rec, subject, predicate, obj, object_kind)
        return rec

    def _facts(self, subject: str, predicate: Optional[str] = None, valid_at=None) -> List[MemoryRecord]:
        data = {"subject_key": _norm(subject)}
        if predicate:
            data["predicate_key"] = _norm(predicate)
        return self.repo.find(type=self.TYPE, data=data, valid_at=valid_at, limit=200)

    def _link(self, rec: MemoryRecord, subject, predicate, obj, object_kind):
        dst = self.repo.find_entity(obj)
        if dst is None and object_kind:
            dst = {"entity_id": self.repo.upsert_entity(obj, object_kind)}
        src = self.repo.find_entity(subject)
        if dst and src:
            self.repo.relate(src["entity_id"], _norm(predicate), dst["entity_id"], memory_id=rec.memory_id,
                             confidence=rec.confidence, valid_from=rec.valid_from)

    def facts_about(self, subject: str, predicate: Optional[str] = None, valid_at=None) -> List[MemoryRecord]:
        """Current facts (or those true at valid_at) about a subject, by name or alias."""
        ent = self.repo.find_entity(subject)
        out = self._facts(ent["name"] if ent else subject, predicate, valid_at)
        if ent:  # also free-text facts filed under the entity
            seen = {r.memory_id for r in out}
            out += [r for r in self.repo.find(type=self.TYPE, entity_id=ent["entity_id"], valid_at=valid_at)
                    if r.memory_id not in seen and (not predicate or r.data.get("predicate_key") == _norm(predicate))]
        return out

    def value(self, subject: str, predicate: str, valid_at=None) -> Optional[str]:
        facts = self.facts_about(subject, predicate, valid_at)
        return facts[0].data.get("object") if facts else None

    def history(self, subject: str, predicate: str) -> List[Dict[str, Any]]:
        """Every value this subject + predicate has had, oldest first, with when each was true."""
        rows = self.repo.find(type=self.TYPE, status=None, order="oldest", limit=500,
                              data={"subject_key": _norm(subject), "predicate_key": _norm(predicate)})
        rows.sort(key=lambda r: (r.valid_from or r.created_at, r.status != "superseded"))
        return [{"object": r.data.get("object"), "valid_from": r.valid_from, "valid_to": r.valid_to,
                 "status": r.status, "memory_id": r.memory_id} for r in rows]

    # ── preferences ────────────────────────────────────────────
    def _pref_records(self, key: str) -> List[MemoryRecord]:
        return self.repo.find(type=self.TYPE, category="preference", data={"key": key}, limit=20)

    def set_preference(self, key: str, value: Any, text: Optional[str] = None, source: str = "user_confirmed",
                       reference: Optional[str] = None) -> MemoryRecord:
        """Set (or change) one operator preference; the old value is superseded, not erased."""
        content = text or f"Operator preference {key}: {value}"
        data = {"key": key, "value": value, "subject": OPERATOR, "predicate": f"prefers:{key}", "object": str(value),
                "subject_key": OPERATOR, "predicate_key": _norm(f"prefers:{key}"), "object_key": _norm(value),
                "multi": False}
        current = self._pref_records(key)
        if current and current[0].data.get("value") == value:
            return current[0]
        if not current:
            return self.mm.remember(content, type=self.TYPE, category="preference", entity=OPERATOR,
                                    entity_kind="person", source_type=source, reference=reference, importance=0.8,
                                    data=data)
        verified = source == "user_confirmed"
        new = MemoryRecord(content=content, type=self.TYPE, category="preference", entity_id=current[0].entity_id,
                           source=MemorySource(source, reference), importance=0.8, data=data,
                           confidence=1.0 if verified else 0.8,
                           verification_status=Verification.VERIFIED.value if verified else Verification.UNVERIFIED.value)
        rec = self.repo.supersede(current[0].memory_id, new, actor=source, reason=f"preference {key} changed")
        for extra in current[1:]:
            self.repo.update(extra.memory_id, reason=f"preference {key} changed", status="superseded",
                             valid_to=rec.valid_from)
        return rec

    def preference(self, key: str, default: Any = None) -> Any:
        current = self._pref_records(key)
        return current[0].data.get("value", default) if current else default

    def preferences(self) -> Dict[str, Any]:
        return {r.data["key"]: r.data.get("value") for r in self.repo.find(type=self.TYPE, category="preference",
                                                                          limit=500, order="oldest")
                if r.data.get("key")}
