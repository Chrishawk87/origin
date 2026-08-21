"""Public "Grade Rescue" deficiency analyzer.

A lead-generation tool for the Origin Compliance site: a contractor answers a
short guided checklist about what's failing on their ISN / Avetta / PEC /
Veriforce scorecard, and gets an instant plain-English breakdown of what's
wrong, how serious it is, roughly what it takes to fix, and a next step.

It reuses the calibrated grading engine in ``compliance_grading`` to project a
letter grade from the checklist, so the read stays consistent with the rest of
the app. No LLM key required — the checklist maps deterministically to inputs.

The email a contractor enters to see their result is captured as a lead
(appended to ``rescue_leads.jsonl`` on the data volume) and, if email is
configured, a notification is sent to the business inbox.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import DATA_DIR

LEADS_FILE = DATA_DIR / "rescue_leads.jsonl"
NOTIFY_TO = "info@originmanagementsolutions.com"

PLATFORMS = ["ISNetworld", "Avetta", "PEC", "Veriforce"]

INDUSTRIES = [
    "Oilfield / energy services", "Trucking / motor carrier",
    "General / industrial construction", "Electrical", "Mechanical / HVAC",
    "Roofing / concrete / steel", "Marine / plant services", "Other",
]

# Each checklist item carries the human-facing finding AND how it nudges the
# grading inputs. severity: high | medium (drives ordering + fix scope).
CATEGORIES: List[Dict[str, Any]] = [
    {
        "id": "missing_programs",
        "label": "Written safety programs are missing or got rejected",
        "group": "Written programs",
        "severity": "high",
        "detail": ("Written programs are 30\u201340% of your grade, and a reviewer marks every "
                   "missing or rejected one against you. This is usually the single biggest "
                   "reason a grade fails."),
    },
    {
        "id": "scope_mismatch",
        "label": "My programs don't match my scope of work",
        "group": "Written programs",
        "severity": "high",
        "detail": ("Reviewers cross-check each program against your declared scope. Generic "
                   "templates fail here every time \u2014 if you do hot work, confined space or "
                   "DOT driving and the program doesn't say so, it gets kicked back."),
    },
    {
        "id": "outdated_programs",
        "label": "Programs are outdated or missing required OSHA/DOT language",
        "group": "Written programs",
        "severity": "medium",
        "detail": ("Programs have to carry the current mandatory language and citations. Old or "
                   "boilerplate wording gets flagged even when the program exists."),
    },
    {
        "id": "insurance_gap",
        "label": "A required insurance certificate or endorsement is missing/expired",
        "group": "Insurance",
        "severity": "high",
        "detail": ("A single missing or expired COI or endorsement is a hard fail on most "
                   "platforms \u2014 it can hold the whole grade red no matter how good everything "
                   "else looks."),
    },
    {
        "id": "high_trir",
        "label": "My injury rate (TRIR) is too high",
        "group": "Safety stats",
        "severity": "high",
        "detail": ("A TRIR above your industry benchmark drags your grade and locks you out of "
                   "bids. We audit what's actually recordable \u2014 often the number is higher than "
                   "it should be \u2014 and set up case management to bring it back in line honestly."),
    },
    {
        "id": "high_emr",
        "label": "My EMR is above 1.0",
        "group": "Safety stats",
        "severity": "high",
        "detail": ("An EMR over 1.0 signals higher-than-average losses; many operators gate bids "
                   "on it and your insurance rises with it. We help you document and manage it down."),
    },
    {
        "id": "open_citation",
        "label": "I have an open OSHA citation or a recent inspection",
        "group": "Safety stats",
        "severity": "high",
        "detail": ("An open citation shows on your record and spooks reviewers. We build the "
                   "abatement documentation and update the programs so it stops costing you bids. "
                   "(We handle the documentation side \u2014 we don't contest citations; that's a lawyer.)"),
    },
    {
        "id": "msq_incomplete",
        "label": "My MSQ / questionnaire isn't finished",
        "group": "Account setup",
        "severity": "medium",
        "detail": ("An incomplete Management System Questionnaire leaves easy points on the table "
                   "and can stall the review. It's quick for us to close out correctly."),
    },
    {
        "id": "training_gap",
        "label": "Training / T-RAVS documentation is missing",
        "group": "Account setup",
        "severity": "medium",
        "detail": ("Missing training records and T-RAVS items grade against you. We assemble the "
                   "documentation package so it lines up with your programs."),
    },
    {
        "id": "new_account",
        "label": "Brand new account \u2014 I need full setup / first qualification",
        "group": "Account setup",
        "severity": "high",
        "detail": ("Starting from zero: questionnaire, scope configuration, and the full program "
                   "set for first-time qualification. We do the whole setup, with rush turnaround "
                   "when a job deadline is close."),
    },
    {
        "id": "audit_coming",
        "label": "An operator, insurer, or internal audit is coming up",
        "group": "Account setup",
        "severity": "medium",
        "detail": ("We pull your documentation together, close the gaps, and run a mock review so "
                   "you walk in ready instead of scrambling."),
    },
]

CATEGORY_BY_ID = {c["id"]: c for c in CATEGORIES}


def _industry_key(industry: Optional[str]) -> Optional[str]:
    if not industry:
        return None
    s = industry.lower()
    if "oil" in s or "energy" in s:
        return "oil and gas"
    if "truck" in s or "motor" in s:
        return "trucking"
    if "construct" in s:
        return "construction"
    if "electric" in s:
        return "construction"
    if "roof" in s or "concrete" in s or "steel" in s:
        return "construction"
    if "marine" in s or "plant" in s:
        return "construction"
    return None


def _build_grade_inputs(checked: List[str], industry: Optional[str]) -> Dict[str, Any]:
    from .compliance_grading import bls_benchmark

    inp: Dict[str, Any] = {}
    if industry:
        inp["industry"] = industry

    # written programs
    if "new_account" in checked:
        inp["programs_required"] = 15
        inp["programs_complete"] = 0
    else:
        prog_deficit = 0
        if "missing_programs" in checked:
            prog_deficit += 6
        if "scope_mismatch" in checked:
            prog_deficit += 4
        if "outdated_programs" in checked:
            prog_deficit += 2
        if prog_deficit:
            inp["programs_required"] = 12
            inp["programs_complete"] = max(0, 12 - prog_deficit)

    # insurance
    if "insurance_gap" in checked:
        inp["insurance_required"] = 4
        inp["insurance_met"] = 2

    # safety stats
    bench = bls_benchmark(_industry_key(industry))
    trir_avg = float(bench.get("trir", 3.0)) if isinstance(bench, dict) else 3.0
    if "high_trir" in checked:
        inp["trir"] = round(trir_avg * 1.6, 1)
        inp["trir_cap"] = round(trir_avg, 1)
    if "high_emr" in checked:
        inp["emr"] = 1.25
        inp["emr_cap"] = 1.0
    if "open_citation" in checked:
        inp["open_citations"] = 1

    # setup ratios
    if "msq_incomplete" in checked:
        inp["msq_complete"] = 0.4
    if "training_gap" in checked:
        inp["training_complete"] = 0.35

    return inp


def _price_and_scope(checked: List[str]) -> Dict[str, Any]:
    program_issues = [c for c in ("missing_programs", "scope_mismatch", "outdated_programs") if c in checked]
    highs = [c for c in checked if CATEGORY_BY_ID.get(c, {}).get("severity") == "high"]

    if "new_account" in checked:
        low, high = 1200, 1800
        scope = "Full new-account setup \u2014 questionnaire, scope configuration, and the complete program set for first qualification."
    elif len(program_issues) >= 2 or len(highs) >= 3:
        low, high = 1100, 1500
        scope = "A full Grade Rescue \u2014 pull your deficiency report and rewrite every failed or missing program, scope-matched, then batch-resubmit."
    elif program_issues or "insurance_gap" in checked:
        low, high = 850, 1250
        scope = "A focused Grade Rescue on the items that are failing, resubmitted in one cycle."
    else:
        low, high = 650, 950
        scope = "A targeted cleanup of the gaps you flagged, handled in one pass."

    retainer = any(c in checked for c in ("high_trir", "high_emr", "audit_coming", "open_citation"))
    return {
        "price_low": low, "price_high": high, "scope": scope,
        "suggest_retainer": retainer,
        "one_cycle": bool(program_issues or "insurance_gap" in checked or "new_account" in checked),
    }


def analyze(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run the guided-checklist analysis. Does NOT capture a lead."""
    from .compliance_grading import estimate_grade, CAVEAT

    platform = (payload.get("platform") or "ISNetworld").strip()
    industry = (payload.get("industry") or "").strip() or None
    checked = [c for c in (payload.get("issues") or []) if c in CATEGORY_BY_ID]

    findings = []
    for cid in checked:
        c = CATEGORY_BY_ID[cid]
        findings.append({
            "id": cid, "title": c["label"], "group": c["group"],
            "severity": c["severity"], "detail": c["detail"],
        })
    # high severity first, preserving order within
    findings.sort(key=lambda f: 0 if f["severity"] == "high" else 1)

    # Only project a letter grade when the contractor flagged something that
    # actually moves a grade. If they only checked minor setup items (e.g. an
    # unfinished MSQ), we don't know the rest of their scorecard, so showing a
    # hard "F" off one signal would be misleading — skip the badge instead.
    GRADE_SIGNALS = {"missing_programs", "scope_mismatch", "outdated_programs",
                     "insurance_gap", "high_trir", "high_emr", "open_citation",
                     "new_account"}
    grade = None
    if any(c in GRADE_SIGNALS for c in checked):
        grade = estimate_grade(platform, _build_grade_inputs(checked, industry))

    scope = _price_and_scope(checked) if checked else None

    if not checked:
        headline = "Tell us what's failing and we'll show you exactly where you stand."
    elif grade and grade.get("grade") in ("A", "B"):
        headline = "You're close \u2014 a few fixable items are keeping you from a clean grade."
    else:
        headline = "Here's what's dragging your grade \u2014 and it's all fixable."

    return {
        "platform": platform,
        "industry": industry,
        "headline": headline,
        "grade": grade,
        "findings": findings,
        "scope": scope,
        "caveat": CAVEAT,
    }


