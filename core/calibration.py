# core/calibration.py
"""
Measurement pipeline for false-positive rates of username OSINT enumeration.
Runs sequential checks on random, nonexistent usernames to determine which
platforms fool verify_hit() into marking them verified or uncertain.
"""
import sys
import os
import re
import json
import string
import random
import shutil
import logging
import tempfile
import argparse
import asyncio
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager

from core.target_model import Target, Entity
from core.tool_locator import find_tool
from core.doctor import check_tool_status
from modules.verify import get_site_family
import modules.social_enum as social_enum

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
DEFAULT_CALIBRATION_DIR = Path.home() / ".jarvis" / "calibration"


def generate_random_username() -> str:
    """
    Generate a 14-16 character random username consisting of lowercase letters
    and digits, starting with a letter.
    """
    length = random.randint(14, 16)
    first_char = random.choice(string.ascii_lowercase)
    rest = "".join(random.choices(string.ascii_lowercase + string.digits, k=length - 1))
    return first_char + rest


def generate_calibration_usernames(count: int = 3) -> list:
    """
    Generate unique random usernames. Clamps count between 1 and 10.
    """
    clamped_count = min(10, max(1, count))
    names = set()
    while len(names) < clamped_count:
        names.add(generate_random_username())
    return list(names)


def check_tools_available() -> tuple[bool, str]:
    """Check if sherlock or maigret is available."""
    sherlock_bin = find_tool("sherlock")
    maigret_bin = find_tool("maigret")
    if not sherlock_bin and not maigret_bin:
        msg = (
            "Neither 'sherlock' nor 'maigret' was found in PATH or ~/.local/bin.\n"
            "Live calibration requires at least one OSINT tool installed.\n"
            "Run 'python3 jarvis.py doctor' for system tool status."
        )
        return False, msg
    return True, ""


@asynccontextmanager
async def disable_evidence_capture():
    """
    Context manager that disables screenshot and evidence file creation during calibration.
    Browser-based DOM checks continue normally, but no image files are written to disk.
    """
    import modules.evidence as evidence
    import modules.headless_probe as headless_probe

    orig_capture = getattr(evidence, "capture", None)
    orig_probe = getattr(headless_probe, "probe", None)

    async def no_op_capture(*args, **kwargs):
        return {"success": False, "screenshot_path": None, "screenshot_url": None}

    async def no_screenshot_probe(url, platform, screenshot_path=None, **kwargs):
        if orig_probe:
            return await orig_probe(url, platform, screenshot_path=None, **kwargs)
        return {"exists": False}

    evidence.capture = no_op_capture
    if orig_probe:
        headless_probe.probe = no_screenshot_probe

    try:
        yield
    finally:
        if orig_capture:
            evidence.capture = orig_capture
        if orig_probe:
            headless_probe.probe = orig_probe


def validate_names(names_str: str) -> list[str]:
    """
    Validate and parse a comma-separated list of test usernames.
    Enforces:
    - 1 to 10 usernames
    - Each username must be alphanumeric (letters and digits)
    - Each username must be between 8 and 32 characters long
    """
    if not names_str or not names_str.strip():
        raise ValueError("No usernames provided in --names.")
    raw_names = [n.strip() for n in names_str.split(",") if n.strip()]
    if not raw_names:
        raise ValueError("No usernames provided in --names.")
    if len(raw_names) > 10:
        raise ValueError(f"Too many usernames provided ({len(raw_names)}). Maximum allowed is 10.")

    validated = []
    for name in raw_names:
        if not name.isalnum():
            raise ValueError(f"Invalid username '{name}': must be alphanumeric (letters and digits only).")
        if not (8 <= len(name) <= 32):
            raise ValueError(f"Invalid username '{name}': length must be between 8 and 32 characters (got {len(name)}).")
        validated.append(name)
    return validated


async def run_single_target(
    username: str,
    temp_base_dir: Path | None = None,
    canary_context: dict | None = None,
) -> dict:
    """
    Run a single username through the standard social_enum pipeline.
    Always uses lessons_store=None to measure raw verification performance.
    Cleans up any transient case files so nothing remains in the project cases/ directory.
    Guarantees cases/octocat is never deleted or altered.
    """
    target = Target(primary=username, target_type="username")
    case_slug = target.case_slug

    async with disable_evidence_capture():
        try:
            await social_enum.run(
                target,
                username,
                on_find=None,
                lessons_store=None,
                canary_context=canary_context,
            )
        except TypeError:
            await social_enum.run(
                target,
                username,
                on_find=None,
                lessons_store=None,
            )

    # Clean up transient calibration case directory if created, NEVER delete cases/octocat
    if case_slug and case_slug != "octocat":
        project_case_dir = PROJECT_ROOT / "cases" / case_slug
        if project_case_dir.exists():
            shutil.rmtree(project_case_dir, ignore_errors=True)

    all_tested = target.entities + target.rejected_entities
    sherlock_raw = sum(1 for e in all_tested if "sherlock" in e.sources)
    maigret_raw = sum(1 for e in all_tested if "maigret" in e.sources)
    total_raw = len(all_tested)

    verified_entities = [e for e in target.entities if e.metadata.get("verified") is True]
    uncertain_entities = [
        e for e in target.entities
        if e.metadata.get("verified") is not True and e.metadata.get("verified") is not False
    ]
    rejected_entities = list(target.rejected_entities)

    verified_hits = []
    for e in verified_entities:
        verified_hits.append({
            "platform": e.platform or "Unknown",
            "url": e.metadata.get("url", ""),
            "status": "verified",
            "verification_method": e.metadata.get("verification_method", "unknown"),
            "verification_reason": e.metadata.get("verification_reason", "unknown"),
            "canary_checked": e.metadata.get("canary_checked", False),
            "canary_status": e.metadata.get("canary_status", "none"),
        })

    uncertain_hits = []
    for e in uncertain_entities:
        uncertain_hits.append({
            "platform": e.platform or "Unknown",
            "url": e.metadata.get("url", ""),
            "status": "uncertain",
            "verification_method": e.metadata.get("verification_method", "unknown"),
            "verification_reason": e.metadata.get("verification_reason", "unknown"),
            "canary_checked": e.metadata.get("canary_checked", False),
            "canary_status": e.metadata.get("canary_status", "none"),
        })

    rejected_hits = []
    for e in rejected_entities:
        rejected_hits.append({
            "platform": e.platform or "Unknown",
            "url": e.metadata.get("url", ""),
            "status": "rejected",
            "verification_method": e.metadata.get("verification_method", "unknown"),
            "verification_reason": e.metadata.get("verification_reason", "unknown"),
            "canary_checked": e.metadata.get("canary_checked", False),
            "canary_status": e.metadata.get("canary_status", "none"),
        })

    return {
        "username": username,
        "raw_hits": total_raw,
        "sherlock_hits": sherlock_raw,
        "maigret_hits": maigret_raw,
        "verified_count": len(verified_entities),
        "uncertain_count": len(uncertain_entities),
        "rejected_count": len(rejected_entities),
        "verified_hits": verified_hits,
        "uncertain_hits": uncertain_hits,
        "rejected_hits": rejected_hits,
    }


