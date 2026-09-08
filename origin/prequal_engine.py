"""Prequal Readiness Engine — the Safety Intelligence Engine's customer-facing
ISN / Avetta / Veriforce / PEC compliance layer (Phase 5).

Phases 1-4 build the company's real safety posture: the sourced regulatory
analysis (P1), the company profile + deterministic risk from open corrective
actions (P2), the written-program / training / JHA package (P3), and the photo
walk-through audit that feeds new CAPAs (P4). Phase 5 answers the question every
contractor actually gets paid or rejected on:

    "If my hiring client runs me through ISNetworld / Avetta / Veriforce today,
     do I pass — and if not, exactly what do I still owe?"

It does that WITHOUT inventing anything. It:

  1. Pulls the company's own posture out of the earlier phases — how many mandated
     written programs Origin has assembled vs. how many are still missing
     (program_engine.build_package), and the open / overdue corrective actions
     (company_profile.compute_risk).
  2. Folds in the safety statistics that Origin genuinely cannot derive (EMR, TRIR,
     DART, insurance status) — these are USER-SUPPLIED, never guessed. If they're
     absent, the engine says so and grades only what it has.
  3. Calls the research-grounded, clearly-labelled ESTIMATE grader
     (compliance_grading.estimate_grade) for the requested platform.
  4. Produces a GAP LIST where every item is traceable to a real
     prequal-platform knowledge-base record (compliance_kb rejection reasons) —
     split honestly into what Origin DETECTED from the company's own data and what
     the contractor must still SELF-SUPPLY (records Origin can't see, like a
     carrier EMR letter or COI endorsements).

House rules, identical to every other SIE module: deterministic, fully offline
(no LLM anywhere), never-fabricate, single-classification, and isolated +
non-fatal registration so a bug here can never take down the live app.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import company_profile as company
from . import program_engine as program
from . import compliance_grading as grading
from . import compliance_kb as kb

# Bridge the grader's canonical platform key to the knowledge-base platform name.
# BROWZ routes to Avetta and PEC routes to Veriforce post-consolidation, so their
# rejection logic (and KB records) are the acquirer's.
_CANON_TO_KB: Dict[str, str] = {
    "ISN": "ISNetworld",
    "Avetta": "Avetta",
    "Veriforce": "Veriforce",
    "PEC": "Veriforce",
    "BROWZ": "Avetta",
}

# The three live networks a company is realistically graded on when it hasn't told
# us which platforms its clients mandate.
_DEFAULT_PLATFORMS = ["ISN", "Avetta", "Veriforce"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── knowledge-base lookups (sourced, cached upstream) ─────────────────────────
def _platform_rejection_record(canon: str) -> Optional[Dict[str, Any]]:
    kb_name = _CANON_TO_KB.get(canon, "ISNetworld")
    for r in kb.prequal_knowledge():
        if r.get("platform") == kb_name and r.get("topic") == "rejection reasons":
            return r
    return None


def _master_rejection_record() -> Optional[Dict[str, Any]]:
    for r in kb.prequal_knowledge():
        if r.get("id") == "prequal-master-rejections":
            return r
    return None


def _reason_source(record: Optional[Dict[str, Any]], needle: str) -> Optional[Dict[str, str]]:
    """Find the verbatim rejection-reason string that matches `needle` inside a KB
    record and return it as a traceable source. Never fabricates a reason — if the
    record has no matching line, returns None."""
    if not record:
        return None
    low = needle.lower()
    for reason in record.get("rejection_reasons", []):
        if low in reason.lower():
            return {"record_id": record.get("id", ""), "platform": record.get("platform", ""),
                    "reason": reason}
    return None


# ── metric assembly (user-supplied only — never derived/guessed) ──────────────
_METRIC_KEYS = (
    "emr", "emr_cap", "trir", "trir_cap", "dart", "dart_cap",
    "insurance_ok", "insurance_required", "insurance_met",
    "msq_complete", "training_complete", "fatalities", "open_citations",
)


def platform_requirements(platform: str) -> Dict[str, Any]:
    """The sourced rejection-reason checklist for a platform (what a reviewer marks
    a submission down for), plus the cross-platform master list. Retrieval only."""
    canon = grading.estimate_grade(platform, {}).get("platform", "ISN") \
        if platform else "ISN"
    # estimate_grade with empty inputs still canonicalizes the platform for us.
    rec = _platform_rejection_record(canon)
    master = _master_rejection_record()
    return {
        "platform": canon,
        "kb_platform": _CANON_TO_KB.get(canon, "ISNetworld"),
        "platform_rejection_reasons": (rec or {}).get("rejection_reasons", []),
        "platform_record_id": (rec or {}).get("id", ""),
        "master_rejection_reasons": (master or {}).get("rejection_reasons", []),
        "master_record_id": (master or {}).get("id", ""),
    }


# ── remediation / one-click "Fix it" descriptors ───────────────────────────────
# For every gap Origin can act on, this says what Origin will generate and drop
# straight into the sub's document vault. auto=True surfaces a "Fix it" button in
# the portal. `produces` is the plain-English name of the document it creates.
# (human-review is intentionally absent — it needs a person, not a document.)
FIX_SPECS: Dict[str, Dict[str, Any]] = {
    "programs-missing":     {"auto": True, "label": "Build the manual",
                             "produces": "Company-specific written safety manual (all mandated programs)"},
    "programs-specificity": {"auto": True, "label": "Build the manual",
                             "produces": "Company-specific written safety manual (all mandated programs)"},
    "dna-program":          {"auto": True, "label": "Generate program",
                             "produces": "Drug & Alcohol (DOT) written program"},
    "training-records":     {"auto": True, "label": "Build the matrix",
                             "produces": "Individual training-record matrix"},
    "oq-individual":        {"auto": True, "label": "Build the tracker",
                             "produces": "Operator Qualification per-individual + task tracker"},
    "emr-letter":           {"auto": True, "label": "Draft the letter",
                             "produces": "Carrier EMR request letter"},
    "coi-endorsements":     {"auto": True, "label": "Draft the letter",
                             "produces": "COI + endorsements broker request letter"},
    "rates-reconcile":      {"auto": True, "label": "Build the worksheet",
                             "produces": "TRIR / DART reconciliation worksheet"},
    "capa-overdue":         {"auto": True, "label": "Build the record",
                             "produces": "CAPA closure record"},
    "capa-open":            {"auto": True, "label": "Build the record",
                             "produces": "CAPA closure record"},
}


# ── gap builder ───────────────────────────────────────────────────────────────
def _gap(gid: str, severity: str, status: str, title: str, detail: str,
         source: Optional[Dict[str, str]]) -> Dict[str, Any]:
    gap = {"id": gid, "severity": severity, "status": status,
           "title": title, "detail": detail, "source": source or {}}
    fix = FIX_SPECS.get(gid)
    if fix:
        gap["fix"] = dict(fix)
    return gap


def _build_gaps(canon: str, pkg: Dict[str, Any], risk: Dict[str, Any],
                metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Deterministic gap list. `status` is the honest part:
      - "detected"  = Origin confirmed this from the company's own data.
      - "checklist" = a record Origin cannot see; the contractor must self-supply
                      it. Every gap is sourced to a real KB rejection reason.
    """
    plat = _platform_rejection_record(canon)
    master = _master_rejection_record()
    gaps: List[Dict[str, Any]] = []

    summary = pkg.get("summary", {}) if pkg else {}
    missing = int(summary.get("programs_mandated_missing", 0) or 0)
    mandated = int(summary.get("programs_mandated", 0) or 0)

    # 1. Mandated written programs the library still can't supply (DETECTED).
    if missing > 0:
        gaps.append(_gap(
            "programs-missing", "high", "detected",
            f"{missing} of {mandated} mandated written program(s) not yet assembled",
            "These OSHA-mandated written programs are in scope but not yet in the "
            "Origin package. A missing required program is a direct RAVS/T-RAVS/"
            "Safety-Manual-Audit deficiency.",
            _reason_source(plat, "missing") or _reason_source(master, "template"),
        ))
    elif mandated > 0:
        # Origin supplies the programs — surface the #1 failure as a verify item.
        gaps.append(_gap(
            "programs-specificity", "info", "checklist",
            f"All {mandated} mandated written programs assembled — verify company-specificity",
            "Origin builds each program scope-specific, assertive ('will/shall'), "
            "with named responsible parties and inspection frequencies. Confirm the "
            "final documents read as YOUR company's before upload — a generic/template "
            "manual is every platform's #1 review failure.",
            _reason_source(master, "template"),
        ))

    # 2. Overdue corrective actions (DETECTED) — a citation with an unclosed
    #    corrective action is a documented rejection trigger.
    overdue = int(risk.get("overdue_capas", 0) or 0)
    open_capas = int(risk.get("open_capas", 0) or 0)
    if overdue > 0:
        gaps.append(_gap(
            "capa-overdue", "high", "detected",
            f"{overdue} corrective action(s) past the OSHA abatement deadline",
            "An open citation with no closed/documented corrective action is a "
            "rejection trigger across every platform. Close and document these first.",
            _reason_source(master, "corrective action")
            or _reason_source(plat, "corrective action"),
        ))
    elif open_capas > 0:
        gaps.append(_gap(
            "capa-open", "medium", "detected",
            f"{open_capas} corrective action(s) in progress — document closure",
            "Corrective actions are underway and on time. Attach the closure "
            "evidence (what was fixed, when, by whom) so any related citation shows "
            "a documented corrective action at review.",
            _reason_source(master, "corrective action"),
        ))
    if risk.get("human_review_open"):
        gaps.append(_gap(
            "human-review", "high", "detected",
            "At least one finding is flagged for human review",
            "A finding Origin could not deterministically match to a source is "
            "awaiting a human decision. Resolve it before representing readiness.",
            None,
        ))

    # 3. Safety statistics Origin cannot derive (CHECKLIST unless supplied).
    if metrics.get("emr") is None:
        gaps.append(_gap(
            "emr-letter", "high", "checklist",
            "Carrier-issued EMR letter not on file",
            "Provide the current EMR on a letter issued by the insurance CARRIER. "
            "A broker summary or premium statement is rejected. Origin cannot see "
            "your EMR — supply it to grade the safety-statistics component.",
            _reason_source(master, "EMR") or _reason_source(plat, "EMR"),
        ))
    if metrics.get("trir") is None or metrics.get("dart") is None:
        gaps.append(_gap(
            "rates-reconcile", "high", "checklist",
            "TRIR / DART not supplied or not reconciled to the OSHA 300/300A",
            "Enter TRIR and DART computed from your OSHA 300/300A hours and cases. "
            "Reported rates that don't reconcile with the uploaded logs (usually a "
            "hours-worked error) are the #2 cross-platform rejection reason.",
            _reason_source(master, "reconcile") or _reason_source(master, "300"),
        ))

    # 4. Insurance / COI (CHECKLIST unless confirmed).
    if metrics.get("insurance_ok") is not True and not metrics.get("insurance_required"):
        gaps.append(_gap(
            "coi-endorsements", "high", "checklist",
            "COI + endorsements not confirmed",
            "Upload a COI meeting the client's limits with additional-insured "
            "(CG 20 10 / CG 20 37), blanket waiver of subrogation, primary & "
            "non-contributory wording, named insured matching your registration "
            "exactly, and effective dates before work start.",
            _reason_source(master, "Insurance") or _reason_source(plat, "COI"),
        ))

    # 5. Training records (CHECKLIST unless a completeness ratio is supplied).
    tc = metrics.get("training_complete")
    if tc is None or (isinstance(tc, (int, float)) and float(tc) < 1.0):
        gaps.append(_gap(
            "training-records", "medium", "checklist",
            "Individual training records with names, dates, competency",
            "Training must be evidenced per individual — names, completion dates, "
            "and a competency check, not a sign-in sheet only. Origin's programs "
            "state the training requirement; you supply the completion records.",
            _reason_source(master, "Training") or _reason_source(plat, "Training"),
        ))

    # 6. Veriforce/PEC-specific: OQ per individual+task and Drug & Alcohol program.
    if canon in ("Veriforce", "PEC"):
        gaps.append(_gap(
            "oq-individual", "medium", "checklist",
            "Operator Qualification per individual + covered task (DOT scope)",
            "For DOT-covered pipeline/midstream work, OQ records must be at the "
            "individual worker + covered-task level with dates — not company level.",
            _reason_source(plat, "OQ") or _reason_source(master, "OQ"),
        ))
        gaps.append(_gap(
            "dna-program", "medium", "checklist",
            "Drug & Alcohol program documentation (DOT)",
            "Veriforce/DOT clients require documented Drug & Alcohol program "
            "records. Confirm the program and its records are current.",
            _reason_source(plat, "Drug") or _reason_source(master, "Drug"),
        ))

    return gaps


