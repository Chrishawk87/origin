"""Training Intelligence — the Safety Intelligence Engine's live training layer
(Stage 6).

Training used to be KB-static: the program package could hand a company the OSHA
training *requirements* that apply to it, but nothing tracked WHO on the roster has
actually been trained, on WHAT, or WHEN it expires. Stage 6 makes training a living
matrix: employee/role -> required training (derived from the requirements engine) ->
assignment -> completion -> expiration, with expiration driving monitor alerts.

Trust story, identical to every other SIE stage:

  * The CATALOG is DERIVED, never a new source of truth. The set of courses a
    company needs is resolved on demand from `requirements_engine` (the Stage 2
    "what applies and why" layer) filtered to the standards where
    `compliance_kb.training_requirement()` confirms OSHA actually imposes a training
    obligation. If the package has no training record for a standard, there is no
    course — we never fabricate a training duty. Delete nothing; recompute from the
    profile + KB and the same catalog comes back.

  * The one thing this engine PERSISTS is the net-new, authoritative datum no other
    collection holds: the roster (employees + roles) and their completion records
    (who was trained on what, and when). That is the human/live fact the rest of the
    system can't derive.

  * Expiration is DETERMINISTIC. A small curated table of refresher cadences
    (`REFRESHER_MONTHS`) carries only the well-documented periodic OSHA training
    calendars (e.g. annual respirator/bloodborne/HAZWOPER refresher, triennial
    powered-industrial-truck re-evaluation). Every other course has NO fixed cadence
    (initial / as-needed / on-change) and therefore can NEVER produce a false
    "expired" alert — absence of a cadence means the completion stays current.

Dependency direction is training -> requirements/KB/company (never the reverse), so
a bug here cannot break scoping, the package, or recording a company. Same house
rules: file-based on the persistent volume, fully deterministic and offline
(consults no model), never fabricates, isolated + non-fatal registration.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Module-level so FastAPI can resolve the `request: Request` annotation under
# `from __future__ import annotations` (see review_engine.py for the full note).
try:  # pragma: no cover - trivial import guard
    from fastapi import Request
except Exception:  # pragma: no cover
    Request = None  # type: ignore

from . import requirements_engine
from . import compliance_kb

try:
    from .paths import DATA_DIR
except ImportError:  # bare import in ad-hoc scripts
    import os as _os
    DATA_DIR = Path(_os.environ.get("ORIGIN_DATA_DIR") or (Path.home() / ".origin"))

TRAINING_DIR = DATA_DIR / "training"

# How soon (days) before an expiration date a completion is flagged "expiring".
DUE_SOON_DAYS = 30

# ── curated refresher cadences (months) ──────────────────────────────────────
# Keyed by bare CFR section. ONLY the well-documented periodic OSHA training
# calendars live here. A section absent from this table (or mapped to None) has no
# fixed retraining cadence — it is initial / as-needed / on-change — so a completed
# course for it stays "current" forever and never raises a false expiration alert.
# Sourced from the standards themselves; conservative on purpose.
REFRESHER_MONTHS: Dict[str, Optional[int]] = {
    "1910.95": 12,      # Occupational noise — annual training for exposed employees
    "1910.120": 12,     # HAZWOPER — annual 8-hr refresher
    "1910.134": 12,     # Respiratory protection — annual training + fit test
    "1910.157": 12,     # Portable fire extinguishers — annual training
    "1910.1030": 12,    # Bloodborne pathogens — annual training
    "1910.178": 36,     # Powered industrial trucks — re-evaluation at least every 3 yr
}

STATUS_MISSING = "missing"
STATUS_CURRENT = "current"
STATUS_EXPIRING = "expiring"
STATUS_EXPIRED = "expired"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or "x"


def _ensure_dir() -> None:
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)


# ── CFR section extraction ────────────────────────────────────────────────────
_SECTION = re.compile(r"\b(19\d\d\.\d+[A-Za-z]?)\b")


def _sections(citation: str) -> List[str]:
    """Every CFR section number in a (possibly compound) citation string, e.g.
    '29 CFR 1910.146 / 1926 Subpart AA' -> ['1910.146']. Deterministic."""
    return _SECTION.findall(citation or "")


def _add_months(d: date, months: int) -> date:
    """Add whole months to a date without external deps (calendar-safe)."""
    m0 = d.month - 1 + months
    y = d.year + m0 // 12
    m = m0 % 12 + 1
    # clamp the day to the last valid day of the target month
    import calendar
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def _parse_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


# ── catalog (DERIVED from requirements_engine + KB training obligations) ──────
def _course_id(cid: str, section: str) -> str:
    return f"{cid}::train:{_slug(section)}"


def catalog_from_profile(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the training courses a company needs, derived and fully sourced.

    Walks the company's classified requirement set (Stage 2), and for every CFR
    section it cites, keeps the course ONLY IF `compliance_kb.training_requirement()`
    confirms OSHA imposes a training obligation for that section. Each course carries
    the citation, the requirement's applicability reason, the classification, and the
    refresher cadence (or None = initial/as-needed). Never fabricates a duty.
    """
    data = requirements_engine.requirements_from_profile(rec)
    cid = data["company_id"]
    seen: Dict[str, Dict[str, Any]] = {}

    for r in data.get("requirements", []):
        for sec in _sections(r.get("citation", "")):
            tr = compliance_kb.training_requirement(sec)
            if not tr:
                continue  # no KB training obligation for this section -> no course
            if sec in seen:
                continue
            months = REFRESHER_MONTHS.get(sec)
            seen[sec] = {
                "course_id": _course_id(cid, sec),
                "company_id": cid,
                "section": sec,
                "title": tr.get("standard_title") or r.get("title") or sec,
                "citation": r.get("citation") or tr.get("citation") or f"29 CFR {sec}",
                "classification": r.get("classification", ""),
                "classification_label": r.get("classification_label", ""),
                "category": r.get("category", ""),
                "why": r.get("why", ""),
                "refresher_months": months,
                "cadence": _cadence_label(months),
                "part_name": tr.get("part_name", ""),
                "source": tr.get("source", ""),
            }

    courses = sorted(seen.values(), key=lambda c: c["section"])
    return {
        "company_id": cid,
        "company": data.get("company", ""),
        "courses": courses,
        "course_count": len(courses),
        "jurisdiction": data.get("jurisdiction"),
    }


