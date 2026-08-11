"""Provider-agnostic LLM interface.

Every provider converts the hub's internal message format to its own API and
returns a normalized `AssistantTurn`. This lets the agent loop stay identical
regardless of which brain (Claude / OpenAI / Ollama) is in use.

Internal message format (list of dicts):
    {"role": "system",    "content": str}
    {"role": "user",      "content": str}
    {"role": "assistant", "content": str, "tool_calls": [ToolCall, ...]}
    {"role": "tool",      "tool_call_id": str, "name": str, "content": str}

Tool schema format (list of dicts):
    {"name": str, "description": str, "input_schema": {json-schema object}}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AssistantTurn:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)

    def to_message(self) -> Dict[str, Any]:
        return {
            "role": "assistant",
            "content": self.text,
            "tool_calls": self.tool_calls,
        }


class LLMProvider:
    """Base class. Subclasses implement `complete`."""

    name: str = "base"

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> AssistantTurn:
        raise NotImplementedError


def split_system(messages: List[Dict[str, Any]]) -> tuple[str, List[Dict[str, Any]]]:
    """Return (system_prompt, non_system_messages)."""
    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    rest = [m for m in messages if m["role"] != "system"]
    return "\n\n".join(p for p in system_parts if p), rest
