#!/usr/bin/env python3
# modules/_pw_worker.py
"""
Standalone Playwright Worker for JARVIS.
Executes under an isolated Python environment (~/.jarvis/pw-venv/bin/python3).
Reads JSON from stdin, controls headless Chromium out-of-process, and outputs
structured JSON to stdout. Never hangs (enforces strict timeout).
"""

import sys
import json
import asyncio
import re
import os
from pathlib import Path

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

_GENERIC_TITLES = {
    "instagram": ["instagram"],
    "tiktok": ["tiktok", "make your day"],
    "facebook": ["facebook", "log in", "log into facebook"],
    "twitter": ["x", "twitter"],
    "x": ["x", "twitter"],
    "threads": ["threads"],
    "snapchat": ["snapchat"],
    "linkedin": ["linkedin"],
}

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def get_chromium_executable() -> str | None:
    """Return path to system Chromium if available and executable, else None."""
    sys_chrome = "/usr/bin/chromium"
    if os.path.isfile(sys_chrome) and os.access(sys_chrome, os.X_OK):
        return sys_chrome
    return None


async def run_check():
    """Quick self-test to verify Playwright and Chromium availability."""
    from playwright.async_api import async_playwright

    exe_path = get_chromium_executable()
    launch_kwargs = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
    }
    if exe_path:
        launch_kwargs["executable_path"] = exe_path

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        await browser.close()

    result = {
        "ok": True,
        "browser": "system chromium" if exe_path else "downloaded chromium",
        "executable_path": exe_path or "default playwright chromium",
    }
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()


async def execute_request(req: dict):
    url = req.get("url", "")
    platform = (req.get("platform") or "").lower().strip()
    screenshot_path = req.get("screenshot_path")
    timeout_sec = float(req.get("timeout", 15.0))
    require_screenshot = bool(req.get("require_screenshot", False))

    if not url:
        raise ValueError("URL is required")

    from playwright.async_api import async_playwright

    exe_path = get_chromium_executable()
    launch_kwargs = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
    }
    if exe_path:
        launch_kwargs["executable_path"] = exe_path

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        final_url = url
        status_code = None
        page_title = ""
        og_title = None
        og_description = None
        og_image = None
        body_text = ""
        not_found_signals = []
        saved_screenshot = None

        try:
            resp = None
            try:
                resp = await page.goto(url, wait_until="networkidle", timeout=min(10000, int(timeout_sec * 600)))
            except Exception:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=min(6000, int(timeout_sec * 400)))

            if resp:
                status_code = resp.status

            final_url = page.url
            await asyncio.sleep(1.0)

            page_title = (await page.title() or "").strip()

            og_title = await page.evaluate("""
                () => {
                    const el = document.querySelector('meta[property="og:title"]');
                    return el ? el.getAttribute('content') : null;
                }
            """)

            og_image = await page.evaluate("""
                () => {
                    const el = document.querySelector('meta[property="og:image"]');
                    return el ? el.getAttribute('content') : null;
                }
            """)

            og_description = await page.evaluate("""
                () => {
                    const el = document.querySelector('meta[property="og:description"]') || document.querySelector('meta[name="description"]');
                    return el ? el.getAttribute('content') : null;
                }
            """)

            body_text = await page.evaluate("""
                () => document.body ? document.body.innerText.substring(0, 3000) : ''
            """) or ""

            # Check not-found patterns
            combined_text = f"{page_title}\n{og_title or ''}\n{body_text}".lower()
            for pat in _NOT_FOUND_PATTERNS:
                if re.search(pat, combined_text, re.IGNORECASE):
                    not_found_signals.append(pat)

            # Generic title check
            is_generic = False
            for gt in _GENERIC_TITLES.get(platform, []):
                if page_title.lower().strip() == gt.lower().strip():
                    is_generic = True
                    break

            has_not_found = len(not_found_signals) > 0
            has_profile_signal = (
                (og_title and not any(re.search(pat, og_title, re.IGNORECASE) for pat in _NOT_FOUND_PATTERNS))
                or (og_image and "default" not in og_image.lower())
                or (page_title and not is_generic and not has_not_found)
            )

            exists = False
            if has_not_found:
                exists = False
            elif has_profile_signal:
                exists = True
            else:
                exists = False

            # Capture screenshot if requested and applicable
            if screenshot_path and (exists or require_screenshot):
                try:
                    Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=screenshot_path, full_page=True)
                    saved_screenshot = screenshot_path
                except Exception:
                    pass

            result = {
                "final_url": final_url,
                "status_code": status_code,
                "page_title": page_title,
                "title": page_title,
                "og_title": og_title,
                "og_description": og_description,
                "og_image": og_image,
                "text_excerpt": body_text[:500],
                "not_found_signals": not_found_signals,
                "exists": exists,
                "screenshot_path": saved_screenshot,
                "error": None,
            }
            sys.stdout.write(json.dumps(result) + "\n")
            sys.stdout.flush()

        finally:
            await context.close()
            await browser.close()


async def main():
    if "--check" in sys.argv:
        try:
            await asyncio.wait_for(run_check(), timeout=5.0)
            sys.exit(0)
        except Exception as e:
            err_res = {"ok": False, "error": str(e)}
            sys.stdout.write(json.dumps(err_res) + "\n")
            sys.stdout.flush()
            sys.exit(1)

    raw_input = sys.stdin.read().strip()
    if not raw_input:
        sys.stderr.write("No JSON request provided on stdin\n")
        sys.exit(1)

    try:
        req = json.loads(raw_input)
    except Exception as e:
        sys.stderr.write(f"Invalid JSON on stdin: {e}\n")
        sys.exit(1)

    overall_timeout = float(req.get("timeout", 15.0)) + 3.0
    try:
        await asyncio.wait_for(execute_request(req), timeout=overall_timeout)
        sys.exit(0)
    except Exception as e:
        err_res = {
            "error": str(e),
            "exists": False,
            "page_title": "",
            "title": "",
            "og_image": None,
            "og_description": None,
            "screenshot_path": None,
        }
        sys.stdout.write(json.dumps(err_res) + "\n")
        sys.stdout.flush()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
