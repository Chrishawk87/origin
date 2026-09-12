"""Origin Abatement — Matter engine (Phase 1 core spine).

Origin Abatement is OSHA abatement intelligence + evidence management for
OSHA-defense attorneys. This module owns the top-level **Matter** object and the
first four links of the generalized engine:

    SOURCE  →  FACT/CONDITION  →  REQUIREMENT  →  ACTION  →  EVIDENCE  →  VERIFICATION  →  DECISION
    (reg)      (citation item)    (abatement req)  (capa)    (vault)      (human)         (package)

Scope of THIS module:
  * Matter          — the case container (client, citations, deadlines, links).
  * Citation Item   — one cited standard within a Matter (the FACT/CONDITION).
  * Regulatory map  — resolves each standard to a fully-sourced record (SOURCE),
                      via the existing deterministic Origin regulatory brain
                      (citations.cite → compliance_kb). The LLM is never the
                      source of truth here.
  * Deadline engine — derives workflow dates from the citation (DEADLINE).
  * Requirement     — the abatement obligation, anchored on 29 CFR 1903.19.
  * Readiness score — composed from the Evidence Vault's gap state.

House rules (identical to capa.py / citation_engine.py / company_profile.py):
  * Deterministic, offline, never-fabricate. No language model decides anything
    in this module. Extraction/vision live in abatement_intake.py and are always
    flagged for human verification.
  * NOT AN AI LAWYER. Nothing here is legal advice. Every deadline is a
    "System-calculated workflow date — verify against citation/order." Every
    readiness number is an "Internal workflow indicator. Not an OSHA
    determination." The attorney owns all legal strategy, conclusions, and
    submission.
  * File-based on the persistent volume (ORIGIN_DATA_DIR/abatement/matters), one
    JSON per matter. No database dependency, so a bug here can never break the
    live app.
  * Isolated + non-fatal route registration.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import citations

# Module-level Request so FastAPI can resolve `request: Request` route
# annotations under PEP 563 string annotations (mirrors company_profile.py).
try:
    from starlette.requests import Request
except Exception:  # pragma: no cover
    Request = Any  # type: ignore

try:
    from .paths import DATA_DIR
except ImportError:  # bare import in ad-hoc scripts
    import os as _os
    DATA_DIR = Path(_os.environ.get("ORIGIN_DATA_DIR") or (Path.home() / ".origin"))

ABATEMENT_DIR = DATA_DIR / "abatement"
MATTERS_DIR = ABATEMENT_DIR / "matters"

# ── mandatory guardrail strings (used verbatim across the whole feature) ───────
DISCLAIMER_NOT_LEGAL = (
    "Origin Abatement is a workflow and evidence tool, not a law firm and not a "
    "substitute for legal judgment. It does not provide legal advice. The "
    "attorney is responsible for all legal strategy, conclusions, and any "
    "submission to OSHA."
)
DEADLINE_DISCLAIMER = "System-calculated workflow date — verify against citation/order."
READINESS_DISCLAIMER = "Internal workflow indicator. Not an OSHA determination."
EXTRACTION_FLAG = "AI EXTRACTED — VERIFY"

# 29 CFR 1903.19 is the OSHA abatement-verification standard the whole feature
# anchors on (abatement certification, documentation, and — for movable
# equipment — tagging). We reference it, we never re-interpret it for the user.
ABATEMENT_STANDARD = "29 CFR 1903.19"

# ── matter workflow status (NOT a legal status) ────────────────────────────────
MATTER_STAGES: List[tuple] = [
    ("intake", "Intake"),
    ("active", "Active"),
    ("in_review", "Attorney Review"),
    ("submitted", "Submitted"),
    ("closed", "Closed"),
]
MATTER_STAGE_CODES: List[str] = [c for c, _ in MATTER_STAGES]
MATTER_STAGE_LABELS: Dict[str, str] = {c: l for c, l in MATTER_STAGES}

# OSHA citation classifications (kept aligned with capa.SEVERITY_WEIGHT keys).
CLASSIFICATIONS = [
    "willful", "repeat", "serious", "other_than_serious",
    "failure_to_abate", "de_minimis", "",
]


# ── helpers ────────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "matter"


def _ensure_dir() -> None:
    MATTERS_DIR.mkdir(parents=True, exist_ok=True)


def _parse_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _iso(d: Optional[date]) -> str:
    return d.isoformat() if d else ""


# ── SOURCE: regulatory mapping (deterministic, via the Origin regulatory brain) ─
def reg_lookup(standard: str) -> Dict[str, Any]:
    """Resolve a cited standard to a fully-sourced regulatory record.

    Wraps the existing deterministic resolver (citations.cite → compliance_kb)
    and shapes it into the uniform record the Abatement UI shows for every
    regulatory result: Source / Title / Standard / Jurisdiction / Effective date
    / URL / Section / Reasoning / Confidence.

    Never fabricates: if the corpus can't resolve the string, ok=False and no
    title/text is invented (confidence 0). The regulatory brain is the source of
    truth — no language model ever supplies these fields.
    """
    base = citations.cite(standard or "")
    ok = bool(base.get("ok"))
    part = (base.get("part") or "").strip()
    # Jurisdiction is inferred from the citation part deterministically, never guessed.
    juris = ""
    cit = (base.get("citation") or standard or "").strip()
    # Classify by CFR title FIRST (a 40 CFR body still resolves as kind='verbatim',
    # so the kind-based OSHA branch must come last or EPA/MSHA get mislabeled).
    if base.get("kind") == "fmcsa" or "49 CFR" in cit:
        juris = "Federal — DOT/FMCSA"
    elif base.get("kind") == "epa" or "40 CFR" in cit:
        juris = "Federal — EPA"
    elif "30 CFR" in cit:
        juris = "Federal — MSHA"
    elif "43 CFR" in cit:
        juris = "Federal — BLM/DOI"
    elif "29 CFR" in cit or base.get("kind") in ("verbatim", "training", "osha"):
        juris = "Federal — OSHA"
    elif cit:
        juris = "Federal"
    return {
        "ok": ok,
        "standard": cit,
        "section": cit,
        "title": base.get("title", ""),
        "url": base.get("url", ""),
        "source": base.get("url") or ("Origin Compliance KB" if ok else ""),
        "jurisdiction": juris,
        # The corpus stores verbatim text but not always a reliable per-section
        # effective date; we never invent one. Blank means "confirm on eCFR."
        "effective_date": "",
        "verbatim": base.get("verbatim", ""),
        "confidence": 1.0 if ok else 0.0,
        "reasoning": (
            "Matched from the Origin regulatory corpus by deterministic citation "
            "lookup (no language model). Verify the effective/current version on "
            "eCFR." if ok else
            "Not found in the Origin regulatory corpus. Attorney to supply/verify "
            "the standard text; nothing was fabricated."
        ),
    }


# ── REQUIREMENT: the abatement obligation, anchored on 29 CFR 1903.19 ───────────
def abatement_requirement(standard: str, reg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Deterministic abatement-requirement record for one cited standard.

    This states the *workflow* obligation (correct the condition, then certify /
    document abatement under 1903.19). It is not legal advice and does not decide
    whether any particular filing applies — the attorney confirms that."""
    reg = reg or reg_lookup(standard)
    ref_std = ABATEMENT_STANDARD
    ref = reg_lookup(ref_std)
    is_osha = "OSHA" in (reg.get("jurisdiction") or "")
    std_txt = reg.get("standard") or standard
    if is_osha:
        req = (
            f"Correct the cited condition to bring the workplace into compliance "
            f"with {std_txt}. Under {ref_std}, the employer generally must "
            f"(a) complete abatement by the abatement date, and (b) submit abatement "
            f"certification — with abatement documentation for certain violations, and "
            f"a tag for movable equipment. The attorney confirms which "
            f"certification/documentation obligations apply."
        )
        anchor_std, anchor_url = (ref.get("standard") or ref_std), ref.get("url", "")
    else:
        # Non-OSHA agencies (EPA/MSHA/DOT/etc) do not certify under 1903.19; the
        # abatement obligation is defined by the cited rule itself.
        req = (
            f"Correct the cited condition to bring the operation into compliance with "
            f"{std_txt}, and retain records/documentation demonstrating the correction "
            f"({reg.get('jurisdiction') or 'the citing agency'} governs the abatement "
            f"and any reporting deadlines). The attorney confirms the applicable "
            f"correction and documentation obligations for this standard."
        )
        anchor_std, anchor_url = std_txt, reg.get("url", "")
    return {
        "standard": std_txt,
        "requirement": req,
        "anchor_standard": anchor_std,
        "anchor_url": anchor_url,
        "note": DISCLAIMER_NOT_LEGAL,
    }


