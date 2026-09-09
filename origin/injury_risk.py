"""Injury-Risk Ranking — the Safety Intelligence Engine's roster-triage layer
(Feature #5).

A general contractor doesn't want a wall of equally-loud subcontractor cards; it
wants the one most likely to hurt someone (or fail a client audit) to float to the
top. This module produces a **deterministic, explainable injury-risk score** for a
single subcontractor and ranks a whole GC roster worst-first from it.

The score is a 0–100 composite of four signal groups, each capped so no single
signal can dominate, and each **never-fabricate**: a signal that the data can't
support contributes zero and is honestly marked "not supplied / not scored" rather
than guessed. Same house rules as every other SIE module — deterministic, fully
offline (no LLM, no network), file-based via the engines it already leans on, and
isolated so a hiccup in one signal can never sink the score.

Signal groups and their caps (sum to 100):

  * Injury metrics (0–40) — TRIR / DART / EMR the sub self-supplies, benchmarked to
    industry-average. Origin never derives these; no number → that sub-factor is
    not scored.
  * Corrective actions + risk band (0–25) — company_profile.compute_risk (open /
    overdue CAPAs, existing deterministic risk band).
  * Prequal + document gaps (0–20) — missing mandated written programs and
    detected high-severity prequal gaps from prequal_engine.assess_all.
  * Expiring compliance (0–15) — expired / expiring COI, training, medical, OQ, EMR
    from compliance_health.health_for_sub.

Public entry points:

    score_sub(rec, *, company_id="", metrics=None)
        The composite score + per-group breakdown + drivers for one sub. Pure read.

    rank_roster(subs, *, bridge=None, metrics_of=None)
        Score every sub record in ``subs`` and return them worst-first with a
        rollup. ``bridge(rec) -> company_id`` maps a sub to its profile (optional);
        ``metrics_of(rec) -> dict`` supplies per-sub injury metrics (optional).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

# The engines this layer composes. All deterministic + offline; none import this
# module, so there is no circular-import risk.
from . import company_profile as _company
from . import prequal_engine as _prequal
from . import compliance_health as _health

# Same band thresholds as company_profile so "High" means the same thing across the
# whole product. Ordered high→low; first match wins.
RISK_BANDS = [
    (70.0, "Critical"),
    (40.0, "High"),
    (15.0, "Moderate"),
    (0.0, "Low"),
]

# Per-group caps (must sum to 100).
CAP_METRICS = 40.0
CAP_CAPA = 25.0
CAP_PREQUAL = 20.0
CAP_HEALTH = 15.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _band(score: float) -> str:
    for threshold, label in RISK_BANDS:
        if score >= threshold:
            return label
    return "Low"


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _num(v: Any) -> Optional[float]:
    """Parse a user-supplied metric to a float, or None. Strips a trailing '%' and
    stray whitespace. Never guesses — a blank / unparseable value is None so the
    sub-factor stays unscored (never-fabricate)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().rstrip("%").strip()
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


