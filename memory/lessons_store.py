# memory/lessons_store.py
"""
Persistent lesson store for false-positive memory.
Uses ChromaDB + sentence-transformers for semantic search when available,
falls back to a simple JSON file store otherwise.

Every lesson has a stable id (from its trigger + platform), shared by the JSON
record, its vector and the unified memory database, so updating a lesson also
replaces its vector instead of leaving the old text searchable.
"""
import hashlib
import json
import logging
import re
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
_chroma_init_warning_logged = False

LESSONS_DIR = Path(__file__).parent.parent / "cases" / ".lessons"


def lesson_id(trigger: str, platform: str) -> str:
    """Stable id for one (trigger, platform) lesson."""
    key = f"{str(platform or 'general').strip().lower()}\x1f{str(trigger or '').strip().lower()}"
    return "les_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class LessonsStore:
    """
    Stores lessons learned from false-positive reports.
    Two backends:
      1. ChromaDB + sentence-transformers (semantic RAG)
      2. JSON flat-file fallback (keyword match)
    """

    def __init__(self, base_dir: Path | None = None, use_vectors: bool = True):
        self._chroma = None
        self._collection = None
        self._available = False
        self._dir = Path(base_dir) if base_dir else LESSONS_DIR
        # Mirrored into the unified memory database only for the real store (never a test copy).
        self._mirror = self._dir.resolve() == LESSONS_DIR.resolve()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._json_path = self._dir / "lessons.json"
        self._lessons = self._load_json()
        for entry in self._lessons:
            entry.setdefault("id", lesson_id(entry.get("trigger", ""), entry.get("platform", "general")))
        if use_vectors:
            self._init_chroma()
            self._resync_vectors()

    # ── ChromaDB bootstrap ────────────────────────────────────
    def _init_chroma(self):
        global _chroma_init_warning_logged
        try:
            import chromadb

            client = chromadb.PersistentClient(path=str(self._dir / "chroma"))
            self._collection = client.get_or_create_collection(
                name="lessons",
                metadata={"hnsw:space": "cosine"},
            )
            self._chroma = client
            self._available = True
        except Exception as e:
            # ChromaDB or sentence-transformers not installed — use JSON fallback
            if not _chroma_init_warning_logged:
                logger.warning(f"[lessons_store] ChromaDB initialization failed ({e}), falling back to JSON store.")
                _chroma_init_warning_logged = True
            self._available = False

    def _resync_vectors(self):
        """Make the vector index match the JSON lessons exactly: one vector per
        lesson under its stable id, with its current text. Older builds keyed
        vectors by list position and never refreshed them on update, so the
        index could return stale or duplicate lessons."""
        if not (self._available and self._collection is not None):
            return
        try:
            wanted = {e["id"]: e for e in self._lessons}
            have = set(self._collection.get(include=[])["ids"])
            stale = sorted(have - set(wanted))
            if stale:
                self._collection.delete(ids=stale)
            missing = [e for i, e in wanted.items() if i not in have]
            self._upsert_vectors(missing)
        except Exception as e:
            logger.warning(f"[lessons_store] vector resync failed ({e}); keyword search still works.")

    def _upsert_vectors(self, entries: list):
        if not (self._available and self._collection is not None) or not entries:
            return
        try:
            self._collection.upsert(
                ids=[e["id"] for e in entries],
                documents=[f"{e.get('trigger', '')} | {e.get('lesson', '')}" for e in entries],
                metadatas=[{"platform": e.get("platform", "general"), "context": e.get("context", "general")}
                           for e in entries],
            )
        except Exception:
            pass

    # ── JSON fallback I/O ─────────────────────────────────────
    def _load_json(self) -> list:
        if self._json_path.exists():
            try:
                return json.loads(self._json_path.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save_json(self):
        tmp = self._json_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._lessons, indent=2), encoding="utf-8")
        tmp.replace(self._json_path)

    # ── Public API ────────────────────────────────────────────
    def add_lesson(
        self,
        trigger: str,
        lesson: str,
        platform: str,
        context: str = "general",
    ) -> bool:
        """
        Store a lesson. Updates existing lesson in place if trigger and platform match,
        or appends a new lesson. Returns True on success.
        """
        clean_trigger = (trigger or "").strip()
        clean_platform = (platform or "general").strip().lower()
        lid = lesson_id(clean_trigger, clean_platform)
        entry = next((e for e in self._lessons if e.get("id") == lid), None)
        if entry is not None:  # update in place, and replace its vector (was: vector left stale)
            entry.update(lesson=lesson, context=context, timestamp=_utc_now())
        else:
            entry = {
                "id": lid,
                "trigger": clean_trigger,
                "lesson": lesson,
                "platform": clean_platform,
                "context": context,
                "timestamp": _utc_now(),
            }
            self._lessons.append(entry)

        # Always persist to JSON regardless of Chroma availability
        self._save_json()
        self._upsert_vectors([entry])
        if self._mirror:
            from memory.core import legacy_bridge
            legacy_bridge.lesson(clean_trigger, lesson, clean_platform, context)
        return True

    def has_platform_warning(self, platform: str) -> str | None:
        """
        Check if there's a stored lesson warning about a platform.
        Returns the lesson text if found, None otherwise.
        """
        platform_lower = platform.lower()

        # Try ChromaDB semantic search first
        if self._available and self._collection is not None:
            try:
                results = self._collection.query(
                    query_texts=[f"{platform} false positive unreliable"],
                    n_results=1,
                    where={"platform": platform_lower},
                )
                if results and results["documents"] and results["documents"][0]:
                    return results["documents"][0][0]
            except Exception:
                pass

        # JSON fallback — simple keyword match
        for entry in reversed(self._lessons):
            if entry.get("platform", "").lower() == platform_lower:
                return entry.get("lesson", "")

        return None

    def get_lessons_for_context(self, context: str = "general") -> list:
        """Return all lessons matching a given context (exact match, as before)."""
        return [
            entry for entry in self._lessons
            if entry.get("context", "general") == context
        ]

    def search_lessons(self, query: str, platform: str | None = None, context: str | None = None,
                       limit: int = 5) -> list:
        """Lessons relevant to free text, optionally restricted by platform / context.
        Semantic (vector) ranking when available, keyword overlap otherwise. Each
        result is a copy of the lesson with a 'score' (higher = more relevant)."""
        pool = [e for e in self._lessons
                if (platform is None or e.get("platform") == platform.strip().lower())
                and (context is None or e.get("context", "general") == context)]
        if not pool or not str(query or "").strip():
            return [dict(e) for e in reversed(pool)][:limit]
        by_id = {e["id"]: e for e in pool}
        if self._available and self._collection is not None:
            try:
                where = {}
                if platform is not None:
                    where["platform"] = platform.strip().lower()
                if context is not None:
                    where["context"] = context
                if len(where) > 1:
                    where = {"$and": [{k: v} for k, v in where.items()]}
                res = self._collection.query(query_texts=[query], n_results=min(limit, len(pool)),
                                             where=where or None)
                hits = [dict(by_id[i], score=round(1.0 - float(d), 4))
                        for i, d in zip(res["ids"][0], res["distances"][0]) if i in by_id]
                if hits:
                    return hits
            except Exception:
                pass
        words = set(re.findall(r"[a-z0-9]{3,}", query.lower()))
        scored = []
        for e in pool:
            text = set(re.findall(r"[a-z0-9]{3,}",
                                  f"{e.get('trigger', '')} {e.get('lesson', '')} {e.get('context', '')}".lower()))
            overlap = len(words & text)
            if overlap:
                scored.append(dict(e, score=round(overlap / max(len(words), 1), 4)))
        return sorted(scored, key=lambda e: -e["score"])[:limit]
