"""Multi-model orchestration — the part that makes the *brain* its own layer.

The brain is not Claude or GPT. It is a coordination layer that holds several
model *workers* (Claude, GPT, a local model, …) and can:

  - consult any single worker for a sub-task, or
  - run a structured *collaboration* where several workers talk to each other
    (refine / debate / panel) and their output is merged into one answer.

A `coordinator` worker (which you choose — it can be a cheap local model) runs
the main agent loop and decides when to delegate; but the collaboration
protocols are deterministic, so even a weak coordinator can invoke a strong
multi-model exchange.

Token economy: workers are consulted with only the relevant slice of context
(the sub-task + the specific prior statements they need), never the whole main
conversation. A per-worker call counter makes spend visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .llm import LLMProvider, build_provider

# keep inter-model messages compact to control token spend
_CTX_CAP = 6000


def _cap(text: str, limit: int = _CTX_CAP) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


@dataclass
class Worker:
    name: str
    provider_name: str
    model: str
    role: str = ""
    _provider: Optional[LLMProvider] = None


class WorkerPool:
    """Holds every configured model worker; builds providers lazily so a
    missing key for an unused worker never blocks startup."""

    def __init__(self, workers_cfg: Dict[str, Any]):
        self.workers_cfg = workers_cfg or {}
        self.workers: Dict[str, Worker] = {}
        self.calls: Dict[str, int] = {}
        self.errors: Dict[str, str] = {}
        for name, cfg in self.workers_cfg.items():
            cfg = cfg or {}
            self.workers[name] = Worker(
                name=name,
                provider_name=cfg.get("provider", "anthropic"),
                model=cfg.get("model", "?"),
                role=cfg.get("role", cfg.get("description", "")),
            )
            self.calls[name] = 0

    def names(self) -> List[str]:
        return list(self.workers.keys())

    def has(self, name: str) -> bool:
        return name in self.workers

    def _build(self, name: str) -> LLMProvider:
        w = self.workers[name]
        if w._provider is not None:
            return w._provider
        cfg = self.workers_cfg[name] or {}
        pname = cfg.get("provider", "anthropic")
        nested = {k: v for k, v in cfg.items() if k not in ("provider", "role", "description")}
        provider = build_provider({"provider": pname, pname: nested})
        w._provider = provider
        w.model = getattr(provider, "model", w.model)
        return provider

    def provider(self, name: str) -> LLMProvider:
        if name not in self.workers:
            raise KeyError(f"unknown worker '{name}'")
        return self._build(name)

    def roles(self) -> str:
        lines = []
        for n, w in self.workers.items():
            lines.append(f"- {n} ({w.provider_name}/{w.model}): {w.role or 'general'}")
        return "\n".join(lines) if lines else "(no workers configured)"

    # --- single query ------------------------------------------------------
    def ask(self, name: str, user: str, system: str = "") -> str:
        try:
            provider = self.provider(name)
        except SystemExit as e:      # missing key / package
            self.errors[name] = str(e)
            return f"ERROR: worker '{name}' unavailable: {e}"
        except KeyError as e:
            return f"ERROR: {e}"
        msgs: List[Dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": user})
        try:
            turn = provider.complete(msgs, [])
        except Exception as e:
            return f"ERROR: worker '{name}' failed: {e}"
        self.calls[name] = self.calls.get(name, 0) + 1
        return turn.text or "(empty response)"

    def stats(self) -> str:
        return ", ".join(f"{n}:{c}" for n, c in self.calls.items()) or "(no calls yet)"


# ── collaboration protocols ───────────────────────────────────────────────
@dataclass
class Collaboration:
    answer: str
    transcript: List[Dict[str, str]] = field(default_factory=list)

    def as_text(self, include_transcript: bool = True) -> str:
        out = []
        if include_transcript:
            out.append("=== collaboration transcript ===")
            for step in self.transcript:
                out.append(f"\n[{step['worker']} · {step['stage']}]\n{step['text']}")
            out.append("\n=== synthesized answer ===")
        out.append(self.answer)
        return "\n".join(out)


_REFINE_SYS = "You are one expert on a multi-model team. Be rigorous, concrete, and honest about uncertainty."
_CRITIC_SYS = "You are a rigorous reviewer. Find real errors, gaps, and risks. Be specific and constructive; do not rewrite the whole thing."
_SYNTH_SYS = "You are the synthesizer for a multi-model team. Merge the strongest, correct elements from all inputs into one clear, complete final answer. Resolve contradictions using sound judgment."


def run_collaboration(
    pool: WorkerPool,
    task: str,
    workers: List[str],
    mode: str = "refine",
    rounds: int = 2,
    synthesizer: Optional[str] = None,
) -> Collaboration:
    from .util import today_str
    task = f"(Current date: {today_str()}. Base your answer on present-day information, not outdated assumptions.)\n\n{task}"
    workers = [w for w in workers if pool.has(w)]
    if not workers:
        return Collaboration(answer="ERROR: no valid workers for collaboration.")
    if len(workers) == 1:
        ans = pool.ask(workers[0], f"Task:\n{task}\n\nProvide your best complete answer.", _REFINE_SYS)
        return Collaboration(answer=ans, transcript=[{"worker": workers[0], "stage": "solo", "text": ans}])

    synth = synthesizer if (synthesizer and pool.has(synthesizer)) else workers[0]
    transcript: List[Dict[str, str]] = []

    if mode == "panel":
        contributions = {}
        for w in workers:
            a = pool.ask(w, f"Task:\n{task}\n\nProvide your best complete answer.", _REFINE_SYS)
            contributions[w] = a
            transcript.append({"worker": w, "stage": "answer", "text": a})
        merged = _synthesize(pool, synth, task, contributions)
        transcript.append({"worker": synth, "stage": "synthesis", "text": merged})
        return Collaboration(answer=merged, transcript=transcript)

    if mode == "debate":
        positions: Dict[str, str] = {}
        for w in workers:
            a = pool.ask(w, f"Task:\n{task}\n\nGive your answer with reasoning.", _REFINE_SYS)
            positions[w] = a
            transcript.append({"worker": w, "stage": "opening", "text": a})
        for r in range(max(0, rounds - 1)):
            updated = {}
            for w in workers:
                others = "\n\n".join(f"[{o}]: {_cap(positions[o])}" for o in workers if o != w)
                a = pool.ask(
                    w,
                    f"Task:\n{task}\n\nOther models said:\n{others}\n\n"
                    "Address their strongest points, then give your updated answer.",
                    _REFINE_SYS,
                )
                updated[w] = a
                transcript.append({"worker": w, "stage": f"round {r + 2}", "text": a})
            positions = updated
        merged = _synthesize(pool, synth, task, positions)
        transcript.append({"worker": synth, "stage": "synthesis", "text": merged})
        return Collaboration(answer=merged, transcript=transcript)

    # default: refine (propose → critique → revise), looping through workers
    author = workers[0]
    current = pool.ask(author, f"Task:\n{task}\n\nProduce your best complete answer.", _REFINE_SYS)
    transcript.append({"worker": author, "stage": "draft", "text": current})
    for critic in workers[1:]:
        crit = pool.ask(
            critic,
            f"Task:\n{task}\n\nAnother model produced this answer:\n---\n{_cap(current)}\n---\n"
            "Critique it rigorously: list concrete errors, gaps, and improvements. Do not rewrite it.",
            _CRITIC_SYS,
        )
        transcript.append({"worker": critic, "stage": "critique", "text": crit})
        current = pool.ask(
            author,
            f"Task:\n{task}\n\nYour previous answer:\n---\n{_cap(current)}\n---\n"
            f"A reviewer raised:\n---\n{_cap(crit)}\n---\n"
            "Produce an improved final answer that incorporates the valid points.",
            _REFINE_SYS,
        )
        transcript.append({"worker": author, "stage": "revision", "text": current})
    return Collaboration(answer=current, transcript=transcript)


def _synthesize(pool: WorkerPool, synth: str, task: str, contributions: Dict[str, str]) -> str:
    joined = "\n\n".join(f"[{w}]:\n{_cap(t)}" for w, t in contributions.items())
    return pool.ask(
        synth,
        f"Task:\n{task}\n\nCandidate answers from multiple models:\n{joined}\n\n"
        "Produce the single best final answer, merging the strongest correct elements "
        "and fixing any errors.",
        _SYNTH_SYS,
    )
