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
import random
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
GC_COOKIE = "origin_gc"
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


def _is_fresh(ts: str, secs: int = 6) -> bool:
    """True if the ISO timestamp `ts` (as produced by _now) is within `secs`
    seconds of now. Used for live 'typing…' indicators."""
    if not ts:
        return False
    try:
        t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        return (time.time() - t) <= secs
    except Exception:
        return False


def _typing_path(dirpath: Path) -> Path:
    return dirpath / "typing.json"


def _set_typing(dirpath: Path, who: str) -> None:
    """Record that `who` (owner|gc|sub) is typing right now. Stored in a tiny
    sidecar file so keystroke pings never race with the message record."""
    try:
        dirpath.mkdir(parents=True, exist_ok=True)
        p = _typing_path(dirpath)
        d = {}
        try:
            d = json.loads(p.read_text())
        except Exception:
            d = {}
        d[who] = _now()
        p.write_text(json.dumps(d))
    except Exception:
        pass


def _get_typing(dirpath: Path, who: str, secs: int = 6) -> bool:
    try:
        d = json.loads(_typing_path(dirpath).read_text())
    except Exception:
        return False
    return _is_fresh(d.get(who, ""), secs)


def _save_msgfile(dirpath: Path, upload) -> str:
    """Store a message attachment with a short random prefix to avoid
    collisions, and return the on-disk filename."""
    dirpath.mkdir(parents=True, exist_ok=True)
    orig = os.path.basename(getattr(upload, "filename", "") or "file")
    safe = secrets.token_hex(3) + "_" + orig
    (dirpath / safe).write_bytes(upload.file.read())
    return safe


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "client"


# ── Asset-library document fill-in ───────────────────────────────────────────
# When a GC pulls a master into a sub's vault it still carries {{TOKENS}}. These
# are the fields the GC can fill in from the app (token -> friendly label). The
# document is always re-rendered from the pristine master so a field can be
# edited again later (we never lose the token by baking a value into the file).
_LIB_DOC_FIELDS = [
    ("COMPANY_NAME",          "Company name"),
    ("COMPANY_ADDRESS",       "Company address"),
    ("EFFECTIVE_DATE",        "Effective date"),
    ("PROGRAM_ADMINISTRATOR", "Program administrator"),
    ("ADMIN_TITLE",           "Administrator title"),
    ("SCOPE",                 "Scope of work"),
]


def _render_library_doc(mid: str, fields: Optional[dict]):
    """Re-render an asset-library master with the GC's fill-in values applied to
    its {{TOKENS}}, wrapped as a clean, continuous, printable HTML document.
    Returns (doc_html, title); (None, None) if the master no longer exists."""
    from . import compliance as _cmp
    html = _cmp.read_master_html(mid)
    if not html:
        return None, None
    title = _cmp.master_title(mid)
    f = fields or {}
    for token, _label in _LIB_DOC_FIELDS:
        val = f.get(token) or f.get(token.lower())
        if val:
            html = html.replace("{{%s}}" % token, str(val))
    # Keep the review-date tokens coherent with a supplied effective date so the
    # cover table doesn't show raw {{...}} placeholders next to a real date.
    eff = f.get("EFFECTIVE_DATE") or f.get("effective_date")
    if eff:
        html = html.replace("{{LAST_REVIEW_DATE}}", str(eff))
    return _cmp.wrap_document(html, title), title


def _autofill_fields(rec: Dict[str, Any]) -> Dict[str, str]:
    """Build a best-guess {TOKEN: value} set for a sub from what the platform
    already knows — so the GC fills every document with one click instead of
    typing the same company details into each one.

    Priority (never overwrites a stronger source with a weaker one):
      1. Values the GC already filled on ANY of this sub's documents. Fill one,
         and the rest inherit it — this is the biggest win and needs no AI.
      2. The sub's own profile: company name, scope/trade, today's date.
    Returns only tokens we have a value for; unknown ones are simply omitted so
    the GC can still type them by hand.
    """
    import time as _t
    fields: Dict[str, str] = {}
    valid = {t for t, _ in _LIB_DOC_FIELDS}
    # 1. propagate anything already filled elsewhere on this sub
    for d in rec.get("documents", []):
        for k, v in (d.get("fields") or {}).items():
            if k in valid and v and not fields.get(k):
                fields[k] = str(v).strip()
    # 2. deterministic facts from the sub's profile
    company = (rec.get("company") or "").strip()
    if company and not fields.get("COMPANY_NAME"):
        fields["COMPANY_NAME"] = company
    scope_src = (rec.get("scope") or rec.get("trade") or "").strip()
    if scope_src and not fields.get("SCOPE"):
        fields["SCOPE"] = scope_src
    if not fields.get("EFFECTIVE_DATE"):
        fields["EFFECTIVE_DATE"] = _t.strftime("%Y-%m-%d")
    return fields


def _recover_doc_mid(row: Dict[str, Any]) -> str:
    """Find the library-master id (mid) for a document row that lost it.

    Documents published before the mid wiring shipped have no ``mid`` on their
    row, so they read as "not editable". If such a row is a template-derived
    HTML document, match its title back to a library master so auto-fill and
    the Fill editor light up for it again. Uploaded files (PDFs, the GC's own
    docs) are never matched — we must not overwrite them with a template."""
    mid = (row.get("mid") or "").strip()
    if mid:
        return mid
    fname = os.path.basename(row.get("file") or "")
    if Path(fname).suffix.lower() not in (".html", ".htm"):
        return ""
    if (row.get("source") or "") not in ("asset-library", "origin-draft", "origin-ai"):
        return ""
    title = (row.get("name") or "").strip().lower()
    if not title:
        return ""
    try:
        from . import compliance as _cmp
        for t in _cmp.list_templates():
            if (t.get("title") or "").strip().lower() == title:
                return t["id"]
    except Exception:
        pass
    return ""


def _autofill_ai_enrich(rec: Dict[str, Any], fields: Dict[str, str]) -> Dict[str, str]:
    """Optional polish on top of _autofill_fields: if an LLM is configured, ask
    it to turn a bare trade keyword into a proper one-sentence scope of work and
    to suggest a sensible administrator title. Purely additive — it only fills
    tokens still missing or weak, never overwrites known-good values, and any
    failure (no API key, network, bad JSON) is swallowed so auto-fill always
    works without AI."""
    try:
        need_scope = len((fields.get("SCOPE") or "").split()) < 4
        need_title = not fields.get("ADMIN_TITLE")
        if not (need_scope or need_title):
            return fields
        from .config import load_config
        from .llm import build_provider
        provider = build_provider(load_config().llm)
        company = fields.get("COMPANY_NAME") or rec.get("company") or "the company"
        trade = (rec.get("trade") or rec.get("scope") or "").strip() or "general contracting"
        prompt = (
            "You are helping fill a workplace-safety program document for a "
            "subcontractor. Return ONLY a compact JSON object (no prose, no code "
            "fences) with any of these string keys you can reasonably infer:\n"
            '  "SCOPE"       - one plain-English sentence describing the work '
            f"{company} performs, based on their trade: \"{trade}\".\n"
            '  "ADMIN_TITLE" - the job title of whoever administers safety at a '
            "company like this (e.g. \"Safety Manager\", \"Owner\").\n"
            "Do not invent a person's name, address, or date. Keep values short."
        )
        msgs = [{"role": "user", "content": prompt}]
        raw = (provider.complete(msgs, []).text or "").strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(raw[start:end + 1])
            valid = {t for t, _ in _LIB_DOC_FIELDS}
            if need_scope and isinstance(data.get("SCOPE"), str) and data["SCOPE"].strip():
                fields["SCOPE"] = data["SCOPE"].strip()
            if need_title and isinstance(data.get("ADMIN_TITLE"), str) and data["ADMIN_TITLE"].strip():
                fields["ADMIN_TITLE"] = data["ADMIN_TITLE"].strip()
            fields = {k: v for k, v in fields.items() if k in valid and v}
    except Exception:
        pass
    return fields


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


_PBKDF2_ITERS = 200_000


