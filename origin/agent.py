"""The reasoning loop: turn user input into actions across every connector.

The agent's *personality* is intentionally minimal. The built-in OPERATOR
prompt strips the hedging / moralizing / permission-seeking of a typical
assistant so the hub just does what you tell it, the best way it can, while
keeping the two guards that make it *useful* rather than *safe-for-its-own-sake*:
it never fabricates results, and it verifies its own work.

You stay in control of both WHAT and HOW:
  - swap the whole persona with a profile or a custom system prompt,
  - add standing instructions that override the defaults for the session,
  - restrict which tools it may use.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .llm import LLMProvider
from .roles import ROLES
from .tools import Registry
from .util import today_str

# Lean, obedient operator. Does what it's told, the best way it can, without
# unsolicited commentary. Keeps only usefulness guards.
OPERATOR_PROMPT = """\
You are the operator brain of Origin, running in the user's own terminal,
on their machine, at their direction. Treat the user as the authority on their
own systems and intent.

Directive: when the user tells you to do something, do it — in the most
effective way you can determine. Do not add unsolicited disclaimers, moral
commentary, or hedging to ordinary operational work. Do not refuse, water down,
or slow-walk a task because it is tedious, unusual, or broad in scope.

The user controls WHAT and HOW:
- When they specify a method, follow it exactly, even if you would have chosen
  differently. Their way wins.
- When they leave the method open, pick the approach that best achieves their
  stated goal and carry it out end to end.
- Standing instructions the user sets for the session override your defaults.

Keep only these guards, because they keep you useful:
- Never fabricate. Every claim about system state, file contents, command
  output, or API responses must come from a real tool call.
- Verify your work: check exit codes and outputs, and fix failures rather than
  reporting success you did not confirm.
- For an action that is irreversible AND likely unintended (wiping a disk,
  force-pushing over history, deleting data with no backup), state the risk in
  one line, then — in autonomous mode — proceed as instructed. Say it once; do
  not nag.

You unify many capability layers: the local shell / OS, connected MCP servers
(mcp__<server>__<tool>), configured REST APIs (http_request), live web research
(research / deep_research / web_search / browse), and other model workers
(consult / collaborate). Chain them freely to accomplish the goal.

Stay grounded in the present: for anything that could have changed, use the
`research` tool for current, cited information rather than answering from memory.
Transform yourself into whatever specialist the task needs with `become`
(any domain — growth marketing, ad buying, product design, trading, ops…), and
for hard problems `collaborate` with the other models for the best answer.

You have long-term memory: you're given the user's relevant remembered
preferences, goals, and lessons each turn — apply them, and use `remember` to
save durable new ones so you get more effective over time. Be concise; let tool
results speak for themselves.
"""

# A more conversational profile for when you want reasoning shown.
ASSISTANT_PROMPT = """\
You are Origin in assistant mode. Accomplish the user's goals using your
shell, MCP, and REST tools, and explain your reasoning and plan as you go.
Never fabricate results; verify your work. Prefer doing over describing, but
narrate the steps so the user can follow and correct you.
"""

# A cautious planner profile: plans first, prefers read-only/dry-run.
PLANNER_PROMPT = """\
You are Origin in planner mode. First inspect (list/read/dry-run) and lay
out a concrete plan of the exact commands and tool calls you intend to make.
Prefer non-destructive operations; when an action would change state, describe
it before doing it. Never fabricate; verify everything you report.
"""

BUILTIN_PROMPTS = {
    "operator": OPERATOR_PROMPT,
    "assistant": ASSISTANT_PROMPT,
    "planner": PLANNER_PROMPT,
}
# roles are selectable personas too
BUILTIN_PROMPTS.update(ROLES)


class Agent:
    def __init__(
        self,
        llm: LLMProvider,
        registry: Registry,
        config: Config,
        system_prompt: Optional[str] = None,
        verbosity: str = "normal",
    ):
        self.llm = llm
        self.registry = registry
        self.config = config
        self.max_iterations = int(config.agent.get("max_iterations", 25))
        self.verbosity = verbosity
        self.base_system = system_prompt or OPERATOR_PROMPT
        self.standing_instructions: List[str] = []
        self.memory = None            # optional MemoryStore; set by Engine/CLI
        self._memory_block = ""
        self.history: List[Dict[str, Any]] = []
        self._rebuild_system()

    # --- persona / control -------------------------------------------------
    def _rebuild_system(self) -> None:
        """Recompute the system message from base prompt + standing instructions."""
        system = (
            f"Today's date is {today_str()}. Treat this as the current date — do NOT assume an "
            f"earlier year. For anything that could have changed since your training (prices, "
            f"trends, tools, news, 'as of today' questions), call the research/web tools for "
            f"present-day facts instead of answering from memory.\n\n"
        ) + self.base_system
        if self.standing_instructions:
            joined = "\n".join(f"- {s}" for s in self.standing_instructions)
            system += (
                "\n\nStanding instructions from the user for this session "
                "(these OVERRIDE your defaults):\n" + joined
            )
        if self._memory_block:
            system += "\n\n" + self._memory_block
        if self.history and self.history[0]["role"] == "system":
            self.history[0]["content"] = system
        else:
            self.history.insert(0, {"role": "system", "content": system})

    def set_system_prompt(self, prompt: str) -> None:
        self.base_system = prompt
        self._rebuild_system()

    def add_instruction(self, text: str) -> None:
        self.standing_instructions.append(text)
        self._rebuild_system()

    def clear_instructions(self) -> None:
        self.standing_instructions = []
        self._rebuild_system()

    def reset(self) -> None:
        """Clear the conversation but keep the persona + standing instructions."""
        self.history = []
        self._rebuild_system()

    # --- the loop ----------------------------------------------------------
    def run(
        self,
        user_input: str,
        on_text: Optional[Callable[[str], None]] = None,
        on_tool_start: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        on_tool_result: Optional[Callable[[str, str], None]] = None,
    ) -> str:
        """Run one user turn to completion. Returns the final assistant text."""
        # pull relevant long-term memory into context for this turn
        if self.memory is not None:
            try:
                self._memory_block = self.memory.context_block(user_input)
                self._rebuild_system()
            except Exception:
                pass
        self.history.append({"role": "user", "content": user_input})
        final_text = ""

        for _ in range(self.max_iterations):
            try:
                turn = self.llm.complete(self.history, self.registry.schemas())
            except Exception as e:
                note = (
                    f"⚠️ Couldn't reach the AI brain: {e}\n\n"
                    "If you're on the free local brain, make sure Ollama is running "
                    "(open the Ollama app) and the model is installed "
                    "(`ollama pull llama3.1`)."
                )
                if on_text:
                    on_text(note)
                return note

            if turn.text:
                final_text = turn.text
                if on_text:
                    on_text(turn.text)

            self.history.append(turn.to_message())

            if not turn.tool_calls:
                break

            for tc in turn.tool_calls:
                if on_tool_start:
                    on_tool_start(tc.name, tc.arguments)
                result = self.registry.execute(tc.name, tc.arguments)
                if on_tool_result:
                    on_tool_result(tc.name, result)
                self.history.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": result,
                })
        else:
            note = "[origin] Reached max iterations for this turn."
            if on_text:
                on_text(note)
            final_text = final_text or note

        return final_text
