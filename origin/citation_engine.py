"""Citation Engine — the Safety Intelligence Engine's OSHA-citation analyzer
(Phase 1).

Give it an OSHA citation (a standard number + the inspector's description, plus
whatever is known: inspection/citation number, classification, penalty, abatement
date) and it returns a complete, **fully-sourced 12-part analysis**:

    1.  summary                        7.  training
    2.  source (the verified standard) 8.  documentation
    3.  hazard explanation             9.  evidence checklist
    4.  immediate corrective action    10. follow-up checklist
    5.  recommended corrective action  11. recurrence prevention
    6.  root cause                     12. abatement tracking

Every one of those twelve outputs carries a single, explicit classification —
never mixed:

    OSHA-required   — traceable to a current CFR record in the knowledge store
    Origin-rec      — Origin's proprietary process / professional recommendation
    Best-practice   — industry consensus, not legally mandated
    Customer-req    — an operator / ISN / Avetta requirement (per-customer)

…plus the source records it rests on, a confidence score, and a human-review flag.

How it honors the platform's hard rules:
  * Deterministic core. The twelve outputs are computed by rule from the resolved
    standard (knowledge_store.resolve) + the OSHA 2254 training package + Origin's
    abatement playbook. No language model decides what the rule is.
  * Fully offline. Nothing here calls an external API. An optional explain hook can
    later re-phrase a body with a local model, but it is OFF by default, so the
    analyzer works with every AI key unset.
  * Never fabricates. If the cited standard resolves in neither the volume
    overrides nor the base corpus, the 'source' output says exactly:
    "Unable to verify this requirement from the current knowledge base," the whole
    analysis is flagged for human review, and no citation is invented.
  * Not legal advice. Willful / repeat / failure-to-abate classifications, and any
    injury/fatality language, force human_review_required = True. The disclaimer
    states Origin is not an attorney, OSHA official, or licensed safety professional.

State is file-based on the persistent volume (ORIGIN_DATA_DIR/citations), the same
proven pattern as portal.py / abatement.py, so a bug here can never break the app.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import compliance_kb as kb
from . import knowledge_store as ks

try:
    from .paths import DATA_DIR
except ImportError:
    import os as _os
    DATA_DIR = Path(_os.environ.get("ORIGIN_DATA_DIR") or (Path.home() / ".origin"))

CITATIONS_DIR = DATA_DIR / "citations"

# ── classification labels (single, never mixed) ─────────────────────────────
OSHA_REQUIRED = "OSHA-required"
ORIGIN_REC = "Origin recommendation"
BEST_PRACTICE = "Best practice"
CUSTOMER_REQ = "Customer requirement"

# Classifications that carry legal weight and always demand a human pass.
_HIGH_STAKES_CLASS = {"willful", "repeat", "failure_to_abate", "failure to abate"}
_HIGH_STAKES_WORDS = re.compile(
    r"\b(fatal|fatalit|death|died|amputat|hospitaliz|caught|struck-by|struck by|"
    r"engulf|electrocut|criminal)\w*", re.I)

_DISCLAIMER = (
    "Origin's analysis is a compliance aid, not legal advice. Origin is not an "
    "attorney, an OSHA representative, or a licensed professional engineer or safety "
    "professional. OSHA-required items are traceable to the cited standard; all other "
    "items are Origin's professional recommendations or industry best practice. "
    "Verify abatement obligations and deadlines with the issuing OSHA office."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _item(title: str, body: str, classification: str, *, basis: str = "",
          source_refs: Optional[List[Dict[str, str]]] = None,
          confidence: float = 0.7, human_review: bool = False) -> Dict[str, Any]:
    """One of the twelve outputs, in the standard envelope shape."""
    return {
        "title": title,
        "body": body,
        "classification": classification,
        "basis": basis,
        "source_refs": source_refs or [],
        "confidence": round(float(confidence), 2),
        "human_review_required": bool(human_review),
    }


# ── parsing an inbound citation ──────────────────────────────────────────────
_CFR_RE = re.compile(r"(?i)\b((?:29|49)\s*C\.?F\.?R\.?\s*)?(\d{3,4}\.\d+[A-Za-z]?(?:\([0-9A-Za-z]+\))*)")
_MONEY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)")
_INSP_RE = re.compile(r"(?i)inspection\s*#?\s*([0-9]{6,})")
_CIT_RE = re.compile(r"(?i)citation\s*#?\s*([0-9]+[a-z]?)")
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b")
_CLASS_MAP = [
    ("failure_to_abate", re.compile(r"(?i)failure[\s-]*to[\s-]*abate")),
    ("willful", re.compile(r"(?i)willful")),
    ("repeat", re.compile(r"(?i)repeat")),
    ("serious", re.compile(r"(?i)serious")),
    ("other", re.compile(r"(?i)other[\s-]*than[\s-]*serious|\bother\b")),
]


def parse_citation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize an inbound payload (structured fields and/or free text) into the
    citation facts the engine reasons over. Structured fields always win; free text
    only fills what's missing."""
    text = str(payload.get("text") or "")
    standard = (payload.get("standard") or payload.get("standard_cited") or "").strip()
    if not standard:
        m = _CFR_RE.search(text)
        if m:
            standard = (m.group(1) or "").strip() + m.group(2)
    # bare section for resolving
    sm = re.search(r"(\d{3,4}\.\d+[A-Za-z]?(?:\([0-9A-Za-z]+\))*)", standard or "")
    section = sm.group(1) if sm else ""

    classification = (payload.get("classification") or "").strip().lower()
    if not classification and text:
        for label, rx in _CLASS_MAP:
            if rx.search(text):
                classification = label
                break

    penalty = payload.get("penalty")
    if penalty in (None, "") and text:
        mm = _MONEY_RE.search(text)
        if mm:
            penalty = mm.group(0)

    abatement_date = (payload.get("abatement_date") or "").strip()
    if not abatement_date and text:
        dm = _DATE_RE.search(text)
        if dm:
            abatement_date = dm.group(1)

    inspection_number = (payload.get("inspection_number") or "").strip()
    if not inspection_number and text:
        im = _INSP_RE.search(text)
        if im:
            inspection_number = im.group(1)

    citation_number = (payload.get("citation_number") or "").strip()
    if not citation_number and text:
        cm = _CIT_RE.search(text)
        if cm:
            citation_number = cm.group(1)

    return {
        "standard": standard or section,
        "section": section,
        "paragraph": (payload.get("paragraph") or "").strip(),
        "description": (payload.get("description") or text or "").strip(),
        "classification": classification,
        "penalty": penalty or "",
        "abatement_date": abatement_date,
        "inspection_number": inspection_number,
        "citation_number": citation_number,
        "company": (payload.get("company") or "").strip(),
    }


