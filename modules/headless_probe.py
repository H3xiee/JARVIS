# modules/headless_probe.py
"""
Headless browser probe for JS-heavy platforms.
Uses an out-of-process Playwright worker (~/.jarvis/pw-venv/bin/python3 modules/_pw_worker.py)
to render pages that httpx can't verify.
Degrades gracefully if the venv or Playwright is not available.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

import asyncio
import json
import re

VENV_PYTHON = Path.home() / ".jarvis" / "pw-venv" / "bin" / "python3"
WORKER_SCRIPT = Path(__file__).parent / "_pw_worker.py"

# Platforms known to require JS rendering for profile verification
JS_HEAVY_PLATFORMS = {
    "instagram", "tiktok", "facebook", "twitter", "x",
    "threads", "snapchat", "linkedin",
}

# Signals that indicate a profile does NOT exist
_NOT_FOUND_PATTERNS = [
    r"page not found",
    r"user not found",
    r"this account doesn't exist",
    r"doesn't exist",
    r"sorry, this page",
    r"content isn't available",
    r"this page isn't available",
    r"no results found",
    r"404",
    r"couldn't find this account",
    r"profile isn't available",
    r"the link may be broken",
    r"profile may have been removed",
]

_NOT_FOUND_RE = re.compile(
    "|".join(_NOT_FOUND_PATTERNS), re.IGNORECASE
)


async def get_browser_context(pw=None):
    """Deprecated: browser runs out-of-process via _pw_worker.py."""
    return None, None


_PLAYWRIGHT_STATUS = None


def get_playwright_status() -> dict:
    """
    Check status of the out-of-process Playwright worker.
    Caches the result for the process lifetime.
    """
    global _PLAYWRIGHT_STATUS
    if _PLAYWRIGHT_STATUS is not None:
        return _PLAYWRIGHT_STATUS

    if not VENV_PYTHON.is_file():
        _PLAYWRIGHT_STATUS = {
            "available": False,
            "interpreter": str(VENV_PYTHON),
            "browser": "none",
            "executable_path": "not found",
            "reason": f"Isolated venv not found at {VENV_PYTHON}",
            "setup_cmd": "bash scripts/setup_playwright.sh",
        }
        return _PLAYWRIGHT_STATUS

    if not WORKER_SCRIPT.is_file():
        _PLAYWRIGHT_STATUS = {
            "available": False,
            "interpreter": str(VENV_PYTHON),
            "browser": "none",
            "executable_path": "not found",
            "reason": f"Worker script missing at {WORKER_SCRIPT}",
            "setup_cmd": "bash scripts/setup_playwright.sh",
        }
        return _PLAYWRIGHT_STATUS

    try:
        import subprocess
        p = subprocess.run(
            [str(VENV_PYTHON), str(WORKER_SCRIPT), "--check"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if p.returncode == 0:
            data = json.loads(p.stdout)
            if data.get("ok"):
                _PLAYWRIGHT_STATUS = {
                    "available": True,
                    "interpreter": str(VENV_PYTHON),
                    "browser": data.get("browser", "chromium"),
                    "executable_path": data.get("executable_path", ""),
                    "reason": "OK",
                    "setup_cmd": "bash scripts/setup_playwright.sh",
                }
                return _PLAYWRIGHT_STATUS
            else:
                _PLAYWRIGHT_STATUS = {
                    "available": False,
                    "interpreter": str(VENV_PYTHON),
                    "browser": "none",
                    "executable_path": "none",
                    "reason": data.get("error", "Worker check failed"),
                    "setup_cmd": "bash scripts/setup_playwright.sh",
                }
                return _PLAYWRIGHT_STATUS
        else:
            err = (p.stderr or p.stdout or "").strip()
            _PLAYWRIGHT_STATUS = {
                "available": False,
                "interpreter": str(VENV_PYTHON),
                "browser": "none",
                "executable_path": "none",
                "reason": f"Worker exited with code {p.returncode}: {err}",
                "setup_cmd": "bash scripts/setup_playwright.sh",
            }
            return _PLAYWRIGHT_STATUS
    except Exception as exc:
        _PLAYWRIGHT_STATUS = {
            "available": False,
            "interpreter": str(VENV_PYTHON),
            "browser": "none",
            "executable_path": "none",
            "reason": f"Worker check error: {exc}",
            "setup_cmd": "bash scripts/setup_playwright.sh",
        }
        return _PLAYWRIGHT_STATUS


def is_playwright_available() -> bool:
    """Check if the out-of-process Playwright worker is functional."""
    status = get_playwright_status()
    return status.get("available", False)


async def probe(url: str, platform: str, screenshot_path: str|None = None) -> dict:
    """
    Load a URL in headless Chromium via the out-of-process worker,
    wait for JS rendering, check whether a real profile exists,
    and optionally capture a screenshot.

    Returns:
        {"exists": bool, "title": str, "og_image": str|None, "og_description": str|None, ...}
    """
    result = {
        "exists": False,
        "title": "",
        "og_image": None,
        "og_description": None,
        "status_code": None,
        "screenshot_path": None,
        "not_found_signals": [],
    }

    if not is_playwright_available():
        return result

    req_data = {
        "url": url,
        "platform": platform,
        "screenshot_path": screenshot_path,
        "timeout": 15.0,
    }

    try:
        proc = await asyncio.create_subprocess_exec(
            str(VENV_PYTHON),
            str(WORKER_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout_bytes, _ = await asyncio.wait_for(
            proc.communicate(json.dumps(req_data).encode("utf-8")),
            timeout=22.0,
        )

        if proc.returncode == 0 and stdout_bytes:
            data = json.loads(stdout_bytes.decode("utf-8"))
            result.update({
                "exists": bool(data.get("exists", False)),
                "title": data.get("page_title") or data.get("title") or "",
                "og_image": data.get("og_image"),
                "og_description": data.get("og_description"),
                "status_code": data.get("status_code"),
                "screenshot_path": data.get("screenshot_path"),
                "not_found_signals": data.get("not_found_signals", []),
                "final_url": data.get("final_url", url),
                "text_excerpt": data.get("text_excerpt", ""),
                "body": data.get("text_excerpt", ""),
            })
            return result
        else:
            return result
    except Exception:
        return result
