"""Small shared helpers."""

from __future__ import annotations

from datetime import datetime, timezone


def today_str() -> str:
    """Human-readable current date, e.g. 'Tuesday, August 12, 2026' (UTC)."""
    return datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
