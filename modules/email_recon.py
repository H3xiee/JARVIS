import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

import asyncio
import logging
import shutil
import time
from typing import List, Callable, Any, Awaitable, Dict, Tuple, Optional
import httpx
from core.target_model import Entity, Breach, Target
from core.tool_locator import find_tool, build_subprocess_env
import core.config

logger = logging.getLogger(__name__)

# Provider in-process cache: (provider_name, email) -> (cached_timestamp, result_tuple)
_PROVIDER_CACHE: Dict[Tuple[str, str], Tuple[float, Tuple[str, str, List[Dict[str, Any]], str]]] = {}
_LAST_CALL_TIMESTAMP: Dict[str, float] = {}
_PROVIDER_LOCK: asyncio.Lock = asyncio.Lock()


def clear_provider_cache() -> None:
    """Clear in-process breach provider response cache."""
    _PROVIDER_CACHE.clear()


async def _throttle_provider(provider_name: str, min_delay: float = 1.0) -> None:
    """Ensure a minimum delay between consecutive calls to the same provider."""
    async with _PROVIDER_LOCK:
        now = time.time()
        last_call = _LAST_CALL_TIMESTAMP.get(provider_name, 0.0)
        elapsed = now - last_call
        if elapsed < min_delay:
            await asyncio.sleep(min_delay - elapsed)
        _LAST_CALL_TIMESTAMP[provider_name] = time.time()


