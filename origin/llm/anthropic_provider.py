"""Anthropic Claude provider."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .base import AssistantTurn, LLMProvider, ToolCall, sanitize_history, split_system


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, cfg: Dict[str, Any]):
        import os

        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise SystemExit(
                "The 'anthropic' package is required for the Anthropic provider.\n"
                "Install it with: pip install anthropic"
            ) from e

        key_env = cfg.get("api_key_env", "ANTHROPIC_API_KEY")
        api_key = cfg.get("api_key") or os.environ.get(key_env)
        if not api_key:
            raise SystemExit(
                f"No Anthropic API key found. Set ${key_env} or add "
                "llm.anthropic.api_key to your config."
            )
        self.client = Anthropic(api_key=api_key)
        self.model = cfg.get("model", "claude-sonnet-4-5")
        self.max_tokens = int(cfg.get("max_tokens", 4096))

    def _convert_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        i = 0
        n = len(messages)
        while i < n:
            m = messages[i]
            role = m["role"]
            if role == "user":
                out.append({"role": "user", "content": m["content"]})
                i += 1
            elif role == "assistant":
                blocks: List[Dict[str, Any]] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls", []):
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    })
                out.append({"role": "assistant", "content": blocks or ""})
                i += 1
            elif role == "tool":
                # Coalesce ALL consecutive tool results into ONE user message.
                # Anthropic requires every tool_result to sit in the single user
                # message right after the assistant message that made the calls —
                # emitting one user message per result breaks that pairing.
                results: List[Dict[str, Any]] = []
                while i < n and messages[i]["role"] == "tool":
                    tm = messages[i]
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": tm["tool_call_id"],
                        "content": tm["content"],
                    })
                    i += 1
                out.append({"role": "user", "content": results})
            else:
                i += 1
        return out

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> AssistantTurn:
        system, rest = split_system(sanitize_history(messages))
        anthropic_tools = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("input_schema", {"type": "object", "properties": {}}),
            }
            for t in tools
        ]

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": self._convert_messages(rest),
        }
        if system:
            kwargs["system"] = system
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools

        resp = self.client.messages.create(**kwargs)

        turn = AssistantTurn()
        for block in resp.content:
            if block.type == "text":
                turn.text += block.text
            elif block.type == "tool_use":
                args = block.input if isinstance(block.input, dict) else {}
                turn.tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=args))
        return turn
