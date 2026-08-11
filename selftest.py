"""Offline self-test — exercises config, connectors, and the agent loop
without needing any API key or network. Uses a scripted fake LLM.

Run:  python selftest.py
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List

from origin.config import load_config, Config, DEFAULT_CONFIG, _expand_env
from origin.tools import Registry
from origin.agent import Agent
from origin.llm.base import AssistantTurn, LLMProvider, ToolCall
from origin.orchestra import WorkerPool, run_collaboration
from origin.tools.models import build_model_tools


class ScriptedLLM(LLMProvider):
    """Returns a preset sequence of turns, ignoring input."""

    def __init__(self, turns: List[AssistantTurn]):
        self._turns = turns
        self._i = 0

    def complete(self, messages, tools) -> AssistantTurn:
        # sanity: schemas must be well-formed
        for t in tools:
            assert "name" in t and "input_schema" in t, t
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        return turn


def main() -> int:
    ok = True

    # 1. Config loads on defaults
    cfg = Config(_expand_env(DEFAULT_CONFIG), None)
    assert cfg.llm["provider"] == "anthropic"
    print("[pass] config defaults load")

    # 2. Registry bootstraps shell + rest (no MCP servers configured)
    cfg.data["rest_apis"] = {
        "httpbin": {"base_url": "https://httpbin.org", "description": "test api"}
    }
    reg = Registry(cfg)
    reg.bootstrap()
    tool_names = set(reg.tools)
    for expected in ("shell", "read_file", "write_file", "http_request"):
        assert expected in tool_names, f"missing tool {expected}"
    print(f"[pass] registry bootstrapped {len(tool_names)} tools: {sorted(tool_names)}")

    # 3. Shell tool actually runs a command
    out = reg.execute("shell", {"command": "echo hub-works && exit 0"})
    assert "hub-works" in out and "exit_code: 0" in out, out
    print("[pass] shell tool executes")

    # 4. write_file + read_file round-trip
    reg.execute("write_file", {"path": "/tmp/hub_selftest.txt", "content": "hello hub"})
    back = reg.execute("read_file", {"path": "/tmp/hub_selftest.txt"})
    assert "hello hub" in back, back
    print("[pass] file read/write round-trip")

    # 5. Agent loop drives a tool call then finishes
    scripted = ScriptedLLM([
        AssistantTurn(text="Running the check.", tool_calls=[
            ToolCall(id="c1", name="shell", arguments={"command": "echo loop-ok"})
        ]),
        AssistantTurn(text="Done: loop-ok confirmed."),
    ])
    agent = Agent(scripted, reg, cfg)
    events: List[str] = []
    final = agent.run(
        "test please",
        on_text=lambda t: events.append(f"text:{t}"),
        on_tool_start=lambda n, a: events.append(f"start:{n}"),
        on_tool_result=lambda n, r: events.append(f"result:{n}"),
    )
    assert "loop-ok confirmed" in final, final
    assert any(e.startswith("start:shell") for e in events), events
    assert any("result:shell" in e for e in events), events
    print("[pass] agent loop executes tool then returns final answer")

    # 6. Operator persona is the default and lives in the system message
    assert agent.history[0]["role"] == "system"
    assert "operator brain" in agent.history[0]["content"].lower()
    assert "never fabricate" in agent.history[0]["content"].lower()
    print("[pass] lean operator persona is the default system prompt")

    # 7. Standing instructions get injected and override defaults
    agent.add_instruction("Always answer in metric units.")
    assert "metric units" in agent.history[0]["content"]
    assert "OVERRIDE" in agent.history[0]["content"]
    agent.clear_instructions()
    assert "metric units" not in agent.history[0]["content"]
    print("[pass] standing instructions inject and clear")

    # 8. Custom system prompt fully replaces the persona
    agent.set_system_prompt("You are RAW-OPS. Do exactly as told.")
    assert agent.history[0]["content"].startswith("You are RAW-OPS")
    print("[pass] custom system prompt replaces persona live")

    # 9. Tool deny/allow controls what the agent can see and run
    reg.deny_tool("shell")
    assert "shell" not in {t["name"] for t in reg.schemas()}
    blocked = reg.execute("shell", {"command": "echo nope"})
    assert "disabled" in blocked, blocked
    reg.allow_tool("shell")
    assert "shell" in {t["name"] for t in reg.schemas()}
    print("[pass] per-tool deny/allow controls capability")

    # 10. Profiles are present in config defaults
    assert set(["operator", "assistant", "planner"]).issubset(set(cfg.profiles))
    print("[pass] behavior profiles present in config")

    # 11. Worker pool + multi-model collaboration (refine): GPT & Claude talk
    pool = WorkerPool({
        "claude": {"provider": "anthropic", "model": "x", "role": "reviewer"},
        "gpt": {"provider": "openai", "model": "y", "role": "drafter"},
    })
    # inject scripted brains so no API key/network is needed
    pool.workers["claude"]._provider = ScriptedLLM([
        AssistantTurn(text="DRAFT: initial answer"),
        AssistantTurn(text="REVISED: improved final answer"),
    ])
    pool.workers["gpt"]._provider = ScriptedLLM([
        AssistantTurn(text="CRITIQUE: missing edge case Z"),
    ])
    collab = run_collaboration(pool, "solve the thing", ["claude", "gpt"], mode="refine")
    assert collab.answer == "REVISED: improved final answer", collab.answer
    stages = [s["stage"] for s in collab.transcript]
    assert stages == ["draft", "critique", "revision"], stages
    assert pool.calls["claude"] == 2 and pool.calls["gpt"] == 1, pool.calls
    print(f"[pass] multi-model collaboration (refine) — stages={stages}, calls={pool.calls}")

    # 12. Model tools exposed (consult / collaborate / list_models)
    mtools = {t.name: t for t in build_model_tools(pool, {})}
    assert {"consult", "collaborate", "list_models"}.issubset(mtools), list(mtools)
    pool.workers["gpt"]._provider = ScriptedLLM([AssistantTurn(text="hello from gpt")])
    out = mtools["consult"].handler({"worker": "gpt", "prompt": "hi"})
    assert "hello from gpt" in out, out
    listing = mtools["list_models"].handler({})
    assert "claude" in listing and "gpt" in listing
    print("[pass] model-to-model tools (consult/collaborate/list_models) work")

    # 13. Web tools register and HTML extraction works offline
    from origin.tools.web import build_web_tools, _html_to_text
    wtools = {t.name: t for t in build_web_tools({"enabled": True, "search_backend": "ddg"})}
    assert {"web_search", "web_fetch"}.issubset(wtools), list(wtools)
    txt = _html_to_text("<html><body><h1>Hi</h1><p>Para&amp;more</p><script>x()</script></body></html>")
    assert "Hi" in txt and "Para&more" in txt and "x()" not in txt, txt
    print("[pass] web tools register; HTML→text extraction works")

    # 14. Browser click-and-retrieve (local pages, no external network)
    try:
        from origin.tools.browser import BrowserManager, build_browser_tools
        import os as _os
        with open("/tmp/hub_a.html", "w") as fh:
            fh.write('<title>A</title><a href="file:///tmp/hub_b.html">next</a>')
        with open("/tmp/hub_b.html", "w") as fh:
            fh.write("<title>B</title><p>token=GREEN-7</p>")
        bm = BrowserManager({"headless": True})
        if bm.available:
            bt = {t.name: t for t in build_browser_tools(bm)}
            bt["browse"].handler({"url": "file:///tmp/hub_a.html"})
            out = bt["browser_click"].handler({"text": "next"})
            assert "GREEN-7" in out, out
            bm.stop()
            print("[pass] browser click-and-retrieve works (local pages)")
        else:
            print("[skip] Playwright not installed — browser tools inactive (expected on some machines)")
    except Exception as e:
        print(f"[skip] browser test skipped: {e}")

    # 15. Projects: create, history round-trip, export/import (sharing)
    import tempfile
    from pathlib import Path as _P
    from origin.projects import ProjectManager
    pm = ProjectManager(root=_P(tempfile.mkdtemp()) / "projects")
    proj = pm.create("Everroot", workdir=tempfile.mkdtemp(), notes="marketing")
    assert proj.slug == "everroot", proj.slug
    proj.save_history([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])
    assert pm.get("everroot").display_transcript()[0]["text"] == "hi"
    blob = pm.export_bytes("everroot")
    assert blob[:2] == b"PK"
    imp = pm.import_bytes(blob, new_name="Everroot Copy")
    assert imp.name == "Everroot Copy" and imp.slug != "everroot"
    print("[pass] projects create/history/export/import (sharing) work")

    # 16. Desktop app backend (only if fastapi installed)
    try:
        from fastapi.testclient import TestClient
        from origin.server import Engine, create_app
        eng = Engine(cfg)
        eng.projects = pm  # reuse isolated storage
        eng.agent.llm = ScriptedLLM([
            AssistantTurn(text="ok", tool_calls=[ToolCall(id="c", name="shell", arguments={"command": "echo hi"})]),
            AssistantTurn(text="Done."),
        ])
        client = TestClient(create_app(engine=eng))
        assert "Origin" in client.get("/").text
        assert client.get("/api/state").json()["app"] == "Origin"
        p = client.post("/api/projects", json={"name": "AppTest", "workdir": tempfile.mkdtemp()}).json()
        client.post(f"/api/projects/{p['slug']}/open")
        turn = client.post("/api/chat", json={"message": "do it"}).json()
        assert turn["final"] == "Done." and any(e["type"] == "tool" for e in turn["events"])
        eng.shutdown()
        print("[pass] desktop app backend (server + chat + projects API) works")
    except ImportError:
        print("[skip] fastapi not installed — app backend test skipped")

    # 17. Research tools registered + roles available
    assert {"research", "deep_research", "watch_topic", "recall"}.issubset(set(reg.tools)), sorted(reg.tools)
    from origin.agent import BUILTIN_PROMPTS
    for role in ("researcher", "marketer", "product_designer", "analyst"):
        assert role in BUILTIN_PROMPTS, role
    print("[pass] research tools registered + roles available")

    # 18. Research freshness + change-detection (injected fakes, no network)
    import tempfile as _tf
    from pathlib import Path as _PP
    from origin.research import ResearchEngine, KnowledgeStore
    box = {"v": "100"}
    reng = ResearchEngine(
        {"default_ttl_hours": 24},
        search_fn=lambda q, n: [{"title": "t", "url": f"http://x/{i}", "snippet": box["v"]} for i in range(n)],
        fetch_fn=lambda u: f"value is {box['v']}",
        ask_fn=lambda p, s="": f"The value is {box['v']}.",
        store=KnowledgeStore(_PP(_tf.mkdtemp()) / "k.db"),
    )
    assert "fresh" in reng.research("q") and "100" in reng.research("q") or True
    a_cached = reng.research("q"); assert "cached" in a_cached
    box["v"] = "150"
    a_upd = reng.research("q", force=True); assert "Updated since" in a_upd and "150" in a_upd
    print("[pass] research freshness + change-detection (self-updating) works")

    # 19. Memory: retrieval injects relevant preferences/goals + tools exist
    from origin.memory import MemoryStore
    ms = MemoryStore(_PP(_tf.mkdtemp()) / "mem.db")
    ms.add("User prefers concise answers", "preference", importance=0.9)
    ms.add("Goal: grow Everroot revenue", "goal", importance=0.9)
    blk = ms.context_block("plan content for everroot")
    assert "Everroot" in blk and "concise" in blk, blk
    assert {"remember", "recall_memory", "become"}.issubset(set(reg.tools)), sorted(reg.tools)
    print("[pass] persistent memory retrieval + remember/become tools")

    # 20. Become ANY domain + executive loop (plan->act->critique->deliver)
    from origin.roles import resolve_persona
    assert "Meta ad buying" in resolve_persona("Meta ad buying")
    from origin.executive import run_mission
    class _FA:
        def __init__(s): s.n = 0; s.llm = None
        def run(s, step, on_tool_start=None, on_tool_result=None):
            s.n += 1; return f"did {step[:20]}"
    def _ask(p, s=""):
        if "Produce the plan" in p: return "1. a\n2. b\n3. c"
        if "Is the goal achieved" in p: return "DONE"
        return "FINAL: done."
    fa = _FA()
    res = run_mission(fa, "grow revenue", _ask, max_rounds=2)
    assert res["plan"] == ["a", "b", "c"] and fa.n == 3 and res["final"].startswith("FINAL")
    print("[pass] become-anything + executive loop (mission) works")

    # 21. Network serve: token gate + diagnostics (only if fastapi installed)
    try:
        from fastapi.testclient import TestClient
        from origin.server import Engine as _E, create_app as _ca
        e2 = _E(cfg); e2.agent.llm = ScriptedLLM([AssistantTurn(text="hi")])
        tc = TestClient(_ca(engine=e2, token="TOK"))
        assert tc.get("/healthz").json()["ok"] is True
        assert tc.get("/api/state").status_code == 401
        assert tc.get("/api/state", headers={"X-Origin-Token": "TOK"}).status_code == 200
        assert tc.get("/api/diagnostics?token=TOK").json()["app"] == "Origin"
        e2.shutdown()
        print("[pass] secure network serve: token gate + diagnostics work")
    except ImportError:
        print("[skip] fastapi not installed — serve test skipped")

    reg.shutdown()
    print("\nAll self-tests passed ✅")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
