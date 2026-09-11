"""FMCSA carrier one-pager — pulls the SAFER Company Snapshot and CSA/SMS BASICs
for a single USDOT number from FMCSA's free QCMobile API and turns them into a
call-ready one-pager with a drafted corrective-action plan.

Why this exists
---------------
When a rated (Unsatisfactory/Conditional) or alerted carrier says "ok, take a
look," Origin needs to walk in already knowing the carrier's exact story: fleet
size, rating + date, inspection/out-of-service/crash history, and which of the
seven BASICs are in alert. QCMobile is the same public data the SAFER website
shows, but as JSON, so the app can assemble the whole picture from just the DOT
number instead of the owner copy-pasting screenshots.

Key facts baked in
------------------
* QCMobile needs a free "web key" (register at https://mobile.fmcsa.dot.gov).
  Set it on Railway as FMCSA_WEB_KEY. Without it every call 403s — the module
  degrades gracefully and tells the caller the key is missing.
* Per the FAST Act (2015), for PROPERTY carriers the Crash Indicator and Hazmat
  Compliance BASICs are HIDDEN from the public API — visible only to the carrier
  behind their FMCSA Portal PIN. We surface those two as "not public" rather than
  pretending they're clean. Passenger carriers show all seven.
* The corrective-action plan is deterministic (no external LLM): each alerted
  BASIC maps to the written program / process Origin already builds. Never
  fabricates numbers — every figure comes straight from the API payload.

The network calls only work from a host with outbound internet (Railway), not
the build sandbox. Every function is defensive and never raises to the caller.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

# Reuse the hardened, never-raises HTTP helpers from leadradar.
try:
    from . import leadradar as _lr
except Exception:  # pragma: no cover - allow flat import in tools/tests
    import leadradar as _lr  # type: ignore


# QCMobile base. Override-able so a future endpoint move doesn't require a code
# change. The web key is passed as the ?webKey= query parameter.
QCMOBILE_BASE = os.environ.get(
    "QCMOBILE_BASE", "https://mobile.fmcsa.dot.gov/qc/services").strip().rstrip("/")


def _web_key() -> str:
    return (os.environ.get("FMCSA_WEB_KEY", "") or "").strip()


# ---------------------------------------------------------------------------
# BASIC catalog — the seven CSA/SMS categories, in the order FMCSA lists them,
# each mapped to the Origin remediation focus and the written program that fixes
# it. `public_property` = False means the BASIC is hidden for property carriers
# without the carrier's Portal PIN (FAST Act).
# ---------------------------------------------------------------------------

_BASICS: List[Dict[str, Any]] = [
    {"code": "UnsafeDriving", "name": "Unsafe Driving", "public_property": True,
     "fix": "Driver discipline + speed/seat-belt/mobile-device policy, coaching log",
     "program": "Driver Safety & Discipline Policy (49 CFR 392)"},
    {"code": "HoursOfService", "name": "Hours-of-Service (Fatigued Driving)",
     "public_property": True,
     "fix": "ELD/RODS audit, HOS training, supporting-document retention",
     "program": "Hours-of-Service & ELD Compliance Program (49 CFR 395)"},
    {"code": "DriverFitness", "name": "Driver Fitness", "public_property": True,
     "fix": "Driver Qualification file rebuild (MVR, medical card, road test)",
     "program": "Driver Qualification File Program (49 CFR 391)"},
    {"code": "ControlledSubstances", "name": "Controlled Substances / Alcohol",
     "public_property": True,
     "fix": "Random pool enrollment, Clearinghouse queries, RTD process",
     "program": "Drug & Alcohol Testing Program (49 CFR 382 + Clearinghouse)"},
    {"code": "VehicleMaintenance", "name": "Vehicle Maintenance",
     "public_property": True,
     "fix": "DVIR discipline, PM schedule, annual inspection file",
     "program": "Vehicle Maintenance & Inspection Program (49 CFR 396)"},
    {"code": "HazmatCompliance", "name": "Hazmat Compliance",
     "public_property": False,
     "fix": "Hazmat handling, placarding, security plan (if applicable)",
     "program": "Hazmat Compliance Program (49 CFR 397 / 172)"},
    {"code": "CrashIndicator", "name": "Crash Indicator",
     "public_property": False,
     "fix": "Crash register + preventability review, accident countermeasures",
     "program": "Accident Register & Review Program (49 CFR 390.15)"},
]

_BASIC_BY_CODE = {b["code"]: b for b in _BASICS}

_RATING_LABEL = {
    "U": "Unsatisfactory", "C": "Conditional", "S": "Satisfactory",
    "": "Unrated / None",
}


def _pick(d: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", "null"):
            return str(v).strip()
    return ""


def _num(v: Any) -> Optional[float]:
    try:
        if v in (None, "", "null"):
            return None
        return float(v)
    except Exception:
        return None


def _get_json(path: str) -> Optional[Any]:
    key = _web_key()
    if not key:
        return None
    url = f"{QCMOBILE_BASE}/{path}?webKey={key}"
    return _lr._http_get_json(url, timeout=30)


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------

def carrier_snapshot(dot: str) -> Dict[str, Any]:
    """Return the SAFER-equivalent identity + rating + fleet + 24-month summary
    for a USDOT number. Shape is stable even when the API is unreachable."""
    dot = (dot or "").strip()
    out: Dict[str, Any] = {
        "ok": False, "dot": dot, "reason": "", "identity": {}, "rating": {},
        "fleet": {}, "safety": {},
    }
    if not dot.isdigit():
        out["reason"] = "invalid_dot"
        return out
    if not _web_key():
        out["reason"] = "no_web_key"
        out["note"] = ("FMCSA_WEB_KEY is not set. Register a free web key at "
                       "mobile.fmcsa.dot.gov and set it on Railway.")
        return out
    data = _get_json(f"carriers/{dot}")
    # QCMobile wraps the payload as {"content": {"carrier": {...}}}
    carrier = {}
    if isinstance(data, dict):
        content = data.get("content")
        if isinstance(content, dict):
            carrier = content.get("carrier") or content
        elif isinstance(content, list) and content:
            carrier = (content[0] or {}).get("carrier", content[0])
    if not isinstance(carrier, dict) or not carrier:
        out["reason"] = "unreachable_or_empty"
        out["note"] = "FMCSA QCMobile returned no carrier for this DOT."
        return out

    rating = _pick(carrier, "safetyRating").upper()[:1]
    out["identity"] = {
        "legal_name": _pick(carrier, "legalName"),
        "dba_name": _pick(carrier, "dbaName"),
        "dot": dot,
        "mc": _pick(carrier, "mcs150FormDate") and "",  # not always present
        "phone": _pick(carrier, "phone", "telephone"),
        "address": ", ".join([p for p in [
            _pick(carrier, "phyStreet"),
            ", ".join([x for x in [_pick(carrier, "phyCity"),
                                    _pick(carrier, "phyState"),
                                    _pick(carrier, "phyZipcode", "phyZip")] if x]),
        ] if p]),
    }
    out["rating"] = {
        "code": rating,
        "label": _RATING_LABEL.get(rating, "Unrated / None"),
        "rating_date": _pick(carrier, "safetyRatingDate"),
        "review_date": _pick(carrier, "reviewDate"),
        # Persistence insight: a U/C rating does NOT expire — it stands until the
        # carrier corrects and formally requests an upgrade. Surface that so the
        # owner knows an old date is still a live problem.
        "still_active_if_rated": rating in ("U", "C"),
    }
    out["fleet"] = {
        "power_units": _pick(carrier, "totalPowerUnits", "powerUnits"),
        "drivers": _pick(carrier, "totalDrivers", "driverTotal"),
        "operation": _pick(carrier, "carrierOperation", "operation"),
    }
    out["safety"] = {
        "driver_insp": _pick(carrier, "driverInsp"),
        "driver_oos": _pick(carrier, "driverOosInsp"),
        "driver_oos_rate": _pick(carrier, "driverOosRate"),
        "driver_oos_natl_avg": _pick(carrier, "driverOosRateNationalAverage"),
        "vehicle_insp": _pick(carrier, "vehicleInsp"),
        "vehicle_oos": _pick(carrier, "vehicleOosInsp"),
        "vehicle_oos_rate": _pick(carrier, "vehicleOosRate"),
        "vehicle_oos_natl_avg": _pick(carrier, "vehicleOosRateNationalAverage"),
        "crash_total": _pick(carrier, "crashTotal"),
        "fatal_crash": _pick(carrier, "fatalCrash"),
        "injury_crash": _pick(carrier, "injCrash"),
        "tow_crash": _pick(carrier, "towawayCrash"),
    }
    out["ok"] = True
    return out


def carrier_basics(dot: str) -> Dict[str, Any]:
    """Return the CSA/SMS BASICs for a USDOT number, flagging which are in alert
    and which are non-public (property-carrier Crash/Hazmat)."""
    dot = (dot or "").strip()
    out: Dict[str, Any] = {"ok": False, "dot": dot, "reason": "", "basics": []}
    if not dot.isdigit():
        out["reason"] = "invalid_dot"
        return out
    if not _web_key():
        out["reason"] = "no_web_key"
        return out
    data = _get_json(f"carriers/{dot}/basics")
    rows: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        content = data.get("content")
        if isinstance(content, list):
            rows = [r for r in content if isinstance(r, dict)]
        elif isinstance(content, dict):
            rows = [content]

    # Index API rows by BASIC code so we can present all seven in a stable order.
    api_by_code: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        b = r.get("basic") if isinstance(r.get("basic"), dict) else r
        bt = b.get("basicsType") if isinstance(b.get("basicsType"), dict) else {}
        code = _pick(bt, "basicsCode") or _pick(b, "basicsCode", "code")
        if code:
            api_by_code[code] = b

    result: List[Dict[str, Any]] = []
    for spec in _BASICS:
        b = api_by_code.get(spec["code"], {})
        alert = str(_pick(b, "exceededFMCSAInterventionThreshold",
                          "overThreshold", "onRoadPerformanceThresholdViolationInd")
                    ).upper().startswith("Y")
        percentile = _pick(b, "basicsPercentile", "percentile")
        has_data = bool(b)
        public = spec["public_property"] or has_data
        result.append({
            "code": spec["code"],
            "name": spec["name"],
            "public": public,
            "alert": alert if has_data else False,
            "percentile": percentile,
            "measure": _pick(b, "measureValue", "basicsRunDate") and _pick(b, "measureValue"),
            "serious_violation": str(_pick(b, "seriousViolationFromInspNum")
                                     ).upper().startswith("Y"),
            "fix": spec["fix"],
            "program": spec["program"],
            "note": ("" if public else
                     "Hidden for property carriers — visible only with the "
                     "carrier's FMCSA Portal PIN."),
        })
    out["basics"] = result
    out["ok"] = True
    return out


def _corrective_plan(snap: Dict[str, Any], basics: Dict[str, Any]) -> List[Dict[str, str]]:
    """Deterministic corrective-action plan. Priority order:
       1) every BASIC in alert (highest first by list order),
       2) the driver/vehicle OOS rates if they beat the national average,
       3) if rated U/C, the formal rating-upgrade request as the closing step."""
    plan: List[Dict[str, str]] = []
    for b in (basics.get("basics") or []):
        if b.get("alert"):
            plan.append({
                "trigger": f"{b['name']} BASIC in alert"
                           + (f" ({b['percentile']} percentile)" if b.get("percentile") else ""),
                "action": b["fix"],
                "deliverable": b["program"],
            })
    safety = snap.get("safety") or {}
    dr, dn = _num(safety.get("driver_oos_rate")), _num(safety.get("driver_oos_natl_avg"))
    if dr is not None and dn is not None and dr > dn:
        plan.append({
            "trigger": f"Driver out-of-service rate {dr:g}% vs {dn:g}% national avg",
            "action": "DQ-file + HOS coaching to pull the driver OOS rate below average",
            "deliverable": "Driver Qualification & HOS corrective package",
        })
    vr, vn = _num(safety.get("vehicle_oos_rate")), _num(safety.get("vehicle_oos_natl_avg"))
    if vr is not None and vn is not None and vr > vn:
        plan.append({
            "trigger": f"Vehicle out-of-service rate {vr:g}% vs {vn:g}% national avg",
            "action": "DVIR discipline + PM schedule to pull the vehicle OOS rate below average",
            "deliverable": "Vehicle Maintenance corrective package",
        })
    rating = (snap.get("rating") or {})
    if rating.get("code") in ("U", "C"):
        plan.append({
            "trigger": f"{rating.get('label')} safety rating dated "
                       f"{rating.get('rating_date') or 'unknown'} (still active until upgraded)",
            "action": "Assemble the corrective-action evidence and file a formal "
                      "Request for Change to Safety Rating (upgrade) with FMCSA",
            "deliverable": "Safety-rating upgrade petition + evidence packet",
        })
    if not plan:
        plan.append({
            "trigger": "No public BASIC in alert and no rating deficiency found",
            "action": "Confirm DQ files, DVIRs, drug/alcohol pool, and ELD records "
                      "are audit-ready before the next compliance review",
            "deliverable": "Preventive compliance file audit",
        })
    return plan


def one_pager(dot: str) -> Dict[str, Any]:
    """Full call-ready one-pager for a USDOT number: identity, rating, fleet,
    24-month safety summary, BASICs breakdown, and a drafted corrective-action
    plan. Never raises."""
    try:
        snap = carrier_snapshot(dot)
        basics = carrier_basics(dot)
        ok = bool(snap.get("ok"))
        result = {
            "ok": ok,
            "dot": (dot or "").strip(),
            "identity": snap.get("identity", {}),
            "rating": snap.get("rating", {}),
            "fleet": snap.get("fleet", {}),
            "safety": snap.get("safety", {}),
            "basics": basics.get("basics", []),
            "corrective_plan": _corrective_plan(snap, basics),
            "links": {
                "safer": (f"https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY&"
                          f"query_type=queryCarrierSnapshot&query_param=USDOT&"
                          f"query_string={(dot or '').strip()}"),
                "sms": f"https://ai.fmcsa.dot.gov/SMS/Carrier/{(dot or '').strip()}/Overview.aspx",
            },
        }
        if not ok:
            result["reason"] = snap.get("reason", "")
            result["note"] = snap.get("note", "")
        return result
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "dot": (dot or "").strip(), "reason": "error",
                "note": str(exc)[:160], "corrective_plan": [], "basics": []}


# ---------------------------------------------------------------------------
# Route registration — isolated + non-fatal, mirroring the other engines.
# ---------------------------------------------------------------------------

def register_fmcsa_snapshot(app) -> None:
    from fastapi.responses import JSONResponse

    @app.get("/api/fmcsa/snapshot/{dot}")
    def fmcsa_one_pager(dot: str):
        try:
            return one_pager(dot)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=200)

    @app.get("/api/fmcsa/config")
    def fmcsa_config():
        return {"ok": True, "web_key_configured": bool(_web_key())}
