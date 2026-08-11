"""MCP (Model Context Protocol) connector.

Connects to any number of MCP servers over stdio, discovers their tools, and
exposes each as a hub Tool named `mcp__<server>__<tool>`. Sessions are kept
alive for the lifetime of the REPL on a dedicated asyncio event loop running
in a background thread, so the synchronous agent loop can call into them.

If the optional `mcp` package is not installed, or no servers are configured,
this connector simply contributes no tools (with a friendly note).
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import AsyncExitStack
from typing import Any, Dict, List

from .base import Tool

_MAX_OUTPUT = 30_000


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT] + f"\n...[{len(text) - _MAX_OUTPUT} chars truncated]..."


class MCPManager:
    def __init__(self, servers_config: Dict[str, Any]):
        self.servers_config = servers_config or {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.sessions: Dict[str, Any] = {}
        self.tool_defs: Dict[str, List[Any]] = {}
        self._stack: AsyncExitStack | None = None
        self.errors: Dict[str, str] = {}
        self.available = True

    # --- lifecycle ---------------------------------------------------------
    def _run_loop(self) -> None:
        assert self.loop is not None
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> None:
        enabled = {
            name: cfg
            for name, cfg in self.servers_config.items()
            if (cfg or {}).get("enabled", True)
        }
        if not enabled:
            self.available = False
            return

        try:
            import mcp  # noqa: F401
        except ImportError:
            self.available = False
            self.errors["*"] = (
                "The 'mcp' package is not installed; MCP servers were skipped. "
                "Install with: pip install mcp"
            )
            return

        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

        fut = asyncio.run_coroutine_threadsafe(self._connect_all(enabled), self.loop)
        try:
            fut.result(timeout=90)
        except Exception as e:  # pragma: no cover
            self.errors["*"] = f"MCP connection error: {e}"

    async def _connect_all(self, enabled: Dict[str, Any]) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stack = AsyncExitStack()
        for name, cfg in enabled.items():
            try:
                params = StdioServerParameters(
                    command=cfg["command"],
                    args=cfg.get("args", []),
                    env=cfg.get("env") or None,
                )
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listing = await session.list_tools()
                self.sessions[name] = session
                self.tool_defs[name] = listing.tools
            except Exception as e:
                self.errors[name] = str(e)

    def stop(self) -> None:
        if self._stack is not None and self.loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._stack.aclose(), self.loop)
                fut.result(timeout=10)
            except Exception:
                pass
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)

    # --- calling -----------------------------------------------------------
    def _call(self, server: str, tool: str, args: Dict[str, Any]) -> str:
        session = self.sessions.get(server)
        if session is None or self.loop is None:
            return f"ERROR: MCP server '{server}' is not connected."

        async def _do() -> Any:
            return await session.call_tool(tool, args or {})

        try:
            fut = asyncio.run_coroutine_threadsafe(_do(), self.loop)
            result = fut.result(timeout=180)
        except Exception as e:
            return f"ERROR calling {server}.{tool}: {e}"

        # Normalize MCP CallToolResult content into text.
        chunks: List[str] = []
        for item in getattr(result, "content", []) or []:
            itype = getattr(item, "type", None)
            if itype == "text":
                chunks.append(getattr(item, "text", ""))
            else:
                try:
                    chunks.append(json.dumps(item.model_dump()))
                except Exception:
                    chunks.append(str(item))
        if getattr(result, "isError", False):
            return _truncate("ERROR: " + ("\n".join(chunks) or "tool reported an error"))
        return _truncate("\n".join(chunks) if chunks else "(no output)")

    # --- tool exposure -----------------------------------------------------
    def build_tools(self) -> List[Tool]:
        tools: List[Tool] = []
        for server, defs in self.tool_defs.items():
            for td in defs:
                tool_name = td.name
                full_name = f"mcp__{server}__{tool_name}"
                schema = getattr(td, "inputSchema", None) or {"type": "object", "properties": {}}

                def make_handler(srv: str, tname: str):
                    def handler(args: Dict[str, Any]) -> str:
                        return self._call(srv, tname, args)
                    return handler

                tools.append(
                    Tool(
                        name=full_name,
                        description=f"[MCP:{server}] {td.description or tool_name}",
                        input_schema=schema,
                        handler=make_handler(server, tool_name),
                        source="mcp",
                    )
                )
        return tools

    def status(self) -> str:
        lines = []
        for server in self.servers_config:
            if server in self.sessions:
                n = len(self.tool_defs.get(server, []))
                lines.append(f"  ✓ {server}: connected ({n} tools)")
            elif server in self.errors:
                lines.append(f"  ✗ {server}: {self.errors[server]}")
            elif not (self.servers_config[server] or {}).get("enabled", True):
                lines.append(f"  · {server}: disabled")
        if "*" in self.errors:
            lines.append(f"  ! {self.errors['*']}")
        return "\n".join(lines) if lines else "  (none configured)"
