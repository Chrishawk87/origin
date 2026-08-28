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

PLATFORMS = ["ISNetworld", "Avetta", "PEC", "Veriforce", "None / not on a platform yet"]

# Platform values that mean "not graded on a prequal platform yet" — we still
# analyze the flagged issues and scope the work, but we don't project a letter
# grade, since a grade only exists relative to a platform's scorecard.
_NO_PLATFORM = {"none", "none / not on a platform yet", "not on a platform yet", "n/a", ""}

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
    # A letter grade only means something relative to a platform's scorecard. If
    # the contractor isn't on a platform yet, skip the badge and just show the
    # findings + scope of work (still useful for a brand-new setup).
    on_platform = platform.lower() not in _NO_PLATFORM
    if on_platform and any(c in GRADE_SIGNALS for c in checked):
        grade = estimate_grade(platform, _build_grade_inputs(checked, industry))

    scope = _price_and_scope(checked) if checked else None

    # Advisory intel for the public tool. This is drawn ONLY from the curated
    # reference brain (prequal-platform how-to + abatement guidance) — NOT from
    # the self-taught `learned` store, so internal notes Chris teaches Origin
    # are never exposed on a public lead-gen page. Gets richer as the curated
    # knowledge grows. Retrieval only; never affects the grade or scope.
    intel: List[Dict[str, Any]] = []
    try:
        from . import compliance_kb as _kb
        finding_words = " ".join(f["title"] for f in findings)
        q = " ".join(p for p in (platform, industry or "", finding_words) if p)
        intel = _kb.brain_intel(q, limit=4,
                                kinds=["prequal_platform", "abatement"])
    except Exception:
        intel = []

    if not checked:
        headline = "Tell us what's failing and we'll show you exactly where you stand."
    elif not on_platform:
        headline = "Here's what your setup needs to qualify \u2014 and we can handle all of it."
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
        "intel": intel,
        "caveat": CAVEAT,
    }


