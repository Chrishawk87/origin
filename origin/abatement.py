"""ISN Upload Tracker — abatement upload-status ladder for Origin portal clients.

This is the human-in-the-loop layer that tracks a finished compliance document as
it moves from "just built" to "confirmed passing on ISNetworld":

    Draft  →  Human Approved  →  Uploaded to ISN  →  Verified Passed

Design rules (identical philosophy to portal.py, kept deliberately isolated):
  * No database. State lives on the SAME client.json Origin already writes, under a
    NEW top-level key `rec["abatement"]`. That key is untouched by the admin
    "Save & publish" path (which only rewrites documents/coi/platforms/etc.), so a
    document's ladder position survives every profile save.
  * State is stored SEPARATELY from the document rows (not as a field on each doc)
    precisely because admin save rebuilds the documents list from the form and would
    drop any extra keys. Keying by document filename keeps the two joined at read
    time without coupling the write paths.
  * register_abatement(app) is wrapped in try/except by the caller and imports
    nothing heavy, so a bug here can never break the chat app, portal, or dashboard.
  * All routes live under /portal/api/admin/... and reuse the portal's own admin
    cookie, so they inherit the existing back-office auth with no new secrets.

Nothing in portal.py is modified. This module is a pure additive overlay: it reads
the documents Origin already publishes and layers a status ladder on top.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Imported at module scope so FastAPI can resolve the string annotation "Request"
# on the route handlers below. `from __future__ import annotations` turns every
# annotation into a string, and FastAPI resolves them with get_type_hints against
# THIS module's globals — so `Request` must live here, not just inside the
# register function. (portal.py does the same for the same reason.) Without this,
# FastAPI misreads `request: Request` as a required query param and returns 422.
try:
    from starlette.requests import Request
except Exception:  # pragma: no cover
    Request = None  # type: ignore

# Reuse the portal's storage + auth. These are all module-level in portal.py.
from .portal import (
    load_client,
    save_client,
    list_clients,
    _unsign,
    _now,
    ADMIN_COOKIE,
)

# ─────────────────────────── the ladder ───────────────────────────
# Ordered stages. Each is (code, human label). Code is what we store; label is what
# the operator sees. Order defines "forward" progress for the summary/next-action.
STAGES: List[Tuple[str, str]] = [
    ("draft", "Draft"),
    ("human_approved", "Human Approved"),
    ("uploaded_to_isn", "Uploaded to ISN"),
    ("verified_passed", "Verified Passed"),
]
STAGE_CODES: List[str] = [c for c, _ in STAGES]
STAGE_LABELS: Dict[str, str] = {c: lbl for c, lbl in STAGES}

# Documents Origin itself produces are auto-enrolled onto the ladder at "draft".
# Anything else (a COI scan, a client upload) is only tracked once explicitly
# enrolled, so the tracker stays focused on abatement documents.
BUILT_SOURCES = {"origin-draft", "asset-library"}


def normalize_stage(s: str) -> Optional[str]:
    """Accept a stage code OR its human label, in any case / spacing, and return the
    canonical code — or None if it isn't a real stage."""
    if not s:
        return None
    key = str(s).strip().lower().replace(" ", "_").replace("-", "_")
    if key in STAGE_LABELS:
        return key
    # allow matching against the labels too
    for code, label in STAGES:
        if key == label.lower().replace(" ", "_"):
            return code
    return None


def stage_label(code: str) -> str:
    return STAGE_LABELS.get(code, code or "")


def stage_index(code: str) -> int:
    try:
        return STAGE_CODES.index(code)
    except ValueError:
        return -1


# ─────────────────────────── joins ───────────────────────────

def doc_key(doc: Dict[str, Any]) -> str:
    """Stable identity for a document row. Prefer the stored filename (unique per
    client vault); fall back to the display name for rows that have no file yet."""
    return (doc.get("file") or doc.get("name") or "").strip()