def _ref(rec: Dict[str, Any]) -> Dict[str, str]:
    return {
        "citation": rec.get("regulation_number", ""),
        "title": rec.get("title", ""),
        "url": rec.get("source_url", ""),
        "version": rec.get("version_label", ""),
        "jurisdiction": rec.get("jurisdiction", ""),
    }


def _clip(s: str, n: int = 600) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + " …"


# ── the analysis ─────────────────────────────────────────────────────────────
def analyze(payload: Dict[str, Any], *, explain_provider: Any = None) -> Dict[str, Any]:
    """Produce the fully-sourced 12-part analysis for one OSHA citation.

    `explain_provider` is an optional local model used ONLY to re-phrase output
    bodies into friendlier prose. It is never consulted for regulatory facts and
    defaults to None, so the analysis is fully deterministic and offline.
    """
    facts = parse_citation(payload)
    std = facts["standard"]
    rec = ks.resolve(std) if std else None
    resolved = rec is not None
    ref = [_ref(rec)] if rec else []

    cls_raw = facts["classification"]
    high_stakes = (cls_raw in _HIGH_STAKES_CLASS) or bool(
        _HIGH_STAKES_WORDS.search(facts["description"]))

    outputs: Dict[str, Dict[str, Any]] = {}

    # 1) SUMMARY — the citation facts (OSHA-required framing)
    if resolved:
        bits = [f"OSHA cited {facts['company'] or 'the employer'} under {rec['regulation_number']}"]
        if rec.get("title"):
            bits[0] += f" ({rec['title']})"
        if cls_raw:
            bits.append(f"classified as {cls_raw.replace('_', ' ')}")
        if facts["penalty"]:
            bits.append(f"proposed penalty {facts['penalty']}")
        if facts["abatement_date"]:
            bits.append(f"abatement due {facts['abatement_date']}")
        summary = ". ".join([bits[0], "; ".join(bits[1:])]).strip("; .") + "."
        s_conf = rec["confidence"]
    else:
        summary = (f"A citation was recorded for standard '{std or '(none provided)'}', "
                   "but that standard could not be matched to Origin's knowledge base, "
                   "so its requirements cannot be stated with certainty.")
        s_conf = 0.0
    outputs["summary"] = _item(
        "Summary", summary, OSHA_REQUIRED,
        basis="Restates the issued citation.", source_refs=ref,
        confidence=s_conf, human_review=high_stakes or not resolved)

    # 2) SOURCE — the verified standard (or honest refusal)
    if resolved:
        body = f"{rec['regulation_number']} — {rec['title'] or '(untitled section)'}\n"
        body += f"Jurisdiction: {rec['jurisdiction']}  |  Knowledge version: {rec['version_label']}"
        if rec.get("superseded"):
            body += "  |  NOTE: this record has been superseded by a newer version."
        if rec.get("body_text"):
            body += "\n\nRegulatory text (verbatim excerpt):\n" + _clip(rec["body_text"], 800)
        if rec.get("source_url"):
            body += "\n\nOfficial source: " + rec["source_url"]
        outputs["source"] = _item(
            "Source", body, OSHA_REQUIRED,
            basis="Resolved against the versioned regulatory knowledge store.",
            source_refs=ref, confidence=rec["confidence"])
    else:
        outputs["source"] = _item(
            "Source", "Unable to verify this requirement from the current knowledge base.",
            OSHA_REQUIRED, basis="No matching current record in overrides or base corpus.",
            source_refs=[], confidence=0.0, human_review=True)

    # 3) HAZARD EXPLANATION
    if resolved and (rec.get("body_text") or rec.get("hazard_category")):
        hz = ", ".join([h for h in rec.get("hazard_category", []) if h]) or rec.get("title", "")
        body = (f"The cited condition concerns: {hz}. In plain terms, this standard exists to "
                "protect employees from that hazard; the citation asserts the required protection "
                "was not in place at the time of inspection.")
        outputs["hazard_explanation"] = _item(
            "Hazard explanation", body, OSHA_REQUIRED,
            basis="Derived from the resolved standard's subject and text.",
            source_refs=ref, confidence=min(rec["confidence"], 0.85))
    else:
        outputs["hazard_explanation"] = _item(
            "Hazard explanation",
            "Explain the specific hazard the cited condition creates for exposed employees, "
            "based on the inspector's description. (Standard text was not available to confirm "
            "the precise regulatory hazard.)",
            BEST_PRACTICE, basis="Generic hazard framing; standard text unavailable.",
            confidence=0.5, human_review=not resolved)

    # 4) IMMEDIATE CORRECTIVE ACTION (Origin's professional recommendation)
    outputs["immediate_corrective_action"] = _item(
        "Immediate corrective action",
        "Remove exposed employees from the hazard now: stop the affected task, "
        "barricade or guard the condition, tag out or lock out equipment as applicable, "
        "and document the interim protection with a dated photo. This holds exposure to "
        "zero while the permanent fix is put in place.",
        ORIGIN_REC, basis="Origin abatement playbook — interim protection first.",
        source_refs=ref, confidence=0.7, human_review=high_stakes)

    # 5) RECOMMENDED CORRECTIVE ACTION
    body5 = ("Put the permanent fix in place and prove it: correct the physical condition, "
             "update or create the governing written program, and verify the correction with "
             "a supervisor sign-off.")
    if resolved and rec.get("required_elements"):
        body5 += ("\n\nThe cited standard's required elements (OSHA-required — build these into "
                  "the program): " + _clip(str(rec["required_elements"]), 500))
    outputs["recommended_corrective_action"] = _item(
        "Recommended corrective action", body5, ORIGIN_REC,
        basis="Origin corrective-action method; required elements cited where available.",
        source_refs=ref, confidence=0.72, human_review=high_stakes)

    # 6) ROOT CAUSE
    outputs["root_cause"] = _item(
        "Root cause",
        "Determine why the condition existed — not just what was wrong. Work through the usual "
        "drivers: missing or unclear procedure, no training on the task, no inspection that would "
        "have caught it, production pressure, or equipment/design. Naming the true root cause is "
        "what keeps the citation from recurring under a different job.",
        ORIGIN_REC, basis="Origin root-cause method.", confidence=0.65)

    # 7) TRAINING — OSHA-required if the standard carries a training mandate
    tr = kb.training_requirement(std) if std else None
    if tr:
        tref = [{"citation": tr.get("citation", ""), "title": tr.get("standard_title", ""),
                 "url": tr.get("source", ""), "version": "OSHA-2254"}]
        outputs["training"] = _item(
            "Training", "This standard carries a training requirement. Verbatim OSHA training "
            "language:\n\n" + _clip(tr.get("training_requirement", ""), 800), OSHA_REQUIRED,
            basis="OSHA 2254 training package has an entry for this section.",
            source_refs=tref, confidence=0.9, human_review=high_stakes)
    else:
        outputs["training"] = _item(
            "Training", "Train and document the affected employees and their supervisors on the "
            "hazard and the corrected procedure; retain a dated sign-in sheet and the training "
            "content. (No verbatim OSHA training mandate is on file for this exact section.)",
            ORIGIN_REC, basis="No OSHA 2254 entry for this section.", confidence=0.65)

    # 8) DOCUMENTATION — OSHA-required if the standard has a recordkeeping duty
    if resolved and rec.get("recordkeeping"):
        outputs["documentation"] = _item(
            "Documentation", "The standard imposes a recordkeeping duty:\n\n"
            + _clip(str(rec["recordkeeping"]), 500), OSHA_REQUIRED,
            basis="Recordkeeping element present on the resolved standard.",
            source_refs=ref, confidence=min(rec["confidence"], 0.85), human_review=high_stakes)
    else:
        outputs["documentation"] = _item(
            "Documentation", "Assemble the abatement file: the written program covering this "
            "hazard, the corrective work order or invoice, dated before/after photos, and the "
            "training records. This file is what proves abatement to OSHA and to your customers.",
            ORIGIN_REC, basis="Origin documentation standard.", confidence=0.68)

    # 9) EVIDENCE CHECKLIST
    outputs["evidence_checklist"] = _item(
        "Evidence checklist",
        "Collect, dated: (1) photo of the corrected condition; (2) work order / invoice / receipt "
        "for the fix; (3) the revised or new written program; (4) training sign-in sheet; "
        "(5) employee acknowledgment; (6) a signed certification-of-abatement letter. This set "
        "satisfies both an OSHA abatement submission and an ISN/Avetta upload.",
        ORIGIN_REC, basis="Origin abatement-evidence set (OSHA + prequal).", confidence=0.7)

    # 10) FOLLOW-UP CHECKLIST
    due = facts["abatement_date"] or "the OSHA abatement date"
    outputs["followup_checklist"] = _item(
        "Follow-up checklist",
        f"Before {due}: complete the fix, assemble the evidence file, and submit the certification "
        "of abatement to the issuing OSHA area office. Then upload the documents to any customer "
        "prequal account (ISN/Avetta/Veriforce/PEC) that requires them, and schedule a self-audit "
        "to confirm the correction is holding.",
        ORIGIN_REC, basis="Origin follow-through checklist.", confidence=0.7,
        human_review=high_stakes)

    # 11) RECURRENCE PREVENTION
    outputs["recurrence_prevention"] = _item(
        "Recurrence prevention",
        "Fold the lesson into the routine so it can't come back: add the hazard to the job's JSA, "
        "cover it in a toolbox talk, add the item to your periodic self-inspection, and review at a "
        "management safety meeting. Prevention is what an inspector looks for on a repeat visit.",
        BEST_PRACTICE, basis="Industry consensus prevention loop.", confidence=0.6)

    # 12) ABATEMENT TRACKING — the Draft→Verified ladder + the OSHA deadline
    ladder = " → ".join(["Draft", "Human Approved", "Uploaded to ISN", "Verified Passed"])
    body12 = (f"Track this citation's abatement on Origin's status ladder: {ladder}. It starts at "
              "Draft and advances as the fix is approved, submitted, and confirmed.")
    if facts["abatement_date"]:
        body12 += (f"\n\nOSHA-required deadline: abatement must be completed and certified by "
                   f"{facts['abatement_date']}.")
    outputs["abatement_tracking"] = _item(
        "Abatement tracking", body12, ORIGIN_REC,
        basis="Origin abatement ladder; OSHA deadline noted where provided.",
        source_refs=ref, confidence=0.75, human_review=high_stakes)

    # optional local-model phrasing pass (off by default → stays offline)
    if explain_provider is not None:
        for key, it in outputs.items():
            it["body"] = _maybe_explain(it["body"], explain_provider)

    confidences = [it["confidence"] for it in outputs.values()]
    overall_conf = round(sum(confidences) / len(confidences), 2) if confidences else 0.0
    human_review = any(it["human_review_required"] for it in outputs.values())

    record = {
        "id": uuid.uuid4().hex[:12],
        "created_at": _now(),
        "input": facts,
        "standard_resolved": resolved,
        "classification_legend": {
            "OSHA-required": "Traceable to a current CFR record.",
            "Origin recommendation": "Origin's proprietary process / professional recommendation.",
            "Best practice": "Industry consensus, not legally mandated.",
            "Customer requirement": "An operator / ISN / Avetta requirement (stored per customer).",
        },
        "outputs": outputs,
        "confidence": overall_conf,
        "human_review_required": human_review,
        "disclaimer": _DISCLAIMER,
    }
    return record


