"""Model-to-model connector.

Exposes the other LLM workers to the coordinator brain as tools, so a single
task can pull in Claude *and* GPT (and/or a local model) — having them consult
and collaborate with each other — without leaving the hub.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..orchestra import WorkerPool, run_collaboration
from .base import Tool


def build_model_tools(pool: WorkerPool, orchestrator_cfg: Dict[str, Any]) -> List[Tool]:
    if not pool.names():
        return []

    default_workers = orchestrator_cfg.get("collab_workers") or pool.names()
    default_synth = orchestrator_cfg.get("synthesizer")

    def list_models(_args: Dict[str, Any]) -> str:
        return "Available model workers:\n" + pool.roles() + f"\n\ncalls so far — {pool.stats()}"

    def consult(args: Dict[str, Any]) -> str:
        worker = args.get("worker")
        prompt = args.get("prompt", "")
        if not worker:
            return "ERROR: 'worker' is required. Known: " + ", ".join(pool.names())
        if not pool.has(worker):
            return f"ERROR: unknown worker '{worker}'. Known: " + ", ".join(pool.names())
        return pool.ask(worker, prompt, args.get("system", ""))

    def collaborate(args: Dict[str, Any]) -> str:
        task = args.get("task", "")
        if not task:
            return "ERROR: 'task' is required."
        workers = args.get("workers") or default_workers
        mode = args.get("mode", "refine")
        rounds = int(args.get("rounds", 2))
        synth = args.get("synthesizer") or default_synth
        result = run_collaboration(pool, task, workers, mode=mode, rounds=rounds, synthesizer=synth)
        include = bool(args.get("show_transcript", True))
        return result.as_text(include_transcript=include)

    tools = [
        Tool(
            name="list_models",
            description=(
                "List the model workers available for consultation/collaboration "
                "(Claude, GPT, local, …) with their roles and how many times each "
                "has been called this session."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=list_models,
            source="model",
        ),
        Tool(
            name="consult",
            description=(
                "Ask ONE specific model worker a question and get its answer. Use to "
                "delegate a sub-task to the model best suited for it (e.g. route cheap "
                "bulk work to the local model, hard reasoning to Claude)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "worker": {"type": "string", "description": "Worker name (see list_models)."},
                    "prompt": {"type": "string", "description": "The question/task for that worker."},
                    "system": {"type": "string", "description": "Optional system instruction for that worker."},
                },
                "required": ["worker", "prompt"],
            },
            handler=consult,
            source="model",
        ),
        Tool(
            name="collaborate",
            description=(
                "Have MULTIPLE model workers work on one task together and return a "
                "single merged answer. Modes: 'refine' (one proposes, others critique, "
                "author revises), 'debate' (models exchange and defend over rounds), "
                "'panel' (each answers independently, a synthesizer merges). Use for a "
                "job that's hard for any single model alone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "The task for the models to collaborate on."},
                    "workers": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Worker names to involve (default: all).",
                    },
                    "mode": {"type": "string", "enum": ["refine", "debate", "panel"]},
                    "rounds": {"type": "integer", "description": "Exchange rounds for debate (default 2)."},
                    "synthesizer": {"type": "string", "description": "Worker that merges the final answer."},
                    "show_transcript": {"type": "boolean", "description": "Include the exchange transcript (default true)."},
                },
                "required": ["task"],
            },
            handler=collaborate,
            source="model",
        ),
    ]
    return tools
