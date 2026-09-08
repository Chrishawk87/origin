"""form_vault.py — Screen 3 "Form Vault": the official-government-form engine.

The mobile spec's third screen is a split view: on the left a worker answers a
short, plain-language form in about a minute; on the right Origin auto-fills the
*official government form* — the exact fields, in the exact sections, that an
inspector expects — and stamps it "Audit Ready" once every required field is
satisfied. This is the piece that turns a 60-second field entry into a filed,
inspection-grade record.

Built the Origin way, matching every other engine here:

  * Deterministic and offline — there is no LLM. A compact worker answer is
    projected onto the official field layout by an explicit, auditable mapping
    (copy, constant, today's date, join, or a small format template). Nothing is
    guessed; if a required official field has no source it is flagged, not faked.
  * File-based — completed fills are persisted per tenant under DATA_DIR, the
    same convention as brain_router / knowledge_store. No database.
  * Agency-scoped — the vault only offers the forms for the agencies a tenant has
    toggled on in the Brain Router (Screen 1). Toggle MSHA on and Form 5000-23
    appears; leave it off and a mining form can never surface for that account.
    Scope resolution is soft: if the router is unavailable, every form shows.

The form field schemas below mirror the real published layouts (MSHA 5000-23
Certificate of Training, OSHA Form 300A Summary, USACE Activity Hazard Analysis).
They are faithful representations for the auto-fill engine; the authoritative
blank form always lives at the linked government source, and a filled record
here is a field aid, not a substitute for the official filing.
"""

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .paths import DATA_DIR

_STORE = DATA_DIR / "form_vault"


# ── Form catalog ──────────────────────────────────────────────────────────────
# Each form ties to a Brain Router agency `code` (so scoping is a simple set
# check) and declares three things:
#   worker_inputs  — the short, plain-language questions the field person answers.
#   official_fields— the real form's fields, grouped by `section`, in filing order.
#   mapping        — official_field_id -> a rule that produces its value from the
#                    worker answers. Rules (deterministic, no LLM):
#                      {"from": id}                 copy a worker answer
#                      {"const": value}             a fixed value
#                      {"today": true}              today's date (YYYY-MM-DD)
#                      {"join": [ids], "sep": " "}  concatenate answers
#                      {"template": "{a}-{b}"}      format using answer ids
#                    An official field with no mapping is operator-entered on the
#                    official copy (shown blank, and — if required — counts against
#                    "Audit Ready" only when we truly can't source it).

