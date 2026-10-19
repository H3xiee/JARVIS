# core/input_parser.py
import re
from dataclasses import dataclass
from typing import Dict


@dataclass
class ParsedTarget:
    target_type: str
    value: str
    metadata: Dict


def parse(raw: str) -> ParsedTarget:
    raw = raw.strip().strip("'\"")

    # File / image path
    if (
        re.search(r"\.(?:png|jpe?g|gif|webp|bmp|tiff|svg|pdf|txt|csv|json|doc|docx|mp4|mov|mkv|zip|tar|gz)$", raw, re.IGNORECASE)
        or "/" in raw
        or "\\" in raw
    ):
        return ParsedTarget("file", raw, {"error": f"File or image target '{raw}' is not supported for OSINT investigation."})

    # Email
    if re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", raw):
        local, domain = raw.lower().rsplit("@", 1)
        return ParsedTarget("email", raw.lower(), {"local": local, "domain": domain})

    # IPv4
    if re.match(r"^(\d{1,3}\.){3}\d{1,3}$", raw):
        return ParsedTarget("ip", raw, {"version": 4})

    # IPv6
    if re.match(r"^[0-9a-fA-F:]{7,39}$", raw) and ":" in raw:
        return ParsedTarget("ip", raw, {"version": 6})

    # Domain
    if re.match(r"^(https?://)?(www\.)?[a-zA-Z0-9\-]+\.[a-zA-Z]{2,}(/.*)?$", raw):
        clean = re.sub(r"^https?://", "", raw)
        clean = re.sub(r"^www\.", "", clean).split("/")[0]
        return ParsedTarget("domain", clean.lower(), {"apex": clean})

    # Phone
    if re.match(r"^\+?[\d\s\-().]{7,20}$", raw):
        return ParsedTarget("phone", re.sub(r"[^\d+]", "", raw), {})

    # Single word username
    if re.match(r"^[a-zA-Z0-9_.\-]{2,40}$", raw):
        return ParsedTarget("username", raw.lower(), {})

    # Full name (two or more words, letters only) — catches "sree danush"
    if re.match(r"^[a-zA-Z]+([\s\-][a-zA-Z]+)+$", raw):
        parts = raw.lower().split()
        return ParsedTarget(
            "name",
            raw.lower(),
            {
                "first": parts[0],
                "last": parts[-1],
                "query": "+".join(parts),
            },
        )

    return ParsedTarget("unsupported", raw, {"error": f"Cannot classify: {raw!r}"})


def is_classifiable_target(raw: str) -> bool:
    if not raw or not isinstance(raw, str) or not raw.strip():
        return False
    try:
        parsed = parse(raw)
        return parsed.target_type in ("username", "email", "domain", "ip", "name", "phone")
    except Exception:
        return False


VALID_BRIEF_TYPES = {"username", "email", "name", "domain", "ip", "phone"}


def parse_brief_identifiers(entries: list) -> list:
    """
    Validates and sanitizes a list of {type, value, primary} identifier entries.
    Allowed types: username, email, name, domain, ip, phone.
    Rejects empty, over-long (> 100 chars), or malformed values.
    Returns: list of {"type": str, "value": str, "primary": bool} with exactly one primary (default: first).
    """
    if not isinstance(entries, list):
        return []

    parsed_list = []

    for item in entries:
        if not isinstance(item, dict):
            continue
        raw_type = str(item.get("type", "")).strip().lower()
        raw_val = str(item.get("value", "")).strip().strip("'\"")
        is_primary = bool(item.get("primary", False))

        if not raw_type or raw_type not in VALID_BRIEF_TYPES:
            continue
        if not raw_val or len(raw_val) > 100:
            continue

        clean_val = None
        if raw_type == "email":
            if re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", raw_val):
                clean_val = raw_val.lower()
        elif raw_type == "ip":
            if (re.match(r"^(\d{1,3}\.){3}\d{1,3}$", raw_val)) or (re.match(r"^[0-9a-fA-F:]{7,39}$", raw_val) and ":" in raw_val):
                clean_val = raw_val
        elif raw_type == "domain":
            clean_dom = re.sub(r"^https?://", "", raw_val)
            clean_dom = re.sub(r"^www\.", "", clean_dom).split("/")[0].strip()
            if re.match(r"^[a-zA-Z0-9\-]+(\.[a-zA-Z0-9\-]+)*\.[a-zA-Z]{2,}$", clean_dom):
                clean_val = clean_dom.lower()
        elif raw_type == "phone":
            cleaned_phone = re.sub(r"[^\d+]", "", raw_val)
            if re.match(r"^\+?\d{7,20}$", cleaned_phone):
                clean_val = cleaned_phone
        elif raw_type == "username":
            if re.match(r"^[a-zA-Z0-9_.\-]{2,40}$", raw_val):
                clean_val = raw_val.lower()
        elif raw_type == "name":
            clean_name = " ".join(raw_val.split())
            if re.match(r"^[a-zA-Z]+([\s\-][a-zA-Z]+)+$", clean_name) and len(clean_name) <= 80:
                clean_val = clean_name.lower()

        if not clean_val:
            continue

        # Prevent exact duplicate type + value
        if any(e["type"] == raw_type and e["value"] == clean_val for e in parsed_list):
            continue

        parsed_list.append({
            "type": raw_type,
            "value": clean_val,
            "primary": is_primary
        })

    if not parsed_list:
        return []

    # Enforce exactly one primary
    primary_indices = [i for i, e in enumerate(parsed_list) if e.get("primary")]
    if len(primary_indices) == 1:
        pass
    elif len(primary_indices) > 1:
        for idx in primary_indices[1:]:
            parsed_list[idx]["primary"] = False
    else:
        parsed_list[0]["primary"] = True

    return parsed_list