# ── DEADLINE engine ─────────────────────────────────────────────────────────────
def compute_deadlines(item: Dict[str, Any], *, citation_received: str = "") -> Dict[str, Any]:
    """Derive workflow dates for one citation item. Every value is labeled as a
    system-calculated workflow date to verify against the citation/order.

    Dates derived (all best-effort, all verify-required):
      * abatement_date         — copied straight from the citation item (source of truth).
      * certification_due      — abatement_date + 10 calendar days (29 CFR 1903.19(c)
                                 requires certification within 10 calendar days after
                                 the abatement date). Informational; attorney verifies.
      * contest_deadline       — citation_received + 15 working days (the Notice of
                                 Contest window). Informational; attorney verifies.
    Nothing is invented — a field is blank when its input date is missing."""
    ab = _parse_date(item.get("abatement_date", ""))
    recv = _parse_date(citation_received)
    today = _today()

    cert_due = (ab + timedelta(days=10)) if ab else None
    contest = _add_working_days(recv, 15) if recv else None

    def _days_left(d: Optional[date]) -> Optional[int]:
        return (d - today).days if d else None

    return {
        "disclaimer": DEADLINE_DISCLAIMER,
        "abatement_date": _iso(ab),
        "abatement_days_left": _days_left(ab),
        "abatement_overdue": bool(ab and ab < today),
        "certification_due": _iso(cert_due),
        "certification_basis": "Abatement date + 10 calendar days (29 CFR 1903.19(c)) — verify.",
        "contest_deadline": _iso(contest),
        "contest_basis": "Citation received + 15 working days (Notice of Contest) — verify.",
    }


def _add_working_days(start: Optional[date], n: int) -> Optional[date]:
    """Add n working days (Mon–Fri, no holiday calendar) to a date. Holidays are
    NOT accounted for — hence the verify-required labeling on every derived date."""
    if not start:
        return None
    d = start
    added = 0
    while added < n:
        d = d + timedelta(days=1)
        if d.weekday() < 5:  # Mon–Fri
            added += 1
    return d


