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
import secrets
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

# --- secrets ---------------------------------------------------------------
# The signing secret protects EVERY session cookie and is also the pepper mixed
# into every client PIN hash. If an attacker knew it, they could forge a cookie
# for ANY account and read another client's data — the exact "bleed over" we are
# guarding against. So we NEVER ship a hardcoded default:
#   1) ORIGIN_PORTAL_SECRET / ORIGIN_TOKEN from the environment always wins.
#   2) Otherwise we generate a strong random secret ONCE and persist it on the
#      data volume, so it stays stable across restarts/redeploys but is unique to
#      this deployment and unknown to anyone.
def _load_or_create_secret() -> str:
    env = (os.environ.get("ORIGIN_PORTAL_SECRET")
           or os.environ.get("ORIGIN_TOKEN") or "").strip()
    if env:
        return env
    secret_file = PORTAL_DIR / ".portal_secret"
    try:
        if secret_file.is_file():
            val = secret_file.read_text(encoding="utf-8").strip()
            if val:
                return val
        PORTAL_DIR.mkdir(parents=True, exist_ok=True)
        val = secrets.token_urlsafe(48)
        secret_file.write_text(val, encoding="utf-8")
        try:
            os.chmod(secret_file, 0o600)
        except Exception:
            pass
        return val
    except Exception:
        # Volume unavailable: fall back to a per-process random secret. Cookies
        # still can't be forged; they just won't survive a restart (12h TTL).
        return secrets.token_urlsafe(48)


SECRET = _load_or_create_secret()

# Admin console password. NO hardcoded default: if it isn't configured, admin
# login is refused entirely (see admin_login) rather than left wide open.
ADMIN_PASSWORD = (os.environ.get("ORIGIN_ADMIN_PASSWORD")
                  or os.environ.get("ORIGIN_TOKEN") or "").strip()

CLIENT_COOKIE = "origin_portal"
ADMIN_COOKIE = "origin_admin"
SESSION_TTL = 60 * 60 * 12  # 12 hours

# --- client-login brute-force lockout (in-memory, per email) ---------------
# PINs are short, so we throttle guessing: too many misses within the window
# temporarily locks that email. Resets on a correct login or a redeploy.
_LOGIN_FAILS: Dict[str, List[float]] = {}
_LOCK_WINDOW = 15 * 60   # 15 minutes
_LOCK_MAX = 6            # allowed misses within the window before lockout


def _login_locked(email: str) -> bool:
    hits = [t for t in _LOGIN_FAILS.get(email, []) if t > time.time() - _LOCK_WINDOW]
    _LOGIN_FAILS[email] = hits
    return len(hits) >= _LOCK_MAX


def _login_note_fail(email: str) -> None:
    _LOGIN_FAILS.setdefault(email, []).append(time.time())


def _login_clear(email: str) -> None:
    _LOGIN_FAILS.pop(email, None)


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


_PBKDF2_ITERS = 200_000


