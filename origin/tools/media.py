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


def _dest(filename: str) -> str:
    """Put bare filenames into a visible 'outputs/' folder in the workspace and
    make sure the parent directory exists, so products always have a home."""
    if "/" not in filename and os.sep not in filename:
        filename = os.path.join("outputs", filename)
    parent = os.path.dirname(filename)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return filename


def _openai_client(key_env: str):
    import os
    key = os.environ.get(key_env)
    if not key:
        return None
    from openai import OpenAI
    return OpenAI(api_key=key)


def _save_video(client, vid_id: str, filename: str) -> str:
    """Download a finished video by trying the known SDK access patterns."""
    data = None
    # pattern A: dedicated download endpoint returning a streamable body
    for attempt in (
        lambda: client.videos.download_content(vid_id, variant="video"),
        lambda: client.videos.download_content(vid_id),
    ):
        try:
            resp = attempt()
            data = resp.read() if hasattr(resp, "read") else (
                resp.content if hasattr(resp, "content") else resp)
            if data:
                break
        except Exception:
            continue
    if not data:
        return ""
    if isinstance(data, str):
        data = data.encode()
    with open(filename, "wb") as fh:
        fh.write(data)
    return filename


def build_media_tools(config) -> List[Tool]:
    media_cfg = (config.data.get("media") if hasattr(config, "data") else {}) or {}
    image_model = media_cfg.get("image_model", "gpt-image-1")
    video_model = media_cfg.get("video_model", "sora-2")
    key_env = ((config.llm.get("openai") or {}).get("api_key_env", "OPENAI_API_KEY")
               if hasattr(config, "llm") else "OPENAI_API_KEY")

    def generate_image(args: Dict[str, Any]) -> str:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return "ERROR: 'prompt' is required."
        filename = args.get("filename") or "image.png"
        if not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            filename += ".png"
        filename = _dest(filename)
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

    def _veo_generate(prompt: str, filename: str, args: Dict[str, Any]) -> str:
        import base64
        import os
        import time

        import requests
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            return "ERROR: Veo needs a funded Google/Gemini account — set $GEMINI_API_KEY."
        model = args.get("model") or media_cfg.get("veo_model", "veo-3.1-generate-preview")
        base = "https://generativelanguage.googleapis.com/v1beta"
        try:
            start = requests.post(
                f"{base}/models/{model}:predictLongRunning",
                params={"key": key},
                json={"instances": [{"prompt": prompt}],
                      "parameters": {"durationSeconds": int(args.get("seconds", 8)),
                                     "aspectRatio": args.get("aspect", "16:9")}},
                timeout=60,
            )
            if start.status_code >= 400:
                return f"ERROR starting Veo ({model}): {start.status_code} {start.text[:300]}"
            op = start.json().get("name")
            waited = 0
            while waited < 180:
                time.sleep(8)
                waited += 8
                d = requests.get(f"{base}/{op}", params={"key": key}, timeout=60).json()
                if d.get("done"):
                    resp = d.get("response", {}) or {}
                    vids = (resp.get("generatedVideos")
                            or resp.get("generateVideoResponse", {}).get("generatedSamples") or [])
                    if vids:
                        v = vids[0]
                        vobj = v.get("video", {}) if isinstance(v.get("video"), dict) else {}
                        b64 = vobj.get("bytesBase64Encoded") or v.get("bytesBase64Encoded")
                        uri = vobj.get("uri") or v.get("uri")
                        if b64:
                            open(filename, "wb").write(base64.b64decode(b64))
                            return f"Created video '{filename}' (Veo) — it's in your Files panel."
                        if uri:
                            vb = requests.get(uri, params={"key": key}, timeout=180)
                            open(filename, "wb").write(vb.content)
                            return f"Created video '{filename}' (Veo) — it's in your Files panel."
                    return f"Veo finished but I couldn't locate the video bytes. Response keys: {list(resp.keys())}"
            return f"Veo job still rendering after {waited}s (op {op}). Ask again shortly to retrieve it."
        except Exception as e:
            return (f"ERROR generating video with Veo ({model}): {e}\n"
                    "(If this is an API-shape mismatch, tell Claude the exact error to adjust.)")

    def generate_video(args: Dict[str, Any]) -> str:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return "ERROR: 'prompt' is required."
        filename = args.get("filename") or "video.mp4"
        if not filename.lower().endswith((".mp4", ".mov", ".webm")):
            filename += ".mp4"
        filename = _dest(filename)
        engine = (args.get("engine") or media_cfg.get("video_provider", "sora")).lower()
        if engine in ("veo", "google", "gemini"):
            return _veo_generate(prompt, filename, args)
        client = _openai_client(key_env)
        if client is None:
            return f"ERROR: video generation needs a funded Sora account — set ${key_env}."
        model = args.get("model", video_model)
        seconds = str(args.get("seconds", 8))
        size = args.get("size", "1280x720")
        try:
            import time
            job = client.videos.create(model=model, prompt=prompt, seconds=seconds, size=size)
            vid_id = getattr(job, "id", None)
            status = getattr(job, "status", "") or ""
            waited = 0
            while status.lower() in ("queued", "in_progress", "processing", "running") and waited < 180:
                time.sleep(6)
                waited += 6
                job = client.videos.retrieve(vid_id)
                status = getattr(job, "status", "") or ""
            if status.lower() not in ("completed", "succeeded", "done"):
                return (f"Video job submitted (id: {vid_id}, status: {status or 'processing'}). "
                        f"It's still rendering — call check_video with id '{vid_id}' in a bit to finish it.")
            saved = _save_video(client, vid_id, filename)
            if not saved:
                return f"Video {vid_id} finished but I couldn't download it automatically. Try check_video id '{vid_id}'."
            return f"Created video '{filename}' — it's in your Files panel to preview and download."
        except Exception as e:
            return (f"ERROR generating video ({model}): {e}\n"
                    "(If this is an API-shape mismatch, tell Claude the exact error and it'll adjust.)")

    def check_video(args: Dict[str, Any]) -> str:
        vid_id = (args.get("id") or "").strip()
        if not vid_id:
            return "ERROR: 'id' is required (from a previous generate_video call)."
        filename = _dest(args.get("filename") or f"{vid_id}.mp4")
        client = _openai_client(key_env)
        if client is None:
            return f"ERROR: needs ${key_env}."
        try:
            job = client.videos.retrieve(vid_id)
            status = (getattr(job, "status", "") or "").lower()
            if status not in ("completed", "succeeded", "done"):
                return f"Video {vid_id} status: {status or 'processing'}. Not ready yet."
            saved = _save_video(client, vid_id, filename)
            return (f"Saved '{filename}' to your Files panel." if saved
                    else f"Video {vid_id} is ready but download failed.")
        except Exception as e:
            return f"ERROR checking video: {e}"

    return [
        Tool(
            name="generate_video",
            description=(
                "Generate an original short video from a text prompt and save it to the project's "
                "files. Choose the engine: 'sora' (OpenAI) or 'veo' (Google). Rendering is slow; if "
                "not done quickly, Sora returns a job id to finish with check_video. Requires a "
                "funded account for the chosen engine."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "engine": {"type": "string", "enum": ["sora", "veo"], "description": "Which video model to use."},
                    "filename": {"type": "string"},
                    "seconds": {"type": "integer", "description": "clip length (e.g. 8)"},
                    "size": {"type": "string", "description": "Sora size, e.g. 1280x720"},
                    "aspect": {"type": "string", "description": "Veo aspect, e.g. 16:9"},
                },
                "required": ["prompt"],
            },
            handler=generate_video,
            source="media",
        ),
        Tool(
            name="check_video",
            description="Check/download a video job started by generate_video, by its id.",
            input_schema={
                "type": "object",
                "properties": {"id": {"type": "string"}, "filename": {"type": "string"}},
                "required": ["id"],
            },
            handler=check_video,
            source="media",
        ),
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
