"""
recordability.py — Guided OSHA recordability & reporting determination engine.

Turns the facts of a single injury/illness case into an authoritative answer:
  • Is it work-related?          (29 CFR 1904.5, incl. the (b)(2) exceptions)
  • Is it a new case?            (29 CFR 1904.6)
  • Is it recordable?            (29 CFR 1904.7 general recording criteria)
  • Which OSHA 300 Log column?   (G death / H days-away / I restricted-transfer / J other)
  • Must OSHA be notified?       (29 CFR 1904.39 — 8-hr fatality / 24-hr hosp/amp/eye)
  • Privacy-concern case?        (29 CFR 1904.29(b)(6)-(9))

This is a pure-Python decision engine with NO framework dependencies so it can be
unit-tested in isolation and called from server routes, the portal, or the CLI.

The logic is grounded in the Origin compliance KB records:
  29-cfr-1904-5-determination-of-work-relatedness
  29-cfr-1904-7-general-recording-criteria
  29-cfr-1904-29-forms-and-privacy-concern-cases
  29-cfr-1904-32-annual-summary-300a-posting-certification
  29-cfr-1904-39-reporting-fatalities-and-severe-injuries

Design principle: the engine NEVER silently guesses. Every conclusion carries a
plain-English rationale and the citation it rests on, so a reviewer (or an ISN
auditor) can follow the reasoning. When an input is missing, the engine says what
it still needs rather than defaulting to "not recordable."
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# 1904.5(b)(2) — the closed list of work-relatedness EXCEPTIONS.
# If an exception applies, the case is NOT work-related and therefore not recordable.
# ─────────────────────────────────────────────────────────────────────────────
WORK_RELATEDNESS_EXCEPTIONS: List[Dict[str, str]] = [
    {"key": "member_of_public",
     "label": "Employee was present as a member of the general public (not as an employee)"},
    {"key": "symptoms_surface_at_work_nonwork_cause",
     "label": "Symptoms merely surfaced at work but resulted SOLELY from a non-work event or exposure"},
    {"key": "voluntary_wellness",
     "label": "Voluntary participation in a wellness program, or recreational/athletic activity (e.g., company softball) that was voluntary and not a work requirement"},
    {"key": "eating_drinking_own_food",
     "label": "Injury/illness solely from eating, drinking, or preparing the employee's OWN food/drink"},
    {"key": "personal_grooming_self_medication",
     "label": "Personal grooming, self-medication for a non-work condition, or intentionally self-inflicted injury"},
    {"key": "motor_vehicle_parking_commute",
     "label": "Motor-vehicle accident in a company parking lot/access road while commuting to/from work"},
    {"key": "common_cold_or_flu",
     "label": "The illness is the common cold or the flu"},
    {"key": "mental_illness_no_professional_opinion",
     "label": "Mental illness, with no professional opinion from a physician/PLHCP linking it to work"},
    {"key": "personal_task_outside_hours",
     "label": "Employee was doing a personal task, outside assigned working hours, unrelated to employment"},
]
_EXCEPTION_KEYS = {e["key"] for e in WORK_RELATEDNESS_EXCEPTIONS}

# ─────────────────────────────────────────────────────────────────────────────
# 1904.7(b)(5)(ii) — the CLOSED first-aid list.
# Anything NOT on this list that a health-care professional does is "medical
# treatment beyond first aid" → the case is recordable.
# ─────────────────────────────────────────────────────────────────────────────
FIRST_AID_TREATMENTS: List[Dict[str, str]] = [
    {"key": "otc_meds_nonrx_strength",
     "label": "Non-prescription medication at non-prescription strength"},
    {"key": "tetanus_immunization",
     "label": "Tetanus immunization"},
    {"key": "clean_flush_soak_surface_wound",
     "label": "Cleaning, flushing, or soaking wounds on the skin surface"},
    {"key": "wound_coverings",
     "label": "Wound coverings (bandages, Band-Aids, gauze pads, butterfly bandages, Steri-Strips)"},
    {"key": "hot_cold_therapy",
     "label": "Hot or cold therapy"},
    {"key": "non_rigid_support",
     "label": "Non-rigid means of support (elastic bandages, wraps, non-rigid back belts)"},
    {"key": "temporary_immobilization_for_transport",
     "label": "Temporary immobilization device used to transport an accident victim (splints, slings, neck collars, backboards)"},
    {"key": "drilling_nail_or_draining_blister",
     "label": "Drilling a fingernail/toenail to relieve pressure, or draining fluid from a blister"},
    {"key": "eye_patches",
     "label": "Eye patches"},
    {"key": "remove_foreign_body_eye_irrigation_swab",
     "label": "Removing foreign bodies from the eye using only irrigation or a cotton swab"},
    {"key": "remove_splinter_simple_means",
     "label": "Removing splinters/foreign material from areas other than the eye by irrigation, tweezers, cotton swabs, or simple means"},
    {"key": "finger_guards",
     "label": "Using finger guards"},
    {"key": "massage",
     "label": "Massage"},
    {"key": "drinking_fluids_heat_stress",
     "label": "Drinking fluids for relief of heat stress"},
]
_FIRST_AID_KEYS = {t["key"] for t in FIRST_AID_TREATMENTS}

# Treatments that are, by rule, ALWAYS beyond first aid (i.e., recordable trigger).
BEYOND_FIRST_AID_TREATMENTS: List[Dict[str, str]] = [
    {"key": "prescription_medication",
     "label": "Prescription medication (at any dose) — including a single dose"},
    {"key": "otc_at_prescription_strength",
     "label": "Over-the-counter medication used at PRESCRIPTION strength"},
    {"key": "sutures_staples",
     "label": "Sutures, staples, or surgical glue to close a wound"},
    {"key": "rigid_immobilization",
     "label": "Rigid means of support / immobilization for treatment (casts, rigid splints, hard braces)"},
    {"key": "physical_therapy_chiropractic",
     "label": "Physical therapy or chiropractic treatment"},
    {"key": "wound_closing_device_beyond_stripes",
     "label": "Any wound-closing device beyond skin closures (butterfly/Steri-Strip are first aid; devices beyond are not)"},
    {"key": "surgical_debridement",
     "label": "Surgical debridement or removal of dead tissue"},
    {"key": "iv_fluids",
     "label": "Administration of IV fluids to treat (not merely for heat-stress hydration)"},
    {"key": "device_to_remove_foreign_body",
     "label": "Removal of a foreign body from a wound using a device/procedure beyond simple means"},
]
_BEYOND_FIRST_AID_KEYS = {t["key"] for t in BEYOND_FIRST_AID_TREATMENTS}

# ─────────────────────────────────────────────────────────────────────────────
# 300 Log part-M "type of illness" columns (checkbox on the far right of the log)
# ─────────────────────────────────────────────────────────────────────────────
CASE_TYPES: List[Dict[str, str]] = [
    {"key": "injury", "label": "Injury"},
    {"key": "skin_disorder", "label": "Skin disorder"},
    {"key": "respiratory_condition", "label": "Respiratory condition"},
    {"key": "poisoning", "label": "Poisoning"},
    {"key": "hearing_loss", "label": "Hearing loss"},
    {"key": "all_other_illness", "label": "All other illnesses"},
]


# ─────────────────────────────────────────────────────────────────────────────
# The guided intake schema — the ordered questions a determination needs.
# The front-end renders these; the engine consumes the collected `facts`.
# ─────────────────────────────────────────────────────────────────────────────
def intake_schema() -> Dict[str, Any]:
    """Return the ordered set of questions + option catalogs for the UI."""
    return {
        "exceptions": WORK_RELATEDNESS_EXCEPTIONS,
        "first_aid": FIRST_AID_TREATMENTS,
        "beyond_first_aid": BEYOND_FIRST_AID_TREATMENTS,
        "case_types": CASE_TYPES,
        "questions": [
            {"id": "is_injury_or_illness", "type": "yesno",
             "q": "Is there an actual injury or illness (a diagnosis, or signs/symptoms)?",
             "help": "A near-miss with no injury is not recordable. There must be an injury or illness."},
            {"id": "work_related", "type": "select",
             "q": "Did an event or exposure in the work environment cause, contribute to, or significantly aggravate the condition?",
             "options": [
                 {"key": "yes", "label": "Yes — work caused, contributed, or aggravated it"},
                 {"key": "exception", "label": "No — one of the 1904.5(b)(2) exceptions applies"},
                 {"key": "unsure", "label": "Unsure / need to apply the exceptions"},
             ],
             "help": "1904.5 presumes work-relatedness for anything from the work environment unless a listed exception applies."},
            {"id": "exception", "type": "select_exception",
             "q": "Which exception applies?",
             "show_if": {"work_related": "exception"}},
            {"id": "new_case", "type": "yesno",
             "q": "Is this a NEW case (not a recurrence of a previously recorded injury to the same body part)?",
             "help": "1904.6: a new case is one the employee had not previously experienced, or from which they had fully recovered before a new event."},
            {"id": "death", "type": "yesno",
             "q": "Did the case result in death?"},
            {"id": "days_away", "type": "yesno",
             "q": "Did it result in one or more days away from work (beyond the day of injury)?"},
            {"id": "days_away_count", "type": "number",
             "q": "How many calendar days away from work? (count begins the day AFTER injury; cap at 180)",
             "show_if": {"days_away": True}},
            {"id": "restricted_or_transfer", "type": "yesno",
             "q": "Did it result in restricted work / a job transfer (employee kept working but limited)?"},
            {"id": "restricted_count", "type": "number",
             "q": "How many calendar days of restriction/transfer? (cap at 180)",
             "show_if": {"restricted_or_transfer": True}},
            {"id": "loss_of_consciousness", "type": "yesno",
             "q": "Did the employee lose consciousness?"},
            {"id": "significant_diagnosis", "type": "yesno",
             "q": "Did a physician/PLHCP diagnose a significant injury/illness (fractured/cracked bone, punctured eardrum, cancer, chronic irreversible disease)?"},
            {"id": "treatments", "type": "multi_treatment",
             "q": "What treatment was given? (select all that apply)",
             "help": "If ANY treatment beyond the closed first-aid list was given, the case is recordable."},
            {"id": "case_type", "type": "select_case_type",
             "q": "What is the type of case? (for the 300 Log illness column)"},
            # Reporting (1904.39) — separate from recordability, applies to ALL employers.
            {"id": "hospitalized_inpatient", "type": "yesno",
             "q": "Was the employee admitted in-patient to a hospital (formal admission, not just ER treatment)?"},
            {"id": "amputation", "type": "yesno",
             "q": "Was there an amputation (including a fingertip amputation with bone loss)?"},
            {"id": "loss_of_eye", "type": "yesno",
             "q": "Was there a loss of an eye?"},
            {"id": "privacy_concern", "type": "yesno",
             "q": "Is this a privacy-concern case (intimate body part, sexual assault, mental illness, HIV/HBV/TB, contaminated needlestick, or employee asked to withhold name)?"},
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────
def _b(facts: Dict[str, Any], key: str) -> Optional[bool]:
    """Read a tri-state boolean: True / False / None(unanswered)."""
    v = facts.get(key)
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("yes", "y", "true", "1"):
            return True
        if s in ("no", "n", "false", "0"):
            return False
    return None


def _cap180(n: Any) -> Optional[int]:
    try:
        v = int(n)
    except (TypeError, ValueError):
        return None
    if v < 0:
        v = 0
    return min(v, 180)


def _treatments_beyond_first_aid(facts: Dict[str, Any]) -> Tuple[Optional[bool], List[str]]:
    """
    Return (beyond_first_aid?, offending_labels).
    True  -> at least one selected treatment is beyond the closed first-aid list.
    False -> treatments were selected and ALL are on the first-aid list.
    None  -> no treatment info was provided.
    """
    tx = facts.get("treatments")
    if not tx:
        return None, []
    if isinstance(tx, str):
        tx = [tx]
    beyond: List[str] = []
    label_by_key = {t["key"]: t["label"] for t in (FIRST_AID_TREATMENTS + BEYOND_FIRST_AID_TREATMENTS)}
    for k in tx:
        if k in _BEYOND_FIRST_AID_KEYS:
            beyond.append(label_by_key.get(k, k))
        elif k in _FIRST_AID_KEYS:
            continue
        else:
            # Unknown treatment key — treat as beyond first aid, flagged, to stay conservative.
            beyond.append(str(k))
    return (len(beyond) > 0), beyond


# ─────────────────────────────────────────────────────────────────────────────
# Core evaluation
# ─────────────────────────────────────────────────────────────────────────────
def evaluate(facts: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluate a single case. Returns a determination dict:
      {
        "recordable": True/False/None,
        "column": "G"|"H"|"I"|"J"|None,       # 300 Log outcome column
        "column_label": "...",
        "case_type": "...",
        "days_away": int|None,
        "restricted_days": int|None,
        "work_related": True/False/None,
        "new_case": True/False/None,
        "reporting": {...},                    # 1904.39 obligations
        "privacy_case": bool,
        "steps": [ {step, result, basis, citation}, ... ],   # the reasoning trail
        "needs": [ "question ids still required" ],
        "summary": "one-line plain-English verdict",
      }
    """
    steps: List[Dict[str, str]] = []
    needs: List[str] = []

    def step(name: str, result: str, basis: str, citation: str) -> None:
        steps.append({"step": name, "result": result, "basis": basis, "citation": citation})

    det: Dict[str, Any] = {
        "recordable": None, "column": None, "column_label": None,
        "case_type": facts.get("case_type") or "injury",
        "days_away": None, "restricted_days": None,
        "work_related": None, "new_case": None,
        "reporting": {}, "privacy_case": bool(_b(facts, "privacy_concern")),
        "steps": steps, "needs": needs, "summary": "",
    }

    # ── Gate 0: is there an injury/illness at all? ──────────────────────────
    inj = _b(facts, "is_injury_or_illness")
    if inj is False:
        step("Injury or illness present?", "No",
             "No injury or illness occurred (e.g., a near-miss). Nothing to record.",
             "29 CFR 1904.7(a)")
        det["recordable"] = False
        det["summary"] = "Not recordable — no injury or illness occurred."
        _attach_reporting(facts, det, steps)
        return det
    if inj is None:
        needs.append("is_injury_or_illness")

    # ── Gate 1: work-relatedness (1904.5) ───────────────────────────────────
    wr = facts.get("work_related")
    exception = facts.get("exception")
    if wr == "exception" or (exception in _EXCEPTION_KEYS):
        if exception in _EXCEPTION_KEYS:
            elabel = next((e["label"] for e in WORK_RELATEDNESS_EXCEPTIONS if e["key"] == exception), exception)
            step("Work-related?", "No — exception applies",
                 f"1904.5(b)(2) exception: {elabel}. Not work-related, so not recordable. "
                 f"Document the basis for the not-work-related call in case of audit.",
                 "29 CFR 1904.5(b)(2)")
            det["work_related"] = False
            det["recordable"] = False
            det["summary"] = "Not recordable — a 1904.5(b)(2) exception makes this not work-related."
            _attach_reporting(facts, det, steps)
            return det
        else:
            needs.append("exception")
    elif wr in ("yes", True):
        det["work_related"] = True
        step("Work-related?", "Yes",
             "An event/exposure in the work environment caused, contributed to, or significantly "
             "aggravated the condition, and no 1904.5(b)(2) exception applies. Work-relatedness is presumed.",
             "29 CFR 1904.5(a)")
    elif wr in ("no", False):
        det["work_related"] = False
        det["recordable"] = False
        step("Work-related?", "No",
             "The condition did not arise from the work environment. Not recordable.",
             "29 CFR 1904.5")
        det["summary"] = "Not recordable — the case is not work-related."
        _attach_reporting(facts, det, steps)
        return det
    else:
        needs.append("work_related")

    # ── Gate 2: new case (1904.6) ────────────────────────────────────────────
    nc = _b(facts, "new_case")
    det["new_case"] = nc
    if nc is False:
        step("New case?", "No",
             "This is a recurrence/continuation of a previously recorded case for the same body part, "
             "not a new case. Do not open a new 300 Log line; update the existing case if severity changed.",
             "29 CFR 1904.6")
        det["recordable"] = False
        det["summary"] = "Not a new case — update the existing 300 Log entry rather than recording a new one."
        _attach_reporting(facts, det, steps)
        return det
    elif nc is True:
        step("New case?", "Yes",
             "The employee had not previously experienced this injury/illness to this body part, or had "
             "fully recovered before a new work event caused it. Evaluate against 1904.7.",
             "29 CFR 1904.6")
    else:
        needs.append("new_case")

    # ── Gate 3: general recording criteria (1904.7) ─────────────────────────
    death = _b(facts, "death")
    days_away = _b(facts, "days_away")
    restricted = _b(facts, "restricted_or_transfer")
    loc = _b(facts, "loss_of_consciousness")
    sig = _b(facts, "significant_diagnosis")
    beyond_fa, beyond_labels = _treatments_beyond_first_aid(facts)

    days_away_n = _cap180(facts.get("days_away_count")) if days_away else None
    restricted_n = _cap180(facts.get("restricted_count")) if restricted else None
    det["days_away"] = days_away_n
    det["restricted_days"] = restricted_n

    triggers: List[Tuple[str, str]] = []  # (criterion, human basis)
    if death:
        triggers.append(("death", "The case resulted in death."))
    if days_away:
        triggers.append(("days_away",
                         f"The case resulted in days away from work"
                         + (f" ({days_away_n} calendar day(s), counted from the day after injury, capped at 180)."
                            if days_away_n is not None else " (day count still needed).")))
    if restricted:
        triggers.append(("restricted",
                         f"The case resulted in restricted work or job transfer"
                         + (f" ({restricted_n} calendar day(s))." if restricted_n is not None else ".")))
    if beyond_fa:
        triggers.append(("medical_beyond_first_aid",
                         "Medical treatment beyond first aid was given: "
                         + "; ".join(beyond_labels) + "."))
    if loc:
        triggers.append(("loss_of_consciousness", "The employee lost consciousness."))
    if sig:
        triggers.append(("significant_diagnosis",
                         "A physician/PLHCP diagnosed a significant injury/illness (e.g., fracture, "
                         "punctured eardrum, cancer, chronic irreversible disease)."))

    # If treatment info missing and no other trigger fired, we can't be sure.
    core_answered = all(x is not None for x in [death, days_away, restricted, loc, sig]) and beyond_fa is not None
    if beyond_fa is None:
        needs.append("treatments")

    if triggers:
        det["recordable"] = True
        crit_list = ", ".join(t[0].replace("_", " ") for t in triggers)
        step("Recordable? (general criteria)", "Yes",
             "Recordable because at least one 1904.7 general recording criterion is met — "
             + " ".join(b for _, b in triggers)
             + " Under 1904.7(b)(5), any treatment not on the closed first-aid list is 'medical treatment "
               "beyond first aid.'",
             "29 CFR 1904.7")
        det["_triggers"] = [t[0] for t in triggers]
    elif core_answered and (inj is not False):
        det["recordable"] = False
        step("Recordable? (general criteria)", "No",
             "None of the 1904.7 general recording criteria are met: no death, no days away, no "
             "restriction/transfer, only first-aid treatment, no loss of consciousness, and no "
             "significant diagnosis. First aid alone does not make a case recordable.",
             "29 CFR 1904.7(b)(5)")
        det["summary"] = "Not recordable — work-related, but only first aid; no 1904.7 criterion is met."
    else:
        # Missing inputs — keep recordable as None and report what's needed.
        step("Recordable? (general criteria)", "Need more info",
             "Cannot finalize until the outstanding question(s) are answered: "
             + ", ".join(needs) + ".",
             "29 CFR 1904.7")

    # ── 300 Log column (most severe outcome wins) ───────────────────────────
    if det["recordable"] is True:
        if death:
            det["column"], det["column_label"] = "G", "Death"
        elif days_away:
            det["column"], det["column_label"] = "H", "Days away from work"
        elif restricted:
            det["column"], det["column_label"] = "I", "Job transfer or restriction"
        else:
            det["column"], det["column_label"] = "J", "Other recordable case"
        step("300 Log column", f"Column {det['column']} — {det['column_label']}",
             "Check the single most severe outcome: Death (G) > Days away (H) > "
             "Restriction/transfer (I) > Other recordable (J). Record only one outcome column per case.",
             "29 CFR 1904.7(b)(1); 1904.29")

    # ── Reporting to OSHA (1904.39) — always evaluated ──────────────────────
    _attach_reporting(facts, det, steps)

    # ── Privacy-concern handling (1904.29(b)(6)-(9)) ────────────────────────
    if det["privacy_case"] and det["recordable"]:
        step("Privacy-concern case", "Yes",
             "Do NOT enter the employee's name on the 300 Log — write 'privacy case' and keep a separate, "
             "confidential list linking the case number to the name.",
             "29 CFR 1904.29(b)(6)-(9)")

    # ── Summary line ────────────────────────────────────────────────────────
    if needs:
        det["summary"] = det["summary"] or ("Almost there — still need: " + ", ".join(sorted(set(needs))) + ".")
    elif det["recordable"] is True:
        rep = det["reporting"]
        extra = ""
        if rep.get("required"):
            extra = f" ALSO report to OSHA within {rep['deadline_hours']}h ({rep['reason']})."
        det["summary"] = (f"RECORDABLE — log on the OSHA 300 as Column {det['column']} "
                          f"({det['column_label']}).{extra}")
    elif det["recordable"] is False and not det["summary"]:
        det["summary"] = "Not recordable."

    det["needs"] = sorted(set(needs))
    return det