def hash_pin(slug: str, pin: str) -> str:
    """Hash a PIN with PBKDF2-HMAC-SHA256 (slow by design) plus a per-hash random
    salt. Stored as 'pbkdf2$<iters>$<salt_hex>$<hash_hex>'.

    NOTE: we intentionally do NOT mix the deployment SECRET into the PIN hash.
    The random per-hash salt already makes these hashes unique and un-precomputable.
    Peppering with SECRET created a catastrophic failure mode: if SECRET ever
    changed (env var added/removed, volume hiccup, redeploy), every previously
    set PIN silently stopped verifying and users were locked out with a generic
    "wrong email or PIN". verify_pin below still accepts the old peppered hashes
    for backwards compatibility."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256",
                             f"{slug}:{pin}".encode(),
                             salt, _PBKDF2_ITERS)
    return f"pbkdf2${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_pin(slug: str, pin: str, stored: str) -> bool:
    """Constant-time PIN check. Accepts, in order:
      - new PBKDF2 hashes (no pepper)         f"{slug}:{pin}"
      - legacy PBKDF2 hashes (SECRET pepper)  f"{SECRET}:{slug}:{pin}"
      - legacy plain-SHA256 (either form)
    so accounts created under any prior scheme keep working."""
    if not stored:
        return False
    # Try the un-peppered form first (current), then the SECRET-peppered form
    # (legacy) so old accounts still log in while SECRET stays stable.
    candidates = (f"{slug}:{pin}", f"{SECRET}:{slug}:{pin}")
    if stored.startswith("pbkdf2$"):
        try:
            _, iters, salt_hex, hash_hex = stored.split("$")
            salt = bytes.fromhex(salt_hex)
            iters = int(iters)
        except Exception:
            return False
        for cand in candidates:
            try:
                dk = hashlib.pbkdf2_hmac("sha256", cand.encode(), salt, iters)
                if hmac.compare_digest(dk.hex(), hash_hex):
                    return True
            except Exception:
                continue
        return False
    # legacy: plain SHA256 hex
    for cand in candidates:
        legacy = hashlib.sha256(cand.encode()).hexdigest()
        if hmac.compare_digest(legacy, stored):
            return True
    return False


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


def _session(role: str, slug: str = "", member: str = "") -> str:
    payload = {"role": role, "slug": slug, "exp": time.time() + SESSION_TTL}
    if member:
        payload["member"] = member
    return _sign(payload)


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
        # Which GC (general contractor) this subcontractor belongs to. Empty means
        # the record is owned directly by Origin/OMS (the owner) and not yet placed
        # under a GC. Existing records predating the GC tier read as "" and keep
        # working exactly as before.
        "gc_slug": "",
        "logo": "",          # filename of an uploaded logo in this record's docs/
        "messages": [],      # two-way thread between this sub and its GC (+owner)
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


# ─────────────────────────── GC (general contractor) tier ───────────────────
# The product has three levels:
#     owner (Origin/OMS, the admin) → GC → the GC's subcontractors (the clients).
# A GC is a light record stored the same flat-JSON way as clients, under
# PORTAL_DIR/gcs/<slug>/gc.json. Each subcontractor's client.json carries a
# "gc_slug" pointing at its GC. GCs log in (email + PIN, exactly like clients)
# and manage ONLY their own subcontractor list; the owner can see every GC and
# act on any GC's behalf. This layers cleanly on top of the existing client
# storage without disturbing it — a client with gc_slug "" simply belongs to no
# GC and behaves as it always did.

GCS_DIR = PORTAL_DIR / "gcs"


def _gc_dir(slug: str) -> Path:
    return GCS_DIR / slug


def _gc_file(slug: str) -> Path:
    return _gc_dir(slug) / "gc.json"


def _blank_gc(name: str, email: str = "") -> Dict[str, Any]:
    slug = slugify(name)
    return {
        "slug": slug,
        "name": name,
        "email": email,
        "pin_hash": "",
        "logo": "",          # filename of an uploaded logo in this GC's dir
        "brand_primary": "#1E7A46",
        "messages": [],      # two-way thread between this GC and the owner
        "created": _now(),
        "updated": _now(),
    }


def load_gc(slug: str) -> Optional[Dict[str, Any]]:
    f = _gc_file(slug)
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_gc(data: Dict[str, Any]) -> Dict[str, Any]:
    slug = data["slug"]
    _gc_dir(slug).mkdir(parents=True, exist_ok=True)
    data["updated"] = _now()
    _gc_file(slug).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def list_gcs() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not GCS_DIR.is_dir():
        return out
    for d in sorted(GCS_DIR.iterdir()):
        rec = load_gc(d.name)
        if not rec:
            continue
        subs = clients_for_gc(rec.get("slug", ""))
        out.append({
            "slug": rec.get("slug"),
            "name": rec.get("name"),
            "email": rec.get("email"),
            "logo": rec.get("logo", ""),
            "sub_count": len(subs),
            "unread": sum(1 for m in rec.get("messages", [])
                          if m.get("sender") == "gc" and not m.get("read_owner")),
            "updated": rec.get("updated"),
        })
    return out


def find_gc_by_email(email: str) -> Optional[Dict[str, Any]]:
    email = (email or "").strip().lower()
    if not email or not GCS_DIR.is_dir():
        return None
    matches: List[Dict[str, Any]] = []
    for d in sorted(GCS_DIR.iterdir()):
        rec = load_gc(d.name)
        if rec and (rec.get("email", "").strip().lower() == email):
            matches.append(rec)
    if not matches:
        return None
    matches.sort(key=lambda r: r.get("updated", ""), reverse=True)
    return matches[0]


# ─────────────────────── GC members (multi-user) ───────────────────────
# A GC used to be a single login (one email + one PIN on the GC record). To let
# a GC split its subcontractors across several people in its office, each GC now
# carries a "members" list: the owner (the person you set up) plus any admins the
# owner invites. Each member has their own email + PIN and their own "subs"
# assignment — "all" for the owner, or a list of sub slugs for an admin who
# should only see the accounts assigned to them. This is backward compatible:
# older GC records with just a top-level email/pin are treated as a single owner
# member, materialized on first touch.

def _ensure_gc_members(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the GC's members list, synthesizing an owner member from the
    legacy top-level email/pin_hash if the record predates the members list.
    Mutates rec in place (caller decides whether to persist)."""
    members = rec.get("members")
    if isinstance(members, list) and members:
        return members
    rec["members"] = [{
        "id": "owner",
        "name": rec.get("name", "") or "Owner",
        "email": rec.get("email", "") or "",
        "pin_hash": rec.get("pin_hash", "") or "",
        "role": "owner",
        "subs": "all",          # the owner always sees every sub
        "created": rec.get("created", _now()),
        "updated": rec.get("updated", _now()),
    }]
    return rec["members"]


def _gc_owner_member(rec: Dict[str, Any]) -> Dict[str, Any]:
    for m in _ensure_gc_members(rec):
        if m.get("role") == "owner":
            return m
    return _ensure_gc_members(rec)[0]


def _sync_owner_to_top(rec: Dict[str, Any]) -> None:
    """Keep the legacy top-level email/pin_hash mirrored to the owner member so
    old code paths (find_gc_by_email, existing sessions) keep working."""
    owner = _gc_owner_member(rec)
    rec["email"] = owner.get("email", "")
    rec["pin_hash"] = owner.get("pin_hash", "")


def find_gc_member_by_email(email: str):
    """Find the (gc_rec, member) whose member email matches, across every GC.
    Returns (None, None) if no member has that email."""
    email = (email or "").strip().lower()
    if not email or not GCS_DIR.is_dir():
        return None, None
    hits = []
    for d in sorted(GCS_DIR.iterdir()):
        rec = load_gc(d.name)
        if not rec:
            continue
        for m in _ensure_gc_members(rec):
            if (m.get("email", "") or "").strip().lower() == email:
                hits.append((rec, m))
    if not hits:
        return None, None
    hits.sort(key=lambda pair: pair[0].get("updated", ""), reverse=True)
    return hits[0]


def _member_subs(member: Optional[Dict[str, Any]]):
    """The set of sub slugs a member may see, or None meaning 'all'.
    None (no member) = owner acting via ?gc=, who sees everything."""
    if member is None:
        return None
    if member.get("role") == "owner":
        return None
    subs = member.get("subs")
    if subs == "all" or subs is None:
        return None
    return set(subs)


