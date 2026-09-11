"""Origin Abatement — Evidence Vault + gap analysis (Phase 1 core spine).

The EVIDENCE and VERIFICATION links of the abatement engine:

    SOURCE → FACT/CONDITION → REQUIREMENT → ACTION → **EVIDENCE → VERIFICATION** → DECISION

Responsibilities:
  * Durable file store for abatement evidence (photos, videos, documents), one
    folder per matter on the persistent volume. Origin's photo-audit layer only
    base64s images to a model and keeps nothing; abatement needs the actual files
    preserved as the record of what was corrected.
  * Originals are never altered. Before/after are stored side by side; a "before"
    file is never overwritten by an "after."
  * Deterministic classification and a per-citation-item **gap analysis**:
    MISSING / INCOMPLETE / CONFLICTING / UNVERIFIED / VERIFIED.
  * Human verification: an attorney/paralegal marks an evidence item verified.
    Nothing is auto-verified; no AI output is ever treated as "OSHA violation
    confirmed" — vision, if attached, is stored only as a hedged observation.

House rules: deterministic, offline, never-fabricate, file-based, isolated +
non-fatal registration. Not legal advice.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from starlette.requests import Request
except Exception:  # pragma: no cover
    Request = Any  # type: ignore

try:
    from .paths import DATA_DIR
except ImportError:  # bare import in ad-hoc scripts
    import os as _os
    DATA_DIR = Path(_os.environ.get("ORIGIN_DATA_DIR") or (Path.home() / ".origin"))

EVIDENCE_DIR = DATA_DIR / "abatement" / "evidence"

# Evidence roles + kinds (deterministic vocab — the gap analyzer reasons over these).
ROLES = ["before", "after", "supporting"]
KINDS = ["photo", "video", "document", "other"]
VERIFY_STATES = ["unverified", "verified"]

# Evidence-gap states per citation item.
GAP_MISSING = "missing"          # no evidence at all
GAP_INCOMPLETE = "incomplete"    # some evidence, but the "after" proof is absent
GAP_UNVERIFIED = "unverified"    # after-evidence present, not yet human-verified
GAP_CONFLICTING = "conflicting"  # an evidence item was flagged as conflicting
GAP_VERIFIED = "verified"        # after-evidence present AND human-verified

# Vision/AI hazard language is always hedged — never "violation confirmed."
VISION_DISCLAIMER = ("Potential hazard/observation only. Not an OSHA determination "
                     "and not a confirmation of a violation. Human verification required.")

_EXT_KIND = {
    ".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".gif": "photo",
    ".heic": "photo", ".webp": "photo",
    ".mp4": "video", ".mov": "video", ".avi": "video", ".webm": "video",
    ".pdf": "document", ".doc": "document", ".docx": "document", ".txt": "document",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _matter_dir(matter_id: str) -> Path:
    d = EVIDENCE_DIR / re.sub(r"[^a-zA-Z0-9_-]", "", matter_id or "unknown")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_path(matter_id: str) -> Path:
    return _matter_dir(matter_id) / "index.json"


def _read_index(matter_id: str) -> List[Dict[str, Any]]:
    p = _index_path(matter_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_index(matter_id: str, items: List[Dict[str, Any]]) -> None:
    _index_path(matter_id).write_text(json.dumps(items, indent=2), encoding="utf-8")


def _classify(filename: str, kind_hint: str = "") -> str:
    if kind_hint in KINDS:
        return kind_hint
    ext = Path(filename or "").suffix.lower()
    return _EXT_KIND.get(ext, "other")


def _norm_role(role: str) -> str:
    r = (role or "").strip().lower()
    return r if r in ROLES else "supporting"


# ── ingest ──────────────────────────────────────────────────────────────────────
def store_evidence(matter_id: str, *, content: bytes, filename: str,
                   citation_item_id: str = "", role: str = "supporting",
                   kind: str = "", caption: str = "", by: str = "",
                   ai_observation: str = "") -> Dict[str, Any]:
    """Persist one evidence file for a matter. The original bytes are written
    unmodified; the stored name is content-addressed so an upload never clobbers
    an existing before/after file. Returns the evidence record."""
    matter_dir = _matter_dir(matter_id)
    sha = hashlib.sha256(content or b"").hexdigest()
    ext = Path(filename or "").suffix.lower()
    ev_id = "ev-" + uuid.uuid4().hex[:10]
    stored_name = f"{ev_id}{ext}"
    (matter_dir / stored_name).write_bytes(content or b"")

    rec = {
        "evidence_id": ev_id,
        "matter_id": matter_id,
        "citation_item_id": (citation_item_id or "").strip(),
        "original_filename": filename or "",
        "stored_name": stored_name,
        "kind": _classify(filename, kind),
        "role": _norm_role(role),
        "classification": _classify(filename, kind),
        "caption": (caption or "").strip(),
        "sha256": sha,
        "bytes": len(content or b""),
        "verification_status": "unverified",
        "verified_by": "",
        "verified_at": "",
        "conflicting": False,
        # If a vision model was run, its output is stored ONLY as a hedged
        # observation, never as a confirmed finding.
        "ai_observation": (ai_observation or "").strip(),
        "ai_disclaimer": VISION_DISCLAIMER if ai_observation else "",
        "uploaded_by": by or "",
        "uploaded_at": _now(),
    }
    items = _read_index(matter_id)
    items.insert(0, rec)
    _write_index(matter_id, items)
    return rec


def store_evidence_b64(matter_id: str, *, data_b64: str, filename: str,
                       **kw) -> Dict[str, Any]:
    """JSON-friendly ingest path: base64 payload → store_evidence."""
    raw = base64.b64decode((data_b64 or "").split(",")[-1] or "")
    return store_evidence(matter_id, content=raw, filename=filename, **kw)


# ── verification (human-in-the-loop) ────────────────────────────────────────────
def verify_evidence(matter_id: str, evidence_id: str, *, by: str = "attorney",
                    status: str = "verified") -> Optional[Dict[str, Any]]:
    items = _read_index(matter_id)
    st = status if status in VERIFY_STATES else "verified"
    for i, r in enumerate(items):
        if r.get("evidence_id") == evidence_id:
            r["verification_status"] = st
            r["verified_by"] = by if st == "verified" else ""
            r["verified_at"] = _now() if st == "verified" else ""
            items[i] = r
            _write_index(matter_id, items)
            return r
    return None


def flag_conflicting(matter_id: str, evidence_id: str, *, conflicting: bool = True,
                     by: str = "", note: str = "") -> Optional[Dict[str, Any]]:
    items = _read_index(matter_id)
    for i, r in enumerate(items):
        if r.get("evidence_id") == evidence_id:
            r["conflicting"] = bool(conflicting)
            r["conflict_note"] = (note or "").strip()
            r["conflict_by"] = by or ""
            items[i] = r
            _write_index(matter_id, items)
            return r
    return None


def list_evidence(matter_id: str, *, citation_item_id: str = "") -> List[Dict[str, Any]]:
    items = _read_index(matter_id)
    if citation_item_id:
        items = [r for r in items if r.get("citation_item_id") == citation_item_id]
    return items


def get_file(matter_id: str, evidence_id: str):
    """Return (bytes, record) for one stored evidence file, or (None, None)."""
    for r in _read_index(matter_id):
        if r.get("evidence_id") == evidence_id:
            p = _matter_dir(matter_id) / r.get("stored_name", "")
            if p.exists():
                return p.read_bytes(), r
            return None, r
    return None, None


# ── gap analysis (MISSING / INCOMPLETE / CONFLICTING / UNVERIFIED / VERIFIED) ────
def item_gap_state(evidence: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deterministic evidence-gap state for one citation item's evidence set."""
    if not evidence:
        return {"state": GAP_MISSING, "evidence_count": 0,
                "before": 0, "after": 0, "verified_after": 0}
    if any(e.get("conflicting") for e in evidence):
        state = GAP_CONFLICTING
    else:
        before = [e for e in evidence if e.get("role") == "before"]
        after = [e for e in evidence if e.get("role") == "after"]
        verified_after = [e for e in after if e.get("verification_status") == "verified"]
        if not after:
            # Evidence exists (before/supporting) but no "after" proof of correction.
            state = GAP_INCOMPLETE
        elif verified_after:
            state = GAP_VERIFIED
        else:
            state = GAP_UNVERIFIED
    return {
        "state": state,
        "evidence_count": len(evidence),
        "before": sum(1 for e in evidence if e.get("role") == "before"),
        "after": sum(1 for e in evidence if e.get("role") == "after"),
        "verified_after": sum(1 for e in evidence
                              if e.get("role") == "after"
                              and e.get("verification_status") == "verified"),
    }


