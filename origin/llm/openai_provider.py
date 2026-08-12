"""OpenAI-compatible provider.

Also powers Ollama and any other OpenAI-compatible endpoint by pointing
`base_url` at it (see OllamaProvider below).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .base import AssistantTurn, LLMProvider, ToolCall, sanitize_history


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, cfg: Dict[str, Any]):
        import os

        try:
            from openai import OpenAI
        except ImportError as e:
            raise SystemExit(
                "The 'openai' package is required for the OpenAI/Ollama providers.\n"
                "Install it with: pip install openai"
            ) from e

        key_env = cfg.get("api_key_env", "OPENAI_API_KEY")
        api_key = cfg.get("api_key") or os.environ.get(key_env) or cfg.get("_default_key")
        base_url = cfg.get("base_url")
        if not api_key and not base_url:
            raise SystemExit(
                f"No OpenAI API key found. Set ${key_env} or add "
                "llm.openai.api_key to your config."
            )
        client_kwargs: Dict[str, Any] = {"api_key": api_key or "not-needed"}
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = OpenAI(**client_kwargs)
        self.model = cfg.get("model", "gpt-4o")
        self.max_tokens = int(cfg.get("max_tokens", 4096))

    def _convert_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for m in messages:
            role = m["role"]
            if role in ("system", "user"):
                out.append({"role": role, "content": m["content"]})
            elif role == "assistant":
                msg: Dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
                calls = m.get("tool_calls", [])
                if calls:
                    msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in calls
                    ]
                out.append(msg)
            elif role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": m["tool_call_id"],
                    "name": m.get("name") or "tool",  # gemini needs non-empty function name
                    "content": m["content"],
                })
        return out

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> AssistantTurn:
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": self._convert_messages(sanitize_history(messages)),
            "max_tokens": self.max_tokens,
        }
        if openai_tools:
            kwargs["tools"] = openai_tools

        resp = self.client.chat.completions.create(**kwargs)
        choice = resp.choices[0].message

        turn = AssistantTurn(text=choice.content or "")
        for tc in (choice.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            turn.tool_calls.append(
                ToolCall(id=tc.id, name=tc.function.name, arguments=args)
            )
        return turn


class OllamaProvider(OpenAIProvider):
    """Ollama via its OpenAI-compatible endpoint (default http://localhost:11434/v1)."""

    name = "ollama"

    def __init__(self, cfg: Dict[str, Any]):
        cfg = dict(cfg)
        cfg.setdefault("base_url", "http://localhost:11434/v1")
        cfg.setdefault("_default_key", "ollama")
        cfg.setdefault("model", "llama3.1")
        super().__init__(cfg)


class GrokProvider(OpenAIProvider):
    """xAI Grok via its OpenAI-compatible endpoint."""

    name = "grok"

    def __init__(self, cfg: Dict[str, Any]):
        cfg = dict(cfg)
        cfg.setdefault("base_url", "https://api.x.ai/v1")
        cfg.setdefault("api_key_env", "XAI_API_KEY")
        cfg.setdefault("model", "grok-4")
        super().__init__(cfg)


class GeminiProvider(OpenAIProvider):
    """Google Gemini via its OpenAI-compatible endpoint."""

    name = "gemini"

    def __init__(self, cfg: Dict[str, Any]):
        cfg = dict(cfg)
        cfg.setdefault("base_url", "https://generativelanguage.googleapis.com/v1beta/openai/")
        cfg.setdefault("api_key_env", "GEMINI_API_KEY")
        cfg.setdefault("model", "gemini-flash-latest")
        super().__init__(cfg)
