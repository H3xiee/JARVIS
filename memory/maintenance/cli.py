"""Memory maintenance from the command line.

    python -m memory.maintenance.cli stats
    python -m memory.maintenance.cli check [--repair]
    python -m memory.maintenance.cli backup
    python -m memory.maintenance.cli restore <backup.db>        (JARVIS must be closed)
    python -m memory.maintenance.cli export <file.json>
    python -m memory.maintenance.cli import <file.json>
    python -m memory.maintenance.cli search "<query>"
    python -m memory.maintenance.cli inspect <memory_id>
    python -m memory.maintenance.cli forget --about "<person or thing>" [--backups] [--yes]
    python -m memory.maintenance.cli cleanup | maintain | consolidate | reindex
    python -m memory.maintenance.cli benchmark

Works on data/memory/jarvis_memory.db (or JARVIS_MEMORY_DB). Never prints
anything marked private unless --private is given.
"""

from __future__ import annotations

import argparse
import json
import sys


def _mm():
    from memory.core.memory_manager import get_memory
    from memory.maintenance.memory_migration import ensure_migrated
    mm = get_memory()
    ensure_migrated(mm)
    return mm


def _show(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="memory", description="JARVIS memory maintenance")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    c = sub.add_parser("check")
    c.add_argument("--repair", action="store_true")
    sub.add_parser("backup")
    r = sub.add_parser("restore")
    r.add_argument("backup")
    e = sub.add_parser("export")
    e.add_argument("file")
    i = sub.add_parser("import")
    i.add_argument("file")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--private", action="store_true")
    n = sub.add_parser("inspect")
    n.add_argument("memory_id")
    f = sub.add_parser("forget")
    f.add_argument("--about", required=True)
    f.add_argument("--backups", action="store_true", help="also replace old backups (they hold the old data)")
    f.add_argument("--yes", action="store_true")
    for name in ("cleanup", "maintain", "consolidate", "reindex", "benchmark"):
        sub.add_parser(name)
    args = ap.parse_args(argv)

    if args.cmd == "benchmark":
        from memory.benchmarks.run import main as bench
        return bench([])
    if args.cmd == "restore":
        from memory.core import memory_config
        from memory.maintenance.memory_backup import restore
        print(f"restored into {restore(args.backup, memory_config.db_path())}")
        return 0

    mm = _mm()
    if args.cmd == "stats":
        _show({**mm.stats(), "path": str(mm.repo.path), "maintenance": mm.repo.get_meta("maintenance_at"),
               "recovered": mm.repo.get_meta("recovered")})
    elif args.cmd == "check":
        from memory.maintenance import memory_integrity
        _show(memory_integrity.repair(mm) if args.repair else memory_integrity.check(mm))
    elif args.cmd == "backup":
        from memory.maintenance.memory_backup import backup
        print(backup(mm, label="manual"))
    elif args.cmd == "export":
        print(mm.export_to(args.file))
    elif args.cmd == "import":
        _show(mm.import_from(args.file))
    elif args.cmd == "search":
        for rec in mm.retriever.recall(args.query, k=10):
            if "private" in rec.tags and not args.private:
                continue
            print(f"{rec.memory_id}  [{rec.type}/{rec.category}; {rec.source.type}; {rec.verification_status}]  "
                  f"{rec.content}")
    elif args.cmd == "inspect":
        info = mm.inspect(args.memory_id)
        if info is None:
            print("no such memory")
            return 1
        _show(info)
    elif args.cmd == "forget":
        from memory.maintenance.memory_cleanup import forget_everywhere, preview_forget
        ent = mm.retriever.resolver.resolve(args.about)
        target = {"entity": ent["name"]} if ent else {"text": args.about}
        found = preview_forget(mm, **target)
        print(f"{found['memory_count']} memories and {found['messages']} conversation lines mention {args.about}.")
        if not args.yes and input("Forget them permanently? [y/N] ").strip().lower() != "y":
            print("nothing deleted")
            return 1
        _show(forget_everywhere(mm, purge_backups=args.backups, **target))
    elif args.cmd == "cleanup":
        from memory.maintenance.memory_cleanup import retention
        _show(retention(mm))
    elif args.cmd == "maintain":
        from memory.maintenance.memory_recovery import run_maintenance
        _show(run_maintenance(mm, force=True))
    elif args.cmd == "consolidate":
        _show(mm.consolidate())
    elif args.cmd == "reindex":
        print(mm.retriever.vectors.reindex())
    return 0


if __name__ == "__main__":
    sys.exit(main())
