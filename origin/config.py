"""Configuration loading for Origin.

Config is a YAML file. Resolution order:
  1. --config PATH (CLI flag)
  2. $ORIGIN_CONFIG
  3. ./origin.config.yaml
  4. ~/.origin/config.yaml

Any string value in the config may reference an environment variable with
${VAR} syntax; it is expanded at load time. A .env file in the working
directory (or next to the config) is loaded automatically if python-dotenv
is installed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict

try:
    import yaml
except ImportError as e:  # pragma: no cover
    raise SystemExit("PyYAML is required. Run: pip install -r requirements.txt") from e


_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")

DEFAULT_CONFIG: Dict[str, Any] = {
    "llm": {
        "provider": "anthropic",
        "anthropic": {
            "model": "claude-sonnet-4-5",
            "api_key_env": "ANTHROPIC_API_KEY",
            "max_tokens": 4096,
        },
        "openai": {
            "model": "gpt-4o",
            "api_key_env": "OPENAI_API_KEY",
            "max_tokens": 4096,
        },
        "ollama": {
            "model": "llama3.1",
            "base_url": "http://localhost:11434/v1",
            "max_tokens": 4096,
        },
        "llamacpp": {                # self-contained local brain (no external app)
            "repo_id": "bartowski/Qwen2.5-3B-Instruct-GGUF",
            "filename": "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
            "n_ctx": 8192,
            "max_tokens": 2048,
        },
    },
    "agent": {
        "max_iterations": 25,
        "autonomous": True,          # full autonomous: no confirmation prompts
        "shell_timeout": 300,        # seconds per shell command
        "system_prompt": None,       # None -> use the active profile's prompt
        "profile": "operator",       # which profile to start in
        "verbosity": "normal",       # quiet | normal | verbose
        "tool_allow": None,          # None -> all tools; else list of allowed names
        "tool_deny": None,           # list of tool names to block
    },
    # Named behavior profiles. system_prompt: null uses the built-in prompt of
    # the same name (operator | assistant | planner); a string overrides it.
    # provider/model are optional per-profile brain overrides.
    "profiles": {
        "operator": {
            "description": "Lean, do-what-I-say operator. No hedging. (default)",
            "system_prompt": None,
        },
        "assistant": {
            "description": "Explains its reasoning and plan as it works.",
            "system_prompt": None,
        },
        "planner": {
            "description": "Inspects and plans first; prefers non-destructive ops.",
            "system_prompt": None,
        },
        "researcher": {"description": "Live, cited research; freshest data wins.", "system_prompt": None},
        "marketer": {"description": "Positioning, hooks, channels — grounded in what's working now.", "system_prompt": None},
        "product_designer": {"description": "User-first flows, specs, and rationale.", "system_prompt": None},
        "analyst": {"description": "Quantify, compare, reason from evidence.", "system_prompt": None},
    },
    # Research / self-updating knowledge
    "research": {
        "default_ttl_hours": 24,     # cached answers older than this are re-gathered on ask
        "daily_refresh": True,       # background refresh of watched topics (desktop app)
    },
    # Prompt enhancer: rewrite the user's raw message into a stronger instruction
    # before acting. worker: null uses the coordinator brain; set to a cheap/free
    # model (e.g. "gemini") to cut cost.
    "enhancer": {"enabled": True, "worker": None},
    # Media generation models (need a funded account with access).
    "media": {
        "image_model": "gpt-image-1",
        "video_model": "sora-2",
        "video_provider": "sora",   # sora | veo
        "veo_model": "veo-3.1-generate-preview",
    },
    # Network serving (see `python -m origin.serve`). Token is REQUIRED for any
    # non-localhost access because Origin can run commands on this machine.
    "server": {
        "host": "127.0.0.1",
        "port": 8000,
        "token": None,               # None -> auto-generated when serving on a network
    },
    # Model workers — every one is available simultaneously. The BRAIN is the
    # coordinator (below), not any single model; these are the models it can
    # consult and make collaborate. If empty, the hub falls back to `llm`.
    "workers": {},                   # name -> {provider, model, role, api_key_env, ...}
    "orchestrator": {
        "coordinator": None,         # worker that runs the loop; None -> llm / first worker
        "collab_workers": None,      # default workers for collaborate (None -> all)
        "synthesizer": None,         # default worker that merges collaborations
    },
    "mcp_servers": {},               # name -> {command, args, env, enabled}
    "rest_apis": {},                 # name -> {base_url, headers, description}
    # One-click task presets shown in the app. Edit freely.
    "presets": [
        {
            "label": "TikTok top ads",
            "prompt": (
                "Open TikTok Creative Center Top Ads "
                "(https://ads.tiktok.com/business/creativecenter/inspiration/topads/pc/en) "
                "with the browser, and gather the current top-performing ads for [MY TOPIC]. "
                "For each, capture the brand, the hook/first 3 seconds, the format, and any "
                "visible metrics (likes/CTR). Then summarize the patterns I should copy."
            ),
        },
        {
            "label": "Organize this folder",
            "prompt": (
                "Inspect every file in this project's working folder, then organize it: group "
                "files into clearly named subfolders by type and topic, rename messy filenames, "
                "and give me a summary of exactly what you moved and why."
            ),
        },
        {
            "label": "Competitor scan",
            "prompt": (
                "Search the web for the top competitors and their current marketing angles for "
                "[MY TOPIC]. Summarize each one's positioning, channels, and the gaps I can exploit."
            ),
        },
    ],
    # Web + browser tools — usable by ANY worker (local included), token-free.
    "web": {
        "enabled": True,
        "search_backend": "ddg",     # ddg (no key) | tavily | brave | searxng
        "tavily_api_key_env": "TAVILY_API_KEY",
        "brave_api_key_env": "BRAVE_API_KEY",
        "searxng_url": None,
        "browser": True,             # enable Playwright click-and-retrieve tools
        "headless": True,
    },
}


def _expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} references in strings."""
    if isinstance(value, str):
        def repl(m: "re.Match[str]") -> str:
            return os.environ.get(m.group(1), "")
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _candidate_paths(explicit: str | None) -> list[Path]:
    paths = []
    if explicit:
        paths.append(Path(explicit).expanduser())
    if os.environ.get("ORIGIN_CONFIG"):
        paths.append(Path(os.environ["ORIGIN_CONFIG"]).expanduser())
    paths.append(Path.cwd() / "origin.config.yaml")
    paths.append(Path.home() / ".origin" / "config.yaml")
    return paths


