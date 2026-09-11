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
    if base.get("kind") == "fmcsa" or "49 CFR" in cit:
        juris = "Federal — DOT/FMCSA"
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
    return {
        "standard": reg.get("standard") or standard,
        "requirement": (
            f"Correct the cited condition to bring the workplace into compliance "
            f"with {reg.get('standard') or standard}. Under {ref_std}, the employer "
            f"generally must (a) complete abatement by the abatement date, and "
            f"(b) submit abatement certification — with abatement documentation for "
            f"certain violations, and a tag for movable equipment. The attorney "
            f"confirms which certification/documentation obligations apply."
        ),
        "anchor_standard": ref.get("standard") or ref_std,
        "anchor_url": ref.get("url", ""),
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
