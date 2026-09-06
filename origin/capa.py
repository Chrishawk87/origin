"""CAPA Engine — the Safety Intelligence Engine's Corrective / Preventive Action
tracker (Phase 2).

A CAPA is the record that turns a *finding* (an OSHA citation, a gap, an audit
observation) into *closed-loop remediation*: what the hazard was, what the fix
is, who owns it, when it's due, and the proof it was actually corrected and
verified effective. This module is the connective tissue between the Citation
Engine (Phase 1) and a company's real compliance posture.

Design (identical philosophy to citation_engine.py / abatement.py):
  * Deterministic. `open_from_citation()` derives every field by rule from the
    citation-engine record — the immediate + recommended corrective actions, the
    evidence checklist, the OSHA abatement deadline, the classification, and the
    traceable source refs. No language model decides anything here.
  * Source-preserving. A CAPA carries the citation's `source_refs` forward, so the
    chain "citation → resolved standard → knowledge version" is never broken. A
    CAPA can always answer *why* it exists.
  * File-based on the persistent volume (ORIGIN_DATA_DIR/capa), one JSON per CAPA.
    No database dependency, so this can never break the live chat app or portal.
  * Never destructive to the ISN Upload Tracker (abatement.py). That module tracks
    *document uploads* on a client's portal record; a CAPA tracks *remediation of a
    specific finding*. They are different objects and live in different stores.

The stage ladder deliberately mirrors abatement.py's philosophy (a small, ordered,
named set of stages) so the whole platform speaks one status language:

    Open  →  Root Cause Identified  →  Corrective Action Implemented  →  Verified Effective
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from .paths import DATA_DIR
except ImportError:  # bare import in ad-hoc scripts
    import os as _os
    DATA_DIR = Path(_os.environ.get("ORIGIN_DATA_DIR") or (Path.home() / ".origin"))

CAPA_DIR = DATA_DIR / "capa"

# ── stage ladder (ordered, named — mirrors abatement.py's status language) ────
STAGES: List[Tuple[str, str]] = [
    ("open", "Open"),
    ("root_cause", "Root Cause Identified"),
    ("action_implemented", "Corrective Action Implemented"),
    ("verified", "Verified Effective"),
]
STAGE_CODES: List[str] = [c for c, _ in STAGES]
STAGE_LABELS: Dict[str, str] = {c: lbl for c, lbl in STAGES}


def normalize_stage(s: str) -> Optional[str]:
    s = (s or "").strip().lower().replace(" ", "_")
    if s in STAGE_LABELS:
        return s
    for code, label in STAGES:
        if s == label.lower().replace(" ", "_"):
            return code
    return None


def stage_index(code: str) -> int:
    try:
        return STAGE_CODES.index(code)
    except ValueError:
        return -1


# ── severity weighting (deterministic — drives risk in company_profile.py) ────
# OSHA classification → a fixed hazard weight in [0,1]. Higher = more serious.
SEVERITY_WEIGHT: Dict[str, float] = {
    "willful": 1.0,
    "failure_to_abate": 0.95,
    "repeat": 0.9,
    "serious": 0.6,
    "other": 0.3,
    "other_than_serious": 0.3,
    "": 0.4,            # unclassified — treat as moderate, never zero
}


def severity_weight(classification: str) -> float:
    key = (classification or "").strip().lower().replace(" ", "_").replace("-", "_")
    if key in SEVERITY_WEIGHT:
        return SEVERITY_WEIGHT[key]
    # substring fallbacks so "serious (repeat)" etc. still score
    for k, w in SEVERITY_WEIGHT.items():
        if k and k in key:
            return w
    return SEVERITY_WEIGHT[""]


_MONEY_RE = re.compile(r"[\d,]+(?:\.\d{2})?")


def penalty_amount(penalty: Any) -> float:
    """Best-effort dollar figure out of a penalty string ('$4,500' → 4500.0)."""
    if isinstance(penalty, (int, float)):
        return float(penalty)
    m = _MONEY_RE.search(str(penalty or ""))
    if not m:
        return 0.0
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return 0.0


# ── helpers ───────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "company"


def _ensure_dir() -> None:
    CAPA_DIR.mkdir(parents=True, exist_ok=True)


def _parse_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def is_overdue(rec: Dict[str, Any]) -> bool:
    """A CAPA is overdue if its OSHA abatement deadline has passed and it is not
    yet Verified Effective."""
    if rec.get("stage") == "verified":
        return False
    d = _parse_date(rec.get("abatement_date", ""))
    return bool(d and d < datetime.now(timezone.utc).date())


# ── creation ──────────────────────────────────────────────────────────────────
def open_from_citation(citation_record: Dict[str, Any], *,
                       company_id: str = "", by: str = "system") -> Dict[str, Any]:
    """Deterministically derive a CAPA from a Citation Engine analysis record.

    Pulls the finding, the immediate + recommended corrective actions, the
    evidence checklist, the OSHA deadline, the classification/penalty, and the
    traceable source refs straight off the citation record. Nothing is invented.
    """
    facts = citation_record.get("input", {}) or {}
    outputs = citation_record.get("outputs", {}) or {}
    company = (facts.get("company") or "").strip()
    cid = _slug(company_id or company)

    def _body(key: str) -> str:
        it = outputs.get(key) or {}
        return (it.get("body") or "").strip()

    # Evidence checklist: keep the human-readable list from the citation output.
    evidence = _body("evidence_checklist")

    classification = (facts.get("classification") or "").strip().lower()
    std_source = (outputs.get("source", {}) or {}).get("source_refs", []) or []

    record = {
        "id": "capa-" + uuid.uuid4().hex[:10],
        "company_id": cid,
        "company": company,
        "created_at": _now(),
        "updated_at": _now(),
        "source": "citation",
        "citation_id": citation_record.get("id", ""),
        "standard": facts.get("standard", ""),
        "classification": classification,
        "severity_weight": severity_weight(classification),
        "penalty": facts.get("penalty", ""),
        "penalty_amount": penalty_amount(facts.get("penalty", "")),
        "abatement_date": facts.get("abatement_date", ""),
        "title": (outputs.get("summary", {}) or {}).get("body", "").strip()
                 or f"Citation under {facts.get('standard','(standard)')}",
        "hazard": _body("hazard_explanation"),
        "immediate_action": _body("immediate_corrective_action"),
        "corrective_action": _body("recommended_corrective_action"),
        "root_cause_prompt": _body("root_cause"),
        "evidence_required": evidence,
        "source_refs": std_source,
        "resolved": bool(citation_record.get("standard_resolved")),
        "stage": "open",
        "stage_history": [{"stage": "open", "at": _now(), "by": by}],
        "human_review_required": bool(citation_record.get("human_review_required")),
        "notes": [],
    }
    save(record)
    return record


# ── audit-finding severity (visual inspector's low/medium/high scale) ─────────
# A photo walk-through finding is NOT an OSHA-issued citation, so it has no
# OSHA classification (willful/serious/…). It carries the inspector's own
# low/medium/high severity instead. Map that to a fixed hazard weight so audit
# CAPAs feed the exact same deterministic risk math as citation CAPAs. Kept
# below "serious" (0.6) so a self-identified visible hazard never out-weighs a
# real OSHA serious citation of the same severity band.
AUDIT_SEVERITY_WEIGHT: Dict[str, float] = {
    "high": 0.6,
    "medium": 0.4,
    "low": 0.25,
}


def audit_severity_weight(severity: str) -> float:
    return AUDIT_SEVERITY_WEIGHT.get(
        (severity or "").strip().lower(), AUDIT_SEVERITY_WEIGHT["medium"])


def open_from_audit_finding(finding: Dict[str, Any], *, company: str,
                            company_id: str = "", audit_id: str = "",
                            by: str = "system") -> Dict[str, Any]:
    """Deterministically derive a CAPA from ONE photo walk-through finding.

    The visual inspector describes the hazard in plain language; Origin has
    already (in photo_audit) resolved it to an OSHA standard where one exists.
    This carries that standard's source refs forward so the CAPA can always
    answer *why* it exists. A finding Origin could NOT map to a KB standard is
    tracked honestly and flagged for human review — never force-fit to a guess.
    """
    company = (company or "").strip()
    cid = _slug(company_id or company)
    std = finding.get("standard") or {}
    severity = (finding.get("severity") or "medium").strip().lower()
    matched = bool(std)

    source_refs: List[Dict[str, str]] = []
    if matched:
        source_refs = [{
            "citation": std.get("citation", ""),
            "title": std.get("standard_title", ""),
            "url": std.get("url", ""),
            "version": "OSHA-2254" if std.get("has_verbatim") else "",
            "jurisdiction": "Federal",
        }]

    record = {
        "id": "capa-" + uuid.uuid4().hex[:10],
        "company_id": cid,
        "company": company,
        "created_at": _now(),
        "updated_at": _now(),
        "source": "photo_audit",
        "audit_id": audit_id,
        "citation_id": "",
        "standard": std.get("citation", ""),
        "classification": "",                       # not an OSHA-issued class
        "finding_severity": severity,
        "severity_weight": audit_severity_weight(severity),
        "penalty": "",
        "penalty_amount": 0.0,
        "abatement_date": "",
        "title": (finding.get("title") or "").strip()
                 or "Visible hazard from photo walk-through",
        "hazard": (finding.get("description") or "").strip(),
        "immediate_action": "",
        "corrective_action": (finding.get("recommended_action") or "").strip(),
        "root_cause_prompt": "",
        "evidence_required": "",
        "source_refs": source_refs,
        "resolved": matched,
        "stage": "open",
        "stage_history": [{"stage": "open", "at": _now(), "by": by}],
        # A hazard Origin couldn't source to a standard needs a human to classify
        # it — same never-fabricate posture as the rest of the SIE.
        "human_review_required": not matched,
        "notes": [],
    }
    save(record)
    return record


def create_manual(*, company: str, title: str, hazard: str = "",
                  corrective_action: str = "", classification: str = "",
                  penalty: str = "", abatement_date: str = "",
                  source: str = "manual", by: str = "owner") -> Dict[str, Any]:
    """Open a CAPA by hand (a finding that didn't come from a parsed citation —
    e.g. an internal audit observation)."""
    company = (company or "").strip()
    record = {
        "id": "capa-" + uuid.uuid4().hex[:10],
        "company_id": _slug(company),
        "company": company,
        "created_at": _now(),
        "updated_at": _now(),
        "source": source,
        "citation_id": "",
        "standard": "",
        "classification": (classification or "").strip().lower(),
        "severity_weight": severity_weight(classification),
        "penalty": penalty,
        "penalty_amount": penalty_amount(penalty),
        "abatement_date": abatement_date,
        "title": title.strip() or "Corrective action",
        "hazard": hazard.strip(),
        "immediate_action": "",
        "corrective_action": corrective_action.strip(),
        "root_cause_prompt": "",
        "evidence_required": "",
        "source_refs": [],
        "resolved": False,
        "stage": "open",
        "stage_history": [{"stage": "open", "at": _now(), "by": by}],
        "human_review_required": False,
        "notes": [],
    }
    save(record)
    return record


# ── mutation ──────────────────────────────────────────────────────────────────
def set_stage(capa_id: str, stage: str, *, by: str = "", note: str = "") -> Optional[Dict[str, Any]]:
    rec = get(capa_id)
    if not rec:
        return None
    code = normalize_stage(stage)
    if not code:
        return None
    rec["stage"] = code
    rec["updated_at"] = _now()
    rec.setdefault("stage_history", []).append(
        {"stage": code, "at": _now(), "by": by or "", "note": note or ""})
    save(rec)
    return rec


def add_note(capa_id: str, text: str, *, by: str = "") -> Optional[Dict[str, Any]]:
    rec = get(capa_id)
    if not rec:
        return None
    rec.setdefault("notes", []).append({"at": _now(), "by": by or "", "text": (text or "").strip()})
    rec["updated_at"] = _now()
    save(rec)
    return rec


# ── persistence ───────────────────────────────────────────────────────────────
def save(record: Dict[str, Any]) -> str:
    _ensure_dir()
    path = CAPA_DIR / f"{record['id']}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record["id"]


def get(capa_id: str) -> Optional[Dict[str, Any]]:
    path = CAPA_DIR / f"{(capa_id or '').strip()}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_all() -> List[Dict[str, Any]]:
    if not CAPA_DIR.exists():
        return []
    out: List[Dict[str, Any]] = []
    for p in CAPA_DIR.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _view(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Compact list-row view of a CAPA."""
    return {
        "id": rec.get("id"),
        "company": rec.get("company"),
        "company_id": rec.get("company_id"),
        "title": rec.get("title", "")[:160],
        "standard": rec.get("standard", ""),
        "classification": rec.get("classification", ""),
        "penalty": rec.get("penalty", ""),
        "abatement_date": rec.get("abatement_date", ""),
        "stage": rec.get("stage"),
        "stage_label": STAGE_LABELS.get(rec.get("stage", ""), ""),
        "overdue": is_overdue(rec),
        "human_review_required": rec.get("human_review_required", False),
        "citation_id": rec.get("citation_id", ""),
        "created_at": rec.get("created_at"),
        "updated_at": rec.get("updated_at"),
    }


