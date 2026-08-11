"""Browser connector — real click-and-retrieve via Playwright.

Gives any worker (local included) a live browser: open a page, read its text,
list its links, click elements, type into fields, and screenshot. The browser
is launched lazily on first use and kept alive across calls so the agent can
navigate a site step by step. Costs no LLM tokens.

Requires Playwright + a browser:
    pip install playwright && playwright install chromium
If Playwright isn't installed, these tools simply aren't registered (with a
note), so the rest of the hub keeps working.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from .base import Tool

_MAX_OUTPUT = 30_000


def _truncate(text: str, limit: int = _MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


class BrowserManager:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}
        self._pw = None
        self._browser = None
        self._page = None
        self.available = self._probe()

    def _probe(self) -> bool:
        try:
            import playwright  # noqa: F401
            return True
        except ImportError:
            return False

    def _ensure_page(self):
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        launch_kwargs: Dict[str, Any] = {"headless": self.cfg.get("headless", True)}
        exe = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH") or self.cfg.get("executable_path")
        if exe:
            launch_kwargs["executablePath"] = exe
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        self._page = self._browser.new_page()
        return self._page

    # --- actions -----------------------------------------------------------
    def open(self, url: str, wait: str = "domcontentloaded") -> str:
        page = self._ensure_page()
        page.goto(url, wait_until=wait, timeout=self.cfg.get("timeout_ms", 45000))
        return f"opened {page.url}\ntitle: {page.title()}\n\n{self._page_text()}"

    def read(self) -> str:
        self._ensure_page()
        return self._page_text()

    def _page_text(self) -> str:
        try:
            text = self._page.inner_text("body")
        except Exception:
            text = self._page.content()
        return _truncate(re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip())

    def links(self, limit: int = 60) -> str:
        page = self._ensure_page()
        anchors = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({text: e.innerText.trim(), href: e.href}))",
        )
        out = []
        seen = set()
        for a in anchors:
            href = a.get("href")
            if not href or href in seen:
                continue
            seen.add(href)
            label = (a.get("text") or "").strip().replace("\n", " ")[:80]
            out.append(f"- {label or '(no text)'} → {href}")
            if len(out) >= limit:
                break
        return _truncate("\n".join(out) or "(no links)")

    def click(self, selector: Optional[str], text: Optional[str]) -> str:
        page = self._ensure_page()
        if text and not selector:
            page.get_by_text(text, exact=False).first.click(timeout=self.cfg.get("timeout_ms", 45000))
        elif selector:
            page.click(selector, timeout=self.cfg.get("timeout_ms", 45000))
        else:
            return "ERROR: provide 'selector' or 'text' to click."
        page.wait_for_load_state("domcontentloaded")
        return f"clicked; now at {page.url}\ntitle: {page.title()}\n\n{self._page_text()}"

    def type_text(self, selector: str, value: str, submit: bool) -> str:
        page = self._ensure_page()
        page.fill(selector, value, timeout=self.cfg.get("timeout_ms", 45000))
        if submit:
            page.press(selector, "Enter")
            page.wait_for_load_state("domcontentloaded")
        return f"typed into {selector}; now at {page.url}\n\n{self._page_text()}"

    def screenshot(self, path: str) -> str:
        page = self._ensure_page()
        page.screenshot(path=path, full_page=True)
        return f"saved screenshot to {path}"

    def stop(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._page = self._browser = self._pw = None


def build_browser_tools(manager: BrowserManager) -> List[Tool]:
    if not manager.available:
        return []

    def guard(fn):
        def wrapped(args: Dict[str, Any]) -> str:
            try:
                return fn(args)
            except Exception as e:
                return f"ERROR: {e}"
        return wrapped

    return [
        Tool(
            name="browse",
            description="Open a URL in a real browser and return the page's title and readable text. Starts a session you can then click/read within.",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            handler=guard(lambda a: manager.open(a.get("url", ""))),
            source="browser",
        ),
        Tool(
            name="browser_read",
            description="Return the readable text of the current browser page.",
            input_schema={"type": "object", "properties": {}},
            handler=guard(lambda a: manager.read()),
            source="browser",
        ),
        Tool(
            name="browser_links",
            description="List the links (text → href) on the current page, to decide what to click next.",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "default 60"}},
            },
            handler=guard(lambda a: manager.links(int(a.get("limit", 60)))),
            source="browser",
        ),
        Tool(
            name="browser_click",
            description="Click an element by CSS 'selector' or visible 'text', then return the resulting page text (click-and-retrieve).",
            input_schema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
            handler=guard(lambda a: manager.click(a.get("selector"), a.get("text"))),
            source="browser",
        ),
        Tool(
            name="browser_type",
            description="Type text into an input (CSS 'selector'); set submit=true to press Enter (e.g. to run a search box).",
            input_schema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                    "submit": {"type": "boolean"},
                },
                "required": ["selector", "value"],
            },
            handler=guard(lambda a: manager.type_text(a.get("selector", ""), a.get("value", ""), bool(a.get("submit", False)))),
            source="browser",
        ),
        Tool(
            name="browser_screenshot",
            description="Save a full-page screenshot of the current page to a local path.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=guard(lambda a: manager.screenshot(a.get("path", "screenshot.png"))),
            source="browser",
        ),
    ]
