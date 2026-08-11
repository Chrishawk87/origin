"""Generic REST / HTTP API connector.

Each configured API becomes reachable through a single `http_request` tool.
The agent passes an `api` name (resolved to a base_url + default headers from
config) OR a full `url`. This means you can wire in *any* HTTP API just by
adding a few lines of YAML — no code changes.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from .base import Tool

_MAX_OUTPUT = 30_000


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT] + f"\n...[{len(text) - _MAX_OUTPUT} chars truncated]..."


def build_rest_tools(rest_apis: Dict[str, Any]) -> list[Tool]:
    try:
        import requests
    except ImportError as e:
        raise SystemExit("The 'requests' package is required. pip install requests") from e

    apis = rest_apis or {}

    def describe_apis() -> str:
        if not apis:
            return "No named APIs configured; pass a full 'url' instead."
        lines = []
        for name, cfg in apis.items():
            lines.append(f"- {name}: {cfg.get('base_url', '')} — {cfg.get('description', '')}")
        return "\n".join(lines)

    def http_request(args: Dict[str, Any]) -> str:
        method = (args.get("method") or "GET").upper()
        api = args.get("api")
        path = args.get("path", "")
        url = args.get("url")

        headers: Dict[str, str] = {}
        if api:
            cfg = apis.get(api)
            if not cfg:
                return f"ERROR: unknown api '{api}'. Known APIs:\n{describe_apis()}"
            base = cfg.get("base_url", "").rstrip("/")
            url = base + ("/" + path.lstrip("/") if path else "")
            headers.update(cfg.get("headers", {}) or {})
        elif not url:
            return "ERROR: provide either 'api' (+optional 'path') or a full 'url'."

        headers.update(args.get("headers", {}) or {})

        body = args.get("json")
        data = args.get("data")
        params = args.get("params")

        try:
            resp = requests.request(
                method,
                url,
                headers=headers or None,
                params=params or None,
                json=body if body is not None else None,
                data=data if data is not None else None,
                timeout=args.get("timeout", 60),
            )
        except Exception as e:
            return f"ERROR calling {method} {url}: {e}"

        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype:
            try:
                pretty = json.dumps(resp.json(), indent=2)
            except Exception:
                pretty = resp.text
        else:
            pretty = resp.text

        return _truncate(f"{method} {url}\nstatus: {resp.status_code}\n\n{pretty}")

    schema = {
        "type": "object",
        "properties": {
            "api": {
                "type": "string",
                "description": "Name of a configured API (resolves base_url + auth). Optional if 'url' given.",
            },
            "url": {"type": "string", "description": "Full URL. Optional if 'api' given."},
            "path": {"type": "string", "description": "Path appended to the api's base_url."},
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
                "description": "HTTP method (default GET).",
            },
            "params": {"type": "object", "description": "Query-string parameters."},
            "json": {"type": "object", "description": "JSON request body."},
            "headers": {"type": "object", "description": "Extra headers (merged over api defaults)."},
        },
    }

    desc = (
        "Make an HTTP request to any REST API. Configured APIs (call by 'api' name, "
        "auth is applied automatically):\n" + describe_apis()
    )

    return [
        Tool(
            name="http_request",
            description=desc,
            input_schema=schema,
            handler=http_request,
            source="rest",
        )
    ]
