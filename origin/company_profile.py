"""Company Profile + Risk Engine — the Safety Intelligence Engine's per-company
posture layer (Phase 2).

Phase 1 turns a single citation into a fully-sourced analysis. Phase 2 makes that
matter at the *company* level: it stores a versioned compliance profile for each
company, and it computes a **deterministic, explainable risk score** from that
company's open corrective actions (CAPAs).

Two responsibilities, one module (both are properties of a company):

  1. Profile — company identity + industry/NAICS/state/headcount/activities. The
     required-standard *set* is not stored redundantly here; it is delegated to
     the existing `scoping.scope_company()` resolver so there is exactly one place
     that knows what standards apply to a company.

  2. Risk — `compute_risk()` reads the company's open CAPAs (capa.py) and produces
     a 0–100 score, a band (Low / Moderate / High / Critical), and a list of the
     *drivers* behind the number. It is fully deterministic and rule-based: same
     CAPAs in → same score out. That driver list is what powers a truthful "WHY is
     this company High risk?" answer — no model guesses the score.

Design rules (identical to citation_engine.py / capa.py):
  * Deterministic, offline, never-fabricate. No LLM anywhere.
  * File-based on the persistent volume (ORIGIN_DATA_DIR/companies), one JSON per
    company. No database dependency.
  * Isolated + non-fatal registration, so a bug here can't break the live app.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import capa
from . import scoping

try:
    from .paths import DATA_DIR
except ImportError:  # bare import in ad-hoc scripts
    import os as _os
    DATA_DIR = Path(_os.environ.get("ORIGIN_DATA_DIR") or (Path.home() / ".origin"))

COMPANIES_DIR = DATA_DIR / "companies"

# Risk bands by score (0–100). Ordered high→low; first match wins.
RISK_BANDS = [
    (70.0, "Critical"),
    (40.0, "High"),
    (15.0, "Moderate"),
    (0.0, "Low"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "company"


def _ensure_dir() -> None:
    COMPANIES_DIR.mkdir(parents=True, exist_ok=True)


# ── profile CRUD ──────────────────────────────────────────────────────────────
def upsert(profile: Dict[str, Any], *, by: str = "owner") -> Dict[str, Any]:
    """Create or update a company profile. Keyed by a slug of the company name, so
    re-analyzing citations for the same company updates the one profile."""
    _ensure_dir()
    company = (profile.get("company") or "").strip()
    cid = _slug(profile.get("company_id") or company)
    existing = get(cid) or {}
    record = {
        "company_id": cid,
        "company": company or existing.get("company", ""),
        "industry": (profile.get("industry") or existing.get("industry") or "").strip(),
        "naics": (profile.get("naics") or existing.get("naics") or "").strip(),
        "state": (profile.get("state") or existing.get("state") or "").strip().upper(),
        "headcount": profile.get("headcount", existing.get("headcount")),
        "activities": profile.get("activities", existing.get("activities")) or {},
        "operators": profile.get("operators", existing.get("operators")) or [],
        "customer_platforms": profile.get("customer_platforms",
                                          existing.get("customer_platforms")) or [],
        "created_at": existing.get("created_at") or _now(),
        "updated_at": _now(),
        "updated_by": by,
    }
    path = COMPANIES_DIR / f"{cid}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def get(company_id: str) -> Optional[Dict[str, Any]]:
    path = COMPANIES_DIR / f"{_slug(company_id)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def ensure(company: str) -> Dict[str, Any]:
    """Get-or-create a bare profile for a company name. Used when a citation names
    a company that hasn't been profiled yet, so risk still has somewhere to land."""
    cid = _slug(company)
    rec = get(cid)
    if rec:
        return rec
    return upsert({"company": company}, by="system")


def list_all() -> List[Dict[str, Any]]:
    if not COMPANIES_DIR.exists():
        return []
    out: List[Dict[str, Any]] = []
    for p in COMPANIES_DIR.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    out.sort(key=lambda r: r.get("company", "").lower())
    return out


def scope(company_id: str) -> Optional[Dict[str, Any]]:
    """The company-specific required-standard set, via the existing resolver."""
    rec = get(company_id)
    if not rec:
        return None
    return scoping.scope_company({
        "company": rec.get("company", ""),
        "industry": rec.get("industry", ""),
        "naics": rec.get("naics", ""),
        "state": rec.get("state", ""),
        "headcount": rec.get("headcount"),
        "activities": rec.get("activities", {}),
        "operators": rec.get("operators", []),
    })


# ── the risk engine (deterministic + explainable) ─────────────────────────────
def _band(score: float) -> str:
    for threshold, label in RISK_BANDS:
        if score >= threshold:
            return label
    return "Low"


def _penalty_factor(amount: float) -> float:
    """A gentle, bounded multiplier from a proposed-penalty dollar figure. Log-scaled
    so a $50k penalty doesn't swamp everything, but bigger penalties still weigh more.
    $0 → 1.0, $5k → ~1.37, $15k → ~1.57, $70k → ~1.85, capped at 2.0."""
    if amount <= 0:
        return 1.0
    return min(2.0, 1.0 + math.log10(amount) / 10.0)


