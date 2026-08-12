"""YouTube tools — read a video's transcript and basic info.

Lets Origin "watch" a YouTube video by reading its captions/transcript, so it
can summarize, analyze, or learn from it. Video *generation* (making new videos)
is a separate, heavier capability handled elsewhere.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from .base import Tool

_MAX = 28000


def _video_id(url: str) -> str | None:
    if not url:
        return None
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|/live/)([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url.strip()):
        return url.strip()
    return None


def _seg_text(s: Any) -> str:
    if isinstance(s, dict):
        return s.get("text", "")
    return getattr(s, "text", "")


def build_youtube_tools() -> List[Tool]:
    def transcript(args: Dict[str, Any]) -> str:
        url = args.get("url", "")
        vid = _video_id(url)
        if not vid:
            return "ERROR: couldn't find a YouTube video id in that URL."
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError:
            return "ERROR: the youtube-transcript-api library isn't installed on the server."
        segs = None
        try:
            # classic API
            segs = YouTubeTranscriptApi.get_transcript(vid)
        except AttributeError:
            try:  # newer instance API
                segs = YouTubeTranscriptApi().fetch(vid)
            except Exception as e:
                return f"ERROR fetching transcript: {e}"
        except Exception as e:
            return f"ERROR fetching transcript: {e}"
        try:
            text = "\n".join(_seg_text(s) for s in segs)
        except Exception as e:
            return f"ERROR reading transcript: {e}"
        if not text.strip():
            return "No transcript/captions available for that video."
        more = "" if len(text) <= _MAX else f"\n\n…[truncated; {len(text)} chars total]"
        return f"Transcript for youtube video {vid}:\n\n{text[:_MAX]}{more}"

    def info(args: Dict[str, Any]) -> str:
        import requests
        url = args.get("url", "")
        try:
            r = requests.get("https://www.youtube.com/oembed",
                             params={"url": url, "format": "json"}, timeout=20)
            r.raise_for_status()
            d = r.json()
            return f"Title: {d.get('title')}\nChannel: {d.get('author_name')}\nURL: {url}"
        except Exception as e:
            return f"ERROR getting video info: {e}"

    return [
        Tool(
            name="youtube_transcript",
            description=("Read a YouTube video by fetching its transcript/captions (by URL or ID). "
                         "Use to summarize, analyze, extract ideas, or learn from a video."),
            input_schema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
            handler=transcript,
            source="youtube",
        ),
        Tool(
            name="youtube_info",
            description="Get a YouTube video's title and channel from its URL.",
            input_schema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
            handler=info,
            source="youtube",
        ),
    ]
