#!/usr/bin/env bash
set -euo pipefail

# Idempotent setup for JARVIS isolated Playwright environment
JARVIS_DIR="${HOME}/.jarvis"
VENV_DIR="${JARVIS_DIR}/pw-venv"

mkdir -p "${JARVIS_DIR}"

if [ ! -f "${VENV_DIR}/bin/python3" ]; then
    echo "[setup_playwright] Creating isolated venv at ${VENV_DIR}..."
    python3 -m venv "${VENV_DIR}"
fi

echo "[setup_playwright] Ensuring playwright package is installed..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip setuptools
"${VENV_DIR}/bin/pip" install --quiet playwright

# Test launching with system Chromium
echo "[setup_playwright] Testing browser launch with system Chromium (/usr/bin/chromium)..."
SYSTEM_CHROME_OK=0
if [ -x "/usr/bin/chromium" ]; then
    if "${VENV_DIR}/bin/python3" -c "
import asyncio
from playwright.async_api import async_playwright

async def test():
    async with async_playwright() as p:
        b = await p.chromium.launch(
            executable_path='/usr/bin/chromium',
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        await b.close()

asyncio.run(test())
" 2>/dev/null; then
        SYSTEM_CHROME_OK=1
        echo "[setup_playwright] System Chromium (/usr/bin/chromium) verified successfully. No download needed."
    fi
fi

if [ "${SYSTEM_CHROME_OK}" -eq 0 ]; then
    echo "[setup_playwright] System Chromium not usable; installing browser via playwright..."
    "${VENV_DIR}/bin/playwright" install chromium
    echo "[setup_playwright] Playwright Chromium installation complete."
fi

echo "[setup_playwright] Playwright isolated environment ready at ${VENV_DIR}."