def aggregate_results(
    random_results: list[dict],
    control_result: dict | None,
    pw_available: bool,
    sherlock_status: dict,
    maigret_status: dict,
    apply_lessons: bool = False,
    lessons_store=None,
    is_partial: bool = False,
    canary_context: dict | None = None,
) -> dict:
    """
    Aggregate per-target findings into overall metrics, tables of fooled and uncertain
    platforms, and control comparisons.
    """
    total_raw = sum(r["raw_hits"] for r in random_results)
    total_sherlock = sum(r["sherlock_hits"] for r in random_results)
    total_maigret = sum(r["maigret_hits"] for r in random_results)
    total_verified = sum(r["verified_count"] for r in random_results)
    total_uncertain = sum(r["uncertain_count"] for r in random_results)
    total_rejected = sum(r["rejected_count"] for r in random_results)

    verified_fp_rate = (total_verified / total_raw) if total_raw > 0 else 0.0
    uncertain_fp_rate = (total_uncertain / total_raw) if total_raw > 0 else 0.0

    # Fooled platforms (platforms claiming verified for random nonexistent usernames)
    fooled_map = {}
    for r in random_results:
        uname = r["username"]
        for h in r["verified_hits"]:
            plat = h["platform"]
            if plat not in fooled_map:
                fooled_map[plat] = {
                    "platform": plat,
                    "names": set(),
                    "reasons": set(),
                    "methods": set(),
                    "urls": [],
                }
            fooled_map[plat]["names"].add(uname)
            if h.get("verification_reason"):
                fooled_map[plat]["reasons"].add(h["verification_reason"])
            if h.get("verification_method"):
                fooled_map[plat]["methods"].add(h["verification_method"])
            if h.get("url"):
                fooled_map[plat]["urls"].append(h["url"])

    fooled_platforms = []
    for plat, data in sorted(fooled_map.items(), key=lambda x: len(x[1]["names"]), reverse=True):
        fam = get_site_family(plat)
        fooled_platforms.append({
            "platform": plat,
            "family": fam,
            "fooled_count": len(data["names"]),
            "reasons": sorted(list(data["reasons"])),
            "methods": sorted(list(data["methods"])),
            "urls": sorted(list(set(data["urls"]))),
        })

    unique_fooled_families = len(set(fp.get("family", fp["platform"]) for fp in fooled_platforms))

    # Compact verdicts maps
    verdicts_map = {}
    for r in random_results:
        for h in r.get("rejected_hits", []):
            verdicts_map[h.get("platform", "Unknown")] = "rejected"
        for h in r.get("uncertain_hits", []):
            verdicts_map[h.get("platform", "Unknown")] = "uncertain"
        for h in r.get("verified_hits", []):
            verdicts_map[h.get("platform", "Unknown")] = "verified"

    control_verdicts_map = {}
    if control_result:
        for h in control_result.get("rejected_hits", []):
            control_verdicts_map[h.get("platform", "Unknown")] = "rejected"
        for h in control_result.get("uncertain_hits", []):
            control_verdicts_map[h.get("platform", "Unknown")] = "uncertain"
        for h in control_result.get("verified_hits", []):
            control_verdicts_map[h.get("platform", "Unknown")] = "verified"

    # Uncertain platforms
    uncertain_map = {}
    for r in random_results:
        uname = r["username"]
        for h in r["uncertain_hits"]:
            plat = h["platform"]
            if plat not in uncertain_map:
                uncertain_map[plat] = {
                    "platform": plat,
                    "names": set(),
                    "reasons": set(),
                    "methods": set(),
                    "urls": [],
                }
            uncertain_map[plat]["names"].add(uname)
            if h.get("verification_reason"):
                uncertain_map[plat]["reasons"].add(h["verification_reason"])
            if h.get("verification_method"):
                uncertain_map[plat]["methods"].add(h["verification_method"])
            if h.get("url"):
                uncertain_map[plat]["urls"].append(h["url"])

    uncertain_platforms = []
    for plat, data in sorted(uncertain_map.items(), key=lambda x: len(x[1]["names"]), reverse=True):
        uncertain_platforms.append({
            "platform": plat,
            "uncertain_count": len(data["names"]),
            "reasons": sorted(list(data["reasons"])),
            "methods": sorted(list(data["methods"])),
            "urls": sorted(list(set(data["urls"]))),
        })

    # Verification methods used
    methods_used = set()
    for r in random_results:
        for h in r["verified_hits"] + r["uncertain_hits"]:
            if h.get("verification_method"):
                methods_used.add(h["verification_method"])
    if control_result:
        for h in control_result["verified_hits"] + control_result["uncertain_hits"]:
            if h.get("verification_method"):
                methods_used.add(h["verification_method"])

    # Apply lessons if requested
    lessons_applied = []
    if apply_lessons and fooled_platforms:
        if lessons_store is None:
            from memory.lessons_store import LessonsStore
            lessons_store = LessonsStore()
        for fp in fooled_platforms:
            plat = fp["platform"]
            trigger = f"{plat} username profile claimed to exist but was a false positive"
            lesson = (
                f"{plat} gives false positives on nonexistent usernames — "
                f"lower confidence for future hits on this platform"
            )
            try:
                lessons_store.add_lesson(
                    trigger=trigger,
                    lesson=lesson,
                    platform=plat,
                    context="false_positive",
                )
                lessons_applied.append(plat)
            except Exception as exc:
                logger.warning(f"Failed to add lesson for {plat}: {exc}")

    now = datetime.now()
    raw_hits_per_name = [
        {
            "username": r["username"],
            "sherlock": r["sherlock_hits"],
            "maigret": r["maigret_hits"],
            "total_raw": r["raw_hits"],
        }
        for r in random_results
    ]

    extra_time = round(canary_context.get("extra_time_seconds", 0.0), 2) if canary_context else 0.0
    avg_time_per_hit = round(extra_time / total_raw, 3) if total_raw > 0 else 0.0

    return {
        "timestamp": now.isoformat(),
        "timestamp_formatted": now.strftime("%Y-%m-%d %H:%M:%S"),
        "is_partial": is_partial,
        "environment": {
            "python_version": sys.version.split()[0],
            "sherlock_version": sherlock_status.get("version", "unknown"),
            "sherlock_path": sherlock_status.get("path", "not found"),
            "maigret_version": maigret_status.get("version", "unknown"),
            "maigret_path": maigret_status.get("path", "not found"),
            "playwright_worker_available": pw_available,
        },
        "variance_notice": (
            "Results can vary from day to day because of site changes, "
            "platform redesigns, and rate limiting."
        ),
        "summary": {
            "random_targets_count": len(random_results),
            "random_raw_hits": total_raw,
            "random_sherlock_hits": total_sherlock,
            "random_maigret_hits": total_maigret,
            "random_verified": total_verified,
            "random_uncertain": total_uncertain,
            "random_rejected": total_rejected,
            "verified_false_positive_rate": round(verified_fp_rate, 4),
            "uncertain_false_positive_rate": round(uncertain_fp_rate, 4),
            "unique_fooled_families_count": unique_fooled_families,
            "canary_requests_count": canary_context.get("canary_requests_count", 0) if canary_context else 0,
            "browser_rechecks_count": canary_context.get("browser_rechecks_count", 0) if canary_context else 0,
            "browser_recheck_platforms": sorted(list(canary_context.get("browser_recheck_platforms", set()))) if canary_context else [],
            "extra_time_seconds": extra_time,
            "avg_time_per_hit_seconds": avg_time_per_hit,
            "verification_methods_used": sorted(list(methods_used)),
            "raw_hits_per_name": raw_hits_per_name,
        },
        "control_target": control_result,
        "fooled_platforms": fooled_platforms,
        "uncertain_platforms": uncertain_platforms,
        "verdicts": verdicts_map,
        "control_verdicts": control_verdicts_map,
        "lessons": {
            "apply_lessons": apply_lessons,
            "lessons_applied": lessons_applied,
        },
        "details": {r["username"]: r for r in random_results},
    }


