"""Decide which extracted information deserves long-term storage, and how important it is."""

from __future__ import annotations

BASE = {"preference": 0.8, "rule": 0.9, "decision": 0.8, "commitment": 0.8, "correction": 0.9,
        "project_state": 0.7, "fact": 0.6, "lesson": 0.7}
STORE_THRESHOLD = 0.35


def score(candidate, known_entity: bool = False) -> float:
    s = BASE.get(candidate.kind, 0.4)
    if candidate.explicit:
        s += 0.15          # "remember that..." / "remind me..."
    if candidate.source_type == "user_confirmed":
        s += 0.05
    if known_entity:
        s += 0.05          # about someone / something JARVIS already tracks
    if candidate.hedged:
        s -= 0.25          # "I think...", "maybe..."
    words = len(candidate.content.split())
    if words < 3:
        s -= 0.2
    elif words > 40:
        s -= 0.1           # rambling: probably not one clean fact
    return round(max(0.0, min(1.0, s)), 3)


def worth_storing(candidate, known_entity: bool = False) -> bool:
    return score(candidate, known_entity) >= STORE_THRESHOLD
