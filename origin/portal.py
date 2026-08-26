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
        # Free-text description of the work the client actually performs. Drives
        # the gap finder: what programs their trade/scope requires vs. what they
        # have. Client fills this in; Chris can refine it in admin.
        "scope": "",
        "trade": "",  # optional NAICS/industry keyword Chris uses for the gap run
        "gap_report": None,   # last gap-analysis result (summary + gaps)
        "gap_run_at": "",
        "project_slug": "",   # linked Origin project (main site) — see _ensure_project
        "created": _now(),
        "updated": _now(),
    }


def _ensure_project(rec: Dict[str, Any]) -> None:
    """Mirror this portal client as an Origin PROJECT so it shows up on the main
    Origin site and the AI works in the same folder as the client's profile and
    documents (workdir = the client's portal folder). Idempotent; never fatal —
    a failure here must never block a signup or a save."""
    try:
        from .projects import ProjectManager
    except Exception:
        return
    try:
        pm = ProjectManager()
        slug = rec.get("project_slug")
        if slug and pm.get(slug):
            return  # already linked to a live project
        company = rec.get("company") or rec.get("slug") or "Client"
        workdir = str(_client_dir(rec["slug"]))
        notes = ("Portal client — the real profile + documents live in this "
                 "folder (client.json holds their platforms, grades and COI; "
                 "docs/ holds their files). Manage everything in the Origin admin "
                 "console at /admin; the client sees the results at /portal. Ask "
                 "the AI to review uploads, draft missing programs, or update the "
                 "profile right here.")
        proj = pm.create(company, workdir=workdir, notes=notes)
        rec["project_slug"] = proj.slug
    except Exception as exc:  # pragma: no cover
        print(f"[portal] project link skipped: {exc}")


# File types Origin AI might build into a client's folder that we should surface
# in their portal. Anything else (scratch notes, .json, etc.) is ignored.
_SYNC_DOC_EXTS = {".pdf", ".docx", ".doc", ".md", ".html", ".htm", ".txt",
                  ".xlsx", ".xls", ".csv", ".pptx", ".png", ".jpg", ".jpeg"}


def _sync_docs(rec: Dict[str, Any]) -> bool:
    """Surface files the Origin AI (or Chris) dropped into the client's project
    folder that aren't yet listed in their portal. Because each client's Origin
    project workdir IS their portal docs folder, anything the AI writes there
    lands here — this makes it show up for the client automatically.

    Skips files already referenced by a document or COI row, and drafts that
    were staged for review-first (rec['staged_files']) but not yet published.
    Returns True if it added anything."""
    slug = rec.get("slug")
    if not slug:
        return False
    docs = _client_dir(slug) / "docs"
    if not docs.is_dir():
        return False
    known = set()
    for d in rec.get("documents", []):
        if d.get("file"):
            known.add(d["file"])
    for c in rec.get("coi", []):
        if c.get("file"):
            known.add(c["file"])
    known |= set(rec.get("staged_files", []))
    changed = False
    try:
        entries = sorted(docs.iterdir())
    except Exception:
        return False
    for f in entries:
        try:
            if not f.is_file():
                continue
        except Exception:
            continue
        if f.name in known or f.name.startswith("."):
            continue
        if f.suffix.lower() not in _SYNC_DOC_EXTS:
            continue
        rec.setdefault("documents", []).append({
            "name": f.stem, "sub": "Added by Origin AI",
            "file": f.name, "source": "origin-ai",
        })
        changed = True
    if changed:
        rec["updated"] = _now()
    return changed


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
    _ensure_project(rec)  # mirror as an Origin project (shows on main site)
    return save_client(rec)


# ─────────────────────────── route registration ───────────────────────────