def compare_calibrations(before_data: dict, after_data: dict) -> dict:
    """
    Compare a baseline calibration run with a subsequent run.
    Analyzes:
    - Summary metrics before vs after (raw hits, verified FP count & rate, uncertain, rejected, canary metrics)
    - Hits that transitioned from verified to uncertain or rejected (common platforms only)
    - Platforms present in only one run (separated, never shown as unknown -> verified)
    - Platforms where browser re-check was triggered
    - Platforms for the control user 'octocat' that flipped verdict (common platforms only, flagged if verified -> uncertain/rejected)
    """
    b_sum = before_data.get("summary", {})
    a_sum = after_data.get("summary", {})

    b_raw = b_sum.get("random_raw_hits", 0)
    a_raw = a_sum.get("random_raw_hits", 0)

    b_ver = b_sum.get("random_verified", 0)
    a_ver = a_sum.get("random_verified", 0)

    b_unc = b_sum.get("random_uncertain", 0)
    a_unc = a_sum.get("random_uncertain", 0)

    b_rej = b_sum.get("random_rejected", 0)
    a_rej = a_sum.get("random_rejected", 0)

    b_fp_rate = b_sum.get("verified_false_positive_rate", 0.0)
    a_fp_rate = a_sum.get("verified_false_positive_rate", 0.0)

    b_canary_req = b_sum.get("canary_requests_count", 0)
    a_canary_req = a_sum.get("canary_requests_count", 0)

    b_rechecks = b_sum.get("browser_rechecks_count", 0)
    a_rechecks = a_sum.get("browser_rechecks_count", 0)

    b_extra_time = b_sum.get("extra_time_seconds", 0.0)
    a_extra_time = a_sum.get("extra_time_seconds", 0.0)

    b_avg_time = b_sum.get("avg_time_per_hit_seconds", 0.0)
    a_avg_time = a_sum.get("avg_time_per_hit_seconds", 0.0)

    # Extract platform mappings for random targets
    def extract_random_platforms(data: dict) -> dict[str, dict]:
        res = {}
        details = data.get("details", {})
        for uname, t_data in details.items():
            for h in t_data.get("verified_hits", []):
                p = h.get("platform")
                if p:
                    res[p.lower()] = {**h, "platform": p, "status": "verified", "username": uname}
            for h in t_data.get("uncertain_hits", []):
                p = h.get("platform")
                if p and p.lower() not in res:
                    res[p.lower()] = {**h, "platform": p, "status": "uncertain", "username": uname}
            for h in t_data.get("rejected_hits", []):
                p = h.get("platform")
                if p and p.lower() not in res:
                    res[p.lower()] = {**h, "platform": p, "status": "rejected", "username": uname}
        # Also include any platforms from compact verdicts map that weren't in details
        verdicts = data.get("verdicts", {})
        for p, st in verdicts.items():
            if p.lower() not in res:
                res[p.lower()] = {"platform": p, "status": st, "verification_reason": "", "url": "", "username": "various"}
        return res

    b_plat_map = extract_random_platforms(before_data)
    a_plat_map = extract_random_platforms(after_data)

    changed_to_rejected = []
    changed_to_uncertain = []
    common_random_plats = set(b_plat_map.keys()) & set(a_plat_map.keys())

    for k in sorted(common_random_plats):
        b_hit = b_plat_map[k]
        a_hit = a_plat_map[k]
        if b_hit.get("status") == "verified":
            if a_hit.get("status") == "rejected":
                changed_to_rejected.append({
                    "username": a_hit.get("username", b_hit.get("username", "")),
                    "platform": a_hit.get("platform", b_hit.get("platform", "")),
                    "url": a_hit.get("url", b_hit.get("url", "")),
                    "before_reason": b_hit.get("verification_reason", ""),
                    "after_reason": a_hit.get("verification_reason", ""),
                })
            elif a_hit.get("status") == "uncertain":
                changed_to_uncertain.append({
                    "username": a_hit.get("username", b_hit.get("username", "")),
                    "platform": a_hit.get("platform", b_hit.get("platform", "")),
                    "url": a_hit.get("url", b_hit.get("url", "")),
                    "before_reason": b_hit.get("verification_reason", ""),
                    "after_reason": a_hit.get("verification_reason", ""),
                })

    only_in_after = [
        {"platform": a_plat_map[k].get("platform", k), "status": a_plat_map[k].get("status", "unknown")}
        for k in sorted(set(a_plat_map.keys()) - set(b_plat_map.keys()))
    ]
    only_in_before = [
        {"platform": b_plat_map[k].get("platform", k), "status": b_plat_map[k].get("status", "unknown")}
        for k in sorted(set(b_plat_map.keys()) - set(a_plat_map.keys()))
    ]

    browser_recheck_platforms = a_sum.get("browser_recheck_platforms", [])

    # Control target comparison (common platforms only)
    def extract_ctrl_platforms(data: dict) -> dict[str, dict]:
        ctrl = data.get("control_target") or {}
        res = {}
        for h in ctrl.get("verified_hits", []):
            p = h.get("platform")
            if p:
                res[p.lower()] = {**h, "platform": p, "status": "verified"}
        for h in ctrl.get("uncertain_hits", []):
            p = h.get("platform")
            if p and p.lower() not in res:
                res[p.lower()] = {**h, "platform": p, "status": "uncertain"}
        for h in ctrl.get("rejected_hits", []):
            p = h.get("platform")
            if p and p.lower() not in res:
                res[p.lower()] = {**h, "platform": p, "status": "rejected"}
        ctrl_verdicts = data.get("control_verdicts", {})
        for p, st in ctrl_verdicts.items():
            if p.lower() not in res:
                res[p.lower()] = {"platform": p, "status": st}
        return res

    b_ctrl_map = extract_ctrl_platforms(before_data)
    a_ctrl_map = extract_ctrl_platforms(after_data)

    flipped_octocat = []
    ctrl_only_in_after = []
    ctrl_only_in_before = []

    if b_ctrl_map and a_ctrl_map:
        common_ctrl = set(b_ctrl_map.keys()) & set(a_ctrl_map.keys())
        for k in sorted(common_ctrl):
            b_st = b_ctrl_map[k].get("status", "unknown")
            a_st = a_ctrl_map[k].get("status", "unknown")
            if b_st != a_st:
                tag = "[check by hand]" if (b_st == "verified" and a_st in ("uncertain", "rejected")) else ""
                flipped_octocat.append({
                    "platform": a_ctrl_map[k].get("platform", k),
                    "before_status": b_st,
                    "after_status": a_st,
                    "tag": tag,
                })
        ctrl_only_in_after = [
            {"platform": a_ctrl_map[k].get("platform", k), "status": a_ctrl_map[k].get("status", "unknown")}
            for k in sorted(set(a_ctrl_map.keys()) - set(b_ctrl_map.keys()))
        ]
        ctrl_only_in_before = [
            {"platform": b_ctrl_map[k].get("platform", k), "status": b_ctrl_map[k].get("status", "unknown")}
            for k in sorted(set(b_ctrl_map.keys()) - set(a_ctrl_map.keys()))
        ]

    return {
        "metrics": {
            "before_raw": b_raw,
            "after_raw": a_raw,
            "before_verified": b_ver,
            "after_verified": a_ver,
            "before_uncertain": b_unc,
            "after_uncertain": a_unc,
            "before_rejected": b_rej,
            "after_rejected": a_rej,
            "before_fp_rate": b_fp_rate,
            "after_fp_rate": a_fp_rate,
            "fp_rate_change": a_fp_rate - b_fp_rate,
            "before_canary_requests": b_canary_req,
            "after_canary_requests": a_canary_req,
            "before_browser_rechecks": b_rechecks,
            "after_browser_rechecks": a_rechecks,
            "before_extra_time": b_extra_time,
            "after_extra_time": a_extra_time,
            "before_avg_time_per_hit": b_avg_time,
            "after_avg_time_per_hit": a_avg_time,
        },
        "changed_to_rejected": changed_to_rejected,
        "changed_to_uncertain": changed_to_uncertain,
        "only_in_after": only_in_after,
        "only_in_before": only_in_before,
        "browser_recheck_platforms": browser_recheck_platforms,
        "flipped_octocat": flipped_octocat,
        "ctrl_only_in_after": ctrl_only_in_after,
        "ctrl_only_in_before": ctrl_only_in_before,
    }


