"""Media generation tools (image now; video is a planned follow-up).

Saves output into the current project's working folder (the process cwd, which
the server sets to the open project's workspace), so generated media shows up in
the Files panel and can be previewed/downloaded.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Dict, List

from .base import Tool


def build_media_tools(config) -> List[Tool]:
    media_cfg = (config.data.get("media") if hasattr(config, "data") else {}) or {}
    image_model = media_cfg.get("image_model", "gpt-image-1")
    key_env = ((config.llm.get("openai") or {}).get("api_key_env", "OPENAI_API_KEY")
               if hasattr(config, "llm") else "OPENAI_API_KEY")

    def generate_image(args: Dict[str, Any]) -> str:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return "ERROR: 'prompt' is required."
        filename = args.get("filename") or "image.png"
        if not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            filename += ".png"
        key = os.environ.get(key_env)
        if not key:
            return f"ERROR: image generation needs a funded image model — set ${key_env}."
        try:
            from openai import OpenAI
            client = OpenAI(api_key=key)
            size = args.get("size", "1024x1024")
            resp = client.images.generate(model=image_model, prompt=prompt, size=size)
            b64 = resp.data[0].b64_json
            data = base64.b64decode(b64)
            with open(filename, "wb") as fh:  # cwd == open project's workspace
                fh.write(data)
            return f"Created image '{filename}' ({len(data)} bytes) — it's in your Files panel; click to view or download."
        except Exception as e:
            return f"ERROR generating image ({image_model}): {e}"

    return [
        Tool(
            name="generate_image",
            description=(
                "Generate an image from a text prompt and save it into the project's files "
                "(it appears in the Files panel to preview/download). Requires a funded image "
                "model (default gpt-image-1)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "What to depict."},
                    "filename": {"type": "string", "description": "Output filename (optional)."},
                    "size": {"type": "string", "description": "e.g. 1024x1024 (optional)."},
                },
                "required": ["prompt"],
            },
            handler=generate_image,
            source="media",
        )
    ]