def _attach_reporting(facts: Dict[str, Any], det: Dict[str, Any], steps: List[Dict[str, str]]) -> None:
    """
    1904.39 severe-event reporting — SEPARATE from the 300 Log and applies to ALL
    employers, even partially exempt ones. Fatality: 8 hours. In-patient
    hospitalization / amputation / loss of eye: 24 hours.
    """
    death = _b(facts, "death")
    hosp = _b(facts, "hospitalized_inpatient")
    amp = _b(facts, "amputation")
    eye = _b(facts, "loss_of_eye")

    reporting: Dict[str, Any] = {"required": False, "deadline_hours": None, "reason": "", "how": ""}
    # Only meaningful if the event is work-related (or not yet ruled out).
    not_work_related = det.get("work_related") is False

    if death and not not_work_related:
        reporting = {
            "required": True, "deadline_hours": 8,
            "reason": "work-related fatality",
            "how": "Report within 8 hours of learning of it — OSHA 1-800-321-6742, the online form, or the nearest area office.",
        }
        steps.append({"step": "Report to OSHA?", "result": "YES — within 8 hours",
                      "basis": "A work-related fatality must be reported within 8 hours (reportable if death occurs within 30 days of the incident). Applies to all employers.",
                      "citation": "29 CFR 1904.39(a)(1),(b)(6)"})
    elif (hosp or amp or eye) and not not_work_related:
        reasons = []
        if hosp:
            reasons.append("in-patient hospitalization")
        if amp:
            reasons.append("amputation")
        if eye:
            reasons.append("loss of an eye")
        reporting = {
            "required": True, "deadline_hours": 24,
            "reason": " / ".join(reasons),
            "how": "Report within 24 hours — OSHA 1-800-321-6742, the online form, or the nearest area office.",
        }
        steps.append({"step": "Report to OSHA?", "result": "YES — within 24 hours",
                      "basis": "A work-related in-patient hospitalization, amputation, or loss of an eye must be reported within 24 hours (reportable if it occurs within 24 hours of the incident). An amputation includes a fingertip amputation with bone loss. Applies to all employers; late reporting is a separate, citable violation.",
                      "citation": "29 CFR 1904.39(a)(2),(b)(7)-(8)"})
    else:
        # Only add a 'no report' note if we actually asked the severe-event questions.
        if any(v is not None for v in (death, hosp, amp, eye)):
            steps.append({"step": "Report to OSHA?", "result": "No",
                          "basis": "No work-related fatality, in-patient hospitalization, amputation, or loss of an eye — no 1904.39 report is triggered.",
                          "citation": "29 CFR 1904.39"})
    det["reporting"] = reporting