def format_comparison_table(comp: dict) -> str:
    """Format an ASCII comparison report."""
    m = comp["metrics"]
    lines = [
        "=== Calibration Comparison (Before vs After) ===",
        f"Raw Candidate Hits:   {m['before_raw']:<5} -> {m['after_raw']:<5}",
        f"Verified Hits (FP):   {m['before_verified']:<5} -> {m['after_verified']:<5} ({m['before_fp_rate']*100:.1f}% -> {m['after_fp_rate']*100:.1f}%)",
        f"Uncertain Hits:       {m['before_uncertain']:<5} -> {m['after_uncertain']:<5}",
        f"Rejected Hits:        {m['before_rejected']:<5} -> {m['after_rejected']:<5}",
        f"FP Rate Change:       {m['fp_rate_change']*100:+.1f}%",
        f"Canary Requests:      {m.get('before_canary_requests', 0):<5} -> {m.get('after_canary_requests', 0):<5}",
        f"Browser Re-checks:    {m.get('before_browser_rechecks', 0):<5} -> {m.get('after_browser_rechecks', 0):<5}",
        f"Extra Canary Time:    {m.get('before_extra_time', 0.0):<5.1f}s -> {m.get('after_extra_time', 0.0):<5.1f}s",
        "-" * 50,
    ]

    c_rej = comp.get("changed_to_rejected", [])
    if c_rej:
        lines.append(f"\n[Hits Changed from Verified to Rejected (Soft 404)] ({len(c_rej)}):")
        for item in c_rej:
            lines.append(f"  - {item['platform']:<18} ({item['username']}): {item.get('after_reason', '')}")
    else:
        lines.append("\n[Hits Changed to Rejected]: None")

    c_unc = comp.get("changed_to_uncertain", [])
    if c_unc:
        lines.append(f"\n[Hits Changed from Verified to Uncertain] ({len(c_unc)}):")
        for item in c_unc:
            lines.append(f"  - {item['platform']:<18} ({item['username']}): {item.get('after_reason', '')}")
    else:
        lines.append("\n[Hits Changed to Uncertain]: None")

    only_after = comp.get("only_in_after", [])
    if only_after:
        lines.append(f"\n[Platforms in Current Run (Not Present in Earlier Report)] ({len(only_after)}):")
        for item in only_after:
            lines.append(f"  + {item['platform']:<18} ({item['status']})")

    recheck_plats = comp.get("browser_recheck_platforms", [])
    if recheck_plats:
        lines.append(f"\n[Browser Re-checks Triggered]: {', '.join(recheck_plats)}")
    else:
        lines.append("\n[Browser Re-checks Triggered]: None")

    flipped = comp.get("flipped_octocat", [])
    if flipped:
        lines.append(f"\n[Control Target ('octocat') Verdict Flips] ({len(flipped)}):")
        for f in flipped:
            tag_str = f" {f['tag']}" if f.get("tag") else ""
            lines.append(f"  ! {f['platform']}: {f['before_status']} -> {f['after_status']}{tag_str}")
    else:
        lines.append("\n[Control Target ('octocat') Verdict Flips]: None (0 true positives lost)")

    ctrl_after = comp.get("ctrl_only_in_after", [])
    if ctrl_after:
        lines.append(f"\n[Control Platforms in Current Run (Not Present in Earlier Report)] ({len(ctrl_after)}):")
        for item in ctrl_after:
            lines.append(f"  + {item['platform']:<18} ({item['status']})")

    return "\n".join(lines)


