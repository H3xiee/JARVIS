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