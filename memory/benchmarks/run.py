"""Fixed memory benchmark conversations, run after every memory change.

    python -m memory.benchmarks.run                    # run, compare with baseline.json, exit 1 on regression
    python -m memory.benchmarks.run --update-baseline  # accept the current results as the new baseline
    python -m memory.benchmarks.run --json             # machine-readable report

Each case in conversations/*.json plays a scripted history into a fresh,
temporary memory database (never the real one), applies operations
(corrections, deletions, a simulated crash...) and checks what JARVIS can
recall afterwards. Cases that need a capability not built yet are reported as
"pending" rather than failing; they start counting as soon as the capability
is listed in CAPABILITIES.

Metrics (over all checks that ran):
  recall_precision   expected items among the results returned / results returned
  recall_coverage    expected items found / expected items
  incorrect_rate     checks where a forbidden (outdated / deleted) item came back
  duplicate_rate     duplicate active statements / active memories
  write_failures     memory writes that raised
  latency_ms_p50/max per recall query
  context_tokens     ~tokens of the recalled text handed to a model (chars / 4)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

HERE = Path(__file__).resolve().parent
CASES_DIR = HERE / "conversations"
BASELINE = HERE / "baseline.json"

# What the memory system can do today. Later milestones add theirs here.
CAPABILITIES = {"keyword", "sessions", "versioning", "deletion", "temporal", "sources", "dedupe", "crash_recovery",
                "export", "stores", "vector", "hybrid", "entity_resolution", "context_builder",
                "contradiction_detection", "extraction", "consolidation", "agent_memory_interface",
                "retention", "forget_everywhere", "privacy", "recovery"}


def detected_capabilities() -> set:
    """CAPABILITIES plus what this machine has: a neural embedder makes paraphrase cases runnable."""
    from memory.retrieval.semantic_search import get_embedder
    return set(CAPABILITIES) | ({"neural_embeddings"} if getattr(get_embedder(), "neural", False) else set())

# Tolerances before a change counts as a regression.
TOLERANCE = {"recall_precision": 0.02, "recall_coverage": 0.02, "pass_rate": 0.0}


def default_recall(mm, query: str, k: int, filters: Dict[str, Any]):
    """The full retrieval engine (milestone 3): hybrid search, ranking, entity resolution."""
    return mm.retriever.recall(query, k=k, **filters)


def keyword_recall(mm, query: str, k: int, filters: Dict[str, Any]):
    """Milestone 1 behaviour, kept for comparison: keyword search only."""
    return mm.search(query, limit=k, **filters)


def _when(value):
    """"$now" / "$yesterday" / "$days_ago:N" in case files become real timestamps at run time."""
    if not (isinstance(value, str) and value.startswith("$")):
        return value
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if value == "$now":
        dt = now
    elif value == "$yesterday":
        local = now.astimezone()
        dt = local.replace(hour=12, minute=0, second=0) - timedelta(days=1)
    elif value.startswith("$days_ago:"):
        dt = now - timedelta(days=int(value.split(":", 1)[1]))
    else:
        return value
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class CaseRunner:
    def __init__(self, case: Dict[str, Any], recall: Callable = default_recall):
        from memory.core.memory_manager import MemoryManager
        self.case = case
        self.recall = recall
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis-membench-")
        self.db = Path(self.tmp.name) / "bench.db"
        self.mm = MemoryManager(path=self.db)
        self.ids: Dict[str, str] = {}   # case-local name -> memory_id
        self.write_failures = 0
        self.latencies: List[float] = []
        self.tokens = 0
        self.tp = self.returned = self.expected = self.found = 0
        self.incorrect = 0

    def close(self):
        try:
            self.mm.repo.close()
        finally:
            self.tmp.cleanup()

    # ── setup ──────────────────────────────────────────────────
    def play(self):
        for s in self.case.get("sessions", []):
            s = {k: _when(v) for k, v in s.items()}
            sid = self.mm.repo.start_session(s.get("id"), started_at=s["started_at"])
            for m in s.get("messages", []):
                self.mm.repo.add_message(sid, m["role"], m["content"], at=_when(m.get("at")) or s["started_at"])
            if s.get("ended_at"):
                from narrative.session_memory import summarise_messages
                summary = summarise_messages(s.get("messages", []))
                self.mm.repo.end_session(sid, ended_at=s["ended_at"], summary=summary["text"], data=summary)
        for m in self.case.get("memories", []):
            m = {k: _when(v) for k, v in m.items()}
            name = m.pop("name", None)
            try:
                rec = self.mm.remember(**m)
                if name:
                    self.ids[name] = rec.memory_id
            except Exception:
                self.write_failures += 1

    def _ref(self, name: str) -> str:
        return self.ids.get(name, name)

    def operate(self):
        for op in self.case.get("operations", []):
            kind = op["op"]
            try:
                if kind == "correct":
                    rec = self.mm.correct(self._ref(op["memory"]), op["to"])
                    if op.get("name"):
                        self.ids[op["name"]] = rec.memory_id
                elif kind == "forget":
                    self.mm.forget(self._ref(op["memory"]))
                elif kind == "forget_about":
                    self.mm.forget_about(op["entity"])
                elif kind == "remember":
                    m = dict(op["memory"])
                    name = m.pop("name", None)
                    rec = self.mm.remember(**m)
                    if name:
                        self.ids[name] = rec.memory_id
                elif kind == "store":
                    out = self._call_store(op)
                    if op.get("name") and hasattr(out, "memory_id"):
                        self.ids[op["name"]] = out.memory_id
                elif kind == "consolidate":
                    self.mm.consolidator.use_llm = False  # benchmarks are offline and deterministic
                    self.last_report = self.mm.consolidate(op.get("session"))
                elif kind == "agent_run":
                    from memory.core.agent_memory import agent_memory
                    agent_memory(op["agent"], self.mm).record_run(op["goal"], op.get("outcome", "success"),
                                                                  summary=op.get("summary", ""),
                                                                  tools=op.get("tools", []),
                                                                  failed_tools=op.get("failed_tools"),
                                                                  task_id=op.get("task_id"))
                elif kind == "agent_remember":
                    from memory.core.agent_memory import agent_memory
                    agent_memory(op["agent"], self.mm).remember(**op["memory"])
                elif kind == "retention":
                    from datetime import datetime, timedelta, timezone
                    from memory.maintenance.memory_cleanup import retention
                    now = datetime.now(timezone.utc) + timedelta(days=op.get("days_later", 0))
                    self.last_report = retention(self.mm, now=now)
                elif kind == "forget_everywhere":
                    from memory.maintenance.memory_cleanup import forget_everywhere
                    self.last_report = forget_everywhere(self.mm, text=op.get("text"), entity=op.get("entity"),
                                                         include_legacy=False, purge_backups=op.get("backups", False))
                elif kind == "backup":
                    from memory.maintenance.memory_backup import backup
                    backup(self.mm, label="bench")
                elif kind == "corrupt_reopen":
                    from memory.maintenance.memory_recovery import open_safely
                    self.mm.repo.close()
                    for suffix in ("-wal", "-shm"):
                        side = Path(str(self.db) + suffix)
                        if side.exists():
                            side.unlink()
                    with open(self.db, "r+b") as fh:  # wreck the header and the first pages
                        fh.write(b"\x00" * 4096)
                    self.mm = open_safely(self.db)
                elif kind == "crash_reopen":
                    self._crash(op.get("uncommitted", "half-written memory"))
                elif kind == "export_import":
                    payload = self.mm.export()
                    self.mm.repo.close()
                    from memory.core.memory_manager import MemoryManager
                    self.db = Path(self.tmp.name) / "imported.db"
                    self.mm = MemoryManager(path=self.db)
                    self.mm.import_from(payload)
                else:
                    raise ValueError(f"unknown op {kind}")
            except Exception:
                self.write_failures += 1

    def _call_store(self, spec: Dict[str, Any]):
        """{"store": "prospective", "method": "commit", "args": [...], "kwargs": {...}}; "$name" args are
        replaced by the memory id recorded under that name."""
        target = getattr(self.mm, spec["store"])
        *path, method = spec["method"].split(".")
        for attr in path:
            target = getattr(target, attr)
        fix = lambda v: self._ref(v[1:]) if isinstance(v, str) and v.startswith("$") else v  # noqa: E731
        return getattr(target, method)(*[fix(a) for a in spec.get("args", [])],
                                              **{k: fix(v) for k, v in spec.get("kwargs", {}).items()})

    def _crash(self, text: str):
        """Start a write, kill the connection before commit, reopen."""
        from memory.core.memory_manager import MemoryManager
        conn = sqlite3.connect(str(self.db), isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO memories(memory_id, type, category, content, content_hash, source_type, "
                     "verification_status, created_at, updated_at) VALUES ('crash_1','semantic','general',?,'x',"
                     "'agent','unverified','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')", (text,))
        self.mm.repo.close()
        conn.close()  # never committed
        self.mm = MemoryManager(path=self.db)

    # ── checks ─────────────────────────────────────────────────
    def _texts(self, records) -> List[str]:
        return [r.content.lower() for r in records]

    def _score(self, texts: List[str], check: Dict[str, Any]) -> Dict[str, Any]:
        want = [w.lower() for w in check.get("expect", [])]
        banned = [w.lower() for w in check.get("absent", [])]
        found = [w for w in want if any(w in t for t in texts)]
        relevant = sum(1 for t in texts if any(w in t for w in want)) if want else 0
        leaked = [b for b in banned if any(b in t for t in texts)]
        self.expected += len(want)
        self.found += len(found)
        if want:
            self.tp += relevant
            self.returned += len(texts)
        if leaked:
            self.incorrect += 1
        ok = len(found) == len(want) and not leaked
        return {"ok": ok, "missing": [w for w in want if w not in found], "leaked": leaked}

    def check(self, c: Dict[str, Any]) -> Dict[str, Any]:
        kind = c["kind"]
        if kind == "recall":
            t0 = time.perf_counter()
            res = self.recall(self.mm, c["query"], c.get("k", 3), c.get("filters", {}))
            self.latencies.append((time.perf_counter() - t0) * 1000)
            self.tokens += sum(len(r.content) for r in res) // 4
            return self._score(self._texts(res), c)
        if kind == "session_recall":
            hits = self.mm.repo.search_messages(c["query"], limit=c.get("k", 5))
            return self._score([h["content"].lower() for h in hits], c)
        if kind == "session_summary":
            sessions = self.mm.repo.recent_sessions(limit=1, ended_only=True)
            return self._score([(sessions[0]["summary"] or "").lower()] if sessions else [], c)
        if kind == "about":
            res = self.mm.about(c["entity"], valid_at=c.get("valid_at"), **c.get("filters", {}))
            return self._score(self._texts(res), c)
        if kind == "source":
            res = self.recall(self.mm, c["query"], 1, {})
            if not res:
                return {"ok": False, "missing": ["any result"], "leaked": []}
            src = self.mm.repo.sources(res[0].memory_id)
            ok = any(s["source_type"] == c["source_type"] and (not c.get("reference") or
                                                                 s["reference"] == c["reference"]) for s in src)
            return {"ok": ok and res[0].verification_status == c.get("verification", res[0].verification_status),
                    "missing": [] if ok else [c["source_type"]], "leaked": []}
        if kind == "versions":
            rid = self._ref(c["memory"])
            lineage = self.mm.repo.lineage(rid)
            ok = len(lineage) == c["lineage"] and lineage[-1].status == "active"
            return {"ok": ok, "missing": [] if ok else [f"lineage {len(lineage)} != {c['lineage']}"], "leaked": []}
        if kind == "count":
            n = self.mm.repo.count(**c.get("filters", {}))
            ok = n == c["equals"]
            return {"ok": ok, "missing": [] if ok else [f"count {n} != {c['equals']}"], "leaked": []}
        if kind == "sources_count":
            res = self.recall(self.mm, c["query"], 1, {})
            n = len(self.mm.repo.sources(res[0].memory_id)) if res else 0
            ok = n == c["equals"]
            return {"ok": ok, "missing": [] if ok else [f"sources {n} != {c['equals']}"], "leaked": []}
        if kind == "absent_everywhere":
            needle = c["text"].lower()
            conn = self.mm.repo.raw()
            places = {"memories": "SELECT content FROM memories", "versions": "SELECT snapshot FROM memory_versions",
                      "sources": "SELECT COALESCE(excerpt,'') || ' ' || COALESCE(reference,'') FROM memory_sources",
                      "entities": "SELECT name || ' ' || aliases FROM entities",
                      "access_log": "SELECT COALESCE(detail,'') FROM memory_access_log",
                      "search_index": "SELECT content FROM memories_fts",
                      "messages": "SELECT content FROM session_messages",
                      "session_summaries": "SELECT COALESCE(summary,'') || ' ' || data FROM sessions"}
            leaked = [p for p, sql in places.items() if any(needle in str(r[0]).lower() for r in conn.execute(sql))]
            if leaked:
                self.incorrect += 1
            return {"ok": not leaked, "missing": [], "leaked": leaked}
        if kind == "store":
            out = self._call_store(c)
            if isinstance(out, dict):
                texts = [json.dumps(out).lower()]
            elif isinstance(out, (list, tuple)):
                texts = [(x.content if hasattr(x, "content") else json.dumps(x, default=str)).lower() for x in out]
            else:
                texts = [] if out is None else [str(getattr(out, "content", out)).lower()]
            if "first" in c:
                ok = bool(texts) and c["first"].lower() in texts[0]
                return {"ok": ok, "missing": [] if ok else [f"first={c['first']}"], "leaked": []}
            return self._score(texts, c)
        if kind == "context":
            t0 = time.perf_counter()
            pkg = self.mm.retriever.context(c["query"], k=c.get("k", 10), budget_tokens=c.get("budget", 600))
            self.latencies.append((time.perf_counter() - t0) * 1000)
            self.tokens += pkg.tokens
            res = self._score([pkg.text.lower()], c)
            missing_sections = [s for s in c.get("sections", []) if s not in pkg.sections]
            over = pkg.tokens > c.get("max_tokens", 10 ** 9)
            ok = res["ok"] and not missing_sections and not over
            return {"ok": ok, "missing": res["missing"] + missing_sections + (["over budget"] if over else []),
                    "leaked": res["leaked"]}
        if kind == "agent_context":
            from memory.core.agent_memory import agent_memory
            t0 = time.perf_counter()
            pkg = agent_memory(c["agent"], self.mm).context(c["query"])
            self.latencies.append((time.perf_counter() - t0) * 1000)
            self.tokens += pkg.tokens
            return self._score([pkg.text.lower()], c)
        if kind == "consolidation_report":
            rep = getattr(self, "last_report", {}) or {}
            texts = [json.dumps(rep.get(c["field"], ""), default=str).lower()]
            return self._score(texts, c)
        if kind == "agent_recall":
            from memory.core.agent_memory import agent_memory
            recs = agent_memory(c["agent"], self.mm).recall(c["query"], explicit=c.get("explicit", False))
            return self._score([r.content.lower() for r in recs], c)
        if kind == "meta":
            val = self.mm.repo.get_meta(c["key"]) or ""
            return self._score([val.lower()], c)
        if kind == "integrity":
            from memory.maintenance.memory_integrity import check as integrity
            rep = integrity(self.mm)
            return {"ok": rep["ok"], "missing": rep["problems"], "leaked": []}
        raise ValueError(f"unknown check {kind}")

    def duplicate_rate(self) -> float:
        conn = self.mm.repo.raw()
        active = conn.execute("SELECT COUNT(*) FROM memories WHERE status='active' AND type != 'episodic'").fetchone()[0]
        distinct = conn.execute("SELECT COUNT(DISTINCT content_hash) FROM memories WHERE status='active' "
                                "AND type != 'episodic'").fetchone()[0]
        return (active - distinct) / active if active else 0.0


def load_cases(names: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    cases = []
    for f in sorted(CASES_DIR.glob("*.json")):
        for case in json.loads(f.read_text(encoding="utf-8")):
            case["_file"] = f.name
            if not names or case["id"] in names:
                cases.append(case)
    return cases


def run(names: Optional[List[str]] = None, recall: Callable = default_recall,
        capabilities=None) -> Dict[str, Any]:
    capabilities = detected_capabilities() if capabilities is None else capabilities
    results, lat, totals = [], [], {"tp": 0, "returned": 0, "expected": 0, "found": 0, "incorrect": 0,
                                     "write_failures": 0, "tokens": 0, "checks": 0}
    dup_rates = []
    for case in load_cases(names):
        missing = sorted(set(case.get("requires", [])) - set(capabilities))
        if missing:
            results.append({"id": case["id"], "status": "pending", "needs": missing, "behaviour": case.get("behaviour")})
            continue
        r = CaseRunner(case, recall)
        try:
            r.play()
            r.operate()
            checks = []
            for c in case["checks"]:
                try:
                    checks.append({"kind": c["kind"], **r.check(c)})
                except Exception as exc:
                    checks.append({"kind": c["kind"], "ok": False, "missing": [f"error: {exc}"], "leaked": []})
            dup_rates.append(r.duplicate_rate())
        finally:
            r.close()
        for k, v in (("tp", r.tp), ("returned", r.returned), ("expected", r.expected), ("found", r.found),
                     ("incorrect", r.incorrect), ("write_failures", r.write_failures), ("tokens", r.tokens),
                     ("checks", len(checks))):
            totals[k] += v
        lat += r.latencies
        ok = all(c["ok"] for c in checks) and not r.write_failures
        results.append({"id": case["id"], "status": "pass" if ok else "fail", "behaviour": case.get("behaviour"),
                        "checks": [c for c in checks if not c["ok"]] if not ok else []})
    ran = [r for r in results if r["status"] != "pending"]
    metrics = {
        "pass_rate": round(sum(r["status"] == "pass" for r in ran) / len(ran), 4) if ran else 0.0,
        "recall_precision": round(totals["tp"] / totals["returned"], 4) if totals["returned"] else 1.0,
        "recall_coverage": round(totals["found"] / totals["expected"], 4) if totals["expected"] else 1.0,
        "incorrect_rate": round(totals["incorrect"] / totals["checks"], 4) if totals["checks"] else 0.0,
        "duplicate_rate": round(max(dup_rates), 4) if dup_rates else 0.0,
        "write_failures": totals["write_failures"],
        "latency_ms_p50": round(statistics.median(lat), 2) if lat else 0.0,
        "latency_ms_max": round(max(lat), 2) if lat else 0.0,
        "context_tokens": totals["tokens"],
        "cases": {"pass": sum(r["status"] == "pass" for r in results), "fail": sum(r["status"] == "fail" for r in results),
                  "pending": sum(r["status"] == "pending" for r in results)},
    }
    return {"metrics": metrics, "results": results}


def regressions(report: Dict[str, Any], baseline: Dict[str, Any]) -> List[str]:
    out = []
    m, b = report["metrics"], baseline.get("metrics", {})
    for key, tol in TOLERANCE.items():
        if key in b and m[key] < b[key] - tol:
            out.append(f"{key} fell {b[key]} -> {m[key]}")
    for key in ("incorrect_rate", "duplicate_rate"):
        if key in b and m[key] > b[key]:
            out.append(f"{key} rose {b[key]} -> {m[key]}")
    if m["write_failures"] > b.get("write_failures", 0):
        out.append(f"write_failures rose to {m['write_failures']}")
    passed_before = set(baseline.get("passing", []))
    now_failing = [r["id"] for r in report["results"] if r["status"] == "fail" and r["id"] in passed_before]
    if now_failing:
        out.append("cases that passed before now fail: " + ", ".join(now_failing))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("cases", nargs="*")
    args = ap.parse_args(argv)
    report = run(args.cases or None)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    problems = regressions(report, baseline) if baseline and not args.cases else []
    if args.update_baseline:
        BASELINE.write_text(json.dumps({"metrics": {k: v for k, v in report["metrics"].items()
                                                    if not k.startswith("latency")},
                                        "passing": [r["id"] for r in report["results"] if r["status"] == "pass"]},
                                       indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps({**report, "regressions": problems}, indent=2))
    else:
        for r in report["results"]:
            mark = {"pass": "PASS", "fail": "FAIL", "pending": "....", }[r["status"]]
            extra = f"  (needs {', '.join(r['needs'])})" if r["status"] == "pending" else ""
            print(f"  {mark}  {r['id']:<28} {r.get('behaviour') or ''}{extra}")
            for c in r.get("checks", []):
                print(f"          - {c['kind']}: missing={c['missing']} leaked={c['leaked']}")
        m = report["metrics"]
        print(f"\n  cases: {m['cases']}  precision={m['recall_precision']}  coverage={m['recall_coverage']}  "
              f"incorrect={m['incorrect_rate']}  duplicates={m['duplicate_rate']}  write_failures={m['write_failures']}  "
              f"latency p50={m['latency_ms_p50']}ms  context≈{m['context_tokens']} tokens")
        for p in problems:
            print(f"  REGRESSION: {p}")
        if args.update_baseline:
            print(f"  baseline updated: {BASELINE}")
    return 1 if problems or (not baseline and report["metrics"]["cases"]["fail"]) else 0


if __name__ == "__main__":
    sys.exit(main())