async def _http_get_with_retry(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 10.0,
    max_retries: int = 1,
    backoff: float = 1.0,
) -> httpx.Response:
    """Execute GET request with timeout=10s, follow_redirects=True, and 1 retry on 5xx or timeout."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                if 500 <= resp.status_code < 600 and attempt < max_retries:
                    await asyncio.sleep(backoff)
                    continue
                return resp
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_exc = exc
            if attempt < max_retries:
                await asyncio.sleep(backoff)
                continue
            raise
    if last_exc:
        raise last_exc


async def check_xposedornot(email: str) -> Tuple[str, str, List[Dict[str, Any]], str]:
    """Check XposedOrNot breach analytics API for email exposures."""
    provider_name = "XposedOrNot"
    norm_email = email.strip().lower()
    cache_key = (provider_name, norm_email)

    now = time.time()
    if cache_key in _PROVIDER_CACHE:
        cached_time, cached_res = _PROVIDER_CACHE[cache_key]
        if now - cached_time < 300.0:
            p_name, status, recs, note = cached_res
            return p_name, status, [dict(r) for r in recs], note

    await _throttle_provider(provider_name)

    url = f"https://api.xposedornot.com/v1/breach-analytics?email={email.strip()}"
    headers = {"User-Agent": "JARVIS-OSINT", "Accept": "application/json"}

    try:
        resp = await _http_get_with_retry(url, headers=headers, timeout=10.0)
    except httpx.TimeoutException:
        note = f"{provider_name} error: request timed out"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "error", [], note
    except Exception as e:
        note = f"{provider_name} error: {e}"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "error", [], note

    if resp.status_code == 429:
        note = f"{provider_name} rate limited, skipped"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "rate_limited", [], note
    elif resp.status_code == 404:
        note = f"{provider_name}: not found in this source"
        result = (provider_name, "ok", [], note)
        _PROVIDER_CACHE[cache_key] = (time.time(), result)
        return result
    elif resp.status_code != 200:
        note = f"{provider_name} failed: HTTP {resp.status_code}"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "error", [], note

    try:
        data = resp.json()
    except Exception:
        note = f"{provider_name} error: invalid JSON response"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "error", [], note

    if not isinstance(data, dict):
        note = f"{provider_name} error: invalid JSON response"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "error", [], note

    if data.get("Error") == "Not found" or not data.get("ExposedBreaches"):
        note = f"{provider_name}: not found in this source"
        result = (provider_name, "ok", [], note)
        _PROVIDER_CACHE[cache_key] = (time.time(), result)
        return result

    raw_breaches = data.get("ExposedBreaches", {}).get("breaches_details") or []
    records = []
    for item in raw_breaches:
        if not isinstance(item, dict):
            continue
        # Drop any password or credential fields
        item.pop("password", None)
        item.pop("passwords", None)
        item.pop("top_passwords", None)
        item.pop("hash", None)
        item.pop("hashes", None)

        b_name = item.get("breach", "Unknown")
        b_date = str(item.get("xposed_date") or "Unknown")
        fields_str = item.get("xposed_data", "")
        data_types = [f.strip() for f in fields_str.split(";") if f.strip()]
        rec_count = item.get("xposed_records")

        records.append({
            "name": b_name,
            "date": b_date,
            "data_types": data_types,
            "record_count": rec_count,
        })

    if records:
        note = f"{provider_name}: {len(records)} breaches found"
    else:
        note = f"{provider_name}: not found in this source"

    result = (provider_name, "ok", records, note)
    _PROVIDER_CACHE[cache_key] = (time.time(), result)
    return result


async def check_hudsonrock(email: str) -> Tuple[str, str, List[Dict[str, Any]], str]:
    """Check Hudson Rock Cavalier infostealer exposure API for email compromise."""
    provider_name = "Hudson Rock"
    norm_email = email.strip().lower()
    cache_key = (provider_name, norm_email)

    now = time.time()
    if cache_key in _PROVIDER_CACHE:
        cached_time, cached_res = _PROVIDER_CACHE[cache_key]
        if now - cached_time < 300.0:
            p_name, status, recs, note = cached_res
            return p_name, status, [dict(r) for r in recs], note

    await _throttle_provider(provider_name)

    url = f"https://cavalier.hudsonrock.com/api/json/v2/osint-tools/search-by-email?email={email.strip()}"
    headers = {"User-Agent": "JARVIS-OSINT", "Accept": "application/json"}

    try:
        resp = await _http_get_with_retry(url, headers=headers, timeout=10.0)
    except httpx.TimeoutException:
        note = f"{provider_name} error: request timed out"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "error", [], note
    except Exception as e:
        note = f"{provider_name} error: {e}"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "error", [], note

    if resp.status_code == 429:
        note = f"{provider_name} rate limited, skipped"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "rate_limited", [], note
    elif resp.status_code == 404:
        note = f"{provider_name}: not found in this source"
        result = (provider_name, "ok", [], note)
        _PROVIDER_CACHE[cache_key] = (time.time(), result)
        return result
    elif resp.status_code != 200:
        note = f"{provider_name} failed: HTTP {resp.status_code}"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "error", [], note

    try:
        data = resp.json()
    except Exception:
        note = f"{provider_name} error: invalid JSON response"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "error", [], note

    if not isinstance(data, dict):
        note = f"{provider_name} error: invalid JSON response"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "error", [], note

    stealers = data.get("stealers") or []
    if not stealers:
        note = f"{provider_name}: not found in this source"
        result = (provider_name, "ok", [], note)
        _PROVIDER_CACHE[cache_key] = (time.time(), result)
        return result

    records = []
    for item in stealers:
        if not isinstance(item, dict):
            continue
        # CRITICAL: Always drop leaked passwords and credential lists before storing or processing!
        item.pop("top_passwords", None)
        item.pop("passwords", None)
        item.pop("password", None)
        item.pop("hashes", None)
        item.pop("hash", None)
        item.pop("top_logins", None)

        comp_name = item.get("computer_name") or "Unknown"
        date_raw = item.get("date_compromised", "Unknown")
        date_str = date_raw[:10] if isinstance(date_raw, str) and len(date_raw) >= 10 else str(date_raw)
        corp_cnt = item.get("total_corporate_services") or 0
        user_cnt = item.get("total_user_services") or 0
        rec_cnt = corp_cnt + user_cnt

        if comp_name and comp_name not in ("Not Found", "Unknown"):
            b_name = f"Infostealer ({comp_name})"
        elif date_str and date_str != "Unknown":
            b_name = f"Infostealer Compromise ({date_str})"
        else:
            b_name = "Infostealer Compromise"

        data_types = ["Credentials", "System info"]
        if item.get("malware_path") and item.get("malware_path") != "Not Found":
            data_types.append("Malware path")
        if item.get("operating_system") and item.get("operating_system") != "Not Found":
            data_types.append("OS telemetry")

        records.append({
            "name": b_name,
            "date": date_str,
            "data_types": data_types,
            "record_count": rec_cnt if rec_cnt > 0 else None,
        })

    if records:
        note = f"{provider_name}: {len(records)} compromises found"
    else:
        note = f"{provider_name}: not found in this source"

    result = (provider_name, "ok", records, note)
    _PROVIDER_CACHE[cache_key] = (time.time(), result)
    return result


async def _check_breachdirectory_impl(email: str) -> Tuple[str, str, List[Dict[str, Any]], str]:
    """Query BreachDirectory via RapidAPI if RAPIDAPI_KEY is configured."""
    provider_name = "BreachDirectory"
    api_key = core.config.get_config("RAPIDAPI_KEY")
    if not api_key:
        note = f"{provider_name} skipped: no key configured"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "skipped", [], note

    norm_email = email.strip().lower()
    cache_key = (provider_name, norm_email)

    now = time.time()
    if cache_key in _PROVIDER_CACHE:
        cached_time, cached_res = _PROVIDER_CACHE[cache_key]
        if now - cached_time < 300.0:
            p_name, status, recs, note = cached_res
            return p_name, status, [dict(r) for r in recs], note

    await _throttle_provider(provider_name)

    url = f"https://breachdirectory.p.rapidapi.com/?func=auto&term={email.strip()}"
    headers = {
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": "breachdirectory.p.rapidapi.com",
        "User-Agent": "JARVIS-OSINT",
    }

    try:
        resp = await _http_get_with_retry(url, headers=headers, timeout=10.0)
    except httpx.TimeoutException:
        note = f"{provider_name} error: request timed out"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "error", [], note
    except Exception as e:
        note = f"{provider_name} error: {e}"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "error", [], note

    if resp.status_code == 429:
        note = f"{provider_name} rate limited, skipped"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "rate_limited", [], note
    elif resp.status_code == 404:
        note = f"{provider_name}: not found in this source"
        result = (provider_name, "ok", [], note)
        _PROVIDER_CACHE[cache_key] = (time.time(), result)
        return result
    elif resp.status_code != 200:
        note = f"{provider_name} failed: HTTP {resp.status_code}"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "error", [], note

    try:
        data = resp.json()
    except Exception:
        note = f"{provider_name} error: invalid JSON response"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "error", [], note

    if not isinstance(data, dict):
        note = f"{provider_name} error: invalid JSON response"
        logger.warning(f"[email_recon] {note}")
        return provider_name, "error", [], note

    if not data.get("found"):
        note = f"{provider_name}: not found in this source"
        result = (provider_name, "ok", [], note)
        _PROVIDER_CACHE[cache_key] = (time.time(), result)
        return result

    raw_items = data.get("result", [])
    records = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        item.pop("password", None)
        item.pop("passwords", None)
        item.pop("top_passwords", None)
        item.pop("hash", None)
        item.pop("hashes", None)

        b_name = item.get("sources", ["Unknown"])[0] if item.get("sources") else "Unknown"
        b_date = str(item.get("last_breach", "Unknown"))
        fields = item.get("fields", [])
        if isinstance(fields, str):
            fields = [f.strip() for f in fields.split(",") if f.strip()]

        records.append({
            "name": b_name,
            "date": b_date,
            "data_types": fields,
            "record_count": None,
        })

    if records:
        note = f"{provider_name}: {len(records)} breaches found"
    else:
        note = f"{provider_name}: not found in this source"

    result = (provider_name, "ok", records, note)
    _PROVIDER_CACHE[cache_key] = (time.time(), result)
    return result


async def check_breachdirectory(*args, **kwargs) -> Any:
    """Check BreachDirectory (supports both check_breachdirectory(email) and legacy _check_breachdirectory(target, email, on_find))."""
    if args and isinstance(args[0], Target):
        target: Target = args[0]
        email: str = args[1] if len(args) > 1 else kwargs.get("email", "")
        on_find = args[2] if len(args) > 2 else kwargs.get("on_find")
        provider_name, status, records, note = await _check_breachdirectory_impl(email)
        target.notes.append(note)
        if status == "skipped":
            target.log("tool_skipped", {"tool": "breachdirectory", "reason": "no key configured"})
        breaches: List[Breach] = []
        for r in records:
            b = Breach(
                name=r.get("name", "Unknown"),
                date=r.get("date", "Unknown"),
                exposed_fields=r.get("data_types", []),
                source="breachdirectory",
                record_count=r.get("record_count"),
            )
            if target.add_breach(b):
                breaches.append(b)
                if on_find:
                    await on_find(b)
        return breaches

    email = args[0] if args else kwargs.get("email", "")
    return await _check_breachdirectory_impl(email)

_check_breachdirectory = check_breachdirectory


# Modular registry of breach provider functions
BREACH_PROVIDERS = [
    check_xposedornot,
    check_hudsonrock,
    check_breachdirectory,
]


async def _check_holehe(target: Target, email: str, on_find=None) -> None:
    """Run holehe CLI to check email on 120+ sites."""
    holehe_bin = find_tool("holehe")
    if not holehe_bin:
        note = "holehe not found on PATH or in ~/.local/bin, skipped"
        logger.warning(f"[email_recon] {note}")
        target.notes.append(note)
        target.log("tool_skipped", {"tool": "holehe", "reason": "not installed"})
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            holehe_bin, email, "--only-used", "--no-color",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=build_subprocess_env(),
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode(errors="ignore").splitlines():
            line_str = line.strip()
            # Ignore holehe summary/legend line e.g. "[+] Email used, [-] Email not used, [x] Rate limit"
            if "[+]" in line_str and "email used" not in line_str.lower() and "rate limit" not in line_str.lower():
                # Extract site name
                parts = line_str.split("[+]")[-1].strip().split()
                if parts:
                    site = parts[0]
                    entity = Entity(
                        entity_type="email",
                        value=email,
                        sources=["holehe"],
                        confidence=0.85,
                        platform=site,
                        metadata={
                            "registered": True,
                            "status": "possible registration (unverified)",
                            "registration_status": "possible registration (unverified)",
                        },
                    )
                    if target.add_entity(entity) and on_find:
                        await on_find(entity)

                    # Follow-up profile enrichment if holehe detects Gravatar registration
                    if "gravatar" in site.lower():
                        try:
                            import modules.gravatar_profile as gravatar_profile
                            await gravatar_profile.enrich(target, email, on_find)
                        except Exception as e:
                            logger.warning(f"[email_recon] Gravatar profile enrichment error: {e}")
    except FileNotFoundError:
        note = "holehe not found on PATH or in ~/.local/bin, skipped"
        logger.warning(f"[email_recon] {note}")
        target.notes.append(note)
    except Exception as e:
        note = f"holehe check failed: {e}"
        logger.warning(f"[email_recon] {note}")
        target.notes.append(note)


async def _check_gravatar(target: Target, email: str, on_find=None) -> None:
    """Check if email has a Gravatar — fetches public profile, name, location, links, avatar, screenshot."""
    try:
        import modules.gravatar_profile as gravatar_profile
        await gravatar_profile.enrich(target, email, on_find)
    except Exception as e:
        logger.warning(f"[email_recon] Gravatar check error: {e}")


async def run(target: Target, email: str, on_find=None) -> None:
    # 1. Run breach providers
    provider_tasks = [provider(email) for provider in BREACH_PROVIDERS]
    provider_results = await asyncio.gather(*provider_tasks, return_exceptions=True)

    for res in provider_results:
        if isinstance(res, Exception):
            logger.warning(f"[email_recon] Breach provider error: {res}")
            continue
        if not (isinstance(res, tuple) and len(res) == 4):
            continue

        provider_name, status, records, note = res
        if note:
            target.notes.append(note)
            logger.info(f"[email_recon] {note}")

        if status == "skipped":
            target.log("tool_skipped", {"tool": provider_name.lower(), "reason": note})
        elif status == "rate_limited":
            target.log("tool_rate_limited", {"tool": provider_name.lower(), "reason": note})
        elif status == "error":
            target.log("tool_failed", {"tool": provider_name.lower(), "reason": note})

        for r in records:
            # Ensure no credentials/passwords exist
            r.pop("password", None)
            r.pop("passwords", None)
            r.pop("top_passwords", None)
            r.pop("hash", None)
            r.pop("hashes", None)

            breach = Breach(
                name=r.get("name", "Unknown"),
                date=r.get("date", "Unknown"),
                exposed_fields=r.get("data_types", []),
                source=provider_name,
                record_count=r.get("record_count"),
            )
            # Deduplicate by breach name across providers
            if target.add_breach(breach) and on_find:
                await on_find(breach)

    # 2. Run holehe and gravatar checks
    await asyncio.gather(
        _check_holehe(target, email, on_find),
        _check_gravatar(target, email, on_find),
    )

    # 3. Run multi-technique email intelligence enrichment (Gravatar, Libravatar, Google Maps dork)
    try:
        import modules.email_intel as email_intel
        await email_intel.enrich(target, email, on_find)
    except Exception as e:
        logger.warning(f"[email_recon] Email intel enrichment error: {e}")
