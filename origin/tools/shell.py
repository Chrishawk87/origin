"""Local shell / OS connector.

Gives the agent the ability to run arbitrary commands on the machine the hub
is running on. In autonomous mode these execute without confirmation.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Dict

from .base import Tool

_MAX_OUTPUT = 30_000  # chars returned to the model per command


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT:
        return text
    head = text[: _MAX_OUTPUT // 2]
    tail = text[-_MAX_OUTPUT // 2:]
    omitted = len(text) - _MAX_OUTPUT
    return f"{head}\n...[{omitted} chars truncated]...\n{tail}"


def build_shell_tools(timeout: int = 300) -> list[Tool]:
    def run_shell(args: Dict[str, Any]) -> str:
        command = args.get("command", "")
        if not command:
            return "ERROR: no command provided."
        workdir = args.get("workdir") or os.getcwd()
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=args.get("timeout", timeout),
            )
        except subprocess.TimeoutExpired:
            return f"ERROR: command timed out after {args.get('timeout', timeout)}s."
        except Exception as e:  # pragma: no cover
            return f"ERROR: {e}"

        out = proc.stdout or ""
        err = proc.stderr or ""
        parts = [f"exit_code: {proc.returncode}"]
        if out.strip():
            parts.append(f"stdout:\n{out}")
        if err.strip():
            parts.append(f"stderr:\n{err}")
        return _truncate("\n".join(parts))

    def read_file(args: Dict[str, Any]) -> str:
        path = os.path.expanduser(args.get("path", ""))
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return _truncate(fh.read())
        except Exception as e:
            return f"ERROR reading {path}: {e}"

    def write_file(args: Dict[str, Any]) -> str:
        path = os.path.expanduser(args.get("path", ""))
        content = args.get("content", "")
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            return f"OK: wrote {len(content)} chars to {path}"
        except Exception as e:
            return f"ERROR writing {path}: {e}"

    return [
        Tool(
            name="shell",
            description=(
                "Execute a shell command on the local machine and return its "
                "stdout, stderr, and exit code. Use for any OS operation: running "
                "programs, managing files, calling CLIs, orchestrating other tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                    "workdir": {"type": "string", "description": "Working directory (optional)."},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (optional)."},
                },
                "required": ["command"],
            },
            handler=run_shell,
        ),
        Tool(
            name="read_file",
            description="Read a text file from the local filesystem.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=read_file,
        ),
        Tool(
            name="write_file",
            description="Write text to a file on the local filesystem (creates dirs as needed, overwrites).",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            handler=write_file,
        ),
    ]
