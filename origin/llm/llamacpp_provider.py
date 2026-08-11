"""Built-in local brain via llama.cpp (llama-cpp-python).

This makes Origin self-contained: the inference engine installs with Origin and
the GGUF model is downloaded/cached automatically, so there's no separate app
(no Ollama) to run. The model loads in-process on first use.

Small local models converse well and can do light tool use; for heavy tool-driven
work you'll still get sharper results from Claude/GPT (call them as workers).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .base import AssistantTurn, LLMProvider, ToolCall

# A compact, capable, tool-aware default. ~1.9GB (Q4_K_M).
DEFAULT_REPO = "bartowski/Qwen2.5-3B-Instruct-GGUF"
DEFAULT_FILE = "Qwen2.5-3B-Instruct-Q4_K_M.gguf"


def model_ref(cfg: Dict[str, Any]) -> tuple[str, str]:
    return cfg.get("repo_id", DEFAULT_REPO), cfg.get("filename", DEFAULT_FILE)


class LlamaCppProvider(LLMProvider):
    name = "llamacpp"

    def __init__(self, cfg: Dict[str, Any]):
        cfg = cfg or {}
        try:
            from llama_cpp import Llama
        except ImportError as e:
            raise SystemExit(
                "The built-in brain needs llama-cpp-python:\n"
                "  pip install llama-cpp-python huggingface_hub"
            ) from e

        self.max_tokens = int(cfg.get("max_tokens", 2048))
        n_ctx = int(cfg.get("n_ctx", 8192))
        repo, filename = model_ref(cfg)
        self.model = filename

        load_kwargs: Dict[str, Any] = {"n_ctx": n_ctx, "verbose": False}
        if cfg.get("n_gpu_layers") is not None:
            load_kwargs["n_gpu_layers"] = cfg["n_gpu_layers"]
        # Enable tool/function calling where the format supports it.
        load_kwargs.setdefault("chat_format", cfg.get("chat_format", "chatml-function-calling"))

        try:
            if cfg.get("model_path"):
                self.llm = Llama(model_path=cfg["model_path"], **load_kwargs)
            else:
                self.llm = Llama.from_pretrained(repo_id=repo, filename=filename, **load_kwargs)
        except Exception:
            # retry without the special chat format (older builds / metadata issues)
            load_kwargs.pop("chat_format", None)
            if cfg.get("model_path"):
                self.llm = Llama(model_path=cfg["model_path"], **load_kwargs)
            else:
                self.llm = Llama.from_pretrained(repo_id=repo, filename=filename, **load_kwargs)

    def _convert(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for m in messages:
            role = m["role"]
            if role in ("system", "user"):
                out.append({"role": role, "content": m["content"]})
            elif role == "assistant":
                msg: Dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
                calls = m.get("tool_calls", [])
                if calls:
                    msg["tool_calls"] = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                        for tc in calls
                    ]
                out.append(msg)
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": m.get("tool_call_id", ""),
                            "content": m["content"]})
        return out

    def complete(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> AssistantTurn:
        kwargs: Dict[str, Any] = {
            "messages": self._convert(messages),
            "max_tokens": self.max_tokens,
            "temperature": 0.4,
        }
        oa_tools = [
            {"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}})}}
            for t in tools
        ]
        if oa_tools:
            kwargs["tools"] = oa_tools
            kwargs["tool_choice"] = "auto"

        try:
            resp = self.llm.create_chat_completion(**kwargs)
        except Exception:
            # some builds/models can't take tools — converse without them
            kwargs.pop("tools", None)
            kwargs.pop("tool_choice", None)
            resp = self.llm.create_chat_completion(**kwargs)

        msg = resp["choices"][0]["message"]
        turn = AssistantTurn(text=msg.get("content") or "")
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            turn.tool_calls.append(
                ToolCall(id=tc.get("id", "call"), name=fn.get("name", ""), arguments=args)
            )

        # Small models in function-calling mode sometimes return NOTHING for a
        # plain message. If so, ask again as a normal conversation so the user
        # always gets a real reply.
        if not turn.text and not turn.tool_calls and "tools" in kwargs:
            plain = {"messages": self._convert(messages),
                     "max_tokens": self.max_tokens, "temperature": 0.4}
            try:
                resp2 = self.llm.create_chat_completion(**plain)
                turn.text = resp2["choices"][0]["message"].get("content") or ""
            except Exception:
                pass
        return turn