def hash_pin(slug: str, pin: str) -> str:
    """Hash a PIN with PBKDF2-HMAC-SHA256 (slow by design) plus a per-hash random
    salt and the deployment SECRET as pepper. Stored as
    'pbkdf2$<iters>$<salt_hex>$<hash_hex>'."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256",
                             f"{SECRET}:{slug}:{pin}".encode(),
                             salt, _PBKDF2_ITERS)
    return f"pbkdf2${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_pin(slug: str, pin: str, stored: str) -> bool:
    """Constant-time PIN check. Understands the PBKDF2 format above AND the legacy
    single-SHA256 format so existing accounts keep working after the upgrade."""
    if not stored:
        return False
    if stored.startswith("pbkdf2$"):
        try:
            _, iters, salt_hex, hash_hex = stored.split("$")
            dk = hashlib.pbkdf2_hmac("sha256",
                                     f"{SECRET}:{slug}:{pin}".encode(),
                                     bytes.fromhex(salt_hex), int(iters))
            return hmac.compare_digest(dk.hex(), hash_hex)
        except Exception:
            return False
    # legacy: plain SHA256 hex
    legacy = hashlib.sha256(f"{SECRET}:{slug}:{pin}".encode()).hexdigest()
    return hmac.compare_digest(legacy, stored)


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
    if not email or not CLIENTS_DIR.is_dir():
        return None
    matches: List[Dict[str, Any]] = []
    for d in sorted(CLIENTS_DIR.iterdir()):
        rec = load_client(d.name)
        if rec and (rec.get("email", "").strip().lower() == email):
            matches.append(rec)
    if not matches:
        return None
    # If more than one record somehow shares this email, prefer the most
    # recently updated one. Otherwise a freshly edited profile could appear to
    # "revert" to a stale/seed duplicate that merely sorts first by folder name.
    matches.sort(key=lambda r: r.get("updated", ""), reverse=True)
    return matches[0]


def _latest_lead_for(email: str) -> Dict[str, Any]:
    """Return the most recent captured lead for this email, merged across all of
    that email's submissions so the diagnosis survives even if the visitor's
    latest click (e.g. 'let us handle it') carried fewer fields than an earlier
    tool run. Fields from newer submissions win; industry/issues/program_id from
    any submission are kept. Returns {} if nothing is on file."""
    email = (email or "").strip().lower()
    if not email:
        return {}
    try:
        from .rescue import LEADS_FILE
    except Exception:
        return {}
    if not LEADS_FILE.is_file():
        return {}
    merged: Dict[str, Any] = {}
    try:
        for line in LEADS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if (d.get("email") or "").strip().lower() != email:
                continue
            # Newest line wins for scalar fields; but never overwrite a present
            # diagnosis field with an empty one from a later, thinner submission.
            for k, v in d.items():
                if v in (None, "", [], {}):
                    continue
                merged[k] = v
    except Exception:
        return {}
    return merged


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


def _client_by_project(project_slug: str) -> Optional[Dict[str, Any]]:
    """Find the portal client mirrored by a given Origin project slug, so the
    main Origin chat page can push a file the AI built into the right vault."""
    project_slug = (project_slug or "").strip()
    if not project_slug:
        return None
    for c in list_clients():
        rec = load_client(c["slug"])
        if rec and rec.get("project_slug") == project_slug:
            return rec
    # fallback: the project slug usually equals the client slug
    return load_client(project_slug)


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
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

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
        # Railway terminates TLS at its edge and forwards plain HTTP internally,
        # so request.url.scheme is "http" even for real HTTPS visitors. Honor the
        # forwarded-proto header so session cookies still get the Secure flag.
        xfp = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        return request.url.scheme == "https" or xfp == "https"

    # ===================== PAGES =====================
    # Never let the browser cache these pages — a stale copy of the login JS is
    # the usual reason the portal "misbehaves" after a deploy. no-store forces a
    # fresh fetch every visit.
    _NO_STORE = {"Cache-Control": "no-store, must-revalidate",
                 "Pragma": "no-cache", "Expires": "0"}

    @app.get("/portal", response_class=HTMLResponse)
    def portal_page():
        f = webui / "portal.html"
        html = f.read_text(encoding="utf-8") if f.is_file() else "<h1>Portal</h1><p>page missing</p>"
        return HTMLResponse(html, headers=_NO_STORE)

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page():
        f = webui / "admin.html"
        html = f.read_text(encoding="utf-8") if f.is_file() else "<h1>Admin</h1><p>page missing</p>"
        return HTMLResponse(html, headers=_NO_STORE)

    # ===================== CLIENT API =====================
    @app.post("/portal/api/login")
    def portal_login(request: Request, body: dict = Body(...)):
        email = (body.get("email") or "").strip()
        pin = (body.get("pin") or "").strip()
        key = email.lower()
        if _login_locked(key):
            return JSONResponse(
                {"error": "Too many attempts. Please wait a few minutes and try again."},
                status_code=429)
        rec = find_by_email(email)
        if not rec or not rec.get("pin_hash") or not verify_pin(rec["slug"], pin, rec["pin_hash"]):
            _login_note_fail(key)
            return JSONResponse({"error": "Wrong email or PIN."}, status_code=401)
        _login_clear(key)
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
        # Fail closed: if no admin password is configured, do NOT fall back to a
        # guessable default — refuse admin access entirely.
        if not ADMIN_PASSWORD:
            return JSONResponse(
                {"error": "Admin console is not configured. Set ORIGIN_ADMIN_PASSWORD."},
                status_code=503)
        supplied = (body.get("password") or "")
        if not hmac.compare_digest(supplied, ADMIN_PASSWORD):
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
        incoming_slug = (body.get("slug") or "").strip()
        email = (body.get("email") or "").strip()
        # Guard against duplicate accounts for the same email. Login looks clients
        # up by email, so two records sharing one email make a client's profile
        # appear to "revert" to whichever record sorts first. When saving a NEW
        # client (no slug yet) whose email already exists, edit that existing
        # record in place instead of minting a second one.
        if not incoming_slug and email:
            existing = find_by_email(email)
            if existing:
                incoming_slug = existing["slug"]
        slug = incoming_slug or slugify(company)
        rec = load_client(slug) or _blank_client(company, email,
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

    @app.post("/portal/api/admin/client/{slug}/300log")
    def admin_300log_add(slug: str, request: Request, body: dict = Body(...)):
        """Run a recordability determination and, if the case is recordable,
        append it to THIS client's OSHA 300 Log. The engine makes the 1904 call
        (work-related? new case? recordable? which column? report to OSHA?) — this
        route just writes the verdict onto the client's log so Chris never has to
        re-key it. Body carries the recordability `facts` plus a little case
        metadata (employee, job_title, description, incident_date, location)."""
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_client(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            from . import recordability as _rec
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"error": f"recordability engine unavailable: {exc}"},
                                status_code=500)
        facts = body.get("facts")
        if not isinstance(facts, dict):
            return JSONResponse({"error": "missing recordability facts"}, status_code=400)
        try:
            det = _rec.evaluate(facts)
        except Exception as exc:
            return JSONResponse({"error": f"determination failed: {exc}"}, status_code=400)

        # Only recordable cases belong on the 300 Log. If the engine still needs
        # info, or found the case not recordable, hand the verdict back instead of
        # writing a bad row — the caller can show it and fix the inputs.
        if det.get("recordable") is not True:
            return JSONResponse(
                {"error": "not logged — this case is not recordable (or still needs info).",
                 "determination": det}, status_code=422)

        log = rec.setdefault("osha_300_log", [])
        case_no = (max((c.get("case_no", 0) for c in log), default=0) or 0) + 1
        # 1904.29(b)(6)-(9): privacy-concern cases must NOT name the employee on
        # the log — store "Privacy Case" for display, keep the real name only in a
        # separate, restricted field the client keeps off the public log.
        privacy = bool(det.get("privacy_case"))
        real_name = (body.get("employee") or "").strip()
        case = {
            "case_no": case_no,
            "incident_date": (body.get("incident_date") or "").strip(),
            "employee": "Privacy Case" if privacy else real_name,
            "job_title": (body.get("job_title") or "").strip(),
            "location": (body.get("location") or "").strip(),
            "description": (body.get("description") or "").strip(),
            "column": det.get("column"),
            "column_label": det.get("column_label"),
            "case_type": det.get("case_type"),
            "days_away": det.get("days_away"),
            "restricted_days": det.get("restricted_days"),
            "privacy_case": privacy,
            "reporting": det.get("reporting") or {},
            "summary": det.get("summary"),
            "basis": det.get("steps") or [],
            "logged_at": _now(),
        }
        if privacy and real_name:
            # 1904.29(b)(7): keep a separate confidential list of the names left
            # off the log, so the employer can still tie the case back if needed.
            rec.setdefault("osha_300_privacy_list", []).append(
                {"case_no": case_no, "employee": real_name})
        log.append(case)
        rec["updated"] = _now()
        save_client(rec)
        return {"ok": True, "case": case, "determination": det,
                "log_count": len(log)}

    @app.post("/portal/api/admin/client/{slug}/300log/delete")
    def admin_300log_delete(slug: str, request: Request, body: dict = Body(...)):
        """Remove one case from a client's 300 Log by case_no (a mis-keyed or
        superseded entry). Also drops any matching confidential privacy-list row."""
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_client(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            case_no = int(body.get("case_no"))
        except Exception:
            return JSONResponse({"error": "case_no required"}, status_code=400)
        log = rec.get("osha_300_log", [])
        rec["osha_300_log"] = [c for c in log if c.get("case_no") != case_no]
        if "osha_300_privacy_list" in rec:
            rec["osha_300_privacy_list"] = [
                p for p in rec["osha_300_privacy_list"] if p.get("case_no") != case_no]
        rec["updated"] = _now()
        save_client(rec)
        return {"ok": True, "log_count": len(rec["osha_300_log"])}

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

        # Draft each program in the client's INDUSTRY context so the docs land
        # already written for their trade (Phase 3) — only company/scope tweaks
        # remain. Sector comes from their trade keyword, else their scope text.
        _sector_src = (rec.get("trade") or rec.get("scope") or "").strip() or None
        drafts = _gaps.draft_programs(ids, company=rec.get("company"),
                                      effective_date=body.get("effective_date"),
                                      sector=_sector_src)
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

    @app.get("/portal/api/admin/library/preview", response_class=HTMLResponse)
    def admin_library_preview(request: Request, mid: str = ""):
        """Render an Asset Library master straight to the browser so Chris can
        eyeball the exact document BEFORE dropping it into a client's vault.
        Read-only: nothing is written or published."""
        if not admin_session(request):
            return HTMLResponse("<h1>Admin only</h1>", status_code=401)
        try:
            from . import compliance as _cmp
        except Exception as exc:  # pragma: no cover
            return HTMLResponse(f"<h1>Asset library unavailable</h1><p>{exc}</p>",
                                status_code=500)
        mid = (mid or "").strip()
        html = _cmp.read_master_html(mid) if mid else None
        if not html:
            return HTMLResponse("<h1>Document not found</h1>", status_code=404)
        return HTMLResponse(html, headers=_NO_STORE)

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

    @app.post("/api/portal/publish")
    def api_portal_publish(request: Request, body: dict = Body(...)):
        """Push a file the Origin AI built (inside a portal-client project) into
        that client's document vault. Lives under /api so it's gated by the main
        app's origin-token — the Origin chat page is already signed in with it."""
        project_slug = (body.get("project_slug") or "").strip()
        rel = (body.get("path") or "").strip()
        if not project_slug or not rel:
            return JSONResponse({"error": "project and file path are required"}, status_code=400)
        rec = _client_by_project(project_slug)
        if not rec:
            return JSONResponse(
                {"error": "This project isn't linked to a client portal, so there's "
                          "nowhere to send it."}, status_code=404)
        slug = rec["slug"]
        workdir = _client_dir(slug)
        try:
            src = (workdir / rel).resolve()
            src.relative_to(workdir.resolve())  # stay inside the client folder
        except Exception:
            return JSONResponse({"error": "invalid file path"}, status_code=400)
        if not src.is_file():
            return JSONResponse({"error": "file not found"}, status_code=404)
        docs_dir = workdir / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        dest = docs_dir / src.name
        if src.resolve() != dest.resolve():
            dest.write_bytes(src.read_bytes())
        fname = dest.name
        title = (body.get("name") or src.stem).strip() or src.stem
        row = {"name": title, "sub": "Built by Origin AI", "file": fname,
               "source": "origin-ai"}
        for existing in rec.setdefault("documents", []):
            if existing.get("name") == title or existing.get("file") == fname:
                existing.update(row)
                break
        else:
            rec["documents"].append(row)
        staged = rec.get("staged_files", [])
        if fname in staged:
            staged.remove(fname)
        rec["updated"] = _now()
        save_client(rec)
        return {"ok": True, "company": rec.get("company"), "slug": slug,
                "file": fname, "title": title}

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

    @app.get("/portal/api/admin/leads")
    def admin_leads(request: Request):
        """List the leads captured by the public free tools (rescue_leads.jsonl)
        so Chris can turn a hot lead into a portal client in one click. Each row
        is flagged `converted` if a portal account already exists for that email."""
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rows: List[Dict[str, Any]] = []
        try:
            from .rescue import LEADS_FILE
        except Exception:
            return {"leads": []}
        try:
            if LEADS_FILE.is_file():
                for line in LEADS_FILE.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    email = (d.get("email") or "").strip()
                    existing = find_by_email(email) if email else None
                    row = {
                        "ts": d.get("ts", ""),
                        "name": d.get("name", ""),
                        "company": d.get("company", ""),
                        "email": email,
                        "phone": d.get("phone", ""),
                        "platform": d.get("platform") or d.get("industry") or "",
                        "source": d.get("source") or "grade-rescue",
                        "intent": d.get("intent", ""),
                        "projected_grade": d.get("projected_grade", ""),
                        # Diagnosis captured with the lead — lets the card preview
                        # what will be built on convert (Phase 4).
                        "industry": (d.get("industry") or "").strip(),
                        "issue_count": len(d.get("issues") or []),
                        "matched_program": d.get("matched_program") or "",
                        "converted": bool(existing),
                        "slug": existing.get("slug") if existing else "",
                    }
                    # Lead Radar rows carry their own enrichment (citation authority,
                    # penalty, callability score, source URL). Pass it through so the
                    # admin view can show why this lead is worth a call.
                    if (d.get("source") == "lead-radar") or d.get("radar_kind"):
                        for k in ("radar_kind", "radar_label", "radar_authority",
                                  "radar_penalty", "radar_state", "radar_city",
                                  "radar_naics", "radar_opened", "radar_score",
                                  "radar_priority", "radar_url", "radar_summary",
                                  "radar_trade_match", "radar_address",
                                  "radar_rating", "radar_mine", "radar_dot"):
                            row[k] = d.get(k, "")
                    rows.append(row)
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"error": f"could not read leads: {exc}"}, status_code=500)
        # newest first; collapse repeat submissions from the same email to the
        # most recent so the list stays actionable.
        rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
        seen: Dict[str, dict] = {}
        deduped = []
        for r in rows:
            key = (r["email"] or "").lower()
            if key and key in seen:
                # Backfill the diagnosis onto the kept (newest) row from an older
                # submission — a later "handle" click can carry fewer fields than
                # the tool run that produced the actual diagnosis.
                kept = seen[key]
                if not kept.get("industry") and r.get("industry"):
                    kept["industry"] = r["industry"]
                if not kept.get("issue_count") and r.get("issue_count"):
                    kept["issue_count"] = r["issue_count"]
                if not kept.get("matched_program") and r.get("matched_program"):
                    kept["matched_program"] = r["matched_program"]
                continue
            if key:
                seen[key] = r
            deduped.append(r)
        return {"leads": deduped}

    @app.post("/portal/api/admin/lead/convert")
    def admin_lead_convert(request: Request, body: dict = Body(...)):
        """Turn a captured lead into a portal client account and email them an
        invite with their login link + a temporary PIN. Idempotent: if a client
        already exists for that email, returns it instead of making a duplicate."""
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        email = (body.get("email") or "").strip()
        if "@" not in email or "." not in email.split("@")[-1]:
            return JSONResponse({"error": "A valid email is required to create the account."},
                                status_code=400)
        existing = find_by_email(email)
        if existing:
            return {"ok": True, "already": True, "slug": existing["slug"],
                    "company": existing.get("company", "")}
        company = (body.get("company") or "").strip() or email.split("@")[0]
        rec = _blank_client(company, email, "docs")
        base = rec.get("slug") or "client"
        slug, n = base, 2
        while load_client(slug):
            slug = f"{base}-{n}"
            n += 1
        rec["slug"] = slug
        # extra context carried from the lead (save_client persists the whole rec)
        if body.get("phone"):
            rec["phone"] = (body.get("phone") or "").strip()
        rec["lead_source"] = (body.get("source") or "free tool").strip()
        # temporary PIN so they can sign in immediately; they can be told to
        # change it later. Emailed to the client and returned to Chris.
        import random
        temp_pin = f"{random.randint(0, 999999):06d}"
        rec["pin_hash"] = hash_pin(slug, temp_pin)
        _ensure_project(rec)  # mirror as an Origin project on the main site

        # ── Phase 4: pre-load the exact documentation this client needs ──────
        # Recover the diagnosis captured with their lead (trade + flagged issues,
        # or the OSHA standard they were cited under) and turn it into the full
        # required-program list — priorities flagged — so nothing is missed. Runs
        # the gap analysis and seeds the "Documents (included in plan)" tab with
        # a build target for each required written program.
        recommended_count = 0
        jha_count = 0
        try:
            lead_dx = _latest_lead_for(email)
        except Exception:
            lead_dx = {}
        lead_industry = (lead_dx.get("industry") or "").strip()
        if lead_industry and not rec.get("trade"):
            rec["trade"] = lead_industry
        try:
            from . import gaps as _gaps
            plan = _gaps.recommend_documents(
                industry=lead_industry or None,
                issues=lead_dx.get("issues") or [],
                citation_program_id=lead_dx.get("program_id") or None,
            )
        except Exception:
            plan = None
        if plan:
            rec["recommended_docs"] = plan.get("documents", [])
            if plan.get("report"):
                rec["gap_report"] = plan["report"]
                rec["gap_run_at"] = _now()
            existing_names = {d.get("name") for d in rec.get("documents", [])}
            for d in plan.get("documents", []):
                # Only draftable written programs become build rows; reference
                # items (insurance/benchmarks) stay in recommended_docs as advice.
                if not d.get("needs_program"):
                    continue
                if d.get("title") in existing_names:
                    continue
                sub = f"Recommended \u2014 {d['priority']}"
                if d.get("citation"):
                    sub += f" \u00b7 {d['citation']}"
                needs_jha = bool(d.get("needs_jha"))
                if needs_jha:
                    sub += " \u00b7 + JSA"
                rec.setdefault("documents", []).append({
                    "name": d["title"], "sub": sub, "file": "",
                    "source": "recommended", "to_build": True,
                    "program_id": d["id"], "priority": d["priority"],
                    "needs_jha": needs_jha,
                    "sub_sections": d.get("sub_sections") or [],
                })
                existing_names.add(d["title"])
                recommended_count += 1
                if needs_jha:
                    jha_count += 1

        save_client(rec)
        # invite the client with their login link + temp PIN
        invited = False
        invite_err = ""
        try:
            base_url = str(request.base_url).rstrip("/")
        except Exception:
            base_url = "https://origin-production-1352.up.railway.app"
        portal_url = f"{base_url}/portal"
        try:
            from .compliance import send_email, resend_configured, smtp_configured
            if resend_configured() or smtp_configured():
                res = send_email(
                    to=email,
                    subject="Your compliance portal is ready",
                    body=(
                        f"Hi{(' ' + (body.get('name') or '').strip()) if body.get('name') else ''},\n\n"
                        f"We've set up a private portal for {company} where you can view and "
                        f"download the compliance documents we prepare for you, and track your "
                        f"prequal status in one place.\n\n"
                        f"Sign in here: {portal_url}\n"
                        f"Email: {email}\n"
                        f"Temporary PIN: {temp_pin}\n\n"
                        f"You'll be able to set your own PIN after your first sign-in.\n\n"
                        f"— Origin Management Solutions\n"
                        f"info@originmanagementsolutions.com"
                    ),
                )
                invited = bool(res.get("sent"))
                if not invited:
                    invite_err = res.get("error", "")
            else:
                invite_err = ("Email isn't turned on yet (no RESEND_API_KEY), so no invite "
                              "was sent. Give them the login link, email, and PIN below yourself.")
        except Exception as exc:  # pragma: no cover
            invite_err = str(exc)
        return {"ok": True, "slug": slug, "company": company,
                "temp_pin": temp_pin, "invited": invited,
                "invite_error": invite_err, "portal_url": portal_url,
                "recommended_count": recommended_count,
                "jha_count": jha_count}

    @app.get("/portal/api/admin/radar/config")
    def admin_radar_config(request: Request):
        """Expose the Lead Radar knobs + whether a DOL key is configured."""
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        try:
            from . import leadradar as _radar
            return {"ok": True, "config": _radar.radar_config_schema()}
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.post("/portal/api/admin/radar/run")
    def admin_radar_run(request: Request, body: dict = Body(default=None)):
        """Run Lead Radar on demand: pull recent penalty-bearing OSHA / state-OSHA
        citations (+ optional safety-enforcement news), score for callability, and
        write new leads into the admin Leads view (source='lead-radar'). Returns a
        summary + the scored leads (top-of-list first)."""
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        body = body or {}
        try:
            from . import leadradar as _radar
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"error": f"radar unavailable: {exc}"}, status_code=500)

        def _states(v):
            if not v:
                return None
            if isinstance(v, str):
                v = [s for s in re.split(r"[,\s]+", v) if s]
            return [str(s).strip().upper() for s in v if str(s).strip()] or None

        try:
            result = _radar.run_radar(
                states=_states(body.get("states")),
                since_days=int(body.get("since_days") or 30),
                min_penalty=float(body.get("min_penalty") or 1000),
                include_news=bool(body.get("include_news", True)),
                include_fmcsa=bool(body.get("include_fmcsa", True)),
                include_msha=bool(body.get("include_msha", True)),
                target_trades_only=bool(body.get("target_trades_only", False)),
                min_score=int(body.get("min_score") or 0),
                persist=bool(body.get("persist", True)),
            )
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"error": str(exc)}, status_code=500)
        # Trim the returned leads so the admin response stays light; the full set
        # is already persisted into the Leads view.
        result["leads"] = result.get("leads", [])[:100]
        return result

    @app.post("/portal/api/admin/radar/export.csv")
    def admin_radar_export_csv(request: Request, body: dict = Body(default=None)):
        """Run Lead Radar and stream the results back as a clean CSV download
        (exact columns: Company Name, Street Address, City, State, Zip,
        Violation Type, Penalty, Date Issued, Source). Does NOT persist —
        this is a pull-the-list-and-go export for outreach."""
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        body = body or {}
        try:
            from . import leadradar as _radar
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"error": f"radar unavailable: {exc}"}, status_code=500)

        def _states(v):
            if not v:
                return None
            if isinstance(v, str):
                v = [s for s in re.split(r"[,\s]+", v) if s]
            return [str(s).strip().upper() for s in v if str(s).strip()] or None

        try:
            csv_text = _radar.run_radar_csv(
                states=_states(body.get("states")),
                since_days=int(body.get("since_days") or 30),
                min_penalty=float(body.get("min_penalty") or 1000),
                include_news=bool(body.get("include_news", True)),
                include_fmcsa=bool(body.get("include_fmcsa", True)),
                include_msha=bool(body.get("include_msha", True)),
                target_trades_only=bool(body.get("target_trades_only", False)),
                min_score=int(body.get("min_score") or 0),
            )
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"error": str(exc)}, status_code=500)
        import datetime as _dt
        fname = "reverse-leads-%s.csv" % _dt.date.today().isoformat()
        return Response(
            content=csv_text,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="%s"' % fname},
        )

    @app.post("/portal/api/admin/client/{slug}/email-docs")
    def admin_email_docs(slug: str, request: Request, body: dict = Body(...)):
        """Email the client the finished documents in their vault (attached), plus
        a link to sign in to their portal. Delivery = portal + auto-email."""
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_client(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        to = (rec.get("email") or "").strip()
        if "@" not in to:
            return JSONResponse({"error": "This client has no email on file."}, status_code=400)
        docs_dir = _client_dir(slug) / "docs"
        # optionally limit to specific files; default = every published document
        wanted = set(body.get("files") or [])
        paths, names = [], []
        for d in rec.get("documents", []):
            fn = d.get("file")
            if not fn:
                continue
            if wanted and fn not in wanted:
                continue
            p = docs_dir / os.path.basename(fn)
            if p.is_file():
                paths.append(p)
                names.append(d.get("name") or fn)
        if not paths:
            return JSONResponse(
                {"error": "There are no published documents with files to send yet."},
                status_code=400)
        try:
            base_url = str(request.base_url).rstrip("/")
        except Exception:
            base_url = "https://origin-production-1352.up.railway.app"
        portal_url = f"{base_url}/portal"
        doc_list = "\n".join(f"  \u2022 {n}" for n in names)
        try:
            from .compliance import send_email, resend_configured, smtp_configured
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"error": f"mailer unavailable: {exc}"}, status_code=500)
        if not (resend_configured() or smtp_configured()):
            return JSONResponse(
                {"error": "Email isn't turned on yet. Add a RESEND_API_KEY on Railway to "
                          "send documents by email. (They're already in the client's portal.)"},
                status_code=503)
        res = send_email(
            to=to,
            subject=f"Your compliance documents from Origin Management Solutions",
            body=(
                f"Hi{(' ' + (rec.get('contact') or '')) if rec.get('contact') else ''},\n\n"
                f"Your documents are ready. They're attached to this email, and you can also "
                f"view or re-download them anytime in your portal:\n\n"
                f"{portal_url}\n\n"
                f"Included:\n{doc_list}\n\n"
                f"Reply to this email if you need anything adjusted.\n\n"
                f"— Origin Management Solutions\n"
                f"info@originmanagementsolutions.com"
            ),
            attachments=paths,
        )
        if res.get("sent"):
            rec["docs_emailed_at"] = _now()
            save_client(rec)
            return {"ok": True, "sent": True, "count": len(paths), "to": to}
        return JSONResponse({"error": res.get("error", "send failed")}, status_code=502)

    @app.post("/portal/api/admin/seed")
    def admin_seed(request: Request):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = seed_test_client()
        return {"ok": True, "slug": rec["slug"], "login": {"email": rec["email"], "pin": "1234"}}
