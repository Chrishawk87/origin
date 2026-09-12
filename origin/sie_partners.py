"""Origin — White-label SIE partners (the tenant tier above a firm's clients).

Chris wanted to onboard partner firms the SAME way he onboards GCs, but for the
Safety Intelligence Engine / Abatement side: the owner creates a PARTNER account,
the partner signs in at /sie, creates their own client accounts (abatement
matters) that never bleed into any other partner's, saves all their documents,
and — if they choose — restyles the dashboard with their own brand.

Design (identical house-rules to portal.py / sie_gate.py, kept SAFE + additive):

  * A partner is a light flat-JSON record under PORTAL_DIR/sie_partners/<slug>/
    partner.json — exactly the storage shape GCs use, so nothing new to learn.
  * Isolation is FREE: the whole Abatement feature already scopes every matter,
    deadline, evidence item and saved document by `firm_slug =
    request.state.sie_gc_slug`. A partner session simply stamps that slug with
    the partner's own slug, so a partner can only ever see its own matters. No
    new isolation code, no schema migration.
  * A partner logs in with email + PIN (reusing portal.verify_pin), minting a
    dedicated PARTNER cookie (portal.SIE_PARTNER_COOKIE) that no existing flow
    reads — so adding partners cannot disturb the owner, GC, or client consoles.
  * Branding is a few extra fields (brand_name / brand_primary / brand_secondary
    / theme / logo / tagline); the /sie console reads them via /api/sie-partner/
    self and recolors itself. Absent/blank fields fall back to Origin's defaults.

register_sie_partners(app) is wrapped in try/except by the caller, imports
nothing heavy, and fails closed — a bug here can never break the rest of Origin.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

# Module-level so FastAPI can resolve the string annotation `request: Request`
# (this module uses `from __future__ import annotations`, so annotations are
# strings resolved against these module globals — a function-local import is
# invisible to that resolver and makes FastAPI treat `request` as a query param).
try:
    from starlette.requests import Request
except Exception:  # pragma: no cover
    Request = Any  # type: ignore


def _portal():
    from . import portal as _p
    return _p


# Storage lives beside the GC vault, under the same portal data root.
def _partners_dir() -> Path:
    return _portal().PORTAL_DIR / "sie_partners"


def _partner_dir(slug: str) -> Path:
    return _partners_dir() / slug


def _partner_file(slug: str) -> Path:
    return _partner_dir(slug) / "partner.json"


# Origin's own defaults — a partner that never sets branding looks like Origin.
DEFAULT_BRAND_PRIMARY = "#1E7A46"
DEFAULT_BRAND_SECONDARY = "#186338"


def _blank_partner(name: str, email: str = "") -> Dict[str, Any]:
    P = _portal()
    slug = P.slugify(name)
    now = P._now()
    return {
        "slug": slug,
        "name": name,
        "email": (email or "").strip(),
        "pin_hash": "",
        # ── white-label branding (all optional; blank → Origin defaults) ──
        "brand_name": name,                 # wordmark shown in the console
        "brand_primary": DEFAULT_BRAND_PRIMARY,
        "brand_secondary": DEFAULT_BRAND_SECONDARY,
        "theme": "dark",                    # "dark" | "light"
        "logo": "",                         # filename of an uploaded logo, if any
        "tagline": "",                      # optional sub-headline
        "created": now,
        "updated": now,
    }


def load_partner(slug: str) -> Optional[Dict[str, Any]]:
    f = _partner_file((slug or "").strip())
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_partner(data: Dict[str, Any]) -> Dict[str, Any]:
    P = _portal()
    slug = data["slug"]
    _partner_dir(slug).mkdir(parents=True, exist_ok=True)
    data["updated"] = P._now()
    _partner_file(slug).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def _matter_count(slug: str) -> int:
    """How many abatement matters belong to this partner (best-effort, non-fatal)."""
    try:
        from . import abatement_matter as _mm
        return sum(1 for m in _mm.list_matters(firm_slug=slug) or [])
    except Exception:
        return 0


def list_partners() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    d = _partners_dir()
    if not d.is_dir():
        return out
    for sub in sorted(d.iterdir()):
        rec = load_partner(sub.name)
        if not rec:
            continue
        out.append({
            "slug": rec.get("slug"),
            "name": rec.get("name"),
            "email": rec.get("email"),
            "brand_name": rec.get("brand_name") or rec.get("name"),
            "brand_primary": rec.get("brand_primary") or DEFAULT_BRAND_PRIMARY,
            "theme": rec.get("theme") or "dark",
            "logo": rec.get("logo", ""),
            "has_pin": bool(rec.get("pin_hash")),
            "matter_count": _matter_count(rec.get("slug", "")),
            "created": rec.get("created"),
            "updated": rec.get("updated"),
        })
    return out


def find_partner_by_email(email: str) -> Optional[Dict[str, Any]]:
    email = (email or "").strip().lower()
    d = _partners_dir()
    if not email or not d.is_dir():
        return None
    matches: List[Dict[str, Any]] = []
    for sub in sorted(d.iterdir()):
        rec = load_partner(sub.name)
        if rec and (rec.get("email", "").strip().lower() == email):
            matches.append(rec)
    if not matches:
        return None
    matches.sort(key=lambda r: r.get("updated", ""), reverse=True)
    return matches[0]


def public_view(rec: Dict[str, Any]) -> Dict[str, Any]:
    """A partner record safe to hand to the browser (no pin_hash)."""
    return {
        "slug": rec.get("slug"),
        "name": rec.get("name"),
        "email": rec.get("email"),
        "brand_name": rec.get("brand_name") or rec.get("name"),
        "brand_primary": rec.get("brand_primary") or DEFAULT_BRAND_PRIMARY,
        "brand_secondary": rec.get("brand_secondary") or DEFAULT_BRAND_SECONDARY,
        "theme": rec.get("theme") or "dark",
        "logo": rec.get("logo", ""),
        "tagline": rec.get("tagline", ""),
        "has_pin": bool(rec.get("pin_hash")),
    }


# ── branding sanitizers (never trust raw input) ───────────────────────────────
_HEX = set("0123456789abcdefABCDEF")


def _clean_color(val: Any, fallback: str) -> str:
    s = (str(val or "")).strip()
    if s.startswith("#") and len(s) in (4, 7) and all(c in _HEX for c in s[1:]):
        return s
    return fallback


def apply_branding(rec: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a partner-supplied branding payload onto their record, in place."""
    if not isinstance(body, dict):
        return rec
    if "brand_name" in body:
        rec["brand_name"] = (str(body.get("brand_name") or "")).strip() or rec.get("name", "")
    if "tagline" in body:
        rec["tagline"] = (str(body.get("tagline") or "")).strip()[:140]
    if "brand_primary" in body:
        rec["brand_primary"] = _clean_color(body.get("brand_primary"), DEFAULT_BRAND_PRIMARY)
    if "brand_secondary" in body:
        rec["brand_secondary"] = _clean_color(body.get("brand_secondary"), DEFAULT_BRAND_SECONDARY)
    if "theme" in body:
        rec["theme"] = "light" if str(body.get("theme")).lower() == "light" else "dark"
    if "logo" in body:
        rec["logo"] = (str(body.get("logo") or "")).strip()
    return rec


