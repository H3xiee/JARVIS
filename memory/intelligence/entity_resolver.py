"""Entity resolution: recognise different references to the same person / project / tool.

"Ravi", "ravi kumar", "Ravi's", "the assistant project" vs "assistant project",
"JARVIS" vs its alias "the assistant": all resolve to one stored entity, by
(in order) exact name or alias, normalised form (case, articles, possessives,
punctuation), then a fuzzy match strict enough not to merge different people.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional, Tuple

_ARTICLES = {"the", "a", "an", "my", "our", "this", "that"}
_TRAILING = {"project", "app", "application", "repo", "repository"}
_TOKEN = re.compile(r"[a-z0-9]+(?:[.'][a-z0-9]+)*")


def normalise(name: str) -> str:
    words = _TOKEN.findall(str(name or "").lower().replace("’", "'"))
    words = [w[:-2] if w.endswith("'s") else w for w in words]
    while words and words[0] in _ARTICLES:
        words = words[1:]
    return " ".join(words)


def _core(name: str) -> str:
    """normalise() without trailing generic nouns: 'assistant project' -> 'assistant'."""
    words = normalise(name).split()
    while len(words) > 1 and words[-1] in _TRAILING:
        words = words[:-1]
    return " ".join(words)


class EntityResolver:
    FUZZY = 0.88

    def __init__(self, repo):
        self.repo = repo

    def _names(self) -> List[Tuple[str, Dict[str, Any]]]:
        out = []
        for e in self.repo.entities():
            out.append((e["name"], e))
            out += [(a, e) for a in e["aliases"]]
        return out

    def resolve(self, mention: str, kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The stored entity this mention refers to, or None."""
        if not str(mention or "").strip():
            return None
        hit = self.repo.find_entity(mention)
        if hit and (not kind or hit["kind"] == kind):
            return hit
        n, c = normalise(mention), _core(mention)
        if not n:
            return None
        names = [(nm, e) for nm, e in self._names() if not kind or e["kind"] == kind]
        for nm, e in names:
            if normalise(nm) == n or _core(nm) == c:
                return e
        # a first name / leading words that fit exactly one entity ("Ravi" -> "Ravi Kumar")
        starts = {e["entity_id"]: e for nm, e in names if _core(nm).startswith(c + " ")}
        if len(starts) == 1 and len(c) >= 3:
            return next(iter(starts.values()))
        best, best_score = None, 0.0
        for nm, e in names:
            cand = _core(nm)
            if not cand or abs(len(cand) - len(c)) > max(3, len(c) // 3):
                continue
            score = difflib.SequenceMatcher(None, c, cand).ratio()
            # a fuzzy match must still agree on the first letter (Ravi != Kavi)
            if score > best_score and cand[:1] == c[:1]:
                best, best_score = e, score
        return best if best_score >= self.FUZZY and len(c) >= 4 else None

    def resolve_or_create(self, mention: str, kind: str = "other") -> str:
        hit = self.resolve(mention, kind=None)
        if hit:
            if normalise(mention) != normalise(hit["name"]):
                self.repo.upsert_entity(hit["name"], aliases=[mention.strip()], entity_id=hit["entity_id"])
            return hit["entity_id"]
        return self.repo.upsert_entity(mention.strip(), kind)

    def mentions(self, text: str) -> List[Dict[str, Any]]:
        """Known entities mentioned in free text, longest match first, each once."""
        norm_text = " " + normalise(text) + " "
        found, seen = [], set()
        for nm, e in sorted(self._names(), key=lambda x: -len(x[0])):
            for variant in {normalise(nm), _core(nm)}:
                if len(variant) >= 2 and f" {variant} " in norm_text and e["entity_id"] not in seen:
                    seen.add(e["entity_id"])
                    found.append(e)
                    norm_text = norm_text.replace(f" {variant} ", " ")
        # fuzzy single-word names (typos such as "Ravvi")
        for word in set(norm_text.split()):
            if len(word) >= 4 and word not in _ARTICLES:
                e = self.resolve(word)
                if e and e["entity_id"] not in seen:
                    seen.add(e["entity_id"])
                    found.append(e)
        return found

    def merge(self, keep_id: str, drop_id: str) -> str:
        """Fold one entity into another: memories, relationships and names move over."""
        keep, drop = self.repo.get_entity(keep_id), self.repo.get_entity(drop_id)
        if not keep or not drop or keep_id == drop_id:
            return keep_id
        with self.repo._tx() as c:
            c.execute("UPDATE memories SET entity_id=? WHERE entity_id=?", (keep_id, drop_id))
            for col, other in (("src_entity", "dst_entity"), ("dst_entity", "src_entity")):
                c.execute(f"UPDATE OR IGNORE relationships SET {col}=? WHERE {col}=?", (keep_id, drop_id))
            c.execute("DELETE FROM relationships WHERE src_entity=? OR dst_entity=?", (drop_id, drop_id))
            c.execute("DELETE FROM relationships WHERE src_entity=dst_entity")
            c.execute("DELETE FROM entities WHERE entity_id=?", (drop_id,))
            self.repo._log(c, "merge_entity", None, "system", {"kept": keep_id, "dropped": drop_id})
        self.repo.upsert_entity(keep["name"], entity_id=keep_id, aliases=[drop["name"], *drop["aliases"]])
        return keep_id
