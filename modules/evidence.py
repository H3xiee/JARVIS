# modules/evidence.py
"""
Evidence capture module.
Takes full-page screenshots of verified targets via the out-of-process Playwright worker.
"""
import sys
from pathlib import Path
from datetime import datetime
import asyncio
import json

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))


async def capture(url: str, case_slug: str, entity_id: str) -> dict:
    """
    Capture a full-page screenshot of a URL via the out-of-process Playwright worker.

    Returns:
        {"screenshot_path": str|None, "captured_at": str, "success": bool}
    """
    result = {
        "screenshot_path": None,
        "captured_at": datetime.now().isoformat(),
        "success": False
    }

    try:
        from modules.headless_probe import is_playwright_available, VENV_PYTHON, WORKER_SCRIPT
        if not is_playwright_available():
            return result
    except Exception:
        return result

    try:
        evidence_dir = PROJECT_ROOT / "cases" / case_slug / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = str(evidence_dir / f"{entity_id}.png")

        req_data = {
            "url": url,
            "platform": "web",
            "screenshot_path": screenshot_path,
            "require_screenshot": True,
            "timeout": 15.0,
        }

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
            if data.get("screenshot_path") and Path(screenshot_path).exists():
                result["screenshot_path"] = f"cases/{case_slug}/evidence/{entity_id}.png"
                result["success"] = True
    except Exception:
        pass

    return result
