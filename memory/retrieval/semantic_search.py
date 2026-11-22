"""Vector (semantic) search over memories, stored in the memory database itself.

Embedders, best first:
  minilm  all-MiniLM-L6-v2 through ONNX (ChromaDB's runtime), used only when the
          model is already on disk: JARVIS never downloads it behind your back.
  hash    a dependency-free feature-hashing embedder (words + character
          trigrams). It finds spelling variants and shared words, not synonyms.
JARVIS_MEMORY_EMBEDDER = auto (default) | minilm | hash.

Vectors live in the memory_vectors table (one per memory per model), so a
deletion removes them with the memory (foreign key cascade) and there is no
second store to drift out of date. Search is exact cosine over the active
memories (numpy), which is fast for the tens of thousands of memories a
personal assistant collects.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9]+")


_STOP = frozenset("""a an the and or but if so to of in on at by for with from into is are was were be been do does
did have has had i me my you your we our it its this that what which who how why when where can could would should
will please tell show give let""".split())


class HashEmbedder:
    """Words, character trigrams and word pairs, hashed into 512 signed buckets.
    Filler words are skipped so "how do I reconect my gogle" is about "reconect gogle"."""
    name = "hash-v2"
    dim = 512
    neural = False

    def _features(self, text: str) -> Iterable[Tuple[str, float]]:
        words = [w for w in _WORD.findall(str(text).lower()) if w not in _STOP] or _WORD.findall(str(text).lower())
        for w in words:
            yield "w:" + (w[:-1] if len(w) > 4 and w.endswith("s") else w), 1.0
            padded = f"#{w}#"
            for i in range(len(padded) - 2):
                yield "c:" + padded[i:i + 3], 0.6
        for a, b in zip(words, words[1:]):
            yield f"b:{a}_{b}", 0.3

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for feat, weight in self._features(text):
                h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little")
                out[row, h % self.dim] += weight if (h >> 63) & 1 else -weight
        return _normalise(out)


class MiniLMEmbedder:
    name = "minilm-l6-v2"
    dim = 384
    neural = True

    def __init__(self):
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
        self._ef = ONNXMiniLM_L6_V2()

    @staticmethod
    def available() -> bool:
        try:
            from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
            root = Path(ONNXMiniLM_L6_V2.DOWNLOAD_PATH)
            return (root / "onnx" / "model.onnx").exists()
        except Exception:
            return False

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _normalise(np.asarray(self._ef(list(texts)), dtype=np.float32))


def _normalise(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(np.float32)


_embedder_lock = threading.Lock()
_embedders: Dict[str, object] = {}


def get_embedder(kind: Optional[str] = None):
    kind = (kind or os.environ.get("JARVIS_MEMORY_EMBEDDER") or "auto").lower()
    with _embedder_lock:
        if kind not in _embedders:
            emb = None
            if kind in ("auto", "minilm") and MiniLMEmbedder.available():
                try:
                    emb = MiniLMEmbedder()
                except Exception as exc:
                    logger.warning("[memory] MiniLM embedder unavailable (%s); using hashing", exc)
            if emb is None and kind == "minilm":
                logger.warning("[memory] MiniLM model not on disk; using the hashing embedder")
            _embedders[kind] = emb or HashEmbedder()
        return _embedders[kind]


def _embed_text(rec) -> str:
    """What gets embedded: the statement plus the few structured words that describe it."""
    extra = [rec.category.replace("_", " ")]
    for key in ("name", "trigger", "goal", "context", "subject", "predicate"):
        v = rec.data.get(key)
        if isinstance(v, str) and v and v.lower() not in rec.content.lower():
            extra.append(v.replace("_", " "))
    return f"{rec.content} ({', '.join(extra)})"


class VectorIndex:
    """Keeps memory_vectors in step with the repository and answers nearest-neighbour queries."""

    def __init__(self, repo, embedder=None):
        self.repo = repo
        self.embedder = embedder or get_embedder()
        repo.on_change(self._on_change)

    @property
    def model(self) -> str:
        return self.embedder.name

    def _on_change(self, action: str, rec):
        if action == "delete":
            return  # cascade removed the vector
        if rec.status != "active":
            return  # superseded / archived: keep the old vector for historical queries
        try:
            self.index([rec])
        except Exception as exc:  # never fail a memory write over its embedding; reindex() catches up
            logger.warning("[memory] embedding failed: %s", exc)

    def index(self, records) -> int:
        records = [r for r in records if r is not None]
        if not records:
            return 0
        conn = self.repo.raw()
        with self.repo._lock:
            have = {row[0]: row[1] for row in conn.execute(
                f"SELECT memory_id, content_hash FROM memory_vectors WHERE model=? AND memory_id IN "
                f"({','.join('?' * len(records))})", [self.model] + [r.memory_id for r in records])}
        todo = [r for r in records if have.get(r.memory_id) != r.hash]
        if not todo:
            return 0
        vecs = self.embedder.embed([_embed_text(r) for r in todo])
        with self.repo._tx() as c:
            for r, v in zip(todo, vecs):
                if c.execute("SELECT 1 FROM memories WHERE memory_id=?", (r.memory_id,)).fetchone():
                    c.execute("INSERT OR REPLACE INTO memory_vectors(memory_id, model, dim, content_hash, vector) "
                              "VALUES (?,?,?,?,?)", (r.memory_id, self.model, int(v.shape[0]), r.hash,
                                                     v.astype(np.float32).tobytes()))
        return len(todo)

    def reindex(self, batch: int = 128) -> int:
        """Embed every memory missing a vector for this model (or whose text changed)."""
        conn = self.repo.raw()
        with self.repo._lock:
            ids = [r[0] for r in conn.execute(
                "SELECT m.memory_id FROM memories m LEFT JOIN memory_vectors v ON v.memory_id=m.memory_id "
                "AND v.model=? WHERE v.memory_id IS NULL OR v.content_hash != m.content_hash", (self.model,))]
        done = 0
        for i in range(0, len(ids), batch):
            done += self.index([self.repo.get(x) for x in ids[i:i + batch]])
        return done

    def search(self, query: str, limit: int = 20, candidate_ids: Optional[Iterable[str]] = None,
               min_score: float = 0.0, **filters) -> List[Tuple[str, float]]:
        """[(memory_id, cosine)] best first, over memories matching the repository filters."""
        if not str(query or "").strip():
            return []
        self.reindex()
        where, params = self.repo._where(**({"status": "active"} | filters))
        sql = (f"SELECT v.memory_id, v.vector FROM memory_vectors v JOIN memories m ON m.memory_id=v.memory_id"
               f"{where}{' AND ' if where else ' WHERE '}v.model=?")
        params = params + [self.model]
        if candidate_ids is not None:
            cids = list(candidate_ids)
            if not cids:
                return []
            sql += f" AND v.memory_id IN ({','.join('?' * len(cids))})"
            params += cids
        with self.repo._lock:
            rows = self.repo.raw().execute(sql, params).fetchall()
        if not rows:
            return []
        q = self.embedder.embed([query])[0]
        mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
        scores = mat @ q
        order = np.argsort(-scores)[:limit]
        return [(rows[i][0], float(scores[i])) for i in order if scores[i] > min_score]
