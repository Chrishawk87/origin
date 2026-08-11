"""Research connector — exposes the self-updating knowledge engine as tools."""

from __future__ import annotations

from typing import Any, Dict, List

from ..research import ResearchEngine
from .base import Tool


def build_research_tools(engine: ResearchEngine) -> List[Tool]:
    def research(args: Dict[str, Any]) -> str:
        q = args.get("question", "")
        if not q:
            return "ERROR: 'question' is required."
        return engine.research(
            q,
            max_age_hours=args.get("max_age_hours"),
            force=bool(args.get("refresh", False)),
        )

    def deep_research(args: Dict[str, Any]) -> str:
        q = args.get("question", "")
        if not q:
            return "ERROR: 'question' is required."
        return engine.deep_research(q)

    def watch_topic(args: Dict[str, Any]) -> str:
        q = args.get("question", "")
        if not q:
            return "ERROR: 'question' is required."
        return engine.watch(q, on=bool(args.get("on", True)))

    def recall(_a: Dict[str, Any]) -> str:
        return engine.recall()

    return [
        Tool(
            name="research",
            description=(
                "Answer a question with CURRENT information gathered live from the web, "
                "with sources. Results are cached with a timestamp; re-asking later re-checks "
                "for changes and tells you what changed. Set refresh=true to force a re-gather, "
                "or max_age_hours to control staleness."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "refresh": {"type": "boolean", "description": "Force a fresh gather even if cached."},
                    "max_age_hours": {"type": "number", "description": "Treat cache older than this as stale."},
                },
                "required": ["question"],
            },
            handler=research,
            source="research",
        ),
        Tool(
            name="deep_research",
            description="A wider, deeper research pass (more queries + more sources) for hard questions.",
            input_schema={"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
            handler=deep_research,
            source="research",
        ),
        Tool(
            name="watch_topic",
            description="Track a question so Origin auto-refreshes it daily and flags changes. Set on=false to stop.",
            input_schema={
                "type": "object",
                "properties": {"question": {"type": "string"}, "on": {"type": "boolean"}},
                "required": ["question"],
            },
            handler=watch_topic,
            source="research",
        ),
        Tool(
            name="recall",
            description="List everything Origin has researched, with how long ago and which are watched.",
            input_schema={"type": "object", "properties": {}},
            handler=recall,
            source="research",
        ),
    ]