# ── Tool 2: RAVS / prequal rejection decoder ────────────────────────────────
# A contractor whose written program (or account) got kicked back picks the
# reason(s) they were given and gets a plain-English read on what the reviewer
# actually means and what they want to see — WITHOUT us handing over the fix
# language. Each entry is authored from the curated prequal-platform KB
# (rejection_reasons lists). Free-typed reasons fall back to the curated brain.
REJECTIONS: List[Dict[str, Any]] = [
    {
        "id": "template_generic", "group": "Written program",
        "label": "Generic / template manual (not company-specific)",
        "meaning": ("The reviewer can tell the program came off a shelf \u2014 it reads like a "
                    "template with a cover page changed. That's the single most common reason "
                    "a written program is rejected across every platform."),
        "wants": ("A program written as YOUR company: legal name, your sites, your equipment, "
                  "and your actual job tasks woven all the way through \u2014 not just on page one."),
    },
    {
        "id": "scope_mismatch", "group": "Written program",
        "label": "Program doesn't match my declared scope of work",
        "meaning": ("Reviewers cross-check every program against the scope you declared. If you "
                    "do hot work, confined space, or DOT driving and the program doesn't cover "
                    "it \u2014 or it promises work you don't actually do \u2014 it gets kicked back."),
        "wants": ("Programs that mirror your declared scope exactly: everything you do is "
                  "covered, and nothing you don't do is over-promised."),
    },
    {
        "id": "missing_element", "group": "Written program",
        "label": "A required element is missing (rescue plan, periodic inspection, competent person\u2026)",
        "meaning": ("The standard behind the program names specific mandatory elements and one "
                    "is absent \u2014 e.g. a confined-space rescue plan, the LOTO annual inspection, "
                    "or a named fall-protection competent person."),
        "wants": ("Every mandatory element present and clearly labeled so the reviewer can check "
                  "each one off against the standard."),
    },
    {
        "id": "non_assertive", "group": "Written program",
        "label": "Non-assertive language (\u201cshould\u201d instead of \u201cwill / shall\u201d)",
        "meaning": ("Reviewers reject conditional wording. A program has to commit the company "
                    "to doing things, not suggest them."),
        "wants": ("Mandatory \u201cwill / shall\u201d language throughout \u2014 the program reads as a "
                  "commitment, not a recommendation."),
    },
    {
        "id": "no_responsible_party", "group": "Written program",
        "label": "No named responsible person / program administrator",
        "meaning": ("There's no accountable owner named in the program, so the reviewer can't "
                    "see who's responsible for running it."),
        "wants": ("A named responsible party (title is fine) with their duties spelled out."),
    },
    {
        "id": "wrong_citation", "group": "Written program",
        "label": "Wrong, missing, or outdated OSHA / DOT citation",
        "meaning": ("The program cites the wrong standard, an old revision, or none at all. "
                    "Reviewers check that the citations are current and correct."),
        "wants": ("The correct, current CFR citations tied to each covered topic."),
    },
    {
        "id": "undated_stale", "group": "Written program",
        "label": "Undated or stale program (old revision date)",
        "meaning": ("The program has no revision date or an obviously old one, which signals it "
                    "isn't being maintained."),
        "wants": ("A current, dated revision with a stated review cycle."),
    },
    {
        "id": "coi_defect", "group": "Insurance",
        "label": "Insurance / COI defect (limits, endorsements, named insured, dates)",
        "meaning": ("Your certificate doesn't meet the client's requirement \u2014 a limit is short, "
                    "an endorsement (waiver of subrogation, additional insured) is missing, the "
                    "named insured is off, or the dates lapsed. This is a hard fail on most "
                    "platforms and can hold the whole grade red."),
        "wants": ("A COI with the exact limits, endorsements, named insured, and valid dates "
                  "the client requires \u2014 not close, exact."),
    },
    {
        "id": "emr_letter", "group": "Insurance",
        "label": "EMR submitted as a broker summary, not a carrier letter",
        "meaning": ("Reviewers require the experience-modification rate on the official letter "
                    "from your carrier or rating bureau \u2014 a broker recap doesn't count."),
        "wants": ("The official EMR rate letter for each year they ask for."),
    },
    {
        "id": "stats_mismatch", "group": "Safety stats",
        "label": "Reported TRIR / DART doesn't reconcile with my OSHA 300 / 300A",
        "meaning": ("The numbers you entered don't tie to the OSHA logs you uploaded. That "
                    "contradiction is an automatic flag \u2014 reviewers assume the worse of the two."),
        "wants": ("Reported rates that reconcile exactly with your 300A, hours, and case counts."),
    },
    {
        "id": "citation_no_capa", "group": "Safety stats",
        "label": "OSHA citation with no attached corrective action",
        "meaning": ("An open or past citation is showing with no abatement documentation, so "
                    "the reviewer has nothing proving you fixed it."),
        "wants": ("Corrective-action / abatement proof attached to the citation."),
    },
    {
        "id": "missing_uploads", "group": "Account setup",
        "label": "Missing or incomplete required uploads",
        "meaning": ("One or more required documents simply aren't uploaded, so the review "
                    "stalls before it can even score you."),
        "wants": ("The full required document set uploaded in the right slots."),
    },
    {
        "id": "drug_alcohol", "group": "Account setup",
        "label": "Missing Drug & Alcohol program documentation",
        "meaning": ("A required Drug & Alcohol policy is absent \u2014 and if you're DOT-regulated, "
                    "the DOT parts are expected too."),
        "wants": ("A compliant Drug & Alcohol program (plus DOT testing provisions if they "
                  "apply to you)."),
    },
    {
        "id": "oq_records", "group": "Account setup",
        "label": "OQ records at company level, not per individual + task (Veriforce / PEC)",
        "meaning": ("Operator Qualification has to be proven per person per covered task. A "
                    "company-level statement doesn't satisfy it."),
        "wants": ("Individual OQ records mapped to each covered task and worker."),
    },
    {
        "id": "training_docs", "group": "Account setup",
        "label": "Training / T-RAVS records missing names, dates, or competency",
        "meaning": ("Rosters were submitted that don't prove who was trained, on what, and "
                    "when \u2014 so they don't back up the programs."),
        "wants": ("Per-person training records with name, date, topic, and how competency was "
                  "confirmed, lined up with your programs."),
    },
    {
        "id": "ai_overcommit", "group": "Written program",
        "label": "AI-generated / over-committed program (promises more than I do)",
        "meaning": ("The program commits to procedures or equipment you don't actually have in "
                    "the field. Reviewers increasingly catch this \u2014 and an operator audit will too."),
        "wants": ("An honest program scoped to what you can actually prove on site."),
    },
]
REJECTION_BY_ID = {r["id"]: r for r in REJECTIONS}