# ── session helpers (reuse portal's proven signer + cookie scheme) ────────────
def partner_slug_from_request(request) -> Optional[str]:
    """The slug of the partner whose session cookie this request carries, or None.
    Mirrors server._gc_slug_ok — reads portal.SIE_PARTNER_COOKIE via portal's
    module-level _unsign so there is ONE signing scheme across the whole app."""
    try:
        P = _portal()
        ck = getattr(request, "cookies", {}) or {}
        p = P._unsign(ck.get(P.SIE_PARTNER_COOKIE, ""))
        if p and p.get("role") == "sie_partner":
            return (p.get("slug") or "").strip() or None
    except Exception:
        pass
    return None


# ── routes ────────────────────────────────────────────────────────────────────
def register_sie_partners(app) -> None:
    """Owner CRUD + partner self-service. Isolated + non-fatal, like the rest."""
    from fastapi import Body
    from fastapi.responses import JSONResponse

    try:
        from starlette.requests import Request
    except Exception:  # pragma: no cover
        Request = Any  # type: ignore

    P = _portal()

    def _is_owner(request) -> bool:
        p = P._unsign(request.cookies.get(P.ADMIN_COOKIE, ""))
        return bool(p and p.get("role") == "admin")

    # ── owner: list every partner ─────────────────────────────────────────────
    @app.get("/api/sie-partner/list")
    def sie_partner_list(request: Request):
        if not _is_owner(request):
            return JSONResponse({"error": "Owner only."}, status_code=403)
        return {"ok": True, "partners": list_partners()}

    # ── owner: create a partner (same feel as creating a GC) ──────────────────
    @app.post("/api/sie-partner/create")
    def sie_partner_create(request: Request, body: dict = Body(...)):
        if not _is_owner(request):
            return JSONResponse({"error": "Owner only."}, status_code=403)
        name = (body.get("name") or "").strip()
        email = (body.get("email") or "").strip()
        pin = (body.get("pin") or "").strip()
        if not name:
            return JSONResponse({"error": "A partner name is required."}, status_code=400)
        rec = _blank_partner(name, email)
        if not rec["slug"]:
            return JSONResponse({"error": "That name can't be turned into an ID."},
                                status_code=400)
        if load_partner(rec["slug"]):
            return JSONResponse({"error": "A partner with that name already exists."},
                                status_code=409)
        temp_pin = pin or "1234"
        rec["pin_hash"] = P.hash_pin(rec["slug"], temp_pin)
        apply_branding(rec, body)  # allow branding at create time too
        save_partner(rec)
        return {"ok": True, "partner": public_view(rec),
                "temp_pin": None if pin else temp_pin}

    # ── partner: read my own record + branding ────────────────────────────────
    # NOTE: path is /self/... so the middleware whitelist can grant partners this
    # route (and only this one) without exposing the owner-only routes above.
    # These STATIC /self routes MUST be declared before the dynamic /{slug}
    # routes below, or Starlette matches /{slug} first (slug == "self") and hits
    # the owner guard, returning 403 to a legitimate partner.
    @app.get("/api/sie-partner/self")
    def sie_partner_self(request: Request):
        slug = partner_slug_from_request(request)
        if not slug:
            return JSONResponse({"error": "Not a partner session."}, status_code=403)
        rec = load_partner(slug)
        if not rec:
            return JSONResponse({"error": "No such partner."}, status_code=404)
        return {"ok": True, "partner": public_view(rec)}

    # ── partner: restyle my own dashboard ─────────────────────────────────────
    @app.post("/api/sie-partner/self/branding")
    def sie_partner_self_branding(request: Request, body: dict = Body(...)):
        slug = partner_slug_from_request(request)
        if not slug:
            return JSONResponse({"error": "Not a partner session."}, status_code=403)
        rec = load_partner(slug)
        if not rec:
            return JSONResponse({"error": "No such partner."}, status_code=404)
        apply_branding(rec, body)
        save_partner(rec)
        return {"ok": True, "partner": public_view(rec)}

    # ── owner: reset a partner's PIN ──────────────────────────────────────────
    @app.post("/api/sie-partner/{slug}/pin")
    def sie_partner_pin(slug: str, request: Request, body: dict = Body(default=None)):
        if not _is_owner(request):
            return JSONResponse({"error": "Owner only."}, status_code=403)
        rec = load_partner(slug)
        if not rec:
            return JSONResponse({"error": "No such partner."}, status_code=404)
        pin = ((body or {}).get("pin") or "").strip() or "1234"
        rec["pin_hash"] = P.hash_pin(rec["slug"], pin)
        save_partner(rec)
        return {"ok": True, "temp_pin": pin}

    # ── owner: set a partner's branding on their behalf ───────────────────────
    @app.post("/api/sie-partner/{slug}/branding")
    def sie_partner_owner_branding(slug: str, request: Request, body: dict = Body(...)):
        if not _is_owner(request):
            return JSONResponse({"error": "Owner only."}, status_code=403)
        rec = load_partner(slug)
        if not rec:
            return JSONResponse({"error": "No such partner."}, status_code=404)
        apply_branding(rec, body)
        save_partner(rec)
        return {"ok": True, "partner": public_view(rec)}
