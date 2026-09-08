"""Brain Router — the agency-mode engine behind Origin's "Unified App".

Origin is a universal Governance / Risk / Compliance platform, not a single-
agency tool. A corporate admin toggles ON the regulatory agencies that actually
govern their work (OSHA construction, MSHA mining, FMCSA trucking, EPA, USACE,
PHMSA pipeline, FRA rail). That single choice drives three things, all resolved
deterministically here from Origin's own knowledge — never an LLM:

  1. NAVIGATION   — which agency-specific tabs appear (Pre-Shift Logs, Part 46
                    Training, Driver Logs, AHA Matrix, …). Toggle MSHA on and the
                    mining tabs slide in; toggle it off and they disappear.
  2. CITATION SCOPE — the photo-audit / citation resolver is restricted so ONLY
                    an enabled agency's CFR standards can surface. A mine finding
                    can never be reported to a customer that hasn't turned on
                    mining, and OSHA-only customers never see a 30/40/43/49 CFR
                    citation. Each authority stays in its own lane; nothing blends.
  3. FORM VAULT   — which official government forms the agency uses (MSHA 5000-23,
                    OSHA 300/300A, USACE AHA Matrix, …) so the Form Engine knows
                    what to auto-fill.

Storage is file-based JSON per tenant (the same DATA_DIR convention every other
SIE module uses); there is no database. "Unset" (no profile saved yet) means the
tool behaves exactly as it does today — no scoping — so turning the router on is
purely additive and never a regression for an existing account.
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .paths import DATA_DIR

# ── Agency catalog ────────────────────────────────────────────────────────────
# The single source of truth for what an agency "mode" means. `authority` is the
# code the citation resolver stamps on a standard (photo_audit._authority_for),
# so citation scoping can be done by authority set. `titles` are the CFR titles
# the agency governs. `brain` is the state of that agency's knowledge corpus:
#   ready   — verbatim CFR is loaded; citations resolve now.
#   partial — some parts loaded, not the whole title.
#   planned — mode can be toggled, but no brain yet (won't cite until ingested).
# USACE governs via EM 385-1-1 (Army Corps manual), not a CFR title, so it has
# no citation authority — it drives the AHA checklist/form, not CFR citations.

AGENCY_CATALOG: List[Dict[str, Any]] = [
    {
        "code": "OSHA",
        "label": "OSHA — General Industry & Construction",
        "authority": "OSHA",
        "titles": ["29"],
        "brain": "ready",
        "tabs": [
            {"id": "osha_programs", "label": "1910 / 1926 Programs"},
            {"id": "osha_300", "label": "300 Log"},
        ],
        "forms": ["OSHA 300", "OSHA 300A", "OSHA 301"],
    },
    {
        "code": "MSHA",
        "label": "MSHA — Mining Operations",
        "authority": "MSHA",
        "titles": ["30"],
        "brain": "ready",
        "tabs": [
            {"id": "msha_preshift", "label": "Pre-Shift Logs"},
            {"id": "msha_part46", "label": "Part 46 Training"},
        ],
        "forms": ["MSHA 5000-23", "MSHA 7000-1"],
    },
    {
        "code": "FMCSA",
        "label": "FMCSA — Motor Carrier / Trucking",
        "authority": "DOT",
        "titles": ["49"],
        "brain": "ready",
        "tabs": [
            {"id": "fmcsa_logs", "label": "Driver Logs (HOS)"},
            {"id": "fmcsa_dvir", "label": "Vehicle Inspections"},
        ],
        "forms": ["Driver Vehicle Inspection Report (DVIR)", "Record of Duty Status"],
    },
    {
        "code": "EPA",
        "label": "EPA — Environmental",
        "authority": "EPA",
        "titles": ["40"],
        "brain": "ready",
        "tabs": [
            {"id": "epa_spcc", "label": "SPCC / Spill"},
            {"id": "epa_waste", "label": "Waste Manifests"},
        ],
        "forms": ["SPCC Plan", "Uniform Hazardous Waste Manifest"],
    },
    {
        "code": "BLM",
        "label": "BLM — Federal Lands / Lease",
        "authority": "BLM",
        "titles": ["43"],
        "brain": "ready",
        "tabs": [
            {"id": "blm_lease", "label": "Lease Operations"},
        ],
        "forms": ["Sundry Notice (Form 3160-5)"],
    },
    {
        "code": "USACE",
        "label": "USACE — Army Corps (EM 385-1-1)",
        "authority": None,  # governs via EM 385, not a CFR title → no CFR citation lane
        "titles": [],
        "brain": "ready",
        "tabs": [
            {"id": "usace_aha", "label": "AHA Matrix"},
        ],
        "forms": ["Activity Hazard Analysis (AHA)"],
    },
    {
        "code": "PHMSA",
        "label": "PHMSA — Pipeline Safety",
        "authority": "DOT",
        "titles": ["49"],          # Parts 190–199 (pipeline) — distinct from FMCSA's 380–399
        "brain": "planned",         # hazmat parts partial; pipeline parts not ingested yet
        "tabs": [
            {"id": "phmsa_integrity", "label": "Pipeline Integrity"},
        ],
        "forms": ["PHMSA Incident Report (F 7000-1)"],
    },
    {
        "code": "FRA",
        "label": "FRA — Railroad",
        "authority": "DOT",
        "titles": ["49"],          # Parts 200–299 (rail)
        "brain": "planned",         # no rail brain yet
        "tabs": [
            {"id": "fra_logs", "label": "Rail Safety Logs"},
        ],
        "forms": ["FRA Accident/Incident Report (F 6180.54)"],
    },
]

# Fast lookups derived from the catalog.
_BY_CODE: Dict[str, Dict[str, Any]] = {a["code"]: a for a in AGENCY_CATALOG}
VALID_CODES: List[str] = [a["code"] for a in AGENCY_CATALOG]

# Core tabs every mode shows — the universal spine of the app.
BASE_TABS: List[Dict[str, str]] = [
    {"id": "dashboard", "label": "Dashboard"},
    {"id": "photo", "label": "Photo Audit"},
    {"id": "checklist", "label": "Checklist"},
    {"id": "review", "label": "Review"},
]

# What a brand-new (unset) profile shows in the nav: every agency whose brain is
# live. Citation scoping stays OFF while unset (see citation_authorities), so this
# only affects which tabs are visible, never what can be cited.
_DEFAULT_DISPLAY: List[str] = [a["code"] for a in AGENCY_CATALOG if a["brain"] == "ready"]

_STORE = DATA_DIR / "brain_router"


# ── Persistence (file-based, per tenant) ──────────────────────────────────────
def _key(gc_slug: Optional[str]) -> str:
    """A tenant's storage key. Owner / global profile lives under '_owner'."""
    slug = (gc_slug or "").strip()
    return slug or "_owner"