def decode_rejections(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Decode the rejection reasons a contractor was given into plain English:
    what each one means and what the reviewer wants to see. Does NOT hand over
    the fix language or a finished program. Does NOT capture a lead."""
    platform = (payload.get("platform") or "").strip() or "your platform"
    chosen = [r for r in (payload.get("reasons") or []) if r in REJECTION_BY_ID]
    other = (payload.get("other") or "").strip()

    decoded: List[Dict[str, Any]] = []
    for rid in chosen:
        r = REJECTION_BY_ID[rid]
        decoded.append({
            "id": rid, "group": r["group"], "title": r["label"],
            "meaning": r["meaning"], "wants": r["wants"],
        })

    # Free-typed reason: pull the closest curated prequal knowledge so the read
    # is still grounded rather than generic. Retrieval only.
    intel: List[Dict[str, Any]] = []
    if other:
        try:
            from . import compliance_kb as _kb
            q = " ".join(p for p in (platform, other) if p and p != "your platform")
            intel = _kb.brain_intel(q, limit=3, kinds=["prequal_platform", "abatement"])
        except Exception:
            intel = []

    n = len(decoded) + (1 if other else 0)
    if n == 0:
        headline = "Tell us what you were told and we'll translate it into plain English."
    elif n == 1:
        headline = "Here's exactly what that rejection means \u2014 and it's fixable."
    else:
        headline = f"Here's what all {n} of those really mean \u2014 and every one is fixable."

    return {
        "platform": platform,
        "headline": headline,
        "decoded": decoded,
        "other": other,
        "intel": intel,
        "count": n,
        "caveat": ("This is a plain-English read of standard reviewer feedback, not a legal or "
                   "platform ruling. When we do your Grade Rescue we rewrite each rejected item "
                   "to the exact standard and resubmit it for you."),
    }


# ── Tool 3: Readiness quiz ───────────────────────────────────────────────────
# A fast "are you ready to pass?" check. Yes/No/Unsure across the things that
# actually decide a prequal grade, weighted by how much each one moves the
# needle. Returns a readiness score, a band, and the gaps (the No/Unsure items)
# — without handing over the fixes.
QUIZ: List[Dict[str, Any]] = [
    {"id": "programs_scope", "weight": 3,
     "q": "Do you have a written safety program for every task in your declared scope of work?",
     "gap": "Missing scope-matched written programs is the #1 reason grades fail."},
    {"id": "company_specific", "weight": 2,
     "q": "Are your programs company-specific (your name, sites, equipment) \u2014 not a generic template?",
     "gap": "Template manuals get rejected on sight; they have to read like your company."},
    {"id": "citations_language", "weight": 2,
     "q": "Do your programs cite current OSHA/DOT standards and use \u201cwill / shall\u201d language?",
     "gap": "Old citations and \u201cshould\u201d language get programs kicked back."},
    {"id": "training_records", "weight": 2,
     "q": "Can you produce training records with names, dates, and topics for each program?",
     "gap": "Training records without names/dates/competency fail review."},
    {"id": "coi_current", "weight": 3,
     "q": "Is your COI current with the exact limits and endorsements your clients require?",
     "gap": "A single COI/endorsement defect can hold your whole grade red."},
    {"id": "emr_letter", "weight": 1,
     "q": "Do you have your EMR as an official carrier / bureau letter (not a broker summary)?",
     "gap": "Reviewers require the carrier EMR letter, not a broker recap."},
    {"id": "stats_reconcile", "weight": 2,
     "q": "Do your reported TRIR/DART numbers match your OSHA 300/300A logs exactly?",
     "gap": "Numbers that don't reconcile with your 300A are an automatic flag."},
    {"id": "no_open_citation", "weight": 2,
     "q": "Are you free of open OSHA citations that have no abatement documentation attached?",
     "gap": "An open citation with no corrective action attached spooks reviewers."},
    {"id": "msq_complete", "weight": 1,
     "q": "Is your MSQ / questionnaire fully completed and consistent with your uploads?",
     "gap": "An incomplete or contradictory questionnaire leaves easy points on the table."},
    {"id": "responsible_party", "weight": 1,
     "q": "Does each program name a responsible person / administrator?",
     "gap": "Programs with no named responsible party get rejected."},
]
QUIZ_BY_ID = {q["id"]: q for q in QUIZ}
_QUIZ_TOTAL = sum(q["weight"] for q in QUIZ)


def score_readiness(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Score the readiness quiz. answers = {id: 'yes'|'no'|'unsure'}. 'yes'
    earns full weight; 'no'/'unsure' earn nothing and surface as a gap. Does
    NOT capture a lead."""
    platform = (payload.get("platform") or "").strip() or None
    answers = payload.get("answers") or {}

    earned = 0
    gaps: List[Dict[str, Any]] = []
    for q in QUIZ:
        a = str(answers.get(q["id"], "")).strip().lower()
        if a == "yes":
            earned += q["weight"]
        else:
            gaps.append({"id": q["id"], "title": q["q"], "gap": q["gap"],
                         "answer": a or "unanswered", "weight": q["weight"]})

    pct = round(100 * earned / _QUIZ_TOTAL) if _QUIZ_TOTAL else 0
    # heaviest gaps first
    gaps.sort(key=lambda g: -g["weight"])

    if pct >= 90:
        band, light = "Ready to pass", "GREEN"
        headline = "You're in strong shape \u2014 just a couple of things to lock down."
    elif pct >= 70:
        band, light = "Close", "AMBER"
        headline = "You're close \u2014 a handful of fixable items stand between you and a clean grade."
    elif pct >= 50:
        band, light = "At risk", "AMBER"
        headline = "You're at risk \u2014 several of the things reviewers weigh most are open."
    else:
        band, light = "Not ready yet", "RED"
        headline = "Not ready yet \u2014 but every gap here is fixable, and we do exactly this."

    return {
        "platform": platform,
        "score": pct,
        "band": band,
        "light": light,
        "headline": headline,
        "gaps": gaps,
        "gap_count": len(gaps),
        "total_questions": len(QUIZ),
        "caveat": ("This is a quick self-check, not a platform score. Your real grade depends "
                   "on your specific scorecard \u2014 which is exactly what our free 15-minute review "
                   "walks with you."),
    }


def capture_generic_lead(payload: Dict[str, Any], *, source: str,
                         summary: str = "", extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Persist a lead from any of the public tools and notify the inbox.
    `source` tags which tool it came from (e.g. 'rejection', 'readiness')."""
    lead = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "name": (payload.get("name") or "").strip(),
        "company": (payload.get("company") or "").strip(),
        "email": (payload.get("email") or "").strip(),
        "phone": (payload.get("phone") or "").strip(),
        "platform": (payload.get("platform") or "").strip(),
    }
    if extra:
        lead.update(extra)
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
            body = (
                f"New {source} lead from the site tool.\n\n"
                f"Name:     {lead['name'] or '(not given)'}\n"
                f"Company:  {lead['company'] or '(not given)'}\n"
                f"Email:    {lead['email']}\n"
                f"Phone:    {lead['phone'] or '(not given)'}\n"
                f"Platform: {lead['platform'] or '(not given)'}\n\n"
                f"{summary}\n"
            )
            res = send_email(NOTIFY_TO,
                             f"New {source} lead: {lead['company'] or lead['email']}", body)
            notified = bool(res.get("sent"))
    except Exception:
        notified = False

    return {"saved": saved, "notified": notified}


def capture_handle(payload: Dict[str, Any], *, tool: str,
                   summary: str = "") -> Dict[str, Any]:
    """High-intent capture: the visitor clicked 'let us handle it for you' on a
    free tool. Log it as a hot lead and send an unmistakable notification so
    Chris knows a fix was requested — no call or email required of the visitor."""
    lead = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": f"{tool}-handle",
        "intent": "wants_fix",
        "name": (payload.get("name") or "").strip(),
        "company": (payload.get("company") or "").strip(),
        "email": (payload.get("email") or "").strip(),
        "phone": (payload.get("phone") or "").strip(),
        "platform": (payload.get("platform") or "").strip(),
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
            body = (
                f"HOT LEAD — they clicked 'let us handle it for you' on the {tool} tool.\n"
                f"They want us to build/fix it. Reach out today.\n\n"
                f"Name:     {lead['name'] or '(not given)'}\n"
                f"Company:  {lead['company'] or '(not given)'}\n"
                f"Email:    {lead['email']}\n"
                f"Phone:    {lead['phone'] or '(not given)'}\n"
                f"Platform: {lead['platform'] or '(not given)'}\n\n"
                f"{summary}\n"
            )
            res = send_email(
                NOTIFY_TO,
                f"HOT LEAD — wants us to handle it ({tool}): {lead['company'] or lead['email']}",
                body)
            notified = bool(res.get("sent"))
    except Exception:
        notified = False

    return {"saved": saved, "notified": notified}


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
