# modules/verify.py
"""
Content-verification layer for OSINT hits.
Re-fetches entity URLs to validate Sherlock/Maigret results and
reduce false positives. Routes JS-heavy platforms to the headless
browser module.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

import asyncio
import json
import re
import time
import urllib.parse
from pathlib import Path
import httpx
from core.target_model import Entity

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

# Verification overrides configuration (data/verify_overrides.json).
# Maps platform names or URL pattern substrings to override behaviors, e.g.:
# {
#   "LegitForum": "trusted_query_profile",
#   "index.php?action=profile": "trusted_query_profile"
# }
# Any platform or URL pattern matching "trusted_query_profile" is exempted
# from search-style URL rejection.
VERIFY_OVERRIDES_FILE = PROJECT_ROOT / "data" / "verify_overrides.json"

# Import headless probe's platform set for routing
try:
    from modules.headless_probe import JS_HEAVY_PLATFORMS, probe as headless_probe
except ImportError:
    JS_HEAVY_PLATFORMS = set()
    headless_probe = None


def get_url_pattern_key(url: str, username: str) -> str:
    """
    Extract a normalized URL pattern key: scheme, host, path with username replaced
    by {u}, and sorted query parameter names.
    """
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if username:
            path = re.sub(re.escape(username), "{u}", path, flags=re.IGNORECASE)
            path = re.sub(re.escape(urllib.parse.quote(username)), "{u}", path, flags=re.IGNORECASE)
        query_params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query_keys = sorted(k for k, _ in query_params)
        query_str = "&".join(query_keys)
        return f"{parsed.scheme}://{parsed.netloc.lower()}{path}?{query_str}"
    except Exception:
        return url


def build_canary_url(url: str, candidate_user: str, canary_user: str) -> tuple[str, bool]:
    """
    Replace every occurrence of candidate_user in url with canary_user.
    Returns (canary_url, was_replaced).
    """
    if not candidate_user or not url:
        return url, False
    new_url = re.sub(re.escape(candidate_user), canary_user, url, flags=re.IGNORECASE)
    new_url = re.sub(re.escape(urllib.parse.quote(candidate_user)), canary_user, new_url, flags=re.IGNORECASE)
    return new_url, (new_url != url)


def normalize_text_for_comparison(text: str, user: str) -> str:
    """
    Normalize text: replace username with {u}, strip scripts, styles, nonces,
    timestamps, hashes, HTML tags, and collapse whitespace.
    """
    if not text:
        return ""
    t = re.sub(re.escape(user), "{u}", text, flags=re.IGNORECASE)
    t = re.sub(re.escape(urllib.parse.quote(user)), "{u}", t, flags=re.IGNORECASE)
    t = re.sub(r"<script[^>]*>.*?</script>", " ", t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r"<style[^>]*>.*?</style>", " ", t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r'nonce=["\']?[a-zA-Z0-9+/=_-]+["\']?', ' ', t, flags=re.IGNORECASE)
    t = re.sub(r'csrf[-_]?token["\']?\s*[:=]\s*["\']?[a-zA-Z0-9+/=_-]+["\']?', ' ', t, flags=re.IGNORECASE)
    t = re.sub(r'\b[a-f0-9]{32,64}\b', ' ', t, flags=re.IGNORECASE)
    t = re.sub(r'\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b', ' ', t)
    t = re.sub(r'\b\d{10,13}\b', ' ', t)
    return re.sub(r'\s+', ' ', t).strip().lower()


def compute_jaccard_similarity(text1: str, text2: str, max_chars: int = 20000) -> float:
    """
    Compute bounded Jaccard similarity over word tokens up to max_chars.
    Fast and O(N).
    """
    tokens1 = set(text1[:max_chars].split())
    tokens2 = set(text2[:max_chars].split())
    if not tokens1 and not tokens2:
        return 1.0
    if not tokens1 or not tokens2:
        return 0.0
    intersection = len(tokens1 & tokens2)
    union = len(tokens1 | tokens2)
    return intersection / union if union > 0 else 0.0


def load_verify_overrides() -> dict:
    """Load verification overrides from data/verify_overrides.json if present."""
    if VERIFY_OVERRIDES_FILE.exists():
        try:
            with open(VERIFY_OVERRIDES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def is_search_style_url(url: str, username: str, platform: str = "") -> bool:
    """
    Check if candidate URL is a search endpoint rather than a profile page.
    Hits where the username only appears as a query parameter (e.g. q=, query=,
    search=, username=) or forum member search paths without being in the URL path segment.
    Exempts entries marked 'trusted_query_profile' in data/verify_overrides.json.
    """
    if not url or not username:
        return False

    overrides = load_verify_overrides()
    if platform and overrides.get(platform) == "trusted_query_profile":
        return False
    for pat, rule in overrides.items():
        if rule == "trusted_query_profile" and pat.lower() in url.lower():
            return False

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False

    path_lower = parsed.path.lower()
    user_lower = username.lower()
    quoted_user = urllib.parse.quote(username).lower()

    # Check if username is in path segment (e.g. /users/username or /@username)
    user_in_path = (user_lower in path_lower) or (quoted_user in path_lower)
    if user_in_path:
        return False

    # Check query parameters
    query_params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    search_keys = {"q", "query", "search", "s", "keyword", "term", "name", "username", "user", "u"}

    is_search = False
    for k, v in query_params:
        k_low = k.lower()
        v_low = v.lower()
        if v_low == user_lower or v_low == quoted_user:
            if k_low in search_keys:
                is_search = True
                break
            if "/members" in path_lower or "/search" in path_lower:
                is_search = True
                break

    if not is_search and ("/members" in path_lower or "/search" in path_lower):
        if f"username={user_lower}" in parsed.query.lower() or f"q={user_lower}" in parsed.query.lower():
            is_search = True

    return is_search


def get_site_family(platform: str) -> str:
    """
    Extract the base site family for grouping related platforms
    (e.g., 'OP.GG [LeagueOfLegends] Brazil' -> 'OP.GG [LeagueOfLegends]').
    """
    if not platform:
        return "Unknown"
    p = platform.strip()
    m = re.match(r'^(.*?\s*\[[^\]]+\])(?:\s+.*)?$', p)
    if m:
        return m.group(1).strip()
    m_reg = re.match(r'^(.*?)\s*\[[A-Za-z]{2,4}\]$', p)
    if m_reg:
        return m_reg.group(1).strip()
    return p


class CanaryTransportPool:
    """
    Polite transport pool for canary HTTP requests:
    - Global concurrency capped at max_concurrent (default 8).
    - Host-level synchronization: at most 1 in-flight request per host.
    - Host-level politeness delay: at least per_host_delay (0.25s) between requests to the same host.
    - Timeout: default 6.0s per request with up to 1 retry.
    """
    def __init__(self, max_concurrent: int = 8, per_host_delay: float = 0.25, timeout: float = 6.0):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last_time: dict[str, float] = {}
        self._per_host_delay = per_host_delay
        self._timeout = timeout

    def _get_host_lock(self, host: str) -> asyncio.Lock:
        if host not in self._host_locks:
            self._host_locks[host] = asyncio.Lock()
        return self._host_locks[host]

    async def fetch(self, url: str) -> tuple[int | None, str, str, str, Exception | None]:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.netloc or "").lower()
        host_lock = self._get_host_lock(host)

        async with self._semaphore:
            async with host_lock:
                last_time = self._host_last_time.get(host, 0.0)
                now = time.time()
                elapsed = now - last_time
                if elapsed < self._per_host_delay:
                    await asyncio.sleep(self._per_host_delay - elapsed)

                status = None
                final_url = url
                title = ""
                body = ""
                fetch_err = None

                try:
                    async with httpx.AsyncClient(
                        timeout=self._timeout,
                        headers={"User-Agent": _USER_AGENT},
                        follow_redirects=True,
                        max_redirects=5,
                    ) as client:
                        last_resp = None
                        last_exc = None
                        for attempt in range(2):
                            if attempt > 0:
                                await asyncio.sleep(0.2)
                            try:
                                r = await client.get(url)
                                if r.status_code >= 500:
                                    last_resp = r
                                    continue
                                last_resp = r
                                last_exc = None
                                break
                            except httpx.TimeoutException as exc:
                                last_exc = exc
                                continue
                            except Exception as exc:
                                last_exc = exc
                                break

                        if last_exc is not None and last_resp is None:
                            fetch_err = last_exc
                        elif last_resp is not None:
                            status = last_resp.status_code
                            final_url = str(last_resp.url)
                            body = last_resp.text[:20000]
                            t_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
                            title = t_match.group(1).strip() if t_match else ""
                        else:
                            fetch_err = RuntimeError("No response")
                except Exception as exc:
                    fetch_err = exc
                finally:
                    self._host_last_time[host] = time.time()

                return status, final_url, title, body, fetch_err


# ── Not-found signals (case-insensitive) ──────────────────────
_NOT_FOUND_STRINGS = [
    "doesn't exist",
    "does not exist",
    "user not found",
    "page not found",
    "this account doesn't exist",
    "this account does not exist",
    "sorry, this page",
    "this page isn't available",
    "content isn't available",
    "no results found",
    "couldn't find this account",
    "the page you were looking for",
    "hmm...this page doesn't exist",
    "nothing to see here",
    "account suspended",
    "profile isn't available",
    "the link may be broken",
    "profile may have been removed",
]

_NOT_FOUND_RE = re.compile(
    "|".join(re.escape(s) for s in _NOT_FOUND_STRINGS),
    re.IGNORECASE,
)

# Generic site titles that don't confirm a real profile
_GENERIC_TITLES = {
    "instagram", "tiktok", "facebook", "log in", "twitter",
    "snapchat", "linkedin", "x", "page not found", "404",
    "error", "not found", "reddit", "threads",
}

# Realistic browser User-Agent
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


async def verify_hit(entity: Entity, lessons_store=None, case_slug: str = None, canary_context: dict = None) -> Entity:
    """
    Verify that an entity's URL actually points to a real profile.
    Modifies entity.confidence and entity.metadata["verified"] in place.

    Args:
        entity:         The Entity to verify (must have metadata["url"]).
        lessons_store:  Optional LessonsStore instance for RAG-based
                        prior confidence adjustment.
        case_slug:      The slug name of the active case folder.
        canary_context: Optional dictionary holding canary cache, counts,
                        and timing stats across the investigation run.

    Returns:
        The same entity, with updated confidence and verified flag.
    """
    # ── Fix 2: API-confirmed targets should bypass verification checks ──
    api_confirmed_sources = {"github", "github_commits"}
    if any(src in api_confirmed_sources for src in entity.sources):
        entity.metadata["verified"] = True
        entity.metadata["verification_method"] = "api"
        entity.metadata["verification_reason"] = "api confirmed"
        # Capture screenshot for API confirmed source if a URL and case_slug exist
        url = entity.metadata.get("url", "")
        if url and case_slug:
            try:
                import modules.evidence as evidence
                entity_id = entity.entity_id
                cap_res = await evidence.capture(url, case_slug, entity_id)
                if cap_res.get("success"):
                    shot = cap_res.get("screenshot_url") or cap_res.get("screenshot_path")
                    entity.metadata["screenshot_path"] = shot
                    entity.metadata["screenshot_url"] = shot
            except Exception:
                pass
        return entity

    url = entity.metadata.get("url", "")
    if not url:
        # No URL to verify — leave as-is
        entity.metadata.setdefault("verification_method", "none")
        entity.metadata.setdefault("verification_reason", "no url to verify")
        return entity

    platform = (entity.platform or "").strip()

    # ── RAG prior: check past lessons before even fetching ─────
    if lessons_store and platform:
        try:
            warning = lessons_store.has_platform_warning(platform)
            if warning:
                # Past lesson says this platform is unreliable
                entity.confidence *= 0.5
                entity.metadata["lesson_warning"] = warning
        except Exception:
            pass  # Lessons store failure is non-fatal

    # ── Route: JS-heavy platforms → headless probe ─────────────
    # Make check case-insensitive to ensure matches like "Instagram" align with JS_HEAVY_PLATFORMS
    from modules.headless_probe import is_playwright_available
    pw_ok = is_playwright_available()
    is_js_heavy = platform.lower() in JS_HEAVY_PLATFORMS
    if is_js_heavy and headless_probe is not None and pw_ok:
        print(f"[verify] Routing '{platform}' via headless_probe for '{entity.value}'")
        entity = await _verify_via_headless(entity, url, platform, case_slug)
        method = "browser"
    else:
        # ── Route: standard HTTP verification ──────────────────────
        method = "http_no_browser" if is_js_heavy else "http"
        print(f"[verify] Routing '{platform}' via standard httpx fetch for '{entity.value}' (method={method})")
        entity = await _verify_via_http(entity, url, platform, method=method)

    # ── Canary verification layer (detects soft-404 false positives) ─
    if canary_context is not None and method != "browser":
        entity = await _apply_canary_check(entity, url, platform, method, canary_context, case_slug)

    # ── Capture screenshot if verified, case_slug provided, and Playwright available ──
    if entity.metadata.get("verified") is True and case_slug and pw_ok:
        try:
            import modules.evidence as evidence
            entity_id = entity.entity_id
            cap_res = await evidence.capture(url, case_slug, entity_id)
            if cap_res.get("success"):
                shot = cap_res.get("screenshot_url") or cap_res.get("screenshot_path")
                entity.metadata["screenshot_path"] = shot
                entity.metadata["screenshot_url"] = shot
        except Exception:
            pass

    return entity


async def _verify_via_headless(entity: Entity, url: str, platform: str, case_slug: str = None) -> Entity:
    """Verify via Playwright headless browser."""
    entity.metadata["verification_method"] = "browser"
    try:
        entity_id = entity.entity_id
        if case_slug:
            evidence_dir = Path(__file__).parent.parent / "cases" / case_slug / "evidence"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = str(evidence_dir / f"{entity_id}.png")
        else:
            screenshot_path = None

        result = await headless_probe(url, platform, screenshot_path=screenshot_path)

        if result["exists"]:
            entity.metadata["verified"] = True
            entity.metadata["verification_reason"] = "browser confirmed"
            entity.metadata["page_title"] = result.get("title", "")
            if result.get("og_description"):
                entity.metadata["description"] = result.get("og_description")
            if screenshot_path:
                shot = f"/cases/{case_slug}/evidence/{entity_id}.png"
                entity.metadata["screenshot_path"] = shot
                entity.metadata["screenshot_url"] = shot
        else:
            entity.metadata["verified"] = False
            entity.metadata["verification_reason"] = "not found in browser"
            entity.confidence *= 0.3
            if screenshot_path:
                try:
                    Path(screenshot_path).unlink(missing_ok=True)
                except Exception:
                    pass

        entity.metadata["_cand_status"] = 200 if result.get("exists") else 404
        entity.metadata["_cand_final_url"] = result.get("url", url)
        entity.metadata["_cand_title"] = result.get("title", "")
        entity.metadata["_cand_body"] = (result.get("title", "") + " " + (result.get("og_description") or "") + " " + result.get("body", ""))

    except Exception:
        # Headless probe crashed — inconclusive
        entity.metadata["verified"] = None
        entity.metadata["verification_reason"] = "browser probe failed"
        entity.confidence *= 0.6

    return entity


async def _verify_via_http(entity: Entity, url: str, platform: str, method: str = "http") -> Entity:
    """Verify via direct HTTP fetch + HTML content analysis."""
    entity.metadata["verification_method"] = method
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
            max_redirects=5,
        ) as client:
            last_resp = None
            last_exc = None
            for attempt in range(2):
                if attempt > 0:
                    await asyncio.sleep(0.5)
                try:
                    r = await client.get(url)
                    if r.status_code >= 500:
                        last_resp = r
                        continue
                    last_resp = r
                    last_exc = None
                    break
                except httpx.TimeoutException as exc:
                    last_exc = exc
                    continue
                except Exception as exc:
                    last_exc = exc
                    break

            if last_exc is not None:
                entity.metadata["verified"] = None
                if isinstance(last_exc, httpx.TimeoutException):
                    entity.metadata["verification_reason"] = "timeout after retry"
                    entity.confidence *= 0.7
                else:
                    entity.metadata["verification_reason"] = "fetch error after retry"
                    entity.confidence *= 0.6
                return entity

            if last_resp is None:
                entity.metadata["verified"] = None
                entity.metadata["verification_reason"] = "no response"
                entity.confidence *= 0.6
                return entity

            r = last_resp

            # 5xx after retry
            if r.status_code >= 500:
                entity.metadata["verified"] = None
                entity.metadata["verification_reason"] = "http 5xx after retry"
                entity.confidence *= 0.6
                return entity

            entity.metadata["_cand_status"] = r.status_code
            entity.metadata["_cand_final_url"] = str(r.url)
            entity.metadata["_cand_body"] = r.text[:20000]

            # Hard 404/410 — clear signal
            if r.status_code in (404, 410):
                entity.metadata["verified"] = False
                entity.metadata["verification_reason"] = f"http {r.status_code}"
                entity.confidence *= 0.2
                return entity

            # Other Non-200 — inconclusive
            if r.status_code != 200:
                entity.metadata["verified"] = None
                entity.metadata["verification_reason"] = f"http {r.status_code}"
                entity.confidence *= 0.6
                return entity

            html = entity.metadata["_cand_body"]
            html_lower = html.lower()

            # Check for not-found signals in body
            has_not_found = bool(_NOT_FOUND_RE.search(html_lower))

            # Check for positive profile signals
            has_og_title = 'property="og:title"' in html_lower or "property='og:title'" in html_lower
            has_og_image = 'property="og:image"' in html_lower or "property='og:image'" in html_lower

            # Extract <title> for generic check
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
            page_title = title_match.group(1).strip() if title_match else ""
            is_generic_title = page_title.lower().strip() in _GENERIC_TITLES
            entity.metadata["_cand_title"] = page_title

            has_profile_signal = (
                (has_og_title or has_og_image)
                and not is_generic_title
            )

            # Extract og:description
            desc_match = re.search(r'property=["\']og:description["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE)
            if not desc_match:
                desc_match = re.search(r'content=["\'](.*?)["\']\s+property=["\']og:description["\']', html, re.IGNORECASE)
            og_desc = desc_match.group(1).strip() if desc_match else ""

            if has_not_found:
                entity.metadata["verified"] = False
                entity.metadata["verification_reason"] = "not-found text matched"
                entity.confidence *= 0.3
            elif method == "http_no_browser":
                # Honest handling for JS-heavy platforms checked with http_no_browser:
                # Clear 404 or not-found text may still reject, but must NEVER be verified=True.
                entity.metadata["verified"] = None
                entity.metadata["verification_reason"] = "needs browser check"
                if page_title:
                    entity.metadata["page_title"] = page_title
                if og_desc:
                    entity.metadata["description"] = og_desc
            elif has_profile_signal:
                entity.metadata["verified"] = True
                entity.metadata["verification_reason"] = "profile metadata present"
                entity.metadata["page_title"] = page_title
                if og_desc:
                    entity.metadata["description"] = og_desc
            elif not is_generic_title and page_title:
                # Has a non-generic title but no og tags — cautiously accept
                entity.metadata["verified"] = True
                entity.metadata["verification_reason"] = "profile title matched"
                entity.metadata["page_title"] = page_title
                if og_desc:
                    entity.metadata["description"] = og_desc
            elif is_generic_title:
                entity.metadata["verified"] = None
                entity.metadata["verification_reason"] = "generic or login title"
                entity.confidence *= 0.6
                if page_title:
                    entity.metadata["page_title"] = page_title
            else:
                # Inconclusive — no clear signals either way
                entity.metadata["verified"] = None
                entity.metadata["verification_reason"] = "inconclusive"
                entity.confidence *= 0.6

    except Exception:
        # Any other fetch error — inconclusive
        entity.metadata["verified"] = None
        entity.metadata.setdefault("verification_method", method)
        entity.metadata.setdefault("verification_reason", "fetch error")
        entity.confidence *= 0.6

    return entity


async def _apply_canary_check(
    entity: Entity,
    url: str,
    platform: str,
    method: str,
    canary_context: dict,
    case_slug: str = None,
) -> Entity:
    """
    Compare candidate profile response with canary responses on the same platform
    for random nonexistent usernames to detect soft 404s and reduce false positives.
    Uses two independently generated canaries to establish an empirical noise floor.
    """
    # Pop candidate response details cached by verification
    cand_status = entity.metadata.pop("_cand_status", 200)
    cand_final_url = entity.metadata.pop("_cand_final_url", url)
    cand_title = entity.metadata.pop("_cand_title", entity.metadata.get("page_title", ""))
    cand_body = entity.metadata.pop("_cand_body", "")

    # Only run canary check if hit is verified OR uncertain with status 200
    was_verified = (entity.metadata.get("verified") is True)
    was_uncertain = (entity.metadata.get("verified") is None and cand_status in (200, 403, 429))

    # Never upgrade a hit already rejected or uncertain for network/5xx reasons
    if not (was_verified or was_uncertain):
        return entity

    # Canary context setup
    canary_context.setdefault("canary_cache", {})
    canary_context.setdefault("canary_usernames", {})
    canary_context.setdefault("family_verdicts", {})
    canary_context.setdefault("canary_requests_count", 0)
    canary_context.setdefault("browser_rechecks_count", 0)
    canary_context.setdefault("browser_recheck_platforms", set())
    canary_context.setdefault("extra_time_seconds", 0.0)
    pool = canary_context.setdefault("transport_pool", CanaryTransportPool())

    family = get_site_family(platform)

    # Sibling family verdict reuse (e.g. OP.GG regions)
    if family in canary_context["family_verdicts"]:
        fam_cached = canary_context["family_verdicts"][family]
        entity.metadata["verified"] = fam_cached.get("verified")
        entity.metadata["verification_reason"] = fam_cached.get("verification_reason")
        entity.metadata["canary_checked"] = fam_cached.get("canary_checked", False)
        entity.metadata["canary_status"] = fam_cached.get("canary_status", "none")
        if "confidence_mult" in fam_cached:
            entity.confidence *= fam_cached["confidence_mult"]
        return entity

    # Search-style URL detection: query-based profile URLs are queries, not profiles
    if is_search_style_url(url, entity.value, platform):
        entity.metadata["verified"] = None
        entity.metadata["verification_reason"] = "search-style URL, cannot confirm an account exists"
        entity.metadata["canary_checked"] = False
        entity.metadata["canary_status"] = "search_style"
        canary_context["family_verdicts"][family] = {
            "verified": None,
            "verification_reason": "search-style URL, cannot confirm an account exists",
            "canary_status": "search_style",
            "canary_checked": False,
        }
        return entity

    clean_plat = (platform or "unknown").strip().lower()
    if clean_plat not in canary_context["canary_usernames"]:
        from core.calibration import generate_random_username
        u1 = generate_random_username()
        u2 = generate_random_username()
        while u2 == u1:
            u2 = generate_random_username()
        canary_context["canary_usernames"][clean_plat] = (u1, u2)
    canary_user_1, canary_user_2 = canary_context["canary_usernames"][clean_plat]

    # Build canary URLs
    canary_url_1, was_replaced_1 = build_canary_url(url, entity.value, canary_user_1)
    canary_url_2, was_replaced_2 = build_canary_url(url, entity.value, canary_user_2)
    if not was_replaced_1 or not was_replaced_2:
        entity.metadata["canary_checked"] = False
        entity.metadata["canary_status"] = "canary_skipped"
        return entity

    pattern_key = get_url_pattern_key(url, entity.value)

    # Check cache
    if pattern_key in canary_context["canary_cache"]:
        cached = canary_context["canary_cache"][pattern_key]
        if isinstance(cached, tuple) and len(cached) == 2:
            canary_data_1, canary_data_2 = cached
        else:
            canary_data_1 = cached
            canary_data_2 = cached
    else:
        t0 = time.time()
        if method == "browser" and headless_probe is not None:
            try:
                h1, h2 = await asyncio.gather(
                    headless_probe(canary_url_1, platform, screenshot_path=None),
                    headless_probe(canary_url_2, platform, screenshot_path=None),
                )
                canary_data_1 = {
                    "status": 200 if h1.get("exists") else 404,
                    "final_url": h1.get("url", canary_url_1),
                    "title": h1.get("title", ""),
                    "body": (h1.get("title", "") + " " + (h1.get("og_description") or "") + " " + h1.get("body", "")),
                    "fetch_err": None,
                }
                canary_data_2 = {
                    "status": 200 if h2.get("exists") else 404,
                    "final_url": h2.get("url", canary_url_2),
                    "title": h2.get("title", ""),
                    "body": (h2.get("title", "") + " " + (h2.get("og_description") or "") + " " + h2.get("body", "")),
                    "fetch_err": None,
                }
            except Exception as exc:
                canary_data_1 = {"fetch_err": exc, "status": None}
                canary_data_2 = {"fetch_err": exc, "status": None}
        else:
            res1, res2 = await asyncio.gather(
                pool.fetch(canary_url_1),
                pool.fetch(canary_url_2),
            )
            c1_status, c1_url, c1_title, c1_body, c1_err = res1
            c2_status, c2_url, c2_title, c2_body, c2_err = res2

            canary_data_1 = {
                "status": c1_status,
                "final_url": c1_url,
                "title": c1_title,
                "body": c1_body,
                "fetch_err": c1_err,
            }
            canary_data_2 = {
                "status": c2_status,
                "final_url": c2_url,
                "title": c2_title,
                "body": c2_body,
                "fetch_err": c2_err,
            }

        duration = time.time() - t0
        canary_context["canary_requests_count"] += 2
        canary_context["extra_time_seconds"] += duration
        canary_context["canary_cache"][pattern_key] = (canary_data_1, canary_data_2)

    # Handle fetch failure
    if (
        canary_data_1.get("fetch_err") is not None
        or canary_data_2.get("fetch_err") is not None
        or canary_data_1.get("status") is None
        or canary_data_2.get("status") is None
    ):
        entity.metadata["canary_checked"] = True
        entity.metadata["canary_url"] = canary_url_1
        entity.metadata["canary_url_1"] = canary_url_1
        entity.metadata["canary_url_2"] = canary_url_2
        entity.metadata["canary_status"] = "canary_unavailable"
        return entity

    c1_status = canary_data_1["status"]
    c2_status = canary_data_2["status"]

    cand_norm_url = normalize_text_for_comparison(cand_final_url, entity.value)
    c1_norm_url = normalize_text_for_comparison(canary_data_1["final_url"], canary_user_1)
    c2_norm_url = normalize_text_for_comparison(canary_data_2["final_url"], canary_user_2)

    cand_norm_title = normalize_text_for_comparison(cand_title, entity.value)
    c1_norm_title = normalize_text_for_comparison(canary_data_1["title"], canary_user_1)
    c2_norm_title = normalize_text_for_comparison(canary_data_2["title"], canary_user_2)

    cand_norm_text = normalize_text_for_comparison(cand_body, entity.value)
    c1_norm_text = normalize_text_for_comparison(canary_data_1["body"], canary_user_1)
    c2_norm_text = normalize_text_for_comparison(canary_data_2["body"], canary_user_2)

    cand_text_len = len(cand_norm_text)
    c1_text_len = len(c1_norm_text)
    c2_text_len = len(c2_norm_text)

    # 1. Blocked or rate limited check
    c1_blocked = (c1_status in (403, 429) or (500 <= c1_status < 600) or "captcha" in c1_norm_text)
    c2_blocked = (c2_status in (403, 429) or (500 <= c2_status < 600) or "captcha" in c2_norm_text)
    cand_blocked = (cand_status in (403, 429) or (500 <= cand_status < 600) or "captcha" in cand_norm_text)

    if cand_blocked:
        entity.metadata["verified"] = None
        entity.metadata["verification_reason"] = "canary inconclusive (blocked)"
        entity.metadata["canary_checked"] = True
        entity.metadata["canary_url"] = canary_url_1
        entity.metadata["canary_url_1"] = canary_url_1
        entity.metadata["canary_url_2"] = canary_url_2
        entity.metadata["canary_status"] = "inconclusive"
        return entity

    if (c1_blocked or c2_blocked) and cand_status == 200:
        entity.metadata["verified"] = None
        entity.metadata["verification_reason"] = "canary blocked or rate limited"
        entity.metadata["canary_checked"] = True
        entity.metadata["canary_url"] = canary_url_1
        entity.metadata["canary_url_1"] = canary_url_1
        entity.metadata["canary_url_2"] = canary_url_2
        entity.metadata["canary_status_1"] = c1_status
        entity.metadata["canary_status_2"] = c2_status
        entity.metadata["canary_status"] = "canary_blocked"
        canary_context["family_verdicts"][family] = {
            "verified": None,
            "verification_reason": "canary blocked or rate limited",
            "canary_status": "canary_blocked",
            "canary_checked": True,
        }
        return entity

    # 2. Compute noise floor
    noise_floor = compute_jaccard_similarity(c1_norm_text, c2_norm_text, max_chars=20000)

    # If the site serves wildly different content for two nonexistent names
    # or one canary 404s while the other 200s, site is too volatile
    if (noise_floor < 0.9) or (c1_status != c2_status):
        entity.metadata["verified"] = None
        entity.metadata["verification_reason"] = "pages vary for nonexistent usernames (cannot confirm)"
        entity.metadata["canary_checked"] = True
        entity.metadata["canary_url"] = canary_url_1
        entity.metadata["canary_url_1"] = canary_url_1
        entity.metadata["canary_url_2"] = canary_url_2
        entity.metadata["canary_status_1"] = c1_status
        entity.metadata["canary_status_2"] = c2_status
        entity.metadata["canary_noise_floor"] = round(noise_floor, 3)
        entity.metadata["canary_status"] = "noisy_canaries"
        canary_context["family_verdicts"][family] = {
            "verified": None,
            "verification_reason": "pages vary for nonexistent usernames (cannot confirm)",
            "canary_status": "noisy_canaries",
            "canary_checked": True,
        }
        return entity

    # 3. Both canaries consistently indicate not-found (404/410 or not-found phrase)
    c1_not_found = (c1_status in (404, 410)) or bool(_NOT_FOUND_RE.search(canary_data_1["body"].lower()))
    c2_not_found = (c2_status in (404, 410)) or bool(_NOT_FOUND_RE.search(canary_data_2["body"].lower()))
    c1_redirected = (
        cand_norm_url != c1_norm_url
        and any(c1_norm_url.rstrip("/").endswith(p) for p in ("/login", "/home", "/search", "/404", "/error", "/members"))
    )

    if (c1_not_found and c2_not_found) or c1_redirected:
        if was_verified:
            entity.metadata["verified"] = True
            entity.metadata["verification_reason"] = "differs from nonexistent-user page"
        entity.metadata["canary_checked"] = True
        entity.metadata["canary_url"] = canary_url_1
        entity.metadata["canary_url_1"] = canary_url_1
        entity.metadata["canary_url_2"] = canary_url_2
        entity.metadata["canary_status_1"] = c1_status
        entity.metadata["canary_status_2"] = c2_status
        entity.metadata["canary_noise_floor"] = round(noise_floor, 3)
        entity.metadata["canary_status"] = "differs"
        return entity

    # 4. Consistent canaries (noise_floor >= 0.9): compare candidate to canaries
    sim1 = compute_jaccard_similarity(cand_norm_text, c1_norm_text, max_chars=20000)
    sim2 = compute_jaccard_similarity(cand_norm_text, c2_norm_text, max_chars=20000)
    cand_sim = max(sim1, sim2)

    entity.metadata["canary_checked"] = True
    entity.metadata["canary_url"] = canary_url_1
    entity.metadata["canary_url_1"] = canary_url_1
    entity.metadata["canary_url_2"] = canary_url_2
    entity.metadata["canary_status_1"] = c1_status
    entity.metadata["canary_status_2"] = c2_status
    entity.metadata["canary_noise_floor"] = round(noise_floor, 3)
    entity.metadata["canary_cand_similarity"] = round(cand_sim, 3)

    # Candidate must be noticeably more different than the canaries are from each other
    is_soft_404 = (cand_sim > (noise_floor - 0.10))

    if is_soft_404:
        if cand_text_len >= 500:
            entity.metadata["verified"] = False
            entity.metadata["verification_reason"] = "same page as a nonexistent username (soft 404)"
            entity.metadata["canary_status"] = "soft_404"
            entity.metadata["canary_similarity"] = round(cand_sim, 3)
            entity.confidence *= 0.2
            canary_context["family_verdicts"][family] = {
                "verified": False,
                "verification_reason": "same page as a nonexistent username (soft 404)",
                "canary_status": "soft_404",
                "canary_checked": True,
                "confidence_mult": 0.2,
            }
            return entity
        else:
            # SPA shell with sparse visible text (< 500 chars)
            from modules.headless_probe import is_playwright_available, probe as probe_fn
            pw_ok = is_playwright_available()
            rechecks = canary_context.get("browser_rechecks_count", 0)

            if pw_ok and rechecks < 20 and probe_fn is not None:
                t_recheck = time.time()
                try:
                    cand_rendered = await probe_fn(url, platform, screenshot_path=None)
                    c1_rendered = await probe_fn(canary_url_1, platform, screenshot_path=None)
                    c2_rendered = await probe_fn(canary_url_2, platform, screenshot_path=None)
                    canary_context["browser_rechecks_count"] = rechecks + 1
                    canary_context.setdefault("browser_recheck_platforms", set()).add(platform)
                    canary_context["extra_time_seconds"] += (time.time() - t_recheck)

                    cand_rend_text = normalize_text_for_comparison(
                        (cand_rendered.get("title", "") + " " + (cand_rendered.get("og_description") or "") + " " + cand_rendered.get("body", "")),
                        entity.value
                    )
                    c1_rend_text = normalize_text_for_comparison(
                        (c1_rendered.get("title", "") + " " + (c1_rendered.get("og_description") or "") + " " + c1_rendered.get("body", "")),
                        canary_user_1
                    )
                    c2_rend_text = normalize_text_for_comparison(
                        (c2_rendered.get("title", "") + " " + (c2_rendered.get("og_description") or "") + " " + c2_rendered.get("body", "")),
                        canary_user_2
                    )

                    rend_noise_floor = compute_jaccard_similarity(c1_rend_text, c2_rend_text)

                    # If rendered canary pages vary significantly (like Bibsonomy tag clouds/feeds)
                    if rend_noise_floor < 0.9 or (c1_rendered.get("exists") != c2_rendered.get("exists")):
                        entity.metadata["verified"] = None
                        entity.metadata["verification_reason"] = "pages vary for nonexistent usernames (cannot confirm)"
                        entity.metadata["canary_status"] = "noisy_canaries"
                        entity.metadata["canary_noise_floor"] = round(rend_noise_floor, 3)
                        canary_context["family_verdicts"][family] = {
                            "verified": None,
                            "verification_reason": "pages vary for nonexistent usernames (cannot confirm)",
                            "canary_status": "noisy_canaries",
                            "canary_checked": True,
                        }
                        return entity

                    rend_sim1 = compute_jaccard_similarity(cand_rend_text, c1_rend_text)
                    rend_sim2 = compute_jaccard_similarity(cand_rend_text, c2_rend_text)
                    rend_cand_sim = max(rend_sim1, rend_sim2)
                    entity.metadata["canary_cand_similarity"] = round(rend_cand_sim, 3)
                    entity.metadata["canary_noise_floor"] = round(rend_noise_floor, 3)

                    rend_diff = (
                        (cand_rendered.get("exists") is True and c1_rendered.get("exists") is False)
                        or (rend_cand_sim <= rend_noise_floor - 0.10 and abs(len(cand_rend_text) - len(c1_rend_text)) > 50)
                    )

                    if rend_diff:
                        if was_verified:
                            entity.metadata["verified"] = True
                            entity.metadata["verification_method"] = "browser"
                            entity.metadata["verification_reason"] = "differs from nonexistent-user page (rendered)"
                        entity.metadata["canary_status"] = "differs_rendered"
                    else:
                        entity.metadata["verified"] = False
                        entity.metadata["verification_reason"] = "same page as a nonexistent username (soft 404)"
                        entity.metadata["canary_status"] = "soft_404"
                        entity.confidence *= 0.2
                        canary_context["family_verdicts"][family] = {
                            "verified": False,
                            "verification_reason": "same page as a nonexistent username (soft 404)",
                            "canary_status": "soft_404",
                            "canary_checked": True,
                            "confidence_mult": 0.2,
                        }
                except Exception:
                    entity.metadata["verified"] = None
                    entity.metadata["verification_reason"] = "cannot distinguish from a nonexistent username without a browser"
                    entity.metadata["canary_status"] = "uncertain_no_browser"
                    entity.confidence *= 0.6
                return entity
            else:
                entity.metadata["verified"] = None
                entity.metadata["verification_reason"] = "cannot distinguish from a nonexistent username without a browser"
                entity.metadata["canary_status"] = "uncertain_no_browser"
                entity.confidence *= 0.6
                return entity

    # Content genuinely differs
    if was_verified:
        entity.metadata["verified"] = True
        entity.metadata["verification_reason"] = "differs from nonexistent-user page"
    entity.metadata["canary_status"] = "differs"
    entity.metadata["canary_similarity"] = round(cand_sim, 3)
    return entity
