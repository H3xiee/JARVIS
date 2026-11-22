"""Opening the memory database safely, and the daily maintenance run.

open_safely(path)   opens the database; if it is unreadable or fails its quick
                    check, the damaged file is set aside (never deleted), the
                    newest backup that passes an integrity check is restored,
                    and if there is none JARVIS starts with an empty memory
                    rather than not starting. What happened is recorded.
run_maintenance(mm) at most once a day: backup, retention, integrity check
                    (with safe repairs), catch-up consolidation of sessions
                    that ended while JARVIS was busy, and vector re-indexing.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)
MAINTENANCE_EVERY = timedelta(hours=20)
_lock = threading.Lock()
_running = set()


def _sound(path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def open_safely(path: Path):
    from memory.core.memory_manager import MemoryManager
    from memory.maintenance.memory_backup import BACKUP_DIRNAME, list_backups, restore
    path = Path(path)
    if not path.exists() or _sound(path):
        try:
            return MemoryManager(path=path)
        except sqlite3.DatabaseError as exc:
            logger.warning("[memory] database could not be opened: %s", exc)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine = path.with_name(f"{path.name}.corrupt-{stamp}")
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(path) + suffix)
        if src.exists():
            shutil.move(str(src), str(quarantine) + suffix)
    restored = None
    for b in reversed(list_backups(path.parent / BACKUP_DIRNAME)):
        try:
            restore(b, path)
            restored = b
            break
        except Exception:
            continue
    mm = MemoryManager(path=path)
    mm.repo.set_meta("recovered", json.dumps({"at": stamp, "quarantined": str(quarantine),
                                              "restored_from": str(restored) if restored else None}))
    logger.warning("[memory] damaged database set aside as %s; %s", quarantine.name,
                   f"restored {restored.name}" if restored else "started with an empty memory")
    return mm


def run_maintenance(mm, force: bool = False, consolidate: bool = True) -> Dict[str, Any]:
    from memory.maintenance import memory_backup, memory_cleanup, memory_integrity
    key = str(mm.repo.path)
    with _lock:
        if key in _running:
            return {"skipped": "already running"}
        last = mm.repo.get_meta("maintenance_at")
        if not force and last:
            try:
                if datetime.now(timezone.utc) - datetime.fromisoformat(last) < MAINTENANCE_EVERY:
                    return {"skipped": "ran recently", "last": last}
            except ValueError:
                pass
        _running.add(key)
    report: Dict[str, Any] = {}
    try:
        report["backup"] = str(memory_backup.backup(mm, label="daily"))
        report["retention"] = memory_cleanup.retention(mm)
        check = memory_integrity.check(mm)
        if not check["ok"]:
            report["repaired"] = check["problems"]
            check = memory_integrity.repair(mm)
        report["integrity_ok"] = check["ok"]
        if consolidate:
            try:
                report["sessions_summarised"] = len(mm.consolidator.summarise_sessions())
            except Exception as exc:
                report["sessions_summarised"] = f"error {type(exc).__name__}"
        try:
            report["reindexed"] = mm.retriever.vectors.reindex() if mm.retriever.vectors else 0
        except Exception as exc:
            report["reindexed"] = f"error {type(exc).__name__}"
        mm.repo.set_meta("maintenance_at", datetime.now(timezone.utc).isoformat())
        mm.repo.set_meta("maintenance_report", json.dumps(report, default=str))
    finally:
        with _lock:
            _running.discard(key)
    return report


def maintain_in_background(mm) -> Optional[threading.Thread]:
    def go():
        try:
            run_maintenance(mm)
        except Exception as exc:
            logger.warning("[memory] maintenance failed: %s", exc)
    t = threading.Thread(target=go, name="memory-maintenance", daemon=True)
    t.start()
    return t
