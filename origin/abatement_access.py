"""Origin Abatement — roles & per-matter client access (Phase 2).

Additive access layer for the Abatement feature. It does NOT rebuild Origin's
authentication — it reuses portal.py's proven HMAC signer and the existing
session cookies (see sie_gate.py). It only adds two things on top:

  1. A small, explicit ROLE model for the abatement workflow so the UI can show
     the right lens and the server can refuse actions a role may not take:

        FIRM SIDE (the law firm — signs in the normal way at /sie)
          * law_firm_admin — the firm owner. Full control, can mint client links.
          * attorney       — owns legal strategy, verification, and submission.
          * paralegal       — organizes the matter and evidence; may NOT verify
                              items/evidence or attest a submission (those are the
                              attorney's calls).

        CLIENT SIDE (the cited employer — no account, no password)
          * client_admin   — the client's point person on a single matter.
          * client_user    — a client teammate on a single matter.
          * read_only      — view only.

  2. A per-matter MAGIC LINK for clients. The firm mints a time-limited link for
     ONE matter; whoever opens it can upload documents/photos and mark their
     assigned tasks done — but can NEVER verify evidence, change legal status, or
     submit to OSHA. The link is an HMAC-signed, self-expiring token (portal._sign),
     redeemed at /abatement/enter which sets a cookie whose lifetime matches the
     link. No account is ever created (creating accounts is a prohibited action).

House rules (identical to the rest of the feature): deterministic, offline,
never-fabricate, isolated + non-fatal route registration. The hard guardrail —
**a client can never verify, never change legal status, and never submit** — is
enforced here in one place so every other module can rely on it.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

try:
    from starlette.requests import Request
except Exception:  # pragma: no cover
    Request = Any  # type: ignore


# ── role model ────────────────────────────────────────────────────────────────
LAW_FIRM_ADMIN = "law_firm_admin"
ATTORNEY = "attorney"
PARALEGAL = "paralegal"
CLIENT_ADMIN = "client_admin"
CLIENT_USER = "client_user"
READ_ONLY = "read_only"

FIRM_ROLES = (LAW_FIRM_ADMIN, ATTORNEY, PARALEGAL)
CLIENT_ROLES = (CLIENT_ADMIN, CLIENT_USER)

ROLE_LABELS: Dict[str, str] = {
    LAW_FIRM_ADMIN: "Law-firm admin",
    ATTORNEY: "Attorney",
    PARALEGAL: "Paralegal",
    CLIENT_ADMIN: "Client (lead)",
    CLIENT_USER: "Client (team)",
    READ_ONLY: "Read only",
}

# Capability table. Read this instead of hard-coding role checks anywhere else.
#   manage  — create/edit matters, items, tasks, upload on the firm side
#   verify  — mark a citation item or a piece of evidence verified (legal call)
#   submit  — attest that a package was filed / move status to submitted
#   mint    — generate a client access link
#   upload  — attach documents/photos and complete assigned client tasks
_CAPS: Dict[str, Dict[str, bool]] = {
    LAW_FIRM_ADMIN: {"manage": True,  "verify": True,  "submit": True,  "mint": True,  "upload": True},
    ATTORNEY:       {"manage": True,  "verify": True,  "submit": True,  "mint": True,  "upload": True},
    PARALEGAL:      {"manage": True,  "verify": False, "submit": False, "mint": True,  "upload": True},
    CLIENT_ADMIN:   {"manage": False, "verify": False, "submit": False, "mint": False, "upload": True},
    CLIENT_USER:    {"manage": False, "verify": False, "submit": False, "mint": False, "upload": True},
    READ_ONLY:      {"manage": False, "verify": False, "submit": False, "mint": False, "upload": False},
}


def can(role: str, capability: str) -> bool:
    """The single source of truth for 'may this role do X'."""
    return bool(_CAPS.get(role or "", {}).get(capability, False))


# ── per-matter client token (reuses portal.py's signer, no new crypto) ─────────
_CLIENT_COOKIE = "origin_abate_client"
_TOKEN_KIND = "abate_client"
_MIN_TTL = 15 * 60             # 15 minutes floor
_MAX_TTL = 60 * 60 * 24 * 90   # 90 days ceiling
_DEFAULT_TTL = 60 * 60 * 24 * 14  # 14 days


def _portal():
    from . import portal as _p
    return _p


def mint_client_token(matter_id: str, *, role: str = CLIENT_ADMIN,
                      seconds: int = _DEFAULT_TTL, name: str = "") -> Dict[str, Any]:
    """Sign a self-expiring token that grants CLIENT access to ONE matter."""
    if role not in CLIENT_ROLES:
        role = CLIENT_ADMIN
    try:
        secs = int(seconds)
    except Exception:
        secs = _DEFAULT_TTL
    if secs <= 0:
        secs = _DEFAULT_TTL
    secs = max(_MIN_TTL, min(_MAX_TTL, secs))
    exp = time.time() + secs
    token = _portal()._sign({
        "kind": _TOKEN_KIND, "matter_id": matter_id, "role": role,
        "name": (name or "").strip(), "exp": exp,
    })
    return {"token": token, "expires_in": secs, "expires_at": int(exp),
            "matter_id": matter_id, "role": role}


def read_client_token(tok: str) -> Optional[Dict[str, Any]]:
    """Return the payload of a valid, unexpired client token, else None.
    (_unsign already rejects tampered or expired tokens.)"""
    if not tok:
        return None
    p = _portal()._unsign(tok)
    if not p or p.get("kind") != _TOKEN_KIND or not p.get("matter_id"):
        return None
    return p


# ── actor resolution — who is making this request, and what may they do ────────
def _firm_actor(request) -> Optional[Dict[str, Any]]:
    """Recognize a firm session from the existing SIE cookies. Owner → firm
    admin; a named staff/GC session → attorney (highest firm authority under the
    owner). This maps abatement roles onto the auth Origin already has, without
    adding a new login."""
    P = _portal()
    ck = getattr(request, "cookies", {}) or {}
    p = P._unsign(ck.get(P.ADMIN_COOKIE, ""))
    if p and p.get("role") == "admin":
        role = ATTORNEY if p.get("member") else LAW_FIRM_ADMIN
        return {"kind": "firm", "role": role, "firm_slug": (p.get("slug") or "") or None,
                "matter_id": None, "name": p.get("member", "")}
    g = P._unsign(ck.get(P.GC_COOKIE, ""))
    if g and g.get("role") == "gc":
        return {"kind": "firm", "role": ATTORNEY,
                "firm_slug": (g.get("slug") or "").strip() or None,
                "matter_id": None, "name": ""}
    # White-label SIE partner (see sie_partners.py): a first-class firm tenant.
    # The partner runs its own book of matters, so it gets full firm-admin
    # authority — but ONLY over matters stamped with its own slug (the abatement
    # store already scopes every matter by firm_slug == sie_gc_slug).
    sp = P._unsign(ck.get(getattr(P, "SIE_PARTNER_COOKIE", "origin_sie_partner"), ""))
    if sp and sp.get("role") == "sie_partner":
        return {"kind": "firm", "role": LAW_FIRM_ADMIN,
                "firm_slug": (sp.get("slug") or "").strip() or None,
                "matter_id": None, "name": ""}
    return None


def _client_actor(request) -> Optional[Dict[str, Any]]:
    """Recognize a per-matter client from the abatement cookie or a ?t= token."""
    ck = getattr(request, "cookies", {}) or {}
    tok = ck.get(_CLIENT_COOKIE, "")
    if not tok:
        try:
            tok = request.query_params.get("t", "")  # type: ignore[attr-defined]
        except Exception:
            tok = ""
    payload = read_client_token(tok)
    if not payload:
        return None
    return {"kind": "client", "role": payload.get("role", CLIENT_USER),
            "firm_slug": None, "matter_id": payload.get("matter_id"),
            "name": payload.get("name", "")}


def resolve_actor(request) -> Dict[str, Any]:
    """Return the current actor as {kind, role, firm_slug, matter_id, name, caps}.
    Falls back to an anonymous read-only actor so callers never crash."""
    actor = _firm_actor(request) or _client_actor(request)
    if not actor:
        actor = {"kind": "anon", "role": READ_ONLY, "firm_slug": None,
                 "matter_id": None, "name": ""}
    actor["caps"] = dict(_CAPS.get(actor["role"], _CAPS[READ_ONLY]))
    actor["role_label"] = ROLE_LABELS.get(actor["role"], actor["role"])
    return actor


def client_scope_matter(request) -> Optional[str]:
    """If (and only if) this request is a client, the one matter they may touch.
    Firm sessions return None (they are not matter-scoped by a token)."""
    a = _client_actor(request)
    return a.get("matter_id") if a else None


# ── routes ─────────────────────────────────────────────────────────────────────
def register_abatement_access(app) -> None:
    """Attach access routes. Isolated + non-fatal, mirroring the other modules."""
    from fastapi import Body
    from fastapi.responses import JSONResponse, RedirectResponse

    P = _portal()

    def _secure(request) -> bool:
        xfp = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        return request.url.scheme == "https" or xfp == "https"

    @app.get("/api/abatement/whoami")
    def ab_whoami(request: Request):
        a = resolve_actor(request)
        return {"ok": True, "role": a["role"], "role_label": a["role_label"],
                "kind": a["kind"], "matter_id": a["matter_id"], "caps": a["caps"],
                "name": a.get("name", "")}

    @app.post("/api/abatement/matters/{matter_id}/client-link")
    def ab_client_link(matter_id: str, request: Request, body: dict = Body(default=None)):
        # Only a firm actor with the 'mint' capability may create client links.
        a = resolve_actor(request)
        if a["kind"] != "firm" or not can(a["role"], "mint"):
            return JSONResponse(
                {"error": "Only firm staff can create client access links."},
                status_code=403)
        p = body if isinstance(body, dict) else {}
        secs = 0
        try:
            if p.get("seconds"):
                secs = int(p["seconds"])
            elif p.get("days"):
                secs = int(float(p["days"]) * 86400)
            elif p.get("hours"):
                secs = int(float(p["hours"]) * 3600)
        except Exception:
            secs = 0
        if secs <= 0:
            secs = _DEFAULT_TTL
        role = p.get("role") if p.get("role") in CLIENT_ROLES else CLIENT_ADMIN
        minted = mint_client_token(matter_id, role=role, seconds=secs,
                                   name=p.get("name", ""))
        base = str(request.base_url).rstrip("/")
        url = f"{base}/abatement/enter?t={minted['token']}"
        return {"ok": True, "url": url, "expires_in": minted["expires_in"],
                "expires_at": minted["expires_at"], "role": role}

    @app.get("/abatement/enter")
    def ab_enter(request: Request):
        # Redeem a client link: set a self-expiring cookie, then land on the
        # client portal for that matter. No account, no password.
        tok = (request.query_params.get("t") or "").strip()
        payload = read_client_token(tok)
        if not payload:
            return RedirectResponse("/abatement/client?expired=1", status_code=302)
        remaining = int(max(0, float(payload.get("exp", 0)) - time.time()))
        if remaining <= 0:
            return RedirectResponse("/abatement/client?expired=1", status_code=302)
        resp = RedirectResponse("/abatement/client", status_code=302)
        resp.set_cookie(_CLIENT_COOKIE, tok, httponly=True, samesite="lax",
                        max_age=remaining, secure=_secure(request))
        return resp

    @app.post("/api/abatement/client-logout")
    def ab_client_logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(_CLIENT_COOKIE, path="/")
        return resp
