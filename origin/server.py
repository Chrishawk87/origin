"""Origin desktop backend — a small local web server the app window talks to.

Wraps the whole engine (workers, tools, web, browser, shell) plus projects, and
exposes it over a localhost HTTP API consumed by the bundled web UI.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

_KIND_MAP = {
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image", "webp": "image", "svg": "image",
    "mp4": "video", "webm": "video", "mov": "video", "m4v": "video",
    "mp3": "audio", "wav": "audio", "m4a": "audio", "ogg": "audio",
    "pdf": "pdf",
    "txt": "text", "md": "text", "csv": "text", "json": "text", "py": "text", "js": "text",
    "html": "text", "yaml": "text", "yml": "text", "log": "text", "docx": "doc", "xlsx": "doc", "pptx": "doc",
}


def file_kind(name: str) -> str:
    return _KIND_MAP.get(name.rsplit(".", 1)[-1].lower() if "." in name else "", "other")

from .agent import Agent
from .config import Config, load_config
from .llm import build_provider
from .llm.base import AssistantTurn, LLMProvider
from .projects import Project, ProjectManager
from .tools import Registry

# Imported at module scope so FastAPI can resolve the `Request` annotation
# (needed because `from __future__ import annotations` makes annotations strings).
try:
    from starlette.requests import Request
except Exception:  # pragma: no cover
    Request = None  # type: ignore


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
        self.roles: List[str] = []
        import threading as _threading
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._jobs_lock = _threading.Lock()
        self._chat_lock = _threading.Lock()
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

    def _tool_names(self) -> List[str]:
        try:
            return [t["name"] for t in self.registry.schemas()]
        except Exception:
            return []

    def set_role(self, names) -> Dict[str, Any]:
        """Adopt one OR several roles/expert domains at once.

        `names` may be a string ("marketer"), a comma-separated string
        ("marketer, growth hacker"), or a list. Each may be a known role or ANY
        domain (Origin composes a world-class expert on the fly). The persona
        explicitly tells the model which role(s) it is AND which of the
        currently-loaded tools to lean on for that work.
        """
        from .roles import compose_persona
        if isinstance(names, str):
            names = [p.strip() for p in names.split(",")]
        names = [n.strip() for n in (names or []) if n and str(n).strip()]
        if not names:
            # Clearing roles → back to the plain operator brain.
            from .agent import OPERATOR_PROMPT
            self.agent.set_system_prompt(OPERATOR_PROMPT)
            self.roles = []
            self.role = None
            return {"ok": True, "roles": [], "recommended_tools": []}
        persona, recommended = compose_persona(names, self._tool_names())
        self.agent.set_system_prompt(persona)
        self.roles = names
        self.role = ", ".join(names)
        return {"ok": True, "roles": names, "recommended_tools": recommended}

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

    def delete_project(self, slug: str, purge_workspace: bool = False) -> bool:
        # If the project being deleted is the open one, close it first.
        if self.active and self.active.slug == slug:
            try:
                self.active.save_history(self.agent.history)
            except Exception:
                pass
            self.active = None
            self.agent.reset()
        return self.projects.delete(slug, purge_workspace=purge_workspace)

    # ── chat ────────────────────────────────────────────────────────────────
    def _enhance(self, text: str) -> str:
        """Rewrite the raw message into a stronger instruction (or return it)."""
        from .enhancer import enhance_prompt
        enh = self.config.data.get("enhancer", {}) or {}
        worker = enh.get("worker")

        def ask(prompt: str, system: str = "") -> str:
            if worker and self.pool.has(worker):
                return self.pool.ask(worker, prompt, system)
            return self._ask(prompt, system)

        ctx = f"Project: {self.active.name}." if self.active else ""
        return enhance_prompt(ask, text, ctx)

    def _workspace_files(self) -> Dict[str, tuple]:
        """Map of relative path -> (mtime, size) for the open project's files.

        Used to diff before/after a turn so we can show the user EXACTLY what
        Origin just produced, instead of only saying 'it's in a folder'."""
        snap: Dict[str, tuple] = {}
        if not self.active:
            return snap
        base = Path(self.active.workdir)
        if not base.exists():
            return snap
        for p in base.rglob("*"):
            parts = p.relative_to(base).parts
            if any(x.startswith(".") for x in parts):
                continue
            if p.is_file():
                try:
                    st = p.stat()
                    snap["/".join(parts)] = (st.st_mtime, st.st_size)
                except OSError:
                    pass
        return snap

    def _diff_artifacts(self, before: Dict[str, tuple], after: Dict[str, tuple]) -> List[Dict[str, Any]]:
        """New or changed files, newest first — the products of this turn."""
        changed = []
        for path, meta in after.items():
            if path not in before or before[path] != meta:
                changed.append((path, meta))
        changed.sort(key=lambda kv: kv[1][0], reverse=True)  # by mtime desc
        out = []
        for path, (_mtime, size) in changed[:24]:
            out.append({
                "path": path,
                "name": path.rsplit("/", 1)[-1],
                "kind": file_kind(path),
                "size": size,
            })
        return out

    def chat(self, text: str, enhance: bool = True, on_event=None) -> Dict[str, Any]:
        events: List[Dict[str, Any]] = []
        before_files = self._workspace_files()

        def emit(ev: Dict[str, Any]) -> None:
            events.append(ev)
            if on_event:
                try:
                    on_event(ev)
                except Exception:
                    pass

        enhanced = None
        enh_cfg = self.config.data.get("enhancer", {}) or {}
        if enhance and enh_cfg.get("enabled", True) and not self.brain_error:
            improved = self._enhance(text)
            if improved and improved.strip() and improved.strip() != text.strip():
                enhanced = improved.strip()
                emit({"type": "enhanced", "text": enhanced})
        run_text = enhanced or text
        final = self.agent.run(
            run_text,
            on_text=lambda t: emit({"type": "text", "text": t}),
            on_tool_start=lambda n, a: emit({"type": "tool", "name": n, "args": a}),
            on_tool_result=lambda n, r: emit({"type": "result", "name": n, "result": r[:4000]}),
        )
        if self.active:
            self.active.save_history(self.agent.history)
        artifacts = self._diff_artifacts(before_files, self._workspace_files())
        return {"events": events, "final": final, "enhanced": enhanced,
                "artifacts": artifacts, "slug": self.active.slug if self.active else None,
                "calls": self.pool.stats()}

    # ── background chat jobs (so long turns never hit a request timeout) ─────
    def start_chat(self, text: str, enhance: bool = True) -> Dict[str, Any]:
        """Kick off a chat turn in the background and return a job id to poll.

        A hard question can run many model + tool calls in a row and take
        minutes. Holding one HTTP request open that whole time invites browser
        and proxy timeouts (the 'times out without answering' bug). Instead we
        run the turn on a worker thread and let the UI poll for progress."""
        import threading
        import uuid

        jid = uuid.uuid4().hex[:12]
        job: Dict[str, Any] = {
            "status": "running", "events": [], "final": None,
            "enhanced": None, "error": None, "calls": None,
            "artifacts": [], "slug": None,
        }
        with self._jobs_lock:
            self._jobs[jid] = job
            if len(self._jobs) > 40:  # keep the store small
                for k in list(self._jobs.keys())[:-40]:
                    self._jobs.pop(k, None)

        def work() -> None:
            if not self._chat_lock.acquire(blocking=False):
                job["status"] = "error"
                job["error"] = ("Origin is still finishing your previous message — "
                                "give it a moment, then try again.")
                return
            try:
                def on_event(ev: Dict[str, Any]) -> None:
                    job["events"].append(ev)
                    if ev.get("type") == "enhanced":
                        job["enhanced"] = ev.get("text")

                result = self.chat(text, enhance=enhance, on_event=on_event)
                job["final"] = result.get("final")
                job["enhanced"] = result.get("enhanced")
                job["calls"] = result.get("calls")
                job["artifacts"] = result.get("artifacts") or []
                job["slug"] = result.get("slug")
                job["status"] = "done"
            except Exception as e:  # never let the thread die silently
                job["status"] = "error"
                job["error"] = str(e)
            finally:
                self._chat_lock.release()

        threading.Thread(target=work, daemon=True).start()
        return {"job_id": jid, "status": "running"}

    def job_status(self, jid: str) -> Dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(jid)
        if not job:
            return {"status": "error", "error": "unknown or expired job", "events": []}
        return {
            "status": job["status"],
            "events": job["events"],
            "final": job["final"],
            "enhanced": job["enhanced"],
            "error": job["error"],
            "calls": job["calls"],
            "artifacts": job.get("artifacts") or [],
            "slug": job.get("slug"),
        }

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
            "active_roles": self.roles,
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
        from fastapi import Body, FastAPI, Request
        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
    except ImportError as e:  # pragma: no cover
        raise SystemExit("The desktop app needs fastapi + uvicorn:\n  pip install fastapi uvicorn") from e

    import json as _json
    from pathlib import Path

    _KIND = {
        "png": "image", "jpg": "image", "jpeg": "image", "gif": "image", "webp": "image", "svg": "image",
        "mp4": "video", "webm": "video", "mov": "video", "m4v": "video",
        "mp3": "audio", "wav": "audio", "m4a": "audio", "ogg": "audio",
        "pdf": "pdf",
        "txt": "text", "md": "text", "csv": "text", "json": "text", "py": "text", "js": "text",
        "html": "text", "yaml": "text", "yml": "text", "log": "text",
    }

    def _kind(name: str) -> str:
        return _KIND.get(name.rsplit(".", 1)[-1].lower() if "." in name else "", "other")

    def _safe_join(workdir: str, rel: str) -> Path:
        base = Path(workdir).resolve()
        target = (base / (rel or "").lstrip("/")).resolve()
        if base != target and base not in target.parents:
            raise ValueError("path escapes workspace")
        return target

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

    @app.get("/api/accounts")
    def accounts():
        provs = [
            {"id": "claude", "label": "Claude — Anthropic", "key_env": "ANTHROPIC_API_KEY",
             "manage": "https://console.anthropic.com/settings/billing", "use": "chat / reasoning"},
            {"id": "gpt", "label": "GPT — OpenAI", "key_env": "OPENAI_API_KEY",
             "manage": "https://platform.openai.com/settings/organization/billing", "use": "chat + images"},
            {"id": "grok", "label": "Grok — xAI", "key_env": "XAI_API_KEY",
             "manage": "https://console.x.ai", "use": "chat"},
            {"id": "gemini", "label": "Gemini — Google", "key_env": "GEMINI_API_KEY",
             "manage": "https://aistudio.google.com/app/apikey", "use": "chat"},
            {"id": "sora", "label": "Sora video — OpenAI", "key_env": "OPENAI_API_KEY",
             "manage": "https://platform.openai.com/settings/organization/billing", "use": "video generation"},
            {"id": "veo", "label": "Veo video — Google", "key_env": "GEMINI_API_KEY",
             "manage": "https://aistudio.google.com/app/apikey", "use": "video generation"},
        ]
        out = [{**p, "connected": bool(os.environ.get(p["key_env"]))} for p in provs]
        return {"accounts": out,
                "note": "Live balances aren't exposed by these APIs — click Manage to view or top up on each provider's page. Sora shares your OpenAI account; Veo shares your Google/Gemini account."}

    @app.get("/api/workers/models")
    def workers_models():
        """List the model names each provider actually offers right now, so you
        can pick a valid one if a configured model name is out of date."""
        out = {}
        for w in eng.pool.names():
            wcfg = (eng.config.workers.get(w) or {})
            prov = wcfg.get("provider")
            if prov in ("openai", "grok", "gemini", "ollama"):
                try:
                    p = eng.pool.provider(w)
                    ids = sorted(m.id for m in p.client.models.list().data)
                    out[w] = {"provider": prov, "available_models": ids}
                except Exception as e:
                    out[w] = {"provider": prov, "error": str(e)[:300]}
            else:
                out[w] = {"provider": prov, "note": "listing not supported (Anthropic/builtin)"}
        return out

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

    @app.post("/api/projects/{slug}/delete")
    def delete_project(slug: str, body: dict = Body(default={})):
        if not eng.projects.get(slug):
            return JSONResponse({"error": "not found"}, status_code=404)
        purge = bool((body or {}).get("purge_workspace"))
        ok = eng.delete_project(slug, purge_workspace=purge)
        return {"deleted": ok, "slug": slug, "purged_workspace": purge}

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

    # ── files / workspace ────────────────────────────────────────────────
    @app.get("/api/projects/{slug}/files")
    def list_files(slug: str):
        proj = eng.projects.get(slug)
        if not proj:
            return JSONResponse({"error": "not found"}, status_code=404)
        base = Path(proj.workdir)
        base.mkdir(parents=True, exist_ok=True)
        items = []
        for p in sorted(base.rglob("*")):
            rel_parts = p.relative_to(base).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            rel = "/".join(rel_parts)
            if p.is_dir():
                items.append({"path": rel, "dir": True})
            else:
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                items.append({"path": rel, "dir": False, "size": size, "kind": _kind(p.name)})
            if len(items) >= 3000:
                break
        return {"workdir": str(base), "files": items}

    @app.get("/api/projects/{slug}/file")
    def get_file(slug: str, path: str, download: int = 0):
        proj = eng.projects.get(slug)
        if not proj:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            target = _safe_join(proj.workdir, path)
        except ValueError:
            return JSONResponse({"error": "bad path"}, status_code=400)
        if not target.is_file():
            return JSONResponse({"error": "no such file"}, status_code=404)
        disp = "attachment" if download else "inline"
        return FileResponse(str(target), filename=target.name,
                            headers={"Content-Disposition": f'{disp}; filename="{target.name}"'})

    @app.post("/api/projects/{slug}/upload")
    async def upload(slug: str, request: Request):
        proj = eng.projects.get(slug)
        if not proj:
            return JSONResponse({"error": "not found"}, status_code=404)
        form = await request.form()
        uploads = form.getlist("files")
        try:
            rels = _json.loads(form.get("paths") or "[]")
        except Exception:
            rels = []
        saved = 0
        for i, f in enumerate(uploads):
            if not hasattr(f, "filename"):
                continue
            rel = (rels[i] if i < len(rels) and rels[i] else f.filename) or f.filename
            try:
                target = _safe_join(proj.workdir, rel)
            except ValueError:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as out:
                while True:
                    chunk = await f.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            saved += 1
        return {"saved": saved}

    @app.post("/api/chat")
    def chat(body: dict = Body(...)):
        text = (body.get("message") or "").strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)
        # Start in the background and hand back a job id to poll — this is what
        # stops long turns from timing out. (Pass sync=1 to block, for scripts.)
        if body.get("sync"):
            return eng.chat(text, enhance=bool(body.get("enhance", True)))
        return eng.start_chat(text, enhance=bool(body.get("enhance", True)))

    @app.get("/api/chat/status/{jid}")
    def chat_status(jid: str):
        return eng.job_status(jid)

    @app.post("/api/coordinator")
    def coordinator(body: dict = Body(...)):
        return eng.set_coordinator(body.get("worker", ""))

    @app.post("/api/enhance")
    def enhance_ep(body: dict = Body(...)):
        text = (body.get("message") or "").strip()
        if not text:
            return {"enhanced": text}
        try:
            return {"enhanced": eng._enhance(text)}
        except Exception as e:
            return {"enhanced": text, "error": str(e)}

    @app.post("/api/role")
    def role(body: dict = Body(...)):
        # Accepts either {"role": "marketer"} or {"roles": ["marketer","analyst"]}.
        names = body.get("roles")
        if names is None:
            names = body.get("role", "")
        return eng.set_role(names)

    @app.post("/api/mission")
    def mission(body: dict = Body(...)):
        goal = (body.get("goal") or "").strip()
        if not goal:
            return JSONResponse({"error": "goal required"}, status_code=400)
        return eng.mission(goal)

    # ── Compliance document management ──────────────────────────────────────
    from . import compliance as _cmp
    from . import compliance_kb as _kb

    # -- Compliance Knowledge Base: codified OSHA/DOT/insurance standards -----
    # These back the mandatory pre-send validation gate: every compliance
    # document must pass the KB's required-elements checklist for the standard
    # it invokes before it can be rendered/sent to a client.
    @app.get("/api/compliance/kb/standards")
    def compliance_kb_standards(category: str | None = None, q: str | None = None):
        if q:
            recs = _kb.search(q, limit=25)
        else:
            recs = _kb.all_records()
            if category:
                recs = [r for r in recs if r.get("category", "").startswith(category)]
        return {"standards": [
            {"id": r["id"], "title": r["title"], "citation": r["citation"],
             "category": r.get("category", ""), "written_program": r.get("written_program", ""),
             "agencies": r.get("agencies", {})}
            for r in recs
        ]}

    @app.get("/api/compliance/kb/standards/{entry_id}")
    def compliance_kb_standard(entry_id: str):
        r = _kb.get(entry_id)
        if not r:
            return JSONResponse({"error": "unknown standard"}, status_code=404)
        return r

    @app.get("/api/compliance/kb/templates")
    def compliance_kb_templates():
        return _kb.templates()

    @app.post("/api/compliance/kb/validate")
    def compliance_kb_validate(body: dict = Body(...)):
        """Run the OSHA/KB checklist against a document (HTML or entry ids).

        This is the same gate enforced on /compliance/send — exposed so the UI
        and the agent can pre-check a draft while editing, before a client send.
        """
        html = body.get("html")
        if html is None:
            return JSONResponse({"error": "html required"}, status_code=400)
        entry_ids = body.get("entry_ids") or body.get("standards") or None
        return _kb.validate_document(html, entry_ids=entry_ids)

    # -- Asset Library: persistent, editable master documents ----------------
    @app.get("/api/compliance/library")
    def compliance_library():
        return {"templates": _cmp.list_templates()}

    @app.get("/api/compliance/master/{mid}")
    def compliance_master(mid: str):
        html = _cmp.read_master_html(mid)
        if html is None:
            return JSONResponse({"error": "master not found"}, status_code=404)
        return {"id": mid, "title": _cmp.master_title(mid), "html": html}

    @app.post("/api/compliance/master/{mid}/save")
    def compliance_master_save(mid: str, body: dict = Body(...)):
        html = body.get("html")
        if html is None:
            return JSONResponse({"error": "html required"}, status_code=400)
        if not _cmp.save_master_html(mid, html):
            return JSONResponse({"error": "master not found"}, status_code=404)
        return {"saved": True, "id": mid}

    @app.post("/api/compliance/master/new")
    def compliance_master_new(body: dict = Body(...)):
        title = (body.get("title") or "").strip()
        if not title:
            return JSONResponse({"error": "title required"}, status_code=400)
        return _cmp.add_master(title, body.get("html"))

    @app.post("/api/compliance/upload")
    async def compliance_upload(request: Request):
        """Add uploaded HTML file(s) to the Asset Library as new masters."""
        form = await request.form()
        uploads = form.getlist("files")
        added = []
        for f in uploads:
            if not hasattr(f, "filename"):
                continue
            raw = await f.read()
            added.append(_cmp.ingest_upload(f.filename, raw))
        return {"added": added}

    @app.get("/api/compliance/master/{mid}/pdf")
    def compliance_master_pdf(mid: str):
        """Render a library master to PDF on the fly (for editor Download PDF)."""
        html = _cmp.read_master_html(mid)
        if html is None:
            return JSONResponse({"error": "master not found"}, status_code=404)
        title = _cmp.master_title(mid)
        out = _cmp.LIBRARY_DIR / (mid + ".pdf")
        try:
            _cmp.render_pdf(html, out, title=title)
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=200)
        dl = _cmp.safe_filename(title)[:-5] + ".pdf"
        return FileResponse(str(out), filename=dl, media_type="application/pdf")

    # -- Assign a master into a customer project (copy-on-assign) -------------
    @app.post("/api/projects/{slug}/compliance/assign")
    def compliance_assign(slug: str, body: dict = Body(...)):
        proj = eng.projects.get(slug)
        if not proj:
            return JSONResponse({"error": "not found"}, status_code=404)
        mid = (body.get("master_id") or body.get("template_id") or "").strip()
        html = _cmp.read_master_html(mid)
        if html is None:
            return JSONResponse({"error": "master not found"}, status_code=404)
        title = _cmp.master_title(mid)
        base = _safe_join(proj.workdir, _cmp.ASSIGN_SUBDIR)
        target = _cmp.unique_path(base, _cmp.safe_filename(title))
        target.write_text(html, encoding="utf-8")
        rel = str(target.relative_to(Path(proj.workdir).resolve()))
        return {"assigned": True, "path": rel, "title": title}

    # -- Edit / save / render / send a project copy --------------------------
    @app.get("/api/projects/{slug}/compliance/content")
    def compliance_content(slug: str, path: str):
        proj = eng.projects.get(slug)
        if not proj:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            target = _safe_join(proj.workdir, path)
        except ValueError:
            return JSONResponse({"error": "bad path"}, status_code=400)
        if not target.is_file():
            return JSONResponse({"error": "no such file"}, status_code=404)
        return {"path": path, "html": target.read_text(encoding="utf-8", errors="replace")}

    @app.post("/api/projects/{slug}/compliance/save")
    def compliance_save(slug: str, body: dict = Body(...)):
        proj = eng.projects.get(slug)
        if not proj:
            return JSONResponse({"error": "not found"}, status_code=404)
        path = body.get("path") or ""
        html = body.get("html")
        if html is None:
            return JSONResponse({"error": "html required"}, status_code=400)
        try:
            target = _safe_join(proj.workdir, path)
        except ValueError:
            return JSONResponse({"error": "bad path"}, status_code=400)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
        return {"saved": True, "path": path}

    @app.post("/api/projects/{slug}/compliance/render")
    def compliance_render(slug: str, body: dict = Body(...)):
        proj = eng.projects.get(slug)
        if not proj:
            return JSONResponse({"error": "not found"}, status_code=404)
        path = body.get("path") or ""
        try:
            target = _safe_join(proj.workdir, path)
        except ValueError:
            return JSONResponse({"error": "bad path"}, status_code=400)
        if not target.is_file():
            return JSONResponse({"error": "no such file"}, status_code=404)
        html = target.read_text(encoding="utf-8", errors="replace")
        pdf_path = target.with_suffix(".pdf")
        try:
            _cmp.render_pdf(html, pdf_path, title=target.stem)
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=200)
        rel = str(pdf_path.relative_to(Path(proj.workdir).resolve()))
        return {"rendered": True, "path": rel}

    @app.post("/api/projects/{slug}/compliance/send")
    def compliance_send(slug: str, body: dict = Body(...)):
        proj = eng.projects.get(slug)
        if not proj:
            return JSONResponse({"error": "not found"}, status_code=404)
        path = body.get("path") or ""
        to = (body.get("to") or "").strip()
        subject = (body.get("subject") or "").strip()
        message = body.get("message") or ""
        if not to:
            return JSONResponse({"error": "recipient email required"}, status_code=400)
        try:
            target = _safe_join(proj.workdir, path)
        except ValueError:
            return JSONResponse({"error": "bad path"}, status_code=400)
        if not target.is_file():
            return JSONResponse({"error": "no such file"}, status_code=404)
        html = target.read_text(encoding="utf-8", errors="replace")

        # ── HARD COMPLIANCE GATE ────────────────────────────────────────────
        # No compliance document leaves Origin for a client until it passes the
        # KB's OSHA/DOT required-elements checklist for the standard it invokes.
        # A reviewer can override only by explicitly acknowledging the gate
        # (override=true) after reading the report — never silently.
        entry_ids = body.get("entry_ids") or body.get("standards") or None
        report = _kb.validate_document(html, entry_ids=entry_ids)
        if not report.get("passed") and not body.get("override"):
            return JSONResponse({
                "error": "blocked_by_compliance_gate",
                "message": ("This document did not pass the OSHA/compliance checklist and "
                            "was NOT sent. Fix the gaps below, or resend with override=true "
                            "after reviewing."),
                "validation": report,
            }, status_code=422)

        pdf_path = target.with_suffix(".pdf")
        try:
            _cmp.render_pdf(html, pdf_path, title=target.stem)
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=200)
        rel = str(pdf_path.relative_to(Path(proj.workdir).resolve()))
        result = _cmp.send_email(to, subject or target.stem, message, attachment=pdf_path)
        result["pdf_path"] = rel
        result["validation"] = report
        result["overridden"] = bool(report.get("passed") is False and body.get("override"))
        return result

    app.state.engine = eng
    return app