# ── the four signal groups (each returns points + supplied + drivers) ──────────
def _metrics_from(rec: Dict[str, Any], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Assemble the injury metrics for a sub, newest/most-explicit winning:
    an explicit ``override`` (a live prequal run) over the persisted
    ``prequal_metrics`` over the bare top-level ``trir`` / ``emr`` / ``dart``
    fields the roster row already carries. All user-supplied; nothing derived."""
    out: Dict[str, Any] = {}
    for k in ("trir", "dart", "emr"):
        if rec.get(k) not in (None, ""):
            out[k] = rec.get(k)
    pm = rec.get("prequal_metrics")
    if isinstance(pm, dict):
        for k in ("trir", "dart", "emr"):
            if pm.get(k) is not None:
                out[k] = pm.get(k)
    if isinstance(override, dict):
        for k in ("trir", "dart", "emr"):
            if override.get(k) is not None:
                out[k] = override.get(k)
    return out


def _score_metrics(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Injury metrics group (0–40). Each of EMR / TRIR / DART is scored only when
    supplied, benchmarked to industry-average; a missing number contributes 0 and
    is reported as not-scored. EMR 1.0 is the industry mean, so only above-average
    EMR adds points."""
    drivers: List[str] = []
    supplied: List[str] = []
    pts = 0.0

    emr = _num(metrics.get("emr"))
    if emr is not None:
        supplied.append("EMR")
        # Only above-average (>1.0) EMR is penalized; 1.33+ tops out the sub-cap.
        e_pts = _clamp((emr - 1.0) * 60.0, 0.0, 20.0)
        pts += e_pts
        if e_pts > 0:
            drivers.append(f"EMR {emr:g} is above the 1.0 industry average.")
        else:
            drivers.append(f"EMR {emr:g} is at or below the 1.0 industry average.")

    trir = _num(metrics.get("trir"))
    if trir is not None:
        supplied.append("TRIR")
        t_pts = _clamp(trir * 2.0, 0.0, 12.0)
        pts += t_pts
        if trir > 0:
            drivers.append(f"TRIR {trir:g} recordable rate.")

    dart = _num(metrics.get("dart"))
    if dart is not None:
        supplied.append("DART")
        d_pts = _clamp(dart * 2.7, 0.0, 8.0)
        pts += d_pts
        if dart > 0:
            drivers.append(f"DART {dart:g} days-away/restricted rate.")

    if not supplied:
        drivers.append("No injury metrics (EMR / TRIR / DART) supplied — not scored.")

    return {
        "group": "Injury metrics",
        "points": round(_clamp(pts, 0.0, CAP_METRICS), 1),
        "cap": CAP_METRICS,
        "supplied": bool(supplied),
        "supplied_fields": supplied,
        "drivers": drivers,
    }


def _score_capa(company_id: str) -> Dict[str, Any]:
    """Corrective-actions + risk-band group (0–25). Leans entirely on the existing
    deterministic risk engine so the number is consistent with the company's risk
    band everywhere else. No profile / no CAPAs → 0, honestly stated."""
    drivers: List[str] = []
    pts = 0.0
    band = None
    open_capas = overdue = 0
    if company_id:
        try:
            risk = _company.compute_risk(company_id)
            open_capas = int(risk.get("open_capas", 0) or 0)
            overdue = int(risk.get("overdue_capas", 0) or 0)
            band = risk.get("band")
            # Risk score is 0–100; take a fifth of it (→ 0–20) and add an overdue
            # surcharge up to 5. Together capped at 25.
            pts = _clamp(float(risk.get("score", 0) or 0) * 0.20, 0.0, 20.0)
            if overdue:
                pts = _clamp(pts + min(5.0, overdue * 2.5), 0.0, CAP_CAPA)
            if open_capas:
                drivers.append(f"{open_capas} open corrective action(s); risk band {band}.")
                if overdue:
                    drivers.append(f"{overdue} past the OSHA abatement deadline.")
            else:
                drivers.append("No open corrective actions on file.")
            if risk.get("human_review_open"):
                drivers.append("A finding is flagged for human review.")
        except Exception:
            drivers.append("Corrective-action risk could not be read — not scored.")
    else:
        drivers.append("No company profile bridged — corrective-action risk not scored.")

    return {
        "group": "Corrective actions + risk band",
        "points": round(_clamp(pts, 0.0, CAP_CAPA), 1),
        "cap": CAP_CAPA,
        "supplied": company_id != "" and band is not None,
        "band": band,
        "open_capas": open_capas,
        "overdue_capas": overdue,
        "drivers": drivers,
    }


def _score_prequal(company_id: str, metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Prequal + document-gaps group (0–20). Counts the mandated written programs
    the library still can't supply and the detected high-severity prequal gaps
    across the platforms the sub's clients mandate. Origin-detected only — the
    self-supply checklist items are NOT counted here (that's the sub's homework,
    not a measured risk)."""
    drivers: List[str] = []
    pts = 0.0
    missing_programs = 0
    detected_high = 0
    if company_id:
        try:
            out = _prequal.assess_all(company_id, metrics) or {}
            seen_gap_ids = set()
            for a in out.get("assessments", []) or []:
                posture = a.get("posture", {})
                missing_programs = max(missing_programs,
                                       int(posture.get("programs_missing", 0) or 0))
                for g in a.get("gaps", []) or []:
                    if g.get("status") == "detected" and g.get("severity") == "high":
                        seen_gap_ids.add(g.get("id", ""))
            detected_high = len(seen_gap_ids)
            pts = _clamp(missing_programs * 4.0 + detected_high * 3.0, 0.0, CAP_PREQUAL)
            if missing_programs:
                drivers.append(f"{missing_programs} mandated written program(s) not yet assembled.")
            if detected_high:
                drivers.append(f"{detected_high} detected high-severity prequal gap(s).")
            if not missing_programs and not detected_high:
                drivers.append("No detected prequal deficiencies.")
        except Exception:
            drivers.append("Prequal readiness could not be read — not scored.")
    else:
        drivers.append("No company profile bridged — prequal gaps not scored.")

    return {
        "group": "Prequal + document gaps",
        "points": round(_clamp(pts, 0.0, CAP_PREQUAL), 1),
        "cap": CAP_PREQUAL,
        "supplied": company_id != "",
        "programs_missing": missing_programs,
        "detected_high_gaps": detected_high,
        "drivers": drivers,
    }


def _score_health(rec: Dict[str, Any], company_id: str) -> Dict[str, Any]:
    """Expiring-compliance group (0–15). Expired items weigh more than expiring
    ones. Reads the compliance-health feed, which only surfaces items that carry a
    real date on file — never-fabricate."""
    drivers: List[str] = []
    pts = 0.0
    expired = expiring = 0
    try:
        feed = _health.health_for_sub(rec, company_id=company_id)
        counts = feed.get("counts", {})
        expired = int(counts.get("expired", 0) or 0)
        expiring = int(counts.get("expiring", 0) or 0)
        pts = _clamp(expired * 5.0 + expiring * 2.0, 0.0, CAP_HEALTH)
        if expired:
            drivers.append(f"{expired} expired compliance item(s) (COI / training / medical / OQ / EMR).")
        if expiring:
            drivers.append(f"{expiring} item(s) expiring within 30 days.")
        if not expired and not expiring:
            drivers.append("Nothing dated is expired or expiring soon.")
    except Exception:
        drivers.append("Compliance-health feed could not be read — not scored.")

    return {
        "group": "Expiring compliance",
        "points": round(_clamp(pts, 0.0, CAP_HEALTH), 1),
        "cap": CAP_HEALTH,
        "supplied": True,
        "expired": expired,
        "expiring": expiring,
        "drivers": drivers,
    }


# ── the composite ─────────────────────────────────────────────────────────────
def score_sub(rec: Dict[str, Any], *, company_id: str = "",
              metrics: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Injury-risk score (0–100) + explainable breakdown for one subcontractor.
    Deterministic and offline. ``company_id`` is the bridged company_profile id
    (from portal._ensure_profile_for_sub); without it the CAPA/prequal groups stay
    unscored but the metrics + expiring-compliance groups still count. ``metrics``
    optionally overrides the injury metrics read from the record."""
    cid = (company_id or "").strip()
    m = _metrics_from(rec, metrics)

    groups = [
        _score_metrics(m),
        _score_capa(cid),
        _score_prequal(cid, m),
        _score_health(rec, cid),
    ]
    score = round(_clamp(sum(g["points"] for g in groups), 0.0, 100.0), 1)
    band = _band(score)

    # The one-line "why it's ranked here" — the biggest contributing group first.
    top = max(groups, key=lambda g: g["points"])
    if score <= 0:
        headline = "No measured injury-risk signals."
    else:
        headline = f"Top driver: {top['group'].lower()} ({top['points']:g} pts)."

    return {
        "ok": True,
        "sub": rec.get("slug", ""),
        "company": rec.get("company", ""),
        "company_id": cid,
        "score": score,
        "band": band,
        "headline": headline,
        "groups": groups,
        "metrics_used": m,
        "scored_at": _now(),
        "method": "Deterministic 0–100 composite: injury metrics (\u226440) + "
                  "corrective-actions/risk (\u226425) + prequal/document gaps (\u226420) + "
                  "expiring compliance (\u226415). Each signal never-fabricate: unsupplied "
                  "data scores 0 and is marked not-scored. Bands match company risk: "
                  "Critical\u226570 / High\u226540 / Moderate\u226515 / Low.",
    }


def rank_roster(subs: List[Dict[str, Any]], *,
                bridge: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
                metrics_of: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
                ) -> Dict[str, Any]:
    """Score every subcontractor record in ``subs`` and return them worst-first.

    ``bridge(rec) -> company_id`` maps a sub to its company_profile id so the
    CAPA + prequal groups can score (portal passes ``_ensure_profile_for_sub``).
    ``metrics_of(rec) -> dict`` optionally supplies per-sub injury metrics beyond
    what the record already carries. Both are optional; the ranking degrades
    gracefully to the signals it can compute. Deterministic + offline."""
    rows: List[Dict[str, Any]] = []
    for rec in subs or []:
        if not isinstance(rec, dict):
            continue
        try:
            cid = ""
            if bridge is not None:
                cid = (bridge(rec) or "").strip()
            met = metrics_of(rec) if metrics_of is not None else None
            s = score_sub(rec, company_id=cid, metrics=met)
        except Exception:
            # A single un-scoreable sub must never sink the roster ranking.
            s = {"ok": False, "sub": rec.get("slug", ""), "company": rec.get("company", ""),
                 "company_id": "", "score": 0.0, "band": "Low",
                 "headline": "Could not score this subcontractor.", "groups": [],
                 "metrics_used": {}}
        rows.append(s)

    order = {"Critical": 0, "High": 1, "Moderate": 2, "Low": 3}
    rows.sort(key=lambda r: (order.get(r.get("band"), 9), -float(r.get("score", 0) or 0),
                             (r.get("company") or "").lower()))
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    band_counts: Dict[str, int] = {"Critical": 0, "High": 0, "Moderate": 0, "Low": 0}
    for r in rows:
        band_counts[r.get("band", "Low")] = band_counts.get(r.get("band", "Low"), 0) + 1

    return {
        "ok": True,
        "count": len(rows),
        "worst": rows[0] if rows else None,
        "band_counts": band_counts,
        "ranked": rows,
        "generated_at": _now(),
    }