def _cadence_label(months: Optional[int]) -> str:
    if not months:
        return "Initial / as-needed"
    if months == 12:
        return "Annual"
    if months == 36:
        return "Every 3 years"
    if months % 12 == 0:
        return f"Every {months // 12} years"
    return f"Every {months} months"


def catalog_for(company_id: str) -> Optional[Dict[str, Any]]:
    from . import company_profile as cp
    rec = cp.get(company_id)
    if not rec:
        return None
    return catalog_from_profile(rec)


# ── roster persistence (the authoritative net-new data) ──────────────────────
def _employee_path(eid: str) -> Path:
    return TRAINING_DIR / f"{(eid or '').strip()}.json"


def _employee_id(company_id: str, name: str) -> str:
    return f"emp-{_slug(company_id)}-{_slug(name)}"


def save_employee(rec: Dict[str, Any]) -> str:
    _ensure_dir()
    _employee_path(rec["employee_id"]).write_text(
        json.dumps(rec, indent=2), encoding="utf-8")
    return rec["employee_id"]


def get_employee(eid: str) -> Optional[Dict[str, Any]]:
    p = _employee_path(eid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_employees() -> List[Dict[str, Any]]:
    if not TRAINING_DIR.exists():
        return []
    out: List[Dict[str, Any]] = []
    for p in TRAINING_DIR.glob("emp-*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def list_employees(company_id: str) -> List[Dict[str, Any]]:
    cid = _slug(company_id)
    rows = [e for e in _load_employees() if _slug(e.get("company_id", "")) == cid]
    rows.sort(key=lambda e: (0 if e.get("active", True) else 1,
                             (e.get("name") or "").lower()))
    return rows


def add_employee(company_id: str, name: str, *, role: str = "",
                 company: str = "", by: str = "owner") -> Dict[str, Any]:
    """Insert-or-update one roster member. Idempotent by stable id (company+name),
    so re-adding the same person updates their role instead of duplicating them."""
    cid = (company_id or "").strip()
    eid = _employee_id(cid, name)
    rec = get_employee(eid) or {
        "employee_id": eid,
        "company_id": cid,
        "completions": {},
        "created_at": _now(),
    }
    rec.update({
        "name": (name or "").strip(),
        "role": (role or "").strip(),
        "company": company or rec.get("company", ""),
        "active": True,
        "updated_at": _now(),
        "updated_by": by,
    })
    rec.setdefault("completions", {})
    save_employee(rec)
    return rec


def deactivate_employee(eid: str, *, by: str = "owner") -> Optional[Dict[str, Any]]:
    rec = get_employee(eid)
    if not rec:
        return None
    rec["active"] = False
    rec["updated_at"] = _now()
    rec["updated_by"] = by
    save_employee(rec)
    return rec


def record_completion(eid: str, course_id: str, *, completed_on: str = "",
                      by: str = "owner", note: str = "") -> Optional[Dict[str, Any]]:
    """Log that an employee completed a course on a date (default: today). This is
    the one authoritative fact the engine owns; everything else is derived from it."""
    rec = get_employee(eid)
    if not rec:
        return None
    on = (completed_on or "").strip() or _today().isoformat()
    d = _parse_date(on)
    if not d:
        return None
    rec.setdefault("completions", {})[course_id] = {
        "completed_on": d.isoformat(),
        "recorded_by": by,
        "recorded_at": _now(),
        "note": (note or "").strip(),
    }
    rec["updated_at"] = _now()
    save_employee(rec)
    return rec


# ── deterministic status computation ──────────────────────────────────────────
def _status_for(completion: Optional[Dict[str, Any]],
                refresher_months: Optional[int]) -> Tuple[str, str, Optional[int]]:
    """(status, expires_on, days_left) for one employee-course pair.

    No completion            -> missing.
    Completed, no cadence     -> current forever (expires_on="", days_left=None).
    Completed, cadence set    -> current / expiring / expired vs today + refresher.
    """
    if not completion:
        return STATUS_MISSING, "", None
    if not refresher_months:
        return STATUS_CURRENT, "", None
    done = _parse_date(completion.get("completed_on", ""))
    if not done:
        return STATUS_CURRENT, "", None
    expires = _add_months(done, refresher_months)
    days_left = (expires - _today()).days
    if days_left < 0:
        st = STATUS_EXPIRED
    elif days_left <= DUE_SOON_DAYS:
        st = STATUS_EXPIRING
    else:
        st = STATUS_CURRENT
    return st, expires.isoformat(), days_left


def matrix_for(company_id: str) -> Optional[Dict[str, Any]]:
    """The full training matrix for a company: every active employee crossed with
    every applicable course, each cell carrying its deterministic status. Also
    returns the flat list of expired / expiring cells that monitoring consumes."""
    cat = catalog_for(company_id)
    if cat is None:
        return None
    courses = cat["courses"]
    employees = [e for e in list_employees(company_id) if e.get("active", True)]

    rows: List[Dict[str, Any]] = []
    tally = {STATUS_MISSING: 0, STATUS_CURRENT: 0,
             STATUS_EXPIRING: 0, STATUS_EXPIRED: 0}
    expired: List[Dict[str, Any]] = []
    expiring: List[Dict[str, Any]] = []

    for e in employees:
        comps = e.get("completions", {}) or {}
        cells: List[Dict[str, Any]] = []
        for c in courses:
            comp = comps.get(c["course_id"])
            st, exp_on, days_left = _status_for(comp, c.get("refresher_months"))
            tally[st] = tally.get(st, 0) + 1
            cell = {
                "course_id": c["course_id"],
                "section": c["section"],
                "title": c["title"],
                "citation": c["citation"],
                "cadence": c["cadence"],
                "status": st,
                "completed_on": (comp or {}).get("completed_on", ""),
                "expires_on": exp_on,
                "days_left": days_left,
            }
            cells.append(cell)
            if st == STATUS_EXPIRED:
                expired.append({"employee": e.get("name", ""),
                                "employee_id": e.get("employee_id", ""), **cell})
            elif st == STATUS_EXPIRING:
                expiring.append({"employee": e.get("name", ""),
                                 "employee_id": e.get("employee_id", ""), **cell})
        rows.append({
            "employee_id": e.get("employee_id", ""),
            "name": e.get("name", ""),
            "role": e.get("role", ""),
            "cells": cells,
        })

    return {
        "company_id": cat["company_id"],
        "company": cat["company"],
        "courses": courses,
        "course_count": len(courses),
        "employee_count": len(employees),
        "rows": rows,
        "tally": tally,
        "expired": expired,
        "expiring": expiring,
    }


def company_training_summary(company_id: str) -> Optional[Dict[str, Any]]:
    """Lightweight rollup for one company (counts only) — what monitoring and the
    console badge need without materializing the whole matrix payload."""
    m = matrix_for(company_id)
    if m is None:
        return None
    return {
        "company_id": m["company_id"],
        "company": m["company"],
        "course_count": m["course_count"],
        "employee_count": m["employee_count"],
        "tally": m["tally"],
        "expired_count": len(m["expired"]),
        "expiring_count": len(m["expiring"]),
    }


def portfolio_training(gc_slug: str = "") -> Dict[str, Any]:
    """Portfolio-wide training rollup across profiled companies. When ``gc_slug``
    is set, the rollup covers only that GC's own companies (Stage 3 tenant
    scoping) so a GC console never sees another tenant's roster."""
    from . import company_profile as cp
    companies = cp.list_all(gc_slug=gc_slug or None)
    out: List[Dict[str, Any]] = []
    tot_expired = tot_expiring = 0
    for rec in companies:
        cid = rec.get("company_id", "")
        if not cid:
            continue
        try:
            s = company_training_summary(cid)
        except Exception:
            s = None
        if not s:
            continue
        tot_expired += s["expired_count"]
        tot_expiring += s["expiring_count"]
        out.append(s)
    out.sort(key=lambda s: (-s["expired_count"], -s["expiring_count"]))
    return {
        "companies": out,
        "total_expired": tot_expired,
        "total_expiring": tot_expiring,
    }


# ── monitoring bridge (consumed by monitor_engine's sweep) ───────────────────
def training_alerts_for(company_id: str) -> List[Dict[str, Any]]:
    """Deterministic training alerts for one company, as neutral dicts the monitor
    turns into first-class alerts. One aggregated row per condition (expired /
    expiring) so a big roster can't flood the feed. Offline, never fabricates."""
    m = matrix_for(company_id)
    if m is None:
        return []
    out: List[Dict[str, Any]] = []
    if m["expired"]:
        names = ", ".join(sorted({x["employee"] for x in m["expired"]}))[:200]
        out.append({
            "rule": "training_expired",
            "part": "expired",
            "severity": "high",
            "title": f"{len(m['expired'])} required training(s) expired",
            "detail": f"Expired training for: {names}. "
                      "Periodic OSHA-required refresher training is past due.",
        })
    if m["expiring"]:
        names = ", ".join(sorted({x["employee"] for x in m["expiring"]}))[:200]
        out.append({
            "rule": "training_due_soon",
            "part": "expiring",
            "severity": "medium",
            "title": f"{len(m['expiring'])} required training(s) expiring within "
                     f"{DUE_SOON_DAYS} days",
            "detail": f"Refresher training due soon for: {names}.",
        })
    return out


# ── routes (gated by _auth under /api/*, isolated + non-fatal, owner-only) ────
def register_training(app) -> None:
    """Attach training-intelligence routes. Tenant-aware (Stage 3): an owner/admin
    session sees the whole portfolio; a logged-in GC sees only its own companies'
    training. The overview rollup filters by ``request.state.sie_gc_slug`` here,
    and the per-company / per-employee routes are ownership-gated in server.py's
    _auth (a GC can only reach a company or employee it owns). Isolated +
    non-fatal, fully offline. The catalog is derived on every call; only the
    roster + completions are persisted."""
    from fastapi import Body, Request
    from fastapi.responses import JSONResponse

    @app.get("/api/training/overview")
    def training_overview(request: Request):
        gc = (getattr(request.state, "sie_gc_slug", None) or "")
        try:
            return {"ok": True, **portfolio_training(gc_slug=gc)}
        except Exception as exc:  # never 500 the tool
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/training/{company_id}/catalog")
    def training_catalog(company_id: str):
        data = catalog_for(company_id)
        if data is None:
            return JSONResponse({"error": "company not found"}, status_code=404)
        return {"ok": True, **data}

    @app.get("/api/training/{company_id}/matrix")
    def training_matrix(company_id: str):
        data = matrix_for(company_id)
        if data is None:
            return JSONResponse({"error": "company not found"}, status_code=404)
        return {"ok": True, **data}

    @app.get("/api/training/{company_id}/summary")
    def training_summary(company_id: str):
        data = company_training_summary(company_id)
        if data is None:
            return JSONResponse({"error": "company not found"}, status_code=404)
        return {"ok": True, **data}

    @app.post("/api/training/{company_id}/employee")
    def training_add_employee(company_id: str, body: dict = Body(default=None)):
        payload = body if isinstance(body, dict) else {}
        name = (payload.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "name is required"}, status_code=400)
        try:
            rec = add_employee(company_id, name, role=payload.get("role", ""),
                               company=payload.get("company", ""),
                               by=payload.get("by", "owner"))
            return {"ok": True, "employee": rec}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.post("/api/training/employee/{eid}/complete")
    def training_complete(eid: str, body: dict = Body(default=None)):
        payload = body if isinstance(body, dict) else {}
        course_id = (payload.get("course_id") or "").strip()
        if not course_id:
            return JSONResponse({"error": "course_id is required"}, status_code=400)
        rec = record_completion(eid, course_id,
                                completed_on=payload.get("completed_on", ""),
                                by=payload.get("by", "owner"),
                                note=payload.get("note", ""))
        if rec is None:
            return JSONResponse({"error": "employee not found or bad date"},
                                status_code=404)
        return {"ok": True, "employee": rec}

    @app.post("/api/training/employee/{eid}/deactivate")
    def training_deactivate(eid: str, body: dict = Body(default=None)):
        payload = body if isinstance(body, dict) else {}
        rec = deactivate_employee(eid, by=payload.get("by", "owner"))
        if rec is None:
            return JSONResponse({"error": "employee not found"}, status_code=404)
        return {"ok": True, "employee": rec}
