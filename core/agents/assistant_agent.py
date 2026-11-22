# core/agents/assistant_agent.py
"""AssistantAgent -- general knowledge tools folded in from the legacy cognitive loop.

The cognitive loop (core/jarvis_reasoning_loop.py) used to offer web search and
memory recall next to its tactical tools, which meant two plausible tools for
one job. They live here now, in the agents' catalogue; the cognitive loop keeps
only the tactical / OSINT namespace.

Tools (category / risk):
  read  web_search     -- live web results (DuckDuckGo via SystemSkillEngine)
  read  fetch_page     -- the readable text of one web page (from a search result
                          or a link the operator gave)
  read  memory_recall  -- what JARVIS remembers: the operator's rules, habits,
                          lessons and logged notes (data/jarvis_memory.json),
                          searched by the words asked about

Web pages and remembered text are outside content: both results are untrusted.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from html import unescape
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from core.agents.base_agent import AgentResult, BaseAgent, RiskTier, ToolSpec

_WEB_RE = re.compile(r"\b(?:search\s+(?:the\s+)?(?:web|internet|online)|web\s+search|google(?:\s+search)?|"
                     r"look\s+(?:it\s+)?up\s+online|on\s+the\s+(?:web|internet)|online|latest\s+news|news\s+(?:about|on))\b", re.I)
_MEMORY_RE = re.compile(r"\b(?:do\s+you\s+remember|what\s+do\s+you\s+(?:remember|know\s+about\s+me)|did\s+i\s+(?:tell|say|ask)|"
                        r"remind\s+me\s+what\s+i\s+(?:said|told)|my\s+rules?|what\s+did\s+i\s+(?:say|tell\s+you))\b", re.I)
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "about", "what", "did", "do", "you", "i", "me",
         "my", "remember", "say", "said", "tell", "told", "is", "was", "that", "it", "search", "web", "online", "google",
         "please", "can", "could", "would", "look", "up", "find"}


class AssistantAgent(BaseAgent):
    name = "assistant"
    description = ("general knowledge: search the WEB for news, facts, people and websites (not the operator's own files), "
                   "and recall what JARVIS remembers from earlier conversations (the operator's rules, habits, notes)")
    default_tier = RiskTier.READ
    # Narrow phrases only: "google chrome" is an app and "is it online" isn't a search.
    route_keywords = ("google search", "google it", "google for", "web search", "search the web", "search online",
                      "look it up online", "latest news", "news about", "do you remember", "did i tell you",
                      "what did i say")
    # "search the web" is not a file search, whatever the word "search" suggests.
    route_strong_keywords = ("search the web", "search online", "web search", "google search", "google for",
                             "on the internet", "do you remember", "did i tell you", "what did i say")
    route_strong_overrides = ("local_research", "system_ops")

    # ── Providers (overridable / mockable) ─────────────────────
    def _live_search(self, query: str) -> Tuple[str, List[Dict[str, Any]]]:
        """Keyless web results: the app's DuckDuckGo path first, Bing's results page when
        DuckDuckGo answers with a bot check (it does, from some networks) or nothing."""
        from core.system_skills import SystemSkillEngine
        summary, results = SystemSkillEngine().perform_live_search(query)
        if any(r.get("snippet") and not str(r.get("title", "")).startswith("Google Search:") for r in results or []):
            return summary, results
        try:
            bing = _bing_search(query)
        except Exception:
            bing = []
        return ("Bing results", bing) if bing else (summary, results)

    def _http_get(self, url: str) -> Tuple[int, str, str, str]:
        """(status, final url, content type, body text). Blocking; follows redirects, checking each hop."""
        import httpx
        current = url
        with httpx.Client(timeout=15.0, follow_redirects=False,
                          headers={"User-Agent": "Mozilla/5.0 (JARVIS page reader)"}) as http:
            for _ in range(5):
                problem = _unsafe_url(current)
                if problem:
                    raise PermissionError(problem)
                with http.stream("GET", current) as resp:
                    if resp.is_redirect:
                        current = str(resp.url.join(resp.headers.get("location", "")))
                        continue
                    ctype = resp.headers.get("content-type", "")
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > _MAX_PAGE_BYTES:
                            break
                    body = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
                    return resp.status_code, current, ctype, body
        raise ValueError("too many redirects")

    @staticmethod
    def _recall_long_term(query: str, private: bool = False) -> List[Dict[str, Any]]:
        """The unified memory (facts, preferences, past runs, lessons...), each with where it came from."""
        from memory.core.agent_memory import agent_memory, enabled
        if not enabled():
            return []
        mem = agent_memory("assistant")
        if query:
            recs = mem.recall(query, k=6, explicit=private)
        else:
            recs = mem.mm.repo.find(type=list(mem.profile.types), limit=6, order="updated")
        return [{"kind": r.category if r.type == "semantic" else r.type, "text": r.content,
                 "source": r.source.type, "verification": r.verification_status, "when": r.updated_at}
                for r in recs if mem.allowed(r, explicit=private)]

    @staticmethod
    def _forget_target(about: str):
        from memory.core.memory_manager import get_memory
        mm = get_memory()
        ent = mm.retriever.resolver.resolve(about)
        return mm, ({"entity": ent["name"]} if ent else {"text": about})

    async def memory_forget_preview(self, args: dict, ctx: dict) -> AgentResult:
        about = str(args.get("about") or "").strip()
        if len(about) < 3:
            return AgentResult(status="failed", summary="What should I forget? Name the person, project or thing.")
        from memory.maintenance.memory_cleanup import preview_forget
        mm, target = await asyncio.to_thread(self._forget_target, about)
        found = await asyncio.to_thread(preview_forget, mm, **target)
        if not found["memory_count"] and not found["messages"]:
            return AgentResult(status="failed", summary=f"I don't remember anything about {about}.")
        sample = "; ".join(found["memories"][:3])
        return AgentResult(status="done", data=found,
                           summary=f"Forget everything about {about}? That's {found['memory_count']} "
                                   f"memor{'ies' if found['memory_count'] != 1 else 'y'} and {found['messages']} "
                                   f"conversation line{'s' if found['messages'] != 1 else ''}"
                                   + (f", including: {sample}" if sample else "") + ". This can't be undone.")

    async def memory_forget(self, args: dict, ctx: dict) -> AgentResult:
        """Runs only after the operator said yes (DESTRUCTIVE)."""
        about = str(args.get("about") or "").strip()
        from memory.maintenance.memory_cleanup import forget_everywhere
        mm, target = await asyncio.to_thread(self._forget_target, about)
        rep = await asyncio.to_thread(lambda: forget_everywhere(mm, purge_backups=bool(args.get("include_backups")),
                                                                **target))
        return AgentResult(status="done", data=rep,
                           summary=f"Forgotten: {rep['memories']} memories and {rep['messages']} conversation lines "
                                   f"about {about}" + (", backups included" if rep["backups_removed"] else "") + ".")

    def _memory(self) -> Dict[str, Any]:
        from core.jarvis_memory import JarvisMemory
        return JarvisMemory().load_memory()

    # ── Catalogue ──────────────────────────────────────────────
    def tools(self) -> List[ToolSpec]:
        R = RiskTier.READ
        return [
            ToolSpec("web_search",
                     "Search the web for news, facts, people, products and websites. Then read the most relevant "
                     "result with fetch_page if the snippets don't answer it; don't repeat the search with small "
                     "rewordings. Not for the operator's own files "
                     "(search_files) or their email (check_inbox). Not for finding someone's online accounts or a "
                     "domain's infrastructure: that is the tactical OSINT console, outside these tools.",
                     {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                     "read", R, self.web_search, untrusted_output=True, progress="Searching the web"),
            ToolSpec("fetch_page",
                     "Read the main text of one web page: a URL from web_search results or one the operator gave. "
                     "Use after web_search when the snippets aren't enough.",
                     {"type": "object", "properties": {"url": {"type": "string"},
                                                       "max_chars": {"type": "integer", "description": "Default 6000"}},
                      "required": ["url"]},
                     "read", R, self.fetch_page, untrusted_output=True, progress="Reading the page"),
            ToolSpec("memory_forget",
                     "Permanently forget everything JARVIS remembers about a person, project or thing the operator "
                     "names: memories, their history, conversation lines and search indexes. Only when the operator "
                     "asks to forget or delete something from memory. include_backups also clears old backups.",
                     {"type": "object", "properties": {"about": {"type": "string"},
                                                       "include_backups": {"type": "boolean"}}, "required": ["about"]},
                     "action", RiskTier.DESTRUCTIVE, self.memory_forget, preview=self.memory_forget_preview,
                     grounded_args=("about",), progress="Forgetting"),
            ToolSpec("memory_recall",
                     "Look up what JARVIS remembers about the operator: their stated rules and phrase meanings, "
                     "habits, lessons learned and logged notes. Not their files or email.",
                     {"type": "object", "properties": {"query": {"type": "string", "description": "What to recall; "
                                                                                                    "empty = the most recent"}}},
                     "read", R, self.memory_recall, untrusted_output=True, progress="Checking my memory"),
        ]

    # ── Fallback entry point (no planning model) ───────────────
    async def run(self, goal: str, context: dict) -> AgentResult:
        text = (goal or "").strip()
        if _MEMORY_RE.search(text):
            return await self.memory_recall({"query": text}, {})
        query = re.sub(r"^\s*(?:please\s+)?(?:(?:can|could)\s+you\s+)?(?:google|search\s+(?:the\s+)?(?:web|internet|online)|"
                       r"web\s+search|look\s+up)\s+(?:for\s+|about\s+)?", "", text, flags=re.I).strip(" ?.!")
        query = re.sub(r"\s+(?:online|on\s+the\s+(?:web|internet))\s*$", "", query, flags=re.I).strip()
        return await self.web_search({"query": query or text}, {})

    # ── Tools ──────────────────────────────────────────────────
    async def web_search(self, args: dict, ctx: dict) -> AgentResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return AgentResult(status="failed", summary="What should I search the web for?")
        try:
            summary, results = await asyncio.wait_for(asyncio.to_thread(self._live_search, query), 15)
        except Exception as exc:
            return AgentResult(status="failed", summary=f"The web search failed ({type(exc).__name__}).")
        real = [r for r in results or [] if r.get("snippet") and not str(r.get("title", "")).startswith("Google Search:")]
        if not real:
            return AgentResult(status="failed", summary=f"The web search for '{query}' came back empty; the search "
                                                        "service may be unreachable.")
        self.hud({"type": "web_results", "query": query, "results": real[:6]})
        top = "; ".join(f"{r['title']}" for r in real[:3])
        return AgentResult(status="done", summary=f"{len(real)} web results for '{query}'. Top: {top}",
                           data=[{"title": r.get("title"), "snippet": str(r.get("snippet") or "")[:220], "url": r.get("url")}
                                 for r in real[:5]])

    async def fetch_page(self, args: dict, ctx: dict) -> AgentResult:
        url = str(args.get("url") or "").strip()
        problem = _unsafe_url(url, resolve=False)
        if problem:
            return AgentResult(status="failed", summary=problem)
        prov = ctx.get("provenance")
        if prov is not None and not any(prov.seen_literal(u) for u in {url, url.rstrip("/"), url.split("#")[0]})                 and not _named_site_without_data(url, prov):
            # A URL nobody wrote: the model assembled it (and could have packed data into it).
            return AgentResult(status="failed", summary="That link didn't come from you or from a search result, so "
                                                        "I won't open it. Search first, or give me the link.")
        try:
            status, final, ctype, body = await asyncio.wait_for(asyncio.to_thread(self._http_get, url), 20)
        except PermissionError as exc:
            return AgentResult(status="failed", summary=str(exc))
        except Exception as exc:
            return AgentResult(status="failed", summary=f"Couldn't load the page ({type(exc).__name__}).")
        if status >= 400:
            return AgentResult(status="failed", summary=f"The site answered HTTP {status}.")
        if ctype and not any(t in ctype for t in ("html", "text", "xml", "json")):
            return AgentResult(status="failed", summary=f"That link is a {ctype.split(';')[0]} file, not a page I can read.")
        is_html = "html" in ctype or "<html" in body[:500].lower()
        title, text = extract_readable(body) if is_html else ("", body)
        limit = max(500, min(20000, int(args.get("max_chars") or 6000)))
        if not text.strip():
            return AgentResult(status="failed", summary="The page had no readable text (it may need JavaScript).")
        cut = len(text) > limit
        self.hud({"type": "web_page", "url": final, "title": title, "excerpt": text[:3000]})
        return AgentResult(status="done", summary=f"Read '{title or urlparse(final).netloc}' ({len(text):,} characters"
                                                  + (f", first {limit:,} kept" if cut else "") + ")",
                           data={"url": final, "title": title, "text": text[:limit]})

    async def memory_recall(self, args: dict, ctx: dict) -> AgentResult:
        query = str(args.get("query") or "").strip()
        prov = (ctx or {}).get("provenance")
        # private memories only when the operator's own words ask for them
        private = "private" in query.lower() and (prov is None or prov.trusted_mentions("private"))
        try:
            found = await asyncio.to_thread(self._recall_long_term, query, private)
        except Exception:
            found = []
        if found:
            return AgentResult(status="done", data=found,
                               summary=f"{len(found)} thing{'s' if len(found) != 1 else ''} remembered: "
                                       + "; ".join(f["text"] for f in found[:3]))
        try:
            mem = await asyncio.to_thread(self._memory)
        except Exception as exc:
            return AgentResult(status="failed", summary=f"Memory isn't available ({type(exc).__name__}).")
        entries: List[Tuple[str, str]] = []
        entries += [("rule", str(r)) for r in mem.get("custom_rules", []) or []]
        entries += [("habit", str(p)) for p in mem.get("user_speech_patterns", []) or []]
        entries += [("lesson", str(l)) for l in mem.get("failures_and_lessons", []) or []]
        entries += [(str(h.get("category") or "note"), str(h.get("entry")))
                    for h in mem.get("history_log", []) or [] if isinstance(h, dict) and h.get("entry")]
        words = [w for w in re.findall(r"[a-z0-9']{3,}", query.lower()) if w not in _STOP]
        if words:
            scored = [(sum(w in text.lower() for w in words), i, kind, text) for i, (kind, text) in enumerate(entries)]
            hits = [(kind, text) for score, i, kind, text in sorted(scored, key=lambda s: (-s[0], -s[1])) if score > 0][:6]
        else:
            hits = list(reversed(entries))[:6]
        if not hits:
            return AgentResult(status="done", data=[],
                               summary=f"I don't have anything remembered about {', '.join(words) or 'that'}.")
        return AgentResult(status="done", data=[{"kind": k, "text": t} for k, t in hits],
                           summary=f"{len(hits)} thing{'s' if len(hits) != 1 else ''} remembered"
                                   + (f" about {', '.join(words)}" if words else "") + ": " + "; ".join(t for _, t in hits[:3]))


# ═══════════════════════════════════════════════════════════════
# Page fetching helpers
# ═══════════════════════════════════════════════════════════════
_MAX_PAGE_BYTES = 2 * 1024 * 1024


def _unsafe_url(url: str, resolve: bool = True) -> Optional[str]:
    """Why a URL must not be fetched (local network, odd scheme...), or None."""
    try:
        parts = urlparse(url)
    except ValueError:
        return "That isn't a valid link."
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "Only http(s) web links can be read."
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return "I won't read pages on this computer or the local network."
    addrs: List[str] = []
    try:
        addrs = [str(ipaddress.ip_address(host))]
    except ValueError:
        if resolve:
            try:
                addrs = list({i[4][0] for i in socket.getaddrinfo(host, None)})
            except OSError:
                return "That site's address couldn't be found."
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            # e.g. 192.168.x.x routers, 169.254.169.254 cloud metadata, 127.0.0.1 local services
            return "I won't read pages on this computer or the local network."
    return None


def _named_site_without_data(url: str, prov) -> bool:
    """'Read the python.org downloads page': the operator named the site, and the URL carries
    no query string (where smuggled data would go), so a model-built link to it is fine."""
    parts = urlparse(url)
    if parts.query or len(parts.path) > 120:
        return False
    host = (parts.hostname or "").lower()
    domain = ".".join(host.split(".")[-2:]) if host.count(".") >= 1 else host
    return bool(domain) and (prov.trusted_mentions(host) or prov.trusted_mentions(domain))


_DROP_BLOCKS = re.compile(r"<(script|style|noscript|svg|nav|footer|header|aside|form|iframe|template)\b[^>]*>.*?</\1\s*>",
                          re.I | re.S)
_BREAKS = re.compile(r"<(?:br|/p|/div|/li|/h[1-6]|/tr|/section|/article)\b[^>]*>", re.I)


def extract_readable(html: str) -> Tuple[str, str]:
    """(title, main text) from an HTML page: drops scripts, styles, navigation and
    boilerplate blocks, prefers <article>/<main> when present."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", " ", unescape(m.group(1))).strip() if m else ""
    body = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    body = _DROP_BLOCKS.sub(" ", body)
    main = re.search(r"<(article|main)\b[^>]*>(.*?)</\1>", body, re.I | re.S)
    if main and len(re.sub(r"<[^>]+>", "", main.group(2)).strip()) > 400:
        body = main.group(2)
    body = _BREAKS.sub("\n", body)
    text = unescape(re.sub(r"<[^>]+>", " ", body))
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if len(line) > 1)
    return title, re.sub(r"\n{3,}", "\n\n", text).strip()


