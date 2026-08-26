"""Client-facing compliance PORTAL for Origin.

This is the customer storefront. Each contractor logs into their own space and
sees ONLY their own compliance status, insurance dates, and the documents Origin
prepared for them. They never touch the AI, the gap finder, or the policy library.

Chris (the operator) uses the ADMIN console to type each client's grades, COI
dates, and to answer document requests — that's the private back office.

Design rules that keep this SAFE and isolated:
  * All state lives in flat JSON under DATA_DIR/portal/  (same volume as the rest
    of Origin). No database, matching the existing app.
  * Client API routes live under /portal/... (NOT /api/...) so the existing
    x-origin-token middleware does not block real customers.
  * register_portal(app) is wrapped in try/except by the caller, and this module
    depends on nothing heavy, so a bug here can never break the chat app,
    gap finder, or dashboard.
  * Sessions are HMAC-signed cookies using only the standard library.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Imported at module scope so FastAPI can resolve the string annotation "Request"
# (needed because `from __future__ import annotations` makes annotations strings,
# and get_type_hints resolves them against this module's globals).
try:
    from starlette.requests import Request
    from fastapi import UploadFile
except Exception:  # pragma: no cover
    Request = None  # type: ignore
    UploadFile = None  # type: ignore

# --- where data lives (decoupled so this file can be unit-tested alone) ---
try:
    from .paths import DATA_DIR  # normal in-package import
except Exception:  # pragma: no cover - standalone/test import
    DATA_DIR = Path(os.environ.get("ORIGIN_DATA_DIR") or (Path.home() / ".origin"))

PORTAL_DIR = DATA_DIR / "portal"
CLIENTS_DIR = PORTAL_DIR / "clients"

# --- secrets (set these on Railway for production) ---
SECRET = (os.environ.get("ORIGIN_PORTAL_SECRET")
          or os.environ.get("ORIGIN_TOKEN")
          or "origin-portal-dev-secret-change-me")
ADMIN_PASSWORD = (os.environ.get("ORIGIN_ADMIN_PASSWORD")
                  or os.environ.get("ORIGIN_TOKEN")
                  or "origin-admin")

CLIENT_COOKIE = "origin_portal"
ADMIN_COOKIE = "origin_admin"
SESSION_TTL = 60 * 60 * 12  # 12 hours


# ─────────────────────────── small helpers ───────────────────────────

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "client"


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_pin(slug: str, pin: str) -> str:
    return hashlib.sha256(f"{SECRET}:{slug}:{pin}".encode()).hexdigest()


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


def _session(role: str, slug: str = "") -> str:
    return _sign({"role": role, "slug": slug, "exp": time.time() + SESSION_TTL})


# ─────────────────────────── storage ───────────────────────────

def _client_dir(slug: str) -> Path:
    return CLIENTS_DIR / slug


def _client_file(slug: str) -> Path:
    return _client_dir(slug) / "client.json"


def load_client(slug: str) -> Optional[Dict[str, Any]]:
    f = _client_file(slug)
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_client(data: Dict[str, Any]) -> Dict[str, Any]:
    slug = data["slug"]
    d = _client_dir(slug)
    (d / "docs").mkdir(parents=True, exist_ok=True)
    data["updated"] = _now()
    _client_file(slug).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def list_clients() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not CLIENTS_DIR.is_dir():
        return out
    for d in sorted(CLIENTS_DIR.iterdir()):
        rec = load_client(d.name)
        if rec:
            out.append({
                "slug": rec.get("slug"),
                "company": rec.get("company"),
                "email": rec.get("email"),
                "client_type": rec.get("client_type", "prequal"),
                "open_requests": sum(1 for r in rec.get("requests", []) if r.get("status") == "new"),
                "updated": rec.get("updated"),
            })
    return out


def find_by_email(email: str) -> Optional[Dict[str, Any]]:
    email = (email or "").strip().lower()
    if not CLIENTS_DIR.is_dir():
        return None
    for d in sorted(CLIENTS_DIR.iterdir()):
        rec = load_client(d.name)
        if rec and (rec.get("email", "").strip().lower() == email):
            return rec
    return None


def _blank_client(company: str, email: str, client_type: str = "prequal") -> Dict[str, Any]:
    slug = slugify(company)
    return {
        "slug": slug,
        "company": company,
        "email": email,
        "pin_hash": "",
        "client_type": client_type,
        "plan": "Professional" if client_type == "prequal" else "Compliance Documentation",
        "platforms": {
            "isnetworld": {"label": "ISNetworld", "grade": "", "status": "Compliant"},
            "avetta": {"label": "Avetta", "grade": "", "status": "Compliant"},
            "pec": {"label": "PEC Premier", "grade": "", "status": "Green"},
            "veriforce": {"label": "Veriforce", "grade": "", "status": "Active"},
        },
        "coi": [],
        "documents": [],
        "available": [],
        "requests": [],
        "created": _now(),
        "updated": _now(),
    }


def _public_view(rec: Dict[str, Any]) -> Dict[str, Any]:
    """What the logged-in client is allowed to see (no pin hash)."""
    safe = {k: v for k, v in rec.items() if k != "pin_hash"}
    return safe


# ─────────────────────────── seed ───────────────────────────

def seed_test_client() -> Dict[str, Any]:
    """Create the sample 'Test Contractor' so Chris can log in immediately.
    Idempotent: overwrites the test record only."""
    rec = _blank_client("Test Contractor, LLC", "office@testcontractor.com", "prequal")
    rec["pin_hash"] = hash_pin(rec["slug"], "1234")
    rec["platforms"] = {
        "isnetworld": {"label": "ISNetworld", "grade": "A", "status": "Compliant"},
        "avetta": {"label": "Avetta", "grade": "B", "status": "Action needed"},
        "pec": {"label": "PEC Premier", "grade": "92", "status": "Green"},
        "veriforce": {"label": "Veriforce", "grade": "Active", "status": "Active"},
    }
    rec["coi"] = [
        {"name": "General Liability", "carrier": "Travelers", "expires": "2027-03-14"},
        {"name": "Workers' Compensation", "carrier": "Texas Mutual", "expires": "2026-09-22"},
        {"name": "Commercial Auto", "carrier": "Progressive", "expires": "2027-01-08"},
        {"name": "Umbrella / Excess", "carrier": "The Hartford", "expires": "2026-08-30"},
    ]
    rec["documents"] = [
        {"name": "Company Safety Manual", "sub": "Updated Aug 2026", "file": ""},
        {"name": "Fall Protection Program", "sub": "OSHA 1926.501", "file": ""},
        {"name": "Hazard Communication (HazCom)", "sub": "OSHA 1910.1200", "file": ""},
        {"name": "Lockout / Tagout (LOTO)", "sub": "OSHA 1910.147", "file": ""},
        {"name": "OSHA 300 / 300A Log — 2025", "sub": "Filed", "file": ""},
    ]
    rec["available"] = [
        {"name": "Confined Space Entry Program", "code": "OSHA 1910.146"},
        {"name": "Process Safety Management (PSM)", "code": "OSHA 1910.119"},
    ]
    return save_client(rec)


# ─────────────────────────── route registration ───────────────────────────

def register_portal(app) -> None:
    """Attach all portal + admin routes to an existing FastAPI app."""
    from fastapi import Body, File, Form, Request, UploadFile
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

    webui = Path(__file__).parent / "webui"
    CLIENTS_DIR.mkdir(parents=True, exist_ok=True)

    # ---- cookie auth helpers (read request, return payload or None) ----
    def client_session(request: Request) -> Optional[Dict[str, Any]]:
        p = _unsign(request.cookies.get(CLIENT_COOKIE, ""))
        return p if p and p.get("role") == "client" else None

    def admin_session(request: Request) -> bool:
        p = _unsign(request.cookies.get(ADMIN_COOKIE, ""))
        return bool(p and p.get("role") == "admin")

    def _secure(request: Request) -> bool:
        return request.url.scheme == "https"

    # ===================== PAGES =====================
    @app.get("/portal", response_class=HTMLResponse)
    def portal_page():
        f = webui / "portal.html"
        return f.read_text(encoding="utf-8") if f.is_file() else "<h1>Portal</h1><p>page missing</p>"

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page():
        f = webui / "admin.html"
        return f.read_text(encoding="utf-8") if f.is_file() else "<h1>Admin</h1><p>page missing</p>"

    # ===================== CLIENT API =====================
    @app.post("/portal/api/login")
    def portal_login(request: Request, body: dict = Body(...)):
        email = (body.get("email") or "").strip()
        pin = (body.get("pin") or "").strip()
        rec = find_by_email(email)
        if not rec or not rec.get("pin_hash") or hash_pin(rec["slug"], pin) != rec["pin_hash"]:
            return JSONResponse({"error": "Wrong email or PIN."}, status_code=401)
        resp = JSONResponse({"ok": True, "company": rec.get("company")})
        resp.set_cookie(CLIENT_COOKIE, _session("client", rec["slug"]),
                        httponly=True, samesite="lax", max_age=SESSION_TTL,
                        secure=_secure(request))
        return resp

    @app.post("/portal/api/logout")
    def portal_logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(CLIENT_COOKIE)
        return resp

    @app.get("/portal/api/me")
    def portal_me(request: Request):
        sess = client_session(request)
        if not sess:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sess["slug"])
        if not rec:
            return JSONResponse({"error": "account not found"}, status_code=404)
        return _public_view(rec)

    @app.post("/portal/api/request")
    def portal_request(request: Request, body: dict = Body(...)):
        sess = client_session(request)
        if not sess:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sess["slug"])
        if not rec:
            return JSONResponse({"error": "account not found"}, status_code=404)
        program = (body.get("program") or "a program").strip()
        note = (body.get("note") or "").strip()
        rec.setdefault("requests", []).append({
            "program": program, "note": note, "ts": _now(),
            "status": "new", "price": "",
        })
        save_client(rec)
        # notify Chris — never fail the request if email is down
        try:
            from .compliance import send_email
            send_email(
                to=os.environ.get("ORIGIN_MAIL_FROM", "info@originmanagementsolutions.com"),
                subject=f"Document request — {rec.get('company')}",
                body=(f"{rec.get('company')} requested: {program}\n\n"
                      f"Note: {note or '(none)'}\n\nOpen the admin console to quote it."),
            )
        except Exception as exc:
            print(f"[portal] request email skipped: {exc}")
        return {"ok": True}

    @app.get("/portal/api/doc")
    def portal_doc(request: Request, file: str = ""):
        sess = client_session(request)
        if not sess:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sess["slug"])
        if not rec:
            return JSONResponse({"error": "account not found"}, status_code=404)
        # only allow files listed on THIS client's record
        allowed = {d.get("file") for d in rec.get("documents", []) if d.get("file")}
        name = os.path.basename(file or "")
        if name not in allowed:
            return JSONResponse({"error": "not authorized for this document"}, status_code=403)
        path = _client_dir(sess["slug"]) / "docs" / name
        if not path.is_file():
            return JSONResponse({"error": "file not found"}, status_code=404)
        return FileResponse(str(path))

    # ===================== ADMIN API =====================
    @app.post("/portal/api/admin/login")
    def admin_login(request: Request, body: dict = Body(...)):
        if (body.get("password") or "") != ADMIN_PASSWORD:
            return JSONResponse({"error": "Wrong password."}, status_code=401)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(ADMIN_COOKIE, _session("admin"),
                        httponly=True, samesite="lax", max_age=SESSION_TTL,
                        secure=_secure(request))
        return resp

    @app.post("/portal/api/admin/logout")
    def admin_logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(ADMIN_COOKIE)
        return resp

    @app.get("/portal/api/admin/clients")
    def admin_clients(request: Request):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        return {"clients": list_clients()}

    @app.get("/portal/api/admin/client/{slug}")
    def admin_get_client(slug: str, request: Request):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_client(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        out = dict(rec)
        out["pin_set"] = bool(rec.get("pin_hash"))
        out.pop("pin_hash", None)
        return out

    @app.post("/portal/api/admin/client")
    def admin_save_client(request: Request, body: dict = Body(...)):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        company = (body.get("company") or "").strip()
        if not company:
            return JSONResponse({"error": "company name required"}, status_code=400)
        slug = (body.get("slug") or slugify(company)).strip()
        rec = load_client(slug) or _blank_client(company, body.get("email", ""),
                                                 body.get("client_type", "prequal"))
        rec["slug"] = slug
        rec["company"] = company
        for key in ("email", "client_type", "plan"):
            if key in body:
                rec[key] = body[key]
        for key in ("platforms", "coi", "documents", "available"):
            if key in body and body[key] is not None:
                rec[key] = body[key]
        # optional PIN (re)set
        pin = (body.get("pin") or "").strip()
        if pin:
            rec["pin_hash"] = hash_pin(slug, pin)
        save_client(rec)
        return {"ok": True, "slug": slug}

    @app.post("/portal/api/admin/client/{slug}/delete")
    def admin_delete_client(slug: str, request: Request):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        import shutil
        d = _client_dir(slug)
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": True}

    @app.post("/portal/api/admin/client/{slug}/upload")
    def admin_upload(slug: str, request: Request, file: UploadFile = File(...),
                     name: str = Form("")):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_client(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        safe = os.path.basename(file.filename or "document")
        dest = _client_dir(slug) / "docs"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / safe).write_bytes(file.file.read())
        doc_name = (name or safe).strip()
        # attach to an existing doc row of the same name, else add a new row
        for d in rec.setdefault("documents", []):
            if d.get("name") == doc_name:
                d["file"] = safe
                break
        else:
            rec["documents"].append({"name": doc_name, "sub": "Uploaded", "file": safe})
        save_client(rec)
        return {"ok": True, "file": safe}

    @app.get("/portal/api/admin/requests")
    def admin_requests(request: Request):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rows: List[Dict[str, Any]] = []
        for c in list_clients():
            rec = load_client(c["slug"])
            for i, r in enumerate(rec.get("requests", [])):
                rows.append({"slug": c["slug"], "company": c["company"], "index": i, **r})
        rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return {"requests": rows}

    @app.post("/portal/api/admin/request/quote")
    def admin_quote(request: Request, body: dict = Body(...)):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_client(body.get("slug", ""))
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            i = int(body.get("index"))
            rec["requests"][i]["price"] = (body.get("price") or "").strip()
            rec["requests"][i]["status"] = (body.get("status") or "quoted").strip()
        except Exception:
            return JSONResponse({"error": "bad request index"}, status_code=400)
        save_client(rec)
        return {"ok": True}

    @app.post("/portal/api/admin/seed")
    def admin_seed(request: Request):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = seed_test_client()
        return {"ok": True, "slug": rec["slug"], "login": {"email": rec["email"], "pin": "1234"}}
