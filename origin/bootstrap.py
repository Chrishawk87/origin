"""`origin doctor` — get Origin ready to run out of the gate.

Checks the pieces Origin can use (LLM brains, Ollama, browser, web) and, where
possible, sets them up: pulls a default Ollama model so there's a free local
brain from first launch, and installs the Playwright browser for
click-and-retrieve. Anything it can't do automatically it prints as a one-liner.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

DEFAULT_OLLAMA_MODEL = "llama3.1"


def ensure_builtin_model(repo: str = "bartowski/Qwen2.5-3B-Instruct-GGUF",
                         filename: str = "Qwen2.5-3B-Instruct-Q4_K_M.gguf") -> bool:
    """Download + cache the built-in llama.cpp model so first chat is instant."""
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("  (huggingface_hub not installed; run: pip install huggingface_hub)")
        return False
    try:
        print(f"  fetching built-in model {filename} (one-time, ~1.9GB)…")
        hf_hub_download(repo_id=repo, filename=filename)
        return True
    except Exception as e:
        print(f"  could not download model now: {e}")
        return False


def _ok(msg): print(f"  \033[32m✓\033[0m {msg}")
def _no(msg): print(f"  \033[33m•\033[0m {msg}")
def _hd(msg): print(f"\n\033[1m{msg}\033[0m")


def _has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def doctor(fix: bool = True) -> int:
    print("\033[1m◍ Origin doctor\033[0m — checking your setup")

    _hd("Python packages")
    for mod, why in [
        ("rich", "terminal UI"), ("yaml", "config"), ("requests", "web"),
        ("fastapi", "desktop app"), ("uvicorn", "desktop app"),
        ("webview", "native window"), ("playwright", "click-and-retrieve"),
        ("anthropic", "Claude worker"), ("openai", "GPT/Ollama worker"),
        ("mcp", "MCP servers"), ("trafilatura", "better web extraction"),
    ]:
        (_ok if _has_module(mod) else _no)(f"{mod} — {why}")

    _hd("Browser (click-and-retrieve)")
    if _has_module("playwright"):
        if fix:
            print("  installing Chromium (one-time)…")
            try:
                subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                               check=False)
                _ok("Chromium ready")
            except Exception as e:
                _no(f"could not install Chromium: {e}")
        else:
            _no("run: playwright install chromium")
    else:
        _no("pip install playwright  &&  playwright install chromium")

    _hd("Built-in brain (llama.cpp — self-contained, no external app)")
    if _has_module("llama_cpp"):
        _ok("llama-cpp-python installed")
        if fix:
            ensure_builtin_model()
        else:
            _no("run 'origin --doctor' (without --check) to download the model")
    else:
        _no("pip install llama-cpp-python huggingface_hub   (self-contained local brain)")

    _hd("Alternative local brain (Ollama — optional)")
    ollama = shutil.which("ollama")
    if ollama:
        _ok("ollama installed")
        try:
            listed = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=10).stdout
        except Exception:
            listed = ""
        if DEFAULT_OLLAMA_MODEL.split(":")[0] in listed:
            _ok(f"model '{DEFAULT_OLLAMA_MODEL}' present")
        elif fix:
            print(f"  pulling '{DEFAULT_OLLAMA_MODEL}' (this can take a few minutes)…")
            try:
                subprocess.run(["ollama", "pull", DEFAULT_OLLAMA_MODEL], check=False)
                _ok(f"model '{DEFAULT_OLLAMA_MODEL}' ready")
            except Exception as e:
                _no(f"could not pull model: {e}")
        else:
            _no(f"run: ollama pull {DEFAULT_OLLAMA_MODEL}")
    else:
        _no("Ollama not found. Install it from https://ollama.com , then: "
            f"ollama pull {DEFAULT_OLLAMA_MODEL}")

    _hd("Cloud brains (optional — used only for hard reasoning)")
    (_ok if os.environ.get("ANTHROPIC_API_KEY") else _no)(
        "ANTHROPIC_API_KEY " + ("set" if os.environ.get("ANTHROPIC_API_KEY") else "not set (Claude worker off)"))
    (_ok if os.environ.get("OPENAI_API_KEY") else _no)(
        "OPENAI_API_KEY " + ("set" if os.environ.get("OPENAI_API_KEY") else "not set (GPT worker off)"))

    print("\n\033[1mReady.\033[0m  Start the app with:  ./origin-app.sh   (or: python run_app.py)")
    print("Or the terminal:  python -m origin\n")
    return 0


def main() -> int:
    fix = "--check" not in sys.argv
    return doctor(fix=fix)


if __name__ == "__main__":
    raise SystemExit(main())
