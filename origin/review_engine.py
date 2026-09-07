"""Human-Review Queue — the Safety Intelligence Engine's review store (Stage 5).

Stage 4 gave every audit a `review_queue`: unmatched or low-confidence findings
were routed there instead of silently becoming a corrective action. But that
queue was a lightweight list riding on the audit record — no reviewer identity,
no logged decision, no timestamp, no inbox to work it. Stage 5 makes review a
real, first-class store.

A ReviewItem is one thing waiting on a human: an audit finding Origin could not
confidently action. The item carries enough context to review it (title,
severity, confidence, WHY it was queued, the tentative citation) plus the one
piece of net-new information no other collection holds — the reviewer's DECISION
(who, when, approve / approve-with-edits / reject, and the resulting action).

Trust story, unchanged from every other stage:
  * The audit record's review_queue + finding.route stays the source of truth for
    WHAT needs review (derived upstream from photo_audit's honest confidence).
  * `ingest_from_audits()` reconstructs the PENDING side of this store from those
    audits — idempotent, so it is safe to run on every inbox load. Nothing about a
    pending item is authoritative here; delete the store and re-ingest.
  * The only authoritative data a ReviewItem owns is the human decision, and every
    decision writes STRAIGHT BACK to the audit -> CAPA chain (approve = open the
    CAPA via audit_engine.promote_finding; reject = dismiss via reject_finding), so
    the queue can never drift into a second, contradictory truth.

Dependency direction is review -> audit (never the reverse): audit_engine must
stay ignorant of this layer so a bug here can't break recording an audit. Same
house rules as the rest of the SIE: file-based on the persistent volume, fully
deterministic and offline (consults no model), never fabricates, isolated +
non-fatal registration.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Imported at module level so FastAPI can resolve the `request: Request`
# annotations on the routes below — with `from __future__ import annotations`
# active, annotations are strings resolved against module globals, so a
# function-local import would leave `Request` unresolved (FastAPI would then
# mistake `request` for a query param). Guarded: the engine must still import
# even if FastAPI is absent (offline/self-test contexts).
try:  # pragma: no cover - trivial import guard
    from fastapi import Request
except Exception:  # pragma: no cover
    Request = None  # type: ignore

try:
    from .paths import DATA_DIR
except ImportError:  # bare import in ad-hoc scripts
    import os as _os
    DATA_DIR = Path(_os.environ.get("ORIGIN_DATA_DIR") or (Path.home() / ".origin"))

REVIEW_DIR = DATA_DIR / "review"

# Decision states. A pending item is awaiting a human; the other two are terminal
# and always accompanied by a logged reviewer + timestamp + resulting action.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or "item"


def _ensure_dir() -> None:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)


def item_id_for(source_type: str, source_id: str, ref_id: str) -> str:
    """A STABLE id for the item a (source_type, source_id, ref_id) triple names,
    so re-ingesting the same audit finding updates the one item instead of
    duplicating it. For an audit finding that is (audit_id, finding_id)."""
    return f"rev-{_slug(source_type)}-{_slug(source_id)}-{_slug(ref_id)}"


# ── persistence ───────────────────────────────────────────────────────────────
def save(rec: Dict[str, Any]) -> str:
    _ensure_dir()
    path = REVIEW_DIR / f"{rec['item_id']}.json"
    path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return rec["item_id"]


def get(item_id: str) -> Optional[Dict[str, Any]]:
    path = REVIEW_DIR / f"{(item_id or '').strip()}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_all() -> List[Dict[str, Any]]:
    if not REVIEW_DIR.exists():
        return []
    out: List[Dict[str, Any]] = []
    for p in REVIEW_DIR.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


# ── opening / ingesting items ────────────────────────────────────────────────
def open_item(*, source_type: str, source_id: str, ref_id: str,
              company_id: str = "", company: str = "", title: str = "",
              severity: str = "medium", confidence: float = 0.0,
              reason: str = "", standard: str = "",
              by: str = "system") -> Dict[str, Any]:
    """Insert-or-refresh one review item. Idempotent by stable id: a still-pending
    item is refreshed with the latest context; an item a human has already DECIDED
    is left untouched (its decision is authoritative and must never be clobbered by
    a re-ingest)."""
    iid = item_id_for(source_type, source_id, ref_id)
    existing = get(iid)
    if existing and existing.get("status") != STATUS_PENDING:
        return existing  # decided items are immutable to ingest
    rec = existing or {
        "item_id": iid,
        "status": STATUS_PENDING,
        "created_at": _now(),
        "reviewer": "",
        "decided_at": "",
        "decision": "",
        "resulting_action": {},
        "note": "",
    }
    rec.update({
        "source_type": source_type,
        "source_id": source_id,
        "ref_id": ref_id,
        "company_id": company_id,
        "company": company,
        "title": (title or "").strip() or "Unspecified hazard",
        "severity": (severity or "medium").strip().lower(),
        "confidence": round(float(confidence or 0.0), 2),
        "reason": reason,
        "standard": standard,
        "updated_at": _now(),
        "opened_by": rec.get("opened_by") or by,
    })
    save(rec)
    return rec


def ingest_from_audits() -> Dict[str, Any]:
    """Reconstruct the PENDING side of the store from every audit's review_queue.
    Idempotent and cheap — safe to run on every inbox load (the "self-healing
    inbox", mirroring the spine's auto-rebuild). Opens a pending item for each
    still-queued audit finding; leaves already-decided items alone. Offline, no
    model, and it never mutates an audit."""
    opened = 0
    seen = 0
    try:
        from . import audit_engine as ae
        audits = ae._load_all() if hasattr(ae, "_load_all") else []
    except Exception:
        audits = []
    for a in audits:
        aid = (a.get("id") or "").strip()
        if not aid:
            continue
        company = a.get("company", "")
        cid = a.get("company_id", "")
        for q in (a.get("review_queue") or []):
            fid = (q.get("finding_id") or "").strip()
            if not fid:
                continue
            seen += 1
            before = get(item_id_for("audit_finding", aid, fid))
            open_item(
                source_type="audit_finding", source_id=aid, ref_id=fid,
                company_id=cid, company=company,
                title=q.get("title", ""), severity=q.get("severity", "medium"),
                confidence=q.get("confidence", 0.0), reason=q.get("reason", ""),
                standard=q.get("standard", ""))
            if before is None:
                opened += 1
    return {"ingested": seen, "opened_new": opened}


# ── decisions (write back to the audit -> CAPA chain) ────────────────────────
def approve(item_id: str, *, by: str = "owner",
            edits: Optional[Dict[str, Any]] = None,
            note: str = "") -> Optional[Dict[str, Any]]:
    """A human confirms a candidate is real. For an audit finding this opens the
    CAPA on the audit chain (optionally applying reviewer edits — e.g. supplying
    the citation an unmatched detection lacked), then records the decision on the
    item. The corrective action never exists without this logged human decision."""
    rec = get(item_id)
    if not rec:
        return None
    action: Dict[str, Any] = {"type": "none"}
    if rec.get("source_type") == "audit_finding":
        try:
            from . import audit_engine as ae
            out = ae.promote_finding(rec["source_id"], rec["ref_id"],
                                     by=by, edits=edits)
            if out is None:
                return {"error": "source audit/finding not found", "item": rec}
            capa_rec = out.get("capa") or {}
            action = {"type": "capa", "capa_id": capa_rec.get("id", ""),
                      "created": out.get("created", False)}
        except Exception as exc:
            return {"error": f"approve write-back failed: {exc}", "item": rec}
    rec.update({
        "status": STATUS_APPROVED,
        "decision": "approve",
        "reviewer": by,
        "decided_at": _now(),
        "resulting_action": action,
        "note": (note or "").strip(),
        "edits": edits if isinstance(edits, dict) else {},
        "updated_at": _now(),
    })
    save(rec)
    return {"ok": True, "item": rec}


def reject(item_id: str, *, by: str = "owner",
           note: str = "") -> Optional[Dict[str, Any]]:
    """A human dismisses a candidate as not actionable. For an audit finding this
    clears it from the audit's review queue (no CAPA opens) and logs the decision.
    Nothing model-derived is discarded silently — the rejection is recorded on the
    chain with a reviewer and timestamp."""
    rec = get(item_id)
    if not rec:
        return None
    action: Dict[str, Any] = {"type": "none"}
    if rec.get("source_type") == "audit_finding":
        try:
            from . import audit_engine as ae
            out = ae.reject_finding(rec["source_id"], rec["ref_id"],
                                    by=by, note=note)
            if out is None:
                return {"error": "source audit/finding not found", "item": rec}
            action = {"type": "dismissed"}
        except Exception as exc:
            return {"error": f"reject write-back failed: {exc}", "item": rec}
    rec.update({
        "status": STATUS_REJECTED,
        "decision": "reject",
        "reviewer": by,
        "decided_at": _now(),
        "resulting_action": action,
        "note": (note or "").strip(),
        "updated_at": _now(),
    })
    save(rec)
    return {"ok": True, "item": rec}


# ── read side ────────────────────────────────────────────────────────────────
def _view(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "item_id": rec.get("item_id"),
        "status": rec.get("status"),
        "company": rec.get("company"),
        "company_id": rec.get("company_id"),
        "title": rec.get("title"),
        "severity": rec.get("severity"),
        "confidence": rec.get("confidence"),
        "reason": rec.get("reason"),
        "standard": rec.get("standard"),
        "source_type": rec.get("source_type"),
        "source_id": rec.get("source_id"),
        "ref_id": rec.get("ref_id"),
        "reviewer": rec.get("reviewer", ""),
        "decision": rec.get("decision", ""),
        "decided_at": rec.get("decided_at", ""),
        "resulting_action": rec.get("resulting_action", {}),
        "created_at": rec.get("created_at"),
    }


def _gc_company_ids(gc_slug: str) -> set:
    """The set of company_ids owned by one GC (Stage 3 tenant boundary). Used to
    scope portfolio-wide review rollups to a single tenant's slice — a GC must
    never see another GC's items. Returns an empty set on any failure, which
    means a scoped view shows nothing rather than everything (fail-closed)."""
    try:
        from . import company_profile as cp
        return {_slug(r.get("company_id", ""))
                for r in cp.list_all(gc_slug=gc_slug)}
    except Exception:
        return set()


def list_items(company_id: str = "", status: str = "",
               gc_slug: str = "") -> List[Dict[str, Any]]:
    """List review items, newest first. Filter by company and/or status. A
    high-severity, still-pending item sorts to the top of an inbox. When
    ``gc_slug`` is set, the list is scoped to that GC's own companies (Stage 3):
    every other tenant's items are excluded before any other filter."""
    cid = _slug(company_id) if company_id else ""
    rows = _load_all()
    if gc_slug:
        owned = _gc_company_ids(gc_slug)
        rows = [r for r in rows if _slug(r.get("company_id", "")) in owned]
    if cid:
        rows = [r for r in rows if _slug(r.get("company_id", "")) == cid]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    sev_rank = {"high": 0, "medium": 1, "low": 2}
    rows.sort(key=lambda r: (
        0 if r.get("status") == STATUS_PENDING else 1,
        sev_rank.get(r.get("severity"), 1),
        r.get("created_at", "")), reverse=False)
    return [_view(r) for r in rows]


def overview(gc_slug: str = "") -> Dict[str, Any]:
    """Portfolio-wide review rollup: how many items are pending / approved /
    rejected, plus a per-company breakdown for a console inbox. When ``gc_slug``
    is set, the rollup covers only that GC's own companies (Stage 3 tenant
    scoping) so a GC console never counts another tenant's items."""
    rows = _load_all()
    if gc_slug:
        owned = _gc_company_ids(gc_slug)
        rows = [r for r in rows if _slug(r.get("company_id", "")) in owned]
    pending = sum(1 for r in rows if r.get("status") == STATUS_PENDING)
    approved = sum(1 for r in rows if r.get("status") == STATUS_APPROVED)
    rejected = sum(1 for r in rows if r.get("status") == STATUS_REJECTED)
    per_company: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        cid = r.get("company_id", "")
        pc = per_company.setdefault(cid, {
            "company_id": cid, "company": r.get("company", ""),
            "pending": 0, "approved": 0, "rejected": 0})
        st = r.get("status")
        if st == STATUS_PENDING:
            pc["pending"] += 1
        elif st == STATUS_APPROVED:
            pc["approved"] += 1
        elif st == STATUS_REJECTED:
            pc["rejected"] += 1
    return {
        "total": len(rows),
        "pending": pending,
        "approved": approved,
        "rejected": rejected,
        "companies": sorted(per_company.values(), key=lambda x: -x["pending"]),
    }


# ── routes (gated by _auth under /api/*, isolated + non-fatal, owner-only) ────
def register_review(app) -> None:
    """Attach human-review-queue routes. Tenant-aware (Stage 3): an owner/admin
    session sees the whole portfolio; a logged-in GC sees only its own companies'
    items. Scoping is enforced two ways — the portfolio rollups (overview/list)
    filter by ``request.state.sie_gc_slug`` here, and the per-item routes are
    ownership-gated in server.py's _auth (a GC can only touch an item whose
    company it owns). Isolated + non-fatal, fully offline."""
    from fastapi import Body, Request
    from fastapi.responses import JSONResponse

    def _gc(request) -> str:
        # Owner/admin → "" (whole portfolio); GC → its own slug (its slice only).
        return (getattr(request.state, "sie_gc_slug", None) or "")

    @app.post("/api/review/ingest")
    def review_ingest(body: dict = Body(default=None)):
        try:
            return {"ok": True, **ingest_from_audits()}
        except Exception as exc:  # never 500 the tool
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/review/overview")
    def review_overview(request: Request):
        try:
            ingest_from_audits()  # self-healing inbox: sync before reporting
        except Exception:
            pass
        try:
            return {"ok": True, **overview(gc_slug=_gc(request))}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/review/list")
    def review_list(request: Request, company_id: str = "", status: str = ""):
        try:
            ingest_from_audits()  # self-healing inbox
        except Exception:
            pass
        try:
            return {"ok": True,
                    "items": list_items(company_id, status, gc_slug=_gc(request))}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/review/{item_id}")
    def review_get(item_id: str):
        rec = get(item_id)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return rec

    @app.post("/api/review/{item_id}/approve")
    def review_approve(item_id: str, request: Request, body: dict = Body(default=None)):
        # A GC reaches this only for an item it owns (enforced in _auth). Stamp
        # the reviewer as the GC when it's a GC session, else the owner.
        payload = body if isinstance(body, dict) else {}
        by = payload.get("by") or _gc(request) or "owner"
        out = approve(item_id, by=by,
                      edits=payload.get("edits"), note=payload.get("note", ""))
        if out is None:
            return JSONResponse({"error": "review item not found"}, status_code=404)
        return out

    @app.post("/api/review/{item_id}/reject")
    def review_reject(item_id: str, request: Request, body: dict = Body(default=None)):
        payload = body if isinstance(body, dict) else {}
        by = payload.get("by") or _gc(request) or "owner"
        out = reject(item_id, by=by, note=payload.get("note", ""))
        if out is None:
            return JSONResponse({"error": "review item not found"}, status_code=404)
        return out
