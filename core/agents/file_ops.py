# core/agents/file_ops.py
"""
Writing and moving the operator's files, safely.

* Only plain document types can be written (text, Markdown, CSV, JSON, HTML,
  XML, YAML): JARVIS never writes programs or scripts.
* Protected locations (system folders, drive roots, the profile root, JARVIS
  itself) are refused for every source and destination.
* Nothing is overwritten silently: writing over an existing file keeps a backup
  first, and moves / copies never replace an existing file.
* Every change is written to a journal, so ``undo`` puts the last batch back:
  moves are moved back, created files go to the Recycle Bin, overwritten files
  get their old content back.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

WRITABLE_EXTS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".html", ".htm", ".xml", ".yaml", ".yml", ".log"}
MAX_WRITE_BYTES = 1024 * 1024
FOLDER_ALIASES = {"downloads": "Downloads", "documents": "Documents", "desktop": "Desktop",
                  "pictures": "Pictures", "music": "Music", "videos": "Videos"}


def default_save_dir() -> Path:
    """Where 'save it' goes when the operator didn't say: Documents/JARVIS."""
    return Path.home() / "Documents" / "JARVIS"


def protected(path: str) -> Optional[str]:
    from core.agents.system_ops_agent import protected_reason
    return protected_reason(path)


def resolve_dir(raw: str, base: Optional[Path], prov=None) -> Tuple[Optional[Path], Optional[str]]:
    """A destination folder from what the model passed.

    * 'downloads' / 'documents' ...       -> that folder in the profile
    * a relative name ('PDFs', 'Invoices/2026') -> inside ``base`` (the source's folder,
      or Documents/JARVIS), never escaping it
    * an absolute path                       -> only if the operator wrote it (provenance)
    """
    text = str(raw or "").strip().strip('"')
    if not text:
        return (base, None) if base else (None, "Where should it go?")
    alias = FOLDER_ALIASES.get(text.lower().rstrip("/\\"))
    if alias:
        return Path.home() / alias, None
    p = Path(os.path.expanduser(text))
    # "Documents/JARVIS", "downloads/PDFs": a profile folder plus a subfolder, not something relative
    # to the base (seen live: the default Documents/JARVIS ended up nested inside itself).
    if not p.is_absolute() and p.parts and p.parts[0].lower() in FOLDER_ALIASES:
        rest = Path(*p.parts[1:]) if len(p.parts) > 1 else Path()
        root = Path.home() / FOLDER_ALIASES[p.parts[0].lower()]
        target = (root / rest).resolve()
        if not str(target).lower().startswith(str(root.resolve()).lower()):
            return None, "That destination would leave the folder it belongs in."
        return target, None
    if p.is_absolute():
        inside_base = base is not None and _is_inside(p, base)
        if prov is not None and not prov.trusted_mentions(text) and not inside_base \
                and not any(prov.trusted_mentions(str(a)) for a in p.parents if len(a.parts) > 2):
            # Allowed: a path the operator wrote, anything inside a folder they wrote, or
            # anything inside the files' own folder ("a PDFs folder next to them").
            return None, f"The folder {text} didn't come from you; tell me where you want it."
        return p, None
    if base is None:
        base = default_save_dir()
    target = (base / p).resolve()
    if not str(target).lower().startswith(str(base.resolve()).lower()):
        return None, "That destination would leave the folder it belongs in."
    return target, None


def _is_inside(path: Path, folder: Path) -> bool:
    try:
        return str(path.resolve()).lower().startswith(str(folder.resolve()).lower().rstrip("\\/") + os.sep.lower()) \
            or path.resolve() == folder.resolve()
    except OSError:
        return False


# ═══════════════════════════════════════════════════════════════
# Journal (undo)
# ═══════════════════════════════════════════════════════════════
class FileJournal:
    STATE = "file_journal.json"
    _memory: List[dict] = []

    @classmethod
    def batches(cls) -> List[dict]:
        from core.agents.supervisor import agent_state_dir, load_json_state
        return list(load_json_state(cls.STATE, {}).get("batches", [])) if agent_state_dir() is not None else cls._memory

    @classmethod
    def _store(cls, batches: List[dict]) -> None:
        from core.agents.supervisor import agent_state_dir, save_json_state
        if agent_state_dir() is not None:
            save_json_state(cls.STATE, {"batches": batches[-30:]})
        else:
            cls._memory = batches[-30:]

    @classmethod
    def record(cls, ops: List[dict], label: str) -> None:
        if not ops:
            return
        batches = cls.batches()
        batches.append({"id": uuid.uuid4().hex[:8], "time": time.time(), "label": label, "ops": ops})
        cls._store(batches)

    @staticmethod
    def backup_dir() -> Path:
        from core.agents.supervisor import agent_state_dir
        root = agent_state_dir()
        d = (root / "file_backups") if root is not None else Path(os.environ.get("TEMP", "/tmp")) / "jarvis_file_backups"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @classmethod
    def undo(cls) -> Tuple[List[str], List[str]]:
        """Reverse the newest batch. Returns (done, problems)."""
        batches = cls.batches()
        if not batches:
            return [], ["there's no file change of mine to undo"]
        batch = batches.pop()
        done, problems = [], []
        for op in reversed(batch["ops"]):
            kind, src, dst = op["op"], op.get("src"), op.get("dst")
            name = os.path.basename(dst or src or "")
            try:
                if kind == "move":
                    if os.path.lexists(src):
                        problems.append(f"{os.path.basename(src)} (something is back at its old place)")
                    elif not os.path.lexists(dst):
                        problems.append(f"{name} (it's no longer where I put it)")
                    else:
                        os.makedirs(os.path.dirname(src), exist_ok=True)
                        shutil.move(dst, src)
                        done.append(f"moved {name} back")
                elif kind in ("copy", "create"):
                    if os.path.lexists(dst):
                        from core.agents.system_ops_agent import Trash
                        res = Trash.send([dst])
                        (done if res.status == "done" else problems).append(
                            f"removed {name}" if res.status == "done" else f"{name} ({res.summary})")
                elif kind == "overwrite":
                    backup = op.get("backup")
                    if backup and os.path.exists(backup):
                        shutil.copy2(backup, dst)
                        done.append(f"restored the previous {name}")
                    else:
                        problems.append(f"{name} (the backup is gone)")
                elif kind == "mkdir":
                    if os.path.isdir(dst) and not os.listdir(dst):
                        os.rmdir(dst)
            except Exception as exc:
                problems.append(f"{name} ({type(exc).__name__})")
        cls._store(batches)
        return done, problems