def _maybe_explain(body: str, provider: Any) -> str:
    """Best-effort local-model rephrase of one output body. Never raises; returns
    the deterministic body unchanged on any failure so offline behavior is safe."""
    try:
        if provider is None or getattr(provider, "client", None) is None:
            return body
        msgs = [
            {"role": "system", "content": "Rewrite the compliance note in clear, plain "
             "language for a contractor. Do not add, remove, or change any facts, numbers, "
             "or citations. Return only the rewritten note."},
            {"role": "user", "content": body},
        ]
        out = provider.complete(msgs, []).text
        return out.strip() or body
    except Exception:
        return body


# ── file persistence ─────────────────────────────────────────────────────────
def save(record: Dict[str, Any]) -> str:
    CITATIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = CITATIONS_DIR / f"{record['id']}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record["id"]


def get(citation_id: str) -> Optional[Dict[str, Any]]:
    path = CITATIONS_DIR / f"{(citation_id or '').strip()}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_recent(limit: int = 50) -> List[Dict[str, Any]]:
    if not CITATIONS_DIR.exists():
        return []
    files = sorted(CITATIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: List[Dict[str, Any]] = []
    for p in files[:limit]:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": r.get("id"), "created_at": r.get("created_at"),
                        "standard": r.get("input", {}).get("standard", ""),
                        "resolved": r.get("standard_resolved"),
                        "human_review_required": r.get("human_review_required"),
                        "confidence": r.get("confidence")})
        except Exception:
            continue
    return out