def list_for_company(company_id: str, *, include_verified: bool = True) -> List[Dict[str, Any]]:
    cid = _slug(company_id)
    rows = [r for r in _load_all() if r.get("company_id") == cid]
    if not include_verified:
        rows = [r for r in rows if r.get("stage") != "verified"]
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return rows


def list_open_for_company(company_id: str) -> List[Dict[str, Any]]:
    """Full records (not views) for open (not-yet-verified) CAPAs — the input the
    risk engine reasons over."""
    return list_for_company(company_id, include_verified=False)


def list_recent(limit: int = 100) -> List[Dict[str, Any]]:
    rows = _load_all()
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return [_view(r) for r in rows[:limit]]


def overview() -> Dict[str, Any]:
    """Portfolio-wide CAPA counts by stage, plus overdue and per-company rollups."""
    rows = _load_all()
    counts = {c: 0 for c in STAGE_CODES}
    overdue = 0
    per_company: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        st = r.get("stage", "open")
        if st in counts:
            counts[st] += 1
        od = is_overdue(r)
        overdue += 1 if od else 0
        cid = r.get("company_id", "")
        pc = per_company.setdefault(cid, {
            "company_id": cid, "company": r.get("company", ""),
            "total": 0, "open": 0, "verified": 0, "overdue": 0})
        pc["total"] += 1
        pc["overdue"] += 1 if od else 0
        if st == "verified":
            pc["verified"] += 1
        else:
            pc["open"] += 1
    total = len(rows)
    return {
        "counts": counts,
        "labels": STAGE_LABELS,
        "order": STAGE_CODES,
        "total": total,
        "verified": counts.get("verified", 0),
        "open": total - counts.get("verified", 0),
        "overdue": overdue,
        "companies": sorted(per_company.values(),
                            key=lambda x: (-x["overdue"], -x["open"])),
    }