def _path(gc_slug: Optional[str]) -> Path:
    return _STORE / f"{_key(gc_slug)}.json"


def _load(gc_slug: Optional[str]) -> Optional[Dict[str, Any]]:
    p = _path(gc_slug)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_active(gc_slug: Optional[str] = None) -> Optional[List[str]]:
    """The agencies this tenant has explicitly enabled, or None if they have
    never set a profile. None is meaningful: it means "unset" → no citation
    scoping, preserving the tool's pre-router behavior exactly."""
    rec = _load(gc_slug)
    if not rec:
        return None
    codes = rec.get("active_agencies")
    if not isinstance(codes, list):
        return None
    # keep only codes still in the catalog, preserve catalog order
    live = [c for c in VALID_CODES if c in set(codes)]
    return live


def set_active(agencies: List[str], gc_slug: Optional[str] = None) -> Dict[str, Any]:
    """Persist the tenant's enabled agencies (validated against the catalog).
    Unknown codes are dropped rather than erroring. Returns the resulting
    profile so the caller can echo the new nav + scope straight back."""
    requested = agencies if isinstance(agencies, list) else []
    clean = [c for c in VALID_CODES if c in set(requested)]  # catalog order, valid only
    _STORE.mkdir(parents=True, exist_ok=True)
    rec = {"active_agencies": clean, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
    _path(gc_slug).write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return profile(gc_slug)


# ── Derived views ─────────────────────────────────────────────────────────────
def effective_agencies(gc_slug: Optional[str] = None) -> List[str]:
    """The agency codes to DISPLAY for this tenant — their saved set, or the
    default (all live brains) when unset. Used for nav + catalog flags, never
    for citation scoping."""
    active = get_active(gc_slug)
    return active if active is not None else list(_DEFAULT_DISPLAY)


def nav_for(gc_slug: Optional[str] = None) -> List[Dict[str, str]]:
    """The dynamic navigation for this tenant: the universal base tabs plus the
    agency-specific tabs for every enabled agency, in catalog order. This is what
    makes the console reshape itself the moment an agency is toggled."""
    tabs: List[Dict[str, str]] = list(BASE_TABS)
    enabled = set(effective_agencies(gc_slug))
    for a in AGENCY_CATALOG:
        if a["code"] in enabled:
            for t in a.get("tabs", []):
                tabs.append({**t, "agency": a["code"]})
    return tabs


def citation_authorities(gc_slug: Optional[str] = None) -> Optional[Set[str]]:
    """The set of citation authorities a tenant's photo-audit / resolver may
    surface. Returns None when the tenant has no saved profile — None means "do
    not scope" (every brain allowed), which preserves the exact pre-router
    behavior. Once a profile is saved, only enabled agencies' authorities pass;
    USACE contributes none (it drives the AHA form, not CFR citations).

    Note: FMCSA, PHMSA and FRA all resolve to the DOT authority (title 49);
    part-level separation is a later refinement. Enabling any one of them admits
    title-49 citations."""
    active = get_active(gc_slug)
    if active is None:
        return None  # unset → no scoping
    auths: Set[str] = set()
    for code in active:
        a = _BY_CODE.get(code)
        if a and a.get("authority"):
            auths.add(a["authority"])
    return auths


def catalog(gc_slug: Optional[str] = None) -> List[Dict[str, Any]]:
    """The full agency catalog, each entry flagged with whether this tenant has
    it enabled — everything the Screen-1 'Brain Router' toggle list needs."""
    enabled = set(effective_agencies(gc_slug))
    out: List[Dict[str, Any]] = []
    for a in AGENCY_CATALOG:
        out.append({
            "code": a["code"],
            "label": a["label"],
            "titles": a["titles"],
            "authority": a["authority"],
            "brain": a["brain"],
            "tabs": a["tabs"],
            "forms": a["forms"],
            "enabled": a["code"] in enabled,
        })
    return out


def profile(gc_slug: Optional[str] = None) -> Dict[str, Any]:
    """The tenant's full router state in one object: which agencies are on, the
    resulting nav, the citation-authority scope, whether a profile is saved yet,
    and the flagged catalog for the toggle UI."""
    active = get_active(gc_slug)
    return {
        "ok": True,
        "configured": active is not None,
        "active_agencies": effective_agencies(gc_slug),
        "nav": nav_for(gc_slug),
        "authorities": sorted(citation_authorities(gc_slug) or []),
        "scoped": citation_authorities(gc_slug) is not None,
        "agencies": catalog(gc_slug),
    }


# ── Routes (isolated + tenant-scoped, same pattern as every SIE module) ────────
def register_brain_router(app) -> None:
    from fastapi import Body, Request
    from fastapi.responses import JSONResponse

    def _scope(request):
        st = getattr(request, "state", None)
        return getattr(st, "sie_gc_slug", None)

    @app.get("/api/brain-router/catalog")
    def brain_router_catalog(request: Request):
        try:
            return {"ok": True, "agencies": catalog(_scope(request))}
        except Exception as exc:  # never 500 the tool
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/brain-router/profile")
    def brain_router_profile(request: Request):
        try:
            return profile(_scope(request))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.post("/api/brain-router/profile")
    async def brain_router_set_profile(request: Request, body: dict = Body(default=None)):
        payload = body if isinstance(body, dict) else {}
        if not payload:
            try:
                payload = await request.json()
            except Exception:
                payload = {}
        agencies = payload.get("agencies") if isinstance(payload, dict) else None
        if not isinstance(agencies, list):
            return JSONResponse(
                {"error": "Provide 'agencies': a list of agency codes "
                          f"from {VALID_CODES}."}, status_code=400)
        try:
            return set_active(agencies, gc_slug=_scope(request))
        except Exception as exc:
            return JSONResponse({"error": f"Could not save profile: {exc}"},
                                status_code=200)
