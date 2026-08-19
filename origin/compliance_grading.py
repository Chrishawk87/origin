"""Prequal rating engine + safety-metrics calculator.

This is what turns Origin's compliance officer from a document drafter into an
AUDITOR and SAFETY SPECIALIST: it computes the OSHA safety metrics the prequal
platforms judge you on (TRIR / DART / LTIR / severity), benchmarks them against
BLS industry averages, and ESTIMATES the grade a contractor would earn on
ISNetworld / Avetta / Veriforce / PEC / BROWZ.

IMPORTANT — honesty about the model
-----------------------------------
The platforms do NOT publish their exact grade math, and every grade is
configured per hiring client. So this engine is a *research-grounded estimate*
built from each platform's published grading criteria and weightings, not the
proprietary formula. Every output is labelled "estimated" and every grade is
qualified with the drivers behind it so a human can sanity-check it.

The model is CALIBRATABLE: drop a JSON file at
``$ORIGIN_DATA_DIR/compliance_calibration.json`` (or record real scorecards via
the ``grade_calibrate`` tool) and the engine will use your real-world weights
and grade bands instead of the defaults.

Sources the defaults are grounded in (see the tools' output for citations):
  - OSHA recordkeeping 29 CFR 1904; incidence-rate formula (cases x 200,000 / hrs)
  - BLS industry TRIR/DART averages (2023-2024)
  - NCCI Experience Rating Plan (EMR, 1.00 baseline)
  - ISN RAVS weighting (written programs ~30-40% of grade; MSQ ~10-15%),
    A/B/C/F letter scale; Avetta green/amber/red + Safety Maturity Index
    (0-100 -> A-D); Veriforce A/B approved vs C/D/F.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

# The OSHA constant: hours worked by 100 full-time employees in a year
# (100 x 40 x 50). Every incidence rate normalizes to "per 100 FTE".
OSHA_HOURS_BASE = 200_000

# ── BLS industry benchmarks (approximate, 2023-2024) ────────────────────────
# TRIR/DART "per 100 FTE" averages by sector. These move a little each year and
# vary by exact NAICS + company size; treat as a benchmark, not a hard cutoff.
# Private-industry-all is the safe default when the sector is unknown.
_BLS: Dict[str, Dict[str, Any]] = {
    "all":            {"trir": 2.9, "dart": 1.5, "label": "All private industry"},
    "construction":   {"trir": 2.3, "dart": 1.4, "label": "Construction (NAICS 23)"},
    "manufacturing":  {"trir": 3.0, "dart": 1.6, "label": "Manufacturing (NAICS 31-33)"},
    "oil_gas":        {"trir": 0.8, "dart": 0.5, "label": "Oil & gas extraction (NAICS 211/213)"},
    "transportation": {"trir": 4.0, "dart": 2.6, "label": "Transportation & warehousing (NAICS 48-49)"},
    "utilities":      {"trir": 1.4, "dart": 0.8, "label": "Utilities (NAICS 22)"},
    "warehousing":    {"trir": 4.8, "dart": 3.2, "label": "Warehousing & storage (NAICS 493)"},
    "healthcare":     {"trir": 4.5, "dart": 2.4, "label": "Healthcare & social assistance (NAICS 62)"},
    "agriculture":    {"trir": 4.3, "dart": 2.4, "label": "Agriculture (NAICS 11)"},
    "general_industry": {"trir": 2.9, "dart": 1.5, "label": "General industry (proxy: all private)"},
}

_SECTOR_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("oil_gas", ["oil", "gas", "oilfield", "well", "drilling", "upstream", "midstream",
                 "petroleum", "refin", "pipeline", "energy"]),
    ("construction", ["construction", "contractor", "building", "concrete", "roofing",
                      "electric", "plumb", "hvac", "scaffold", "excavat", "trade"]),
    ("transportation", ["trucking", "transport", "freight", "hauling", "logistics",
                        "carrier", "dot", "fmcsa", "fleet"]),
    ("warehousing", ["warehouse", "warehousing", "distribution", "fulfillment"]),
    ("manufacturing", ["manufactur", "fabricat", "plant", "mill", "assembly", "machining"]),
    ("utilities", ["utility", "utilities", "power", "grid", "substation", "water treatment"]),
    ("healthcare", ["health", "hospital", "medical", "clinic", "nursing", "care"]),
    ("agriculture", ["agricultur", "farm", "grain", "crop", "livestock"]),
]


def bls_benchmark(sector_or_industry: Optional[str]) -> Dict[str, Any]:
    """Best-guess BLS TRIR/DART benchmark for an industry name or sector key."""
    if not sector_or_industry:
        return dict(_BLS["all"], key="all")
    low = str(sector_or_industry).strip().lower()
    if low in _BLS:
        return dict(_BLS[low], key=low)
    for key, kws in _SECTOR_KEYWORDS:
        if any(k in low for k in kws):
            return dict(_BLS[key], key=key)
    return dict(_BLS["all"], key="all")


# ── safety-metric calculators (OSHA-standard formulas) ──────────────────────
def _rate(cases: Optional[float], hours: Optional[float]) -> Optional[float]:
    try:
        cases = float(cases)
        hours = float(hours)
    except (TypeError, ValueError):
        return None
    if hours <= 0:
        return None
    return round(cases * OSHA_HOURS_BASE / hours, 2)


def compute_metrics(
    hours: Optional[float] = None,
    recordables: Optional[float] = None,
    dart_cases: Optional[float] = None,
    days_away_cases: Optional[float] = None,
    lost_days: Optional[float] = None,
    fatalities: Optional[float] = None,
    industry: Optional[str] = None,
    emr: Optional[float] = None,
) -> Dict[str, Any]:
    """Compute TRIR / DART / LTIR / severity from raw OSHA-300 counts + hours,
    and benchmark TRIR/DART against the BLS average for the industry."""
    bench = bls_benchmark(industry)
    trir = _rate(recordables, hours)
    dart = _rate(dart_cases, hours)
    ltir = _rate(days_away_cases, hours)
    severity = _rate(lost_days, hours)

    def _verdict(rate: Optional[float], avg: float) -> Optional[str]:
        if rate is None:
            return None
        if rate <= avg * 0.5:
            return "well below average (excellent)"
        if rate <= avg:
            return "at/below average (good)"
        if rate <= avg * 1.5:
            return "above average (watch)"
        return "well above average (red flag)"

    out: Dict[str, Any] = {
        "hours": hours,
        "benchmark": bench,
        "trir": trir,
        "dart": dart,
        "ltir": ltir,
        "severity_rate": severity,
        "fatalities": fatalities,
        "emr": emr,
        "trir_vs_bls": _verdict(trir, bench["trir"]),
        "dart_vs_bls": _verdict(dart, bench["dart"]),
        "notes": [],
    }
    if emr is not None:
        try:
            e = float(emr)
            out["emr_verdict"] = (
                "below 1.0 — better than industry baseline" if e < 1.0
                else "1.0 — industry baseline" if abs(e - 1.0) < 1e-6
                else "above 1.0 — worse than baseline; many operators cap at 1.0"
            )
        except (TypeError, ValueError):
            out["emr"] = None
    if fatalities:
        out["notes"].append("Fatality present — most operators treat this as a hard disqualifier "
                             "pending review regardless of rates.")
    return out


# ── grade thresholds parsed from operator profiles ──────────────────────────
def parse_cap(text: Any) -> Optional[float]:
    """Pull a numeric ceiling out of a messy threshold string like
    '<=1.0 (letter if higher)' or '< 2.0'. Returns None when there is no explicit
    number — e.g. '<= NAICS avg (3-yr)' means "beat the BLS average", not 3.0, so
    the caller should fall back to the BLS benchmark."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text)
    # drop time-window tokens so '3-yr' / '6 mo' aren't misread as caps
    s = re.sub(r"\b\d+\s*-?\s*(yr|yrs|year|years|mo|month|months|day|days|week|weeks)\b",
               " ", s, flags=re.IGNORECASE)
    # 'NAICS avg' / 'industry average' with no explicit number => no numeric cap
    if re.search(r"avg|average", s, re.IGNORECASE) and not re.search(r"\d", s):
        return None
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