def _load_dotenv(near: Path | None) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    # Load ./.env first, then a .env next to the config file (config wins).
    load_dotenv(Path.cwd() / ".env")
    if near is not None:
        load_dotenv(near.parent / ".env", override=False)


class Config:
    def __init__(self, data: Dict[str, Any], path: Path | None):
        self.data = data
        self.path = path

    # convenience accessors -------------------------------------------------
    @property
    def llm(self) -> Dict[str, Any]:
        return self.data["llm"]

    @property
    def agent(self) -> Dict[str, Any]:
        return self.data["agent"]

    @property
    def mcp_servers(self) -> Dict[str, Any]:
        return self.data.get("mcp_servers") or {}

    @property
    def rest_apis(self) -> Dict[str, Any]:
        return self.data.get("rest_apis") or {}

    @property
    def profiles(self) -> Dict[str, Any]:
        return self.data.get("profiles") or {}

    @property
    def workers(self) -> Dict[str, Any]:
        return self.data.get("workers") or {}

    @property
    def orchestrator(self) -> Dict[str, Any]:
        return self.data.get("orchestrator") or {}

    @property
    def web(self) -> Dict[str, Any]:
        return self.data.get("web") or {}

    @property
    def presets(self) -> list:
        return self.data.get("presets") or []

    def get(self, *keys, default=None):
        cur: Any = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


def load_config(explicit_path: str | None = None) -> Config:
    found: Path | None = None
    for p in _candidate_paths(explicit_path):
        if p.is_file():
            found = p
            break

    _load_dotenv(found)

    if found is None:
        # No config file — run on defaults (Anthropic + shell only).
        return Config(_expand_env(DEFAULT_CONFIG), None)

    with open(found, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    merged = _deep_merge(DEFAULT_CONFIG, raw)
    merged = _expand_env(merged)
    return Config(merged, found)