# ── Citation Item (the FACT/CONDITION) ─────────────────────────────────────────
def build_citation_item(raw: Dict[str, Any], *, citation_received: str = "",
                        extracted: bool = False) -> Dict[str, Any]:
    """Assemble one fully-sourced Citation Item from raw fields.

    ``extracted=True`` marks every field as machine-extracted and stamps the
    verify flag — used by abatement_intake.py. Manually-typed items are not
    flagged, but are still unverified until an attorney marks them verified."""
    standard = (raw.get("standard") or "").strip()
    reg = reg_lookup(standard)
    req = abatement_requirement(standard, reg)
    item = {
        "item_id": "ci-" + uuid.uuid4().hex[:10],
        "citation_number": (raw.get("citation_number") or "").strip(),
        "standard": reg.get("standard") or standard,
        "classification": _norm_class(raw.get("classification", "")),
        "proposed_penalty": (raw.get("proposed_penalty") or "").strip(),
        "abatement_date": (raw.get("abatement_date") or "").strip(),
        # The alleged condition = the described violation text (the FACT/CONDITION).
        "alleged_condition": (raw.get("alleged_condition") or "").strip(),
        "extraction_source": "ai_ocr" if extracted else "manual",
        "extraction_flag": EXTRACTION_FLAG if extracted else "",
        "verified": False,
        "verified_by": "",
        "verified_at": "",
        "regulatory": reg,
        "requirement": req,
        "corrective_action_ids": [],
        "created_at": _now(),
    }
    item["deadlines"] = compute_deadlines(item, citation_received=citation_received)
    return item


def _norm_class(c: str) -> str:
    c = (c or "").strip().lower().replace(" ", "_").replace("-", "_")
    if c in CLASSIFICATIONS:
        return c
    for k in CLASSIFICATIONS:
        if k and k in c:
            return k
    return c or ""


def _norm_standard(s: str) -> str:
    """Canonical key for a cited standard so duplicates collapse regardless of how
    they were typed. '29 CFR 1910.1200', '1910.1200', '1910.1200(a)' all differ by
    surface form; this strips CFR/spaces/case so add-item can catch a repeat."""
    s = (s or "").strip().lower()
    for junk in ("29 cfr", "cfr", "§", " "):
        s = s.replace(junk, "")
    return s.strip("().-")


# ── Matter CRUD ────────────────────────────────────────────────────────────────
def create_matter(*, client_name: str, firm_slug: str = "", client_id: str = "",
                  osha_inspection_number: str = "", citation_issued_date: str = "",
                  citation_received_date: str = "", by: str = "attorney") -> Dict[str, Any]:
    """Open a new abatement Matter. The client is linked by company_profile id so
    the existing profile/risk engine can back it."""
    _ensure_dir()
    client_name = (client_name or "").strip()
    cid = _slug(client_id or client_name)
    record = {
        "id": "matter-" + uuid.uuid4().hex[:10],
        "created_at": _now(),
        "updated_at": _now(),
        "status": "intake",
        "firm_slug": (firm_slug or "").strip(),
        "client_id": cid,
        "client_name": client_name,
        "osha_inspection_number": (osha_inspection_number or "").strip(),
        "citation_issued_date": (citation_issued_date or "").strip(),
        "citation_received_date": (citation_received_date or "").strip(),
        "citation_items": [],
        "notes": [],
        "created_by": by,
        "disclaimer": DISCLAIMER_NOT_LEGAL,
    }
    save(record)
    return record


def add_citation_item(matter_id: str, raw: Dict[str, Any], *,
                      extracted: bool = False) -> Optional[Dict[str, Any]]:
    rec = get(matter_id)
    if not rec:
        return None
    item = build_citation_item(
        raw, citation_received=rec.get("citation_received_date", ""),
        extracted=extracted)
    rec.setdefault("citation_items", []).append(item)
    if rec.get("status") == "intake":
        rec["status"] = "active"
    rec["updated_at"] = _now()
    save(rec)
    return item


def add_citation_items(matter_id: str, rows: List[Dict[str, Any]], *,
                       extracted: bool = False) -> List[Dict[str, Any]]:
    out = []
    for r in rows or []:
        it = add_citation_item(matter_id, r, extracted=extracted)
        if it:
            out.append(it)
    return out


