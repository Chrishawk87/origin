"""
onboarding.py — Zero-setup self-building onboarding.

The magic selling point: a new company answers a short intake (trade + a few
hazard checkboxes) OR uploads its ISN/Avetta prequal questionnaire, and Origin
immediately assembles the *whole* compliance stack — a combined written safety
manual (every mandated program), a training-record matrix, the carrier EMR and
COI/endorsement request letters, a TRIR/DART reconciliation worksheet, and (when
the scope calls for it) a DOT Drug & Alcohol program and an Operator
Qualification tracker — and drops all of it into that company's document vault,
editable after. The less a person has to do, the more they want the software.

This module is deliberately *write-target agnostic*. It knows how to SCOPE a
company and how to BUILD the document set; it does not know where the documents
live. `build_full_stack(...)` returns a list of finished, print-ready documents
as ``{"gap_id", "title", "html"}`` and lets the caller persist them:

  * the owner/app side stores them via ``company_profile.save_company_doc``;
  * the platform sub side writes them into the sub's ``_client_dir/docs`` and
    appends a ``documents`` row — the exact same pattern the one-click prequal
    Fix already uses.

Everything here is deterministic, offline, and reuses the existing engines
(``scoping.scope_company`` and ``prequal_fix.generate``); it adds no new
knowledge and no external LLM call.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from . import scoping as _scoping
from . import prequal_fix as _fix


# ─────────────────────────────────────────────────────────────────────────────
# 1) Which documents make up a fresh company's stack.
#
# A brand-new company has none of these on file, so "build everything now" means
# building the core set every company needs plus the scope-triggered extras.
# Each id is a gap id ``prequal_fix.generate`` already knows how to build.
# ─────────────────────────────────────────────────────────────────────────────

# Always built — every prequal-graded company needs these regardless of trade.
CORE_STACK: List[str] = [
    "programs-missing",   # → combined company safety manual (all mandated programs)
    "training-records",   # → individual training-record matrix
    "rates-reconcile",    # → TRIR / DART reconciliation worksheet
    "emr-letter",         # → carrier EMR request letter
    "coi-endorsements",   # → COI + endorsements broker request letter
]

# Scope-triggered extras — added only when the company's scope of work calls for
# them, so a drywall sub doesn't get a DOT drug program it will never use.
_DOT_HINTS = ("truck", "trucking", "driver", "dot ", " dot", "fmcsa", "cdl",
              "transport", "haul", "motor carrier", "pipeline", "gas distribution")
_OQ_HINTS = ("pipeline", "gas distribution", "operator qualification", " oq",
             "distribution integrity", "192.", "gas transmission")


def stack_ids(scope: Optional[Dict[str, Any]], scope_text: str = "") -> List[str]:
    """The ordered list of fix-document ids to build for a fresh company."""
    ids: List[str] = list(CORE_STACK)
    text = " " + (scope_text or "").lower() + " "
    trig_cats = {(_t.get("category") or "").lower()
                 for _t in ((scope or {}).get("triggered") or [])}

    if any(h in text for h in _DOT_HINTS):
        ids.append("dna-program")
    if any(h in text for h in _OQ_HINTS):
        ids.append("oq-individual")
    # Belt-and-suspenders: a process-safety trigger also implies heavy DOT/O&G
    # operations where a D&A program is expected.
    if "process safety" in trig_cats and "dna-program" not in ids:
        ids.append("dna-program")

    # De-dupe while preserving order.
    seen: set = set()
    out: List[str] = []
    for gid in ids:
        if gid not in seen:
            seen.add(gid)
            out.append(gid)
    return out


def build_full_stack(company_id: str, *, company: str, scope_text: str = "",
                     scope: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """Build the full document stack for a fresh company.

    Returns a list of ``{"gap_id", "title", "html"}``. One document failing to
    build never aborts the rest — the stack is best-effort so onboarding always
    produces *something* usable.
    """
    company = (company or "").strip() or "Company"
    scope_text = (scope_text or "").strip()
    out: List[Dict[str, str]] = []
    for gid in stack_ids(scope, scope_text):
        try:
            doc = _fix.generate(gid, company=company, company_id=company_id,
                                scope=scope_text)
        except Exception:
            doc = None
        if doc and doc.get("html"):
            out.append({"gap_id": gid, "title": doc.get("title", gid),
                        "html": doc["html"]})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 2) Read an uploaded ISN / Avetta / PEC prequal questionnaire and infer the
#    scoping intake from it, so the user can onboard by dropping in the exact
#    document the platform sent them instead of answering the picker.
#
# Deterministic: we pull any 6-digit NAICS and 2-letter state, arm the trade's
# hazard triggers via scoping.suggest_activities on the detected trade line, and
# add any hazard explicitly named in the questionnaire body.
# ─────────────────────────────────────────────────────────────────────────────

# Explicit-hazard keyword → scoping trigger key. Kept in sync with scoping.TRIGGERS.
_HAZARD_KEYWORDS: Dict[str, List[str]] = {
    "confined_space": ["confined space", "permit-required", "permit required",
                       "tank entry", "vessel entry", "vault", "manhole"],
    "forklifts":      ["forklift", "powered industrial truck", "lift truck",
                       "order picker"],
    "cranes_rigging": ["crane", "rigging", "hoist", "overhead lift", "rigger"],
    "hot_work":       ["hot work", "welding", "cutting torch", "torch cutting",
                       "burning permit", "grinding spark"],
    "respirators":    ["respirator", "respiratory protection", "scba", "fit test",
                       "fit-test"],
    "silica":         ["silica", "crystalline silica", "concrete cutting",
                       "concrete grinding"],
    "lead":           ["lead paint", "lead abatement", "lead exposure",
                       "lead-based"],
    "asbestos":       ["asbestos", "acm ", "asbestos-containing"],
    "excavation":     ["excavation", "trenching", "trench", "shoring",
                       "soil classification"],
    "fall_exposure":  ["fall protection", "working at height", "at heights",
                       "aerial lift", "scaffold", "6 feet", "six feet"],
    "electrical":     ["electrical safe work", "energized", "arc flash",
                       "nfpa 70e", "qualified electrical", "70e"],
    "loto":           ["lockout", "tagout", "loto", "energy control",
                       "lock-out"],
    "noise":          ["hearing conservation", "noise exposure", "decibel",
                       "85 dba", "audiometric"],
    "bloodborne":     ["bloodborne", "exposure control plan", "bbp "],
    "hazwoper":       ["hazwoper", "hazardous waste", "emergency response",
                       "1910.120"],
    "psm":            ["process safety management", "psm ", "1910.119",
                       "highly hazardous chemical"],
    "fire_extinguishers": ["fire extinguisher", "portable fire"],
}

_VALID_TRIGGER_KEYS = set(_HAZARD_KEYWORDS.keys())

# US state abbreviations, for pulling a state out of free text safely.
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

_NAICS_RE = re.compile(r"\b(\d{6})\b")
_HEADCOUNT_RE = re.compile(
    r"(?:employees?|headcount|workforce|staff)[^\d]{0,20}(\d{1,5})", re.I)
_STATE_LABEL_RE = re.compile(r"\bstate\b[^A-Za-z]{0,12}([A-Za-z]{2})\b", re.I)
_TRADE_LABEL_RE = re.compile(
    r"(?:trade|industry|scope of work|scope|nature of (?:work|business)|"
    r"business type|services?)\s*[:\-]\s*([^\n\r]{3,120})", re.I)


def parse_questionnaire(text: str) -> Dict[str, Any]:
    """Infer a scoping intake from a pasted/uploaded prequal questionnaire.

    Returns ``{industry, naics, state, headcount, activities, signals}`` where
    ``activities`` is a de-duped list of scoping trigger keys and ``signals`` is
    a short human list explaining what was detected (for the UI to show and let
    the user confirm before building).
    """
    raw = text or ""
    low = raw.lower()
    signals: List[str] = []

    # NAICS — first plausible 6-digit code.
    naics = ""
    m = _NAICS_RE.search(raw)
    if m:
        naics = m.group(1)
        signals.append(f"NAICS {naics}")

    # State — prefer an explicit "State: XX" label, else any standalone 2-letter
    # code that is a real state and near a state-ish word.
    state = ""
    ms = _STATE_LABEL_RE.search(raw)
    if ms and ms.group(1).upper() in _US_STATES:
        state = ms.group(1).upper()
    if state:
        signals.append(f"State {state}")

    # Headcount.
    headcount: Optional[int] = None
    mh = _HEADCOUNT_RE.search(raw)
    if mh:
        try:
            headcount = int(mh.group(1))
            signals.append(f"~{headcount} employees")
        except ValueError:
            headcount = None

    # Trade / industry — an explicit label line if present, else the whole text
    # is scanned for trade keywords by suggest_activities below.
    industry = ""
    mt = _TRADE_LABEL_RE.search(raw)
    if mt:
        industry = mt.group(1).strip(" .;:-")
    if not industry and naics:
        industry = naics
    if industry:
        signals.append(f"Trade: {industry}")

    # Arm hazard triggers two ways: (a) the detected trade's hint set, run over
    # the WHOLE document so multi-trade questionnaires arm everything mentioned;
    # (b) any hazard explicitly named in the body.
    activities: List[str] = []

    sa = _scoping.suggest_activities(industry or raw)
    for k in (sa.get("suggested") or []):
        if k in _VALID_TRIGGER_KEYS and k not in activities:
            activities.append(k)

    for key, words in _HAZARD_KEYWORDS.items():
        if key in activities:
            continue
        if any(w in low for w in words):
            activities.append(key)

    if activities:
        signals.append(f"{len(activities)} hazard program(s) detected")

    return {
        "industry": industry,
        "naics": naics,
        "state": state,
        "headcount": headcount,
        "activities": activities,
        "signals": signals,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3) Convenience: scope + describe the stack that WILL be built, without building
#    it yet. Used by the wizard to preview "here's what Origin will create."
# ─────────────────────────────────────────────────────────────────────────────

# Human labels for each buildable document, so the UI can show a checklist
# before/after building. Mirrors prequal_fix's produced-document names.
STACK_LABELS: Dict[str, str] = {
    "programs-missing": "Company safety manual (all mandated written programs)",
    "training-records": "Training-record matrix",
    "rates-reconcile":  "TRIR / DART reconciliation worksheet",
    "emr-letter":       "Carrier EMR request letter",
    "coi-endorsements": "COI + endorsements broker request letter",
    "dna-program":      "Drug & Alcohol (DOT) written program",
    "oq-individual":    "Operator Qualification per-individual tracker",
}


def plan_stack(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Scope the company and return the scope result plus the labelled list of
    documents Origin will build — no documents generated yet."""
    scope = _scoping.scope_company(profile)
    scope_text = (profile.get("industry") or profile.get("naics") or "").strip()
    ids = stack_ids(scope, scope_text)
    will_build = [{"id": gid, "label": STACK_LABELS.get(gid, gid)} for gid in ids]
    return {"scope": scope, "will_build": will_build, "count": len(will_build)}
