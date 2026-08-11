"""LLM provider factory."""

from __future__ import annotations

from typing import Any, Dict

from .base import AssistantTurn, LLMProvider, ToolCall


def build_provider(llm_cfg: Dict[str, Any]) -> LLMProvider:
    provider = (llm_cfg.get("provider") or "anthropic").lower()

    if provider == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(llm_cfg.get("anthropic", {}))

    if provider == "openai":
        from .openai_provider import OpenAIProvider
        return OpenAIProvider(llm_cfg.get("openai", {}))

    if provider == "ollama":
        from .openai_provider import OllamaProvider
        return OllamaProvider(llm_cfg.get("ollama", {}))

    if provider in ("grok", "xai"):
        from .openai_provider import GrokProvider
        return GrokProvider(llm_cfg.get("grok", {}))

    if provider in ("gemini", "google"):
        from .openai_provider import GeminiProvider
        return GeminiProvider(llm_cfg.get("gemini", {}))

    if provider in ("llamacpp", "builtin", "local-builtin"):
        from .llamacpp_provider import LlamaCppProvider
        return LlamaCppProvider(llm_cfg.get("llamacpp", {}))

    raise SystemExit(
        f"Unknown LLM provider '{provider}'. "
        "Choose one of: anthropic, openai, grok, gemini, ollama, llamacpp."
    )


__all__ = ["build_provider", "LLMProvider", "AssistantTurn", "ToolCall"]