# ═══════════════════════════════════════════════════════════════
# Operations (blocking; run them in a thread)
# ═══════════════════════════════════════════════════════════════
def write_text(path: Path, content: str, *, overwrite: bool = False, append: bool = False) -> Tuple[Optional[dict], Optional[str]]:
    """Write one document. Returns (journal op, problem)."""
    if path.suffix.lower() not in WRITABLE_EXTS:
        return None, (f"I only write documents ({', '.join(sorted(e for e in WRITABLE_EXTS if len(e) <= 5))}); "
                      f"'{path.suffix or 'no extension'}' isn't one.")
    reason = protected(str(path))
    if reason:
        return None, f"I won't write to {path}: {reason}."
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        return None, "That's more than 1 MB of text; I won't write a file that large."
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_dir():
            return None, f"{path} is a folder."
        if not (overwrite or append):
            return None, f"{path.name} already exists; say whether to overwrite it or add to it."
        backup = FileJournal.backup_dir() / f"{uuid.uuid4().hex[:8]}_{path.name}"
        shutil.copy2(path, backup)
        with open(path, "a" if append else "w", encoding="utf-8", newline="") as f:
            f.write(("\n" if append and path.stat().st_size else "") + content if append else content)
        return {"op": "overwrite", "dst": str(path), "backup": str(backup)}, None
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    return {"op": "create", "dst": str(path)}, None


def move_or_copy(sources: List[str], dest_dir: Path, *, copy: bool = False,
                 new_name: Optional[str] = None) -> Tuple[List[dict], List[str], List[str]]:
    """Move (or copy) files/folders into ``dest_dir``. Never overwrites.
    Returns (journal ops, done names, problems)."""
    ops: List[dict] = []
    done, problems = [], []
    # Check what would be created INSIDE the folder: Documents itself is protected from being
    # moved or deleted, but putting files into it is fine. Trees (system folders, JARVIS) still refuse.
    reason = protected(str(dest_dir / "_"))
    if reason:
        return [], [], [f"{dest_dir} ({reason})"]
    if not dest_dir.exists():
        dest_dir.mkdir(parents=True)
        ops.append({"op": "mkdir", "dst": str(dest_dir)})
    elif not dest_dir.is_dir():
        return [], [], [f"{dest_dir} is a file, not a folder"]
    for src in sources:
        name = new_name if (new_name and len(sources) == 1) else os.path.basename(src.rstrip("\\/"))
        if new_name and os.sep in new_name or (new_name and "/" in new_name):
            problems.append(f"{new_name} (a new name can't contain a folder)")
            continue
        dst = dest_dir / name
        why = protected(src) or protected(str(dst))
        if why:
            problems.append(f"{os.path.basename(src)} ({why})")
            continue
        if not os.path.lexists(src):
            problems.append(f"{os.path.basename(src)} (not found)")
            continue
        if os.path.lexists(dst):
            problems.append(f"{name} (already exists in {dest_dir.name})")
            continue
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
            continue
        try:
            if copy:
                (shutil.copytree if os.path.isdir(src) else shutil.copy2)(src, dst)
                ops.append({"op": "copy", "src": src, "dst": str(dst)})
            else:
                shutil.move(src, dst)
                ops.append({"op": "move", "src": src, "dst": str(dst)})
            done.append(name)
        except Exception as exc:
            problems.append(f"{os.path.basename(src)} ({type(exc).__name__})")
    return ops, done, problems


def find_passages(text: str, query: str, max_hits: int = 5, width: int = 600) -> List[Dict[str, Any]]:
    """The best passages of a long text for a query, best first, with how far in they are."""
    import re
    words = [w for w in re.findall(r"[a-z0-9]{3,}", (query or "").lower())
             if w not in {"the", "and", "what", "does", "say", "about", "this", "that", "with", "for", "from"}]
    phrase = (query or "").strip().lower()
    if not text or not (words or phrase):
        return []
    low = text.lower()
    hits: List[Tuple[float, int]] = []
    step = width // 2
    for start in range(0, max(1, len(text) - step), step):
        window = low[start:start + width]
        score = sum(window.count(w) for w in words) + (5 if phrase and phrase in window else 0)
        if score:
            hits.append((score, start))
    hits.sort(key=lambda h: (-h[0], h[1]))
    picked: List[int] = []
    for _, start in hits:
        if all(abs(start - p) >= width for p in picked):
            picked.append(start)
        if len(picked) >= max_hits:
            break
    out = []
    for start in picked:
        snippet = re.sub(r"\s+", " ", text[start:start + width]).strip()
        out.append({"position": f"{round(100 * start / max(1, len(text)))}% in", "text": snippet})
    return out
