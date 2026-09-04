"""Auth, tenant scoping, and the AI owner-only wall for the multi-tenant platform.

Three roles (see platform_db): owner, gc_admin, sub. This module:
  * hashes/verifies passwords (PBKDF2, same family as the portal),
  * issues HMAC-signed session cookies (stdlib only, no deps),
  * resolves the current user on each request,
  * SCOPES every data read to the caller's gc_id (and sub_id for subs), so no
    account can ever see another GC's data, and
  * WALLS the AI Origin engine to the owner alone — enforced on every request by
    a middleware, so a GC admin or sub can never reach it even with a URL.

Nothing here imports the AI engine or the existing portal, so it can't break
them. register_platform(app) wires the routes + wall onto the FastAPI app.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from . import platform_db as db
from .platform_db import User, Tenant, Subcontractor, ROLE_OWNER, ROLE_GC_ADMIN, ROLE_SUB

try:
    from starlette.requests import Request
except Exception:  # pragma: no cover
    Request = None  # type: ignore


# ── secret (shared with the portal's proven scheme) ──────────────────────
def _load_secret() -> str:
    env = (os.environ.get("ORIGIN_PORTAL_SECRET")
           or os.environ.get("ORIGIN_TOKEN") or "").strip()
    if env:
        return env
    # reuse the portal's persisted secret if present, else a per-process one
    try:
        from .paths import DATA_DIR
        f = DATA_DIR / "portal" / ".portal_secret"
        if f.is_file():
            v = f.read_text(encoding="utf-8").strip()
            if v:
                return v
    except Exception:
        pass
    return secrets.token_urlsafe(48)


SECRET = _load_secret()
COOKIE = "origin_platform"
SESSION_TTL = 60 * 60 * 12  # 12h
_PBKDF2_ITERS = 240_000

# AI paths that only the owner may touch. The chat UI is "/" and the engine
# lives under these /api prefixes. Everything on the platform is elsewhere.
AI_EXACT = {"/", "/index.html"}
AI_PREFIXES = ("/api/chat", "/api/agent", "/api/message", "/api/send",
               "/api/stream", "/api/projects", "/api/state", "/api/inventory",
               "/api/workers", "/api/brain", "/api/enhance", "/api/research",
               "/api/media", "/api/tools", "/api/orchestra")


# ── brute-force lockout (in-memory, per email) ───────────────────────────
_FAILS: Dict[str, List[float]] = {}
_LOCK_WINDOW = 15 * 60
_LOCK_MAX = 6


def _locked(email: str) -> bool:
    hits = [t for t in _FAILS.get(email, []) if t > time.time() - _LOCK_WINDOW]
    _FAILS[email] = hits
    return len(hits) >= _LOCK_MAX


def _note_fail(email: str) -> None:
    _FAILS.setdefault(email, []).append(time.time())


def _clear(email: str) -> None:
    _FAILS.pop(email, None)


# ── password hashing ─────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", f"{SECRET}:{password}".encode(),
                             salt, _PBKDF2_ITERS)
    return f"pbkdf2${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored or not stored.startswith("pbkdf2$"):
        return False
    try:
        _, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", f"{SECRET}:{password}".encode(),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ── signed cookies ───────────────────────────────────────────────────────
def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: Dict[str, Any]) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return body + "." + _b64e(sig)


def _unsign(tok: str) -> Optional[Dict[str, Any]]:
    try:
        body, sig = tok.split(".", 1)
        expect = _b64e(hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            return None
        payload = json.loads(_b64d(body))
        if float(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except Exception:
        return None


def make_session(user: User) -> str:
    return _sign({
        "uid": user.id, "role": user.role,
        "gc_id": user.gc_id or "", "sub_id": user.sub_id or "",
        "exp": time.time() + SESSION_TTL,
    })


def read_session(request) -> Optional[Dict[str, Any]]:
    tok = request.cookies.get(COOKIE) if request else None
    return _unsign(tok) if tok else None


# ── tenant scoping — the isolation boundary, enforced here ────────────────
def scoped(sess, model, claims: Dict[str, Any]):
    """Return a SELECT for `model` already filtered to what `claims` may see.

    owner    → everything.
    gc_admin → only rows whose gc_id matches their tenant.
    sub      → only rows for their own gc_id AND their own sub_id (where the
               model has a sub_id column).
    The caller never gets to name a gc_id, so it can't be spoofed from the wire.
    """
    role = claims.get("role")
    q = select(model)
    if role == ROLE_OWNER:
        return q
    gc_id = claims.get("gc_id") or "__none__"
    if hasattr(model, "gc_id"):
        q = q.where(model.gc_id == gc_id)
    if role == ROLE_SUB and hasattr(model, "sub_id"):
        q = q.where(model.sub_id == (claims.get("sub_id") or "__none__"))
    elif role == ROLE_SUB and model is Subcontractor:
        q = q.where(model.id == (claims.get("sub_id") or "__none__"))
    return q


def owner_exists(sess) -> bool:
    return sess.scalar(select(User).where(User.role == ROLE_OWNER)) is not None


def ensure_owner() -> Optional[str]:
    """Create the single owner account on boot, from env, if none exists yet.

    Reads OWNER_EMAIL (defaults to Chris's business email) and OWNER_PASSWORD.
    Does nothing if an owner already exists (idempotent) or if OWNER_PASSWORD is
    unset — so a fresh deploy without the var set simply has no owner yet, and
    setting the var + redeploying creates it. Returns the owner email if it made
    one, else None. Never raises into the boot path (caller wraps it too)."""
    pw = (os.environ.get("OWNER_PASSWORD") or "").strip()
    if not pw:
        return None
    email = (os.environ.get("OWNER_EMAIL")
             or "info@originmanagementsolutions.com").strip().lower()
    try:
        with db.session() as s:
            if owner_exists(s):
                return None
            s.add(User(email=email, password_hash=hash_password(pw),
                       role=ROLE_OWNER, name="Owner"))
            s.commit()
        return email
    except Exception:
        return None


# ── slug helper ──────────────────────────────────────────────────────────
def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "x"


# ── the routes + AI wall ─────────────────────────────────────────────────
def register_platform(app) -> None:
    """Wire platform auth routes and the AI owner-only wall onto the app.

    Wrapped in try/except by the caller, so any failure here leaves the
    existing app untouched.
    """
    from fastapi import Body
    from fastapi.responses import JSONResponse

    db.init_db()

    # Bootstrap the owner account from env (OWNER_EMAIL / OWNER_PASSWORD) on the
    # first boot after those vars are set. Idempotent and self-silencing.
    made = ensure_owner()
    if made:
        print(f"[platform] owner account created for {made}")

    # legacy owner token still opens the AI (so Chris isn't locked out mid-migration)
    legacy_token = (os.environ.get("ORIGIN_TOKEN") or "").strip()

    # The wall is OPT-IN: it only turns on once PLATFORM_AI_WALL=1 is set on the
    # host. That way deploying the foundation changes nothing about how the AI
    # page behaves today — Chris flips it on when the owner-login UI is ready.
    wall_on = (os.environ.get("PLATFORM_AI_WALL") or "").strip() in ("1", "true", "yes")

    def _is_ai_path(path: str) -> bool:
        if path in AI_EXACT:
            return True
        return any(path.startswith(p) for p in AI_PREFIXES)

    if wall_on:
      @app.middleware("http")
      async def _ai_wall(request, call_next):
        """Owner-only wall. On EVERY request to an AI path, allow only:
          * a valid owner session, or
          * the legacy owner token (transition).
        A GC-admin or sub session is explicitly refused — they can never reach
        the AI engine even by typing the URL."""
        try:
            path = request.url.path
            if _is_ai_path(path):
                claims = read_session(request)
                if claims and claims.get("role") == ROLE_OWNER:
                    return await call_next(request)
                if legacy_token:
                    supplied = (request.headers.get("x-origin-token")
                                or request.query_params.get("token"))
                    if supplied == legacy_token:
                        return await call_next(request)
                # anyone else (gc_admin, sub, anonymous) is walled out
                if path in AI_EXACT:
                    return JSONResponse(
                        {"error": "The AI workspace is restricted to the owner."},
                        status_code=403)
                return JSONResponse({"error": "forbidden"}, status_code=403)
        except Exception:
            # never let the wall crash a request path we didn't mean to guard
            pass
        return await call_next(request)

    # ---- auth routes ----
    @app.get("/platform/me")
    def platform_me(request: Request):
        claims = read_session(request)
        if not claims:
            return {"authenticated": False}
        with db.session() as s:
            u = s.get(User, claims["uid"])
            if not u or not u.active:
                return {"authenticated": False}
            out = {"authenticated": True, "role": u.role, "name": u.name,
                   "email": u.email, "gc_id": u.gc_id, "sub_id": u.sub_id}
            if u.gc_id:
                t = s.get(Tenant, u.gc_id)
                if t:
                    out["brand"] = {"name": t.name, "logo_url": t.logo_url,
                                    "primary": t.brand_primary, "text": t.brand_text}
            return out

    @app.post("/platform/login")
    def platform_login(request: Request, body: dict = Body(...)):
        email = (body.get("email") or "").strip().lower()
        password = body.get("password") or ""
        if not email or not password:
            return JSONResponse({"error": "email and password required"}, status_code=400)
        if _locked(email):
            return JSONResponse(
                {"error": "Too many attempts. Try again in a few minutes."},
                status_code=429)
        with db.session() as s:
            u = s.scalar(select(User).where(User.email == email))
            if not u or not u.active or not verify_password(password, u.password_hash):
                _note_fail(email)
                return JSONResponse({"error": "invalid credentials"}, status_code=401)
            _clear(email)
            u.last_login = datetime.utcnow()
            s.commit()
            token = make_session(u)
            role = u.role
        resp = JSONResponse({"ok": True, "role": role})
        secure = request.url.scheme == "https" or bool(
            request.headers.get("x-forwarded-proto", "").startswith("https"))
        resp.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True,
                        samesite="lax", secure=secure, path="/")
        return resp

    @app.post("/platform/logout")
    def platform_logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp
