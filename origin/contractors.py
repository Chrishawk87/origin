"""contractors.py — Origin Contractor Compliance Dashboard (INTERNAL tool).

A living roll-up of every contractor Chris has run through the Gap Finder.
Each time an analysis runs with a contractor name, we snapshot the full
gap report here; the dashboard reads those snapshots back and rolls them up
into the portfolio table + per-contractor drill-down.

Storage mirrors the Projects pattern: one folder per contractor under
DATA_DIR/contractors/<slug>/ holding

  * contractor.json  — name, industry, state, operators, timestamps,
                        and Chris's MANUAL status overrides (the dots he
                        sets by hand for the columns the engine can't measure)
  * report.json      — the most recent find_gaps() output (the snapshot)

Nothing here is client-facing; the /api routes sit behind the app token.

Status model per dimension:
  "green" / "yellow" / "red"  — a known verdict
  "unknown"                    — no signal yet; Chris still needs to set it
Effective status = the manual override if Chris set one, else the auto value
the engine derived from the gap report.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import DATA_DIR

ROOT = DATA_DIR / "contractors"

VALID_STATUS = ("green", "yellow", "red", "unknown")
_ORDER = {"unknown": 0, "green": 1, "yellow": 2, "red": 3}

# The ten dashboard dimensions (order = display order in the drill-down).
# hard_stop = a red here forces overall Risk = Critical (per Chris's rules).
# The columns the engine can measure get an auto value; the rest start
# "unknown" and wait for Chris to click a dot.
DIMENSIONS: List[Dict[str, Any]] = [
    {"key": "insurance",          "label": "Insurance",          "hard_stop": True},
    {"key": "coi",                "label": "COI",                "hard_stop": True},
    {"key": "workers_comp",       "label": "Workers Comp",       "hard_stop": True},
    {"key": "emr",                "label": "EMR",                "hard_stop": False},
    {"key": "trir",               "label": "TRIR",               "hard_stop": False},
    {"key": "osha",               "label": "OSHA",               "hard_stop": False},
    {"key": "safety_program",     "label": "Safety Program",     "hard_stop": False},
    {"key": "isn",                "label": "ISN",                "hard_stop": True},
    {"key": "training",           "label": "Training",           "hard_stop": False},
    {"key": "owner_requirements", "label": "Owner Requirements", "hard_stop": False},
]
_DIM_BY_KEY = {d["key"]: d for d in DIMENSIONS}


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "contractor"


# ── status helpers ──────────────────────────────────────────────────────────
def _gap_color(g: dict) -> str:
    """A single gap's status as a dashboard color. A platform-flagged FAILING
    is red (the reviewer already bounced it), an inferred FAILING is yellow."""
    st = g.get("status")
    if st == "MISSING":
        return "red"
    if st == "FAILING":
        return "red" if g.get("platform_flagged") else "yellow"
    if st == "PRESENT":
        return "green"
    return "unknown"


def _worst(colors: List[str]) -> str:
    """Worst color in a group; 'unknown' if the group is empty."""
    worst = "unknown"
    for c in colors:
        if _ORDER.get(c, 0) > _ORDER.get(worst, 0):
            worst = c
    return worst


def _cat(g: dict) -> str:
    return (g.get("category") or "")


def _kwd(g: dict, *words: str) -> bool:
    hay = (g.get("title", "") + " " + g.get("id", "")).lower()
    return any(w in hay for w in words)


def _auto_statuses(report: dict) -> Dict[str, str]:
    """Derive an auto status for each dimension from a find_gaps() report.

    Only dimensions the engine has real signal for get a color; the numeric /
    externally-verified ones (EMR, TRIR, OSHA log, Workers Comp) stay 'unknown'
    so Chris sets them by hand rather than the board inventing a verdict.
    """
    gaps = report.get("gaps", []) or []
    summary = report.get("summary", {}) or {}
    defrep = report.get("deficiency_report", {}) or {}

    def grp(pred) -> str:
        return _worst([_gap_color(g) for g in gaps if pred(g)])

    auto: Dict[str, str] = {}

    # Insurance & COI — KB category 08 covers COIs / insurance verification.
    ins = [g for g in gaps if _cat(g).startswith("08")]
    auto["insurance"] = _worst([_gap_color(g) for g in ins])
    coi = [g for g in ins if _kwd(g, "coi", "certificate of insurance", "additional insured")]
    auto["coi"] = _worst([_gap_color(g) for g in coi]) if coi else "unknown"

    # Workers Comp — usually a doc Chris verifies; only auto if a matching
    # standard actually surfaced.
    wc = [g for g in gaps if _kwd(g, "workers comp", "workers' comp", "workman")]
    auto["workers_comp"] = _worst([_gap_color(g) for g in wc]) if wc else "unknown"

    # Safety metrics (EMR / TRIR) are numbers pulled from a mod sheet / OSHA
    # 300A — left to Chris. OSHA (citation/recordkeeping history) likewise.
    auto["emr"] = "unknown"
    auto["trir"] = "unknown"
    auto["osha"] = "unknown"

    # Safety Program — the core written safety programs (General Industry,
    # Construction, O&G specialty, Maritime, Healthcare, Environmental, DOT).
    prog_cats = ("01", "02", "03", "04", "06", "11", "12")
    auto["safety_program"] = grp(
        lambda g: g.get("needs_program") and _cat(g)[:2] in prog_cats)

    # ISN / qualification — a detected deficiency report or ANY platform-flagged
    # item is a hard red; otherwise fall back to the prequal-process/management
    # categories (05, 09).
    if defrep.get("detected") or any(g.get("platform_flagged") for g in gaps):
        auto["isn"] = "red"
    else:
        auto["isn"] = grp(lambda g: _cat(g)[:2] in ("05", "09"))

    # Training — programs whose training piece is the gap, or anything titled
    # "training". Falls back to the overall program set as a soft signal.
    train = [g for g in gaps if _kwd(g, "training", "competent person", "qualified")]
    if train:
        auto["training"] = _worst([_gap_color(g) for g in train])
    elif summary.get("programs_missing", 0):
        auto["training"] = "yellow"
    else:
        auto["training"] = "unknown"

    # Owner Requirements — client-driven / state-specific programs (cat 10).
    owner = [g for g in gaps if _cat(g).startswith("10")]
    auto["owner_requirements"] = _worst([_gap_color(g) for g in owner]) if owner else "unknown"

    return auto


def _effective(auto: Dict[str, str], overrides: Dict[str, str]) -> Dict[str, str]:
    eff = {}
    for d in DIMENSIONS:
        k = d["key"]
        ov = overrides.get(k)
        eff[k] = ov if ov in ("green", "yellow", "red") else auto.get(k, "unknown")
    return eff


# ── risk (Chris's confirmed thresholds) ──────────────────────────────────────
def _risk(compliance_pct: int, eff: Dict[str, str], programs_missing: int) -> str:
    hard = [d["key"] for d in DIMENSIONS if d["hard_stop"]]
    reds = [k for k, v in eff.items() if v == "red"]
    yellows = [k for k, v in eff.items() if v == "yellow"]
    if any(eff.get(k) == "red" for k in hard) or programs_missing > 0:
        return "Critical"
    if reds or compliance_pct < 75:
        return "High"
    if yellows or compliance_pct < 90:
        return "Medium"
    return "Low"


def _columns(report: dict, eff: Dict[str, str]) -> Dict[str, str]:
    """Roll the ten dimensions up into the four portfolio-table columns."""
    return {
        "insurance": _worst([eff["insurance"], eff["coi"], eff["workers_comp"]]),
        "safety": _worst([eff["safety_program"], eff["emr"], eff["trir"], eff["osha"]]),
        "qualification": _worst([eff["isn"], eff["owner_requirements"], eff["training"]]),
    }


def _consequence(risk: str, eff: Dict[str, str], operators: List[str]) -> str:
    n = len([o for o in (operators or []) if o])
    if risk == "Critical":
        if n:
            return (f"Potential loss of qualification with "
                    f"{n} customer{'s' if n != 1 else ''}")
        return "Potential loss of qualification"
    if risk == "High":
        return "At risk of failing the next prequalification review"
    if risk == "Medium":
        return "Minor items to resolve before the next review"
    return "In good standing"


# ── snapshot persistence ──────────────────────────────────────────────────
def _dir(slug: str) -> Path:
    return ROOT / slug


def save_snapshot(name: str, report: dict,
                  industry: str = "", state: Optional[str] = None,
                  operators: Optional[List[str]] = None) -> str:
    """Persist (or refresh) a contractor's latest gap-analysis snapshot.

    Any manual status overrides Chris already set are preserved across
    re-analysis. Returns the contractor slug.
    """
    slug = slugify(name)
    d = _dir(slug)
    d.mkdir(parents=True, exist_ok=True)

    meta_path = d / "contractor.json"
    overrides: Dict[str, str] = {}
    created = time.time()
    if meta_path.is_file():
        try:
            old = json.loads(meta_path.read_text())
            overrides = old.get("overrides", {}) or {}
            created = old.get("created", created)
        except Exception:
            pass

    meta = {
        "name": name.strip(),
        "slug": slug,
        "industry": industry or (report.get("meta", {}) or {}).get("industry", ""),
        "state": state or (report.get("meta", {}) or {}).get("state", ""),
        "operators": operators or [],
        "overrides": overrides,
        "created": created,
        "updated": time.time(),
    }
    meta_path.write_text(json.dumps(meta, indent=2))
    (d / "report.json").write_text(json.dumps(report, indent=2))
    return slug


def _load(slug: str) -> Optional[Dict[str, Any]]:
    d = _dir(slug)
    meta_path = d / "contractor.json"
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return None
    report = {}
    rp = d / "report.json"
    if rp.is_file():
        try:
            report = json.loads(rp.read_text())
        except Exception:
            report = {}
    return {"meta": meta, "report": report}


def _rollup(meta: dict, report: dict) -> Dict[str, Any]:
    summary = report.get("summary", {}) or {}
    compliance = int(summary.get("readiness_pct", 0) or 0)
    programs_missing = int(summary.get("programs_missing", 0) or 0)
    auto = _auto_statuses(report)
    overrides = meta.get("overrides", {}) or {}
    eff = _effective(auto, overrides)
    cols = _columns(report, eff)
    risk = _risk(compliance, eff, programs_missing)
    operators = meta.get("operators", []) or []
    return {
        "compliance": compliance,
        "columns": cols,
        "risk": risk,
        "auto": auto,
        "effective": eff,
        "programs_missing": programs_missing,
        "consequence": _consequence(risk, eff, operators),
    }


def list_contractors() -> List[Dict[str, Any]]:
    """Portfolio rows for the dashboard table."""
    ROOT.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    for d in sorted(ROOT.iterdir()):
        if not d.is_dir():
            continue
        rec = _load(d.name)
        if not rec:
            continue
        roll = _rollup(rec["meta"], rec["report"])
        m = rec["meta"]
        rows.append({
            "slug": m["slug"],
            "name": m["name"],
            "industry": m.get("industry", ""),
            "state": m.get("state", ""),
            "operators": m.get("operators", []),
            "updated": m.get("updated", 0),
            "compliance": roll["compliance"],
            "insurance": roll["columns"]["insurance"],
            "safety": roll["columns"]["safety"],
            "qualification": roll["columns"]["qualification"],
            "risk": roll["risk"],
        })
    # Worst risk first, then lowest compliance.
    _rrank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    rows.sort(key=lambda r: (_rrank.get(r["risk"], 9), r["compliance"]))
    return rows


# Which dimension a given gap belongs under, for the drill-down breakdown.
def _dim_for_gap(g: dict) -> str:
    cat2 = _cat(g)[:2]
    if _kwd(g, "coi", "certificate of insurance", "additional insured"):
        return "coi"
    if cat2 == "08":
        return "insurance"
    if _kwd(g, "workers comp", "workers' comp", "workman"):
        return "workers_comp"
    if cat2 == "10":
        return "owner_requirements"
    if cat2 in ("05", "09"):
        return "isn"
    if _kwd(g, "training", "competent person", "qualified"):
        return "training"
    return "safety_program"


def _reason(g: dict) -> str:
    if g.get("platform_flagged") and g.get("platform_reason"):
        return g["platform_reason"]
    if g.get("status") == "MISSING":
        return "Missing written program"
    miss = g.get("missing_elements") or []
    if miss:
        return "Missing: " + "; ".join(miss[:2])
    fp = g.get("failure_points") or []
    if fp:
        return fp[0]
    return g.get("status", "")


def get_contractor(slug: str) -> Optional[Dict[str, Any]]:
    """Full drill-down: score, dimensions (auto/manual/effective), the deficiency
    breakdown grouped by dimension, and the estimated consequence."""
    rec = _load(slug)
    if not rec:
        return None
    meta, report = rec["meta"], rec["report"]
    roll = _rollup(meta, report)
    overrides = meta.get("overrides", {}) or {}

    dims = []
    for d in DIMENSIONS:
        k = d["key"]
        dims.append({
            "key": k, "label": d["label"], "hard_stop": d["hard_stop"],
            "auto": roll["auto"].get(k, "unknown"),
            "manual": overrides.get(k, ""),
            "status": roll["effective"].get(k, "unknown"),
        })

    # Group the non-green gaps under their dimension for the deficiency list.
    breakdown: Dict[str, List[dict]] = {d["key"]: [] for d in DIMENSIONS}
    for g in report.get("gaps", []) or []:
        if _gap_color(g) == "green":
            continue
        key = _dim_for_gap(g)
        breakdown[key].append({
            "title": g.get("title", ""),
            "citation": g.get("citation", ""),
            "status": g.get("status", ""),
            "color": _gap_color(g),
            "reason": _reason(g),
            "platform_flagged": g.get("platform_flagged", False),
        })
    # Unmatched platform items → surface under ISN so nothing is lost.
    for u in (report.get("deficiency_report", {}) or {}).get("unmatched", []) or []:
        breakdown["isn"].append({
            "title": u.get("topic", u.get("raw", "")), "citation": "",
            "status": "FLAGGED", "color": "red",
            "reason": "Flagged by platform — match by hand",
            "platform_flagged": True,
        })

    critical_count = sum(1 for v in roll["effective"].values() if v == "red")

    return {
        "slug": meta["slug"],
        "name": meta["name"],
        "industry": meta.get("industry", ""),
        "state": meta.get("state", ""),
        "operators": meta.get("operators", []),
        "updated": meta.get("updated", 0),
        "compliance": roll["compliance"],
        "risk": roll["risk"],
        "consequence": roll["consequence"],
        "critical_count": critical_count,
        "columns": roll["columns"],
        "dimensions": dims,
        "breakdown": breakdown,
        "headline": (report.get("summary", {}) or {}).get("headline", ""),
    }


def set_status(slug: str, dim_key: str, value: str) -> bool:
    """Set (or clear) a manual override for one dimension. value '' clears it."""
    if dim_key not in _DIM_BY_KEY:
        return False
    if value and value not in VALID_STATUS:
        return False
    meta_path = _dir(slug) / "contractor.json"
    if not meta_path.is_file():
        return False
    meta = json.loads(meta_path.read_text())
    overrides = meta.get("overrides", {}) or {}
    if not value or value == "unknown":
        overrides.pop(dim_key, None)
    else:
        overrides[dim_key] = value
    meta["overrides"] = overrides
    meta["updated"] = time.time()
    meta_path.write_text(json.dumps(meta, indent=2))
    return True


def delete_contractor(slug: str) -> bool:
    d = _dir(slug)
    if not d.is_dir():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return not d.exists()
