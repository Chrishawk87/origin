"""Origin REPL — the interactive terminal front-end."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, Optional, Tuple

from .agent import Agent, BUILTIN_PROMPTS, OPERATOR_PROMPT
from .config import Config, load_config
from .llm import LLMProvider, build_provider
from .orchestra import WorkerPool, run_collaboration
from .tools import Registry

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.markdown import Markdown
    from rich.table import Table
    _RICH = True
except ImportError:  # graceful fallback if rich isn't installed
    _RICH = False


# ── persona / brain resolution ────────────────────────────────────────────
def resolve_prompt(config: Config, profile_name: str, profile_cfg: Dict[str, Any]) -> str:
    if profile_cfg.get("system_prompt"):
        return profile_cfg["system_prompt"]
    if profile_name in BUILTIN_PROMPTS:
        return BUILTIN_PROMPTS[profile_name]
    if config.agent.get("system_prompt"):
        return config.agent["system_prompt"]
    return OPERATOR_PROMPT


def build_provider_for(
    config: Config, profile_cfg: Dict[str, Any]
) -> Tuple[LLMProvider, str, str]:
    llm = dict(config.llm)
    prov = (profile_cfg.get("provider") or llm.get("provider") or "anthropic")
    llm["provider"] = prov
    if profile_cfg.get("model"):
        nested = dict(llm.get(prov, {}) or {})
        nested["model"] = profile_cfg["model"]
        llm[prov] = nested
    provider = build_provider(llm)
    return provider, prov, getattr(provider, "model", "?")


def resolve_brain(
    config: Config, profile_cfg: Dict[str, Any], pool: WorkerPool
) -> Tuple[LLMProvider, str, str, Optional[str]]:
    """Choose the coordinator brain. Prefer a named worker (the brain is NOT
    inherently Claude/GPT); fall back to the top-level `llm` block."""
    coord = profile_cfg.get("coordinator") or config.orchestrator.get("coordinator")
    if coord and pool.has(coord):
        provider = pool.provider(coord)  # may raise SystemExit if key/pkg missing
        w = pool.workers[coord]
        return provider, f"{w.provider_name}:{coord}", getattr(provider, "model", "?"), coord
    provider, pname, model = build_provider_for(config, profile_cfg)
    return provider, pname, model, None


# ── UI ──────────────────────────────────────────────────────────────────
class UI:
    def __init__(self, verbosity: str = "normal") -> None:
        self.console = Console() if _RICH else None
        self.verbosity = verbosity

    def print(self, *a, **k) -> None:
        if self.console:
            self.console.print(*a, **k)
        else:
            print(*a)

    def rule(self, title: str = "") -> None:
        if self.console:
            self.console.rule(title)
        else:
            print(f"\n=== {title} ===")

    def banner(self, provider: str, model: str, profile: str, registry: Registry) -> None:
        grouped = registry.by_source()
        counts = ", ".join(f"{k}:{len(v)}" for k, v in grouped.items()) or "none"
        text = (
            f"[bold]Origin[/bold]  ·  brain: [cyan]{provider}[/cyan] "
            f"([cyan]{model}[/cyan])  ·  profile: [magenta]{profile}[/magenta]\n"
            f"connectors online — {counts}\n"
            f"type your request, or /help for commands"
        )
        if self.console:
            self.console.print(Panel(text, border_style="cyan", expand=False))
        else:
            print(f"Origin — {provider}/{model} — profile:{profile} — {counts}\n/help for commands")

    def assistant(self, text: str) -> None:
        if self.console:
            self.console.print(Markdown(text))
        else:
            print(text)

    def tool_start(self, name: str, args: Dict[str, Any]) -> None:
        if self.verbosity == "quiet":
            return
        preview = json.dumps(args, ensure_ascii=False)
        if len(preview) > 200:
            preview = preview[:200] + "…"
        if self.console:
            self.console.print(f"[dim]→ [yellow]{name}[/yellow] {preview}[/dim]")
        else:
            print(f"-> {name} {preview}")

    def tool_result(self, name: str, result: str) -> None:
        if self.verbosity == "quiet":
            return
        cap = 4000 if self.verbosity == "verbose" else 600
        snippet = result if len(result) <= cap else result[:cap] + "…"
        if self.console:
            self.console.print(f"[dim]{snippet}[/dim]")
        else:
            print(snippet)


HELP = """\
Commands:
  /help              show this help
  /tools             list every tool available, grouped by connector
  /connectors        show connector status (shell / REST / MCP)

  Control WHAT the agent may do:
  /allow <tool>      re-enable a tool
  /deny  <tool>      block a tool for this session

  Multiple models (the brain orchestrates workers):
  /models            list model workers + call counts
  /coordinator <w>   set which worker is the lead brain
  /consult <w> <q>   ask one worker directly
  /collab <task>     make all workers collaborate on a task (refine mode)
  /collab:<mode> <task>   collaborate in refine | debate | panel

  Transform Origin into a specialist (ANY domain):
  /roles             list built-in role shortcuts
  /become <anything> become a world-class expert in ANY domain
  /mission <goal>    run the executive loop: plan → act → critique → deliver
  /memory            show what Origin remembers

  Control HOW it behaves:
  /profiles          list behavior profiles
  /profile <name>    switch profile (may swap the brain too)
  /system <text>     set a custom system prompt live (replaces the persona)
  /instruct <text>   add a standing instruction that overrides defaults
  /instructions      show standing instructions
  /clearinstructions remove all standing instructions
  /verbose | /quiet | /normal   set how much tool activity is shown

  Session:
  /reset             clear the conversation (keeps persona + connectors)
  /model             show the active brain
  /exit, /quit       shut down

