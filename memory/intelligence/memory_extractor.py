"""Automatic memory extraction: find what is worth remembering in what the operator says.

Two extractors, same output (Candidate):
  RuleExtractor  pattern-based, offline, always on. Catches the explicit forms:
                 "remember that…", "I prefer…", "call me…", "we decided…",
                 "remind me to… by…", "I'll… by…", "X's Y is Z", "the project
                 is now…", "no, it's X not Y" (corrections), "always / never…".
  LLMExtractor   asks the shared agent LLM (Groq, local Ollama fallback) for
                 the same JSON shape. Optional (JARVIS_MEMORY_LLM=0 turns it off).

Every candidate carries the exact words it came from (evidence). A candidate
whose evidence is not literally in the message is discarded, so a model can't
invent memories. Secrets (passwords, API keys, card numbers, OTPs) are never
extracted, and are redacted before any text is sent to a model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

KINDS = ("fact", "preference", "decision", "commitment", "project_state", "lesson", "correction", "rule")


@dataclass
class Candidate:
    kind: str
    content: str
    evidence: str
    subject: Optional[str] = None
    predicate: Optional[str] = None
    object: Optional[str] = None
    key: Optional[str] = None          # preference key
    value: Optional[str] = None        # preference value
    old_value: Optional[str] = None    # corrections: what was wrong
    due: Optional[str] = None          # commitments: due text as said ("by Friday")
    entity: Optional[str] = None
    entity_kind: str = "other"
    source_type: str = "user_stated"
    confidence: float = 0.8
    explicit: bool = False             # "remember that…": the operator asked
    hedged: bool = False               # "I think / maybe": less sure
    extractor: str = "rules"
    reference: Optional[str] = None    # session:message
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── secrets ────────────────────────────────────────────────────
_SECRET_PATTERNS = [
    re.compile(r"\b(?:password|passcode|passwd|pwd|pin|otp|one[- ]time code|verification code|cvv|secret|api[ _-]?key|"
               r"token|private key|recovery (?:code|phrase)|seed phrase)\b", re.I),
    re.compile(r"\b(?:sk|pk|gsk|ghp|gho|xox[abp]|AKIA)[-_A-Za-z0-9]{12,}\b"),
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),                  # card-like digit runs
    re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b"),             # long opaque tokens
]


def contains_secret(text: str) -> bool:
    return any(p.search(text or "") for p in _SECRET_PATTERNS)


def redact(text: str) -> str:
    out = str(text or "")
    for p in _SECRET_PATTERNS[1:]:
        out = p.sub("[REDACTED]", out)
    return re.sub(r"(?i)\b(password|passcode|pin|otp|api[ _-]?key|token|cvv)\b(\s*(?:is|:|=)\s*)\S+", r"\1\2[REDACTED]",
                  out)


# ── helpers ────────────────────────────────────────────────────
_HEDGE = re.compile(r"\b(?:i think|i guess|maybe|probably|perhaps|not sure|might|i believe)\b", re.I)
_LEAD_HEDGE = re.compile(r"^(?:i think|i guess|i believe|i'm pretty sure|maybe|probably|perhaps)\s+(?:that\s+)?",
                         re.I)
_QUESTION = re.compile(r"\?\s*$|^\s*(?:what|who|when|where|why|how|which|is|are|do|does|did|can|could|would|should)\b",
                       re.I)
_SPLIT = re.compile(r"(?<=[.!;])\s+|\s+(?:and also|also,)\s+")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip(" .,!;:-\"'")


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in _SPLIT.split(str(text or "")) if s and s.strip()]


def _cap(s: str) -> str:
    s = _clean(s)
    return s[:1].upper() + s[1:] if s else s


_DUE = r"(?:\s+(?P<due>(?:by|before|on|at|tomorrow|tonight|next|this|in)\b.*))?"

# (kind, pattern, builder) — first match per sentence wins
_RULES: List[tuple] = []


def rule(kind: str, pattern: str):
    def deco(fn):
        _RULES.append((kind, re.compile(pattern, re.I), fn))
        return fn
    return deco


@rule("correction", r"^(?:no[,.]?\s+|nope[,.]?\s+|actually[,.]?\s+|correction[:,]?\s+|that'?s wrong[,.]?\s+)"
                    r"(?P<stmt>.+?)(?:,?\s+not\s+(?P<old>[^,.;]+))?$")
def _correction(m, s):
    stmt = _clean(m.group("stmt"))
    if len(stmt.split()) < 3:
        return None
    return Candidate("correction", _cap(stmt), s, old_value=_clean(m.group("old") or "") or None,
                     source_type="user_confirmed", confidence=1.0)


@rule("rule", r"^(?:please\s+)?(?P<w>always|never|don'?t ever|do not ever)\s+(?P<what>.{4,})$")
def _standing_rule(m, s):
    w = m.group("w").lower().replace("don't ever", "never").replace("do not ever", "never").replace("dont ever", "never")
    return Candidate("rule", _cap(f"{w} {_clean(m.group('what'))}"), s, source_type="user_confirmed",
                     confidence=1.0, explicit=True)


@rule("preference", r"^(?:please\s+)?call me\s+(?P<v>[\w' .-]{2,30})$")
def _salutation(m, s):
    v = _clean(m.group("v"))
    return Candidate("preference", f"Address the operator as '{v}'.", s, key="salutation", value=v,
                     source_type="user_confirmed", confidence=1.0, explicit=True)


@rule("preference", r"^i\s+(?:really\s+)?(?:prefer|like|want)\s+(?P<what>.+?)(?:\s+(?:over|to|rather than|instead of)"
                    r"\s+(?P<other>.+))?$")
def _prefer(m, s):
    what = _clean(m.group("what"))
    if len(what) < 3 or re.match(r"^(?:to know|you to tell|to ask)\b", what, re.I):
        return None
    other = _clean(m.group("other") or "")
    key = re.sub(r"[^a-z0-9]+", "_", (other or what).lower()).strip("_")[:40]
    content = f"Prefers {what}" + (f" over {other}" if other else "") + "."
    return Candidate("preference", content, s, key=key, value=what, source_type="user_stated")


@rule("decision", r"^(?:ok(?:ay)?[,.]?\s+)?(?:we|i)(?:'ve| have)?\s+(?:decided|agreed|chose|chosen|are going|'re going|will go)"
                  r"\s+(?:to|on|with|that)?\s*(?P<what>.{4,})$|^let'?s\s+go\s+with\s+(?P<what2>.{3,})$")
def _decision(m, s):
    what = _clean(m.group("what") or m.group("what2"))
    return Candidate("decision", f"Decision: {what}.", s, source_type="user_confirmed", confidence=0.95)


@rule("commitment", r"^(?:please\s+)?remind me (?:to\s+)?(?P<what>.+?)" + _DUE + r"$")
def _remind(m, s):
    what = _clean(m.group("what"))
    return Candidate("commitment", _cap(what), s, due=_clean(m.group("due") or "") or None,
                     source_type="user_stated", explicit=True, meta={"owner": "jarvis"})


@rule("commitment", r"^i(?:'ll| will| need to| have to| must| promised to| should)\s+(?P<what>(?:send|call|email|finish|"
                    r"submit|pay|book|buy|renew|reply|review|ship|fix|write|prepare|follow up|meet|check)\b.+?)" + _DUE + r"$")
def _promise(m, s):
    return Candidate("commitment", _cap(m.group("what")), s, due=_clean(m.group("due") or "") or None,
                     source_type="user_stated", meta={"owner": "operator"})


@rule("project_state", r"^(?P<proj>(?:the\s+)?[\w' -]{2,40}?(?:project|app|repo|dashboard|website|system))\s+"
                       r"(?:is|'s)\s+(?:now\s+)?(?P<state>(?:done|finished|blocked|on hold|paused|live|shipped|in review|"
                       r"behind|ahead|on track|almost done|started|cancelled)\b.*)$")
def _project_state(m, s):
    proj = _clean(re.sub(r"^the\s+", "", m.group("proj"), flags=re.I))
    return Candidate("project_state", f"{_cap(proj)} status: {_clean(m.group('state'))}.", s, entity=proj,
                     entity_kind="project", source_type="user_stated")


@rule("fact", r"^(?:please\s+)?(?:remember|note|keep in mind|fyi)(?:\s+(?:privately|in private))?(?: that)?[,:]?\s+"
              r"(?P<stmt>.{5,})$")
def _remember(m, s):
    return Candidate("fact", _cap(m.group("stmt")) + ".", s, explicit=True, source_type="user_confirmed",
                     confidence=1.0)


@rule("fact", r"^(?P<subj>(?:my|our)\s+[\w' -]{2,30}?|[A-Z][\w-]+(?:\s+[A-Z][\w-]+)?'s\s+[\w' -]{2,30}?)\s+"
              r"(?P<verb>is|are|was|lives in|works at)\s+(?P<obj>.{2,60})$")
def _possessive_fact(m, s):
    subj, verb, obj = _clean(m.group("subj")), m.group("verb").lower(), _clean(m.group("obj"))
    owner = None
    pm = re.match(r"^([A-Z][\w-]+(?:\s+[A-Z][\w-]+)?)'s\s+(.+)$", subj)
    if pm:
        owner, attr = pm.group(1), pm.group(2)
    else:
        attr = re.sub(r"^(?:my|our)\s+", "", subj, flags=re.I)
    if verb == "was":
        return None  # past state: not a current fact
    content = f"{subj[:1].upper() + subj[1:]} {verb} {obj}."
    return Candidate("fact", content, s, subject=owner or "operator", predicate=attr.lower(), object=obj,
                     entity=owner or "operator", entity_kind="person")


@rule("fact", r"^(?P<who>[A-Z][\w-]+(?:\s+[A-Z][\w-]+)?)\s+(?P<pred>works at|works for|lives in|moved to|joined|is from|"
              r"leads|manages|is the\s+[\w ]{2,25}?\s+(?:of|at|for))\s+(?P<obj>.{2,50})$")
def _person_fact(m, s):
    who, pred, obj = _clean(m.group("who")), m.group("pred").lower(), _clean(m.group("obj"))
    if who.lower() in ("i", "we", "it", "this", "that", "he", "she", "they"):
        return None
    canon = {"works for": "works at", "moved to": "lives in", "joined": "works at"}.get(pred, pred)
    return Candidate("fact", f"{who} {pred} {obj}.", s, subject=who, predicate=canon, object=obj, entity=who,
                     entity_kind="person")


@rule("fact", r"^i\s+(?:work at|work for|live in|moved to|am from|'m from)\s+(?P<obj>.{2,50})$")
def _self_fact(m, s):
    pred = "lives in" if re.search(r"live|moved", s, re.I) else ("is from" if "from" in s.lower() else "works at")
    obj = _clean(m.group("obj"))
    return Candidate("fact", f"The operator {pred} {obj}.", s, subject="operator", predicate=pred, object=obj,
                     entity="operator", entity_kind="person")


@rule("lesson", r"^(?:lesson learned|note to self|next time)[,:]?\s+(?P<what>.{5,})$")
def _lesson(m, s):
    return Candidate("lesson", _cap(m.group("what")) + ".", s, explicit=True, source_type="user_confirmed")


_PRIVATE_CUE = re.compile(r"\b(?:privately|in private|between (?:you and me|us)|keep (?:this|it) (?:private|secret|"
                          r"to yourself)|don'?t (?:tell|share|mention) (?:this|it|anyone))\b", re.I)
_PRIVATE_WORDS = re.compile(r"\s*\b(?:privately|in private|between (?:you and me|us))\b,?", re.I)


class RuleExtractor:
    name = "rules"

    def extract(self, text: str, reference: Optional[str] = None) -> List[Candidate]:
        out = self._extract(text, reference)
        if _PRIVATE_CUE.search(text or ""):  # "remember privately that...", "...keep this between us"
            for c in out:
                c.meta["private"] = True
                cleaned = _cap(_PRIVATE_WORDS.sub("", c.content).strip())
                c.content = cleaned + ("." if c.content.endswith(".") and not cleaned.endswith(".") else "")
        return out

    def _extract(self, text: str, reference: Optional[str] = None) -> List[Candidate]:
        out: List[Candidate] = []
        for sentence in _sentences(text):
            s = sentence.strip().rstrip(".!")
            if re.fullmatch(r"(?:and\s+)?(?:please\s+)?keep (?:this|it) (?:private|secret|to yourself|between us)", s,
                            re.I):
                continue
            if not s or (_QUESTION.search(sentence) and not re.match(r"^(?:no|actually|remember)\b", s, re.I)):
                continue
            # "I think Meera works at Globex" is the same statement, held less firmly
            s = _LEAD_HEDGE.sub("", s) or s
            for kind, rx, fn in _RULES:
                m = rx.search(s)
                if not m:
                    continue
                cand = fn(m, sentence.strip())
                if cand is None:
                    continue
                cand.hedged = bool(_HEDGE.search(sentence))
                if cand.hedged:
                    cand.confidence = min(cand.confidence, 0.5)
                    cand.source_type = "user_stated"
                cand.reference = reference
                out.append(cand)
                break
        return out


# ── model-based extraction ─────────────────────────────────────
_LLM_SYSTEM = """You extract long-term memories from ONE message the operator said to their assistant.
Return JSON: {"memories": [...]}; an empty list is the usual answer. Only extract what the operator clearly
stated about themselves, people they know, their projects, decisions, preferences, standing rules, commitments
with deadlines, or corrections of something said earlier. Never extract questions, small talk, guesses about
the operator, or anything that looks like a password, code, key or card number.
Each item: {"kind": one of fact|preference|decision|commitment|project_state|lesson|correction|rule,
"content": the memory as one short third-person sentence,
"evidence": the EXACT words from the message it comes from (copied, not paraphrased),
optional "subject", "predicate", "object", "key", "value", "old_value", "due", "entity", "entity_kind"
(person|project|organization|place|tool|other), "confidence" 0..1}."""


class LLMExtractor:
    name = "llm"

    def __init__(self, chat: Optional[Callable[..., Any]] = None):
        self._chat = chat

    @staticmethod
    def enabled() -> bool:
        flag = os.environ.get("JARVIS_MEMORY_LLM")
        if flag is not None and flag.strip() in ("0", "false", "no", ""):
            return False
        try:
            from core.agents.supervisor import llm_enabled
            return llm_enabled()
        except Exception:
            return False

    def _call(self, system: str, user: str) -> str:
        chat = self._chat
        if chat is None:
            from core.agents.supervisor import llm_chat as chat
        res = chat(system, user, json_mode=True, max_tokens=600, timeout=20.0, temperature=0.0)
        if asyncio.iscoroutine(res):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(res)
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(1) as ex:  # called from inside an event loop
                return ex.submit(asyncio.run, res).result()
        return res

    def extract(self, text: str, reference: Optional[str] = None) -> List[Candidate]:
        safe = redact(text)
        try:
            raw = self._call(_LLM_SYSTEM, f"Message: {safe}")
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1]) if raw else {}
        except Exception as exc:
            logger.info("[memory] model extraction skipped: %s", type(exc).__name__)
            return []
        out = []
        for item in (data.get("memories") or [])[:8]:
            if not isinstance(item, dict) or item.get("kind") not in KINDS:
                continue
            try:
                conf = float(item.get("confidence", 0.7))
            except (TypeError, ValueError):
                conf = 0.7
            out.append(Candidate(
                kind=item["kind"], content=_clean(item.get("content", "")) + ".", evidence=str(item.get("evidence", "")),
                subject=item.get("subject"), predicate=item.get("predicate"), object=item.get("object"),
                key=item.get("key"), value=item.get("value"), old_value=item.get("old_value"), due=item.get("due"),
                entity=item.get("entity"), entity_kind=item.get("entity_kind") or "other",
                confidence=max(0.0, min(0.9, conf)),  # a model's reading is never "confirmed"
                source_type="user_stated", extractor="llm", reference=reference))
        return out


# only messages that could hold a memory go to the model (not "open chrome", "what's the weather")
_MEMORY_CUE = re.compile(r"\b(?:i|i'm|i've|i'll|my|me|we|we're|our|us|remember|prefer|always|never|decided|project|"
                         r"remind|promise|actually|wrong|birthday|works?|lives?|deadline|due)\b", re.I)


def grounded(c: Candidate, message: str) -> bool:
    """The candidate's evidence must be words the operator actually said."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()  # noqa: E731
    ev = norm(c.evidence)
    return len(ev) >= 4 and ev in norm(message)


def extract(text: str, reference: Optional[str] = None, use_llm: Optional[bool] = None,
            llm: Optional[LLMExtractor] = None) -> List[Candidate]:
    """Rule candidates plus (optionally) model candidates, grounded, secret-free, de-duplicated."""
    text = str(text or "").strip()
    if not text:
        return []
    cands = RuleExtractor().extract(text, reference)
    if use_llm is None:
        use_llm = LLMExtractor.enabled()
    if use_llm and len(text) > 12 and _MEMORY_CUE.search(text):
        cands += (llm or LLMExtractor()).extract(text, reference)
    out, seen = [], set()
    for c in cands:
        if not c.content.strip(". ") or not grounded(c, text):
            continue
        if contains_secret(c.content) or contains_secret(c.evidence):
            continue
        key = (c.kind, re.sub(r"\W+", " ", c.content.lower()).strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out
