"""Prompt enhancer — the 'human → computer' layer.

Before the agent acts, this rewrites the user's raw, casual, or under-specified
message into one clear, complete, well-structured instruction that gets a better
result — without changing what they actually asked for. Trivial/conversational
messages pass through unchanged.
"""

from __future__ import annotations

from typing import Callable

_SYS = (
    "You are an expert prompt engineer embedded inside an autonomous AI agent that has powerful "
    "tools: live web research, file reading/writing, code execution, image and video generation, "
    "and several expert AI models it can consult. Your ONLY job is to rewrite the user's raw "
    "request into ONE clear, complete, well-structured instruction that will get the best possible "
    "result from that agent.\n\n"
    "Rules:\n"
    "- Preserve the user's intent EXACTLY. Never invent facts, add requirements they didn't imply, "
    "or change the subject.\n"
    "- Make the goal, the desired output/format, and any sensible constraints explicit.\n"
    "- When the topic could have changed over time, instruct the agent to use current, researched "
    "information rather than memory.\n"
    "- Keep it tight — a strong instruction, not an essay.\n"
    "- If the message is just conversational (a greeting, a yes/no, small talk) or is already clear "
    "and specific, return it UNCHANGED.\n"
    "Output ONLY the improved instruction — no preamble, no quotes, no explanation."
)


def enhance_prompt(ask_fn: Callable[[str, str], str], raw: str, context: str = "") -> str:
    """Return an improved instruction, or the original on any issue."""
    raw = (raw or "").strip()
    if len(raw) < 4:
        return raw
    user = (f"{context}\n\n" if context else "") + f"User's raw request:\n{raw}\n\nImproved instruction:"
    try:
        out = ask_fn(user, _SYS)
    except Exception:
        return raw
    out = (out or "").strip().strip('"').strip()
    # sanity: don't accept empty, error strings, or runaway output
    if not out or out.startswith(("ERROR", "(")) or len(out) > 4000:
        return raw
    return out