FORM_CATALOG: List[Dict[str, Any]] = [
    # ── MSHA 5000-23 — Certificate of Training ────────────────────────────────
    {
        "id": "msha-5000-23",
        "agency": "MSHA",
        "authority": "MSHA",
        "title": "MSHA Form 5000-23 — Certificate of Training",
        "gov_url": "https://www.msha.gov/sites/default/files/Compliance%20and%20Enforcement/5000-23.pdf",
        "summary": "Certifies a miner completed required Part 46/48 training. One per miner per training event.",
        "worker_inputs": [
            {"id": "company", "label": "Company / operator name", "type": "text", "required": True},
            {"id": "mine_name", "label": "Mine name", "type": "text", "required": True},
            {"id": "mine_id", "label": "MSHA Mine ID", "type": "text", "placeholder": "e.g. 41-01234", "required": True},
            {"id": "miner_name", "label": "Miner's full name", "type": "text", "required": True},
            {"id": "ssn_last4", "label": "Miner SSN — last 4", "type": "text", "placeholder": "####", "required": False},
            {"id": "training_type", "label": "Type of training", "type": "select", "required": True,
             "options": ["New Miner (Part 46)", "New Miner (Part 48)", "Newly Employed Experienced Miner",
                          "Annual Refresher", "Task Training", "Hazard Training"]},
            {"id": "hours", "label": "Training hours", "type": "number", "required": True},
            {"id": "date_completed", "label": "Date completed", "type": "date", "required": True},
            {"id": "instructor", "label": "Instructor's name", "type": "text", "required": True},
        ],
        "official_fields": [
            {"id": "of_company", "label": "Company Name", "section": "Operator", "required": True},
            {"id": "of_mine_name", "label": "Mine Name", "section": "Operator", "required": True},
            {"id": "of_mine_id", "label": "Mine ID Number", "section": "Operator", "required": True},
            {"id": "of_miner", "label": "Name of Person Trained", "section": "Miner", "required": True},
            {"id": "of_ssn", "label": "Last 4 of SSN", "section": "Miner", "required": False},
            {"id": "of_type", "label": "Type of Training", "section": "Training", "required": True},
            {"id": "of_hours", "label": "Number of Training Hours", "section": "Training", "required": True},
            {"id": "of_date", "label": "Date of Training", "section": "Training", "required": True},
            {"id": "of_instructor", "label": "Instructor's Name", "section": "Certification", "required": True},
            {"id": "of_instr_sig", "label": "Instructor's Signature", "section": "Certification", "required": True,
             "signature": True},
        ],
        "mapping": {
            "of_company": {"from": "company"},
            "of_mine_name": {"from": "mine_name"},
            "of_mine_id": {"from": "mine_id"},
            "of_miner": {"from": "miner_name"},
            "of_ssn": {"from": "ssn_last4"},
            "of_type": {"from": "training_type"},
            "of_hours": {"from": "hours"},
            "of_date": {"from": "date_completed"},
            "of_instructor": {"from": "instructor"},
            # of_instr_sig — wet/e-signature applied on the official copy.
        },
    },
    # ── MSHA Workplace / Pre-Shift Examination Record — 30 CFR 56/57.18002 ─────
    {
        "id": "msha-wpe",
        "agency": "MSHA",
        "authority": "MSHA",
        "title": "MSHA Workplace Examination Record — 30 CFR 56/57.18002",
        "gov_url": "https://www.ecfr.gov/current/title-30/chapter-I/subchapter-N/part-56/subpart-Q/section-56.18002",
        "summary": "The each-shift working-place exam a competent person must record: areas examined, adverse conditions found, and corrective action. One per examined shift.",
        "worker_inputs": [
            {"id": "company", "label": "Company / operator name", "type": "text", "required": True},
            {"id": "mine_name", "label": "Mine name", "type": "text", "required": True},
            {"id": "mine_id", "label": "MSHA Mine ID", "type": "text", "placeholder": "e.g. 41-01234", "required": True},
            {"id": "exam_date", "label": "Date of examination", "type": "date", "required": True},
            {"id": "shift", "label": "Shift", "type": "select", "required": True,
             "options": ["Day", "Evening", "Night", "Other"]},
            {"id": "examiner", "label": "Competent person (examiner) name", "type": "text", "required": True},
            {"id": "areas", "label": "Working places examined", "type": "text", "required": True,
             "placeholder": "e.g. Primary crusher, north haul road, shop"},
            {"id": "adverse", "label": "Adverse conditions found?", "type": "select", "required": True,
             "options": ["No adverse conditions found", "Adverse conditions found"]},
            {"id": "conditions", "label": "Describe adverse conditions (if any)", "type": "text", "required": False,
             "placeholder": "Only if conditions were found"},
            {"id": "action", "label": "Corrective action taken", "type": "text", "required": False,
             "placeholder": "What was done to correct it"},
            {"id": "corrected_when", "label": "Date / time corrected", "type": "text", "required": False,
             "placeholder": "e.g. 2026-09-07 14:30, or 'promptly'"},
            {"id": "miners_notified", "label": "Miners notified of conditions?", "type": "select", "required": False,
             "options": ["Yes", "No", "N/A — none found"]},
        ],
        "official_fields": [
            {"id": "of_company", "label": "Operator Name", "section": "Operator", "required": True},
            {"id": "of_mine_name", "label": "Mine Name", "section": "Operator", "required": True},
            {"id": "of_mine_id", "label": "Mine ID Number", "section": "Operator", "required": True},
            {"id": "of_date", "label": "Date of Examination", "section": "Examination", "required": True},
            {"id": "of_shift", "label": "Shift", "section": "Examination", "required": True},
            {"id": "of_areas", "label": "Working Places Examined", "section": "Examination", "required": True},
            {"id": "of_examiner", "label": "Competent Person (name)", "section": "Examination", "required": True},
            {"id": "of_adverse", "label": "Adverse Conditions Found", "section": "Conditions & Corrective Action", "required": True},
            {"id": "of_conditions", "label": "Description of Adverse Conditions", "section": "Conditions & Corrective Action", "required": False},
            {"id": "of_action", "label": "Corrective Action Taken", "section": "Conditions & Corrective Action", "required": False},
            {"id": "of_corrected", "label": "Date / Time Corrected", "section": "Conditions & Corrective Action", "required": False},
            {"id": "of_notified", "label": "Miners Notified of Conditions", "section": "Conditions & Corrective Action", "required": False},
            {"id": "of_sig", "label": "Examiner's Signature", "section": "Certification", "required": True,
             "signature": True},
        ],
        "mapping": {
            "of_company": {"from": "company"},
            "of_mine_name": {"from": "mine_name"},
            "of_mine_id": {"from": "mine_id"},
            "of_date": {"from": "exam_date"},
            "of_shift": {"from": "shift"},
            "of_areas": {"from": "areas"},
            "of_examiner": {"from": "examiner"},
            "of_adverse": {"from": "adverse"},
            "of_conditions": {"from": "conditions"},
            "of_action": {"from": "action"},
            "of_corrected": {"from": "corrected_when"},
            "of_notified": {"from": "miners_notified"},
            # of_sig — competent person signs the official copy.
        },
    },
    # ── OSHA Form 300A — Summary of Work-Related Injuries and Illnesses ────────
    {
        "id": "osha-300a",
        "agency": "OSHA",
        "authority": "OSHA",
        "title": "OSHA Form 300A — Summary of Work-Related Injuries and Illnesses",
        "gov_url": "https://www.osha.gov/sites/default/files/OSHA-300-Forms.xls",
        "summary": "Year-end injury/illness summary. Posted Feb 1–Apr 30 and certified by a company executive.",
        "worker_inputs": [
            {"id": "establishment", "label": "Establishment name", "type": "text", "required": True},
            {"id": "city", "label": "City", "type": "text", "required": True},
            {"id": "state", "label": "State", "type": "text", "required": True},
            {"id": "industry", "label": "Industry description", "type": "text", "required": True},
            {"id": "naics", "label": "NAICS code", "type": "text", "placeholder": "e.g. 236220", "required": False},
            {"id": "avg_employees", "label": "Annual avg # of employees", "type": "number", "required": True},
            {"id": "total_hours", "label": "Total hours worked (all employees)", "type": "number", "required": True},
            {"id": "deaths", "label": "Total deaths", "type": "number", "required": True},
            {"id": "days_away_cases", "label": "Cases with days away from work", "type": "number", "required": True},
            {"id": "restricted_cases", "label": "Cases w/ job transfer or restriction", "type": "number", "required": True},
            {"id": "other_cases", "label": "Other recordable cases", "type": "number", "required": True},
            {"id": "exec_name", "label": "Certifying executive name", "type": "text", "required": True},
            {"id": "exec_title", "label": "Executive title", "type": "text", "required": True},
        ],
        "official_fields": [
            {"id": "of_estab", "label": "Establishment name", "section": "Establishment", "required": True},
            {"id": "of_citystate", "label": "City / State", "section": "Establishment", "required": True},
            {"id": "of_industry", "label": "Industry description", "section": "Establishment", "required": True},
            {"id": "of_naics", "label": "NAICS", "section": "Establishment", "required": False},
            {"id": "of_avg_emp", "label": "Annual average number of employees", "section": "Employment", "required": True},
            {"id": "of_hours", "label": "Total hours worked by all employees", "section": "Employment", "required": True},
            {"id": "of_g_deaths", "label": "(G) Total number of deaths", "section": "Case totals", "required": True},
            {"id": "of_h_daysaway", "label": "(H) Cases with days away from work", "section": "Case totals", "required": True},
            {"id": "of_i_restr", "label": "(I) Cases with job transfer/restriction", "section": "Case totals", "required": True},
            {"id": "of_j_other", "label": "(J) Other recordable cases", "section": "Case totals", "required": True},
            {"id": "of_exec", "label": "Company executive (name & title)", "section": "Certification", "required": True},
            {"id": "of_date", "label": "Date certified", "section": "Certification", "required": True},
        ],
        "mapping": {
            "of_estab": {"from": "establishment"},
            "of_citystate": {"join": ["city", "state"], "sep": ", "},
            "of_industry": {"from": "industry"},
            "of_naics": {"from": "naics"},
            "of_avg_emp": {"from": "avg_employees"},
            "of_hours": {"from": "total_hours"},
            "of_g_deaths": {"from": "deaths"},
            "of_h_daysaway": {"from": "days_away_cases"},
            "of_i_restr": {"from": "restricted_cases"},
            "of_j_other": {"from": "other_cases"},
            "of_exec": {"join": ["exec_name", "exec_title"], "sep": " — "},
            "of_date": {"today": True},
        },
    },
    # ── USACE Activity Hazard Analysis (AHA) — EM 385-1-1 ─────────────────────
    {
        "id": "usace-aha",
        "agency": "USACE",
        "authority": None,  # governed by EM 385-1-1, not a CFR citation lane
        "title": "USACE Activity Hazard Analysis (AHA)",
        "gov_url": "https://www.publications.usace.army.mil/Portals/76/Publications/EngineerManuals/EM_385-1-1.pdf",
        "summary": "Per-activity hazard analysis required before high-risk work on Corps projects. Header auto-fills; hazard rows come from the Checklist/AHA tool.",
        "worker_inputs": [
            {"id": "project", "label": "Project name", "type": "text", "required": True},
            {"id": "location", "label": "Location / work area", "type": "text", "required": True},
            {"id": "contract_no", "label": "Contract number", "type": "text", "required": False},
            {"id": "activity", "label": "Activity / work task", "type": "text", "required": True,
             "placeholder": "e.g. Excavation > 5 ft"},
            {"id": "rac", "label": "Overall Risk Assessment Code (RAC)", "type": "select", "required": True,
             "options": ["E - Extremely High", "H - High", "M - Medium", "L - Low"]},
            {"id": "prepared_by", "label": "Prepared by (name)", "type": "text", "required": True},
        ],
        "official_fields": [
            {"id": "of_project", "label": "Project", "section": "Header", "required": True},
            {"id": "of_location", "label": "Location", "section": "Header", "required": True},
            {"id": "of_contract", "label": "Contract Number", "section": "Header", "required": False},
            {"id": "of_activity", "label": "Activity / Work Task", "section": "Header", "required": True},
            {"id": "of_rac", "label": "Overall Risk Assessment Code (RAC)", "section": "Header", "required": True},
            {"id": "of_prepared", "label": "Prepared By", "section": "Header", "required": True},
            {"id": "of_date", "label": "Date Prepared", "section": "Header", "required": True},
            {"id": "of_rows", "label": "Job Steps · Hazards · Controls · RAC", "section": "Hazard Analysis",
             "required": True, "matrix": True},
        ],
        "mapping": {
            "of_project": {"from": "project"},
            "of_location": {"from": "location"},
            "of_contract": {"from": "contract_no"},
            "of_activity": {"from": "activity"},
            "of_rac": {"from": "rac"},
            "of_prepared": {"from": "prepared_by"},
            "of_date": {"today": True},
            # of_rows is filled from checklist_engine.aha_matrix(activity) at fill time.
        },
    },
]

