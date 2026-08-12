"""Origin desktop backend — a small local web server the app window talks to.

Wraps the whole engine (workers, tools, web, browser, shell) plus projects, and
exposes it over a localhost HTTP API consumed by the bundled web UI.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Dict, List, Optional

from .agent import Agent
from .config import Config, load_config
from .llm import build_provider
from .llm.base import AssistantTurn, LLMProvider
from .projects import Project, ProjectManager
from .tools import Registry


class _NullProvider(LLMProvider):
    """Stands in when no LLM brain is configured, so the app still runs."""

    name = "none"

    def __init__(self, reason: str):
        self.reason = reason or "no LLM configured"
        self.model = "none"

    def complete(self, messages, tools) -> AssistantTurn:
        return AssistantTurn(text=(
            f"⚠️ No AI brain is configured ({self.reason}). Set the coordinator to a "
            "running Ollama model, or add an API key for a worker, then reopen Origin."
        ))


class Engine:
    """Everything the UI drives, in one object."""

    def __init__(self, config: Config):
        self.config = config
        self.registry = Registry(config)
        self.registry.bootstrap()
        self.pool = self.registry.pool
        self.projects = ProjectManager()
        self.active: Optional[Project] = None
        self.coordinator: Optional[str] = None
        self.brain_error: Optional[str] = None
        self.role: Optional[str] = None
        self.agent = Agent(self._resolve_provider(), self.registry, config, verbosity="normal")
        self.registry.set_research_brain(self.agent.llm)
        self.agent.memory = self.registry.memory
        self.registry.set_persona_setter(self.agent.set_system_prompt)
        self._start_daily_refresh()

    def _ask(self, prompt: str, system: str = "") -> str:
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        try:
            return self.agent.llm.complete(msgs, []).text or ""
        except Exception as e:
            return f"(brain error: {e})"

    def mission(self, goal: str) -> Dict[str, Any]:
        from .executive import run_mission
        events: List[Dict[str, Any]] = []
        res = run_mission(self.agent, goal, self._ask, on_event=lambda e: events.append(e))
        if self.active:
            self.active.save_history(self.agent.history)
        return {"events": events, **res}

    def _start_daily_refresh(self) -> None:
        if not self.config.data.get("research", {}).get("daily_refresh", True):
            return
        import threading

        def loop():
            import time as _t
            while True:
                _t.sleep(24 * 3600)
                try:
                    self.registry.research_engine.refresh_watches()
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True).start()

    def set_role(self, name: str) -> Dict[str, Any]:
        """Adopt a known role OR become a world-class expert in ANY domain."""
        from .roles import resolve_persona
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "empty role"}
        self.agent.set_system_prompt(resolve_persona(name))
        self.role = name
        return {"ok": True, "role": name}

    def _resolve_provider(self) -> LLMProvider:
        """Prefer the configured coordinator; if its key/engine is missing, fall
        back to any other worker that builds, then the top-level llm block. This
        makes deployments robust to whichever API key is actually provided."""
        coord = self.config.orchestrator.get("coordinator")
        order = ([coord] if coord else []) + [w for w in self.pool.names() if w != coord]
        for name in order:
            if not self.pool.has(name):
                continue
            try:
                provider = self.pool.provider(name)
                self.coordinator = name
                self.brain_error = None
                return provider
            except SystemExit as e:
                self.brain_error = str(e)
                continue
        try:
            return build_provider(self.config.llm)
        except SystemExit as e:
            self.brain_error = str(e)
            return _NullProvider(str(e))

    # ── projects ───────────────────────────────────────────────────────────
    def open_project(self, slug: str) -> Project:
        proj = self.projects.get(slug)
        if not proj:
            raise KeyError(slug)
        self.active = proj
        try:
            os.makedirs(proj.workdir, exist_ok=True)
            os.chdir(proj.workdir)
        except Exception:
            pass
        self.agent.reset()
        self.agent.history += proj.load_history()
        return proj

    def create_project(self, name: str, workdir: Optional[str], notes: str = "") -> Project:
        return self.projects.create(name, workdir=workdir, notes=notes)

    # ── chat ────────────────────────────────────────────────────────────────
    def chat(self, text: str) -> Dict[str, Any]:
        events: List[Dict[str, Any]] = []
        final = self.agent.run(
            text,
            on_text=lambda t: events.append({"type": "text", "text": t}),
            on_tool_start=lambda n, a: events.append({"type": "tool", "name": n, "args": a}),
            on_tool_result=lambda n, r: events.append({"type": "result", "name": n, "result": r[:4000]}),
        )
        if self.active:
            self.active.save_history(self.agent.history)
        return {"events": events, "final": final, "calls": self.pool.stats()}

    def set_coordinator(self, name: str) -> Dict[str, Any]:
        if not self.pool.has(name):
            return {"ok": False, "error": f"unknown worker '{name}'"}
        try:
            self.agent.llm = self.pool.provider(name)
            self.registry.set_research_brain(self.agent.llm)
            self.coordinator = name
            self.brain_error = None
            return {"ok": True, "coordinator": name}
        except SystemExit as e:
            return {"ok": False, "error": str(e)}

    def state(self) -> Dict[str, Any]:
        return {
            "app": "Origin",
            "active": self.active.to_dict() if self.active else None,
            "transcript": self.active.display_transcript() if self.active else [],
            "projects": [p.to_dict() for p in self.projects.list()],
            "workers": self.pool.names(),
            "worker_roles": self.pool.roles(),
            "coordinator": self.coordinator,
            "role": self.role,
            "roles": __import__("origin.roles", fromlist=["role_names"]).role_names(),
            "memory_count": len(self.registry.memory.all()),
            "tools": self.registry.by_source(),
            "mcp_status": self.registry.mcp.status(),
            "brain_error": self.brain_error,
            "calls": self.pool.stats(),
            "presets": self.config.presets,
        }

    def shutdown(self) -> None:
        self.registry.shutdown()


# ── FastAPI app ─────────────────────────────────────────────────────────────
def create_app(config: Optional[Config] = None, engine: Optional[Engine] = None,
               token: Optional[str] = None):
    try:
        from fastapi import Body, FastAPI
        from fastapi.responses import HTMLResponse, JSONResponse, Response
    except ImportError as e:  # pragma: no cover
        raise SystemExit("The desktop app needs fastapi + uvicorn:\n  pip install fastapi uvicorn") from e

    from pathlib import Path

    eng = engine or Engine(config or load_config())
    app = FastAPI(title="Origin")

    # ── access token (required when Origin is served over a network) ──
    @app.middleware("http")
    async def _auth(request, call_next):
        if token and request.url.path.startswith("/api"):
            supplied = request.headers.get("x-origin-token") or request.query_params.get("token")
            if supplied != token:
                return JSONResponse({"error": "unauthorized — missing or wrong access token"},
                                    status_code=401)
        return await call_next(request)

    # Never return an HTML 500 — the UI expects JSON, so surface errors as JSON.
    @app.exception_handler(Exception)
    async def _json_errors(request, exc):
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=200)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "app": "Origin"}

    @app.get("/api/workers/test")
    def workers_test():
        """Ping every model worker so you can see exactly which ones work and, for
        the ones that don't, the real API error (quota vs wrong model name, etc.)."""
        results = {}
        for w in eng.pool.names():
            r = eng.pool.ask(w, "Reply with exactly the word: OK")
            ok = bool(r) and not r.startswith("ERROR") and not r.startswith("(")
            model = eng.pool.workers[w].model if eng.pool.has(w) else "?"
            results[w] = {"ok": ok, "model": model, "response": r[:400]}
        return results

    @app.get("/api/diagnostics")
    def diagnostics():
        import platform
        st = eng.state()
        return {
            "app": "Origin",
            "python": platform.python_version(),
            "platform": platform.platform(),
            "coordinator": st.get("coordinator"),
            "brain_error": st.get("brain_error"),
            "workers": st.get("workers"),
            "tools_by_connector": {k: len(v) for k, v in st.get("tools", {}).items()},
            "tool_count": sum(len(v) for v in st.get("tools", {}).values()),
            "projects": len(st.get("projects", [])),
            "memory_count": st.get("memory_count"),
            "roles": st.get("roles"),
            "mcp_status": st.get("mcp_status"),
        }

    webui = Path(__file__).parent / "webui" / "index.html"

    @app.get("/", response_class=HTMLResponse)
    def index():
        if webui.is_file():
            return webui.read_text()
        return "<h1>Origin</h1><p>UI file missing.</p>"

    @app.get("/api/state")
    def state():
        return eng.state()

    @app.post("/api/projects")
    def create_project(body: dict = Body(...)):
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "name required"}, status_code=400)
        proj = eng.create_project(name, body.get("workdir"), body.get("notes", ""))
        return proj.to_dict()

    @app.post("/api/projects/{slug}/open")
    def open_project(slug: str):
        try:
            proj = eng.open_project(slug)
        except KeyError:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"opened": proj.to_dict(), "transcript": proj.display_transcript()}

    @app.get("/api/projects/{slug}/export")
    def export_project(slug: str):
        try:
            data = eng.projects.export_bytes(slug)
        except KeyError:
            return JSONResponse({"error": "not found"}, status_code=404)
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{slug}.originproj"'},
        )

    @app.post("/api/projects/import")
    def import_project(body: dict = Body(...)):
        raw = base64.b64decode(body.get("data_b64", ""))
        proj = eng.projects.import_bytes(raw, new_name=body.get("name"))
        return proj.to_dict()

    @app.post("/api/chat")
    def chat(body: dict = Body(...)):
        text = (body.get("message") or "").strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)
        return eng.chat(text)

    @app.post("/api/coordinator")
    def coordinator(body: dict = Body(...)):
        return eng.set_coordinator(body.get("worker", ""))

    @app.post("/api/role")
    def role(body: dict = Body(...)):
        return eng.set_role(body.get("role", ""))

    @app.post("/api/mission")
    def mission(body: dict = Body(...)):
        goal = (body.get("goal") or "").strip()
        if not goal:
            return JSONResponse({"error": "goal required"}, status_code=400)
        return eng.mission(goal)

    app.state.engine = eng
    return app