def matter_gap_analysis(matter_id: str, matter_rec: Optional[Dict[str, Any]] = None
                        ) -> Dict[str, Any]:
    """Per-citation-item evidence gap analysis for a whole matter. ``matter_rec``
    may be passed to avoid a re-read (abatement_matter.readiness passes it)."""
    if matter_rec is None:
        from . import abatement_matter as _am
        matter_rec = _am.get(matter_id) or {}
    all_ev = _read_index(matter_id)
    by_item: Dict[str, List[Dict[str, Any]]] = {}
    for e in all_ev:
        by_item.setdefault(e.get("citation_item_id", ""), []).append(e)

    items_out: List[Dict[str, Any]] = []
    for it in matter_rec.get("citation_items", []):
        iid = it.get("item_id", "")
        gs = item_gap_state(by_item.get(iid, []))
        items_out.append({
            "item_id": iid,
            "standard": it.get("standard", ""),
            "classification": it.get("classification", ""),
            "verified_citation": bool(it.get("verified")),
            **gs,
        })
    # Count evidence attached to no citation item (still surfaced, not lost).
    unlinked = len(by_item.get("", []))
    summary = {s: sum(1 for i in items_out if i["state"] == s)
               for s in (GAP_MISSING, GAP_INCOMPLETE, GAP_CONFLICTING,
                         GAP_UNVERIFIED, GAP_VERIFIED)}
    return {
        "matter_id": matter_id,
        "items": items_out,
        "summary": summary,
        "unlinked_evidence": unlinked,
        "legend": {
            GAP_MISSING: "No evidence on file for this citation item.",
            GAP_INCOMPLETE: "Evidence exists but no 'after' proof of correction yet.",
            GAP_UNVERIFIED: "After-evidence present, awaiting human verification.",
            GAP_CONFLICTING: "An evidence item is flagged as conflicting — resolve before review.",
            GAP_VERIFIED: "After-evidence present and human-verified.",
        },
    }