_BY_ID: Dict[str, Dict[str, Any]] = {f["id"]: f for f in FORM_CATALOG}


# ── Brain Router scope (soft dependency) ──────────────────────────────────────
def _visible_agencies(gc_slug: Optional[str]) -> Optional[set]:
    """The agency codes whose forms this tenant may see, from the Brain Router's
    display set. None means "no scoping" (router unavailable) → show every form,
    preserving graceful behavior if the router isn't wired."""
    try:
        from . import brain_router
        return set(brain_router.effective_agencies(gc_slug))
    except Exception:
        return None


# ── Catalog / schema ──────────────────────────────────────────────────────────
def catalog(gc_slug: Optional[str] = None) -> List[Dict[str, Any]]:
    """The forms available to this tenant — every form whose agency is toggled on
    in the Brain Router (or all forms if the router is unavailable). A lightweight
    listing for the Screen-3 form picker."""
    allowed = _visible_agencies(gc_slug)
    out: List[Dict[str, Any]] = []
    for f in FORM_CATALOG:
        if allowed is not None and f["agency"] not in allowed:
            continue
        out.append({
            "id": f["id"], "agency": f["agency"], "title": f["title"],
            "summary": f["summary"], "gov_url": f["gov_url"],
            "input_count": len(f["worker_inputs"]),
            "field_count": len(f["official_fields"]),
        })
    return out