def generate_markdown_report(data: dict) -> str:
    """Generate a clean, readable Markdown report from aggregated calibration data."""
    summary = data["summary"]
    env = data["environment"]
    control = data.get("control_target")
    is_partial = data.get("is_partial", False)

    title = "# J.A.R.V.I.S. False-Positive Calibration Report"
    if is_partial:
        title += " [PARTIAL REPORT - INTERRUPTED]"

    pw_note = (
        "Yes" if env["playwright_worker_available"]
        else "No (JS-heavy platforms checked by plain HTTP only)"
    )

    md = [
        title,
        "",
        f"- **Date & Time:** {data['timestamp_formatted']}",
        f"- **Python Version:** {env['python_version']}",
        f"- **Sherlock Version:** {env['sherlock_version']} (`{env['sherlock_path']}`)",
        f"- **Maigret Version:** {env['maigret_version']} (`{env['maigret_path']}`)",
        f"- **Playwright Worker Available:** {pw_note}",
        f"- **Notice:** {data['variance_notice']}",
        "",
        "---",
        "",
        "## 1. Summary Metrics (Random Nonexistent Usernames)",
        "",
        "| Metric | Count | Percentage |",
        "| :--- | :--- | :--- |",
        f"| Random Usernames Tested | {summary['random_targets_count']} | — |",
        f"| Raw Candidate Hits | {summary['random_raw_hits']} | 100.0% |",
        f"| Sherlock Raw Hits | {summary['random_sherlock_hits']} | — |",
        f"| Maigret Raw Hits | {summary['random_maigret_hits']} | — |",
        f"| **Verified Hits (False Positives)** | **{summary['random_verified']}** ({summary.get('unique_fooled_families_count', 0)} site families) | **{summary['verified_false_positive_rate'] * 100:.1f}%** |",
        f"| Uncertain Hits | {summary['random_uncertain']} | {summary['uncertain_false_positive_rate'] * 100:.1f}% |",
        f"| Rejected Hits | {summary['random_rejected']} | {(summary['random_rejected'] / summary['random_raw_hits'] * 100 if summary['random_raw_hits'] > 0 else 0):.1f}% |",
        "",
        f"- **Verified False-Positive Rate:** {summary['verified_false_positive_rate'] * 100:.1f}% ({summary['random_verified']}/{summary['random_raw_hits']})",
        f"- **Unique Site Families Fooled:** {summary.get('unique_fooled_families_count', 0)}",
        f"- **Uncertain Rate:** {summary['uncertain_false_positive_rate'] * 100:.1f}% ({summary['random_uncertain']}/{summary['random_raw_hits']})",
        f"- **Canary HTTP Requests:** {summary.get('canary_requests_count', 0)}",
        f"- **Browser Re-checks:** {summary.get('browser_rechecks_count', 0)}" + (f" ({', '.join(summary.get('browser_recheck_platforms', []))})" if summary.get('browser_recheck_platforms') else ""),
        f"- **Canary Extra Time:** {summary.get('extra_time_seconds', 0.0)}s",
        f"- **Average Time Per Hit:** {summary.get('avg_time_per_hit_seconds', 0.0)}s",
        "",
        "### Raw Hits Per Target Username",
        "",
        "| Username | Sherlock Raw | Maigret Raw | Total Raw |",
        "| :--- | :--- | :--- | :--- |",
    ]

    for item in summary.get("raw_hits_per_name", []):
        md.append(f"| `{item['username']}` | {item['sherlock']} | {item['maigret']} | {item['total_raw']} |")

    md.extend([
        "",
        "---",
        "",
        "## 2. Platforms That Fooled Verification (Verified on Nonexistent Usernames)",
        "",
    ])

    fooled = data.get("fooled_platforms", [])
    if fooled:
        md.extend([
            "| Site Family | Platform | Fooled Count | Verification Reason(s) | Verification Method(s) | Sample URL |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ])
        for f in fooled:
            fam = f.get("family", f["platform"])
            reasons = ", ".join(f["reasons"]) or "none"
            methods = ", ".join(f["methods"]) or "unknown"
            sample_url = f["urls"][0] if f["urls"] else "—"
            md.append(f"| {fam} | **{f['platform']}** | {f['fooled_count']} | {reasons} | {methods} | {sample_url} |")
    else:
        md.append("*(No platforms fooled verification; 0 verified false positives detected)*")

    md.extend([
        "",
        "---",
        "",
        "## 3. Platforms Returning Uncertain (Inconclusive)",
        "",
    ])

    uncertain = data.get("uncertain_platforms", [])
    if uncertain:
        md.extend([
            "| Platform | Uncertain Count | Verification Reason(s) | Verification Method(s) |",
            "| :--- | :--- | :--- | :--- |",
        ])
        for u in uncertain:
            reasons = ", ".join(u["reasons"]) or "none"
            methods = ", ".join(u["methods"]) or "unknown"
            md.append(f"| {u['platform']} | {u['uncertain_count']} | {reasons} | {methods} |")
    else:
        md.append("*(No platforms returned uncertain)*")

    md.extend([
        "",
        "---",
        "",
        "## 4. Control Target Comparison (`octocat`)",
        "",
    ])

    if control:
        c_raw = control.get("raw_hits", 0)
        c_ver = control.get("verified_count", 0)
        c_unc = control.get("uncertain_count", 0)
        c_rej = control.get("rejected_count", 0)
        md.extend([
            "| Metric | Count | Percentage |",
            "| :--- | :--- | :--- |",
            f"| Raw Candidate Hits | {c_raw} | 100.0% |",
            f"| Sherlock Raw Hits | {control.get('sherlock_hits', 0)} | — |",
            f"| Maigret Raw Hits | {control.get('maigret_hits', 0)} | — |",
            f"| Verified (not independently confirmed) | {c_ver} | {(c_ver / c_raw * 100 if c_raw > 0 else 0):.1f}% |",
            f"| Uncertain Hits | {c_unc} | {(c_unc / c_raw * 100 if c_raw > 0 else 0):.1f}% |",
            f"| Rejected Hits | {c_rej} | {(c_rej / c_raw * 100 if c_raw > 0 else 0):.1f}% |",
        ])
    else:
        md.append("*(Control run was disabled)*")

    md.extend([
        "",
        "---",
        "",
        "## 5. Verification Methods & Lessons Memory",
        "",
        f"- **Methods Encountered:** {', '.join(summary['verification_methods_used']) or 'none'}",
        f"- **Lessons Memory Update:** {'Applied' if data['lessons']['apply_lessons'] else 'Disabled (--apply-lessons not specified)'}",
    ])

    if data["lessons"]["lessons_applied"]:
        md.append(f"- **Platforms Added to Memory:** {', '.join(data['lessons']['lessons_applied'])}")

    if data.get("comparison"):
        comp = data["comparison"]
        m = comp["metrics"]
        md.extend([
            "",
            "---",
            "",
            "## 6. Calibration Comparison (Before vs After)",
            "",
            "| Metric | Before | After | Change |",
            "| :--- | :--- | :--- | :--- |",
            f"| Raw Candidate Hits | {m['before_raw']} | {m['after_raw']} | {m['after_raw'] - m['before_raw']:+d} |",
            f"| **Verified False Positives** | **{m['before_verified']}** ({m['before_fp_rate']*100:.1f}%) | **{m['after_verified']}** ({m['after_fp_rate']*100:.1f}%) | **{m['after_verified'] - m['before_verified']:+d}** ({m['fp_rate_change']*100:+.1f}%) |",
            f"| Uncertain Hits | {m['before_uncertain']} | {m['after_uncertain']} | {m['after_uncertain'] - m['before_uncertain']:+d} |",
            f"| Rejected Hits | {m['before_rejected']} | {m['after_rejected']} | {m['after_rejected'] - m['before_rejected']:+d} |",
            f"| Canary HTTP Requests | {m.get('before_canary_requests', 0)} | {m.get('after_canary_requests', 0)} | {m.get('after_canary_requests', 0) - m.get('before_canary_requests', 0):+d} |",
            f"| Browser Re-checks | {m.get('before_browser_rechecks', 0)} | {m.get('after_browser_rechecks', 0)} | {m.get('after_browser_rechecks', 0) - m.get('before_browser_rechecks', 0):+d} |",
            f"| Extra Canary Time | {m.get('before_extra_time', 0.0):.1f}s | {m.get('after_extra_time', 0.0):.1f}s | {m.get('after_extra_time', 0.0) - m.get('before_extra_time', 0.0):+.1f}s |",
            "",
        ])
        c_rej = comp.get("changed_to_rejected", [])
        if c_rej:
            md.append("### Hits Changed from Verified to Rejected (Soft 404)\n")
            md.append("| Platform | Target Username | Reason |")
            md.append("| :--- | :--- | :--- |")
            for item in c_rej:
                md.append(f"| **{item['platform']}** | `{item['username']}` | {item.get('after_reason', '')} |")
            md.append("")
        c_unc = comp.get("changed_to_uncertain", [])
        if c_unc:
            md.append("### Hits Changed from Verified to Uncertain\n")
            md.append("| Platform | Target Username | Reason |")
            md.append("| :--- | :--- | :--- |")
            for item in c_unc:
                md.append(f"| **{item['platform']}** | `{item['username']}` | {item.get('after_reason', '')} |")
            md.append("")
        only_after = comp.get("only_in_after", [])
        if only_after:
            md.append("### Platforms in Current Run (Not Present in Earlier Report)\n")
            md.append("| Platform | Status |")
            md.append("| :--- | :--- |")
            for item in only_after:
                md.append(f"| {item['platform']} | {item['status']} |")
            md.append("")
        recheck_plats = comp.get("browser_recheck_platforms", [])
        if recheck_plats:
            md.append(f"- **Browser Re-check Platforms:** {', '.join(recheck_plats)}\n")
        flipped = comp.get("flipped_octocat", [])
        if flipped:
            md.append("### Control Target ('octocat') Verdict Flips\n")
            md.append("| Platform | Before Verdict | After Verdict |")
            md.append("| :--- | :--- | :--- |")
            for f in flipped:
                tag_str = f" {f['tag']}" if f.get("tag") else ""
                md.append(f"| {f['platform']} | {f['before_status']} | {f['after_status']}{tag_str} |")
            md.append("")
        else:
            md.append("- **Control Target ('octocat') Verdict Flips:** None (no true positives lost)\n")

    md.append("")
    return "\n".join(md)


def save_reports(data: dict, out_path_str: str | None = None) -> tuple[Path, Path]:
    """
    Save machine-readable JSON and Markdown summary.
    Guarantees that no secret keys or environment credentials are saved.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if data.get("is_partial"):
        ts += "_partial"

    if out_path_str:
        out_p = Path(out_path_str).expanduser().resolve()
        if out_p.suffix.lower() == ".json":
            json_file = out_p
            md_file = out_p.with_suffix(".md")
        else:
            json_file = out_p.parent / f"{out_p.name}.json"
            md_file = out_p.parent / f"{out_p.name}.md"
    else:
        DEFAULT_CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
        json_file = DEFAULT_CALIBRATION_DIR / f"{ts}.json"
        md_file = DEFAULT_CALIBRATION_DIR / f"{ts}.md"

    json_file.parent.mkdir(parents=True, exist_ok=True)

    # Clean JSON output: ensure pure data dictionary
    clean_json_str = json.dumps(data, indent=2)
    json_file.write_text(clean_json_str, encoding="utf-8")

    md_content = generate_markdown_report(data)
    md_file.write_text(md_content, encoding="utf-8")

    return json_file, md_file


def print_terminal_summary(data: dict, json_path: Path, md_path: Path) -> None:
    """Print readable summary to stdout."""
    summary = data["summary"]
    env = data["environment"]
    control = data.get("control_target")
    is_partial = data.get("is_partial", False)

    status_header = "=== J.A.R.V.I.S. Calibration Summary ==="
    if is_partial:
        status_header = "=== J.A.R.V.I.S. Calibration Summary [PARTIAL - INTERRUPTED] ==="

    print(f"\n{status_header}")
    print(f"Timestamp:       {data['timestamp_formatted']}")
    print(f"Python Version:  {env['python_version']}")
    print(f"Sherlock:        {env['sherlock_version']} ({env['sherlock_path']})")
    print(f"Maigret:         {env['maigret_version']} ({env['maigret_path']})")
    print(f"Playwright:      {'YES' if env['playwright_worker_available'] else 'NO (HTTP fallback only)'}")
    print(f"Notice:          {data['variance_notice']}")
    print("-" * 50)

    print("\n[Random Nonexistent Targets]")
    print(f"  Targets Tested:     {summary['random_targets_count']}")
    print(f"  Raw Candidates:     {summary['random_raw_hits']} (Sherlock: {summary['random_sherlock_hits']}, Maigret: {summary['random_maigret_hits']})")
    fam_count = summary.get('unique_fooled_families_count', len(data.get("fooled_platforms", [])))
    print(f"  Verified (FP):      {summary['random_verified']} ({summary['verified_false_positive_rate'] * 100:.1f}%) across {fam_count} unique site families")
    print(f"  Uncertain:          {summary['random_uncertain']} ({summary['uncertain_false_positive_rate'] * 100:.1f}%)")
    print(f"  Rejected:           {summary['random_rejected']}")
    print(f"  False-Positive Rate: {summary['verified_false_positive_rate'] * 100:.1f}%")
    print(f"  Canary Requests:    {summary.get('canary_requests_count', 0)}")
    rechecks_str = str(summary.get('browser_rechecks_count', 0))
    if summary.get('browser_recheck_platforms'):
        rechecks_str += f" ({', '.join(summary['browser_recheck_platforms'])})"
    print(f"  Browser Re-checks:  {rechecks_str}")
    print(f"  Extra Canary Time:  {summary.get('extra_time_seconds', 0.0)}s (avg {summary.get('avg_time_per_hit_seconds', 0.0)}s/hit)")

    print("\n[Raw Hits Per Target]")
    for item in summary.get("raw_hits_per_name", []):
        print(f"  - {item['username']:<18} Sherlock: {item['sherlock']:<3} Maigret: {item['maigret']:<3} Total: {item['total_raw']}")

    fooled = data.get("fooled_platforms", [])
    if fooled:
        print(f"\n[Platforms Fooled (Verified on Random Name)] ({len(fooled)} across {fam_count} site families)")
        for f in fooled:
            fam = f.get("family", f["platform"])
            reasons = ", ".join(f["reasons"])
            methods = ", ".join(f["methods"])
            print(f"  * [{fam}] {f['platform']:<20} Fooled: {f['fooled_count']} target(s) | Reason: {reasons} | Method: {methods}")
    else:
        print("\n[Platforms Fooled] None. Zero verified false positives.")

    uncertain = data.get("uncertain_platforms", [])
    if uncertain:
        print(f"\n[Platforms Uncertain] ({len(uncertain)})")
        for u in uncertain[:8]:
            reasons = ", ".join(u["reasons"])
            print(f"  ? {u['platform']:<20} Count: {u['uncertain_count']} | Reason: {reasons}")
        if len(uncertain) > 8:
            print(f"  ... and {len(uncertain) - 8} more platforms (see report).")

    if control:
        print("\n[Control Target ('octocat')]")
        print(f"  Raw Candidates:     {control.get('raw_hits', 0)} (Sherlock: {control.get('sherlock_hits', 0)}, Maigret: {control.get('maigret_hits', 0)})")
        print(f"  Verified (not independently confirmed): {control.get('verified_count', 0)}")
        print(f"  Uncertain:          {control.get('uncertain_count', 0)}")
        print(f"  Rejected:           {control.get('rejected_count', 0)}")

    print(f"\n[Verification Methods Encountered]: {', '.join(summary['verification_methods_used']) or 'none'}")
    if not env["playwright_worker_available"]:
        print("  ⓘ Headless browser (Playwright) was unavailable; JS-heavy platforms checked by plain HTTP only.")

    lessons = data.get("lessons", {})
    if lessons.get("apply_lessons"):
        applied = lessons.get("lessons_applied", [])
        if applied:
            print(f"\n[Lessons Memory]: Added {len(applied)} fooled platform(s) to memory: {', '.join(applied)}")
        else:
            print("\n[Lessons Memory]: Enabled, but 0 platforms required memory insertion.")
    else:
        print(f"\n[Lessons Memory]: Disabled. Fooled platforms ({len(fooled)}) were NOT written to memory.")

    if data.get("comparison"):
        print("\n" + format_comparison_table(data["comparison"]))

    print(f"\nReports saved:")
    print(f"  JSON: {json_path}")
    print(f"  MD:   {md_path}\n")


async def run_calibration_async(
    count: int = 3,
    control: bool = True,
    apply_lessons: bool = False,
    out_path: str | None = None,
    lessons_store=None,
    explicit_names: list[str] | None = None,
    compare_path: str | None = None,
) -> tuple[dict, int]:
    """
    Main asynchronous calibration runner.
    """
    tools_ok, tool_err = check_tools_available()
    if not tools_ok:
        print(f"[calibrate] Error: {tool_err}", file=sys.stderr)
        return {}, 1

    if explicit_names:
        usernames = explicit_names
        print(f"[calibrate] Using {len(usernames)} specified test username(s):")
    else:
        usernames = generate_calibration_usernames(count)
        print(f"[calibrate] Generated {len(usernames)} random test usernames:")

    for idx, u in enumerate(usernames, start=1):
        print(f"  [{idx}] {u}")

    print("\n[calibrate] Notice: This run makes several hundred requests per target username using standard timeouts without evasion or proxies.\n")

    sherlock_status = check_tool_status("sherlock")
    maigret_status = check_tool_status("maigret")

    try:
        from modules.headless_probe import is_playwright_available
        pw_available = is_playwright_available()
    except Exception:
        pw_available = False

    temp_base_dir = Path(tempfile.mkdtemp(prefix="jarvis_calib_"))
    random_results = []
    control_result = None
    is_partial = False
    canary_context = {}

    try:
        for idx, uname in enumerate(usernames, start=1):
            print(f"[calibrate] ({idx}/{len(usernames)}) Scanning random nonexistent username '{uname}'...")
            try:
                try:
                    res = await run_single_target(uname, temp_base_dir, canary_context=canary_context)
                except TypeError:
                    res = await run_single_target(uname, temp_base_dir)
                random_results.append(res)
                print(f"            Done '{uname}': {res['raw_hits']} raw hits -> {res['verified_count']} verified, {res['uncertain_count']} uncertain, {res['rejected_count']} rejected.")
            except (KeyboardInterrupt, asyncio.CancelledError):
                print(f"\n[calibrate] Interrupted during scan of '{uname}'. Stopping...")
                is_partial = True
                break

        if control and not is_partial:
            print(f"[calibrate] Scanning control username 'octocat'...")
            try:
                try:
                    control_result = await run_single_target("octocat", temp_base_dir, canary_context=canary_context)
                except TypeError:
                    control_result = await run_single_target("octocat", temp_base_dir)
                print(f"            Done 'octocat': {control_result['raw_hits']} raw hits -> {control_result['verified_count']} verified, {control_result['uncertain_count']} uncertain, {control_result['rejected_count']} rejected.")
            except (KeyboardInterrupt, asyncio.CancelledError):
                print(f"\n[calibrate] Interrupted during control scan ('octocat'). Stopping...")
                is_partial = True

    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[calibrate] Interrupted by user (Ctrl+C). Finalizing partial report...")
        is_partial = True
    finally:
        # Guarantee temporary directory cleanup
        if temp_base_dir.exists():
            shutil.rmtree(temp_base_dir, ignore_errors=True)

    data = aggregate_results(
        random_results=random_results,
        control_result=control_result,
        pw_available=pw_available,
        sherlock_status=sherlock_status,
        maigret_status=maigret_status,
        apply_lessons=apply_lessons,
        lessons_store=lessons_store,
        is_partial=is_partial,
        canary_context=canary_context,
    )

    if compare_path:
        try:
            with open(compare_path, "r", encoding="utf-8") as f:
                before_data = json.load(f)
            comparison = compare_calibrations(before_data, data)
            data["comparison"] = comparison
        except Exception as exc:
            print(f"[calibrate] Warning: Failed to compare with '{compare_path}': {exc}", file=sys.stderr)

    json_path, md_path = save_reports(data, out_path)
    print_terminal_summary(data, json_path, md_path)

    exit_code = 130 if is_partial else 0
    return data, exit_code


def parse_and_run_calibration(args: list[str]) -> int:
    """
    Parse CLI options and execute calibration command synchronously.
    """
    parser = argparse.ArgumentParser(
        prog="jarvis calibrate",
        description="Measure false-positive rates of username OSINT enumeration on random nonexistent usernames.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=3,
        help="Number of random usernames to test (default: 3, max: 10, clamped).",
    )
    parser.add_argument(
        "--names",
        type=str,
        default=None,
        help="Comma-separated list of usernames to test (alphanumeric, 8-32 chars, max 10).",
    )
    parser.add_argument(
        "--compare",
        dest="compare_path",
        type=str,
        default=None,
        help="Path to previous calibration JSON report for comparison.",
    )
    parser.add_argument(
        "--control",
        dest="control",
        action="store_true",
        default=True,
        help="Run control username 'octocat' for comparison (default: on).",
    )
    parser.add_argument(
        "--no-control",
        dest="control",
        action="store_false",
        help="Disable control username 'octocat' run.",
    )
    parser.add_argument(
        "--apply-lessons",
        action="store_true",
        default=False,
        help="Persist platforms that fooled verification into lessons store memory (default: off).",
    )
    parser.add_argument(
        "--out",
        dest="out_path",
        type=str,
        default=None,
        help="Output path for JSON report (default: ~/.jarvis/calibration/<timestamp>.json).",
    )

    parsed = parser.parse_args(args)

    explicit_names = None
    if parsed.names:
        try:
            explicit_names = validate_names(parsed.names)
        except ValueError as exc:
            print(f"[calibrate] Error: {exc}", file=sys.stderr)
            return 2

    compare_path = None
    if parsed.compare_path:
        comp_file = Path(parsed.compare_path).expanduser().resolve()
        if not comp_file.is_file():
            print(f"[calibrate] Error: Compare file not found: {parsed.compare_path}", file=sys.stderr)
            return 2
        compare_path = str(comp_file)

    count = len(explicit_names) if explicit_names else min(10, max(1, parsed.count))

    try:
        _, exit_code = asyncio.run(
            run_calibration_async(
                count=count,
                control=parsed.control,
                apply_lessons=parsed.apply_lessons,
                out_path=parsed.out_path,
                explicit_names=explicit_names,
                compare_path=compare_path,
            )
        )
        return exit_code
    except KeyboardInterrupt:
        print("\n[calibrate] Cancelled by user.")
        return 130


def print_jarvis_help() -> None:
    """Print general CLI help text for jarvis.py without importing GUI libraries."""
    help_text = """usage: python3 jarvis.py [-h] [--cli | --tui] [--hud | --mini] [investigate <target>] [resume <target>] [doctor] [calibrate ...]

J.A.R.V.I.S. : Autonomous Tactical OSINT Specialist & Intelligence System

Commands:
  investigate <target>  Run multi-pass OSINT sweep against a target (email, domain, username, ip, name)
  resume <target>       Open saved investigation case
  cases                 List all archived cases and saved sessions
  pivot <entity>        Pivot investigation on a discovered handle or IP
  notes <text>          Append investigative note or task reminder
  export                Generate offline HTML investigation dossier
  doctor                Run diagnostic health checks on tools and keys
  calibrate [options]   Measure false-positive rates of username pipeline on random usernames
  help, -h, --help      Display this command reference and exit

Calibration Options:
  python3 jarvis.py calibrate [--count N] [--names A,B,C] [--compare PATH] [--no-control] [--apply-lessons] [--out PATH]
    --count N           Number of random usernames to test (default: 3, max: 10)
    --names A,B,C       Comma-separated list of test usernames (alphanumeric, 8-32 chars, max 10)
    --compare PATH      Compare results against a previous calibration JSON report
    --control           Include control username 'octocat' (default: on)
    --no-control        Skip control username 'octocat'
    --apply-lessons     Add platforms that fooled verification to lessons memory (default: off)
    --out PATH          Output path (default: ~/.jarvis/calibration/<timestamp>.json and .md)

Desktop / TUI Options:
  --cli, --tui          Launch interactive terminal mode
  --hud, --mini         Launch desktop HUD overlay mode
"""
    print(help_text)
