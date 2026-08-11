"""Web connector — search + fetch, usable by ANY worker (local included).

These are TOOLS, not model intelligence: the tool does the searching and
reading, the model just decides when to call it. They cost no LLM tokens.
The default search backend (DuckDuckGo HTML) needs no API key at all.

Backends (config: web.search_backend):
  ddg      — DuckDuckGo HTML endpoint, no key (default)
  tavily   — Tavily API (needs TAVILY_API_KEY)
  brave    — Brave Search API (needs BRAVE_API_KEY)
  searxng  — a self-hosted SearXNG instance (needs web.searxng_url)
"""

from __future__ import annotations

import html
import json
import os
import re
from typing import Any, Dict, List

from .base import Tool

_MAX_OUTPUT = 30_000


def _truncate(text: str, limit: int = _MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def _html_to_text(raw: str) -> str:
    """Best-effort readable-text extraction. Uses trafilatura/readability if
    installed, else a dependency-free tag stripper."""
    try:
        import trafilatura  # type: ignore
        extracted = trafilatura.extract(raw, include_links=False)
        if extracted:
            return extracted
    except Exception:
        pass
    # dependency-free fallback
    raw = re.sub(r"(?is)<(script|style|noscript|head).*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?is)</(p|div|li|h[1-6]|tr)>", "\n", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# ── search backends ───────────────────────────────────────────────────────
def _search_ddg(query: str, n: int) -> List[Dict[str, str]]:
    import requests

    resp = requests.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": "Mozilla/5.0 (compatible; Origin/1.0)"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.text
    results: List[Dict[str, str]] = []
    # each result: <a class="result__a" href="URL">TITLE</a> ... snippet
    for m in re.finditer(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.S
    ):
        url = html.unescape(m.group(1))
        title = _html_to_text(m.group(2))
        results.append({"title": title, "url": url, "snippet": ""})
        if len(results) >= n:
            break
    # attach snippets if present
    snippets = re.findall(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', body, re.S)
    for i, s in enumerate(snippets[: len(results)]):
        results[i]["snippet"] = _html_to_text(s)
    return results


def _search_tavily(query: str, n: int, cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    import requests

    key = os.environ.get(cfg.get("tavily_api_key_env", "TAVILY_API_KEY"))
    if not key:
        raise RuntimeError("TAVILY_API_KEY not set")
    resp = requests.post(
        "https://api.tavily.com/search",
        json={"api_key": key, "query": query, "max_results": n},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
        for r in data.get("results", [])
    ]


def _search_brave(query: str, n: int, cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    import requests

    key = os.environ.get(cfg.get("brave_api_key_env", "BRAVE_API_KEY"))
    if not key:
        raise RuntimeError("BRAVE_API_KEY not set")
    resp = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": n},
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
        for r in data.get("web", {}).get("results", [])
    ]


def _search_searxng(query: str, n: int, cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    import requests

    base = cfg.get("searxng_url")
    if not base:
        raise RuntimeError("web.searxng_url not configured")
    resp = requests.get(
        base.rstrip("/") + "/search",
        params={"q": query, "format": "json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
        for r in data.get("results", [])[:n]
    ]


def run_search(query: str, n: int, cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    """Structured search dispatch, reused by the web_search tool AND research."""
    backend = (cfg.get("search_backend") or "ddg").lower()
    if backend == "tavily":
        return _search_tavily(query, n, cfg)
    if backend == "brave":
        return _search_brave(query, n, cfg)
    if backend == "searxng":
        return _search_searxng(query, n, cfg)
    return _search_ddg(query, n)


def fetch_url(url: str) -> str:
    """Fetch a URL and return readable text/JSON. Reused by web_fetch AND research."""
    import requests

    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Origin/1.0)"},
        timeout=45,
    )
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            return json.dumps(resp.json(), indent=2)
        except Exception:
            return resp.text
    if "text/html" in ctype or "<html" in resp.text[:2000].lower():
        return _html_to_text(resp.text)
    return resp.text


def build_web_tools(web_cfg: Dict[str, Any]) -> List[Tool]:
    web_cfg = web_cfg or {}
    if not web_cfg.get("enabled", True):
        return []
    backend = (web_cfg.get("search_backend") or "ddg").lower()

    def web_search(args: Dict[str, Any]) -> str:
        query = args.get("query", "")
        if not query:
            return "ERROR: 'query' is required."
        n = int(args.get("max_results", 6))
        try:
            results = run_search(query, n, web_cfg)
        except Exception as e:
            return f"ERROR: web_search failed ({backend}): {e}"
        if not results:
            return "No results."
        lines = [f"Search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}\n   {r['url']}")
            if r.get("snippet"):
                lines.append(f"   {r['snippet']}")
        return _truncate("\n".join(lines))

    def web_fetch(args: Dict[str, Any]) -> str:
        url = args.get("url", "")
        if not url:
            return "ERROR: 'url' is required."
        try:
            return _truncate(f"{url}\n\n{fetch_url(url)}")
        except Exception as e:
            return f"ERROR: web_fetch failed: {e}"

    return [
        Tool(
            name="web_search",
            description=(
                f"Search the web (backend: {backend}) and return ranked titles, URLs, "
                "and snippets. Token-free. Use to gather current information before acting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "description": "default 6"},
                },
                "required": ["query"],
            },
            handler=web_search,
            source="web",
        ),
        Tool(
            name="web_fetch",
            description=(
                "Fetch a URL and return its readable text (HTML stripped) or JSON. "
                "Use to read a specific page found via web_search."
            ),
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            handler=web_fetch,
            source="web",
        ),
    ]
