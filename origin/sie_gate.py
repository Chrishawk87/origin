"""Unified front-door authentication for the Safety Intelligence Engine (/sie).

Chris wanted ONE app: everyone lands on /sie and signs in there.

  * MASTER (owner) login — a dedicated email + password Chris sets on Railway
    (SIE_OWNER_EMAIL / SIE_OWNER_PASSWORD). A correct login mints a portal ADMIN
    session, so the owner immediately has full, portfolio-wide SIE access via the
    gating that already exists in server.py (_admin_session_ok). This is the fix
    for "I can't log in from my own app": /sie now has its own login screen that
    lands the owner straight into the console.

  * CLIENT login — the SAME email + PIN a customer already uses on the site
    (portal.py). A correct login mints the portal CLIENT session and the front
    door sends them to their own scoped portal view (never the owner console).

  * TEMPORARY access — the owner generates a time-limited MAGIC LINK from their
    phone (they pick the duration each time). Whoever opens the link is signed in
    with owner access until it auto-expires. No PIN, no account. The link is an
    HMAC-signed, self-expiring token; redeeming it issues a session cookie whose
    lifetime matches the link, so access dies exactly when the link would.

Design rules that keep this SAFE and isolated (same as portal.py):
  * Reuses portal.py's PROVEN primitives — the shared signing SECRET, _sign/
    _unsign, the cookie names, verify_pin, find_by_email, _secure — so there is
    ONE cookie scheme and one secret across the whole app. No new crypto.
  * register_sie_gate(app) is wrapped in try/except by the caller and imports
    nothing heavy, so a bug here can never break the console, portal, or engine.
  * Fails closed: with no SIE_OWNER_PASSWORD set, the master login is refused
    (never a guessable default). Brute-force attempts are throttled per email,
    reusing the portal's lockout.
"""

from __future__ import annotations

import hmac
import os
import time
from typing import Any, Dict, Optional

try:
    from starlette.requests import Request
except Exception:  # pragma: no cover
    Request = None  # type: ignore


# ── master (owner) credentials, from the environment ─────────────────────
# Email defaults to Chris's business address; the password MUST be set on the
# host or master login is refused. OWNER_EMAIL/OWNER_PASSWORD are honored too so
# a single pair of vars can serve both this gate and the dormant platform_auth.
def _owner_email() -> str:
    return (os.environ.get("SIE_OWNER_EMAIL")
            or os.environ.get("OWNER_EMAIL")
            or "info@originmanagementsolutions.com").strip().lower()


def _owner_password() -> str:
    return (os.environ.get("SIE_OWNER_PASSWORD")
            or os.environ.get("OWNER_PASSWORD") or "").strip()


# How long a normal master session lasts (mirrors the portal's 12h).
_MASTER_TTL = 60 * 60 * 12
# Guard-rails for the temp magic link (owner-chosen, clamped to a sane range).
_MAGIC_MIN = 5 * 60             # 5 minutes floor
_MAGIC_MAX = 60 * 60 * 24 * 30  # 30 days ceiling
_MAGIC_DEFAULT = 60 * 60 * 24   # 1 day if nothing supplied