def _bing_search(query: str, limit: int = 6) -> List[Dict[str, Any]]:
    """Parse Bing's public results page (no key). Blocking."""
    import base64
    import httpx
    from urllib.parse import parse_qs, quote_plus
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/130.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.8"}
    html = httpx.get(f"https://www.bing.com/search?q={quote_plus(query)}&setlang=en", headers=headers,
                     timeout=10.0, follow_redirects=True).text
    out: List[Dict[str, Any]] = []
    for block in re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', html, re.S):
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        url = unescape(m.group(1))
        if "bing.com/ck/a" in url:  # click-tracking wrapper: the real URL is base64 in u=a1...
            u = (parse_qs(urlparse(url).query).get("u") or [""])[0]
            if u.startswith("a1"):
                try:
                    url = base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode("utf-8", "replace")
                except Exception:
                    pass
        cap = re.search(r'<p[^>]*>(.*?)</p>', block, re.S) or re.search(r'class="b_caption"[^>]*>(.*?)</div>', block, re.S)
        title = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", m.group(2)))).strip()
        snippet = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", cap.group(1)))).strip() if cap else ""
        if url.startswith("http") and title:
            out.append({"title": title, "snippet": snippet.replace("Read more", "").strip(" …"), "url": url})
        if len(out) >= limit:
            break
    return out
