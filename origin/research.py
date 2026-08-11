"""Research engine — Origin's live, self-updating knowledge layer.

Why this makes Origin "grounded in today's world": it answers by searching the
web *at query time* and synthesizing from real sources, rather than reciting a
frozen model memory. Every finding is stored with a timestamp and its sources,
so when you ask the same thing later Origin can tell whether the info is stale,
re-gather it, and report *what changed since last time*.

Design (dependency-injected so it's testable without network/keys):
    ResearchEngine(cfg, search_fn, fetch_fn, ask_fn)
      search_fn(query, n) -> [{title, url, snippet}]
      fetch_fn(url)       -> readable text
      ask_fn(prompt, system) -> synthesized text (the LLM brain)

Storage: SQLite at ~/.origin/knowledge.db
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DB_PATH = Path.home() / ".origin" / "knowledge.db"
DAY = 86400.0


def _key(question: str) -> str:
    norm = re.sub(r"\s+", " ", question.strip().lower())
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def _fmt_age(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < DAY:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // DAY)}d ago"


class KnowledgeStore:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS knowledge(
                key TEXT PRIMARY KEY, question TEXT, answer TEXT, sources TEXT,
                fetched_at REAL, ttl REAL, watch INTEGER DEFAULT 0
            )"""
        )
        self._db.commit()

    def get(self, question: str) -> Optional[Dict[str, Any]]:
        cur = self._db.execute(
            "SELECT question, answer, sources, fetched_at, ttl, watch FROM knowledge WHERE key=?",
            (_key(question),),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "question": row[0], "answer": row[1], "sources": json.loads(row[2] or "[]"),
            "fetched_at": row[3], "ttl": row[4], "watch": bool(row[5]),
        }

    def put(self, question: str, answer: str, sources: List[str], ttl: float, watch: bool = False) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO knowledge(key, question, answer, sources, fetched_at, ttl, watch) "
            "VALUES(?,?,?,?,?,?,?)",
            (_key(question), question, answer, json.dumps(sources), time.time(), ttl, int(watch)),
        )
        self._db.commit()

    def set_watch(self, question: str, on: bool) -> bool:
        cur = self._db.execute("UPDATE knowledge SET watch=? WHERE key=?", (int(on), _key(question)))
        self._db.commit()
        return cur.rowcount > 0

    def list(self, only_watch: bool = False) -> List[Dict[str, Any]]:
        q = "SELECT question, fetched_at, ttl, watch FROM knowledge"
        if only_watch:
            q += " WHERE watch=1"
        q += " ORDER BY fetched_at DESC"
        out = []
        for r in self._db.execute(q):
            out.append({"question": r[0], "fetched_at": r[1], "ttl": r[2], "watch": bool(r[3])})
        return out