# ── login page (self-contained, mobile-first, no external deps) ──────────
_LOGIN_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Sign in — Safety Intelligence Engine</title>
<style>
  :root{ --bg:#0b1220; --card:#111a2e; --line:#22314f; --ink:#e8eefc;
         --muted:#9fb0d0; --accent:#3d7dff; --accent2:#2b63d9; --danger:#ff6b6b; }
  *{ box-sizing:border-box; }
  html,body{ margin:0; height:100%; background:
     radial-gradient(1200px 600px at 50% -10%, #16233f 0%, var(--bg) 60%);
     color:var(--ink); font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  .wrap{ min-height:100%; display:flex; align-items:center; justify-content:center; padding:24px; }
  .card{ width:100%; max-width:420px; background:var(--card); border:1px solid var(--line);
         border-radius:18px; padding:28px 24px 24px; box-shadow:0 20px 60px rgba(0,0,0,.45); }
  .brand{ display:flex; align-items:center; gap:10px; margin-bottom:4px; }
  .dot{ width:12px; height:12px; border-radius:50%; background:var(--accent);
        box-shadow:0 0 16px var(--accent); }
  h1{ font-size:20px; margin:0; letter-spacing:.2px; }
  .sub{ color:var(--muted); font-size:13px; margin:6px 0 20px; }
  .seg{ display:flex; background:#0c1526; border:1px solid var(--line);
        border-radius:12px; padding:4px; margin-bottom:18px; }
  .seg button{ flex:1; border:0; background:transparent; color:var(--muted);
        padding:9px 8px; border-radius:9px; font-weight:600; font-size:14px; cursor:pointer; }
  .seg button.on{ background:var(--accent); color:#fff; }
  label{ display:block; font-size:12px; color:var(--muted); margin:0 0 6px; text-transform:uppercase; letter-spacing:.6px; }
  input{ width:100%; padding:13px 14px; margin-bottom:14px; border-radius:11px;
         border:1px solid var(--line); background:#0c1526; color:var(--ink); font-size:16px; }
  input:focus{ outline:none; border-color:var(--accent); }
  .go{ width:100%; padding:14px; border:0; border-radius:11px; background:var(--accent);
       color:#fff; font-weight:700; font-size:16px; cursor:pointer; }
  .go:active{ background:var(--accent2); }
  .go[disabled]{ opacity:.6; cursor:default; }
  .msg{ min-height:20px; margin-top:14px; font-size:14px; color:var(--danger); text-align:center; }
  .msg.ok{ color:#5fd39a; }
  .foot{ text-align:center; color:var(--muted); font-size:12px; margin-top:18px; }
  a{ color:var(--accent); text-decoration:none; }
  .hide{ display:none; }
</style></head>
<body><div class="wrap"><div class="card">
  <div class="brand"><span class="dot"></span><h1>Safety Intelligence Engine</h1></div>
  <div class="sub">Sign in to continue.</div>

  <div class="seg">
    <button id="tabOwner" class="on" type="button">Owner</button>
    <button id="tabClient" type="button">Client</button>
  </div>

  <form id="form" autocomplete="on">
    <label id="lblEmail">Email</label>
    <input id="email" type="email" inputmode="email" autocomplete="username" placeholder="you@company.com" required>

    <div id="ownerFields">
      <label>Password</label>
      <input id="password" type="password" autocomplete="current-password" placeholder="Your master password">
    </div>

    <div id="clientFields" class="hide">
      <label>PIN</label>
      <input id="pin" type="password" inputmode="numeric" autocomplete="current-password" placeholder="Your PIN">
    </div>

    <button id="go" class="go" type="submit">Sign in</button>
    <div id="msg" class="msg"></div>
  </form>

  <div class="foot" id="foot">Trouble signing in? Contact your Origin administrator.</div>
</div></div>

<script>
(function(){
  var mode = "owner";
  var tabOwner = document.getElementById("tabOwner");
  var tabClient = document.getElementById("tabClient");
  var ownerFields = document.getElementById("ownerFields");
  var clientFields = document.getElementById("clientFields");
  var msg = document.getElementById("msg");
  var go = document.getElementById("go");

  function setMode(m){
    mode = m;
    var owner = (m === "owner");
    tabOwner.classList.toggle("on", owner);
    tabClient.classList.toggle("on", !owner);
    ownerFields.classList.toggle("hide", !owner);
    clientFields.classList.toggle("hide", owner);
    msg.textContent = ""; msg.className = "msg";
  }
  tabOwner.addEventListener("click", function(){ setMode("owner"); });
  tabClient.addEventListener("click", function(){ setMode("client"); });

  document.getElementById("form").addEventListener("submit", function(e){
    e.preventDefault();
    msg.textContent = ""; msg.className = "msg";
    var email = (document.getElementById("email").value || "").trim();
    var body = { email: email };
    if(mode === "owner"){ body.password = document.getElementById("password").value || ""; }
    else { body.pin = (document.getElementById("pin").value || "").trim(); }
    go.disabled = true; go.textContent = "Signing in…";
    fetch("/sie/api/login", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body)
    }).then(function(r){ return r.json().then(function(j){ return {ok:r.ok, j:j}; }); })
      .then(function(res){
        if(res.ok && res.j && res.j.ok){
          msg.className = "msg ok"; msg.textContent = "Welcome. Loading…";
          window.location.href = res.j.redirect || "/sie";
          return;
        }
        msg.textContent = (res.j && res.j.error) || "Sign-in failed.";
        go.disabled = false; go.textContent = "Sign in";
      }).catch(function(){
        msg.textContent = "Network error. Please try again.";
        go.disabled = false; go.textContent = "Sign in";
      });
  });
})();
</script>
</body></html>"""


def register_sie_gate(app) -> None:
    """Wire the /sie sign-in routes onto the app. Wrapped in try/except by the
    caller so any failure here leaves the rest of the app untouched."""
    from fastapi import Body
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

    from . import portal as _portal

    _NO_STORE = {"Cache-Control": "no-store, must-revalidate",
                 "Pragma": "no-cache", "Expires": "0"}

    # ---- helpers that read the current session, reusing portal's scheme ----
    def _admin_ok(request) -> bool:
        p = _portal._unsign(request.cookies.get(_portal.ADMIN_COOKIE, ""))
        return bool(p and p.get("role") == "admin")

    def _gc_slug(request) -> Optional[str]:
        p = _portal._unsign(request.cookies.get(_portal.GC_COOKIE, ""))
        if p and p.get("role") == "gc":
            return (p.get("slug") or "").strip() or None
        return None

    def _client_ok(request) -> bool:
        p = _portal._unsign(request.cookies.get(_portal.CLIENT_COOKIE, ""))
        return bool(p and p.get("role") == "client")

    def _secure(request) -> bool:
        # Railway terminates TLS at its edge; honor the forwarded-proto header so
        # cookies still get Secure on real HTTPS visitors (same as portal._secure).
        xfp = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        return request.url.scheme == "https" or xfp == "https"

    def _sign_admin(exp: float, member: str = "") -> str:
        """Mint an ADMIN session cookie value with an explicit expiry. Uses the
        portal's own signer so portal._unsign (used everywhere) accepts it."""
        payload: Dict[str, Any] = {"role": "admin", "slug": "", "exp": exp}
        if member:
            payload["member"] = member
        return _portal._sign(payload)

    # ── the sign-in page ──────────────────────────────────────────────────
    @app.get("/sie/login", response_class=HTMLResponse)
    def sie_login_page(request: Request):
        # Already signed in as owner/GC? Skip the form.
        if _admin_ok(request) or _gc_slug(request):
            return RedirectResponse("/sie", status_code=302)
        if _client_ok(request):
            return RedirectResponse("/portal", status_code=302)
        return HTMLResponse(_LOGIN_PAGE, headers=_NO_STORE)

    # ── login: master (email+password) OR client (email+PIN) ────────────────
    @app.post("/sie/api/login")
    def sie_login(request: Request, body: dict = Body(...)):
        email = (body.get("email") or "").strip()
        key = email.lower()
        if _portal._login_locked(key):
            return JSONResponse(
                {"error": "Too many attempts. Please wait a few minutes and try again."},
                status_code=429)

        # (1) Client email + PIN — the same credential used on the site.
        pin = (body.get("pin") or "").strip()
        if pin and not body.get("password"):
            rec = _portal.find_by_email(email)
            if (rec and rec.get("pin_hash")
                    and _portal.verify_pin(rec["slug"], pin, rec["pin_hash"])):
                _portal._login_clear(key)
                resp = JSONResponse({"ok": True, "role": "client",
                                     "redirect": "/portal",
                                     "company": rec.get("company")})
                resp.set_cookie(_portal.CLIENT_COOKIE,
                                _portal._session("client", rec["slug"]),
                                httponly=True, samesite="lax",
                                max_age=_portal.SESSION_TTL,
                                secure=_secure(request))
                return resp
            _portal._login_note_fail(key)
            return JSONResponse({"error": "Wrong email or PIN."}, status_code=401)

        # (2) Master owner email + password.
        password = body.get("password") or ""
        owner_pw = _owner_password()
        if not owner_pw:
            return JSONResponse(
                {"error": "Owner login isn't configured yet. Set SIE_OWNER_PASSWORD on the server."},
                status_code=503)
        good = (hmac.compare_digest(key, _owner_email())
                and hmac.compare_digest(password, owner_pw))
        if not good:
            _portal._login_note_fail(key)
            return JSONResponse({"error": "Wrong email or password."}, status_code=401)
        _portal._login_clear(key)
        resp = JSONResponse({"ok": True, "role": "owner", "redirect": "/sie"})
        resp.set_cookie(_portal.ADMIN_COOKIE,
                        _sign_admin(time.time() + _MASTER_TTL),
                        httponly=True, samesite="lax", max_age=_MASTER_TTL,
                        secure=_secure(request))
        return resp

    # ── temporary access: generate a magic link (owner only) ────────────────
    @app.post("/sie/api/magic")
    def sie_magic_make(request: Request, body: dict = Body(default=None)):
        if not _admin_ok(request):
            return JSONResponse({"error": "Only the owner can create access links."},
                                status_code=403)
        body = body or {}
        # Owner picks the duration each time: minutes / hours / days, or raw secs.
        secs = 0
        try:
            if body.get("seconds"):
                secs = int(body["seconds"])
            elif body.get("minutes"):
                secs = int(body["minutes"]) * 60
            elif body.get("hours"):
                secs = int(float(body["hours"]) * 3600)
            elif body.get("days"):
                secs = int(float(body["days"]) * 86400)
        except Exception:
            secs = 0
        if secs <= 0:
            secs = _MAGIC_DEFAULT
        secs = max(_MAGIC_MIN, min(_MAGIC_MAX, secs))
        exp = time.time() + secs
        token = _portal._sign({"kind": "sie_magic", "role": "admin", "exp": exp})
        base = str(request.base_url).rstrip("/")
        url = f"{base}/sie/enter?t={token}"
        return JSONResponse({"ok": True, "url": url, "expires_in": secs,
                             "expires_at": int(exp)})

    # ── temporary access: redeem the link ───────────────────────────────────
    @app.get("/sie/enter")
    def sie_magic_enter(request: Request):
        tok = (request.query_params.get("t") or "").strip()
        payload = _portal._unsign(tok) if tok else None
        if not payload or payload.get("kind") != "sie_magic":
            # Expired or tampered — send them to the normal sign-in screen.
            return RedirectResponse("/sie/login?expired=1", status_code=302)
        exp = float(payload.get("exp", 0))
        remaining = int(max(0, exp - time.time()))
        if remaining <= 0:
            return RedirectResponse("/sie/login?expired=1", status_code=302)
        resp = RedirectResponse("/sie", status_code=302)
        # Session lifetime matches the link, so access dies exactly on expiry.
        resp.set_cookie(_portal.ADMIN_COOKIE, _sign_admin(exp),
                        httponly=True, samesite="lax", max_age=remaining,
                        secure=_secure(request))
        return resp

    # ── who am I (lets the console show the right lens / a sign-out link) ────
    @app.get("/sie/api/whoami")
    def sie_whoami(request: Request):
        if _admin_ok(request):
            return {"authenticated": True, "role": "owner"}
        gc = _gc_slug(request)
        if gc:
            return {"authenticated": True, "role": "gc", "slug": gc}
        if _client_ok(request):
            return {"authenticated": True, "role": "client"}
        return {"authenticated": False}

    # ── sign out (clears every session cookie this app issues) ───────────────
    @app.post("/sie/api/logout")
    def sie_logout():
        resp = JSONResponse({"ok": True, "redirect": "/sie/login"})
        for c in (_portal.ADMIN_COOKIE, _portal.GC_COOKIE, _portal.CLIENT_COOKIE):
            resp.delete_cookie(c, path="/")
        return resp
