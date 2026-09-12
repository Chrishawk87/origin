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
# WHITE-LABEL: one screen, ORIGIN wordmark, a single email + password field.
# No "Owner / Client" tabs — the person just enters their email and secret and
# the server recognizes who they are (owner, staff, GC, or contractor) and sends
# them to the right place. Exactly like the platform's own login.
_LOGIN_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Sign in — ORIGIN</title>
<style>
  :root{ --bg:#0b1220; --card:#111a2e; --line:#2a3a5c; --ink:#f2f6ff;
         --muted:#aab9d6; --accent:#1E7A46; --accent2:#186338; --danger:#ff7b7b; }
  *{ box-sizing:border-box; }
  html,body{ margin:0; height:100%; background:
     radial-gradient(1200px 600px at 50% -10%, #16233f 0%, var(--bg) 60%);
     color:var(--ink); font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  .wrap{ min-height:100%; display:flex; align-items:center; justify-content:center; padding:24px; }
  .card{ width:100%; max-width:400px; background:var(--card); border:1px solid var(--line);
         border-radius:18px; padding:32px 26px 26px; box-shadow:0 20px 60px rgba(0,0,0,.5); }
  .word{ font-size:30px; font-weight:800; letter-spacing:2.5px; text-align:center;
         color:#fff; margin:0 0 2px; }
  .word span{ color:var(--accent); }
  .sub{ color:var(--muted); font-size:13px; margin:4px 0 24px; text-align:center; }
  label{ display:block; font-size:12px; color:var(--muted); margin:0 0 6px; text-transform:uppercase; letter-spacing:.6px; }
  input{ width:100%; padding:13px 14px; margin-bottom:16px; border-radius:11px;
         border:1px solid var(--line); background:#0c1526; color:var(--ink); font-size:16px; }
  input::placeholder{ color:#6c7d9e; }
  input:focus{ outline:none; border-color:var(--accent); }
  .go{ width:100%; padding:14px; border:0; border-radius:11px; background:var(--accent);
       color:#fff; font-weight:700; font-size:16px; cursor:pointer; }
  .go:active{ background:var(--accent2); }
  .go[disabled]{ opacity:.6; cursor:default; }
  .msg{ min-height:20px; margin-top:14px; font-size:14px; color:var(--danger); text-align:center; }
  .msg.ok{ color:#5fd39a; }
  .foot{ text-align:center; color:var(--muted); font-size:12px; margin-top:20px; }
  a{ color:#7fb59a; text-decoration:none; }
</style></head>
<body><div class="wrap"><div class="card">
  <div class="word">ORIGIN<span>.</span></div>
  <div class="sub">Sign in to continue.</div>

  <form id="form" autocomplete="on">
    <label>Email</label>
    <input id="email" type="email" inputmode="email" autocomplete="username" placeholder="you@company.com" required>

    <label>Password</label>
    <input id="secret" type="password" autocomplete="current-password" placeholder="Your password" required>

    <button id="go" class="go" type="submit">Sign in</button>
    <div id="msg" class="msg"></div>
  </form>

  <div class="foot">Trouble signing in? Contact your Origin administrator.</div>
</div></div>

<script>
(function(){
  var msg = document.getElementById("msg");
  var go = document.getElementById("go");

  var q = new URLSearchParams(location.search);
  if(q.get("expired")){ msg.textContent = "Your access link expired. Please sign in."; }

  document.getElementById("form").addEventListener("submit", function(e){
    e.preventDefault();
    msg.textContent = ""; msg.className = "msg";
    var email = (document.getElementById("email").value || "").trim();
    var secret = document.getElementById("secret").value || "";
    go.disabled = true; go.textContent = "Signing in…";
    fetch("/sie/api/login", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ email: email, secret: secret })
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

    def _partner_slug(request) -> Optional[str]:
        cookie = getattr(_portal, "SIE_PARTNER_COOKIE", "origin_sie_partner")
        p = _portal._unsign(request.cookies.get(cookie, ""))
        if p and p.get("role") == "sie_partner":
            return (p.get("slug") or "").strip() or None
        return None

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
        # Already signed in as owner/GC/partner? Skip the form.
        if _admin_ok(request) or _gc_slug(request) or _partner_slug(request):
            return RedirectResponse("/sie", status_code=302)
        if _client_ok(request):
            return RedirectResponse("/portal", status_code=302)
        return HTMLResponse(_LOGIN_PAGE, headers=_NO_STORE)

    # ── login: ONE screen, the server figures out who you are ───────────────
    # White-label: no "Owner / Client" choice. The person types their email +
    # secret and we try, in order, every identity that email could belong to:
    #   1. the master owner  (email + password)      → admin session  → /sie
    #   2. an Origin staff member you set up (email+PIN) → admin session → /sie
    #   3. a GC / GC teammate (email + PIN)          → gc session     → /sie
    #   4. a contractor / client (email + PIN)       → client session → /portal
    # The single "secret" field carries a password for the owner and a PIN for
    # everyone else; legacy "password"/"pin" keys are still accepted so nothing
    # that already posts to this endpoint breaks.
    @app.post("/sie/api/login")
    def sie_login(request: Request, body: dict = Body(...)):
        email = (body.get("email") or "").strip()
        key = email.lower()
        secret = (body.get("secret") or body.get("password")
                  or body.get("pin") or "").strip()
        if _portal._login_locked(key):
            return JSONResponse(
                {"error": "Too many attempts. Please wait a few minutes and try again."},
                status_code=429)
        if not email or not secret:
            return JSONResponse({"error": "Enter your email and password."},
                                status_code=401)

        # (1) Master owner — email + password. If the email IS the owner email we
        #     settle it here and never fall through, so a wrong owner password
        #     can't be silently retried as a PIN.
        if hmac.compare_digest(key, _owner_email()):
            owner_pw = _owner_password()
            if not owner_pw:
                return JSONResponse(
                    {"error": "Owner login isn't configured yet. Set SIE_OWNER_PASSWORD on the server."},
                    status_code=503)
            if hmac.compare_digest(secret, owner_pw):
                _portal._login_clear(key)
                resp = JSONResponse({"ok": True, "role": "owner", "redirect": "/sie"})
                resp.set_cookie(_portal.ADMIN_COOKIE,
                                _sign_admin(time.time() + _MASTER_TTL),
                                httponly=True, samesite="lax", max_age=_MASTER_TTL,
                                secure=_secure(request))
                return resp
            _portal._login_note_fail(key)
            return JSONResponse({"error": "Wrong email or password."}, status_code=401)

        # (2) Origin staff member you set up under Account & Team (email + PIN) —
        #     same full owner console as you, scoped by their member id.
        try:
            member = _portal.find_owner_member_by_email(email)
        except Exception:
            member = None
        if (member and member.get("pin_hash")
                and _portal.verify_pin("owner", secret, member["pin_hash"])):
            _portal._login_clear(key)
            resp = JSONResponse({"ok": True, "role": "staff", "redirect": "/sie",
                                 "name": member.get("name", "")})
            resp.set_cookie(_portal.ADMIN_COOKIE,
                            _portal._session("admin", "", member=member.get("id", "")),
                            httponly=True, samesite="lax",
                            max_age=_portal.SESSION_TTL, secure=_secure(request))
            return resp

        # (3) GC or GC teammate (email + PIN) — lands in the /sie console, scoped
        #     to that GC's own subcontractors by the existing tenant gating.
        try:
            gc_rec, gc_member = _portal.find_gc_member_by_email(email)
        except Exception:
            gc_rec, gc_member = None, None
        if (gc_rec and gc_member and gc_member.get("pin_hash")
                and _portal.verify_pin(gc_rec["slug"], secret, gc_member["pin_hash"])):
            _portal._login_clear(key)
            resp = JSONResponse({"ok": True, "role": "gc", "redirect": "/sie",
                                 "name": gc_rec.get("name", "")})
            resp.set_cookie(_portal.GC_COOKIE,
                            _portal._session("gc", gc_rec["slug"],
                                             gc_member.get("id", "owner")),
                            httponly=True, samesite="lax",
                            max_age=_portal.SESSION_TTL, secure=_secure(request))
            return resp

        # (3b) White-label SIE partner (email + PIN) — lands in the /sie console
        #      scoped to that partner's OWN abatement matters by firm_slug, and
        #      styled with the partner's own brand. Its own cookie, so nothing
        #      else in the app is affected.
        try:
            from . import sie_partners as _sp
            partner = _sp.find_partner_by_email(email)
        except Exception:
            _sp, partner = None, None
        if (partner and partner.get("pin_hash")
                and _portal.verify_pin(partner["slug"], secret, partner["pin_hash"])):
            _portal._login_clear(key)
            resp = JSONResponse({"ok": True, "role": "sie_partner", "redirect": "/sie",
                                 "name": partner.get("name", "")})
            cookie = getattr(_portal, "SIE_PARTNER_COOKIE", "origin_sie_partner")
            resp.set_cookie(cookie,
                            _portal._session("sie_partner", partner["slug"]),
                            httponly=True, samesite="lax",
                            max_age=_portal.SESSION_TTL, secure=_secure(request))
            return resp

        # (4) Contractor / client (email + PIN) — their own scoped portal view.
        rec = _portal.find_by_email(email)
        if (rec and rec.get("pin_hash")
                and _portal.verify_pin(rec["slug"], secret, rec["pin_hash"])):
            _portal._login_clear(key)
            resp = JSONResponse({"ok": True, "role": "client", "redirect": "/portal",
                                 "company": rec.get("company")})
            resp.set_cookie(_portal.CLIENT_COOKIE,
                            _portal._session("client", rec["slug"]),
                            httponly=True, samesite="lax",
                            max_age=_portal.SESSION_TTL, secure=_secure(request))
            return resp

        _portal._login_note_fail(key)
        return JSONResponse({"error": "Wrong email or password."}, status_code=401)

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
            return {"authenticated": True, "role": "owner", "is_owner": True}
        gc = _gc_slug(request)
        if gc:
            return {"authenticated": True, "role": "gc", "slug": gc,
                    "is_owner": False}
        partner = _partner_slug(request)
        if partner:
            # Hand the console this partner's white-label so it can recolor and
            # rename itself. Best-effort: a missing record still authenticates.
            out = {"authenticated": True, "role": "sie_partner",
                   "slug": partner, "is_owner": False}
            try:
                from . import sie_partners as _sp
                rec = _sp.load_partner(partner)
                if rec:
                    pv = _sp.public_view(rec)
                    out["name"] = pv.get("brand_name") or pv.get("name") or ""
                    out["brand_primary"] = pv.get("brand_primary") or ""
                    out["brand_secondary"] = pv.get("brand_secondary") or ""
                    out["theme"] = pv.get("theme") or "dark"
                    out["tagline"] = pv.get("tagline") or ""
            except Exception:
                pass
            return out
        if _client_ok(request):
            return {"authenticated": True, "role": "client", "is_owner": False}
        return {"authenticated": False}

    # ── sign out (clears every session cookie this app issues) ───────────────
    @app.post("/sie/api/logout")
    def sie_logout():
        resp = JSONResponse({"ok": True, "redirect": "/sie/login"})
        cookies = [_portal.ADMIN_COOKIE, _portal.GC_COOKIE, _portal.CLIENT_COOKIE]
        cookies.append(getattr(_portal, "SIE_PARTNER_COOKIE", "origin_sie_partner"))
        for c in cookies:
            resp.delete_cookie(c, path="/")
        return resp
