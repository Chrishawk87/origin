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

    def vision_chain(self) -> List[LLMProvider]:
        """Ordered list of vision-capable brains for the Photo Audit tool.

        The tool tries these in order and uses the FIRST that succeeds. This is
        what makes the feature future-proof: if the primary model is one Google
        just retired (a 404), or its key lapses, or it times out, the tool falls
        straight through to the next brain instead of failing for a customer.

        Order is config-driven (`vision.order`), defaulting to gemini → claude →
        gpt. The Gemini entry is pointed at the `gemini-flash-latest` alias so it
        auto-tracks Google's newest Flash model and never pins to a name that can
        later be shut down. Any worker without a working API key is skipped."""
        cfg = self.config.data.get("vision", {}) or {}
        order = cfg.get("order") or ["gemini", "claude", "gpt"]
        # Per-provider model overrides for the vision path (independent of the
        # agent brain). Default: track Google's latest Flash via the alias.
        model_overrides = dict(cfg.get("models") or {})
        model_overrides.setdefault("gemini", "gemini-flash-latest")

        chain: List[LLMProvider] = []
        seen = set()
        for name in order:
            if name in seen or not self.pool.has(name):
                continue
            seen.add(name)
            try:
                prov = self.pool.provider(name)
            except SystemExit:
                continue
            override = model_overrides.get(name)
            if override and getattr(prov, "model", None) and prov.model != override:
                # Clone so the vision model swap never mutates the shared agent
                # provider (gemini also runs the everyday agent on a pinned model).
                import copy
                try:
                    prov = copy.copy(prov)
                    prov.model = override
                except Exception:
                    pass
            if getattr(prov, "client", None) is not None:
                chain.append(prov)
        # Last resort: the agent's own brain, so the tool still works even with a
        # bare/minimal config.
        if not chain and getattr(self, "agent", None) and getattr(self.agent, "llm", None):
            chain.append(self.agent.llm)
        return chain

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
        """Rewrite the raw message into a stronger instruction (or return it).

        Two guarantees so prompt-enhancement can never wedge a chat turn:
          1) It runs on the ACTIVE coordinator (follows the Brain dropdown) by
             default — so a Claude turn enhances with Claude, not a hardwired
             free-tier model that may be rate-limited or slow. A model can be
             pinned via `enhancer.worker`, but ONLY if `enhancer.pin: true`.
          2) It is wrapped in a hard timeout. The chat turn holds a single
             global lock; if enhancement hangs, the next question fails with
             "still finishing your previous message" and the user must refresh.
             On timeout we abandon enhancement and use the raw message instead.
        """
        import threading
        from .enhancer import enhance_prompt

        enh = self.config.data.get("enhancer", {}) or {}
        pinned = enh.get("worker") if enh.get("pin") else None
        timeout = float(enh.get("timeout_seconds", 12) or 12)

        def ask(prompt: str, system: str = "") -> str:
            if pinned and self.pool.has(pinned):
                return self.pool.ask(pinned, prompt, system)
            return self._ask(prompt, system)   # active coordinator

        ctx = f"Project: {self.active.name}." if self.active else ""

        result = {"text": text}

        def run() -> None:
            try:
                result["text"] = enhance_prompt(ask, text, ctx)
            except Exception:
                result["text"] = text

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            # Enhancement is taking too long — let it finish in the background
            # and just send the raw message so the turn (and the chat lock) is
            # never held hostage by the enhance step.
            return text
        return result["text"]

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
        # Verifiable-accuracy guard: confirm every OSHA/CFR citation in the answer
        # against the Compliance Knowledge Base before the user sees it. Real
        # citations get their official source appended; a regulation number the
        # model asserted but that isn't in the KB is struck and flagged. Only acts
        # when the answer actually cites a standard, so ordinary chat is untouched.
        # Never allowed to break a turn — a guard failure leaves the answer as-is.
        try:
            from .citation_guard import guard_answer
            if final:
                guarded, creport = guard_answer(final)
                if creport.get("changed"):
                    final = guarded
                    emit({"type": "citation_check",
                          "verified": creport.get("verified", []),
                          "unverified": creport.get("unverified", [])})
        except Exception:
            pass
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
            # Wait a short grace period for a previous turn to finish rather than
            # failing instantly — this smooths over the tiny gap between turns so
            # a quick follow-up question doesn't bounce off the lock and force a
            # page refresh. Only a genuinely still-running turn hits the error.
            if not self._chat_lock.acquire(timeout=8):
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

    def inventory(self) -> Dict[str, Any]:
        """A single snapshot of everything the system currently holds — the
        capabilities (tools), the compliance knowledge base + templates, the
        hiring-client requirement profiles, and what Origin has LEARNED at
        runtime (saved memories + cached research). Powers the System panel so
        Chris can see what's stored and what still needs to be added."""
        import origin.compliance_kb as ckb

        # ── Tools (capabilities), with a short description, grouped by source ──
        tools: Dict[str, List[Dict[str, str]]] = {}
        for t in self.registry.tools.values():
            desc = (getattr(t, "description", "") or "").strip().replace("\n", " ")
            tools.setdefault(t.source, []).append(
                {"name": t.name, "description": desc[:240]}
            )
        for src in tools:
            tools[src].sort(key=lambda x: x["name"])
        tool_total = sum(len(v) for v in tools.values())

        # ── Compliance KB + templates ────────────────────────────────────────
        try:
            kb = ckb.kb_stats()
        except Exception as e:
            kb = {"error": str(e)}

        # ── Hiring-client requirement profiles ──────────────────────────────
        try:
            profs = ckb.list_hiring_clients()
        except Exception:
            profs = []
        by_arch: Dict[str, List[Dict[str, Any]]] = {}
        confirmed = 0
        for p in profs:
            by_arch.setdefault(p.get("archetype") or "other", []).append(
                {"hiring_client": p.get("hiring_client"),
                 "confirmed": bool(p.get("confirmed"))}
            )
            if p.get("confirmed"):
                confirmed += 1
        for a in by_arch:
            by_arch[a].sort(key=lambda x: x["hiring_client"] or "")

        # ── Learned memory (saved facts / preferences / goals) ───────────────
        try:
            mems = self.registry.memory.all()
        except Exception:
            mems = []
        mem_by_kind: Dict[str, int] = {}
        for m in mems:
            mem_by_kind[m.get("kind", "fact")] = mem_by_kind.get(m.get("kind", "fact"), 0) + 1
        recent_mem = [
            {"kind": m.get("kind"), "content": (m.get("content") or "")[:200]}
            for m in mems[:15]
        ]

        # ── Learned knowledge (cached research answers) ──────────────────────
        try:
            know = self.registry.research_engine.store.list()
        except Exception:
            know = []
        recent_know = [
            {"question": (k.get("question") or "")[:160],
             "fetched_at": k.get("fetched_at"),
             "watch": bool(k.get("watch"))}
            for k in know[:15]
        ]

        return {
            "tools": {"total": tool_total, "by_source": tools},
            "compliance_kb": kb,
            "profiles": {
                "total": len(profs),
                "confirmed": confirmed,
                "unconfirmed": len(profs) - confirmed,
                "by_archetype": by_arch,
            },
            "memory": {"total": len(mems), "by_kind": mem_by_kind, "recent": recent_mem},
            "knowledge": {"total": len(know), "recent": recent_know},
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

    # ── CORS (public free tools are called cross-origin from the marketing
    # pages, e.g. the Netlify-hosted citation-check page → Railway) ──
    try:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )
    except Exception:
        pass

    # ── access token (required when Origin is served over a network) ──
    def _admin_session_ok(request) -> bool:
        """True when the request carries a valid portal admin/owner session
        cookie. Reused to let the SIE console's /api/* calls through the token
        gate for a logged-in owner without a second credential."""
        try:
            from . import portal as _portal
            p = _portal._unsign(request.cookies.get(_portal.ADMIN_COOKIE, ""))
            return bool(p and p.get("role") == "admin")
        except Exception:
            return False

    import re as _re

    def _gc_slug_ok(request):
        """The slug of the GC whose session cookie this request carries, or None.
        This is what turns the portfolio-wide SIE into a per-tenant product
        (Stage 3): a logged-in GC reaches the SIE, but only its OWN slice.

        Mirrors _admin_session_ok — reads the GC cookie via portal's module-level
        _unsign (portal.gc_session itself is a nested route helper, not importable
        here). A valid, role==gc session yields the GC's own slug."""
        try:
            from . import portal as _portal
            p = _portal._unsign(request.cookies.get(_portal.GC_COOKIE, ""))
            if p and p.get("role") == "gc":
                return (p.get("slug") or "").strip() or None
        except Exception:
            pass
        return None

    def _client_session_ok(request) -> bool:
        """True when the request carries a valid contractor-portal (client)
        session cookie. Used to let a signed-in contractor use the Photo Audit
        tool without exposing it to anonymous visitors."""
        try:
            from . import portal as _portal
            p = _portal._unsign(request.cookies.get(_portal.CLIENT_COOKIE, ""))
            return bool(p and p.get("role") == "client")
        except Exception:
            return False

    # The tenant-scoped SIE surface a logged-in GC may reach. Everything else
    # under /api stays owner-only. Safe-by-default: a path must match here to be
    # reachable by a GC at all, and any company_id it names must be one the GC
    # owns (enforced below). Owner/token access is unaffected by this list.
    _GC_ALLOWED = [
        _re.compile(r"^/api/company/(list|portfolio|upsert)$"),
        _re.compile(r"^/api/company/[^/]+(/(risk|scope))?$"),
        _re.compile(r"^/api/program/package/[^/]+$"),
        _re.compile(r"^/api/program/[^/]+/[^/]+$"),
        _re.compile(r"^/api/prequal/[^/]+$"),
        _re.compile(r"^/api/requirements/[^/]+(/gaps)?$"),
        _re.compile(r"^/api/spine/company/[^/]+$"),
        _re.compile(r"^/api/monitor/company/[^/]+$"),
        _re.compile(r"^/api/audit/list$"),                    # only with ?company_id
        _re.compile(r"^/api/audit/[^/]+$"),                   # audit detail (owned)
        _re.compile(r"^/api/audit/[^/]+/promote/[^/]+$"),
        _re.compile(r"^/api/scoping/[^/]+$"),                 # stateless helpers
        # Review inbox (Stage 5): rollups are gc_slug-filtered in the handler;
        # per-item routes resolve to the item's company below and are owned-gated.
        _re.compile(r"^/api/review/(overview|list)$"),
        _re.compile(r"^/api/review/[^/]+(/(approve|reject))?$"),
        # Training matrix (Stage 6): overview gc_slug-filtered in the handler;
        # per-company + per-employee routes resolve to their company below.
        _re.compile(r"^/api/training/overview$"),
        _re.compile(r"^/api/training/[^/]+/(catalog|matrix|summary|employee)$"),
        _re.compile(r"^/api/training/employee/[^/]+/(complete|deactivate|proof)(/[^/]+)?$"),
        # Brain Router (agency-mode): self-scoping to the tenant's sie_gc_slug;
        # catalog/profile carry no company_id and are tenant-filtered in-handler.
        _re.compile(r"^/api/brain-router/(catalog|profile)$"),
        # Form Vault (Screen 3): official-form catalog/schema/fill. Self-scoping to
        # the tenant's sie_gc_slug; carries no company_id; fill is stateless (or
        # persists under the tenant's own key). Agency visibility is Brain-Router
        # scoped in-handler.
        _re.compile(r"^/api/form-vault/(catalog|fill)$"),
        _re.compile(r"^/api/form-vault/form/[^/]+$"),
    ]
    # Paths a GC may hit that legitimately carry no company_id (they are either
    # self-scoping, stateless, or a tenant-filtered portfolio rollup). Every
    # OTHER allowed path must resolve to a company the GC owns, or the request is
    # refused. The review/training rollups below are safe with no company_id
    # because their handlers filter to request.state.sie_gc_slug.
    _GC_NO_CID_OK = _re.compile(
        r"^/api/(company/(list|portfolio|upsert)|scoping/[^/]+"
        r"|review/(overview|list)|training/overview"
        r"|brain-router/(catalog|profile)"
        r"|form-vault/(catalog|fill|form/[^/]+))$")

    def _gc_company_id(request):
        """Best-effort: the company_id a request targets, for GC ownership checks.
        Covers the path shapes of the tenant-scoped SIE routes plus the
        ?company_id= query used by the audit/CAPA list endpoints. Returns None
        when the path names no company (self-scoping / stateless routes)."""
        q = (request.query_params.get("company_id") or "").strip()
        if q:
            return q
        parts = request.url.path.strip("/").split("/")   # ['api', <area>, ...]
        if len(parts) < 3:
            return None
        area = parts[1]
        if area == "company":
            return None if parts[2] in ("list", "portfolio", "upsert") else parts[2]
        if area in ("prequal", "requirements"):
            return parts[2]
        if area == "program":
            return parts[3] if parts[2] == "package" and len(parts) >= 4 else parts[2]
        if area in ("spine", "monitor") and parts[2] == "company" and len(parts) >= 4:
            return parts[3]
        if area == "audit" and parts[2] not in ("list", "overview", "from-report"):
            try:                                              # audit id → its company
                from . import audit_engine as _ae
                return (_ae.get(parts[2]) or {}).get("company_id")
            except Exception:
                return None
        if area == "review" and parts[2] not in ("overview", "list", "ingest"):
            try:                                        # review item id → its company
                from . import review_engine as _rv
                return (_rv.get(parts[2]) or {}).get("company_id")
            except Exception:
                return None
        if area == "training":
            if parts[2] == "overview":
                return None                             # tenant-filtered in handler
            if parts[2] == "employee" and len(parts) >= 4:
                try:                                    # employee id → its company
                    from . import training_engine as _tr
                    return (_tr.get_employee(parts[3]) or {}).get("company_id")
                except Exception:
                    return None
            return parts[2]                             # /api/training/{company_id}/...
        return None

    def _gc_owns(gc_slug, company_id):
        try:
            from . import company_profile as _cp
            rec = _cp.get(company_id)
            return bool(rec) and (rec.get("gc_slug") or "") == gc_slug
        except Exception:
            return False

    @app.middleware("http")
    async def _auth(request, call_next):
        # The Photo Audit tool is used by GC/sub/owner dashboards whose users
        # authenticate by a PORTAL SESSION (admin/GC/client cookie), not the
        # internal access token — so it can't be gated by the token. But it must
        # still require SOME valid session: previously it was fully public, which
        # let anyone with the link run the whole tool with no login. Now it
        # accepts owner (token/admin), GC, or contractor sessions, and refuses
        # anonymous visitors when the app is served over a network.
        # The cron-sweep endpoint has its OWN token gate (MONITOR_SWEEP_TOKEN)
        # so an external scheduler can trigger it without the internal access
        # token; it must therefore bypass this outer gate.
        path = request.url.path
        _photo_api = path.startswith("/api/photo-audit/")
        _public_api = (path == "/api/monitor/cron-sweep")
        is_api = path.startswith("/api") and not _public_api and not _photo_api

        if _photo_api:
            supplied = (request.headers.get("x-origin-token")
                        or request.query_params.get("token"))
            if (token and supplied == token) or _admin_session_ok(request):
                request.state.sie_owner = True
                request.state.sie_gc_slug = (request.query_params.get("gc") or "").strip() or None
            elif _gc_slug_ok(request):
                request.state.sie_owner = False
                request.state.sie_gc_slug = _gc_slug_ok(request)
            elif _client_session_ok(request):
                request.state.sie_owner = False
                request.state.sie_gc_slug = None
            elif not token:
                # Local single-user run (no access token configured) = owner.
                request.state.sie_owner = True
                request.state.sie_gc_slug = None
            else:
                return JSONResponse({"error": "unauthorized — please sign in"},
                                    status_code=401)
            return await call_next(request)

        if token and is_api:
            supplied = request.headers.get("x-origin-token") or request.query_params.get("token")
            if supplied == token or _admin_session_ok(request):
                # Owner/admin: the global superuser lens. May act for a specific
                # GC by passing ?gc=<slug> (used by the owner's tenant preview).
                request.state.sie_owner = True
                request.state.sie_gc_slug = (request.query_params.get("gc") or "").strip() or None
            else:
                # Not the owner. A logged-in GC gets a tenant-scoped slice of the
                # SIE; anyone else is refused. The SIE console (/sie) is reached
                # by an owner/admin via their portal admin SESSION cookie — honored
                # above — so no second credential prompt for the owner.
                gc_slug = _gc_slug_ok(request)
                if not gc_slug:
                    return JSONResponse({"error": "unauthorized — missing or wrong access token"},
                                        status_code=401)
                if not any(rx.match(path) for rx in _GC_ALLOWED):
                    return JSONResponse({"error": "forbidden"}, status_code=403)
                cid = _gc_company_id(request)
                if cid:
                    if not _gc_owns(gc_slug, cid):
                        return JSONResponse({"error": "forbidden"}, status_code=403)
                elif not _GC_NO_CID_OK.match(path):
                    # An allowed path that needs a company but named none (e.g.
                    # /api/audit/list with no ?company_id) would spill across
                    # tenants — refuse it for a GC.
                    return JSONResponse({"error": "forbidden"}, status_code=403)
                request.state.sie_owner = False
                request.state.sie_gc_slug = gc_slug
        elif is_api:
            # No access token configured (local single-user run): everything is
            # the owner. Still expose the scope so routes read a consistent shape.
            request.state.sie_owner = True
            request.state.sie_gc_slug = (request.query_params.get("gc") or "").strip() or None
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

    @app.get("/api/inventory")
    def inventory():
        return eng.inventory()

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

    # -- Magic button: auto-fill a master for a company ----------------------
    @app.post("/api/compliance/master/{mid}/fill")
    def compliance_master_fill(mid: str, body: dict = Body(default=None)):
        """Return a company-ready copy of a master with its {{TOKENS}} filled.
        Seeds from the company profile (if company_id given), then applies any
        explicit field overrides. Never mutates the blank master."""
        payload = body if isinstance(body, dict) else {}
        cid = (payload.get("company_id") or "").strip()
        fields: dict = {}
        if cid:
            try:
                from . import company_profile as _cp
                fields.update(_cp.doc_fields_for(cid))
            except Exception:
                pass
        override = payload.get("fields")
        if isinstance(override, dict):
            for k, v in override.items():
                if v not in (None, ""):
                    fields[k] = v
        html, title = _cmp.fill_master_html(mid, fields)
        if html is None:
            return JSONResponse({"error": "master not found"}, status_code=404)
        return {"ok": True, "id": mid, "title": title, "html": html, "fields": fields}

    @app.post("/api/compliance/master/{mid}/fill/pdf")
    def compliance_master_fill_pdf(mid: str, body: dict = Body(default=None)):
        """Render a filled (or edited) master to PDF. If the body carries `html`
        (the user's in-place edits), that exact HTML is rendered; otherwise the
        master is filled from company_id + fields first."""
        payload = body if isinstance(body, dict) else {}
        html = payload.get("html")
        title = (payload.get("title") or "").strip()
        if not html:
            cid = (payload.get("company_id") or "").strip()
            fields: dict = {}
            if cid:
                try:
                    from . import company_profile as _cp
                    fields.update(_cp.doc_fields_for(cid))
                except Exception:
                    pass
            override = payload.get("fields")
            if isinstance(override, dict):
                for k, v in override.items():
                    if v not in (None, ""):
                        fields[k] = v
            html, title2 = _cmp.fill_master_html(mid, fields)
            if html is None:
                return JSONResponse({"error": "master not found"}, status_code=404)
            title = title or title2
        title = title or _cmp.master_title(mid) or "Document"
        out = _cmp.LIBRARY_DIR / (mid + "-filled.pdf")
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

        # ── COMPLIANCE GATE ─────────────────────────────────────────────────
        # The gate exists to stop an INCOMPLETE OSHA/DOT written program from
        # reaching a client. It only blocks when a standard is actually detected
        # AND the draft fails its required-elements checklist ("fail"). Documents
        # that don't invoke a codified standard — cover letters, COI/insurance
        # letters, EMR explanation letters, transmittals — return "unverified"
        # and are allowed through, because there's nothing to check them against.
        # A reviewer can still override a genuine "fail" with override=true after
        # reading the report.
        entry_ids = body.get("entry_ids") or body.get("standards") or None
        report = _kb.validate_document(html, entry_ids=entry_ids)
        if report.get("status") == "fail" and not body.get("override"):
            return JSONResponse({
                "error": "blocked_by_compliance_gate",
                "message": ("This written program is missing required elements for the "
                            "standard it cites, so it was NOT sent. Fix the gaps listed "
                            "below, or resend with override=true after reviewing."),
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

    # ── Public "Grade Rescue" deficiency analyzer (lead-gen tool) ───────────
    # These live OUTSIDE /api on purpose: the auth middleware only guards /api,
    # and this tool must be reachable by the public with no access token.
    from . import rescue as _rescue

    rescue_html = Path(__file__).parent / "webui" / "rescue.html"

    @app.get("/rescue", response_class=HTMLResponse)
    def rescue_page():
        if rescue_html.is_file():
            return rescue_html.read_text(encoding="utf-8")
        return "<h1>Grade Rescue</h1><p>Tool page missing.</p>"

    @app.get("/rescue/config")
    def rescue_config():
        return {
            "platforms": _rescue.PLATFORMS,
            "industries": _rescue.INDUSTRIES,
            "categories": [
                {"id": c["id"], "label": c["label"], "group": c["group"]}
                for c in _rescue.CATEGORIES
            ],
        }

    @app.post("/rescue/analyze")
    def rescue_analyze(body: dict = Body(...)):
        # email gate: the report is revealed only after we capture a contact.
        email = (body.get("email") or "").strip()
        if "@" not in email or "." not in email.split("@")[-1]:
            return JSONResponse({"error": "A valid email is required to see your results."},
                                status_code=400)
        result = _rescue.analyze(body)
        lead = _rescue.capture_lead(body, result)
        result["lead"] = lead
        return result

    # ── PUBLIC Tool 2: RAVS / prequal rejection decoder ─────────────────────
    # A contractor whose program or account got kicked back picks the reason(s)
    # they were given and gets a plain-English read on what the reviewer means
    # and wants — without us handing over the fix. Public + email-gated; the
    # lead lands in Origin's rescue_leads.jsonl like every other tool.
    @app.get("/rescue/rejections/config")
    def rescue_rejections_config():
        groups: dict = {}
        order: list = []
        for r in _rescue.REJECTIONS:
            g = r["group"]
            if g not in groups:
                groups[g] = []
                order.append(g)
            groups[g].append({"id": r["id"], "label": r["label"]})
        return {
            "platforms": _rescue.PLATFORMS,
            "groups": [{"group": g, "items": groups[g]} for g in order],
        }

    @app.post("/rescue/rejections")
    def rescue_rejections(body: dict = Body(...)):
        email = (body.get("email") or "").strip()
        if "@" not in email or "." not in email.split("@")[-1]:
            return JSONResponse({"error": "A valid email is required to see your results."},
                                status_code=400)
        reasons = [r for r in (body.get("reasons") or []) if r in _rescue.REJECTION_BY_ID]
        other = (body.get("other") or "").strip()
        if not reasons and not other:
            return JSONResponse(
                {"error": "Pick at least one reason you were given (or type it in)."},
                status_code=400)
        result = _rescue.decode_rejections(body)
        picked = ", ".join(_rescue.REJECTION_BY_ID[r]["label"] for r in reasons) or "(free text)"
        summary = f"Rejection reasons decoded:\n  {picked}"
        if other:
            summary += f"\n  Other: {other}"
        lead = _rescue.capture_generic_lead(
            body, source="rejection", summary=summary,
            extra={"reasons": reasons, "other": other})
        result["lead"] = lead
        return result

    # ── PUBLIC Tool 3: prequal readiness quiz ───────────────────────────────
    # A fast "are you ready to pass?" check across the things that actually
    # decide a grade. Returns a readiness score + the open gaps. Public +
    # email-gated; captures the lead into Origin.
    @app.get("/rescue/readiness/config")
    def rescue_readiness_config():
        return {
            "platforms": _rescue.PLATFORMS,
            "questions": [{"id": q["id"], "q": q["q"]} for q in _rescue.QUIZ],
        }

    @app.post("/rescue/readiness")
    def rescue_readiness(body: dict = Body(...)):
        email = (body.get("email") or "").strip()
        if "@" not in email or "." not in email.split("@")[-1]:
            return JSONResponse({"error": "A valid email is required to see your results."},
                                status_code=400)
        if not (body.get("answers") or {}):
            return JSONResponse({"error": "Answer the quiz so we can score your readiness."},
                                status_code=400)
        result = _rescue.score_readiness(body)
        summary = (f"Readiness score: {result['score']}% ({result['band']}). "
                   f"Open gaps: {result['gap_count']} of {result['total_questions']}.")
        lead = _rescue.capture_generic_lead(
            body, source="readiness", summary=summary,
            extra={"score": result["score"], "band": result["band"]})
        result["lead"] = lead
        return result

    @app.post("/rescue/handle")
    def rescue_handle(body: dict = Body(...)):
        """One-click 'let us handle it for you' from any free tool. Logs a
        high-intent lead and fires a hot-lead notification to the inbox so the
        visitor never has to call or email to get the fix started."""
        email = (body.get("email") or "").strip()
        if "@" not in email or "." not in email.split("@")[-1]:
            return JSONResponse({"error": "A valid email is required so we can start your fix."},
                                status_code=400)
        tool = (body.get("tool") or "tool").strip() or "tool"
        summary = (body.get("summary") or "").strip()
        lead = _rescue.capture_handle(body, tool=tool, summary=summary)
        return {"ok": True, "lead": lead}

    # ── INTERNAL "Gap Finder" (Chris-only) ──────────────────────────────────
    # Load a contractor's documents + industry/state/operators and get back
    # every compliance gap. The PAGE is a shell (like the main UI); the WORK
    # endpoint lives under /api so the auth middleware gates it — this is an
    # internal tool, NOT client-facing like /rescue.
    from . import gaps as _gaps
    from . import contractors as _contractors

    gaps_html = Path(__file__).parent / "webui" / "gaps.html"
    dashboard_html = Path(__file__).parent / "webui" / "dashboard.html"
    recordability_html = Path(__file__).parent / "webui" / "recordability.html"

    # ── Recordability Advisor (29 CFR 1904) — internal tool ────────────────
    from . import recordability as _rec

    @app.get("/recordability", response_class=HTMLResponse)
    def recordability_page():
        if recordability_html.is_file():
            return recordability_html.read_text(encoding="utf-8")
        return "<h1>Recordability Advisor</h1><p>Tool page missing.</p>"

    @app.get("/api/recordability/schema")
    def recordability_schema():
        return _rec.intake_schema()

    @app.post("/api/recordability/evaluate")
    def recordability_evaluate(body: dict = Body(...)):
        try:
            return _rec.evaluate(body or {})
        except Exception as e:  # never 500 on a bad-facts payload
            return JSONResponse({"error": str(e)}, status_code=400)

    # ── Company Scoping (tailored required-standard set) — internal tool ────
    from . import scoping as _scoping
    scoping_html = Path(__file__).parent / "webui" / "scoping.html"

    @app.get("/scoping", response_class=HTMLResponse)
    def scoping_page():
        if scoping_html.is_file():
            return scoping_html.read_text(encoding="utf-8")
        return "<h1>Company Scoping</h1><p>Tool page missing.</p>"

    @app.get("/api/scoping/schema")
    def scoping_schema():
        return _scoping.intake_schema()

    @app.post("/api/scoping/resolve")
    def scoping_resolve(body: dict = Body(...)):
        try:
            return _scoping.scope_company(body or {})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/scoping/suggest")
    def scoping_suggest(industry: str = ""):
        # Which activity checkboxes should the Gap Finder pre-check for this trade?
        try:
            return _scoping.suggest_activities(industry)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/scoping/naics")
    def scoping_naics(q: str = "", limit: int = 8):
        # Plain-English trade/company description -> candidate 2022 NAICS codes.
        # Lets the Companies tab auto-fill the NAICS number from what the user types.
        from . import compliance_kb as _kb
        try:
            rows = _kb.naics_search(q, limit=limit) or []
            results = [{"code": r.get("code"), "title": r.get("title"),
                        "level_name": r.get("level_name", "")} for r in rows]
            return {"ok": True, "results": results}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/scoping/industries")
    def scoping_industries():
        # Curated selectable trade/industry catalog for the Companies tab.
        # Selecting a label auto-fills its NAICS and arms hazard triggers.
        try:
            return {"ok": True, "industries": _scoping.industries()}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/scoping/states")
    def scoping_states():
        # Every selectable U.S. jurisdiction for the state dropdown.
        from . import compliance_kb as _kb
        try:
            return {"ok": True, "states": _kb.us_states()}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/scoping/jurisdiction")
    def scoping_jurisdiction(state: str = ""):
        # Deterministic State Plan / Federal OSHA / both for the selected state.
        from . import compliance_kb as _kb
        try:
            return {"ok": True, "jurisdiction": _kb.jurisdiction_for(state)}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    # ── HazCom chemical-inventory builder (29 CFR 1910.1200) — internal tool ──
    from . import hazcom as _hazcom
    hazcom_html = Path(__file__).parent / "webui" / "hazcom.html"

    @app.get("/hazcom", response_class=HTMLResponse)
    def hazcom_page():
        if hazcom_html.is_file():
            return hazcom_html.read_text(encoding="utf-8")
        return "<h1>HazCom Inventory</h1><p>Tool page missing.</p>"

    @app.get("/api/hazcom/schema")
    def hazcom_schema():
        return _hazcom.intake_schema()

    @app.get("/api/hazcom/identify")
    def hazcom_identify(name: str = ""):
        try:
            return _hazcom.identify_chemical(name)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/api/hazcom/inventory")
    def hazcom_inventory(body: dict = Body(...)):
        try:
            return _hazcom.build_inventory(body or {})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/api/hazcom/program")
    def hazcom_program(body: dict = Body(...)):
        try:
            return {"program": _hazcom.render_hazcom_program(body or {}, sector=(body or {}).get("sector", ""))}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    # ── Photo walk-through audit (vision hazard → verbatim OSHA) — internal ──
    # Upload site/machine photos; a vision model names each visible hazard in
    # PLAIN LANGUAGE (never a CFR number), and Origin maps every hazard to the
    # exact OSHA standard from its own KB, attaching the verbatim law text and
    # the official osha.gov link. Same bulletproof principle as the citation
    # guard: the model never emits a citation, Origin resolves it deterministically.
    from . import photo_audit as _photo_audit
    photo_audit_html = Path(__file__).parent / "webui" / "photo_audit.html"

    @app.get("/photo-audit", response_class=HTMLResponse)
    def photo_audit_page(request: Request):
        # Require a session (owner/GC/contractor) — never anonymous. Without this
        # gate anyone with the link opened a fully working audit and, from there,
        # the rest of the app. Unauthenticated visitors go to the sign-in screen.
        from starlette.responses import RedirectResponse
        authed = (_admin_session_ok(request) or bool(_gc_slug_ok(request))
                  or _client_session_ok(request))
        if not authed and token and (request.query_params.get("token") == token):
            authed = True
        if not authed:
            return RedirectResponse("/sie/login", status_code=302)
        if photo_audit_html.is_file():
            return photo_audit_html.read_text(encoding="utf-8")
        return "<h1>Photo Walk-Through Audit</h1><p>Tool page missing.</p>"

    # ── Safety Intelligence Engine — unified console ─────────────────────────
    # One page over all six SIE phases (monitor / companies / programs /
    # prequal / audits / citation). Page is a shell; the /api/* endpoints it
    # calls are the deterministic engines, token-gated like the rest.
    sie_html = Path(__file__).parent / "webui" / "sie.html"

    @app.get("/sie", response_class=HTMLResponse)
    def sie_console(request: Request):
        # Unified front door: require a session. Owner/GC land in the console;
        # a signed-in client belongs in their own scoped portal; everyone else
        # is sent to the /sie sign-in screen.
        from starlette.responses import RedirectResponse
        authed = _admin_session_ok(request) or bool(_gc_slug_ok(request))
        if not authed and token and (request.query_params.get("token") == token):
            authed = True   # legacy owner access token still opens the console
        if not authed:
            try:
                from . import portal as _portal
                cp = _portal._unsign(request.cookies.get(_portal.CLIENT_COOKIE, ""))
                if cp and cp.get("role") == "client":
                    return RedirectResponse("/portal", status_code=302)
            except Exception:
                pass
            return RedirectResponse("/sie/login", status_code=302)
        if sie_html.is_file():
            return sie_html.read_text(encoding="utf-8")
        return "<h1>Safety Intelligence Engine</h1><p>Console page missing.</p>"

    # The prior multi-tab console is preserved verbatim at /sie-classic so no
    # existing workflow (companies / programs / prequal / audits / review /
    # training) is lost when the new Command Center dashboard takes over /sie.
    sie_classic_html = Path(__file__).parent / "webui" / "sie_classic.html"

    @app.get("/sie-classic", response_class=HTMLResponse)
    def sie_console_classic():
        if sie_classic_html.is_file():
            return sie_classic_html.read_text(encoding="utf-8")
        return "<h1>Safety Intelligence Engine</h1><p>Console page missing.</p>"

    # ── Field Checklist — the Universal Regulatory Router's field page ────────
    # Renders the dynamic checklist + AHA matrix generated from regulatory text
    # by checklist_engine. The page is a shell; /api/checklist/* endpoints are
    # the deterministic engine, owner-gated like the rest.
    checklist_html = Path(__file__).parent / "webui" / "checklist.html"

    @app.get("/checklist", response_class=HTMLResponse)
    def checklist_page():
        if checklist_html.is_file():
            return checklist_html.read_text(encoding="utf-8")
        return "<h1>Field Checklist</h1><p>Tool page missing.</p>"

    # ── Brain Router — the agency-mode toggle console (Screen 1 "Smart Profile") ──
    # Turn on the regulatory agencies that govern this account and the whole app
    # reshapes: nav gains that agency's tabs and the citation resolver is scoped
    # to only its CFR lane. Page is a shell over /api/brain-router/*.
    brain_router_html = Path(__file__).parent / "webui" / "brain_router.html"

    @app.get("/brain-router", response_class=HTMLResponse)
    def brain_router_page():
        if brain_router_html.is_file():
            return brain_router_html.read_text(encoding="utf-8")
        return "<h1>Brain Router</h1><p>Tool page missing.</p>"

    # ── Form Vault — split-screen official-form filler (Screen 3) ─────────────
    # Worker answers a short form on the left; Origin auto-fills the official
    # government form on the right and stamps "Audit Ready". Shell over
    # /api/form-vault/*; agency set scoped by the Brain Router.
    form_vault_html = Path(__file__).parent / "webui" / "form_vault.html"

    @app.get("/form-vault", response_class=HTMLResponse)
    def form_vault_page():
        if form_vault_html.is_file():
            return form_vault_html.read_text(encoding="utf-8")
        return "<h1>Form Vault</h1><p>Tool page missing.</p>"

    # ── App launcher (the installed PWA opens here) ───────────────────────────
    # Routes each phone to whichever dashboard it is signed in to — owner/admin,
    # GC, or contractor — instead of dumping everyone on the internal AI console.
    # Falls back to a sign-in picker when no session is present.
    _app_launcher_html = Path(__file__).parent / "webui" / "app.html"

    @app.get("/app", response_class=HTMLResponse)
    def app_launcher():
        if _app_launcher_html.is_file():
            return _app_launcher_html.read_text(encoding="utf-8")
        return "<h1>Origin</h1><p>Launcher page missing.</p>"

    @app.post("/api/photo-audit/analyze")
    async def photo_audit_analyze(request: Request):
        # Accepts multipart image uploads under field "files". Reads each into
        # memory (raw bytes + media type) and runs the vision → KB mapping. The
        # live vision-capable provider is the engine's own agent LLM.
        form = await request.form()
        uploads = form.getlist("files")
        images: list = []
        for f in uploads:
            if not hasattr(f, "filename") or not f.filename:
                continue
            mt = getattr(f, "content_type", "") or "image/jpeg"
            if not str(mt).lower().startswith("image/"):
                continue
            data = await f.read()
            if data:
                images.append((data, mt))
        if not images:
            return JSONResponse(
                {"error": "Attach at least one photo (JPEG/PNG) to inspect."},
                status_code=400)
        # Optional: bind this walk-through to a company. When supplied, the finished
        # report is persisted as a durable audit and its high-severity findings
        # auto-open CAPAs that feed the deterministic risk engine (Phase 4). Absent
        # a company, the tool behaves exactly as before — a one-off screening.
        _audit_company = (form.get("company") or "").strip()
        # The vision call inside analyze() is a SYNCHRONOUS network request. If we
        # ran it directly here it would block the whole async event loop until the
        # model replied — during which Railway's health checks get no response and
        # it recycles the worker, surfacing to the user as a raw 502 gateway error.
        # So we push the blocking work onto a worker thread and cap it with a hard
        # timeout, letting the loop stay responsive and always returning clean JSON.
        import asyncio
        from starlette.concurrency import run_in_threadpool
        # Build the resilient vision chain (primary + automatic fallbacks). If
        # the newest model ever disappears, the next brain covers it — no
        # unexplainable failure for a customer.
        chain = eng.vision_chain() or [eng.agent.llm]
        primary, fallbacks = chain[0], chain[1:]
        # Brain Router scope: if this tenant has toggled a specific set of agency
        # brains, restrict the citations to only those authorities. An unset
        # profile yields None → no scoping (identical to prior behavior).
        active_authorities = None
        try:
            if _brain is not None:
                active_authorities = _brain.citation_authorities(
                    getattr(request.state, "sie_gc_slug", None))
        except Exception:
            active_authorities = None
        try:
            report = await asyncio.wait_for(
                run_in_threadpool(_photo_audit.analyze, images,
                                  provider=primary, fallbacks=fallbacks,
                                  active_authorities=active_authorities),
                timeout=110,
            )
            # Phase 4: if a company was named, remember the walk-through and fold
            # its findings into that company's posture (audit record + CAPAs +
            # risk). Non-fatal — persistence never breaks the inspection result.
            if _audit_company and isinstance(report, dict) and report.get("findings"):
                try:
                    from . import audit_engine as _audit
                    _rec = await run_in_threadpool(
                        _audit.record_audit, report, company=_audit_company, by="owner")
                    report["audit"] = {
                        "id": _rec["id"], "company_id": _rec["company_id"],
                        "summary": _rec["summary"], "capa_ids": _rec["capa_ids"],
                    }
                except Exception as _ae:
                    print(f"[photo-audit] audit persist failed: {_ae}")
            return report
        except asyncio.TimeoutError:
            return JSONResponse(
                {"error": "The inspection took too long and timed out. "
                          "Try again with a single, clearer photo."},
                status_code=200)
        except Exception as e:  # every brain failed — report cleanly, never 500/502
            # Keep the technical reason in the server log for debugging, but show
            # the customer a calm, non-alarming message.
            print(f"[photo-audit] all vision providers failed: {e}")
            return JSONResponse(
                {"error": "The photo inspector is temporarily unavailable. "
                          "Please try again in a moment."},
                status_code=200)

    # ── PWA: manifest, service worker, and app icons ──────────────────────
    # These make the platform installable to a phone/desktop home screen so the
    # camera-based Photo Audit works like a native field app. Served at root so
    # the service-worker scope covers the whole site. None of these live under
    # /api, so the access-token middleware leaves them public (as it must — the
    # browser fetches them before any token is available).
    _webui_dir = Path(__file__).parent / "webui"

    @app.get("/manifest.webmanifest")
    def pwa_manifest():
        f = _webui_dir / "manifest.webmanifest"
        if f.is_file():
            return Response(f.read_text(encoding="utf-8"),
                            media_type="application/manifest+json")
        return JSONResponse({"error": "manifest missing"}, status_code=404)

    @app.get("/sw.js")
    def pwa_service_worker():
        f = _webui_dir / "sw.js"
        if f.is_file():
            # Service-Worker-Allowed lets the root-scoped SW control the whole app.
            return Response(f.read_text(encoding="utf-8"),
                            media_type="application/javascript",
                            headers={"Service-Worker-Allowed": "/",
                                     "Cache-Control": "no-cache"})
        return Response("// missing", media_type="application/javascript",
                        status_code=404)

    @app.get("/pwa-install.js")
    def pwa_install_js():
        f = _webui_dir / "pwa-install.js"
        if f.is_file():
            return Response(f.read_text(encoding="utf-8"),
                            media_type="application/javascript",
                            headers={"Cache-Control": "no-cache"})
        return Response("// missing", media_type="application/javascript",
                        status_code=404)

    @app.get("/apple-touch-icon.png")
    @app.get("/apple-touch-icon-precomposed.png")
    def pwa_apple_icon():
        f = _webui_dir / "apple-touch-icon.png"
        if f.is_file():
            return FileResponse(str(f), media_type="image/png")
        return Response(status_code=404)

    @app.get("/icons/{name}")
    def pwa_icon(name: str):
        # Only serve known PNG icon files from the webui/icons folder.
        import re as _re
        if not _re.fullmatch(r"[A-Za-z0-9._-]+\.png", name or ""):
            return Response(status_code=404)
        f = _webui_dir / "icons" / name
        if f.is_file():
            return FileResponse(str(f), media_type="image/png")
        return Response(status_code=404)

    @app.get("/gaps", response_class=HTMLResponse)
    def gaps_page():
        if gaps_html.is_file():
            return gaps_html.read_text(encoding="utf-8")
        return "<h1>Gap Finder</h1><p>Tool page missing.</p>"

    @app.get("/api/gaps/config")
    def gaps_config():
        return {
            "operators": [c["hiring_client"] for c in _kb.list_hiring_clients()],
            "pass_ratio": _gaps.PASS_RATIO,
        }

    @app.post("/api/gaps/analyze")
    async def gaps_analyze(request: Request):
        import tempfile
        form = await request.form()
        industry = (form.get("industry") or "").strip()
        state = (form.get("state") or "").strip() or None
        ops_raw = (form.get("operators") or "").strip()
        operators = [o.strip() for o in ops_raw.split(",") if o.strip()] or None
        contractor = (form.get("contractor") or "").strip()
        # Company-scoping activity triggers (comma-separated keys or repeated field).
        acts = form.getlist("activities")
        if len(acts) == 1 and "," in acts[0]:
            acts = [a.strip() for a in acts[0].split(",")]
        activities = [a.strip() for a in acts if a and a.strip()] or None
        if not industry:
            return JSONResponse({"error": "industry is required"}, status_code=400)

        uploads = form.getlist("files")
        docs = []
        with tempfile.TemporaryDirectory(prefix="gapfinder_") as tmp:
            for f in uploads:
                if not hasattr(f, "filename") or not f.filename:
                    continue
                dest = Path(tmp) / Path(f.filename).name
                with open(dest, "wb") as out:
                    while True:
                        chunk = await f.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                text = _gaps.extract_text(str(dest))
                docs.append({"name": f.filename, "text": text})

        report = _gaps.find_gaps(industry, state=state, operators=operators,
                                 docs=docs, activities=activities)
        # If a contractor name was given, snapshot this analysis onto the
        # dashboard (preserving any manual status dots already set).
        if contractor:
            try:
                slug = _contractors.save_snapshot(
                    contractor, report, industry=industry,
                    state=state, operators=operators or [])
                report["contractor_slug"] = slug
                report["contractor_name"] = contractor
            except Exception:
                pass
        return report

    @app.post("/api/gaps/draft")
    def gaps_draft(body: dict = Body(...)):
        """Phase 2: render fillable written programs for the given standard ids
        and return them bundled as a downloadable .zip."""
        ids = body.get("ids") or []
        if not ids:
            return JSONResponse({"error": "no program ids provided"}, status_code=400)
        drafts = _gaps.draft_programs(
            ids,
            company=body.get("company"),
            effective_date=body.get("effective_date"),
        )
        if not drafts:
            return JSONResponse(
                {"error": "none of those standards have a draftable written program"},
                status_code=400)
        import io as _io
        import zipfile as _zip
        # Prefer branded Word docs; fall back to markdown if python-docx is
        # absent (e.g. before the image is rebuilt) so a draft is never lost.
        want_md = str(body.get("format") or "").lower() in ("md", "markdown")
        buf = _io.BytesIO()
        with _zip.ZipFile(buf, "w", _zip.ZIP_DEFLATED) as z:
            index = ["# Draft written programs",
                     "",
                     "Fill every {{PLACEHOLDER}} and replace each [[prompt]] with your "
                     "company-specific procedure before submitting.", ""]
            for d in drafts:
                docx_bytes = None if want_md else _gaps.program_docx_bytes(d["title"], d["markdown"])
                if docx_bytes:
                    name = d["filename"].rsplit(".", 1)[0] + ".docx"
                    z.writestr(name, docx_bytes)
                else:
                    name = d["filename"]
                    z.writestr(name, d["markdown"])
                index.append(f"- {d['title']} ({d['citation']}) — {name}")
            z.writestr("00_INDEX.md", "\n".join(index))
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="origin-draft-programs.zip"'},
        )

    # ── Self-learning: teach Origin from the UI + read back brain intel ──────
    # Both live under /api so the auth middleware gates them (Chris-only). learn()
    # writes a durable `learned` record to the persistent data volume; it is
    # REFERENCE knowledge only and never enters the document send-gate. The intel
    # endpoint is the "what does Origin already know about this" lookup the pages
    # use — it surfaces prequal/abatement/learned knowledge and so gets smarter
    # every time Origin is taught something new.
    @app.post("/api/learn")
    def api_learn(body: dict = Body(...)):
        text = (body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "Type something to teach Origin first."},
                                status_code=400)
        tags = body.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        try:
            rec = _kb.learn(
                text,
                title=(body.get("title") or None),
                source=(body.get("source") or "taught in app"),
                category=(body.get("category") or None),
                tags=tags,
            )
        except (ValueError, OSError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"saved": True, "id": rec["id"], "title": rec["title"],
                "total_learned": len(_kb.learned_knowledge())}

    @app.get("/api/brain/intel")
    def api_brain_intel(q: str = "", limit: int = 6):
        q = (q or "").strip()
        if not q:
            return {"query": q, "intel": []}
        return {"query": q, "intel": _kb.brain_intel(q, limit=limit)}

    # ── PUBLIC "Cited by OSHA?" citation → required-program mapper ──────────
    # A contractor who just got an OSHA citation types the cited CFR standard
    # and instantly learns which written program that standard requires and
    # what abatement looks like. Public + email-gated (captures the lead) like
    # /rescue. The branded draft itself is built with the /api/gaps/draft
    # pipeline once the lead is qualified — this is the front door to it.
    import re as _cite_re

    def _resolve_citation(raw: str):
        """Map a messy user-typed OSHA citation to a KB record. Accepts
        '1910.147', '29 CFR 1910.147', '1910.147(c)(1)', '1926.501', '1904'."""
        raw = (raw or "").strip()
        if not raw:
            return None
        rec = _kb.by_citation(raw)                       # exact canonical
        if rec:
            return rec
        part = sec = None
        m = _cite_re.search(r"(\d{3,4})\.(\d+)", raw)    # pull part.section
        if m:
            part, sec = m.group(1), m.group(2)
        if part and sec:
            canon = f"29 CFR {part}.{sec}"
            rec = _kb.by_citation(canon)
            if rec:
                return rec
            for r in _kb.all_records():                  # prefix / range start
                c = r.get("citation") or ""
                if c.startswith(canon) or c.startswith(f"29 CFR {part}.{sec}-"):
                    return r
            for r in _kb.all_records():                  # inside a .a-.b range
                c = r.get("citation") or ""
                rm = _cite_re.search(rf"29 CFR {part}\.(\d+)-\.(\d+)", c)
                if rm and int(rm.group(1)) <= int(sec) <= int(rm.group(2)):
                    return r
        mp = _cite_re.search(r"\b(\d{3,4})\b", raw)       # part-only e.g. 1904
        if mp:
            rec = _kb.by_citation(f"29 CFR {mp.group(1)}")
            if rec:
                return rec
        hits = _kb.search(raw, limit=1)                   # last resort keyword
        return hits[0] if hits else None

    def _capture_citation_lead(body, matched, raw):
        import json as _json, time as _time
        lead = {
            "ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "citation",
            "name": (body.get("name") or "").strip(),
            "company": (body.get("company") or "").strip(),
            "email": (body.get("email") or "").strip(),
            "phone": (body.get("phone") or "").strip(),
            "typed_citation": raw,
            "matched_citation": (matched.get("citation") if matched else None),
            "matched_program": (matched.get("title") if matched else None),
            # The specific program id the cited standard requires — lets a
            # convert-to-client pre-load the exact abatement document (Phase 4).
            "program_id": (matched.get("id") if matched else None),
        }
        try:
            _rescue.DATA_DIR.mkdir(parents=True, exist_ok=True)
            with _rescue.LEADS_FILE.open("a", encoding="utf-8") as fh:
                fh.write(_json.dumps(lead) + "\n")
        except Exception:
            pass
        try:
            from .compliance import send_email, resend_configured, smtp_configured
            if resend_configured() or smtp_configured():
                prog = f" - {lead['matched_program']}" if lead["matched_program"] else ""
                b = (f"New OSHA-citation lead from the site.\n\n"
                     f"Name:     {lead['name'] or '(not given)'}\n"
                     f"Company:  {lead['company'] or '(not given)'}\n"
                     f"Email:    {lead['email']}\n"
                     f"Phone:    {lead['phone'] or '(not given)'}\n"
                     f"Typed:    {lead['typed_citation']}\n"
                     f"Matched:  {lead['matched_citation'] or '(no direct map)'}{prog}\n")
                send_email(_rescue.NOTIFY_TO,
                           f"OSHA citation lead: {lead['company'] or lead['email']}", b)
        except Exception:
            pass

    @app.post("/citation/lookup")
    def citation_lookup(body: dict = Body(...)):
        email = (body.get("email") or "").strip()
        if "@" not in email or "." not in email.split("@")[-1]:
            return JSONResponse(
                {"error": "A valid email is required to see your citation plan."},
                status_code=400)
        raw = (body.get("citation") or "").strip()
        if not raw:
            return JSONResponse(
                {"error": "Enter the OSHA standard you were cited under (e.g. 1910.147)."},
                status_code=400)
        rec = _resolve_citation(raw)
        if not rec:
            _capture_citation_lead(body, matched=None, raw=raw)
            return {
                "found": False,
                "typed": raw,
                "message": ("We don't have that exact standard pre-mapped — that's "
                            "precisely what our review is for. We'll pull the cited "
                            "standard, confirm the written program it requires, and "
                            "build your abatement packet. Call 832-710-1558 and we'll "
                            "walk it with you now."),
            }
        wp = (rec.get("written_program") or "").strip().lower()
        elems = rec.get("required_elements") or []
        _capture_citation_lead(body, matched=rec, raw=raw)
        return {
            "found": True,
            "typed": raw,
            "citation": rec.get("citation"),
            "title": rec.get("title"),
            "needs_program": wp in ("yes", "conditional"),
            "written_program": rec.get("written_program"),
            "applicability": rec.get("applicability"),
            "required_elements": elems[:8],
            "element_count": len(elems),
            "training": rec.get("training"),
            "recordkeeping": rec.get("recordkeeping"),
            "program_id": rec["id"],
            "can_build": _kb.render_program(rec["id"]) is not None,
        }

    @app.post("/api/citation/draft")
    def citation_draft(body: dict = Body(...)):
        """Internal (token-gated): paste an OSHA citation, get the branded
        written program(s) that standard requires as a downloadable .zip.
        Wires citation → document end to end on top of the Gap Finder drafter."""
        rec = _resolve_citation(body.get("citation") or "")
        if not rec:
            return JSONResponse(
                {"error": "could not map that citation to a standard"}, status_code=400)
        drafts = _gaps.draft_programs(
            [rec["id"]], company=body.get("company"),
            effective_date=body.get("effective_date"))
        if not drafts:
            return JSONResponse(
                {"error": "that standard has no draftable written program"},
                status_code=400)
        import io as _io
        import zipfile as _zip
        want_md = str(body.get("format") or "").lower() in ("md", "markdown")
        buf = _io.BytesIO()
        with _zip.ZipFile(buf, "w", _zip.ZIP_DEFLATED) as z:
            index = [f"# Written program(s) required by {rec.get('citation')}",
                     "",
                     "Fill every {{PLACEHOLDER}} and replace each [[prompt]] with your "
                     "company-specific procedure before submitting for abatement.", ""]
            for d in drafts:
                docx_bytes = None if want_md else _gaps.program_docx_bytes(d["title"], d["markdown"])
                if docx_bytes:
                    name = d["filename"].rsplit(".", 1)[0] + ".docx"
                    z.writestr(name, docx_bytes)
                else:
                    name = d["filename"]
                    z.writestr(name, d["markdown"])
                index.append(f"- {d['title']} ({d['citation']}) — {name}")
            z.writestr("00_INDEX.md", "\n".join(index))
        return Response(
            content=buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition":
                     'attachment; filename="origin-citation-programs.zip"'},
        )

    # ── INTERNAL Contractor Compliance Dashboard (Chris-only) ───────────────
    # A living roll-up of every contractor run through the Gap Finder. Page is
    # a public shell; the /api endpoints are token-gated like the rest.
    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_page():
        if dashboard_html.is_file():
            return dashboard_html.read_text(encoding="utf-8")
        return "<h1>Dashboard</h1><p>Tool page missing.</p>"

    @app.get("/api/dashboard/contractors")
    def dashboard_contractors():
        return {"contractors": _contractors.list_contractors(),
                "dimensions": _contractors.DIMENSIONS}

    @app.get("/api/dashboard/contractor/{slug}")
    def dashboard_contractor(slug: str):
        rec = _contractors.get_contractor(slug)
        if not rec:
            return JSONResponse({"error": "contractor not found"}, status_code=404)
        return rec

    @app.post("/api/dashboard/contractor/{slug}/status")
    def dashboard_set_status(slug: str, body: dict = Body(...)):
        dim = (body.get("dimension") or "").strip()
        value = (body.get("value") or "").strip()
        if not _contractors.set_status(slug, dim, value):
            return JSONResponse({"error": "could not set status (bad slug/dimension/value)"},
                                status_code=400)
        return _contractors.get_contractor(slug)

    @app.delete("/api/dashboard/contractor/{slug}")
    def dashboard_delete(slug: str):
        ok = _contractors.delete_contractor(slug)
        return {"deleted": ok}

    @app.post("/api/dashboard/seed")
    def dashboard_seed():
        # Load the four demo contractors so the board shows a full
        # green/yellow/red mix before any real client documents exist.
        try:
            from . import seed_dashboard as _seed
            written = _seed.seed_samples(force=True)
            return {"seeded": written, "count": len(written)}
        except Exception as exc:  # never 500 the button
            return JSONResponse({"error": f"seed failed: {exc}"}, status_code=400)

    # ── Client Compliance Portal (customer storefront + admin console) ──
    # Isolated module; wrapped so a portal bug can never break the main app.
    try:
        from . import portal as _portal
        _portal.register_portal(app)
    except Exception as _portal_exc:  # pragma: no cover
        print(f"[portal] disabled — registration failed: {_portal_exc}")

    # ── Unified /sie sign-in (master owner login + client + magic link) ──
    # Gives the Safety Intelligence Engine its own front door. Isolated + non-
    # fatal; reuses the portal's proven cookie/secret scheme (see sie_gate.py).
    try:
        from . import sie_gate as _sie_gate
        _sie_gate.register_sie_gate(app)
    except Exception as _sie_gate_exc:  # pragma: no cover
        print(f"[sie_gate] disabled — registration failed: {_sie_gate_exc}")

    # ── ISN Upload Tracker (abatement status ladder) ──
    # Additive overlay on the portal's own client.json; isolated + non-fatal.
    try:
        from . import abatement as _abatement
        _abatement.register_abatement(app)
    except Exception as _abate_exc:  # pragma: no cover
        print(f"[abatement] disabled — registration failed: {_abate_exc}")

    # ── Safety Intelligence Engine — Phase 1 (knowledge store + citation) ──
    # Deterministic, offline-capable OSHA citation analyzer over a versioned,
    # traceable knowledge store. Fully isolated + non-fatal, and requires no
    # external LLM key to function. Registers the base knowledge version on boot.
    try:
        from . import knowledge_store as _ks
        from . import citation_engine as _citation
        _ks.ensure_base()
        _citation.register_citation(app)
    except Exception as _cite_exc:  # pragma: no cover
        print(f"[citation] disabled — registration failed: {_cite_exc}")

    # ── Safety Intelligence Engine — Phase 2 (company profile + risk + CAPA) ──
    # Connects the Citation Engine to a company's posture: a citation becomes a
    # tracked corrective action, the company gets a deterministic risk score, and
    # the profile carries the company-specific required-standard set. Isolated +
    # non-fatal + fully offline, same as every other SIE module.
    try:
        from . import capa as _capa
        from . import company_profile as _company
        from . import citation_engine as _ce
        from . import program_engine as _program
        _capa.register_capa(app)
        _company.register_company(app)
        _program.register_program(app)   # Phase 3: program/training/JSA package
        from . import audit_engine as _audit
        _audit.register_audit(app)       # Phase 4: walk-through audit → CAPA → risk
        from . import prequal_engine as _prequal
        _prequal.register_prequal(app)   # Phase 5: ISN/Avetta/Veriforce readiness + gaps
        from . import monitor_engine as _monitor
        _monitor.register_monitor(app)   # Phase 6: autonomous portfolio monitoring + alerts

        # Combined flow: analyze a citation AND fold it into the company posture in
        # one call — analyze → save → auto-open CAPA → recompute risk.
        from fastapi import Body as _Body, Request as _Request
        from fastapi.responses import JSONResponse as _JSON

        @app.post("/api/citation/track")
        async def citation_track(request: _Request, body: dict = _Body(default=None)):
            payload = body if isinstance(body, dict) else {}
            if not payload:
                try:
                    payload = await request.json()
                except Exception:
                    payload = {}
            if not isinstance(payload, dict) or not (
                    payload.get("standard") or payload.get("text") or payload.get("description")):
                return _JSON({"error": "Provide at least a 'standard' or 'text', plus a "
                                       "'company' to track."}, status_code=400)
            try:
                record = _ce.analyze(payload)      # deterministic, offline
                _ce.save(record)
                out = {"ok": True, "citation": record}
                company = (payload.get("company") or "").strip()
                if company:
                    _company.ensure(company)
                    capa_rec = _capa.open_from_citation(
                        record, company_id=payload.get("company_id", company),
                        by=payload.get("by", "owner"))
                    out["capa"] = capa_rec
                    out["risk"] = _company.compute_risk(payload.get("company_id", company))
                return out
            except Exception as exc:  # never 500 the tool
                return _JSON({"error": f"Citation tracking failed: {exc}"}, status_code=200)
    except Exception as _p2_exc:  # pragma: no cover
        print(f"[sie-phase2] disabled — registration failed: {_p2_exc}")

    # ── Safety Intelligence Engine — Evidence Spine (Stage 1) ──
    # A derived, rebuildable index over the file-based JSON collections: the
    # compliance chain (company → finding → CAPA → source standard, plus
    # citations) made queryable across engines. The spine is NEVER authoritative
    # — rebuild_spine() reconstructs it entirely from the source JSON, so the
    # source of truth and the offline no-LLM guarantee are untouched. Isolated +
    # non-fatal, same as every other SIE module.
    try:
        from . import spine as _spine
        _spine.register_spine(app)
    except Exception as _spine_exc:  # pragma: no cover
        print(f"[spine] disabled — registration failed: {_spine_exc}")

    # ── Safety Intelligence Engine — Applicable Requirements (Stage 2) ──
    # Given a company profile, resolve the tailored set of standards that apply
    # and WHY, each tagged with exactly one of the four classifications
    # (OSHA-required / Origin recommendation / Best practice / Customer
    # requirement) — never blended. Derived on demand from the profile via
    # scoping; no new source of truth. Isolated + non-fatal.
    try:
        from . import requirements_engine as _reqs
        _reqs.register_requirements(app)
    except Exception as _reqs_exc:  # pragma: no cover
        print(f"[requirements] disabled — registration failed: {_reqs_exc}")

    # ── Safety Intelligence Engine — Human-Review Queue (Stage 5) ──
    # Makes review a first-class store: unmatched / low-confidence audit findings
    # become ReviewItems with a logged reviewer decision, and every decision writes
    # back to the audit -> CAPA chain (approve opens the CAPA, reject dismisses it).
    # The pending side is derived from the audits (ingest_from_audits, idempotent);
    # nothing model-derived reaches a customer as authoritative without a logged
    # human decision available. Owner-only (not in the GC allowlist). Isolated +
    # non-fatal, fully offline — the dependency runs review -> audit, never back.
    try:
        from . import review_engine as _review
        _review.register_review(app)
    except Exception as _review_exc:  # pragma: no cover
        print(f"[review] disabled — registration failed: {_review_exc}")

    # ── Safety Intelligence Engine — Training Intelligence (Stage 6) ──
    # Turns training from KB-static into a living matrix: employee/role -> required
    # training (derived from the requirements engine, kept only where the OSHA 2254
    # KB confirms a real training obligation) -> completion -> expiration. The
    # catalog is derived on every call; the only persisted, authoritative data is
    # the roster + completion records. A curated refresher-cadence table drives
    # deterministic expiration, which feeds monitor_engine's sweep (training_expired
    # / training_due_soon). Owner-only (not in the GC allowlist). Isolated +
    # non-fatal, fully offline — dependency runs training -> requirements/KB, never
    # back.
    try:
        from . import training_engine as _training
        _training.register_training(app)
    except Exception as _training_exc:  # pragma: no cover
        print(f"[training] disabled — registration failed: {_training_exc}")

    # Universal Regulatory Router — field layer. The text of a regulation in the
    # file-based knowledge store generates dynamic mobile checklists + an AHA
    # matrix, deterministically and offline. First reference brain: USACE EM
    # 385-1-1 excavation. Owner-gated (not in the GC allowlist). Isolated + non-
    # fatal; source-agnostic so eCFR/OSHA text flows through the same parser.
    try:
        from . import checklist_engine as _checklist
        _checklist.register_checklists(app)
    except Exception as _checklist_exc:  # pragma: no cover
        print(f"[checklist] disabled — registration failed: {_checklist_exc}")

    # eCFR ingest adapter — the live "the law updates, the app updates" pipeline.
    # A deliberate owner action (POST /api/ecfr/ingest) pulls a CFR section's
    # current text from the eCFR and writes it into the same knowledge store, so
    # it generates a dynamic checklist through the parser above. Network is only
    # touched on that explicit request, never at import/boot. Owner-gated,
    # isolated + non-fatal.
    try:
        from . import ecfr_adapter as _ecfr
        _ecfr.register_ecfr(app)
    except Exception as _ecfr_exc:  # pragma: no cover
        print(f"[ecfr] disabled — registration failed: {_ecfr_exc}")

    # Brain Router — agency-mode profile. An admin toggles which regulatory
    # brains (OSHA/MSHA/EPA/BLM/DOT/USACE…) a tenant runs; that profile then
    # (1) reshapes the mobile nav, (2) scopes photo-audit citations to only the
    # enabled authorities, and (3) drives Form Vault template selection. Tenant-
    # scoped to request.state.sie_gc_slug; an unset profile means no scoping
    # (identical to prior behavior). Isolated + non-fatal.
    try:
        from . import brain_router as _brain
        _brain.register_brain_router(app)
    except Exception as _brain_exc:  # pragma: no cover
        _brain = None
        print(f"[brain_router] disabled — registration failed: {_brain_exc}")

    # ── Form Vault (Screen 3): official-government-form auto-fill engine ──────
    # A worker's short answers are projected onto the real form's field layout
    # (MSHA 5000-23, OSHA 300A, USACE AHA) deterministically — no LLM — and the
    # record is stamped "Audit Ready" once every required field is satisfied. The
    # visible form set is scoped to the agencies enabled in the Brain Router.
    # Isolated + non-fatal, same as every SIE module.
    try:
        from . import form_vault as _formvault
        _formvault.register_form_vault(app)
    except Exception as _fv_exc:  # pragma: no cover
        _formvault = None
        print(f"[form_vault] disabled — registration failed: {_fv_exc}")

    # ── RETIRED: the parallel Postgres "/platform" build ──
    # The GC tier (owner → general contractor → subcontractor), logos, and
    # two-way messaging now live natively inside the Client Compliance Portal
    # above (portal.register_portal): the owner works in /admin, each GC logs in
    # at /gc, and subs use the existing /portal. The old platform_* modules
    # (platform_auth/console/gc/media/sub/db/seed) are no longer registered so
    # there is exactly one live system. The files remain on disk for reference
    # but are intentionally NOT wired into the app.

    app.state.engine = eng
    return app