def form_schema(form_id: str, gc_slug: Optional[str] = None) -> Dict[str, Any]:
    """The full schema for one form: its short worker questionnaire and the
    official field layout (grouped by section). Returns an explicit 'ok': False
    when the form is unknown or not enabled for this tenant — never a fabrication."""
    f = _BY_ID.get(form_id)
    if not f:
        return {"ok": False, "error": f"No form '{form_id}' in the vault."}
    allowed = _visible_agencies(gc_slug)
    if allowed is not None and f["agency"] not in allowed:
        return {"ok": False, "error": f"The {f['agency']} brain is not enabled for this account. "
                                      f"Turn it on in the Brain Router to use this form."}
    # group official fields by section, preserving declared order
    sections: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    for of in f["official_fields"]:
        sec = of.get("section", "")
        if sec not in seen:
            grp = {"section": sec, "fields": []}
            seen[sec] = grp
            sections.append(grp)
        seen[sec]["fields"].append({k: of[k] for k in of})
    return {
        "ok": True, "id": f["id"], "agency": f["agency"], "title": f["title"],
        "summary": f["summary"], "gov_url": f["gov_url"],
        "worker_inputs": f["worker_inputs"], "sections": sections,
    }


# ── Fill (the auto-fill projection) ───────────────────────────────────────────
def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _apply_rule(rule: Dict[str, Any], answers: Dict[str, Any]) -> Optional[str]:
    """Project one worker-answer set onto one official field, deterministically.
    Returns the string value, or None if the rule's source(s) are absent."""
    if not isinstance(rule, dict):
        return None
    if "const" in rule:
        return str(rule["const"])
    if rule.get("today"):
        return _today()
    if "from" in rule:
        v = answers.get(rule["from"])
        return None if v in (None, "") else str(v)
    if "join" in rule:
        parts = [str(answers.get(i)) for i in rule["join"] if answers.get(i) not in (None, "")]
        return rule.get("sep", " ").join(parts) if parts else None
    if "template" in rule:
        try:
            out = rule["template"].format(**{k: (answers.get(k) or "") for k in answers})
            return out.strip() or None
        except Exception:
            return None
    return None