# ── routes ────────────────────────────────────────────────────────────────────
def register_capa(app) -> None:
    """Attach CAPA admin routes to an existing FastAPI app. Isolated + non-fatal,
    mirroring abatement.register_abatement / citation_engine.register_citation."""
    from fastapi import Body, Request
    from fastapi.responses import JSONResponse

    @app.get("/api/capa/overview")
    def capa_overview():
        try:
            return {"ok": True, **overview()}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/capa/list")
    def capa_list(company_id: str = ""):
        try:
            if company_id:
                return {"ok": True, "items": [_view(r) for r in list_for_company(company_id)]}
            return {"ok": True, "items": list_recent()}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/capa/{capa_id}")
    def capa_get(capa_id: str):
        rec = get(capa_id)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return rec

    @app.post("/api/capa/from-citation/{citation_id}")
    def capa_from_citation(citation_id: str, body: dict = Body(default=None)):
        from . import citation_engine as ce
        crec = ce.get(citation_id)
        if not crec:
            return JSONResponse({"error": "citation not found"}, status_code=404)
        payload = body if isinstance(body, dict) else {}
        try:
            capa = open_from_citation(crec, company_id=payload.get("company_id", ""),
                                      by=payload.get("by", "owner"))
            return {"ok": True, "capa": capa}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.post("/api/capa/{capa_id}/stage")
    def capa_stage(capa_id: str, body: dict = Body(default=None)):
        payload = body if isinstance(body, dict) else {}
        stage = payload.get("stage", "")
        rec = set_stage(capa_id, stage, by=payload.get("by", ""), note=payload.get("note", ""))
        if not rec:
            return JSONResponse({"error": "not found or invalid stage",
                                 "valid_stages": STAGE_CODES}, status_code=400)
        return {"ok": True, "capa": rec}

    @app.post("/api/capa/{capa_id}/note")
    def capa_note(capa_id: str, body: dict = Body(default=None)):
        payload = body if isinstance(body, dict) else {}
        rec = add_note(capa_id, payload.get("text", ""), by=payload.get("by", ""))
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"ok": True, "capa": rec}

    @app.post("/api/capa/manual")
    def capa_manual(body: dict = Body(default=None)):
        payload = body if isinstance(body, dict) else {}
        if not (payload.get("company") and payload.get("title")):
            return JSONResponse({"error": "company and title are required"}, status_code=400)
        try:
            capa = create_manual(
                company=payload.get("company", ""), title=payload.get("title", ""),
                hazard=payload.get("hazard", ""),
                corrective_action=payload.get("corrective_action", ""),
                classification=payload.get("classification", ""),
                penalty=payload.get("penalty", ""),
                abatement_date=payload.get("abatement_date", ""),
                by=payload.get("by", "owner"))
            return {"ok": True, "capa": capa}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)