# ── the readiness assessment ──────────────────────────────────────────────────
def assess(company_id: str, platform: str = "ISN",
           metrics: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Estimate a profiled company's readiness on ONE platform, with a sourced gap
    list. Returns None if the company has no profile.

    metrics (all optional, user-supplied — Origin never guesses these):
      emr, emr_cap, trir, trir_cap, dart, dart_cap,
      insurance_ok (bool) | insurance_required + insurance_met (ints),
      msq_complete, training_complete (0..1), fatalities (int),
      open_citations (int, overrides the detected overdue count).
    """
    prof = company.get(company_id)
    if not prof:
        return None
    metrics = dict(metrics or {})

    pkg = program.build_package(company_id) or {}
    risk = company.compute_risk(company_id)
    summary = pkg.get("summary", {})

    programs_required = int(summary.get("programs_mandated", 0) or 0)
    programs_complete = programs_required - int(summary.get("programs_mandated_missing", 0) or 0)

    # Assemble grader inputs. Origin supplies what it truly knows (programs from
    # P3, overdue corrective actions from P2); the user supplies the rest.
    inputs: Dict[str, Any] = {
        "industry": prof.get("industry") or prof.get("naics") or "",
    }
    if programs_required:
        inputs["programs_required"] = programs_required
        inputs["programs_complete"] = max(0, programs_complete)
    # Only OVERDUE corrective actions read against the grade as "open citations";
    # on-time in-progress CAPAs are being worked and don't penalize.
    detected_open = int(risk.get("overdue_capas", 0) or 0)
    if detected_open:
        inputs["open_citations"] = detected_open
    for k in _METRIC_KEYS:
        if metrics.get(k) is not None:
            inputs[k] = metrics[k]

    grade = grading.estimate_grade(platform, inputs)
    canon = grade.get("platform", "ISN")
    gaps = _build_gaps(canon, pkg, risk, metrics)

    detected = [g for g in gaps if g["status"] == "detected"]
    checklist = [g for g in gaps if g["status"] == "checklist"]
    high = [g for g in gaps if g["severity"] == "high"]

    # Honest readiness read: the grade is the estimate; the gaps qualify it.
    if any(g["status"] == "detected" and g["severity"] == "high" for g in gaps):
        readiness = "Not ready — resolve the detected high-severity gaps first"
    elif high:
        readiness = "Conditional — supply the outstanding records to confirm the grade"
    else:
        readiness = "On track — verify the checklist items, then submit"

    return {
        "ok": True,
        "company_id": company._slug(company_id),
        "company": prof.get("company", ""),
        "platform": canon,
        "kb_platform": _CANON_TO_KB.get(canon, "ISNetworld"),
        "readiness": readiness,
        "estimated_grade": grade,
        "posture": {
            "programs_mandated": programs_required,
            "programs_missing": int(summary.get("programs_mandated_missing", 0) or 0),
            "programs_available": int(summary.get("programs_available", 0) or 0),
            "open_capas": int(risk.get("open_capas", 0) or 0),
            "overdue_capas": int(risk.get("overdue_capas", 0) or 0),
            "risk_band": risk.get("band"),
            "risk_score": risk.get("score"),
        },
        "metrics_supplied": {k: metrics[k] for k in _METRIC_KEYS if metrics.get(k) is not None},
        "gaps": gaps,
        "gap_counts": {"total": len(gaps), "detected": len(detected),
                       "checklist": len(checklist), "high": len(high)},
        "caveat": grading.CAVEAT,
        "assessed_at": _now(),
    }


def assess_all(company_id: str,
               metrics: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Assess readiness across every platform the company's clients mandate
    (profile.customer_platforms), falling back to the three live networks. Returns
    None if the company has no profile."""
    prof = company.get(company_id)
    if not prof:
        return None
    platforms = prof.get("customer_platforms") or []
    if not platforms:
        platforms = list(_DEFAULT_PLATFORMS)

    results: List[Dict[str, Any]] = []
    seen = set()
    for p in platforms:
        one = assess(company_id, p, metrics)
        if not one:
            continue
        canon = one["platform"]
        if canon in seen:   # BROWZ->Avetta, PEC->Veriforce can collapse; keep one
            continue
        seen.add(canon)
        results.append(one)

    return {
        "ok": True,
        "company_id": company._slug(company_id),
        "company": prof.get("company", ""),
        "platforms_assessed": [r["platform"] for r in results],
        "requested_platforms": platforms,
        "assessments": results,
        "note": "One well-written, scope-specific program set satisfies all three "
                "networks — build once. The scoring engine everywhere is the same "
                "three numbers (TRIR, EMR, DART) benchmarked to BLS by NAICS.",
        "caveat": grading.CAVEAT,
        "assessed_at": _now(),
    }


# ── routes ────────────────────────────────────────────────────────────────────
def register_prequal(app) -> None:
    """Attach Prequal Readiness routes. Isolated + non-fatal, mirroring the other
    SIE modules. Fully offline — no route consults an external model."""
    from fastapi import Body
    from fastapi.responses import JSONResponse

    @app.post("/api/prequal/{company_id}")
    def prequal_assess(company_id: str, body: dict = Body(default=None)):
        payload = body if isinstance(body, dict) else {}
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
        platform = payload.get("platform")
        try:
            if platform:
                out = assess(company_id, platform, metrics)
            else:
                out = assess_all(company_id, metrics)
            if out is None:
                return JSONResponse({"error": "company not found"}, status_code=404)
            return out
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/prequal/platforms")
    def prequal_platforms():
        master = _master_rejection_record() or {}
        return {
            "ok": True,
            "platforms": ["ISN", "Avetta", "Veriforce"],
            "note": "BROWZ routes to Avetta and PEC/ComplyWorks route to Veriforce "
                    "post-consolidation.",
            "master_rejection_reasons": master.get("rejection_reasons", []),
            "master_record_id": master.get("id", ""),
        }

    @app.get("/api/prequal/platform/{platform}")
    def prequal_platform(platform: str):
        try:
            return {"ok": True, **platform_requirements(platform)}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)