# ── FastAPI routes ───────────────────────────────────────────────────────────
def register_citation(app) -> None:
    """Attach Citation Engine routes to an existing FastAPI app. Isolated + non-fatal,
    mirroring abatement.register_abatement. Never raises into the boot path."""
    from fastapi import Body, Request
    from fastapi.responses import JSONResponse

    @app.post("/api/citation/analyze")
    async def citation_analyze(request: Request, body: dict = Body(default=None)):
        payload = body if isinstance(body, dict) else {}
        if not payload:
            try:
                payload = await request.json()
            except Exception:
                payload = {}
        if not isinstance(payload, dict) or not (
                payload.get("standard") or payload.get("text") or payload.get("description")):
            return JSONResponse(
                {"error": "Provide at least a 'standard' (e.g. 29 CFR 1926.501) or 'text' "
                          "with the citation details."}, status_code=400)
        try:
            record = analyze(payload)          # deterministic, offline
            save(record)
            return record
        except Exception as exc:  # never 500 the tool
            return JSONResponse(
                {"error": f"Citation analysis failed: {exc}"}, status_code=200)

    @app.get("/api/citation/list")
    def citation_list():
        return {"ok": True, "items": list_recent()}

    @app.get("/api/citation/{citation_id}")
    def citation_get(citation_id: str):
        from fastapi.responses import JSONResponse as _J
        rec = get(citation_id)
        if not rec:
            return _J({"error": "not found"}, status_code=404)
        return rec

    @app.get("/api/knowledge/status")
    def knowledge_status():
        try:
            return {"ok": True, **ks.status()}
        except Exception as exc:
            from fastapi.responses import JSONResponse as _J
            return _J({"error": str(exc)}, status_code=200)