class ResearchEngine:
    def __init__(
        self,
        cfg: Dict[str, Any],
        search_fn: Callable[[str, int], List[Dict[str, str]]],
        fetch_fn: Callable[[str], str],
        ask_fn: Optional[Callable[[str, str], str]] = None,
        store: Optional[KnowledgeStore] = None,
    ):
        self.cfg = cfg or {}
        self.search_fn = search_fn
        self.fetch_fn = fetch_fn
        self.ask_fn = ask_fn
        self.store = store or KnowledgeStore()
        self.default_ttl = float(self.cfg.get("default_ttl_hours", 24)) * 3600

    def set_brain(self, ask_fn: Callable[[str, str], str]) -> None:
        self.ask_fn = ask_fn

    # ── core ────────────────────────────────────────────────────────────────
    def _gather(self, question: str, breadth: int, depth: int) -> Dict[str, Any]:
        """Search several angles, read the top pages, synthesize with sources."""
        queries = [question]
        # a couple of angle variations to widen coverage
        queries += [f"{question} latest", f"{question} 2026"]
        seen, results = set(), []
        for q in queries[:max(1, breadth)]:
            try:
                for r in self.search_fn(q, 6):
                    u = r.get("url")
                    if u and u not in seen:
                        seen.add(u)
                        results.append(r)
            except Exception:
                continue
        top = results[: max(1, depth)]
        docs = []
        for r in top:
            try:
                text = self.fetch_fn(r["url"])[:6000]
            except Exception:
                text = r.get("snippet", "")
            docs.append(f"SOURCE: {r.get('title','')} ({r['url']})\n{text}")
        sources = [r["url"] for r in top]
        context = "\n\n---\n\n".join(docs) if docs else "\n".join(
            f"{r.get('title','')} — {r.get('url','')}: {r.get('snippet','')}" for r in results[:8]
        )
        if not self.ask_fn:
            # no LLM: return a raw digest of sources
            answer = "Sources found (no synthesis brain set):\n" + "\n".join(f"- {s}" for s in sources)
            return {"answer": answer, "sources": sources}
        prompt = (
            f"Question: {question}\n\n"
            f"Below are current web sources gathered just now. Using ONLY them, write an "
            f"accurate, up-to-date answer. Cite sources inline as [n]. If sources conflict or "
            f"are thin, say so. End with a one-line 'Confidence:' note.\n\nSOURCES:\n{context}"
        )
        answer = self.ask_fn(prompt, "You are a rigorous research analyst. Ground every claim in the provided sources.")
        return {"answer": answer, "sources": sources}

    def research(self, question: str, max_age_hours: Optional[float] = None,
                 force: bool = False, breadth: int = 2, depth: int = 4) -> str:
        ttl = self.default_ttl if max_age_hours is None else max_age_hours * 3600
        cached = self.store.get(question)
        now = time.time()

        if cached and not force and (now - cached["fetched_at"]) < ttl:
            age = _fmt_age(now - cached["fetched_at"])
            src = "\n".join(f"- {s}" for s in cached["sources"])
            return (f"(cached, gathered {age})\n\n{cached['answer']}\n\nSources:\n{src}"
                    f"\n\n[Ask again or say 'refresh' to re-check for changes.]")

        fresh = self._gather(question, breadth, depth)
        note = ""
        if cached:
            changed = _materially_different(cached["answer"], fresh["answer"])
            if changed:
                note = (f"\n\n🔄 Updated since {_fmt_age(now - cached['fetched_at'])}: the information "
                        f"changed compared to what I had before.")
            else:
                note = f"\n\n✓ Re-checked; no material change since {_fmt_age(now - cached['fetched_at'])}."
        self.store.put(question, fresh["answer"], fresh["sources"], ttl,
                       watch=cached["watch"] if cached else False)
        src = "\n".join(f"- {s}" for s in fresh["sources"])
        return f"(fresh, gathered just now){note}\n\n{fresh['answer']}\n\nSources:\n{src}"

    def deep_research(self, question: str) -> str:
        """Wider, deeper pass for hard questions."""
        return self.research(question, force=True, breadth=3, depth=8)

    def watch(self, question: str, on: bool = True) -> str:
        if not self.store.get(question):
            # research it once so there's something to watch
            self.research(question)
        self.store.set_watch(question, on)
        return f"{'Now watching' if on else 'Stopped watching'}: {question} (auto-refreshes daily)."

    def recall(self) -> str:
        items = self.store.list()
        if not items:
            return "Nothing researched yet."
        now = time.time()
        lines = ["Known topics (most recent first):"]
        for it in items:
            w = " ⭐watch" if it["watch"] else ""
            lines.append(f"- {it['question']}  ·  {_fmt_age(now - it['fetched_at'])}{w}")
        return "\n".join(lines)

    def refresh_watches(self) -> int:
        n = 0
        for it in self.store.list(only_watch=True):
            try:
                self.research(it["question"], force=True)
                n += 1
            except Exception:
                continue
        return n


def _materially_different(a: str, b: str) -> bool:
    """Cheap change detector: compare the set of numbers/entities-ish tokens."""
    def sig(t: str):
        nums = set(re.findall(r"\d[\d,\.]*", t or ""))
        words = set(re.findall(r"[A-Za-z]{5,}", (t or "").lower()))
        return nums, words
    na, wa = sig(a)
    nb, wb = sig(b)
    if na != nb:
        return True
    if not wa or not wb:
        return a.strip() != b.strip()
    overlap = len(wa & wb) / max(1, len(wa | wb))
    return overlap < 0.6
