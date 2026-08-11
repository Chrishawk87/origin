"""Executive loop — Origin's plan → act → critique → iterate harness.

A single react-loop is fine for one step. A *mission* is a goal big enough to
need decomposition, tool use across many steps, and a check that it's actually
done. This module drives that: it asks the brain for a plan, executes each step
with the full agent (tools, research, multi-model collaboration, memory), then
critiques the result and runs another round if the goal isn't met.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

_PLANNER_SYS = (
    "You are the executive planner for an autonomous agent that can browse the web, run a "
    "terminal, call APIs/apps, research live, and consult other AI models. Given a goal, output "
    "a concise numbered plan of concrete, executable steps (max 6). No preamble — just the steps."
)
_CRITIC_SYS = (
    "You are a demanding reviewer. Decide if the goal is fully achieved. Respond with 'DONE' on "
    "the first line if it is; otherwise 'CONTINUE' and a numbered list (max 4) of the specific "
    "remaining steps."
)
_SYNTH_SYS = "You synthesize the final deliverable for the user from the work done. Be concrete and complete."


def _parse_steps(text: str) -> List[str]:
    steps = []
    for line in (text or "").splitlines():
        m = re.match(r"\s*\d+[\.\)]\s+(.*)", line)
        if m and m.group(1).strip():
            steps.append(m.group(1).strip())
    if not steps:  # fallback: non-empty lines
        steps = [ln.strip("-• ").strip() for ln in (text or "").splitlines() if ln.strip()][:6]
    return steps[:6]


def run_mission(
    agent,
    goal: str,
    ask_fn: Callable[[str, str], str],
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    max_rounds: int = 2,
) -> Dict[str, Any]:
    def emit(kind, **kw):
        if on_event:
            on_event({"type": kind, **kw})

    transcript: List[Dict[str, str]] = []

    plan_text = ask_fn(f"Goal:\n{goal}\n\nProduce the plan.", _PLANNER_SYS)
    steps = _parse_steps(plan_text)
    emit("plan", steps=steps)

    completed: List[Dict[str, str]] = []
    rounds = 0
    while rounds < max_rounds:
        rounds += 1
        for step in steps:
            emit("step", round=rounds, step=step)
            result = agent.run(
                f"[Mission: {goal}]\nDo this step now and report the concrete result: {step}",
                on_tool_start=lambda n, a: emit("tool", name=n, args=a),
                on_tool_result=lambda n, r: emit("result", name=n, result=r[:2000]),
            )
            completed.append({"step": step, "result": result})
            transcript.append({"step": step, "result": result})

        summary = "\n\n".join(f"STEP: {c['step']}\nRESULT: {c['result'][:1200]}" for c in completed)
        review = ask_fn(f"Goal:\n{goal}\n\nWork done so far:\n{summary}\n\nIs the goal achieved?", _CRITIC_SYS)
        emit("review", text=review)
        if review.strip().upper().startswith("DONE") or rounds >= max_rounds:
            break
        steps = _parse_steps(review)
        if not steps:
            break

    summary = "\n\n".join(f"STEP: {c['step']}\nRESULT: {c['result'][:1500]}" for c in completed)
    final = ask_fn(f"Goal:\n{goal}\n\nAll work:\n{summary}\n\nWrite the final deliverable for the user.", _SYNTH_SYS)
    emit("final", text=final)
    return {"goal": goal, "plan": _parse_steps(plan_text), "transcript": transcript, "final": final, "rounds": rounds}