Anything else you type is sent to the agent, which uses its tools to act.
"""


def _cmd_tools(ui: UI, registry: Registry) -> None:
    grouped = registry.by_source()
    if ui.console:
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("connector")
        table.add_column("tool")
        table.add_column("status")
        for source, names in grouped.items():
            for n in sorted(names):
                status = "on" if registry._permitted(n) else "DENIED"
                table.add_row(source, n, status)
        ui.console.print(table)
    else:
        for source, names in grouped.items():
            print(f"[{source}]")
            for n in sorted(names):
                print(f"  {n} ({'on' if registry._permitted(n) else 'DENIED'})")


def _cmd_profiles(ui: UI, config: Config, active: str) -> None:
    for name, cfg in config.profiles.items():
        mark = "→" if name == active else " "
        desc = (cfg or {}).get("description", "")
        brain = ""
        if (cfg or {}).get("provider") or (cfg or {}).get("model"):
            brain = f"  [brain: {cfg.get('provider','')} {cfg.get('model','')}]".rstrip()
        ui.print(f" {mark} {name}: {desc}{brain}")


def run_repl(args: argparse.Namespace) -> int:
    config = load_config(args.config)

    # apply one-shot CLI overrides
    active_profile = args.profile or config.agent.get("profile", "operator")
    if active_profile not in config.profiles:
        # allow starting in an ad-hoc profile name -> falls back to operator prompt
        config.profiles.setdefault(active_profile, {"description": "(ad-hoc)", "system_prompt": None})

    verbosity = args.verbosity or config.agent.get("verbosity", "normal")
    ui = UI(verbosity)

    profile_cfg = config.profiles.get(active_profile, {}) or {}

    registry = Registry(config)
    ui.print("[dim]booting connectors…[/dim]" if _RICH else "booting connectors…")
    registry.bootstrap()

    coordinator = args.coordinator or None
    if coordinator and registry.pool.has(coordinator):
        profile_cfg = {**profile_cfg, "coordinator": coordinator}
    try:
        provider, prov_name, model_name, coordinator = resolve_brain(config, profile_cfg, registry.pool)
    except SystemExit as e:
        ui.print(f"[red]coordinator unavailable:[/red] {e}\nfalling back to llm block." if _RICH
                 else f"coordinator unavailable: {e}")
        provider, prov_name, model_name = build_provider_for(config, {})
        coordinator = None

    system_prompt = resolve_prompt(config, active_profile, profile_cfg)
    agent = Agent(provider, registry, config, system_prompt=system_prompt, verbosity=verbosity)
    registry.set_research_brain(agent.llm)
    agent.memory = registry.memory
    registry.set_persona_setter(agent.set_system_prompt)

    ui.banner(prov_name, model_name, active_profile, registry)
    if registry.pool.names():
        ui.print(f"[dim]workers: {', '.join(registry.pool.names())}"
                 f"{'  ·  coordinator: ' + coordinator if coordinator else ''}[/dim]"
                 if _RICH else f"workers: {', '.join(registry.pool.names())}")

    if registry.mcp.errors:
        ui.print("[yellow]MCP notes:[/yellow]" if _RICH else "MCP notes:")
        ui.print(registry.mcp.status())

    # one-shot mode
    if args.prompt:
        try:
            agent.run(
                args.prompt,
                on_text=ui.assistant,
                on_tool_start=ui.tool_start,
                on_tool_result=ui.tool_result,
            )
        finally:
            registry.shutdown()
        return 0

    def do_turn(text: str) -> None:
        ui.rule()
        try:
            agent.run(
                text,
                on_text=ui.assistant,
                on_tool_start=ui.tool_start,
                on_tool_result=ui.tool_result,
            )
        except KeyboardInterrupt:
            ui.print("\n[yellow]interrupted[/yellow]" if _RICH else "interrupted")
        except Exception as e:
            ui.print(f"[red]error:[/red] {e}" if _RICH else f"error: {e}")

    try:
        while True:
            try:
                prompt_str = "\n\033[1;36morigin›\033[0m " if _RICH else "\norigin› "
                user_input = input(prompt_str).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user_input:
                continue

            # ── command handling ──
            if user_input in ("/exit", "/quit"):
                break
            if user_input == "/help":
                ui.print(HELP); continue
            if user_input == "/tools":
                _cmd_tools(ui, registry); continue
            if user_input == "/connectors":
                ui.print("shell + REST built in. MCP status:")
                ui.print(registry.mcp.status()); continue
            if user_input == "/profiles":
                _cmd_profiles(ui, config, active_profile); continue
            if user_input == "/roles":
                from .roles import ROLES
                for n in ROLES:
                    ui.print(f"  {n}")
                ui.print("[dim]…or /become <anything> for any domain.[/dim]" if _RICH
                         else "…or /become <anything>")
                continue
            if user_input == "/memory":
                ui.print(registry.memory.summary()); continue
            if user_input.startswith("/role ") or user_input.startswith("/become "):
                from .roles import resolve_persona
                name = user_input.split(" ", 1)[1].strip()
                agent.set_system_prompt(resolve_persona(name))
                ui.print(f"[green]Origin is now a world-class expert in {name}[/green]"
                         if _RICH else f"became: {name}")
                continue
            if user_input.startswith("/mission "):
                from .executive import run_mission
                goal = user_input.split(" ", 1)[1].strip()
                def _ask(p, s=""):
                    msgs = ([{"role": "system", "content": s}] if s else []) + [{"role": "user", "content": p}]
                    return agent.llm.complete(msgs, []).text or ""
                ui.rule("mission")
                run_mission(agent, goal, _ask,
                            on_event=lambda e: ui.print(f"[dim]{e.get('type')}: "
                                                        f"{e.get('step') or e.get('name') or ''}[/dim]" if _RICH else str(e.get('type'))))
                continue
            if user_input == "/reset":
                agent.reset()
                ui.print("[dim]conversation cleared[/dim]" if _RICH else "conversation cleared"); continue
            if user_input == "/model":
                coord = f"  coordinator: {coordinator}" if coordinator else ""
                ui.print(f"{prov_name} / {model_name}  (profile: {active_profile}){coord}"); continue
            if user_input == "/models":
                if registry.pool.names():
                    ui.print("Model workers:")
                    ui.print(registry.pool.roles())
                    ui.print(f"[dim]calls — {registry.pool.stats()}[/dim]" if _RICH
                             else f"calls — {registry.pool.stats()}")
                else:
                    ui.print("No workers configured; using the single `llm` brain. "
                             "Add a `workers:` section to enable multi-model orchestration.")
                continue
            if user_input in ("/verbose", "/quiet", "/normal"):
                verbosity = user_input.strip("/")
                ui.verbosity = verbosity; agent.verbosity = verbosity
                ui.print(f"[dim]verbosity: {verbosity}[/dim]" if _RICH else f"verbosity: {verbosity}"); continue
            if user_input == "/instructions":
                if agent.standing_instructions:
                    for i, s in enumerate(agent.standing_instructions, 1):
                        ui.print(f"  {i}. {s}")
                else:
                    ui.print("(none)")
                continue
            if user_input == "/clearinstructions":
                agent.clear_instructions()
                ui.print("[dim]standing instructions cleared[/dim]" if _RICH else "cleared"); continue

            if user_input.startswith("/profile "):
                name = user_input.split(" ", 1)[1].strip()
                if name not in config.profiles:
                    ui.print(f"unknown profile '{name}'. /profiles to list."); continue
                active_profile = name
                pcfg = config.profiles.get(name, {}) or {}
                try:
                    provider, prov_name, model_name, coordinator = resolve_brain(config, pcfg, registry.pool)
                    agent.llm = provider
                except SystemExit as e:
                    ui.print(f"[red]{e}[/red]" if _RICH else str(e)); continue
                agent.set_system_prompt(resolve_prompt(config, name, pcfg))
                ui.print(f"[green]switched to '{name}'[/green] — brain: {prov_name}/{model_name}"
                         if _RICH else f"switched to '{name}' — {prov_name}/{model_name}")
                continue
            if user_input.startswith("/coordinator "):
                name = user_input.split(" ", 1)[1].strip()
                if not registry.pool.has(name):
                    ui.print(f"unknown worker '{name}'. /models to list."); continue
                try:
                    provider = registry.pool.provider(name)
                    agent.llm = provider
                    coordinator = name
                    prov_name = f"{registry.pool.workers[name].provider_name}:{name}"
                    model_name = getattr(provider, "model", "?")
                    ui.print(f"[green]coordinator brain is now '{name}'[/green] ({prov_name}/{model_name})"
                             if _RICH else f"coordinator: {name}")
                except SystemExit as e:
                    ui.print(f"[red]{e}[/red]" if _RICH else str(e))
                continue
            if user_input.startswith("/consult "):
                rest = user_input.split(" ", 1)[1].strip()
                parts = rest.split(" ", 1)
                if len(parts) < 2:
                    ui.print("usage: /consult <worker> <question>"); continue
                w, q = parts[0], parts[1]
                if not registry.pool.has(w):
                    ui.print(f"unknown worker '{w}'. /models to list."); continue
                ui.rule(f"consulting {w}")
                ui.assistant(registry.pool.ask(w, q))
                continue
            if user_input.startswith("/collab"):
                head, _, task = user_input.partition(" ")
                mode = head.split(":", 1)[1] if ":" in head else "refine"
                if not task.strip():
                    ui.print("usage: /collab <task>   (or /collab:debate <task>)"); continue
                if len(registry.pool.names()) < 1:
                    ui.print("no workers configured; add a `workers:` section."); continue
                ui.rule(f"collaboration ({mode})")
                result = run_collaboration(
                    registry.pool, task.strip(), registry.pool.names(),
                    mode=mode,
                    synthesizer=config.orchestrator.get("synthesizer"),
                )
                ui.assistant(result.as_text(include_transcript=(verbosity != "quiet")))
                ui.print(f"[dim]calls — {registry.pool.stats()}[/dim]" if _RICH
                         else f"calls — {registry.pool.stats()}")
                continue
            if user_input.startswith("/system "):
                agent.set_system_prompt(user_input.split(" ", 1)[1])
                ui.print("[green]system prompt updated[/green]" if _RICH else "system prompt updated"); continue
            if user_input.startswith("/instruct "):
                agent.add_instruction(user_input.split(" ", 1)[1])
                ui.print("[green]standing instruction added[/green]" if _RICH else "instruction added"); continue
            if user_input.startswith("/deny "):
                t = user_input.split(" ", 1)[1].strip()
                registry.deny_tool(t)
                ui.print(f"[yellow]denied tool '{t}'[/yellow]" if _RICH else f"denied {t}"); continue
            if user_input.startswith("/allow "):
                t = user_input.split(" ", 1)[1].strip()
                registry.allow_tool(t)
                ui.print(f"[green]allowed tool '{t}'[/green]" if _RICH else f"allowed {t}"); continue
            if user_input.startswith("/"):
                ui.print(f"unknown command '{user_input}'. /help for options."); continue

            do_turn(user_input)
    finally:
        registry.shutdown()

    ui.print("[dim]Origin shut down.[/dim]" if _RICH else "Origin shut down.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="origin",
        description="Origin — an autonomous agent that unifies shell, MCP servers, and REST APIs.",
    )
    parser.add_argument("-c", "--config", help="Path to a config YAML file.")
    parser.add_argument("-p", "--prompt", help="Run a single request non-interactively and exit.")
    parser.add_argument("--profile", help="Start in a named behavior profile.")
    parser.add_argument("--coordinator", help="Worker name to use as the lead brain.")
    parser.add_argument("--doctor", action="store_true", help="Check/set up Origin (Ollama, browser, keys) and exit.")
    parser.add_argument(
        "--verbosity", choices=["quiet", "normal", "verbose"],
        help="How much tool activity to display.",
    )
    args = parser.parse_args(argv)
    if args.doctor:
        from .bootstrap import doctor
        return doctor()
    try:
        return run_repl(args)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