def compute_risk(company_id: str) -> Dict[str, Any]:
    """Score a company's current risk from its OPEN CAPAs. Returns the score, band,
    and the explicit drivers behind it. Deterministic — no model involved.

    Each open CAPA contributes:  severity_weight × penalty_factor(penalty).
    Overdue CAPAs add a flat surcharge each. Any CAPA still flagged for human review
    floors the band at High. The raw contribution total is scaled to a 0–100 score.
    """
    cid = _slug(company_id)
    open_capas = capa.list_open_for_company(cid)

    contributions: List[Dict[str, Any]] = []
    raw = 0.0
    overdue_count = 0
    review_flag = False

    for c in open_capas:
        sev = float(c.get("severity_weight") or capa.severity_weight(c.get("classification", "")))
        pf = _penalty_factor(float(c.get("penalty_amount") or 0.0))
        contrib = sev * pf
        overdue = capa.is_overdue(c)
        if overdue:
            overdue_count += 1
            contrib += 0.5
        if c.get("human_review_required"):
            review_flag = True
        raw += contrib
        contributions.append({
            "capa_id": c.get("id"),
            "title": c.get("title", "")[:120],
            "standard": c.get("standard", ""),
            "classification": c.get("classification", ""),
            "severity_weight": round(sev, 2),
            "penalty_factor": round(pf, 2),
            "overdue": overdue,
            "contribution": round(contrib, 2),
        })

    # Scale raw contribution total into a 0–100 score. One serious, on-time,
    # low-penalty CAPA (~0.6) lands ~12 (Low/Moderate edge); a willful overdue
    # high-penalty CAPA (~2.5) lands ~50 (High); several push toward Critical.
    score = min(100.0, raw * 20.0)
    band = _band(score)

    # A finding awaiting human review is never allowed to read as merely Low/Moderate.
    if review_flag and band in ("Low", "Moderate"):
        band = "High"

    contributions.sort(key=lambda x: x["contribution"], reverse=True)

    drivers: List[str] = []
    if not open_capas:
        drivers.append("No open corrective actions on file.")
    else:
        drivers.append(f"{len(open_capas)} open corrective action(s).")
        if overdue_count:
            drivers.append(f"{overdue_count} past its OSHA abatement deadline.")
        top = contributions[0]
        if top["classification"]:
            drivers.append(
                f"Highest-weighted item: {top['classification'].replace('_',' ')} "
                f"citation ({top['standard'] or 'standard'}).")
        if review_flag:
            drivers.append("At least one item is flagged for human review.")

    return {
        "company_id": cid,
        "score": round(score, 1),
        "band": band,
        "open_capas": len(open_capas),
        "overdue_capas": overdue_count,
        "human_review_open": review_flag,
        "drivers": drivers,
        "contributions": contributions,
        "computed_at": _now(),
        "method": "Deterministic: Σ(severity_weight × penalty_factor) + overdue surcharge, "
                  "scaled ×20, capped at 100; human-review floors band at High.",
    }


def company_view(company_id: str) -> Optional[Dict[str, Any]]:
    """Everything about a company in one place: profile + scope summary + risk +
    CAPA list. The payload a company dashboard row/drill-down renders from."""
    rec = get(company_id)
    if not rec:
        return None
    sc = scope(company_id) or {}
    return {
        "profile": rec,
        "scope": {
            "sector": sc.get("sector"),
            "sector_label": sc.get("sector_label"),
            "required_count": sc.get("required_count"),
            "triggered_count": sc.get("triggered_count"),
            "recordkeeping": sc.get("recordkeeping"),
        },
        "risk": compute_risk(company_id),
        "capas": [capa._view(c) for c in capa.list_for_company(company_id)],
    }


def portfolio() -> Dict[str, Any]:
    """Risk rollup across every profiled company, worst first."""
    rows: List[Dict[str, Any]] = []
    for p in list_all():
        cid = p.get("company_id", "")
        r = compute_risk(cid)
        rows.append({
            "company_id": cid,
            "company": p.get("company", ""),
            "industry": p.get("industry", ""),
            "state": p.get("state", ""),
            "score": r["score"],
            "band": r["band"],
            "open_capas": r["open_capas"],
            "overdue_capas": r["overdue_capas"],
        })
    order = {"Critical": 0, "High": 1, "Moderate": 2, "Low": 3}
    rows.sort(key=lambda x: (order.get(x["band"], 9), -x["score"]))
    return {"companies": rows, "total": len(rows)}


# ── routes ────────────────────────────────────────────────────────────────────
def register_company(app) -> None:
    """Attach Company Profile + Risk routes. Isolated + non-fatal, mirroring the
    other SIE modules."""
    from fastapi import Body
    from fastapi.responses import JSONResponse

    @app.get("/api/company/portfolio")
    def company_portfolio():
        try:
            return {"ok": True, **portfolio()}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/company/list")
    def company_list():
        return {"ok": True, "items": list_all()}

    @app.post("/api/company/upsert")
    def company_upsert(body: dict = Body(default=None)):
        payload = body if isinstance(body, dict) else {}
        if not payload.get("company"):
            return JSONResponse({"error": "company is required"}, status_code=400)
        try:
            rec = upsert(payload, by=payload.get("by", "owner"))
            return {"ok": True, "profile": rec}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/company/{company_id}")
    def company_get(company_id: str):
        view = company_view(company_id)
        if not view:
            return JSONResponse({"error": "not found"}, status_code=404)
        return view

    @app.get("/api/company/{company_id}/risk")
    def company_risk(company_id: str):
        if not get(company_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"ok": True, **compute_risk(company_id)}

    @app.get("/api/company/{company_id}/scope")
    def company_scope(company_id: str):
        sc = scope(company_id)
        if sc is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"ok": True, "scope": sc}