def _aha_rows(activity: str) -> List[Dict[str, Any]]:
    """Pull the hazard/control matrix for a USACE AHA from the checklist engine,
    which builds it from regulatory text. Soft dependency: returns [] if the
    engine or the activity isn't available, so the header still auto-fills."""
    try:
        from . import checklist_engine
        m = checklist_engine.aha_matrix(activity)
        if isinstance(m, dict) and m.get("ok"):
            return m.get("rows", []) or []
    except Exception:
        pass
    return []


def fill(form_id: str, answers: Optional[Dict[str, Any]] = None,
         gc_slug: Optional[str] = None, persist: bool = False) -> Dict[str, Any]:
    """Auto-fill the official form from a worker's short answers.

    Returns the official layout grouped by section with each field's resolved
    value (or null when unsourced), plus `audit_ready` — True only when every
    REQUIRED official field is satisfied. Deterministic; no LLM. Optionally
    persists the filled record per tenant."""
    f = _BY_ID.get(form_id)
    if not f:
        return {"ok": False, "error": f"No form '{form_id}' in the vault."}
    allowed = _visible_agencies(gc_slug)
    if allowed is not None and f["agency"] not in allowed:
        return {"ok": False, "error": f"The {f['agency']} brain is not enabled for this account."}
    answers = answers if isinstance(answers, dict) else {}

    sections: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    missing_required: List[str] = []

    for of in f["official_fields"]:
        sec = of.get("section", "")
        if sec not in seen:
            grp = {"section": sec, "fields": []}
            seen[sec] = grp
            sections.append(grp)

        rule = f["mapping"].get(of["id"])
        value = _apply_rule(rule, answers) if rule else None
        rows = None
        if of.get("matrix"):
            rows = _aha_rows(answers.get("activity") or "")

        # a required field is "satisfied" if it has a value, matrix rows, or is a
        # signature/operator-applied field (signed on the official copy, not here)
        satisfied = bool(value) or bool(rows) or bool(of.get("signature"))
        if of.get("required") and not satisfied:
            missing_required.append(of["label"])

        field_out = {
            "id": of["id"], "label": of["label"], "value": value,
            "required": bool(of.get("required")),
            "satisfied": satisfied,
        }
        if of.get("signature"):
            field_out["signature"] = True
            field_out["note"] = "Sign on the official copy"
        if of.get("matrix"):
            field_out["matrix"] = True
            field_out["rows"] = rows or []
            if not rows:
                field_out["note"] = "Add hazard rows in the Checklist / AHA tool"
        seen[sec]["fields"].append(field_out)

    audit_ready = len(missing_required) == 0
    result = {
        "ok": True, "id": f["id"], "agency": f["agency"], "title": f["title"],
        "gov_url": f["gov_url"], "sections": sections,
        "audit_ready": audit_ready, "missing_required": missing_required,
        "filled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if persist:
        try:
            _persist(form_id, answers, result, gc_slug)
        except Exception:
            pass  # persistence never breaks the fill
    return result


# ── Persistence (file-based, per tenant) ──────────────────────────────────────
def _key(gc_slug: Optional[str]) -> str:
    slug = (gc_slug or "").strip()
    return slug or "_owner"


def _persist(form_id: str, answers: Dict[str, Any], result: Dict[str, Any],
             gc_slug: Optional[str]) -> None:
    d = _STORE / _key(gc_slug)
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "form_id": form_id, "answers": answers,
        "audit_ready": result.get("audit_ready"),
        "missing_required": result.get("missing_required"),
        "filled_at": result.get("filled_at"),
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    (d / f"{form_id}-{stamp}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")


# ── Routes (isolated + tenant-scoped, same pattern as every SIE module) ────────
def register_form_vault(app) -> None:
    from fastapi import Body, Request
    from fastapi.responses import JSONResponse

    def _scope(request):
        st = getattr(request, "state", None)
        return getattr(st, "sie_gc_slug", None)

    @app.get("/api/form-vault/catalog")
    def form_vault_catalog(request: Request):
        try:
            return {"ok": True, "forms": catalog(_scope(request))}
        except Exception as exc:  # never 500 the tool
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/form-vault/form/{form_id}")
    def form_vault_schema(form_id: str, request: Request):
        try:
            return form_schema(form_id, _scope(request))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.post("/api/form-vault/fill")
    async def form_vault_fill(request: Request, body: dict = Body(default=None)):
        payload = body if isinstance(body, dict) else {}
        if not payload:
            try:
                payload = await request.json()
            except Exception:
                payload = {}
        form_id = (payload.get("form_id") or "").strip() if isinstance(payload, dict) else ""
        if not form_id:
            return JSONResponse({"error": "Provide 'form_id' and 'answers'."}, status_code=400)
        answers = payload.get("answers") if isinstance(payload, dict) else None
        persist = bool(payload.get("persist")) if isinstance(payload, dict) else False
        try:
            return fill(form_id, answers=answers, gc_slug=_scope(request), persist=persist)
        except Exception as exc:
            return JSONResponse({"error": f"Could not fill form: {exc}"}, status_code=200)

    @app.post("/api/form-vault/pdf")
    async def form_vault_pdf(request: Request, body: dict = Body(default=None)):
        """Render the auto-filled official form as a real, downloadable PDF file.

        Same tenant scoping as /fill. Returns application/pdf as an attachment on
        success; a JSON error (200) otherwise, so the tool never hard-fails."""
        from fastapi.responses import Response
        payload = body if isinstance(body, dict) else {}
        if not payload:
            try:
                payload = await request.json()
            except Exception:
                payload = {}
        form_id = (payload.get("form_id") or "").strip() if isinstance(payload, dict) else ""
        if not form_id:
            return JSONResponse({"error": "Provide 'form_id' and 'answers'."}, status_code=400)
        answers = payload.get("answers") if isinstance(payload, dict) else None
        try:
            from . import pdf_render
        except Exception as exc:
            return JSONResponse({"error": f"PDF engine unavailable: {exc}"}, status_code=200)
        try:
            out = pdf_render.render_pdf(form_id, answers=answers, gc_slug=_scope(request))
        except Exception as exc:
            return JSONResponse({"error": f"Could not render PDF: {exc}"}, status_code=200)
        if not out.get("ok"):
            return JSONResponse({"error": out.get("error", "render failed")}, status_code=200)
        headers = {
            "Content-Disposition": f'attachment; filename="{out["filename"]}"',
            "X-Audit-Ready": "1" if out.get("audit_ready") else "0",
            "X-Render-Mode": out.get("mode", "generated"),
            "Cache-Control": "no-store",
        }
        return Response(content=out["pdf"], media_type="application/pdf", headers=headers)
