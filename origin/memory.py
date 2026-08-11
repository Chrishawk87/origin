"""Persistent memory — how Origin gets better the more you use it.

Origin can't retrain its model weights, but it CAN accumulate durable knowledge
about you and your work: your preferences, your goals, facts it has learned, and
lessons from what worked or didn't. Before each task it pulls the most relevant
memories into context; as it works, it writes new ones. Over time it becomes
increasingly tailored and effective — learning by accumulation.

Storage: SQLite at ~/.origin/memory.db
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

MEM_DB = Path.home() / ".origin" / "memory.db"
KINDS = ("preference", "fact", "goal", "lesson", "profile", "result")


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-zA-Z0-9]{4,}", (text or "").lower()))


class MemoryStore:
    def __init__(self, path: Path = MEM_DB):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS memory(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, content TEXT, tags TEXT,
                importance REAL DEFAULT 0.5, created REAL, uses INTEGER DEFAULT 0
            )"""
        )
        self._db.commit()

    def add(self, content: str, kind: str = "fact", tags: str = "", importance: float = 0.6) -> int:
        if kind not in KINDS:
            kind = "fact"
        cur = self._db.execute(
            "INSERT INTO memory(kind, content, tags, importance, created) VALUES(?,?,?,?,?)",
            (kind, content.strip(), tags, float(importance), time.time()),
        )
        self._db.commit()
        return cur.lastrowid

    def all(self) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT id, kind, content, tags, importance, created, uses FROM memory ORDER BY created DESC"
        )
        return [
            {"id": r[0], "kind": r[1], "content": r[2], "tags": r[3],
             "importance": r[4], "created": r[5], "uses": r[6]}
            for r in rows
        ]

    def forget(self, mem_id: int) -> bool:
        cur = self._db.execute("DELETE FROM memory WHERE id=?", (mem_id,))
        self._db.commit()
        return cur.rowcount > 0

    def retrieve(self, query: str, k: int = 6) -> List[Dict[str, Any]]:
        items = self.all()
        if not items:
            return []
        qt = _tokens(query)
        now = time.time()
        scored = []
        for m in items:
            mt = _tokens(m["content"] + " " + (m["tags"] or ""))
            overlap = len(qt & mt)
            recency = max(0.0, 1.0 - (now - m["created"]) / (60 * 86400))  # decays over ~60 days
            score = overlap * 1.0 + m["importance"] * 0.8 + recency * 0.3
            # goals/preferences/profile are always somewhat relevant
            if m["kind"] in ("goal", "preference", "profile"):
                score += 0.5
            scored.append((score, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [m for s, m in scored if s > 0][:k]
        for m in top:
            self._db.execute("UPDATE memory SET uses=uses+1 WHERE id=?", (m["id"],))
        self._db.commit()
        return top

    def context_block(self, query: str, k: int = 6) -> str:
        top = self.retrieve(query, k)
        if not top:
            return ""
        lines = ["What Origin remembers that's relevant here:"]
        for m in top:
            lines.append(f"- ({m['kind']}) {m['content']}")
        return "\n".join(lines)

    def summary(self) -> str:
        items = self.all()
        if not items:
            return "No memories yet."
        by_kind: Dict[str, int] = {}
        for m in items:
            by_kind[m["kind"]] = by_kind.get(m["kind"], 0) + 1
        head = ", ".join(f"{k}:{v}" for k, v in by_kind.items())
        lines = [f"{len(items)} memories ({head}). Most recent:"]
        for m in items[:12]:
            lines.append(f"  [{m['id']}] ({m['kind']}) {m['content'][:90]}")
        return "\n".join(lines)


def build_memory_tools(store: MemoryStore):
    from .tools.base import Tool

    def remember(args: Dict[str, Any]) -> str:
        content = args.get("content", "")
        if not content:
            return "ERROR: 'content' is required."
        mid = store.add(content, kind=args.get("kind", "fact"),
                        tags=args.get("tags", ""), importance=float(args.get("importance", 0.6)))
        return f"Saved to memory (#{mid}, {args.get('kind','fact')})."

    def recall_memory(args: Dict[str, Any]) -> str:
        top = store.retrieve(args.get("query", ""), int(args.get("k", 8)))
        if not top:
            return "No relevant memories."
        return "\n".join(f"[{m['id']}] ({m['kind']}) {m['content']}" for m in top)

    def list_memory(_a: Dict[str, Any]) -> str:
        return store.summary()

    def forget_memory(args: Dict[str, Any]) -> str:
        ok = store.forget(int(args.get("id", -1)))
        return "Forgotten." if ok else "No such memory id."

    return [
        Tool(
            name="remember",
            description=("Save a durable memory so Origin gets better over time. Use for the user's "
                         "preferences, goals, stable facts, and lessons learned. kind ∈ "
                         "preference|fact|goal|lesson|profile|result."),
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "tags": {"type": "string"},
                    "importance": {"type": "number", "description": "0..1"},
                },
                "required": ["content"],
            },
            handler=remember,
            source="memory",
        ),
        Tool(
            name="recall_memory",
            description="Search Origin's long-term memory for relevant preferences/facts/goals/lessons.",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            handler=recall_memory,
            source="memory",
        ),
        Tool(
            name="list_memory",
            description="Summarize everything Origin remembers.",
            input_schema={"type": "object", "properties": {}},
            handler=list_memory,
            source="memory",
        ),
        Tool(
            name="forget_memory",
            description="Delete a memory by its id.",
            input_schema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
            handler=forget_memory,
            source="memory",
        ),
    ]