def capture_lead(payload: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Persist the lead and try to notify the business inbox."""
    lead = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "name": (payload.get("name") or "").strip(),
        "company": (payload.get("company") or "").strip(),
        "email": (payload.get("email") or "").strip(),
        "phone": (payload.get("phone") or "").strip(),
        "platform": result.get("platform"),
        "industry": result.get("industry"),
        "issues": [f["id"] for f in result.get("findings", [])],
        "projected_grade": (result.get("grade") or {}).get("grade"),
    }
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with LEADS_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(lead) + "\n")
        saved = True
    except Exception:
        saved = False

    notified = False
    try:
        from .compliance import send_email, resend_configured, smtp_configured
        if resend_configured() or smtp_configured():
            issue_lines = "\n".join(f"  \u2022 {CATEGORY_BY_ID[i]['label']}" for i in lead["issues"] if i in CATEGORY_BY_ID)
            g = result.get("grade") or {}
            body = (
                f"New Grade Rescue lead from the site tool.\n\n"
                f"Name:     {lead['name'] or '(not given)'}\n"
                f"Company:  {lead['company'] or '(not given)'}\n"
                f"Email:    {lead['email']}\n"
                f"Phone:    {lead['phone'] or '(not given)'}\n"
                f"Platform: {lead['platform']}\n"
                f"Industry: {lead['industry'] or '(not given)'}\n"
                f"Projected grade: {g.get('grade','?')}  ({g.get('traffic_light','')})\n\n"
                f"What they flagged:\n{issue_lines or '  (none)'}\n"
            )
            res = send_email(NOTIFY_TO, f"New Grade Rescue lead: {lead['company'] or lead['email']}", body)
            notified = bool(res.get("sent"))
    except Exception:
        notified = False

    return {"saved": saved, "notified": notified}