# ── calibration (optional real-world tuning) ────────────────────────────────
def _calibration_path() -> str:
    base = os.environ.get("ORIGIN_DATA_DIR") or os.getcwd()
    return os.path.join(base, "compliance_calibration.json")


def load_calibration() -> Dict[str, Any]:
    try:
        with open(_calibration_path(), "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def save_calibration(data: Dict[str, Any]) -> bool:
    try:
        with open(_calibration_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        return True
    except Exception:
        return False


# Default research-grounded component weights per platform. Written programs
# (RAVS) and safety statistics dominate; insurance is a hard gate; MSQ/training
# round it out. Overridable via calibration file -> platforms[NAME]["weights"].
_DEFAULT_WEIGHTS: Dict[str, Dict[str, float]] = {
    "ISN":       {"programs": 0.35, "stats": 0.30, "insurance": 0.20, "msq": 0.10, "training": 0.05},
    "Avetta":    {"programs": 0.30, "stats": 0.30, "insurance": 0.25, "msq": 0.05, "training": 0.10},
    "Veriforce": {"programs": 0.30, "stats": 0.35, "insurance": 0.20, "msq": 0.05, "training": 0.10},
    "PEC":       {"programs": 0.30, "stats": 0.30, "insurance": 0.20, "msq": 0.05, "training": 0.15},
    "BROWZ":     {"programs": 0.30, "stats": 0.30, "insurance": 0.25, "msq": 0.05, "training": 0.10},
}
_DEFAULT_BANDS = {"A": 90, "B": 80, "C": 70, "D": 60}  # else F


def _weights_for(platform: str) -> Dict[str, float]:
    cal = load_calibration().get("platforms", {}).get(platform, {})
    w = dict(_DEFAULT_WEIGHTS.get(platform, _DEFAULT_WEIGHTS["ISN"]))
    w.update(cal.get("weights", {}) or {})
    return w


def _bands_for(platform: str) -> Dict[str, float]:
    cal = load_calibration().get("platforms", {}).get(platform, {})
    b = dict(_DEFAULT_BANDS)
    b.update(cal.get("bands", {}) or {})
    return b


def _letter(score: float, bands: Dict[str, float]) -> str:
    if score >= bands["A"]:
        return "A"
    if score >= bands["B"]:
        return "B"
    if score >= bands["C"]:
        return "C"
    if score >= bands["D"]:
        return "D"
    return "F"


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _stats_subscore(inp: Dict[str, Any], drivers: List[str]) -> Optional[float]:
    """Blend EMR + TRIR + DART into a 0-1 safety-stats score."""
    parts: List[float] = []
    emr = inp.get("emr")
    emr_cap = parse_cap(inp.get("emr_cap")) or 1.0
    if emr is not None:
        try:
            e = float(emr)
            s = 1.0 if e <= emr_cap else _clamp(1.0 - (e - emr_cap) / max(emr_cap, 0.5))
            parts.append(s)
            drivers.append(f"EMR {e:g} vs cap {emr_cap:g} -> {'pass' if e <= emr_cap else 'over (drags grade)'}")
        except (TypeError, ValueError):
            pass

    def _rate_score(rate, cap, bls, name):
        if rate is None:
            return
        try:
            r = float(rate)
        except (TypeError, ValueError):
            return
        target = cap if cap else bls
        if target is None or target <= 0:
            return
        s = 1.0 if r <= target else _clamp(1.0 - (r - target) / target)
        parts.append(s)
        drivers.append(f"{name} {r:g} vs {'cap' if cap else 'BLS'} {target:g} -> "
                       f"{'pass' if r <= target else 'over'}")

    bench = bls_benchmark(inp.get("industry"))
    _rate_score(inp.get("trir"), parse_cap(inp.get("trir_cap")), bench["trir"], "TRIR")
    _rate_score(inp.get("dart"), parse_cap(inp.get("dart_cap")), bench["dart"], "DART")

    if inp.get("open_citations"):
        try:
            n = int(inp["open_citations"])
            if n > 0:
                parts.append(_clamp(1.0 - 0.2 * n))
                drivers.append(f"{n} open OSHA citation(s) -> penalty")
        except (TypeError, ValueError):
            pass
    if not parts:
        return None
    return sum(parts) / len(parts)


def estimate_grade(platform: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Estimate a contractor's grade on one platform from component inputs.

    inputs (all optional; the engine scores what's present and flags what's not):
      programs_required, programs_complete : int   -> RAVS/written-program completeness
      insurance_required, insurance_met    : int   -> COI/endorsement completeness
      insurance_ok                         : bool  -> shortcut for insurance fully met
      emr, emr_cap                         : float
      trir, trir_cap, dart, dart_cap       : float
      industry                             : str   -> selects BLS benchmark
      open_citations                       : int
      msq_complete, training_complete      : 0..1  -> ratios
      fatalities                           : int
    """
    platform = (platform or "ISN").strip().upper()
    canon = {"ISNETWORLD": "ISN", "ISN": "ISN", "AVETTA": "Avetta",
             "VERIFORCE": "Veriforce", "PEC": "PEC", "PEC PREMIER": "PEC",
             "BROWZ": "BROWZ", "COMPLYWORKS": "BROWZ"}.get(platform, "ISN")
    weights = _weights_for(canon)
    bands = _bands_for(canon)
    drivers: List[str] = []
    gates: List[str] = []

    sub: Dict[str, Optional[float]] = {}

    # programs / RAVS completeness
    preq = inputs.get("programs_required")
    pcomp = inputs.get("programs_complete")
    if preq:
        try:
            ratio = _clamp(float(pcomp or 0) / float(preq))
            sub["programs"] = ratio
            drivers.append(f"Written programs: {int(pcomp or 0)}/{int(preq)} complete "
                           f"({ratio*100:.0f}%)")
            if ratio < 1.0:
                gates.append("Missing written programs — RAVS/review will grade those items B/F")
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # insurance
    if inputs.get("insurance_ok") is True:
        sub["insurance"] = 1.0
        drivers.append("Insurance: all required coverages/endorsements met")
    elif inputs.get("insurance_required"):
        try:
            ratio = _clamp(float(inputs.get("insurance_met") or 0)
                           / float(inputs["insurance_required"]))
            sub["insurance"] = ratio
            drivers.append(f"Insurance: {int(inputs.get('insurance_met') or 0)}/"
                           f"{int(inputs['insurance_required'])} requirements met")
            if ratio < 1.0:
                gates.append("Insurance gap — a single missing/expired COI is a hard fail (red)")
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # stats
    stats = _stats_subscore(inputs, drivers)
    if stats is not None:
        sub["stats"] = stats

    # msq / training ratios
    for key, label in (("msq", "MSQ/questionnaire"), ("training", "Training records")):
        v = inputs.get(f"{key}_complete")
        if v is not None:
            try:
                sub[key] = _clamp(float(v))
                drivers.append(f"{label}: {sub[key]*100:.0f}% complete")
            except (TypeError, ValueError):
                pass

    # composite over the components we actually have (renormalize weights)
    avail = {k: v for k, v in sub.items() if v is not None}
    if not avail:
        return {"platform": canon, "score": None, "grade": "?",
                "drivers": ["Not enough inputs to estimate a grade — provide at least "
                            "program completeness or safety stats."],
                "gates": [], "estimated": True}
    wsum = sum(weights.get(k, 0) for k in avail) or 1.0
    score = 100.0 * sum(weights.get(k, 0) * v for k, v in avail.items()) / wsum

    # hard gates cap the letter regardless of composite
    letter = _letter(score, bands)
    if inputs.get("fatalities"):
        gates.append("Fatality on record — hard disqualifier pending operator review")
    if sub.get("insurance") is not None and sub["insurance"] < 1.0 and letter in ("A", "B"):
        letter = "C"  # insurance gaps block approval on most platforms
    if sub.get("programs") is not None and sub["programs"] < 0.6 and letter in ("A", "B"):
        letter = "C"

    # traffic light (Avetta-style) + approval read
    if letter in ("A", "B") and not any("hard" in g.lower() for g in gates):
        light = "GREEN (bid-eligible)"
    elif letter == "C":
        light = "AMBER (conditional — some clients approve, many won't)"
    else:
        light = "RED (not approved until resolved)"

    return {
        "platform": canon,
        "score": round(score, 1),
        "grade": letter,
        "traffic_light": light,
        "components": {k: round(v, 2) for k, v in avail.items()},
        "weights_used": {k: weights.get(k, 0) for k in avail},
        "drivers": drivers,
        "gates": gates,
        "bands": bands,
        "estimated": True,
    }


CAVEAT = (
    "ESTIMATE ONLY. The platforms don't publish their exact grade math and every "
    "grade is configured per hiring client, so treat this as a research-grounded "
    "projection, not the official score. Confirm against the live scorecard. "
    "Calibrate it to reality by recording real scorecards (grade_calibrate)."
)