def _map(rec: Dict[str, Any]) -> Dict[str, Any]:
    m = rec.get("abatement")
    if not isinstance(m, dict):
        m = {}
        rec["abatement"] = m
    return m


def _is_built(doc: Dict[str, Any]) -> bool:
    return (doc.get("source") or "") in BUILT_SOURCES


def effective_stage(rec: Dict[str, Any], doc: Dict[str, Any]) -> Optional[str]:
    """The ladder position for a document, or None if it isn't on the ladder.
    An explicit stored entry wins; otherwise Origin-built docs imply 'draft'."""
    entry = _map(rec).get(doc_key(doc))
    if entry and normalize_stage(entry.get("stage", "")):
        return normalize_stage(entry.get("stage"))
    if _is_built(doc):
        return "draft"
    return None


def _task_view(rec: Dict[str, Any], doc: Dict[str, Any], stage: str) -> Dict[str, Any]:
    entry = _map(rec).get(doc_key(doc), {})
    idx = stage_index(stage)
    nxt = STAGE_CODES[idx + 1] if 0 <= idx < len(STAGE_CODES) - 1 else None
    return {
        "key": doc_key(doc),
        "name": doc.get("name") or doc.get("file") or "(untitled)",
        "sub": doc.get("sub") or "",
        "file": doc.get("file") or "",
        "source": doc.get("source") or "",
        "stage": stage,
        "stage_label": stage_label(stage),
        "next_stage": nxt,
        "next_label": stage_label(nxt) if nxt else "",
        "standard_code": entry.get("standard_code", ""),
        "abatement_date": entry.get("abatement_date", ""),
        "updated": entry.get("updated", ""),
        "history": entry.get("history", []),
    }


