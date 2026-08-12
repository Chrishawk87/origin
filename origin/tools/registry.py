"""Tool registry — the single place the agent looks up every capability."""

from __future__ import annotations

from typing import Any, Dict, List

from ..config import Config
from ..memory import MemoryStore, build_memory_tools
from ..orchestra import WorkerPool
from ..research import ResearchEngine
from ..roles import resolve_persona
from .base import Tool
from .browser import BrowserManager, build_browser_tools
from .mcp_client import MCPManager
from .media import build_media_tools
from .models import build_model_tools
from .research_tools import build_research_tools
from .rest import build_rest_tools
from .shell import build_shell_tools
from .web import build_web_tools, fetch_url, run_search


class Registry:
    def __init__(self, config: Config):
        self.config = config
        self.tools: Dict[str, Tool] = {}
        self.mcp = MCPManager(config.mcp_servers)
        self.pool = WorkerPool(config.workers)
        self.browser = BrowserManager(config.web)
        web_cfg = config.web
        self.research_engine = ResearchEngine(
            config.data.get("research", {}),
            search_fn=lambda q, n: run_search(q, n, web_cfg),
            fetch_fn=fetch_url,
            ask_fn=None,
        )
        self.memory = MemoryStore()
        self._persona_setter = None   # set by Engine/CLI to let `become` retune the agent
        allow = config.agent.get("tool_allow")
        deny = config.agent.get("tool_deny")
        self.allow: set[str] | None = set(allow) if allow else None
        self.deny: set[str] = set(deny) if deny else set()

    def bootstrap(self) -> None:
        # 1. local shell / OS
        for t in build_shell_tools(timeout=int(self.config.agent.get("shell_timeout", 300))):
            self.tools[t.name] = t
        # 2. generic REST APIs
        for t in build_rest_tools(self.config.rest_apis):
            self.tools[t.name] = t
        # 3. MCP servers (may be a no-op)
        self.mcp.start()
        for t in self.mcp.build_tools():
            self.tools[t.name] = t
        # 4. model-to-model tools (consult / collaborate across workers)
        for t in build_model_tools(self.pool, self.config.orchestrator):
            self.tools[t.name] = t
        # 5. web tools (search / fetch) — token-free, any worker can use
        for t in build_web_tools(self.config.web):
            self.tools[t.name] = t
        # 6. browser click-and-retrieve (if Playwright available + enabled)
        if self.config.web.get("browser", True):
            for t in build_browser_tools(self.browser):
                self.tools[t.name] = t
        # 7. research + self-updating knowledge
        for t in build_research_tools(self.research_engine):
            self.tools[t.name] = t
        # 8. memory (gets better over time)
        for t in build_memory_tools(self.memory):
            self.tools[t.name] = t
        # 8b. media generation (image now)
        for t in build_media_tools(self.config):
            self.tools[t.name] = t
        # 9. become-any-expert (self-specialization)
        self.tools["become"] = Tool(
            name="become",
            description=(
                "Transform Origin into a world-class expert in ANY domain to do the current task "
                "at the highest level (e.g. 'growth marketing', 'Meta ad buying', 'iOS design', "
                "'options trading', 'supply chain'). Pass the expertise you need."
            ),
            input_schema={
                "type": "object",
                "properties": {"expertise": {"type": "string"}},
                "required": ["expertise"],
            },
            handler=self._become,
            source="expert",
        )

    def _become(self, args) -> str:
        expertise = (args or {}).get("expertise", "").strip()
        if not expertise:
            return "ERROR: 'expertise' is required."
        persona = resolve_persona(expertise)
        if self._persona_setter:
            self._persona_setter(persona)
            return f"Origin is now operating as a world-class expert in {expertise}."
        return f"(persona composed for {expertise}, but no agent is bound to apply it)"

    def set_persona_setter(self, fn) -> None:
        self._persona_setter = fn

    def set_research_brain(self, provider) -> None:
        """Point the research engine's synthesis at a given LLM provider."""
        def ask(prompt: str, system: str = "") -> str:
            msgs = []
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.append({"role": "user", "content": prompt})
            try:
                return provider.complete(msgs, []).text or ""
            except Exception as e:
                return f"(synthesis error: {e})"
        self.research_engine.set_brain(ask)

    def _permitted(self, name: str) -> bool:
        if name in self.deny:
            return False
        if self.allow is not None and name not in self.allow:
            return False
        return True

    def set_filter(self, allow: set[str] | None = None, deny: set[str] | None = None) -> None:
        """Control which tools the agent may see/use (the 'what' you allow)."""
        self.allow = allow
        self.deny = deny or set()

    def deny_tool(self, name: str) -> None:
        self.deny.add(name)

    def allow_tool(self, name: str) -> None:
        self.deny.discard(name)
        if self.allow is not None:
            self.allow.add(name)

    def schemas(self) -> List[Dict[str, Any]]:
        return [t.schema() for t in self.tools.values() if self._permitted(t.name)]

    def execute(self, name: str, arguments: Dict[str, Any]) -> str:
        tool = self.tools.get(name)
        if tool is None:
            return f"ERROR: unknown tool '{name}'."
        if not self._permitted(name):
            return f"ERROR: tool '{name}' is disabled by the current profile/filter."
        try:
            return tool.run(arguments)
        except Exception as e:
            return f"ERROR while executing '{name}': {e}"

    def by_source(self) -> Dict[str, List[str]]:
        grouped: Dict[str, List[str]] = {}
        for t in self.tools.values():
            grouped.setdefault(t.source, []).append(t.name)
        return grouped

    def shutdown(self) -> None:
        self.mcp.stop()
        self.browser.stop()