# ── routes ──────────────────────────────────────────────────────────────────────
def register_evidence_vault(app) -> None:
    """Attach Evidence Vault routes. Isolated + non-fatal."""
    from fastapi import Body, UploadFile, File, Form
    from fastapi.responses import JSONResponse, Response

    @app.post("/api/abatement/matters/{matter_id}/evidence")
    async def ev_upload(matter_id: str,
                        file: UploadFile = File(...),
                        citation_item_id: str = Form(""),
                        role: str = Form("supporting"),
                        caption: str = Form(""),
                        by: str = Form("")):
        try:
            content = await file.read()
            rec = store_evidence(matter_id, content=content,
                                 filename=file.filename or "upload",
                                 citation_item_id=citation_item_id, role=role,
                                 caption=caption, by=by)
            return {"ok": True, "evidence": rec}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.post("/api/abatement/matters/{matter_id}/evidence-b64")
    def ev_upload_b64(matter_id: str, body: dict = Body(default=None)):
        p = body if isinstance(body, dict) else {}
        if not (p.get("data_b64") and p.get("filename")):
            return JSONResponse({"error": "data_b64 and filename are required"},
                                status_code=400)
        try:
            rec = store_evidence_b64(
                matter_id, data_b64=p["data_b64"], filename=p["filename"],
                citation_item_id=p.get("citation_item_id", ""),
                role=p.get("role", "supporting"), caption=p.get("caption", ""),
                by=p.get("by", ""))
            return {"ok": True, "evidence": rec}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/abatement/matters/{matter_id}/evidence")
    def ev_list(matter_id: str, citation_item_id: str = ""):
        return {"ok": True, "items": list_evidence(matter_id, citation_item_id=citation_item_id)}

    @app.get("/api/abatement/matters/{matter_id}/evidence/{evidence_id}/file")
    def ev_file(matter_id: str, evidence_id: str):
        data, rec = get_file(matter_id, evidence_id)
        if data is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        import mimetypes
        ctype = mimetypes.guess_type(rec.get("original_filename", ""))[0] or "application/octet-stream"
        return Response(content=data, media_type=ctype)

    @app.post("/api/abatement/matters/{matter_id}/evidence/{evidence_id}/verify")
    def ev_verify(matter_id: str, evidence_id: str, body: dict = Body(default=None)):
        p = body if isinstance(body, dict) else {}
        rec = verify_evidence(matter_id, evidence_id, by=p.get("by", "attorney"),
                              status=p.get("status", "verified"))
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"ok": True, "evidence": rec}

    @app.post("/api/abatement/matters/{matter_id}/evidence/{evidence_id}/conflict")
    def ev_conflict(matter_id: str, evidence_id: str, body: dict = Body(default=None)):
        p = body if isinstance(body, dict) else {}
        rec = flag_conflicting(matter_id, evidence_id,
                               conflicting=bool(p.get("conflicting", True)),
                               by=p.get("by", ""), note=p.get("note", ""))
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"ok": True, "evidence": rec}

    @app.get("/api/abatement/matters/{matter_id}/gap-analysis")
    def ev_gap(matter_id: str):
        return {"ok": True, **matter_gap_analysis(matter_id)}