def verify_citation_item(matter_id: str, item_id: str, *, by: str = "attorney",
                         updates: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Attorney verifies (and optionally corrects) an extracted citation item.
    Clears the extraction flag and recomputes regulatory + deadlines from the
    verified values."""
    rec = get(matter_id)
    if not rec:
        return None
    for i, it in enumerate(rec.get("citation_items", [])):
        if it.get("item_id") == item_id:
            if updates:
                for k in ("standard", "classification", "proposed_penalty",
                          "abatement_date", "alleged_condition", "citation_number"):
                    if k in updates:
                        it[k] = (updates[k] or "").strip() if isinstance(updates[k], str) else updates[k]
                it["classification"] = _norm_class(it.get("classification", ""))
                it["regulatory"] = reg_lookup(it.get("standard", ""))
                it["requirement"] = abatement_requirement(it.get("standard", ""), it["regulatory"])
                it["deadlines"] = compute_deadlines(
                    it, citation_received=rec.get("citation_received_date", ""))
            it["verified"] = True
            it["verified_by"] = by
            it["verified_at"] = _now()
            it["extraction_flag"] = ""
            rec["citation_items"][i] = it
            rec["updated_at"] = _now()
            save(rec)
            return it
    return None


def link_corrective_action(matter_id: str, item_id: str, capa_id: str) -> Optional[Dict[str, Any]]:
    rec = get(matter_id)
    if not rec:
        return None
    for it in rec.get("citation_items", []):
        if it.get("item_id") == item_id:
            if capa_id not in it.setdefault("corrective_action_ids", []):
                it["corrective_action_ids"].append(capa_id)
            rec["updated_at"] = _now()
            save(rec)
            return it
    return None


# ── library documents (written programs / JSAs / training) as abatement proof ──
# For document-type citations (no written program, no LOTO, missing HazCom, no
# JSA), the proof of abatement IS a document — the written program, the job
# hazard analysis, or the training record — not a photo. These functions attach
# LIGHT references to Origin's own library onto a citation item. The full text is
# resolved and rendered at package-build time from the live library, so the
# document is never copied/frozen and never fabricated: if the library has no
# body for a ref, nothing is invented.
LIBRARY_DOC_KINDS = ("program", "jsa", "training")


def attach_library_doc(matter_id: str, item_id: str, *, kind: str, doc_id: str,
                       standard: str = "", title: str = "", classification: str = "",
                       by: str = "attorney") -> Optional[Dict[str, Any]]:
    """Attach a reference to a library document (written program, JSA, or training
    requirement) to a citation item as abatement documentation. Deduplicated by
    (kind, doc_id). Returns the updated item, or None if the matter/item is not
    found or the kind is invalid."""
    kind = (kind or "").strip().lower()
    if kind not in LIBRARY_DOC_KINDS:
        return None
    doc_id = (doc_id or "").strip()
    if not doc_id:
        return None
    rec = get(matter_id)
    if not rec:
        return None
    for it in rec.get("citation_items", []):
        if it.get("item_id") == item_id:
            docs = it.setdefault("library_docs", [])
            if any(d.get("kind") == kind and d.get("doc_id") == doc_id for d in docs):
                return it  # already attached — idempotent
            docs.append({
                "ref_id": "ld-" + uuid.uuid4().hex[:8],
                "kind": kind,
                "doc_id": doc_id,
                "standard": (standard or it.get("standard", "") or "").strip(),
                "title": (title or "").strip(),
                "classification": (classification or "").strip(),
                "added_by": by,
                "added_at": _now(),
            })
            rec["updated_at"] = _now()
            save(rec)
            return it
    return None


def get_library_doc(matter_id: str, item_id: str, ref_id: str) -> Optional[Dict[str, Any]]:
    """Return the attached-library-doc reference dict, or None."""
    ref_id = (ref_id or "").strip()
    rec = get(matter_id)
    if not rec:
        return None
    for it in rec.get("citation_items", []):
        if it.get("item_id") == item_id:
            for d in it.get("library_docs", []) or []:
                if d.get("ref_id") == ref_id:
                    return d
    return None


def rename_library_doc(matter_id: str, item_id: str, ref_id: str,
                       title: str) -> Optional[Dict[str, Any]]:
    """Change the display title of an attached library document. Returns the
    updated ref dict, or None if not found."""
    ref_id = (ref_id or "").strip()
    rec = get(matter_id)
    if not rec:
        return None
    for it in rec.get("citation_items", []):
        if it.get("item_id") == item_id:
            for d in it.get("library_docs", []) or []:
                if d.get("ref_id") == ref_id:
                    d["title"] = (title or "").strip()
                    rec["updated_at"] = _now()
                    save(rec)
                    return d
    return None


def set_library_doc_body(matter_id: str, item_id: str, ref_id: str,
                         body: str) -> Optional[Dict[str, Any]]:
    """Store an attorney-edited body override on an attached library document, so
    the edited copy (not the blank master) is what renders in the package. Pass an
    empty string to clear the override and revert to the library master."""
    ref_id = (ref_id or "").strip()
    rec = get(matter_id)
    if not rec:
        return None
    for it in rec.get("citation_items", []):
        if it.get("item_id") == item_id:
            for d in it.get("library_docs", []) or []:
                if d.get("ref_id") == ref_id:
                    b = body if isinstance(body, str) else ""
                    if b.strip():
                        d["body_override"] = b
                        d["edited_at"] = _now()
                    else:
                        d.pop("body_override", None)
                        d.pop("edited_at", None)
                    rec["updated_at"] = _now()
                    save(rec)
                    return d
    return None


def remove_library_doc(matter_id: str, item_id: str, ref_id: str) -> Optional[Dict[str, Any]]:
    """Detach a previously-attached library document from a citation item."""
    ref_id = (ref_id or "").strip()
    rec = get(matter_id)
    if not rec:
        return None
    for it in rec.get("citation_items", []):
        if it.get("item_id") == item_id:
            docs = it.get("library_docs", []) or []
            new_docs = [d for d in docs if d.get("ref_id") != ref_id]
            if len(new_docs) == len(docs):
                return it  # nothing removed
            it["library_docs"] = new_docs
            rec["updated_at"] = _now()
            save(rec)
            return it
    return None


# ── document fill values (auto-fill the written programs / JSAs / training) ─────
# The library documents ship with {{TOKEN}} placeholders (company name, address,
# effective date, program administrator, etc.). These are the client-specific
# values the attorney enters ONCE per matter; they are substituted into every
# attached document at package-build and at view/edit time. Stored on the matter
# so they persist and never bleed across matters.
FILL_FIELDS = (
    "company_name", "company_address", "effective_date",
    "program_administrator", "admin_title", "admin_phone",
    "admin_email", "scope",
)


def default_fill(rec: Dict[str, Any]) -> Dict[str, str]:
    """Best-effort starting values for a matter's document fill, so nothing
    renders as a raw {{TOKEN}}. Only the company name is known for certain (the
    client on the matter); everything else starts blank for the attorney."""
    f = dict.fromkeys(FILL_FIELDS, "")
    f.update({k: str(v) for k, v in (rec.get("fill") or {}).items() if k in FILL_FIELDS})
    if not f.get("company_name"):
        f["company_name"] = rec.get("client_name", "") or ""
    if not f.get("effective_date"):
        f["effective_date"] = datetime.now(timezone.utc).strftime("%B %d, %Y")
    return f


def get_fill(matter_id: str) -> Dict[str, str]:
    rec = get(matter_id)
    return default_fill(rec) if rec else dict.fromkeys(FILL_FIELDS, "")


def set_fill(matter_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Save the attorney-entered document fill values on the matter. Only known
    fields are stored; blanks are allowed (they clear a value)."""
    rec = get(matter_id)
    if not rec:
        return None
    cur = dict(rec.get("fill") or {})
    for k in FILL_FIELDS:
        if k in (fields or {}):
            v = fields[k]
            cur[k] = (v or "").strip() if isinstance(v, str) else v
    rec["fill"] = cur
    rec["updated_at"] = _now()
    save(rec)
    return default_fill(rec)


def set_status(matter_id: str, status: str, *, by: str = "") -> Optional[Dict[str, Any]]:
    rec = get(matter_id)
    if not rec:
        return None
    code = (status or "").strip().lower()
    if code not in MATTER_STAGE_CODES:
        return None
    rec["status"] = code
    rec["updated_at"] = _now()
    rec.setdefault("status_history", []).append({"status": code, "at": _now(), "by": by})
    save(rec)
    return rec


def add_note(matter_id: str, text: str, *, by: str = "") -> Optional[Dict[str, Any]]:
    rec = get(matter_id)
    if not rec:
        return None
    rec.setdefault("notes", []).append(
        {"at": _now(), "by": by or "", "text": (text or "").strip()})
    rec["updated_at"] = _now()
    save(rec)
    return rec


# ── corrective action bridge (reuses capa.py — the ACTION link) ────────────────
def open_corrective_action(matter_id: str, item_id: str, *,
                           corrective_action: str = "", by: str = "attorney") -> Optional[Dict[str, Any]]:
    """Open a CAPA for a citation item, reusing the existing corrective-action
    engine, and link it back to the item. Deadlines/classification/penalty carry
    over from the verified citation item so the CAPA speaks the same language."""
    rec = get(matter_id)
    if not rec:
        return None
    item = next((it for it in rec.get("citation_items", []) if it.get("item_id") == item_id), None)
    if not item:
        return None
    from . import capa as _capa
    title = f"Abate {item.get('standard') or 'cited condition'}"
    capa_rec = _capa.create_manual(
        company=rec.get("client_name", ""),
        title=title,
        hazard=item.get("alleged_condition", ""),
        corrective_action=corrective_action or (item.get("requirement", {}) or {}).get("requirement", ""),
        classification=item.get("classification", ""),
        penalty=item.get("proposed_penalty", ""),
        abatement_date=item.get("abatement_date", ""),
        source="abatement", by=by)
    link_corrective_action(matter_id, item_id, capa_rec["id"])
    return capa_rec


def corrective_action_suggestions(matter_id: str, item_id: str) -> Dict[str, Any]:
    """Grounded, agency-aware corrective actions OSHA/EPA/DOT/MSHA require to abate
    one cited standard. Deterministic — every suggestion is derived from the
    regulatory corpus (abatement_requirement + the matched written program /
    training mandate / JSA) and the certification obligation. Never invents a
    corrective action: when the corpus can't resolve the standard, only the
    generic correct-and-document step (grounded on the abatement rule) is offered.

    Returns {ok, standard, jurisdiction, suggestions:[{title,text,source,kind}]}.
    The UI renders these as one-click "Add" buttons alongside a free-text box."""
    rec = get(matter_id)
    if not rec:
        return {"ok": False, "error": "not found", "suggestions": []}
    item = next((it for it in rec.get("citation_items", [])
                 if it.get("item_id") == item_id), None)
    if not item:
        return {"ok": False, "error": "item not found", "suggestions": []}

    standard = (item.get("standard") or "").strip()
    reg = item.get("regulatory") or reg_lookup(standard)
    juris = reg.get("jurisdiction") or ""
    agency = juris.split("—")[-1].strip() if "—" in juris else (juris or "the citing agency")
    req = item.get("requirement") or abatement_requirement(standard, reg)

    out: List[Dict[str, Any]] = []
    seen = set()

    def _add(title: str, text: str, source: str, kind: str):
        key = (text or "").strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        out.append({"title": title, "text": text.strip(), "source": source, "kind": kind})

    # 1. The primary abatement obligation — correct the cited condition. Grounded
    #    on the requirement engine (the cited rule + the abatement rule chain).
    if req.get("requirement"):
        _add(f"Correct the cited condition — {standard or 'cited standard'}",
             req["requirement"], reg.get("standard") or standard or "abatement requirement",
             "correct")

    # 2. Library documents matched to this standard become concrete corrective
    #    actions (implement the written program / deliver training / adopt the JSA).
    #    Deterministic via the submissions library matcher; excludes nothing here so
    #    the attorney sees the full required document set for the standard.
    try:
        from . import abatement_submissions as _subs
        lib = _subs.library_suggestions(matter_id, item_id)
        for s in (lib.get("suggestions") or []):
            k = s.get("kind")
            t = s.get("title") or s.get("standard") or ""
            cit = s.get("standard") or standard
            if k == "program":
                _add(f"Implement written program — {t}",
                     f"Adopt and implement the written {t} program and train affected "
                     f"employees on it, as required to correct and maintain compliance "
                     f"with {cit}.", cit, "program")
            elif k == "training":
                _add(f"Deliver required training — {cit}",
                     f"Deliver the training required by {cit} to all affected employees "
                     f"and retain signed training/attendance records as abatement proof.",
                     cit, "training")
            elif k == "jsa":
                _add(f"Adopt job hazard analysis — {t}",
                     f"Complete and adopt the job hazard analysis for {t} covering the "
                     f"cited task, and brief affected crews before the work resumes.",
                     cit, "jsa")
    except Exception:
        pass

    # 3. The certification / documentation step — how the correction is proven to
    #    the agency. OSHA certifies under 1903.19; other agencies retain records.
    is_osha = "OSHA" in juris
    if is_osha:
        _add("Certify abatement to OSHA (29 CFR 1903.19)",
             "Complete the correction by the abatement date, then submit abatement "
             "certification to the Area Director — with abatement documentation for "
             "serious/willful/repeat items and an abatement tag on any moved equipment "
             "(29 CFR 1903.19). Attorney confirms which certification/documentation "
             "obligations apply.", "29 CFR 1903.19", "certify")
    else:
        _add(f"Document the correction for {agency}",
             f"Complete the correction and retain dated records/photos demonstrating it. "
             f"{agency} governs the abatement and any reporting deadlines for this "
             f"standard — attorney confirms the applicable filing/reporting obligation.",
             reg.get("standard") or standard or "citing agency", "certify")

    # 4. Evidence step — dated proof the correction happened, feeds the Vault.
    _add("Capture dated evidence of the correction",
         "Photograph the corrected condition with a date/time stamp (and GPS where "
         "available) and file it to the matter's Evidence Vault so the corrective "
         "action is verifiable in the submission package.",
         "Origin Abatement workflow", "evidence")

    return {"ok": True, "matter_id": matter_id, "item_id": item_id,
            "standard": standard, "jurisdiction": juris, "suggestions": out}


def list_item_corrective_actions(matter_id: str, item_id: str) -> Dict[str, Any]:
    """Resolve a citation item's linked CAPAs into compact views for the card.
    Read-only; a missing/deleted CAPA id is skipped (never fabricated)."""
    rec = get(matter_id)
    if not rec:
        return {"ok": False, "error": "not found", "actions": []}
    item = next((it for it in rec.get("citation_items", [])
                 if it.get("item_id") == item_id), None)
    if not item:
        return {"ok": False, "error": "item not found", "actions": []}
    from . import capa as _capa
    actions: List[Dict[str, Any]] = []
    for cid in item.get("corrective_action_ids", []) or []:
        try:
            capa_rec = _capa.get(cid)
        except Exception:
            capa_rec = None
        if not capa_rec:
            continue
        actions.append({
            "id": capa_rec.get("id", ""),
            "title": capa_rec.get("title", ""),
            "corrective_action": capa_rec.get("corrective_action", ""),
            "stage": capa_rec.get("stage", ""),
            "stage_label": _capa.STAGE_LABELS.get(capa_rec.get("stage", ""), ""),
            "abatement_date": capa_rec.get("abatement_date", ""),
            "created_at": capa_rec.get("created_at", ""),
        })
    return {"ok": True, "matter_id": matter_id, "item_id": item_id, "actions": actions}


# ── READINESS (composed from the Evidence Vault gap state) ──────────────────────
def readiness(matter_id: str) -> Dict[str, Any]:
    """Abatement Readiness Score for a matter. Delegates the per-item evidence gap
    assessment to the Evidence Vault and rolls it into a single 0–100 indicator.
    Explicitly labeled: internal workflow indicator, NOT an OSHA determination."""
    rec = get(matter_id)
    if not rec:
        return {"ok": False, "error": "not found"}
    try:
        from . import evidence_vault as _ev
        gap = _ev.matter_gap_analysis(matter_id, rec)
    except Exception as exc:  # pragma: no cover
        gap = {"items": [], "error": str(exc)}
    items = gap.get("items", [])
    # Deterministic weighting of each item's evidence state.
    weight = {"verified": 1.0, "unverified": 0.6, "incomplete": 0.3,
              "conflicting": 0.2, "missing": 0.0}
    if items:
        score = round(100.0 * sum(weight.get(i.get("state"), 0.0) for i in items) / len(items), 1)
    else:
        score = 0.0
    n_missing = sum(1 for i in items if i.get("state") == "missing")
    n_verified = sum(1 for i in items if i.get("state") == "verified")
    drivers: List[str] = []
    if not items:
        drivers.append("No citation items on file yet.")
    else:
        drivers.append(f"{len(items)} citation item(s); {n_verified} with verified evidence, "
                       f"{n_missing} with no evidence.")
        unver = [i for i in items if i.get("state") in ("unverified", "conflicting")]
        if unver:
            drivers.append(f"{len(unver)} item(s) have evidence awaiting human verification.")
    band = ("Ready for review" if score >= 85 else
            "In progress" if score >= 40 else "Early")
    return {
        "ok": True,
        "matter_id": matter_id,
        "score": score,
        "band": band,
        "disclaimer": READINESS_DISCLAIMER,
        "drivers": drivers,
        "gap": gap,
        "computed_at": _now(),
    }


# ── persistence ─────────────────────────────────────────────────────────────────
def save(record: Dict[str, Any]) -> str:
    _ensure_dir()
    path = MATTERS_DIR / f"{record['id']}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record["id"]


def get(matter_id: str) -> Optional[Dict[str, Any]]:
    path = MATTERS_DIR / f"{(matter_id or '').strip()}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_all() -> List[Dict[str, Any]]:
    if not MATTERS_DIR.exists():
        return []
    out: List[Dict[str, Any]] = []
    for p in MATTERS_DIR.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _view(rec: Dict[str, Any]) -> Dict[str, Any]:
    items = rec.get("citation_items", [])
    unverified = sum(1 for it in items if not it.get("verified"))
    overdue = sum(1 for it in items if (it.get("deadlines", {}) or {}).get("abatement_overdue"))
    return {
        "id": rec.get("id"),
        "client_name": rec.get("client_name", ""),
        "client_id": rec.get("client_id", ""),
        "firm_slug": rec.get("firm_slug", ""),
        "status": rec.get("status"),
        "status_label": MATTER_STAGE_LABELS.get(rec.get("status", ""), ""),
        "osha_inspection_number": rec.get("osha_inspection_number", ""),
        "citation_items": len(items),
        "unverified_items": unverified,
        "overdue_items": overdue,
        "created_at": rec.get("created_at"),
        "updated_at": rec.get("updated_at"),
    }


def list_matters(firm_slug: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = _load_all()
    if firm_slug is not None:
        rows = [r for r in rows if (r.get("firm_slug") or "") == firm_slug]
    rows.sort(key=lambda r: r.get("updated_at", ""), reverse=True)
    return [_view(r) for r in rows]


def dashboard(firm_slug: Optional[str] = None) -> Dict[str, Any]:
    """Attorney command-center rollup: counts by status, unverified extractions,
    overdue abatement dates, upcoming deadlines."""
    rows = _load_all()
    if firm_slug is not None:
        rows = [r for r in rows if (r.get("firm_slug") or "") == firm_slug]
    counts = {c: 0 for c in MATTER_STAGE_CODES}
    total_items = unverified = overdue = 0
    upcoming: List[Dict[str, Any]] = []
    today = _today()
    for r in rows:
        counts[r.get("status", "intake")] = counts.get(r.get("status", "intake"), 0) + 1
        for it in r.get("citation_items", []):
            total_items += 1
            if not it.get("verified"):
                unverified += 1
            dl = it.get("deadlines", {}) or {}
            if dl.get("abatement_overdue"):
                overdue += 1
            ab = _parse_date(it.get("abatement_date", ""))
            if ab and 0 <= (ab - today).days <= 30:
                upcoming.append({
                    "matter_id": r.get("id"), "client_name": r.get("client_name", ""),
                    "standard": it.get("standard", ""), "abatement_date": it.get("abatement_date", ""),
                    "days_left": (ab - today).days})
    upcoming.sort(key=lambda x: x["days_left"])
    return {
        "matters": len(rows),
        "status_counts": counts,
        "status_labels": MATTER_STAGE_LABELS,
        "citation_items": total_items,
        "unverified_items": unverified,
        "overdue_items": overdue,
        "upcoming_deadlines": upcoming[:25],
        "disclaimer": DISCLAIMER_NOT_LEGAL,
    }


# ── routes ──────────────────────────────────────────────────────────────────────
def register_abatement_matter(app) -> None:
    """Attach Abatement Matter routes. Isolated + non-fatal, mirroring the other
    SIE modules."""
    from fastapi import Body
    from fastapi.responses import JSONResponse

    def _firm(request):
        st = getattr(request, "state", None)
        # Reuse the SIE tenant scope: owner sees all (None), a scoped session sees
        # only its firm. Defaults to owner/global for local single-user runs.
        gc = getattr(st, "sie_gc_slug", None)
        return gc

    @app.get("/api/abatement/dashboard")
    def ab_dashboard(request: Request):
        try:
            return {"ok": True, **dashboard(firm_slug=_firm(request))}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/abatement/matters")
    def ab_list(request: Request):
        try:
            return {"ok": True, "items": list_matters(firm_slug=_firm(request))}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.post("/api/abatement/matters")
    def ab_create(request: Request, body: dict = Body(default=None)):
        p = body if isinstance(body, dict) else {}
        if not (p.get("client_name") or p.get("client_id")):
            return JSONResponse({"error": "client_name is required"}, status_code=400)
        try:
            rec = create_matter(
                client_name=p.get("client_name", ""), firm_slug=_firm(request) or p.get("firm_slug", ""),
                client_id=p.get("client_id", ""),
                osha_inspection_number=p.get("osha_inspection_number", ""),
                citation_issued_date=p.get("citation_issued_date", ""),
                citation_received_date=p.get("citation_received_date", ""),
                by=p.get("by", "attorney"))
            return {"ok": True, "matter": rec}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/abatement/matters/{matter_id}")
    def ab_get(matter_id: str):
        rec = get(matter_id)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return rec

    @app.post("/api/abatement/matters/{matter_id}/citation-item")
    def ab_add_item(matter_id: str, body: dict = Body(default=None)):
        p = body if isinstance(body, dict) else {}
        if not p.get("standard"):
            return JSONResponse({"error": "standard is required"}, status_code=400)
        rec = get(matter_id)
        if not rec:
            return JSONResponse({"error": "matter not found"}, status_code=404)
        # Duplicate guard: the same cited standard should be ONE citation item, not
        # two identical items (which would each render their own Suggest button).
        want = _norm_standard(p.get("standard"))
        if want:
            for ex in rec.get("citation_items", []):
                if _norm_standard(ex.get("standard")) == want:
                    return {"ok": True, "item": ex, "duplicate": True,
                            "message": f"{ex.get('standard')} is already a citation item on this matter."}
        it = add_citation_item(matter_id, p, extracted=bool(p.get("extracted")))
        if not it:
            return JSONResponse({"error": "matter not found"}, status_code=404)
        return {"ok": True, "item": it}

    @app.post("/api/abatement/matters/{matter_id}/citation-item/{item_id}/verify")
    def ab_verify_item(matter_id: str, item_id: str, body: dict = Body(default=None)):
        p = body if isinstance(body, dict) else {}
        it = verify_citation_item(matter_id, item_id, by=p.get("by", "attorney"),
                                  updates=p.get("updates"))
        if not it:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"ok": True, "item": it}

    @app.post("/api/abatement/matters/{matter_id}/citation-item/{item_id}/corrective-action")
    def ab_open_ca(matter_id: str, item_id: str, body: dict = Body(default=None)):
        p = body if isinstance(body, dict) else {}
        capa_rec = open_corrective_action(
            matter_id, item_id, corrective_action=p.get("corrective_action", ""),
            by=p.get("by", "attorney"))
        if not capa_rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"ok": True, "capa": capa_rec}

    @app.get("/api/abatement/matters/{matter_id}/citation-item/{item_id}/corrective-action/suggestions")
    def ab_ca_suggestions(matter_id: str, item_id: str):
        try:
            r = corrective_action_suggestions(matter_id, item_id)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc), "suggestions": []},
                                status_code=200)
        if not r.get("ok"):
            return JSONResponse(r, status_code=404 if r.get("error") == "not found" else 200)
        return r

    @app.get("/api/abatement/matters/{matter_id}/citation-item/{item_id}/corrective-actions")
    def ab_ca_list(matter_id: str, item_id: str):
        try:
            r = list_item_corrective_actions(matter_id, item_id)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc), "actions": []},
                                status_code=200)
        if not r.get("ok"):
            return JSONResponse(r, status_code=404 if r.get("error") == "not found" else 200)
        return r

    @app.post("/api/abatement/matters/{matter_id}/status")
    def ab_status(matter_id: str, body: dict = Body(default=None)):
        p = body if isinstance(body, dict) else {}
        rec = set_status(matter_id, p.get("status", ""), by=p.get("by", ""))
        if not rec:
            return JSONResponse({"error": "not found or invalid status",
                                 "valid": MATTER_STAGE_CODES}, status_code=400)
        return {"ok": True, "matter": rec}

    @app.post("/api/abatement/matters/{matter_id}/note")
    def ab_note(matter_id: str, body: dict = Body(default=None)):
        p = body if isinstance(body, dict) else {}
        rec = add_note(matter_id, p.get("text", ""), by=p.get("by", ""))
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"ok": True, "matter": rec}

    @app.get("/api/abatement/matters/{matter_id}/fill")
    def ab_get_fill(matter_id: str):
        if not get(matter_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"ok": True, "fill": get_fill(matter_id), "fields": list(FILL_FIELDS)}

    @app.post("/api/abatement/matters/{matter_id}/fill")
    def ab_set_fill(matter_id: str, body: dict = Body(default=None)):
        p = body if isinstance(body, dict) else {}
        f = set_fill(matter_id, p.get("fill") if isinstance(p.get("fill"), dict) else p)
        if f is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"ok": True, "fill": f}

    @app.get("/api/abatement/matters/{matter_id}/readiness")
    def ab_readiness(matter_id: str):
        r = readiness(matter_id)
        if not r.get("ok"):
            return JSONResponse(r, status_code=404 if r.get("error") == "not found" else 200)
        return r

    @app.get("/api/abatement/reg-lookup")
    def ab_reg(standard: str = ""):
        if not standard:
            return JSONResponse({"error": "standard is required"}, status_code=400)
        return {"ok": True, "regulatory": reg_lookup(standard)}
