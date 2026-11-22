"""Where the memory database lives and the default retention policy."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DIR = ROOT / "data" / "memory"
DEFAULT_DB = DEFAULT_DIR / "jarvis_memory.db"


def db_path() -> Path:
    """JARVIS_MEMORY_DB overrides the location (tests point it at a temp folder)."""
    env = os.environ.get("JARVIS_MEMORY_DB")
    return Path(env) if env else DEFAULT_DB


def is_default_db(path) -> bool:
    try:
        return Path(path).resolve() == DEFAULT_DB.resolve()
    except OSError:
        return False


@dataclass(frozen=True)
class RetentionPolicy:
    history_log_max: int = 500           # legacy jarvis_memory.json history_log entries kept inline
    session_messages_days: int = 365     # raw conversation messages kept this long, then archived
    working_memory_hours: int = 24       # working memories expire after this
    access_log_days: int = 90
    backups_kept: int = 7


RETENTION = RetentionPolicy()