def register_portal(app) -> None:
    """Attach all portal + admin routes to an existing FastAPI app."""
    from fastapi import Body, File, Form, Request, UploadFile
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

    webui = Path(__file__).parent / "webui"
    CLIENTS_DIR.mkdir(parents=True, exist_ok=True)

    # Backfill: every existing portal client gets a linked Origin project so it
    # shows up on the main site. Idempotent and non-fatal.
    try:
        for _c in list_clients():
            _rec = load_client(_c["slug"])
            if not _rec:
                continue
            _before = _rec.get("project_slug", "")
            _ensure_project(_rec)
            if _rec.get("project_slug", "") != _before:
                save_client(_rec)
    except Exception as _exc:  # pragma: no cover
        print(f"[portal] project backfill skipped: {_exc}")

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

    @app.post("/portal/api/signup")
    def portal_signup(request: Request, body: dict = Body(...)):
        company = (body.get("company") or "").strip()
        email = (body.get("email") or "").strip()
        pin = (body.get("pin") or "").strip()
        if not company or not email or not pin:
            return JSONResponse({"error": "Company, email, and a PIN are all required."}, status_code=400)
        if len(pin) < 4:
            return JSONResponse({"error": "Your PIN must be at least 4 digits."}, status_code=400)
        if "@" not in email or "." not in email.split("@")[-1]:
            return JSONResponse({"error": "Please enter a valid email address."}, status_code=400)
        if find_by_email(email):
            return JSONResponse({"error": "An account with that email already exists — try logging in."}, status_code=409)
        # new self-signups start as documentation-only; Chris switches them to a
        # prequal-managed plan in the admin console once he knows their platforms.
        rec = _blank_client(company, email, "docs")
        base = rec.get("slug") or "client"
        slug = base
        n = 2
        while True:
            existing = load_client(slug)
            if not existing or (existing.get("email", "").strip().lower() == email.lower()):
                break
            slug = f"{base}-{n}"
            n += 1
        rec["slug"] = slug
        rec["pin_hash"] = hash_pin(slug, pin)
        _ensure_project(rec)  # mirror as an Origin project (shows on main site)
        save_client(rec)
        # let Chris know a new client just signed themselves up
        try:
            from .compliance import send_email
            send_email(
                to=os.environ.get("ORIGIN_MAIL_FROM", "info@originmanagementsolutions.com"),
                subject=f"New portal signup — {company}",
                body=(f"{company} ({email}) just created a portal account.\n\n"
                      f"Open the admin console to set their platforms, grades, "
                      f"COI dates, and documents."),
            )
        except Exception as exc:
            print(f"[portal] signup email skipped: {exc}")
        resp = JSONResponse({"ok": True, "company": company})
        resp.set_cookie(CLIENT_COOKIE, _session("client", slug),
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
        if _sync_docs(rec):
            save_client(rec)
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

    @app.post("/portal/api/scope")
    def portal_scope(request: Request, body: dict = Body(...)):
        """The client describes the work they perform. This feeds the gap
        finder so Origin can spot programs their trade requires that they may
        not know they need."""
        sess = client_session(request)
        if not sess:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sess["slug"])
        if not rec:
            return JSONResponse({"error": "account not found"}, status_code=404)
        rec["scope"] = (body.get("scope") or "").strip()
        rec["updated"] = _now()
        save_client(rec)
        return {"ok": True}

    @app.post("/portal/api/upload")
    def portal_upload(request: Request, file: UploadFile = File(...), name: str = Form("")):
        sess = client_session(request)
        if not sess:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sess["slug"])
        if not rec:
            return JSONResponse({"error": "account not found"}, status_code=404)
        safe = os.path.basename(file.filename or "document")
        dest = _client_dir(sess["slug"]) / "docs"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / safe).write_bytes(file.file.read())
        doc_name = (name or os.path.splitext(safe)[0]).strip() or safe
        rec.setdefault("documents", []).append(
            {"name": doc_name, "sub": "Uploaded by you", "file": safe, "source": "client"})
        rec["updated"] = _now()
        save_client(rec)
        # let Chris know the client submitted something to review for gap analysis
        try:
            from .compliance import send_email
            send_email(
                to=os.environ.get("ORIGIN_MAIL_FROM", "info@originmanagementsolutions.com"),
                subject=f"Client upload — {rec.get('company')}",
                body=(f"{rec.get('company')} uploaded a document: {doc_name} ({safe}).\n\n"
                      f"Review it in the admin console and run a gap analysis to see "
                      f"what still needs to be built."),
            )
        except Exception as exc:
            print(f"[portal] upload email skipped: {exc}")
        return {"ok": True, "file": safe}

    @app.get("/portal/api/doc")
    def portal_doc(request: Request, file: str = ""):
        sess = client_session(request)
        if not sess:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sess["slug"])
        if not rec:
            return JSONResponse({"error": "account not found"}, status_code=404)
        # only allow files listed on THIS client's record (documents + COI certs)
        allowed = {d.get("file") for d in rec.get("documents", []) if d.get("file")}
        allowed |= {c.get("file") for c in rec.get("coi", []) if c.get("file")}
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
        if _sync_docs(rec):
            save_client(rec)
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
        for key in ("email", "client_type", "plan", "scope", "trade"):
            if key in body:
                rec[key] = body[key]
        for key in ("platforms", "coi", "documents", "available"):
            if key in body and body[key] is not None:
                rec[key] = body[key]
        # optional PIN (re)set
        pin = (body.get("pin") or "").strip()
        if pin:
            rec["pin_hash"] = hash_pin(slug, pin)
        _ensure_project(rec)  # mirror as an Origin project (shows on main site)
        save_client(rec)
        return {"ok": True, "slug": slug, "project_slug": rec.get("project_slug", "")}

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

    @app.post("/portal/api/admin/client/{slug}/coi-upload")
    def admin_coi_upload(slug: str, request: Request, file: UploadFile = File(...),
                         index: str = Form(...)):
        """Attach the actual certificate PDF to one insurance / COI row so the
        client can view it in their portal."""
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_client(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            i = int(index)
            row = rec.get("coi", [])[i]
        except Exception:
            return JSONResponse({"error": "Save the certificate row first, then upload its file."},
                                status_code=400)
        safe = os.path.basename(file.filename or "certificate")
        dest = _client_dir(slug) / "docs"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / safe).write_bytes(file.file.read())
        row["file"] = safe
        rec["updated"] = _now()
        save_client(rec)
        return {"ok": True, "file": safe}

    @app.post("/portal/api/admin/client/{slug}/gap")
    def admin_gap(slug: str, request: Request, body: dict = Body(...)):
        """Run the Origin Gap Finder against ONE client — using the documents
        they've uploaded plus the scope of work they perform — and store the
        result on their record. Eliminates the manual download/re-upload loop.

        Body: {industry?, state?, operators?}. If industry is omitted, falls
        back to the client's trade keyword, then their scope-of-work text."""
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_client(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            from . import gaps as _gaps
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"error": f"gap engine unavailable: {exc}"}, status_code=500)

        industry = (body.get("industry") or rec.get("trade") or rec.get("scope") or "").strip()
        if not industry:
            return JSONResponse(
                {"error": "Add a scope of work (or trade) for this client first — "
                          "that's what tells the gap finder which programs their work requires."},
                status_code=400)
        state = (body.get("state") or "").strip() or None
        ops_raw = body.get("operators")
        if isinstance(ops_raw, str):
            operators = [o.strip() for o in ops_raw.split(",") if o.strip()] or None
        elif isinstance(ops_raw, list):
            operators = [str(o).strip() for o in ops_raw if str(o).strip()] or None
        else:
            operators = None

        # Read every document the client already has on file. Their scope text
        # is added as a pseudo-document so trade-specific work they describe but
        # haven't documented still informs coverage.
        docs = []
        docs_dir = _client_dir(slug) / "docs"
        for d in rec.get("documents", []):
            fn = d.get("file")
            if not fn:
                continue
            path = docs_dir / os.path.basename(fn)
            if not path.is_file():
                continue
            try:
                text = _gaps.extract_text(str(path))
            except Exception:
                text = ""
            docs.append({"name": d.get("name") or fn, "text": text})

        try:
            report = _gaps.find_gaps(industry, state=state, operators=operators, docs=docs)
        except Exception as exc:
            return JSONResponse({"error": f"gap analysis failed: {exc}"}, status_code=500)

        # Store a trimmed copy on the record (full gaps list + summary/meta).
        rec["gap_report"] = report
        rec["gap_run_at"] = _now()
        if body.get("industry"):
            rec["trade"] = industry
        save_client(rec)
        return {"ok": True, "report": report}

    @app.post("/portal/api/admin/client/{slug}/draft")
    def admin_draft(slug: str, request: Request, body: dict = Body(...)):
        """Build the missing/failing written programs Origin identified and,
        when publish is set, drop the finished .docx straight into the client's
        document vault so they see it in their portal."""
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_client(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        ids = body.get("ids") or []
        if not ids:
            return JSONResponse({"error": "no program ids selected"}, status_code=400)
        publish = bool(body.get("publish"))
        try:
            from . import gaps as _gaps
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"error": f"gap engine unavailable: {exc}"}, status_code=500)

        drafts = _gaps.draft_programs(ids, company=rec.get("company"),
                                      effective_date=body.get("effective_date"))
        if not drafts:
            return JSONResponse(
                {"error": "none of those standards have a draftable written program"},
                status_code=400)
        docs_dir = _client_dir(slug) / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        built = []
        for d in drafts:
            docx_bytes = _gaps.program_docx_bytes(d["title"], d["markdown"])
            if docx_bytes:
                fname = d["filename"].rsplit(".", 1)[0] + ".docx"
                (docs_dir / fname).write_bytes(docx_bytes)
            else:
                fname = d["filename"]
                (docs_dir / fname).write_text(d["markdown"], encoding="utf-8")
            row = {"name": d["title"], "sub": f"Built by Origin — {d.get('citation', '')}".strip(" —"),
                   "file": fname, "source": "origin-draft"}
            staged = rec.setdefault("staged_files", [])
            if publish:
                if fname in staged:
                    staged.remove(fname)
                # replace an existing row of the same name, else append
                for existing in rec.setdefault("documents", []):
                    if existing.get("name") == d["title"]:
                        existing.update(row)
                        break
                else:
                    rec["documents"].append(row)
            else:
                # staged for review only — keep auto-sync from publishing it
                if fname not in staged:
                    staged.append(fname)
            built.append({"id": d["id"], "title": d["title"], "file": fname,
                          "citation": d.get("citation", "")})
        rec["updated"] = _now()
        save_client(rec)
        return {"ok": True, "published": publish, "built": built}

    @app.get("/portal/api/admin/client/{slug}/draft/preview")
    def admin_draft_preview(slug: str, request: Request, file: str = ""):
        """Let Chris open a built draft from the admin console before publishing."""
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        name = os.path.basename(file or "")
        path = _client_dir(slug) / "docs" / name
        if not name or not path.is_file():
            return JSONResponse({"error": "file not found"}, status_code=404)
        return FileResponse(str(path))

    @app.get("/portal/api/admin/library")
    def admin_library(request: Request):
        """List the Asset Library masters so Chris can pick one to drop into a
        client's document vault straight from the admin console."""
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        try:
            from . import compliance as _cmp
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"error": f"asset library unavailable: {exc}"}, status_code=500)
        try:
            _cmp.ensure_library()
        except Exception:
            pass
        return {"ok": True, "templates": _cmp.list_templates()}

    @app.post("/portal/api/admin/client/{slug}/from-library")
    def admin_from_library(slug: str, request: Request, body: dict = Body(...)):
        """Render an Asset Library master into THIS client's vault as a finished,
        published document. body: {mid, publish?} — publish defaults true."""
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_client(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        mid = (body.get("mid") or "").strip()
        if not mid:
            return JSONResponse({"error": "no library document selected"}, status_code=400)
        try:
            from . import compliance as _cmp
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"error": f"asset library unavailable: {exc}"}, status_code=500)
        html = _cmp.read_master_html(mid)
        if not html:
            return JSONResponse({"error": "that library document was not found"}, status_code=404)
        title = _cmp.master_title(mid)
        docs_dir = _client_dir(slug) / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        # Prefer a finished PDF; fall back to the HTML master if PDF rendering
        # isn't available in this deployment.
        fname = None
        try:
            pdf_path = _cmp.unique_path(docs_dir, (_cmp.safe_filename(title).rsplit(".", 1)[0] + ".pdf"))
            _cmp.render_pdf(html, pdf_path, title=title)
            fname = pdf_path.name
        except Exception:
            html_path = _cmp.unique_path(docs_dir, _cmp.safe_filename(title))
            html_path.write_text(html, encoding="utf-8")
            fname = html_path.name
        publish = body.get("publish")
        publish = True if publish is None else bool(publish)
        row = {"name": title, "sub": "From Asset Library", "file": fname,
               "source": "asset-library"}
        if publish:
            for existing in rec.setdefault("documents", []):
                if existing.get("name") == title:
                    existing.update(row)
                    break
            else:
                rec["documents"].append(row)
        rec["updated"] = _now()
        save_client(rec)
        return {"ok": True, "published": publish, "title": title, "file": fname}

    @app.post("/portal/api/admin/client/{slug}/sync")
    def admin_sync_docs(slug: str, request: Request):
        """Pull any files the Origin AI dropped into this client's project
        folder into their portal document list. Runs automatically on load,
        but this lets Chris force it from the admin console."""
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_client(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        added = _sync_docs(rec)
        if added:
            save_client(rec)
        return {"ok": True, "added": added, "documents": rec.get("documents", [])}

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