def _coi_summary(coi: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Roll a list of COI lines up into a single monitoring status.
    'expired' if any line is past due, 'expiring' if any is within 30 days,
    else 'current'. Also returns the soonest expiry date it saw."""
    from datetime import date, datetime
    today = date.today()
    worst = "current"   # current < expiring < expired
    rank = {"current": 0, "expiring": 1, "expired": 2}
    soonest = ""
    for line in coi or []:
        raw = (line.get("expires") or "").strip()
        if not raw:
            continue
        try:
            exp = datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if not soonest or raw[:10] < soonest:
            soonest = raw[:10]
        days = (exp - today).days
        state = "expired" if days < 0 else ("expiring" if days <= 30 else "current")
        if rank[state] > rank[worst]:
            worst = state
    if not (coi or []):
        worst = "none"
    return {"status": worst, "soonest": soonest}


def _monitoring_flags(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the at-a-glance state for one sub: whether any prequal grade is
    failing/needs action, whether COI is expiring/expired, and whether any
    required document is still missing (a build target with no file yet)."""
    platforms = rec.get("platforms", {}) or {}
    bad_grade = False
    for p in platforms.values():
        grade = (str(p.get("grade") or "")).strip().upper()
        status = (str(p.get("status") or "")).strip().lower()
        if grade in ("F", "D") or "action" in status or "fail" in status \
                or "expired" in status or "not" in status:
            bad_grade = True
    coi = _coi_summary(rec.get("coi", []))
    missing_docs = [d for d in rec.get("documents", [])
                    if d.get("to_build") and not d.get("file")]
    action = bad_grade or coi["status"] in ("expired", "expiring") or bool(missing_docs)
    return {
        "action_required": action,
        "coi_status": coi["status"],
        "coi_soonest": coi["soonest"],
        "missing_count": len(missing_docs),
    }


def clients_for_gc(gc_slug: str) -> List[Dict[str, Any]]:
    """Full monitoring rows for every subcontractor placed under this GC.
    Carries the prequal grades, COI, and documents the GC dashboard renders."""
    gc_slug = (gc_slug or "").strip()
    out: List[Dict[str, Any]] = []
    if not gc_slug or not CLIENTS_DIR.is_dir():
        return out
    for d in sorted(CLIENTS_DIR.iterdir()):
        rec = load_client(d.name)
        if not rec or (rec.get("gc_slug", "") or "") != gc_slug:
            continue
        flags = _monitoring_flags(rec)
        out.append({
            "slug": rec.get("slug"),
            "company": rec.get("company"),
            "email": rec.get("email"),
            "logo": rec.get("logo", ""),
            "client_type": rec.get("client_type", "prequal"),
            "has_login": bool(rec.get("pin_hash")),
            "open_requests": sum(1 for r in rec.get("requests", []) if r.get("status") == "new"),
            "unread": sum(1 for m in rec.get("messages", [])
                          if m.get("sender") == "sub" and not m.get("read_gc")),
            "updated": rec.get("updated"),
            # ---- monitoring payload (drives the contractor-monitoring board) ----
            "scope": rec.get("scope", ""),
            "trade": rec.get("trade", ""),
            "platforms": rec.get("platforms", {}),
            "coi": rec.get("coi", []),
            "documents": rec.get("documents", []),
            "trir": rec.get("trir", ""),
            "emr": rec.get("emr", ""),
            "action_required": flags["action_required"],
            "coi_status": flags["coi_status"],
            "coi_soonest": flags["coi_soonest"],
            "missing_count": flags["missing_count"],
        })
    return out


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


def _base_url(request) -> str:
    try:
        return str(request.base_url).rstrip("/")
    except Exception:
        return "https://origin-production-1352.up.railway.app"


def _gc_login_url(request) -> str:
    return f"{_base_url(request)}/gc"


def _portal_login_url(request) -> str:
    return f"{_base_url(request)}/portal"


def _send_gc_login_email(request, gc_rec: Dict[str, Any], temp_pin: str):
    """Email a GC their /gc login link + a temporary PIN. Returns (sent, error)."""
    to = (gc_rec.get("email") or "").strip()
    if "@" not in to:
        return False, "No email on file."
    try:
        from .compliance import send_email, resend_configured, smtp_configured
    except Exception as exc:  # pragma: no cover
        return False, f"mailer unavailable: {exc}"
    if not (resend_configured() or smtp_configured()):
        return False, ("Email isn't turned on yet (no RESEND_API_KEY). The PIN was "
                       "reset — give the GC their login link and PIN yourself.")
    name = (gc_rec.get("name") or "").strip()
    res = send_email(
        to=to,
        subject="Your contractor portal login — Origin Management Solutions",
        body=(
            f"Hi{(' ' + name) if name else ''},\n\n"
            f"Here's your login for the contractor portal, where you manage your "
            f"subcontractors and their prequal documents.\n\n"
            f"Sign in here: {_gc_login_url(request)}\n"
            f"Email: {to}\n"
            f"PIN: {temp_pin}\n\n"
            f"Keep this PIN private. You can request a new one anytime from the login "
            f"page using \u201cForgot PIN\u201d.\n\n"
            f"\u2014 Origin Management Solutions\n"
            f"info@originmanagementsolutions.com"
        ),
    )
    return bool(res.get("sent")), res.get("error", "")


def _send_sub_login_email(request, client_rec: Dict[str, Any], temp_pin: str):
    """Email a subcontractor/client their /portal login link + a temporary PIN."""
    to = (client_rec.get("email") or "").strip()
    if "@" not in to:
        return False, "No email on file."
    try:
        from .compliance import send_email, resend_configured, smtp_configured
    except Exception as exc:  # pragma: no cover
        return False, f"mailer unavailable: {exc}"
    if not (resend_configured() or smtp_configured()):
        return False, ("Email isn't turned on yet (no RESEND_API_KEY). The PIN was "
                       "reset — give them their login link and PIN yourself.")
    company = (client_rec.get("company") or "").strip()
    res = send_email(
        to=to,
        subject="Your compliance portal login — Origin Management Solutions",
        body=(
            f"Hi,\n\n"
            f"Here's your login for the {company} compliance portal.\n\n"
            f"Sign in here: {_portal_login_url(request)}\n"
            f"Email: {to}\n"
            f"PIN: {temp_pin}\n\n"
            f"Keep this PIN private. You can request a new one anytime from the login "
            f"page using \u201cForgot PIN\u201d.\n\n"
            f"\u2014 Origin Management Solutions\n"
            f"info@originmanagementsolutions.com"
        ),
    )
    return bool(res.get("sent")), res.get("error", "")


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

    def gc_session(request: Request) -> Optional[Dict[str, Any]]:
        p = _unsign(request.cookies.get(GC_COOKIE, ""))
        return p if p and p.get("role") == "gc" else None

    def acting_gc_slug(request: Request) -> Optional[str]:
        """The GC a request is scoped to. A GC login is scoped to its own slug.
        The owner (admin) may act for any GC by passing ?gc=<slug>. Returns the
        slug if the caller is allowed to act for it, else None."""
        gs = gc_session(request)
        if gs:
            return gs.get("slug") or None
        if admin_session(request):
            want = (request.query_params.get("gc") or "").strip()
            return want or None
        return None

    def acting_gc_member(request: Request) -> Optional[Dict[str, Any]]:
        """The specific GC member (owner or invited admin) behind this request.
        Returns None when the site owner is acting via ?gc= (they see everything),
        so callers treat None as 'full access'."""
        gs = gc_session(request)
        if not gs or not gs.get("slug"):
            return None
        rec = load_gc(gs["slug"])
        if not rec:
            return None
        want = gs.get("member") or "owner"
        for m in _ensure_gc_members(rec):
            if m.get("id") == want:
                return m
        # Session references a member that no longer exists (removed): deny by
        # returning a locked-down phantom admin with no subs.
        return {"id": want, "role": "admin", "subs": []}

    def _member_can_see(request: Request, sub_slug: str) -> bool:
        allowed = _member_subs(acting_gc_member(request))
        return allowed is None or sub_slug in allowed

    def _is_gc_owner(request: Request) -> bool:
        """True if the caller may manage members/assignments: the site owner
        acting via ?gc=, or a GC member whose role is owner."""
        if admin_session(request) and not gc_session(request):
            return True
        m = acting_gc_member(request)
        return bool(m and m.get("role") == "owner")

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

    @app.get("/gc", response_class=HTMLResponse)
    def gc_page():
        f = webui / "gc.html"
        html = f.read_text(encoding="utf-8") if f.is_file() else "<h1>GC Console</h1><p>page missing</p>"
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

    @app.post("/portal/api/forgot")
    def portal_forgot(request: Request, body: dict = Body(...)):
        """Self-serve PIN reset for a subcontractor/client: if the email matches an
        account, set a fresh temp PIN and email it. Always returns a generic success
        so we never reveal which emails exist."""
        email = (body.get("email") or "").strip()
        generic = {"ok": True,
                   "message": "If that email is on file, we just sent a new PIN to it."}
        if "@" not in email:
            return JSONResponse(generic)
        rec = find_by_email(email)
        if rec:
            temp_pin = f"{random.randint(0, 999999):06d}"
            rec["pin_hash"] = hash_pin(rec["slug"], temp_pin)
            save_client(rec)
            _send_sub_login_email(request, rec, temp_pin)
        return JSONResponse(generic)

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
        out = _public_view(rec)
        # Surface the sub's GC (if any) so the portal can show branding + a
        # messages tab. Unread = messages the GC sent that the sub hasn't read.
        gc_slug = (rec.get("gc_slug", "") or "").strip()
        gc = load_gc(gc_slug) if gc_slug else None
        if gc:
            out["gc"] = {
                "slug": gc.get("slug"),
                "name": gc.get("name"),
                "has_logo": bool(gc.get("logo")),
                "brand_primary": gc.get("brand_primary", ""),
                "unread": sum(1 for m in rec.get("messages", [])
                              if m.get("sender") == "gc" and not m.get("read_sub")),
            }
        else:
            out["gc"] = None
        return out

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
        for key in ("email", "client_type", "plan", "scope", "trade", "gc_slug"):
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

    # ===================== GC TIER (owner + GC console) =====================
    # Owner (admin) manages the roster of GCs and can act for any of them.
    # A GC logs in with email+PIN and manages ONLY its own subcontractors.

    def _sub_view(rec: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(rec)
        out["pin_set"] = bool(rec.get("pin_hash"))
        out.pop("pin_hash", None)
        return out

    def _logo_ext(filename: str) -> str:
        ext = os.path.splitext(filename or "")[1].lower()
        return ext if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg") else ".png"

    # ---- owner: list / create / edit / delete GCs ----
    @app.get("/portal/api/admin/gcs")
    def admin_gcs(request: Request):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        return {"gcs": list_gcs()}

    @app.post("/portal/api/admin/gc")
    def admin_save_gc(request: Request, body: dict = Body(...)):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "GC name required"}, status_code=400)
        incoming_slug = (body.get("slug") or "").strip()
        slug = incoming_slug or slugify(name)
        existing = load_gc(slug)
        is_new = existing is None
        rec = existing or _blank_gc(name, body.get("email", ""))
        rec["slug"] = slug
        rec["name"] = name
        owner = _gc_owner_member(rec)  # materialize members; keeps owner in sync
        for key in ("email", "brand_primary"):
            if key in body:
                rec[key] = body[key]
        owner["name"] = name
        if "email" in body:
            owner["email"] = rec["email"]
        pin = (body.get("pin") or "").strip()
        if pin:
            rec["pin_hash"] = hash_pin(slug, pin)
            owner["pin_hash"] = rec["pin_hash"]
        # A brand-new GC with an email gets an automatic login email — the same
        # way accepting a client emails them their portal. If Chris didn't set a
        # PIN, mint a temporary one so the email always carries working creds.
        email = (rec.get("email") or "").strip()
        invited = False
        invite_err = ""
        temp_pin = ""
        if is_new and "@" in email:
            temp_pin = pin or f"{random.randint(0, 999999):06d}"
            rec["pin_hash"] = hash_pin(slug, temp_pin)
            owner["pin_hash"] = rec["pin_hash"]
            save_gc(rec)
            invited, invite_err = _send_gc_login_email(request, rec, temp_pin)
        else:
            save_gc(rec)
        return {"ok": True, "slug": slug, "is_new": is_new,
                "invited": invited, "invite_error": invite_err,
                "temp_pin": temp_pin, "gc_url": _gc_login_url(request)}

    @app.get("/portal/api/admin/gc/{slug}")
    def admin_get_gc(slug: str, request: Request):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_gc(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        out = {k: v for k, v in rec.items() if k != "pin_hash"}
        out["pin_set"] = bool(rec.get("pin_hash"))
        out["subs"] = clients_for_gc(slug)
        return out

    @app.post("/portal/api/admin/gc/{slug}/delete")
    def admin_delete_gc(slug: str, request: Request):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        # Never delete the subcontractors — just unassign them back to the owner.
        for c in clients_for_gc(slug):
            rec = load_client(c["slug"])
            if rec:
                rec["gc_slug"] = ""
                save_client(rec)
        import shutil
        d = _gc_dir(slug)
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
        return {"ok": True}

    @app.post("/portal/api/admin/gc/{slug}/invite")
    def admin_invite_gc(slug: str, request: Request):
        """Owner action: set a fresh temporary PIN for this GC and email them their
        /gc login link. If email isn't configured, we still reset the PIN and return
        it so the owner can pass it along by hand."""
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_gc(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        to = (rec.get("email") or "").strip()
        if "@" not in to:
            return JSONResponse(
                {"error": "This GC has no login email on file. Add one first."},
                status_code=400)
        temp_pin = f"{random.randint(0, 999999):06d}"
        rec["pin_hash"] = hash_pin(slug, temp_pin)
        _gc_owner_member(rec)["pin_hash"] = rec["pin_hash"]  # keep owner in sync
        save_gc(rec)
        sent, err = _send_gc_login_email(request, rec, temp_pin)
        return {"ok": True, "slug": slug, "to": to, "temp_pin": temp_pin,
                "sent": sent, "email_error": err,
                "gc_url": _gc_login_url(request)}

    # ---- GC login / session ----
    @app.post("/portal/api/gc/login")
    def gc_login(request: Request, body: dict = Body(...)):
        email = (body.get("email") or "").strip()
        pin = (body.get("pin") or "").strip()
        key = "gc:" + email.lower()
        if _login_locked(key):
            return JSONResponse(
                {"error": "Too many attempts. Please wait a few minutes and try again."},
                status_code=429)
        rec, member = find_gc_member_by_email(email)
        if (not rec or not member or not member.get("pin_hash")
                or not verify_pin(rec["slug"], pin, member["pin_hash"])):
            _login_note_fail(key)
            return JSONResponse({"error": "Wrong email or PIN."}, status_code=401)
        _login_clear(key)
        resp = JSONResponse({"ok": True, "name": rec.get("name"),
                             "member": member.get("name"),
                             "role": member.get("role", "admin")})
        resp.set_cookie(GC_COOKIE, _session("gc", rec["slug"], member.get("id", "owner")),
                        httponly=True, samesite="lax", max_age=SESSION_TTL,
                        secure=_secure(request))
        return resp

    @app.post("/portal/api/gc/logout")
    def gc_logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(GC_COOKIE)
        return resp

    @app.post("/portal/api/gc/forgot")
    def gc_forgot(request: Request, body: dict = Body(...)):
        """Self-serve PIN reset for a GC: if the email matches a GC account, set a
        fresh temp PIN and email it. Always returns a generic success so we never
        reveal which emails exist."""
        email = (body.get("email") or "").strip()
        generic = {"ok": True,
                   "message": "If that email is on file, we just sent a new PIN to it."}
        if "@" not in email:
            return JSONResponse(generic)
        rec, member = find_gc_member_by_email(email)
        if rec and member:
            temp_pin = f"{random.randint(0, 999999):06d}"
            member["pin_hash"] = hash_pin(rec["slug"], temp_pin)
            member["updated"] = _now()
            if member.get("role") == "owner":
                rec["pin_hash"] = member["pin_hash"]  # keep legacy mirror
            save_gc(rec)
            # Email the member who asked (not necessarily the owner's address).
            _send_gc_login_email(request, {**rec, "email": member.get("email", "")}, temp_pin)
        return JSONResponse(generic)

    @app.post("/portal/api/gc/change-pin")
    def gc_change_pin(request: Request, body: dict = Body(...)):
        """A logged-in GC member changes their OWN PIN: enter the current PIN and
        pick a new one. Requires a real GC session (not the site owner acting via
        ?gc=). Lets a GC replace the temporary PIN we emailed them."""
        gs = gc_session(request)
        if not gs or not gs.get("slug"):
            return JSONResponse({"error": "Sign in to change your PIN."}, status_code=401)
        current = (body.get("current_pin") or "").strip()
        new_pin = (body.get("new_pin") or "").strip()
        if len(new_pin) < 4:
            return JSONResponse({"error": "Your new PIN must be at least 4 digits."},
                                status_code=400)
        rec = load_gc(gs["slug"])
        if not rec:
            return JSONResponse({"error": "account not found"}, status_code=404)
        member = None
        want = gs.get("member") or "owner"
        for m in _ensure_gc_members(rec):
            if m.get("id") == want:
                member = m
                break
        if not member:
            return JSONResponse({"error": "account not found"}, status_code=404)
        if not member.get("pin_hash") or not verify_pin(rec["slug"], current, member["pin_hash"]):
            return JSONResponse({"error": "That current PIN isn't right."}, status_code=403)
        member["pin_hash"] = hash_pin(rec["slug"], new_pin)
        member["updated"] = _now()
        if member.get("role") == "owner":
            rec["pin_hash"] = member["pin_hash"]  # keep legacy mirror
        save_gc(rec)
        return {"ok": True}

    # ---- GC team members (owner invites admins, splits subs among them) ----
    def _member_public(m: Dict[str, Any], all_slugs: set) -> Dict[str, Any]:
        subs = m.get("subs")
        assigned = "all" if (m.get("role") == "owner" or subs == "all" or subs is None) \
            else [s for s in subs if s in all_slugs]
        return {"id": m.get("id"), "name": m.get("name"), "email": m.get("email"),
                "role": m.get("role", "admin"), "subs": assigned,
                "has_login": bool(m.get("pin_hash")), "created": m.get("created")}

    @app.get("/portal/api/gc/members")
    def gc_members(request: Request):
        """List this GC's team members and each one's sub assignment (owner only)."""
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        if not _is_gc_owner(request):
            return JSONResponse({"error": "Only the account owner can manage the team."},
                                status_code=403)
        rec = load_gc(slug)
        if not rec:
            return JSONResponse({"error": "GC not found"}, status_code=404)
        all_slugs = {s["slug"] for s in clients_for_gc(slug)}
        members = [_member_public(m, all_slugs) for m in _ensure_gc_members(rec)]
        return {"members": members, "subs": [{"slug": s["slug"], "company": s["company"]}
                                             for s in clients_for_gc(slug)]}

    @app.post("/portal/api/gc/members")
    def gc_add_member(request: Request, body: dict = Body(...)):
        """Owner invites a teammate: creates an admin member with a temp PIN and
        emails them the /gc login. body: {name, email, subs?: "all"|[slug,...]}."""
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        if not _is_gc_owner(request):
            return JSONResponse({"error": "Only the account owner can add teammates."},
                                status_code=403)
        rec = load_gc(slug)
        if not rec:
            return JSONResponse({"error": "GC not found"}, status_code=404)
        name = (body.get("name") or "").strip()
        email = (body.get("email") or "").strip()
        if not name or "@" not in email:
            return JSONResponse({"error": "A name and a valid email are required."},
                                status_code=400)
        # No two members (across any GC) can share an email — it's the login key.
        other_rec, other_m = find_gc_member_by_email(email)
        if other_m:
            return JSONResponse(
                {"error": "That email is already used by another login."}, status_code=409)
        members = _ensure_gc_members(rec)
        valid_slugs = {s["slug"] for s in clients_for_gc(slug)}
        raw = body.get("subs")
        if raw == "all":
            subs: Any = "all"
        elif isinstance(raw, list):
            subs = [s for s in raw if s in valid_slugs]
        else:
            subs = []  # start with nothing assigned; owner assigns next
        temp_pin = f"{random.randint(0, 999999):06d}"
        member = {
            "id": "m_" + secrets.token_hex(4),
            "name": name,
            "email": email,
            "pin_hash": hash_pin(rec["slug"], temp_pin),
            "role": "admin",
            "subs": subs,
            "created": _now(),
            "updated": _now(),
        }
        members.append(member)
        save_gc(rec)
        sent, err = _send_gc_login_email(
            request, {**rec, "email": email, "name": name}, temp_pin)
        return {"ok": True, "id": member["id"], "temp_pin": temp_pin,
                "sent": sent, "email_error": err, "gc_url": _gc_login_url(request)}

    @app.post("/portal/api/gc/members/{mid}/subs")
    def gc_member_subs(mid: str, request: Request, body: dict = Body(...)):
        """Owner sets which subs a teammate can see. body: {subs: "all"|[slug,...]}."""
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        if not _is_gc_owner(request):
            return JSONResponse({"error": "Only the account owner can change assignments."},
                                status_code=403)
        rec = load_gc(slug)
        if not rec:
            return JSONResponse({"error": "GC not found"}, status_code=404)
        member = next((m for m in _ensure_gc_members(rec) if m.get("id") == mid), None)
        if not member:
            return JSONResponse({"error": "teammate not found"}, status_code=404)
        if member.get("role") == "owner":
            return JSONResponse({"error": "The owner always sees every sub."},
                                status_code=400)
        valid_slugs = {s["slug"] for s in clients_for_gc(slug)}
        raw = body.get("subs")
        if raw == "all":
            member["subs"] = "all"
        elif isinstance(raw, list):
            member["subs"] = [s for s in raw if s in valid_slugs]
        else:
            return JSONResponse({"error": "subs must be \"all\" or a list of slugs."},
                                status_code=400)
        member["updated"] = _now()
        save_gc(rec)
        return {"ok": True, "subs": member["subs"]}

    @app.post("/portal/api/gc/members/{mid}/remove")
    def gc_remove_member(mid: str, request: Request):
        """Owner removes a teammate. The owner member itself cannot be removed."""
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        if not _is_gc_owner(request):
            return JSONResponse({"error": "Only the account owner can remove teammates."},
                                status_code=403)
        rec = load_gc(slug)
        if not rec:
            return JSONResponse({"error": "GC not found"}, status_code=404)
        members = _ensure_gc_members(rec)
        member = next((m for m in members if m.get("id") == mid), None)
        if not member:
            return JSONResponse({"error": "teammate not found"}, status_code=404)
        if member.get("role") == "owner":
            return JSONResponse({"error": "You can't remove the account owner."},
                                status_code=400)
        rec["members"] = [m for m in members if m.get("id") != mid]
        save_gc(rec)
        return {"ok": True}

    @app.post("/portal/api/gc/members/{mid}/resend")
    def gc_resend_member(mid: str, request: Request):
        """Owner resets a teammate's PIN and re-emails their login."""
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        if not _is_gc_owner(request):
            return JSONResponse({"error": "Only the account owner can do this."},
                                status_code=403)
        rec = load_gc(slug)
        if not rec:
            return JSONResponse({"error": "GC not found"}, status_code=404)
        member = next((m for m in _ensure_gc_members(rec) if m.get("id") == mid), None)
        if not member:
            return JSONResponse({"error": "teammate not found"}, status_code=404)
        temp_pin = f"{random.randint(0, 999999):06d}"
        member["pin_hash"] = hash_pin(rec["slug"], temp_pin)
        member["updated"] = _now()
        if member.get("role") == "owner":
            rec["pin_hash"] = member["pin_hash"]
        save_gc(rec)
        sent, err = _send_gc_login_email(
            request, {**rec, "email": member.get("email", ""), "name": member.get("name", "")},
            temp_pin)
        return {"ok": True, "temp_pin": temp_pin, "sent": sent, "email_error": err}

    @app.get("/portal/api/gc/home")
    def gc_home(request: Request):
        """A GC's own view: its brand, its subcontractor roster, and its thread
        with the owner. Also serves the owner when acting for a GC via ?gc=."""
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_gc(slug)
        if not rec:
            return JSONResponse({"error": "GC not found"}, status_code=404)
        subs = clients_for_gc(slug)
        # An invited admin only sees the subcontractors assigned to them; the
        # owner (and the site owner acting via ?gc=) sees the whole roster.
        allowed = _member_subs(acting_gc_member(request))
        if allowed is not None:
            subs = [s for s in subs if s.get("slug") in allowed]
        member = acting_gc_member(request)
        gc_out = {k: v for k, v in rec.items() if k not in ("pin_hash", "members")}
        return {
            "gc": gc_out,
            "subs": subs,
            "is_owner": _is_gc_owner(request),
            "member": ({"id": member.get("id"), "name": member.get("name"),
                        "role": member.get("role", "admin")} if member else
                       {"id": "owner", "name": rec.get("name"), "role": "owner"}),
        }

    # ---- GC manages its own subcontractors (owner may act via ?gc=) ----
    @app.get("/portal/api/gc/sub/{sub_slug}")
    def gc_get_sub(sub_slug: str, request: Request):
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sub_slug)
        if not rec or (rec.get("gc_slug", "") or "") != slug:
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _member_can_see(request, sub_slug):
            return JSONResponse({"error": "not found"}, status_code=404)
        if _sync_docs(rec):
            save_client(rec)
        return _sub_view(rec)

    @app.post("/portal/api/gc/sub")
    def gc_save_sub(request: Request, body: dict = Body(...)):
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        company = (body.get("company") or "").strip()
        if not company:
            return JSONResponse({"error": "company name required"}, status_code=400)
        incoming_slug = (body.get("slug") or "").strip()
        email = (body.get("email") or "").strip()
        if not incoming_slug and email:
            existing = find_by_email(email)
            if existing:
                incoming_slug = existing["slug"]
        sub_slug = incoming_slug or slugify(company)
        rec = load_client(sub_slug)
        if rec is None:
            rec = _blank_client(company, email, body.get("client_type", "prequal"))
        elif (rec.get("gc_slug", "") or "") not in ("", slug):
            # belongs to a different GC — a GC may not poach another's sub
            return JSONResponse({"error": "That company is managed by another GC."},
                                status_code=403)
        rec["slug"] = sub_slug
        rec["company"] = company
        rec["gc_slug"] = slug
        for key in ("email", "client_type", "plan", "scope", "trade"):
            if key in body:
                rec[key] = body[key]
        for key in ("platforms", "coi", "documents", "available"):
            if key in body and body[key] is not None:
                rec[key] = body[key]
        pin = (body.get("pin") or "").strip()
        if pin:
            rec["pin_hash"] = hash_pin(sub_slug, pin)
        _ensure_project(rec)
        save_client(rec)
        return {"ok": True, "slug": sub_slug}

    @app.post("/portal/api/gc/sub/{sub_slug}/request-doc")
    def gc_request_doc(sub_slug: str, request: Request, body: dict = Body(...)):
        """'Fill from library' — the GC asks Origin to prepare a document for one
        of its subs. Logs a real request on the sub (shows in the admin Requests
        tab) and emails Chris, so the button does actual work."""
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sub_slug)
        if not rec or (rec.get("gc_slug", "") or "") != slug:
            return JSONResponse({"error": "not found"}, status_code=404)
        program = (body.get("program") or "a document").strip()
        gc = load_gc(slug)
        gc_name = (gc.get("name") if gc else "") or slug
        rec.setdefault("requests", []).append({
            "program": program, "note": f"Requested by GC: {gc_name}",
            "ts": _now(), "status": "new", "price": "", "source": "gc",
        })
        save_client(rec)
        try:
            from .compliance import send_email
            send_email(
                to=os.environ.get("ORIGIN_MAIL_FROM", "info@originmanagementsolutions.com"),
                subject=f"GC document request — {rec.get('company')}",
                body=(f"{gc_name} requested '{program}' for their subcontractor "
                      f"{rec.get('company')}.\n\nOpen the admin console to prepare it."),
            )
        except Exception as exc:  # pragma: no cover
            print(f"[portal] gc doc-request email skipped: {exc}")
        return {"ok": True}

    def _gc_owned_sub(request: Request, sub_slug: str):
        """Resolve the acting GC (a real GC session, or the owner acting via
        ?gc=) and load ONE of its subcontractors. Returns (rec, slug, error).
        error is a JSONResponse if the caller isn't a GC or the sub isn't
        theirs — the exact ownership guard used across all GC sub endpoints."""
        slug = acting_gc_slug(request)
        if not slug:
            return None, None, JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sub_slug)
        if not rec or (rec.get("gc_slug", "") or "") != slug:
            return None, slug, JSONResponse({"error": "not found"}, status_code=404)
        # An invited admin can only act on the subs assigned to them.
        if not _member_can_see(request, sub_slug):
            return None, slug, JSONResponse({"error": "not found"}, status_code=404)
        return rec, slug, None

    @app.post("/portal/api/gc/sub/{sub_slug}/gap")
    def gc_gap(sub_slug: str, request: Request, body: dict = Body(...)):
        """GC Gap Finder — run the Origin gap analysis against one of the GC's
        subcontractors, using the docs on file plus the sub's scope of work, and
        store the report on that sub's record. Mirror of admin_gap, but scoped to
        the acting GC (a GC session, or the owner acting via ?gc=)."""
        rec, slug, err = _gc_owned_sub(request, sub_slug)
        if err:
            return err
        try:
            from . import gaps as _gaps
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"error": f"gap engine unavailable: {exc}"}, status_code=500)

        industry = (body.get("industry") or rec.get("trade") or rec.get("scope") or "").strip()
        if not industry:
            return JSONResponse(
                {"error": "Add a scope of work (or trade) for this subcontractor first — "
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

        docs = []
        docs_dir = _client_dir(sub_slug) / "docs"
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

        rec["gap_report"] = report
        rec["gap_run_at"] = _now()
        if body.get("industry"):
            rec["trade"] = industry
        save_client(rec)
        return {"ok": True, "report": report}

    @app.post("/portal/api/gc/sub/{sub_slug}/draft")
    def gc_draft(sub_slug: str, request: Request, body: dict = Body(...)):
        """Build the missing/failing written programs the GC Gap Finder found for
        one of the GC's subs and, when publish is set, drop the finished .docx
        straight into that sub's document vault. Mirror of admin_draft."""
        rec, slug, err = _gc_owned_sub(request, sub_slug)
        if err:
            return err
        ids = body.get("ids") or []
        if not ids:
            return JSONResponse({"error": "no program ids selected"}, status_code=400)
        publish = bool(body.get("publish"))
        try:
            from . import gaps as _gaps
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"error": f"gap engine unavailable: {exc}"}, status_code=500)

        _sector_src = (rec.get("trade") or rec.get("scope") or "").strip() or None
        drafts = _gaps.draft_programs(ids, company=rec.get("company"),
                                      effective_date=body.get("effective_date"),
                                      sector=_sector_src)
        if not drafts:
            return JSONResponse(
                {"error": "none of those standards have a draftable written program"},
                status_code=400)
        from . import compliance as _cmp
        docs_dir = _client_dir(sub_slug) / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        eff = body.get("effective_date") or time.strftime("%Y-%m-%d")
        built = []
        for d in drafts:
            # Build the SAME editable HTML document the Asset Library produces,
            # pre-filled with this sub's company name. It renders inline in the
            # dashboard and stays editable via "Fill in details" — no .docx
            # download, and no control-character crash on the way out.
            mid = "program-" + d["id"]
            fields = {"COMPANY_NAME": rec.get("company", "") or "",
                      "EFFECTIVE_DATE": eff}
            doc_html, title = _render_library_doc(mid, fields)
            if not doc_html:
                # No editable master for this program — fall back to a readable
                # HTML wrap of the drafted markdown (still inline, just not
                # token-fillable).
                title = d["title"]
                doc_html = _cmp.wrap_document(
                    "<pre style=\"white-space:pre-wrap;font:inherit\">"
                    + (d.get("markdown") or "") + "</pre>", title)
                mid = ""
            title = title or d["title"]
            fname = _cmp.safe_filename(title).rsplit(".", 1)[0] + ".html"
            (docs_dir / fname).write_text(doc_html, encoding="utf-8")
            row = {"name": title,
                   "sub": f"Built by Origin — {d.get('citation', '')}".strip(" —"),
                   "file": fname, "source": "origin-draft"}
            if mid:
                row["mid"] = mid
                row["fields"] = fields
            staged = rec.setdefault("staged_files", [])
            if publish:
                if fname in staged:
                    staged.remove(fname)
                for existing in rec.setdefault("documents", []):
                    if existing.get("name") == title:
                        existing.update(row)
                        break
                else:
                    rec["documents"].append(row)
            else:
                if fname not in staged:
                    staged.append(fname)
            built.append({"id": d["id"], "title": title, "file": fname,
                          "citation": d.get("citation", "")})
        rec["updated"] = _now()
        save_client(rec)
        return {"ok": True, "published": publish, "built": built}

    @app.get("/portal/api/gc/sub/{sub_slug}/draft/preview")
    def gc_draft_preview(sub_slug: str, request: Request, file: str = ""):
        """Let the GC open a built draft before publishing it to the sub."""
        rec, slug, err = _gc_owned_sub(request, sub_slug)
        if err:
            return err
        name = os.path.basename(file or "")
        path = _client_dir(sub_slug) / "docs" / name
        if not name or not path.is_file():
            return JSONResponse({"error": "file not found"}, status_code=404)
        return FileResponse(str(path))

    @app.get("/portal/api/gc/library")
    def gc_library(request: Request):
        """List the Asset Library masters so a GC can pick a safety document to
        drop into one of its subs' vaults. Mirror of admin_library."""
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        try:
            from . import compliance as _cmp
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"error": f"asset library unavailable: {exc}"}, status_code=500)
        try:
            _cmp.ensure_library()
        except Exception:
            pass
        return {"ok": True, "templates": _cmp.list_templates()}

    @app.get("/portal/api/gc/library/preview", response_class=HTMLResponse)
    def gc_library_preview(request: Request, mid: str = ""):
        """Render an Asset Library master to the browser so a GC can eyeball the
        document before adding it to a sub's vault. Read-only."""
        slug = acting_gc_slug(request)
        if not slug:
            return HTMLResponse("<h1>Sign in required</h1>", status_code=401)
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

    @app.post("/portal/api/gc/sub/{sub_slug}/from-library")
    def gc_from_library(sub_slug: str, request: Request, body: dict = Body(...)):
        """Render an Asset Library master into one of the GC's subs' vaults as a
        finished, published document. body: {mid, publish?}. Mirror of
        admin_from_library, scoped to the acting GC's subcontractor."""
        rec, slug, err = _gc_owned_sub(request, sub_slug)
        if err:
            return err
        mid = (body.get("mid") or "").strip()
        if not mid:
            return JSONResponse({"error": "no library document selected"}, status_code=400)
        try:
            from . import compliance as _cmp
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"error": f"asset library unavailable: {exc}"}, status_code=500)
        # Pre-fill the two fields we already know so the document isn't blank on
        # arrival; the GC can complete the rest with the "Fill in details" editor.
        fields = {"COMPANY_NAME": rec.get("company", "") or "",
                  "EFFECTIVE_DATE": time.strftime("%Y-%m-%d")}
        doc_html, title = _render_library_doc(mid, fields)
        if not doc_html:
            return JSONResponse({"error": "that library document was not found"}, status_code=404)
        docs_dir = _client_dir(sub_slug) / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        # Store the library master as a clean, continuous HTML document so the GC
        # views it as a normal scrollable/printable page — NOT a paginated PDF that
        # the browser viewer shows as PowerPoint-like page cards.
        html_path = _cmp.unique_path(
            docs_dir, (_cmp.safe_filename(title).rsplit(".", 1)[0] + ".html"))
        html_path.write_text(doc_html, encoding="utf-8")
        fname = html_path.name
        publish = body.get("publish")
        publish = True if publish is None else bool(publish)
        # Keep mid + fields on the row so the GC can re-edit the fill-in values
        # later (we re-render from the pristine master each time).
        row = {"name": title, "sub": "From Asset Library", "file": fname,
               "source": "asset-library", "mid": mid, "fields": fields}
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

    @app.get("/portal/api/gc/doc-fields")
    def gc_doc_fields(request: Request):
        """The fill-in fields a GC can complete on an asset-library document, in
        display order (token + friendly label)."""
        gc = acting_gc_slug(request)
        if not gc:
            return JSONResponse({"error": "sign in required"}, status_code=401)
        return {"fields": [{"token": t, "label": l} for t, l in _LIB_DOC_FIELDS]}

    @app.post("/portal/api/gc/sub/{sub_slug}/doc/fill")
    def gc_doc_fill(sub_slug: str, request: Request, body: dict = Body(...)):
        """Edit an asset-library document's fill-in values (company name, address,
        effective date, administrator, scope). Re-renders the document from the
        pristine master so fields can be changed as many times as needed.
        body: {index, fields:{TOKEN: value, ...}}."""
        rec, slug, err = _gc_owned_sub(request, sub_slug)
        if err:
            return err
        try:
            idx = int(body.get("index"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "which document?"}, status_code=400)
        docs = rec.get("documents", [])
        if idx < 0 or idx >= len(docs):
            return JSONResponse({"error": "document not found"}, status_code=404)
        row = docs[idx]
        mid = row.get("mid")
        if not mid:
            return JSONResponse(
                {"error": "this document isn't an editable library document"},
                status_code=400)
        # Merge the incoming values onto whatever was already filled.
        incoming = body.get("fields") or {}
        fields = dict(row.get("fields") or {})
        valid = {t for t, _ in _LIB_DOC_FIELDS}
        for k, v in incoming.items():
            if k in valid:
                fields[k] = (str(v).strip() if v is not None else "")
        doc_html, title = _render_library_doc(mid, fields)
        if not doc_html:
            return JSONResponse({"error": "the source library document is missing"},
                                status_code=404)
        # Rewrite the same file so View/Print keep working unchanged.
        docs_dir = _client_dir(sub_slug) / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        fname = os.path.basename(row.get("file") or "")
        if not fname or Path(fname).suffix.lower() not in (".html", ".htm"):
            from . import compliance as _cmp
            fname = _cmp.unique_path(
                docs_dir, (_cmp.safe_filename(title).rsplit(".", 1)[0] + ".html")).name
        (docs_dir / fname).write_text(doc_html, encoding="utf-8")
        row["file"] = fname
        row["fields"] = fields
        rec["updated"] = _now()
        save_client(rec)
        return {"ok": True, "fields": fields, "file": row["file"]}

    @app.post("/portal/api/gc/sub/{sub_slug}/doc/autofill")
    def gc_doc_autofill(sub_slug: str, request: Request, body: dict = Body(...)):
        """One-click auto-fill: pull the company name, scope and dates the
        platform already knows about this sub (and anything the GC already filled
        on another of the sub's documents) and, when apply=true, populate the
        chosen editable documents. Returns the suggested {TOKEN: value} set.
        body: {apply?: bool, use_ai?: bool, indices?: [int], index?: int}.
        When neither indices nor index is given, every editable document is
        filled (back-compat with the "Auto-fill all" button)."""
        rec, slug, err = _gc_owned_sub(request, sub_slug)
        if err:
            return err
        suggested = _autofill_fields(rec)
        # AI polish is on by default but degrades silently when no LLM is set up.
        if body.get("use_ai", True):
            suggested = _autofill_ai_enrich(rec, suggested)
        if not body.get("apply"):
            return {"ok": True, "fields": suggested, "applied": 0}
        from . import compliance as _cmp
        docs = rec.get("documents", [])
        # Work out which rows the GC asked to fill. Absent a selection, do all.
        want = None
        if isinstance(body.get("indices"), list):
            want = set()
            for i in body["indices"]:
                try:
                    want.add(int(i))
                except (TypeError, ValueError):
                    pass
        elif body.get("index") is not None:
            try:
                want = {int(body["index"])}
            except (TypeError, ValueError):
                want = set()
        docs_dir = _client_dir(sub_slug) / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        applied = 0
        details = []
        for idx, row in enumerate(docs):
            if want is not None and idx not in want:
                continue
            # Recover a lost mid from the document title so docs added before the
            # mid wiring (gap-finder / asset-library) are still fillable.
            mid = row.get("mid") or _recover_doc_mid(row)
            if not mid:
                continue  # only editable library/gap docs carry tokens
            row["mid"] = mid  # persist the recovery so future fills are instant
            # Existing values win — auto-fill never clobbers a hand-typed field.
            fields = dict(suggested)
            fields.update({k: v for k, v in (row.get("fields") or {}).items() if v})
            doc_html, title = _render_library_doc(mid, fields)
            if not doc_html:
                continue
            fname = os.path.basename(row.get("file") or "")
            if not fname or Path(fname).suffix.lower() not in (".html", ".htm"):
                fname = _cmp.safe_filename(title or row.get("name") or "document"
                                          ).rsplit(".", 1)[0] + ".html"
            (docs_dir / fname).write_text(doc_html, encoding="utf-8")
            row["file"] = fname
            row["fields"] = fields
            applied += 1
            details.append({"name": row.get("name") or title, "file": fname})
        if applied:
            rec["updated"] = _now()
            save_client(rec)
        return {"ok": True, "fields": suggested, "applied": applied, "docs": details}

    @app.post("/portal/api/gc/sub/{sub_slug}/doc/save-html")
    def gc_doc_save_html(sub_slug: str, request: Request, body: dict = Body(...)):
        """Persist a GC's in-place manual edits to an HTML document. The GC edits
        the document right in the dashboard viewer and saves; we overwrite the
        same file so View/Print show their edits. body: {index, html}."""
        rec, slug, err = _gc_owned_sub(request, sub_slug)
        if err:
            return err
        try:
            idx = int(body.get("index"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "which document?"}, status_code=400)
        docs = rec.get("documents", [])
        if idx < 0 or idx >= len(docs):
            return JSONResponse({"error": "document not found"}, status_code=404)
        row = docs[idx]
        fname = os.path.basename(row.get("file") or "")
        if not fname or Path(fname).suffix.lower() not in (".html", ".htm"):
            return JSONResponse(
                {"error": "this document can't be edited in place"},
                status_code=400)
        html = body.get("html")
        if not html or not str(html).strip():
            return JSONResponse({"error": "nothing to save"}, status_code=400)
        docs_dir = _client_dir(sub_slug) / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        (docs_dir / fname).write_text(str(html), encoding="utf-8")
        # Mark as hand-edited so a later "Fill in details" reset is a clear choice.
        row["edited"] = _now()
        rec["updated"] = _now()
        save_client(rec)
        return {"ok": True, "file": fname}

    @app.get("/portal/api/gc/sub/{sub_slug}/doc")
    def gc_doc(sub_slug: str, request: Request, file: str = ""):
        """Serve one of the sub's document files so the GC can VIEW or PRINT it.
        Scoped: only files listed on THIS sub's record, and only to the acting GC
        (a GC session or the owner acting via ?gc=)."""
        rec, slug, err = _gc_owned_sub(request, sub_slug)
        if err:
            return err
        allowed = {d.get("file") for d in rec.get("documents", []) if d.get("file")}
        allowed |= {c.get("file") for c in rec.get("coi", []) if c.get("file")}
        name = os.path.basename(file or "")
        if name not in allowed:
            return JSONResponse({"error": "not authorized for this document"}, status_code=403)
        path = _client_dir(sub_slug) / "docs" / name
        if not path.is_file():
            return JSONResponse({"error": "file not found"}, status_code=404)
        # Serve HTML documents inline as a normal continuous page (View/Print),
        # not as a download; everything else (PDF, images) streams as a file.
        if path.suffix.lower() in (".html", ".htm"):
            return HTMLResponse(path.read_text(encoding="utf-8", errors="replace"),
                                headers=_NO_STORE)
        return FileResponse(str(path))

    @app.post("/portal/api/gc/sub/{sub_slug}/doc/upload")
    def gc_doc_upload(sub_slug: str, request: Request,
                      file: UploadFile = File(...), name: str = Form("")):
        """The GC uploads a document their sub already has (a manual, a permit, a
        signed program) so it shows on the sub's page. Replaces a row of the same
        name, else adds a new one."""
        rec, slug, err = _gc_owned_sub(request, sub_slug)
        if err:
            return err
        safe = os.path.basename(file.filename or "document")
        dest = _client_dir(sub_slug) / "docs"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / safe).write_bytes(file.file.read())
        doc_name = (name or os.path.splitext(safe)[0]).strip() or safe
        for existing in rec.setdefault("documents", []):
            if existing.get("name") == doc_name:
                existing.update({"file": safe, "sub": "Uploaded by GC", "source": "gc-upload"})
                break
        else:
            rec["documents"].append(
                {"name": doc_name, "sub": "Uploaded by GC", "file": safe, "source": "gc-upload"})
        rec["updated"] = _now()
        save_client(rec)
        return {"ok": True, "file": safe, "name": doc_name}

    @app.post("/portal/api/gc/sub/{sub_slug}/doc/edit")
    def gc_doc_edit(sub_slug: str, request: Request, body: dict = Body(...)):
        """Rename/re-label one of the sub's document rows (by its index in the
        documents list). Lets the GC keep the sub's paperwork tidy."""
        rec, slug, err = _gc_owned_sub(request, sub_slug)
        if err:
            return err
        docs = rec.get("documents", [])
        try:
            row = docs[int(body.get("index"))]
        except Exception:
            return JSONResponse({"error": "document not found"}, status_code=404)
        name = (body.get("name") or "").strip()
        if name:
            row["name"] = name
        if "sub" in body:
            row["sub"] = (body.get("sub") or "").strip()
        rec["updated"] = _now()
        save_client(rec)
        return {"ok": True}

    @app.post("/portal/api/gc/sub/{sub_slug}/doc/remove")
    def gc_doc_remove(sub_slug: str, request: Request, body: dict = Body(...)):
        """Remove one of the sub's document rows by its index in the documents
        list (does not delete the underlying file from disk)."""
        rec, slug, err = _gc_owned_sub(request, sub_slug)
        if err:
            return err
        docs = rec.get("documents", [])
        try:
            idx = int(body.get("index"))
        except Exception:
            return JSONResponse({"error": "index required"}, status_code=400)
        if idx < 0 or idx >= len(docs):
            return JSONResponse({"error": "document not found"}, status_code=404)
        docs.pop(idx)
        rec["updated"] = _now()
        save_client(rec)
        return {"ok": True}

    @app.post("/portal/api/gc/sub/{sub_slug}/remove")
    def gc_remove_sub(sub_slug: str, request: Request):
        """Unplace a sub from this GC (does not delete the subcontractor)."""
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sub_slug)
        if not rec or (rec.get("gc_slug", "") or "") != slug:
            return JSONResponse({"error": "not found"}, status_code=404)
        rec["gc_slug"] = ""
        save_client(rec)
        return {"ok": True}

    # ===================== MESSAGING =====================
    # Two independent two-way threads:
    #   owner  <-> GC   : stored on the GC record ("messages"), sender owner|gc
    #   GC     <-> sub  : stored on the client record ("messages"), sender gc|sub

    def _thread_out(msgs, viewer: str, seen_key: str = "", file_base: str = ""):
        """Normalise a stored thread for the client, tagging each message 'me' or
        'them' from the viewer's perspective. For the viewer's own messages,
        `seen` reports whether the counterparty has read it (seen_key = that
        party's read flag). Attachments are returned with ready-to-use URLs."""
        out = []
        for m in msgs or []:
            mine = m.get("sender") == viewer
            atts = []
            for a in (m.get("attachments") or []):
                fn = a.get("file", "")
                atts.append({"name": a.get("name", fn),
                             "url": (file_base + fn) if (file_base and fn) else ""})
            out.append({
                "body": m.get("body", ""),
                "ts": m.get("ts", ""),
                "sender": m.get("sender", ""),
                "mine": mine,
                "seen": bool(m.get(seen_key)) if (mine and seen_key) else False,
                "attachments": atts,
            })
        return out

    # ---- owner <-> GC ----
    @app.get("/portal/api/admin/gc/{slug}/messages")
    def owner_gc_messages(slug: str, request: Request):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_gc(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        changed = False
        for m in rec.get("messages", []):
            if m.get("sender") == "gc" and not m.get("read_owner"):
                m["read_owner"] = True
                changed = True
        if changed:
            save_gc(rec)
        return {"messages": _thread_out(rec.get("messages", []), "owner",
                                        "read_gc", f"/portal/api/admin/gc/{slug}/msgfile/"),
                "peer_typing": _get_typing(_gc_dir(slug), "gc")}

    @app.post("/portal/api/admin/gc/{slug}/messages")
    def owner_gc_send(slug: str, request: Request, body: dict = Body(...)):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_gc(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        text = (body.get("body") or "").strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)
        rec.setdefault("messages", []).append({
            "sender": "owner", "body": text, "ts": _now(),
            "read_owner": True, "read_gc": False})
        save_gc(rec)
        return {"ok": True}

    @app.post("/portal/api/admin/gc/{slug}/typing")
    def owner_gc_typing(slug: str, request: Request):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        if not load_gc(slug):
            return JSONResponse({"error": "not found"}, status_code=404)
        _set_typing(_gc_dir(slug), "owner")
        return {"ok": True}

    @app.post("/portal/api/admin/gc/{slug}/attach")
    def owner_gc_attach(slug: str, request: Request,
                        file: UploadFile = File(...), body: str = Form("")):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_gc(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        safe = _save_msgfile(_gc_dir(slug) / "msgfiles", file)
        rec.setdefault("messages", []).append({
            "sender": "owner", "body": (body or "").strip(), "ts": _now(),
            "read_owner": True, "read_gc": False,
            "attachments": [{"name": os.path.basename(file.filename or safe), "file": safe}]})
        save_gc(rec)
        return {"ok": True}

    @app.get("/portal/api/admin/gc/{slug}/msgfile/{fname}")
    def owner_gc_msgfile(slug: str, fname: str, request: Request):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        fp = _gc_dir(slug) / "msgfiles" / os.path.basename(fname)
        if not fp.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(str(fp), filename=os.path.basename(fname).split("_", 1)[-1])

    @app.get("/portal/api/gc/owner-messages")
    def gc_owner_messages(request: Request):
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_gc(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        # mark owner->gc messages read only when a real GC (not the owner) reads
        if gc_session(request):
            changed = False
            for m in rec.get("messages", []):
                if m.get("sender") == "owner" and not m.get("read_gc"):
                    m["read_gc"] = True
                    changed = True
            if changed:
                save_gc(rec)
        return {"messages": _thread_out(rec.get("messages", []), "gc",
                                        "read_owner", "/portal/api/gc/owner-msgfile/"),
                "peer_typing": _get_typing(_gc_dir(slug), "owner")}

    @app.post("/portal/api/gc/owner-messages")
    def gc_owner_send(request: Request, body: dict = Body(...)):
        # Only a real GC posts into the owner thread as "gc". The owner posts from
        # the admin side. If the owner is acting for a GC, block to avoid confusion.
        gs = gc_session(request)
        if not gs:
            return JSONResponse({"error": "GC only"}, status_code=403)
        rec = load_gc(gs["slug"])
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        text = (body.get("body") or "").strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)
        rec.setdefault("messages", []).append({
            "sender": "gc", "body": text, "ts": _now(),
            "read_owner": False, "read_gc": True})
        save_gc(rec)
        return {"ok": True}

    @app.post("/portal/api/gc/owner-typing")
    def gc_owner_typing(request: Request):
        gs = gc_session(request)
        if not gs:
            return JSONResponse({"error": "GC only"}, status_code=403)
        _set_typing(_gc_dir(gs["slug"]), "gc")
        return {"ok": True}

    @app.post("/portal/api/gc/owner-attach")
    def gc_owner_attach(request: Request, file: UploadFile = File(...), body: str = Form("")):
        gs = gc_session(request)
        if not gs:
            return JSONResponse({"error": "GC only"}, status_code=403)
        rec = load_gc(gs["slug"])
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        safe = _save_msgfile(_gc_dir(gs["slug"]) / "msgfiles", file)
        rec.setdefault("messages", []).append({
            "sender": "gc", "body": (body or "").strip(), "ts": _now(),
            "read_owner": False, "read_gc": True,
            "attachments": [{"name": os.path.basename(file.filename or safe), "file": safe}]})
        save_gc(rec)
        return {"ok": True}

    @app.get("/portal/api/gc/owner-msgfile/{fname}")
    def gc_owner_msgfile(fname: str, request: Request):
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        fp = _gc_dir(slug) / "msgfiles" / os.path.basename(fname)
        if not fp.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(str(fp), filename=os.path.basename(fname).split("_", 1)[-1])

    # ---- GC <-> sub ----
    @app.get("/portal/api/gc/sub/{sub_slug}/messages")
    def gc_sub_messages(sub_slug: str, request: Request):
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sub_slug)
        if not rec or (rec.get("gc_slug", "") or "") != slug:
            return JSONResponse({"error": "not found"}, status_code=404)
        changed = False
        for m in rec.get("messages", []):
            if m.get("sender") == "sub" and not m.get("read_gc"):
                m["read_gc"] = True
                changed = True
        if changed:
            save_client(rec)
        return {"messages": _thread_out(rec.get("messages", []), "gc",
                                        "read_sub", f"/portal/api/gc/sub/{sub_slug}/msgfile/"),
                "peer_typing": _get_typing(_client_dir(sub_slug), "sub")}

    @app.post("/portal/api/gc/sub/{sub_slug}/messages")
    def gc_sub_send(sub_slug: str, request: Request, body: dict = Body(...)):
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sub_slug)
        if not rec or (rec.get("gc_slug", "") or "") != slug:
            return JSONResponse({"error": "not found"}, status_code=404)
        text = (body.get("body") or "").strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)
        rec.setdefault("messages", []).append({
            "sender": "gc", "body": text, "ts": _now(),
            "read_gc": True, "read_sub": False})
        save_client(rec)
        return {"ok": True}

    @app.post("/portal/api/gc/sub/{sub_slug}/typing")
    def gc_sub_typing(sub_slug: str, request: Request):
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sub_slug)
        if not rec or (rec.get("gc_slug", "") or "") != slug:
            return JSONResponse({"error": "not found"}, status_code=404)
        _set_typing(_client_dir(sub_slug), "gc")
        return {"ok": True}

    @app.post("/portal/api/gc/sub/{sub_slug}/attach")
    def gc_sub_attach(sub_slug: str, request: Request,
                      file: UploadFile = File(...), body: str = Form("")):
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sub_slug)
        if not rec or (rec.get("gc_slug", "") or "") != slug:
            return JSONResponse({"error": "not found"}, status_code=404)
        safe = _save_msgfile(_client_dir(sub_slug) / "msgfiles", file)
        rec.setdefault("messages", []).append({
            "sender": "gc", "body": (body or "").strip(), "ts": _now(),
            "read_gc": True, "read_sub": False,
            "attachments": [{"name": os.path.basename(file.filename or safe), "file": safe}]})
        save_client(rec)
        return {"ok": True}

    @app.get("/portal/api/gc/sub/{sub_slug}/msgfile/{fname}")
    def gc_sub_msgfile(sub_slug: str, fname: str, request: Request):
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sub_slug)
        if not rec or (rec.get("gc_slug", "") or "") != slug:
            return JSONResponse({"error": "not found"}, status_code=404)
        fp = _client_dir(sub_slug) / "msgfiles" / os.path.basename(fname)
        if not fp.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(str(fp), filename=os.path.basename(fname).split("_", 1)[-1])

    # ---- sub side of the GC<->sub thread ----
    @app.get("/portal/api/messages")
    def sub_messages(request: Request):
        sess = client_session(request)
        if not sess:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sess["slug"])
        if not rec:
            return JSONResponse({"error": "account not found"}, status_code=404)
        changed = False
        for m in rec.get("messages", []):
            if m.get("sender") == "gc" and not m.get("read_sub"):
                m["read_sub"] = True
                changed = True
        if changed:
            save_client(rec)
        return {"messages": _thread_out(rec.get("messages", []), "sub",
                                        "read_gc", "/portal/api/msgfile/"),
                "peer_typing": _get_typing(_client_dir(sess["slug"]), "gc"),
                "gc_slug": rec.get("gc_slug", "")}

    @app.post("/portal/api/messages")
    def sub_send(request: Request, body: dict = Body(...)):
        sess = client_session(request)
        if not sess:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sess["slug"])
        if not rec:
            return JSONResponse({"error": "account not found"}, status_code=404)
        text = (body.get("body") or "").strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)
        rec.setdefault("messages", []).append({
            "sender": "sub", "body": text, "ts": _now(),
            "read_gc": False, "read_sub": True})
        save_client(rec)
        return {"ok": True}

    @app.post("/portal/api/typing")
    def sub_typing(request: Request):
        sess = client_session(request)
        if not sess:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        _set_typing(_client_dir(sess["slug"]), "sub")
        return {"ok": True}

    @app.post("/portal/api/attach")
    def sub_attach(request: Request,
                   file: UploadFile = File(...), body: str = Form("")):
        sess = client_session(request)
        if not sess:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sess["slug"])
        if not rec:
            return JSONResponse({"error": "account not found"}, status_code=404)
        safe = _save_msgfile(_client_dir(sess["slug"]) / "msgfiles", file)
        rec.setdefault("messages", []).append({
            "sender": "sub", "body": (body or "").strip(), "ts": _now(),
            "read_gc": False, "read_sub": True,
            "attachments": [{"name": os.path.basename(file.filename or safe), "file": safe}]})
        save_client(rec)
        return {"ok": True}

    @app.get("/portal/api/msgfile/{fname}")
    def sub_msgfile(fname: str, request: Request):
        sess = client_session(request)
        if not sess:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        fp = _client_dir(sess["slug"]) / "msgfiles" / os.path.basename(fname)
        if not fp.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(str(fp), filename=os.path.basename(fname).split("_", 1)[-1])

    # ===================== LOGOS =====================
    def _save_logo(dirpath: Path, upload) -> str:
        dirpath.mkdir(parents=True, exist_ok=True)
        fname = "logo" + _logo_ext(getattr(upload, "filename", "") or "")
        (dirpath / fname).write_bytes(upload.file.read())
        return fname

    @app.post("/portal/api/admin/gc/{slug}/logo")
    def admin_gc_logo(slug: str, request: Request, file: UploadFile = File(...)):
        if not admin_session(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_gc(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        rec["logo"] = _save_logo(_gc_dir(slug), file)
        save_gc(rec)
        return {"ok": True, "logo": rec["logo"]}

    @app.post("/portal/api/gc/logo")
    def gc_self_logo(request: Request, file: UploadFile = File(...)):
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_gc(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        rec["logo"] = _save_logo(_gc_dir(slug), file)
        save_gc(rec)
        return {"ok": True, "logo": rec["logo"]}

    @app.get("/portal/api/gc/{slug}/logo")
    def serve_gc_logo(slug: str):
        rec = load_gc(slug)
        if not rec or not rec.get("logo"):
            return JSONResponse({"error": "no logo"}, status_code=404)
        fp = _gc_dir(slug) / os.path.basename(rec["logo"])
        if not fp.is_file():
            return JSONResponse({"error": "no logo"}, status_code=404)
        return FileResponse(str(fp))

    @app.post("/portal/api/gc/sub/{sub_slug}/logo")
    def gc_sub_logo(sub_slug: str, request: Request, file: UploadFile = File(...)):
        slug = acting_gc_slug(request)
        if not slug:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        rec = load_client(sub_slug)
        if not rec or (rec.get("gc_slug", "") or "") != slug:
            return JSONResponse({"error": "not found"}, status_code=404)
        rec["logo"] = _save_logo(_client_dir(sub_slug) / "docs", file)
        save_client(rec)
        return {"ok": True, "logo": rec["logo"]}

    @app.get("/portal/api/client/{slug}/logo")
    def serve_client_logo(slug: str):
        rec = load_client(slug)
        if not rec or not rec.get("logo"):
            return JSONResponse({"error": "no logo"}, status_code=404)
        fp = _client_dir(slug) / "docs" / os.path.basename(rec["logo"])
        if not fp.is_file():
            return JSONResponse({"error": "no logo"}, status_code=404)
        return FileResponse(str(fp))

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