def list_tasks(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Split a client's documents into those on the ISN ladder and those not, and
    compute a stage summary. Read-only; never mutates the record."""
    tasks: List[Dict[str, Any]] = []
    others: List[Dict[str, Any]] = []
    for doc in rec.get("documents", []) or []:
        st = effective_stage(rec, doc)
        if st is None:
            others.append({"key": doc_key(doc),
                           "name": doc.get("name") or doc.get("file") or "(untitled)",
                           "sub": doc.get("sub") or "",
                           "file": doc.get("file") or ""})
        else:
            tasks.append(_task_view(rec, doc, st))
    return {"tasks": tasks, "others": others, "summary": _summary(tasks)}


def _summary(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {c: 0 for c in STAGE_CODES}
    for t in tasks:
        if t["stage"] in counts:
            counts[t["stage"]] += 1
    total = len(tasks)
    verified = counts.get("verified_passed", 0)
    return {
        "counts": counts,
        "labels": STAGE_LABELS,
        "order": STAGE_CODES,
        "total": total,
        "verified": verified,
        "in_progress": total - verified,
    }


# ─────────────────────────── mutations ───────────────────────────

def _doc_for_key(rec: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    for doc in rec.get("documents", []) or []:
        if doc_key(doc) == key:
            return doc
    return None


def set_stage(rec: Dict[str, Any], key: str, stage: str, by: str = "") -> Optional[Dict[str, Any]]:
    """Move a document to a specific ladder stage, recording an audit entry. Creates
    the tracking record if this is the document's first move. Returns the entry, or
    None if the stage is invalid."""
    code = normalize_stage(stage)
    if not code:
        return None
    key = (key or "").strip()
    if not key:
        return None
    m = _map(rec)
    entry = m.get(key)
    if not isinstance(entry, dict):
        entry = {"stage": "", "history": []}
        m[key] = entry
    # snapshot the current document name for resilience if the row is later renamed
    doc = _doc_for_key(rec, key)
    if doc:
        entry["name"] = doc.get("name") or doc.get("file") or key
    if entry.get("stage") != code:
        entry.setdefault("history", []).append({"stage": code, "at": _now(), "by": by or ""})
    entry["stage"] = code
    entry["updated"] = _now()
    return entry


def enroll(rec: Dict[str, Any], key: str, by: str = "") -> Optional[Dict[str, Any]]:
    """Put a document that isn't yet tracked onto the ladder at Draft."""
    if _map(rec).get(key):
        return _map(rec)[key]
    return set_stage(rec, key, "draft", by=by)


def remove(rec: Dict[str, Any], key: str) -> bool:
    """Take a document off the ladder entirely (drops its tracking record)."""
    m = _map(rec)
    if key in m:
        del m[key]
        return True
    return False


# ─────────────────────────── portfolio ───────────────────────────

def overview() -> Dict[str, Any]:
    """Aggregate ladder counts across every client — an at-a-glance operations view
    of how many abatement documents are stuck where."""
    counts = {c: 0 for c in STAGE_CODES}
    per_client: List[Dict[str, Any]] = []
    for c in list_clients():
        rec = load_client(c.get("slug", ""))
        if not rec:
            continue
        summ = list_tasks(rec)["summary"]
        for code in STAGE_CODES:
            counts[code] += summ["counts"].get(code, 0)
        if summ["total"]:
            per_client.append({
                "slug": rec.get("slug"),
                "company": rec.get("company"),
                "total": summ["total"],
                "verified": summ["verified"],
                "in_progress": summ["in_progress"],
                "counts": summ["counts"],
            })
    total = sum(counts.values())
    return {
        "counts": counts,
        "labels": STAGE_LABELS,
        "order": STAGE_CODES,
        "total": total,
        "verified": counts.get("verified_passed", 0),
        "in_progress": total - counts.get("verified_passed", 0),
        "clients": per_client,
    }


# ─────────────────────────── routes ───────────────────────────

def register_abatement(app) -> None:
    """Attach the ISN Upload Tracker admin routes to an existing FastAPI app.
    Mirrors portal.register_portal: admin-cookie guarded, JSON in/out, no new deps."""
    from fastapi import Body, Request
    from fastapi.responses import JSONResponse

    def _admin_ok(request: Request) -> bool:
        p = _unsign(request.cookies.get(ADMIN_COOKIE, ""))
        return bool(p and p.get("role") == "admin")

    @app.get("/portal/api/admin/client/{slug}/abatement")
    def admin_abatement(slug: str, request: Request):
        if not _admin_ok(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_client(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        data = list_tasks(rec)
        data["ok"] = True
        return data

    @app.post("/portal/api/admin/client/{slug}/abatement/stage")
    def admin_abatement_stage(slug: str, request: Request, body: dict = Body(...)):
        if not _admin_ok(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_client(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        key = (body.get("key") or "").strip()
        stage = body.get("stage") or ""
        if not key:
            return JSONResponse({"error": "which document? (key required)"}, status_code=400)
        entry = set_stage(rec, key, stage, by="admin")
        if entry is None:
            return JSONResponse(
                {"error": "invalid stage — use one of: " + ", ".join(STAGE_CODES)},
                status_code=400)
        save_client(rec)
        return {"ok": True, "entry": entry, **list_tasks(rec)}

    @app.post("/portal/api/admin/client/{slug}/abatement/enroll")
    def admin_abatement_enroll(slug: str, request: Request, body: dict = Body(...)):
        if not _admin_ok(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_client(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        key = (body.get("key") or "").strip()
        if not key:
            return JSONResponse({"error": "which document? (key required)"}, status_code=400)
        enroll(rec, key, by="admin")
        save_client(rec)
        return {"ok": True, **list_tasks(rec)}

    @app.post("/portal/api/admin/client/{slug}/abatement/remove")
    def admin_abatement_remove(slug: str, request: Request, body: dict = Body(...)):
        if not _admin_ok(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        rec = load_client(slug)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        key = (body.get("key") or "").strip()
        removed = remove(rec, key)
        if removed:
            save_client(rec)
        return {"ok": True, "removed": removed, **list_tasks(rec)}

    @app.get("/portal/api/admin/abatement/overview")
    def admin_abatement_overview(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"error": "admin only"}, status_code=401)
        return {"ok": True, **overview()}
