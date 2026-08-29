"""
scoping.py — Company-level compliance scoping.

The gap engine already resolves a required-standard baseline from a company's
NAICS/trade (compliance_kb.naics_applicable). That answers "what does a *typical*
firm in this industry need." This module adds the missing layer: what does THIS
company need, given what it actually does.

Two contractors with the same NAICS can have very different obligations. One runs
forklifts and enters permit-required confined spaces; the other doesn't. Silica,
lead, respirators, cranes, hot work, HAZWOPER, PSM — these are triggered by the
company's actual tasks, equipment, chemicals, and headcount, not by the trade code
alone. A generic sector template over- or under-scopes both firms.

`scope_company(profile)` takes a short intake and returns the tailored required-
standard set: the NAICS baseline UNION the activity-triggered standards, each one
annotated with WHY it applies (base sector vs. "triggered because you selected
X"). It also resolves the recordkeeping obligation (partial-exemption logic under
29 CFR 1904.1/1904.2) so the company knows whether it must keep a 300 Log at all.

The output plugs straight into the existing gap flow: the tailored id list is what
gaps.find_gaps should check the company's uploaded docs against.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import compliance_kb as kb


# ─────────────────────────────────────────────────────────────────────────────
# Activity / condition triggers.
# Each trigger maps a yes/no fact about the company to the OSHA standard(s) it
# pulls in. Where a standard has separate general-industry (1910) and
# construction (1926) versions, both ids are listed and scope_company picks the
# right one from the resolved sector.
# ─────────────────────────────────────────────────────────────────────────────
TRIGGERS: List[Dict[str, Any]] = [
    {"key": "confined_space",
     "q": "Do employees enter tanks, vessels, vaults, pits, or other permit-required confined spaces?",
     "category": "Confined space",
     "gi": "29-cfr-1910-146-permit-required-confined-spaces-general-industry",
     "citation": "29 CFR 1910.146 / 1926 Subpart AA"},
    {"key": "forklifts",
     "q": "Do you operate forklifts or other powered industrial trucks?",
     "category": "Powered industrial trucks",
     "gi": "29-cfr-1910-178-powered-industrial-trucks-forklift-program",
     "citation": "29 CFR 1910.178"},
    {"key": "cranes_rigging",
     "q": "Do you use cranes, derricks, hoists, or perform rigging/lifting?",
     "category": "Cranes & rigging",
     "gi": "29-cfr-1910-179-cranes-derricks-hoists-slings-general-industry-rigging",
     "con": "29-cfr-1926-subpart-cc-1926-1400-1442-cranes-and-derricks-in-construction",
     "citation": "29 CFR 1910.179 / 1926.1400"},
    {"key": "hot_work",
     "q": "Do employees weld, cut, braze, or perform other hot work?",
     "category": "Hot work",
     "gi": "29-cfr-1910-252-welding-cutting-and-brazing-hot-work",
     "citation": "29 CFR 1910.252 / 1926.352"},
    {"key": "respirators",
     "q": "Do any employees wear respirators (required, not voluntary dust masks)?",
     "category": "Respiratory protection",
     "gi": "29-cfr-1910-134-respiratory-protection-program",
     "citation": "29 CFR 1910.134"},
    {"key": "silica",
     "q": "Is there exposure to respirable crystalline silica (cutting/grinding concrete, masonry, sandblasting)?",
     "category": "Silica",
     "gi": "29-cfr-1910-1053-respirable-crystalline-silica-general-industry",
     "con": "29-cfr-1926-1153-respirable-crystalline-silica-written-exposure-control-plan-construction",
     "citation": "29 CFR 1910.1053 / 1926.1153"},
    {"key": "lead",
     "q": "Is there exposure to lead (abrasive blasting, torch-cutting coated steel, renovation of old structures)?",
     "category": "Lead",
     "gi": "29-cfr-1910-1025-lead-general-industry",
     "con": "29-cfr-1926-62-lead-in-construction",
     "citation": "29 CFR 1910.1025 / 1926.62"},
    {"key": "asbestos",
     "q": "Is there potential asbestos exposure (demolition/renovation of older buildings)?",
     "category": "Asbestos",
     "con": "29-cfr-1926-1101-asbestos-in-construction",
     "citation": "29 CFR 1926.1101 / 1910.1001"},
    {"key": "excavation",
     "q": "Do you perform excavation or trenching?",
     "category": "Excavation & trenching",
     "con": "29-cfr-1926-subpart-p-1926-651-652-excavation-and-trenching",
     "citation": "29 CFR 1926.651-652"},
    {"key": "fall_exposure",
     "q": "Do employees work at height with fall exposure (roofs, scaffolds, elevated platforms, leading edges)?",
     "category": "Fall protection",
     "con": "29-cfr-1926-subpart-m-1926-501-503-fall-protection-construction",
     "citation": "29 CFR 1926.501 / 1910.28"},
    {"key": "electrical",
     "q": "Do employees perform electrical work or work on/near energized parts?",
     "category": "Electrical safe work practices",
     "gi": "29-cfr-1910-331-335-electrical-safety-related-work-practices",
     "citation": "29 CFR 1910.331-335 / NFPA 70E"},
    {"key": "loto",
     "q": "Do employees service or maintain machinery/equipment where hazardous energy must be controlled?",
     "category": "Lockout / tagout",
     "gi": "29-cfr-1910-147-control-of-hazardous-energy-lockout-tagout",
     "citation": "29 CFR 1910.147"},
    {"key": "noise",
     "q": "Are employees exposed to noise at or above 85 dBA (an 8-hour TWA)?",
     "category": "Hearing conservation",
     "gi": "29-cfr-1910-95-occupational-noise-exposure-hearing-conservation-program",
     "citation": "29 CFR 1910.95"},
    {"key": "bloodborne",
     "q": "Do employees have occupational exposure to blood or other potentially infectious material (first responders, medical, remediation)?",
     "category": "Bloodborne pathogens",
     "gi": "29-cfr-1910-1030-bloodborne-pathogens-exposure-control-plan",
     "citation": "29 CFR 1910.1030"},
    {"key": "hazwoper",
     "q": "Do employees respond to hazardous-substance releases, or perform hazardous-waste cleanup?",
     "category": "HAZWOPER",
     "gi": "29-cfr-1910-120-hazardous-waste-operations-and-emergency-response-hazwoper",
     "citation": "29 CFR 1910.120"},
    {"key": "psm",
     "q": "Do you handle a highly hazardous chemical at or above its threshold quantity (covered process)?",
     "category": "Process safety management",
     "gi": "29-cfr-1910-119-process-safety-management-of-highly-hazardous-chemicals-psm",
     "citation": "29 CFR 1910.119"},
    {"key": "fire_extinguishers",
     "q": "Do you expect employees to use portable fire extinguishers (vs. total evacuation)?",
     "category": "Portable fire extinguishers",
     "gi": "29-cfr-1910-157-portable-fire-extinguishers",
     "citation": "29 CFR 1910.157"},
]
_TRIGGER_BY_KEY = {t["key"]: t for t in TRIGGERS}

# ─────────────────────────── trade → likely activities ───────────────────────
# Given the free-text industry/trade a user types (e.g. "roofing", "commercial
# electrical contractor"), which activity triggers are LIKELY to apply? These
# are suggestions the Gap Finder pre-checks so the analysis starts with better
# information — the user can always uncheck one or add others. Deliberately a
# little over-inclusive ("extra boxes just in case"): a pre-checked box the user
# clears costs nothing, a missed hazard costs a citation. Keys must exist in
# TRIGGERS. Matching is case-insensitive substring on the industry text, so
# "electrical & mechanical contractor" matches both "electrical" and "mechanical".
TRADE_ACTIVITY_HINTS = {
    "roof":            ["fall_exposure", "hot_work", "silica", "respirators"],
    "electric":        ["electrical", "loto", "fall_exposure", "confined_space", "cranes_rigging"],
    "plumb":           ["confined_space", "hot_work", "silica", "excavation", "loto"],
    "hvac":            ["hot_work", "confined_space", "loto", "fall_exposure", "respirators"],
    "mechanical":      ["hot_work", "confined_space", "loto", "cranes_rigging", "respirators", "fall_exposure"],
    "concrete":        ["silica", "cranes_rigging", "fall_exposure", "noise"],
    "mason":           ["silica", "fall_exposure", "noise", "cranes_rigging"],
    "paint":           ["respirators", "silica", "lead", "confined_space", "fall_exposure"],
    "coat":            ["respirators", "silica", "lead", "confined_space", "fall_exposure", "hazwoper"],
    "blast":           ["respirators", "silica", "lead", "noise", "confined_space"],
    "weld":            ["hot_work", "respirators", "cranes_rigging", "noise", "loto"],
    "fabricat":        ["hot_work", "respirators", "cranes_rigging", "noise", "loto", "forklifts"],
    "demolition":      ["silica", "lead", "asbestos", "fall_exposure", "noise", "hazwoper", "excavation"],
    "demo":            ["silica", "lead", "asbestos", "fall_exposure", "noise", "excavation"],
    "excavat":         ["excavation", "cranes_rigging", "silica", "noise"],
    "earthwork":       ["excavation", "cranes_rigging", "silica", "noise"],
    "grading":         ["excavation", "silica", "noise"],
    "trench":          ["excavation", "confined_space", "silica"],
    "scaffold":        ["fall_exposure", "cranes_rigging"],
    "insulation":      ["asbestos", "respirators", "fall_exposure", "silica"],
    "drywall":         ["silica", "fall_exposure", "noise"],
    "glaz":            ["fall_exposure", "cranes_rigging"],
    "glass":           ["fall_exposure", "cranes_rigging"],
    "steel":           ["fall_exposure", "cranes_rigging", "hot_work", "noise"],
    "iron":            ["fall_exposure", "cranes_rigging", "hot_work", "noise"],
    "erect":           ["fall_exposure", "cranes_rigging", "hot_work"],
    "oil":             ["hot_work", "confined_space", "psm", "hazwoper", "respirators", "fall_exposure", "loto"],
    "gas":             ["hot_work", "confined_space", "psm", "hazwoper", "respirators", "fall_exposure", "loto"],
    "well":            ["hot_work", "confined_space", "hazwoper", "respirators", "fall_exposure", "loto"],
    "pipeline":        ["hot_work", "excavation", "confined_space", "hazwoper", "cranes_rigging"],
    "pipe":            ["hot_work", "confined_space", "cranes_rigging", "excavation"],
    "refinery":        ["hot_work", "confined_space", "psm", "hazwoper", "respirators", "loto"],
    "petrochem":       ["hot_work", "confined_space", "psm", "hazwoper", "respirators", "loto"],
    "hydroblast":      ["confined_space", "respirators", "hazwoper", "noise"],
    "industrial clean":["confined_space", "respirators", "hazwoper", "noise"],
    "environmental":   ["hazwoper", "asbestos", "lead", "respirators", "bloodborne"],
    "remediation":     ["hazwoper", "asbestos", "lead", "respirators", "bloodborne"],
    "abatement":       ["asbestos", "lead", "respirators", "hazwoper"],
    "asbestos":        ["asbestos", "respirators", "hazwoper"],
    "fire protect":    ["hot_work", "fall_exposure", "confined_space"],
    "sprinkler":       ["hot_work", "fall_exposure", "confined_space"],
    "crane":           ["cranes_rigging", "fall_exposure"],
    "rigging":         ["cranes_rigging", "fall_exposure"],
    "landscap":        ["noise", "silica"],
    "manufactur":      ["loto", "forklifts", "noise", "hot_work", "respirators", "cranes_rigging"],
    "warehouse":       ["forklifts", "noise"],
    "distribution":    ["forklifts", "noise"],
    "logistics":       ["forklifts", "noise"],
    "facility mainten":["loto", "electrical", "confined_space", "fall_exposure"],
    "maintenance":     ["loto", "electrical", "fall_exposure"],
    "janitor":         ["bloodborne", "hazwoper"],
    "general contract":["fall_exposure", "silica", "cranes_rigging", "hot_work", "electrical"],
    "construction":    ["fall_exposure", "silica", "cranes_rigging", "hot_work"],
    "drilling":        ["hot_work", "confined_space", "hazwoper", "respirators", "noise", "silica"],
    "boring":          ["excavation", "confined_space", "cranes_rigging"],
    "utilit":          ["electrical", "excavation", "confined_space", "fall_exposure", "loto"],
    "tower":           ["fall_exposure", "cranes_rigging"],
    "carpentr":        ["fall_exposure", "noise", "silica"],
    "framing":         ["fall_exposure", "noise"],
    "flooring":        ["silica", "respirators", "noise"],
}


def suggest_activities(industry: str) -> Dict[str, Any]:
    """Given the free-text industry/trade, return the activity trigger keys the
    Gap Finder should PRE-CHECK. Union across every trade keyword found in the
    text; only returns keys that exist in TRIGGERS. Returns which keyword(s)
    matched too, so the UI can explain why boxes were pre-selected."""
    text = (industry or "").lower()
    keys: List[str] = []
    matched: List[str] = []
    if text.strip():
        for kw, acts in TRADE_ACTIVITY_HINTS.items():
            if kw in text:
                matched.append(kw)
                for a in acts:
                    if a in _TRIGGER_BY_KEY and a not in keys:
                        keys.append(a)
    return {"suggested": keys, "matched": matched}

# NAICS 2- and 3-digit codes that are partially exempt from routine 300-Log
# recordkeeping under 29 CFR 1904.2 / Appendix A (low-hazard industries).
# This is a pragmatic subset for the industries Origin serves; the size rule
# (1904.1, <=10 employees at all times in the prior year) is the more common
# exemption for the small contractors in the book of business.
_LOW_HAZARD_NAICS_PREFIXES = {
    "5411", "5412", "5413", "5415", "5416", "5417", "5418", "5419",  # professional svcs
    "52", "531", "5511", "6111", "8131",  # finance, real estate offices, schools, religious orgs
    "4411", "4413", "4451", "4481", "4482", "4483",  # certain retail
}


def intake_schema() -> Dict[str, Any]:
    """Questions the scoping intake asks, for the front-end to render."""
    return {
        "triggers": [{"key": t["key"], "q": t["q"], "category": t["category"],
                      "citation": t["citation"]} for t in TRIGGERS],
        "fields": [
            {"id": "company", "type": "text", "q": "Company name"},
            {"id": "industry", "type": "text",
             "q": "Industry / trade (or NAICS code)",
             "help": "e.g. 'industrial coating contractor' or '238320'"},
            {"id": "naics", "type": "text", "q": "NAICS code (optional)"},
            {"id": "state", "type": "text", "q": "State (optional, 2-letter)"},
            {"id": "headcount", "type": "number",
             "q": "Maximum number of employees at any time last year",
             "help": "Drives the 1904.1 small-employer recordkeeping exemption (<=10)."},
            {"id": "operators", "type": "text",
             "q": "Hiring clients / operators (optional, comma-separated)"},
        ],
    }


def _stub_from_id(sid: str, why: str, source: str) -> Optional[Dict[str, Any]]:
    """Resolve a KB record id into a required-standard stub with a reason."""
    rec = kb.get(sid)
    if not rec:
        return None
    return {
        "id": sid,
        "title": rec.get("title", sid),
        "citation": rec.get("citation", ""),
        "category": rec.get("category", ""),
        "written_program": rec.get("written_program", ""),
        "why": why,
        "source": source,  # "baseline" | "triggered"
    }


def triggered_standards(industry: str, state: Optional[str],
                        activities: Any) -> List[Dict[str, Any]]:
    """
    Resolve ONLY the activity-triggered standard stubs for a given trade —
    without the NAICS baseline. This is what the Gap Finder unions on top of its
    own baseline so a company profile drives the analysis.

    `activities` may be a list of trigger keys or a {key: bool} dict.
    Each stub carries `why`, `source="triggered"`, and the `trigger` key.
    """
    selected: set = set()
    if isinstance(activities, dict):
        selected = {k for k, v in activities.items() if v}
    elif isinstance(activities, (list, tuple, set)):
        selected = set(activities)
    if not selected:
        return []

    base = kb.naics_applicable((industry or "").strip(), state=state)
    is_construction = (base.get("sector") == "23")

    out: List[Dict[str, Any]] = []
    seen: set = set()
    for key in selected:
        t = _TRIGGER_BY_KEY.get(key)
        if not t:
            continue
        sid = (t.get("con") if (is_construction and t.get("con")) else (t.get("gi") or t.get("con")))
        if not sid or sid in seen:
            continue
        stub = _stub_from_id(
            sid,
            f"Triggered because the company reported: {t['category'].lower()} activity",
            "triggered",
        )
        if stub:
            stub["trigger"] = key
            out.append(stub)
            seen.add(sid)
    return out


def _recordkeeping_obligation(naics: str, headcount: Optional[int]) -> Dict[str, Any]:
    """
    29 CFR 1904.1 (size) and 1904.2 (industry) partial exemptions.
    NOTE: even a partially exempt employer must still REPORT severe events under
    1904.39 (8-hr fatality / 24-hr hospitalization-amputation-eye).
    """
    naics = (naics or "").strip()
    size_exempt = (headcount is not None and headcount <= 10)
    industry_exempt = any(naics.startswith(p) for p in _LOW_HAZARD_NAICS_PREFIXES) if naics else False

    if size_exempt or industry_exempt:
        reasons = []
        if size_exempt:
            reasons.append("had 10 or fewer employees at all times in the prior calendar year (1904.1)")
        if industry_exempt:
            reasons.append("is in a partially exempt low-hazard NAICS industry (1904.2, Appendix A)")
        return {
            "must_keep_300_log": False,
            "reason": "Partially exempt from routine recordkeeping because the company " + " and ".join(reasons) + ".",
            "note": "Still must report fatalities within 8h and in-patient hospitalizations, amputations, or "
                    "loss of an eye within 24h under 1904.39, and must respond to a BLS/OSHA survey if asked.",
            "citation": "29 CFR 1904.1, 1904.2, 1904.39",
        }
    return {
        "must_keep_300_log": True,
        "reason": "Must keep the OSHA 300 Log, 301 Incident Reports, and post the 300A annual summary "
                  "(Feb 1–Apr 30). Not size- or industry-exempt.",
        "note": "Certify and post the 300A; retain records for 5 years (1904.32, 1904.33). "
                "ITA electronic submission may apply by size/NAICS (1904.41).",
        "citation": "29 CFR 1904.29, 1904.32, 1904.33, 1904.41",
    }


def scope_company(profile: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build the company-specific required-standard set.

    profile = {
      "company": str, "industry": str, "naics": str, "state": str,
      "headcount": int, "operators": [str],
      "activities": {trigger_key: bool, ...}   # or a list of selected keys
    }
    """
    industry = (profile.get("industry") or profile.get("naics") or "").strip()
    naics = (profile.get("naics") or "").strip()
    state = (profile.get("state") or "").strip().upper() or None
    headcount = profile.get("headcount")
    try:
        headcount = int(headcount) if headcount not in (None, "") else None
    except (TypeError, ValueError):
        headcount = None

    # Normalize the activity flags into a set of selected trigger keys.
    acts = profile.get("activities")
    selected: set = set()
    if isinstance(acts, dict):
        selected = {k for k, v in acts.items() if v}
    elif isinstance(acts, list):
        selected = set(acts)

    # 1) NAICS/trade baseline via the existing resolver.
    base = kb.naics_applicable(industry or naics, state=state)
    sector = base.get("sector")
    is_construction = (sector == "23")

    stubs: Dict[str, Dict[str, Any]] = {}
    for s in base.get("standards", []):
        st = dict(s)
        st["why"] = "Baseline for " + (base.get("sector_label") or "this industry")
        st["source"] = "baseline"
        stubs[s["id"]] = st

    # 2) Activity/condition triggers layered on top.
    triggered_meta: List[Dict[str, Any]] = []
    for key in selected:
        t = _TRIGGER_BY_KEY.get(key)
        if not t:
            continue
        # Pick the construction variant when the company is a construction firm
        # and a construction id exists; otherwise the general-industry id.
        sid = None
        if is_construction and t.get("con"):
            sid = t["con"]
        else:
            sid = t.get("gi") or t.get("con")
        if not sid:
            continue
        why = f"Triggered because the company reported: {t['category'].lower()} activity"
        stub = _stub_from_id(sid, why, "triggered")
        if stub:
            # If baseline already had it, upgrade the reason to show it's also confirmed by activity.
            if sid in stubs and stubs[sid]["source"] == "baseline":
                stubs[sid]["why"] += f"; confirmed by reported {t['category'].lower()} activity"
            else:
                stubs[sid] = stub
            triggered_meta.append({"key": key, "category": t["category"],
                                   "standard_id": sid, "citation": t["citation"]})

    # 3) Recordkeeping obligation.
    recordkeeping = _recordkeeping_obligation(naics, headcount)

    standards = sorted(stubs.values(), key=lambda x: (x.get("source") != "baseline", x.get("category", ""), x.get("title", "")))
    return {
        "company": profile.get("company", ""),
        "industry": industry,
        "naics": naics,
        "state": state,
        "headcount": headcount,
        "sector": sector,
        "sector_label": base.get("sector_label"),
        "gap_note": base.get("gap_note"),
        "baseline_count": len(base.get("standards", [])),
        "triggered": triggered_meta,
        "triggered_count": len(triggered_meta),
        "required_standards": standards,
        "required_count": len(standards),
        "required_ids": [s["id"] for s in standards],
        "recordkeeping": recordkeeping,
        "operators": [o.strip() for o in (profile.get("operators") or [])
                      if isinstance(o, str) and o.strip()] if isinstance(profile.get("operators"), list)
                     else [o.strip() for o in str(profile.get("operators") or "").split(",") if o.strip()],
    }
