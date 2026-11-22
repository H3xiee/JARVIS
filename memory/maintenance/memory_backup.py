"""Backups of the memory database: consistent online copies, rotated."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from memory.core.memory_config import RETENTION

BACKUP_DIRNAME = "backups"


def backup_dir(mm) -> Path:
    return Path(mm.repo.path).parent / BACKUP_DIRNAME


def backup(mm, label: str = "auto", dest_dir: Optional[Path] = None, keep: int = RETENTION.backups_kept) -> Path:
    dest_dir = Path(dest_dir) if dest_dir else backup_dir(mm)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = mm.repo.backup_to(dest_dir / f"memory-{stamp}-{label}.db")
    rotate(dest_dir, keep)
    return path


def list_backups(dest_dir: Path) -> List[Path]:
    return sorted(Path(dest_dir).glob("memory-*.db"))


def rotate(dest_dir: Path, keep: int) -> None:
    for old in list_backups(dest_dir)[:-keep] if keep > 0 else []:
        try:
            old.unlink()
        except OSError:
            pass


def restore(backup_path: Path, db_path: Path) -> Path:
    """Replace a (closed) database file with a backup, after checking the backup is sound.
    The current file is kept alongside as <name>.before-restore."""
    backup_path, db_path = Path(backup_path), Path(db_path)
    conn = sqlite3.connect(str(backup_path))
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("backup failed its integrity check")
    finally:
        conn.close()
    if db_path.exists():
        shutil.copy2(db_path, db_path.with_suffix(db_path.suffix + ".before-restore"))
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            side.unlink()
    shutil.copy2(backup_path, db_path)
    return db_path
