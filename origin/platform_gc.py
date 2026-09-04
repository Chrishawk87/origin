"""The GC Console — a General Contractor's workspace on the white-label platform.

This is the screen a GC admin logs into. It is scoped hard to their own gc_id:
a GC admin can only ever see and touch their own subcontractors, grades, and
messages. The OWNER (Chris) can also open any GC's workspace by passing ?gc=<id>
— his master key — which lets him run Origin Management Solutions as his own
first GC, and drop into any client's workspace to support them.

From here a GC can:
  * see its subcontractor roster with prequal grades, COI status, and a
    green/amber/red health dot,
  * add a subcontractor,
  * record / update a sub's prequal grade on any platform (ISN, Avetta, ...),
  * remove a subcontractor when they stop working together — which also deletes
    that sub's login credentials so they lose access.

Isolated like the rest of the platform: its own module, wrapped in try/except at
registration, importing only platform_db + platform_auth. A bug here cannot
touch the AI app, the portal, the owner console, or the platform auth layer.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, func

from . import platform_db as db
from . import platform_auth as auth
from .platform_db import (
    Tenant, User, Subcontractor, ComplianceStatus, COI,
    Document, Message, LibraryProgram,
    ROLE_OWNER, ROLE_GC_ADMIN, PLATFORMS,
)

try:
    from starlette.requests import Request
except Exception:  # pragma: no cover
    Request = None  # type: ignore


def _health(grade_values) -> str:
    """Roll a set of letter grades up to green / amber / red."""
    vals = [(g or "").upper() for g in grade_values]
    if any(v in ("F", "D") for v in vals):
        return "red"
    if any(v == "C" for v in vals):
        return "amber"
    return "green"


def _gc_context(request, gc_param: Optional[str]) -> Tuple[Optional[str], Optional[dict]]:
    """Resolve which GC the caller may act on.

    owner    → the GC named in ?gc=<id> (their master key; must specify one).
    gc_admin → their own gc_id, always; the ?gc param is ignored so they can
               never reach another GC.
    sub / anonymous → refused (None).
    Returns (gc_id, claims) or (None, claims/None).
    """
    claims = auth.read_session(request)
    if not claims:
        return None, None
    role = claims.get("role")
    if role == ROLE_OWNER:
        return ((gc_param or "").strip() or None), claims
    if role == ROLE_GC_ADMIN:
        return (claims.get("gc_id") or None), claims
    return None, claims


def _unique_sub_slug(sess, gc_id: str, base: str) -> str:
    root = auth.slugify(base)
    slug = root
    n = 2
    while sess.scalar(select(Subcontractor).where(
            Subcontractor.gc_id == gc_id, Subcontractor.slug == slug)) is not None:
        slug = f"{root}-{n}"
        n += 1
    return slug


def register_gc(app) -> None:
    """Wire the GC console page + gc-scoped roster routes onto the app.

    Wrapped in try/except by the caller so any failure leaves the app untouched.
    """
    from fastapi import Body
    from fastapi.responses import JSONResponse, HTMLResponse

    def _deny():
        return JSONResponse({"error": "not allowed"}, status_code=403)

    # ── roster (read) ────────────────────────────────────────────────────
    @app.get("/platform/gc/roster")
    def roster(request: Request, gc: str = ""):
        gc_id, claims = _gc_context(request, gc)
        if not claims or claims.get("role") not in (ROLE_OWNER, ROLE_GC_ADMIN):
            return _deny()
        if not gc_id:
            return JSONResponse({"error": "no GC selected"}, status_code=400)
        with db.session() as s:
            t = s.get(Tenant, gc_id)
            if not t:
                return JSONResponse({"error": "GC not found"}, status_code=404)
            subs = s.scalars(select(Subcontractor).where(
                Subcontractor.gc_id == gc_id).order_by(Subcontractor.name)).all()
            out = []
            for sub in subs:
                grades = s.scalars(select(ComplianceStatus).where(
                    ComplianceStatus.sub_id == sub.id)).all()
                gmap = {g.platform: g.grade for g in grades}
                cois = s.scalars(select(COI).where(COI.sub_id == sub.id)).all()
                coi = None
                if cois:
                    latest = max(cois, key=lambda c: c.expiry or date.min)
                    days = (latest.expiry - date.today()).days if latest.expiry else None
                    coi = {"carrier": latest.carrier, "coverage": latest.coverage,
                           "expiry": latest.expiry.isoformat() if latest.expiry else None,
                           "days_left": days}
                out.append({
                    "id": sub.id, "name": sub.name,
                    "scope": sub.scope_of_work or [],
                    "contact_name": sub.contact_name, "contact_email": sub.contact_email,
                    "trir": sub.trir, "emr": sub.emr, "health": sub.health,
                    "grades": gmap, "coi": coi,
                })
            gc_info = {"id": t.id, "name": t.name, "slug": t.slug,
                       "brand_primary": t.brand_primary, "brand_text": t.brand_text}
        return {"gc": gc_info, "platforms": list(PLATFORMS), "subs": out,
                "is_owner": claims.get("role") == ROLE_OWNER}

    # ── add a subcontractor ──────────────────────────────────────────────
    @app.post("/platform/gc/subs")
    def add_sub(request: Request, body: dict = Body(...)):
        gc_id, claims = _gc_context(request, body.get("gc"))
        if not claims or claims.get("role") not in (ROLE_OWNER, ROLE_GC_ADMIN):
            return _deny()
        if not gc_id:
            return JSONResponse({"error": "no GC selected"}, status_code=400)
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "subcontractor name is required"},
                                status_code=400)
        scope = body.get("scope_of_work") or []
        if isinstance(scope, str):
            scope = [x.strip() for x in scope.split(",") if x.strip()]
        with db.session() as s:
            if not s.get(Tenant, gc_id):
                return JSONResponse({"error": "GC not found"}, status_code=404)
            sub = Subcontractor(
                gc_id=gc_id, name=name,
                slug=_unique_sub_slug(s, gc_id, name),
                contact_name=(body.get("contact_name") or "").strip(),
                contact_email=(body.get("contact_email") or "").strip().lower(),
                scope_of_work=scope,
                trir=body.get("trir"), emr=body.get("emr"))
            s.add(sub)
            s.commit()
            new = {"id": sub.id, "name": sub.name}
        return {"ok": True, "sub": new}

    # ── record / update a prequal grade ──────────────────────────────────
    @app.post("/platform/gc/subs/{sub_id}/grade")
    def set_grade(sub_id: str, request: Request, body: dict = Body(...)):
        gc_id, claims = _gc_context(request, body.get("gc"))
        if not claims or claims.get("role") not in (ROLE_OWNER, ROLE_GC_ADMIN):
            return _deny()
        platform = (body.get("platform") or "").strip().lower()
        grade = (body.get("grade") or "").strip().upper()
        if platform not in PLATFORMS:
            return JSONResponse(
                {"error": f"platform must be one of {', '.join(PLATFORMS)}"},
                status_code=400)
        with db.session() as s:
            sub = s.get(Subcontractor, sub_id)
            if not sub or sub.gc_id != gc_id:
                return JSONResponse({"error": "subcontractor not found"},
                                    status_code=404)
            row = s.scalar(select(ComplianceStatus).where(
                ComplianceStatus.sub_id == sub_id,
                ComplianceStatus.platform == platform))
            if row:
                row.grade = grade
                row.source = "gc"
                row.graded_on = date.today()
            else:
                s.add(ComplianceStatus(gc_id=gc_id, sub_id=sub_id,
                                       platform=platform, grade=grade,
                                       status="active", source="gc",
                                       graded_on=date.today()))
            s.flush()
            all_grades = s.scalars(select(ComplianceStatus.grade).where(
                ComplianceStatus.sub_id == sub_id)).all()
            sub.health = _health(all_grades)
            s.commit()
            health = sub.health
        return {"ok": True, "health": health}

    # ── remove a subcontractor (and their login credentials) ─────────────
    @app.delete("/platform/gc/subs/{sub_id}")
    def delete_sub(sub_id: str, request: Request, gc: str = ""):
        gc_id, claims = _gc_context(request, gc)
        if not claims or claims.get("role") not in (ROLE_OWNER, ROLE_GC_ADMIN):
            return _deny()
        with db.session() as s:
            sub = s.get(Subcontractor, sub_id)
            if not sub or sub.gc_id != gc_id:
                return JSONResponse({"error": "subcontractor not found"},
                                    status_code=404)
            name = sub.name
            # first revoke every login tied to this sub (their credentials)
            logins = s.scalars(select(User).where(User.sub_id == sub_id)).all()
            revoked = [u.email for u in logins]
            for u in logins:
                s.delete(u)
            # then the sub itself — cascades grades, COIs, documents, messages
            s.delete(sub)
            s.commit()
        return {"ok": True, "deleted": name, "revoked_logins": revoked}

    # ── one subcontractor in full: grades, COI, documents, messages ──────
    @app.get("/platform/gc/subs/{sub_id}")
    def sub_detail(sub_id: str, request: Request, gc: str = ""):
        gc_id, claims = _gc_context(request, gc)
        if not claims or claims.get("role") not in (ROLE_OWNER, ROLE_GC_ADMIN):
            return _deny()
        with db.session() as s:
            sub = s.get(Subcontractor, sub_id)
            if not sub or sub.gc_id != gc_id:
                return JSONResponse({"error": "subcontractor not found"},
                                    status_code=404)
            grades = {g.platform: g.grade for g in s.scalars(
                select(ComplianceStatus).where(
                    ComplianceStatus.sub_id == sub_id)).all()}
            docs = [{"id": d.id, "name": d.name, "category": d.category,
                     "source": d.source,
                     "created_at": d.created_at.isoformat() if d.created_at else None}
                    for d in s.scalars(select(Document).where(
                        Document.sub_id == sub_id).order_by(
                        Document.created_at.desc())).all()]
            msgs = [{"id": m.id, "role": m.sender_role, "body": m.body,
                     "created_at": m.created_at.isoformat() if m.created_at else None}
                    for m in s.scalars(select(Message).where(
                        Message.sub_id == sub_id).order_by(
                        Message.created_at)).all()]
            cois = s.scalars(select(COI).where(COI.sub_id == sub_id)).all()
            coi = None
            if cois:
                latest = max(cois, key=lambda c: c.expiry or date.min)
                days = (latest.expiry - date.today()).days if latest.expiry else None
                coi = {"carrier": latest.carrier, "coverage": latest.coverage,
                       "expiry": latest.expiry.isoformat() if latest.expiry else None,
                       "days_left": days}
            library = [{"id": p.id, "title": p.title, "category": p.category}
                       for p in s.scalars(select(LibraryProgram).order_by(
                           LibraryProgram.title)).all()]
            out = {
                "id": sub.id, "name": sub.name, "scope": sub.scope_of_work or [],
                "contact_name": sub.contact_name, "contact_email": sub.contact_email,
                "trir": sub.trir, "emr": sub.emr, "health": sub.health,
                "grades": grades, "coi": coi, "docs": docs, "messages": msgs,
            }
        return {"sub": out, "platforms": list(PLATFORMS), "library": library,
                "is_owner": claims.get("role") == ROLE_OWNER}

    # ── post a message to a subcontractor's thread ───────────────────────
    @app.post("/platform/gc/subs/{sub_id}/messages")
    def post_message(sub_id: str, request: Request, body: dict = Body(...)):
        gc_id, claims = _gc_context(request, body.get("gc"))
        if not claims or claims.get("role") not in (ROLE_OWNER, ROLE_GC_ADMIN):
            return _deny()
        text = (body.get("body") or "").strip()
        if not text:
            return JSONResponse({"error": "message is empty"}, status_code=400)
        with db.session() as s:
            sub = s.get(Subcontractor, sub_id)
            if not sub or sub.gc_id != gc_id:
                return JSONResponse({"error": "subcontractor not found"},
                                    status_code=404)
            m = Message(gc_id=gc_id, sub_id=sub_id,
                        sender_user_id=claims.get("uid"),
                        sender_role=ROLE_GC_ADMIN, body=text,
                        read_by_gc=True, read_by_sub=False)
            s.add(m)
            s.commit()
            new = {"id": m.id, "role": m.sender_role, "body": m.body,
                   "created_at": m.created_at.isoformat() if m.created_at else None}
        return {"ok": True, "message": new}

    # ── add / fulfil a document for a subcontractor ──────────────────────
    @app.post("/platform/gc/subs/{sub_id}/docs")
    def add_doc(sub_id: str, request: Request, body: dict = Body(...)):
        gc_id, claims = _gc_context(request, body.get("gc"))
        if not claims or claims.get("role") not in (ROLE_OWNER, ROLE_GC_ADMIN):
            return _deny()
        with db.session() as s:
            sub = s.get(Subcontractor, sub_id)
            if not sub or sub.gc_id != gc_id:
                return JSONResponse({"error": "subcontractor not found"},
                                    status_code=404)
            name = (body.get("name") or "").strip()
            category = (body.get("category") or "").strip()
            source = "upload"
            lib_id = (body.get("library_id") or "").strip()
            if lib_id:
                prog = s.get(LibraryProgram, lib_id)
                if not prog:
                    return JSONResponse({"error": "library program not found"},
                                        status_code=404)
                name = name or prog.title
                category = category or prog.category
                source = "library"
            if not name:
                return JSONResponse({"error": "document name is required"},
                                    status_code=400)
            d = Document(gc_id=gc_id, sub_id=sub_id, name=name,
                         category=category, source=source,
                         uploaded_by=claims.get("uid"))
            s.add(d)
            s.commit()
            new = {"id": d.id, "name": d.name, "category": d.category,
                   "source": d.source,
                   "created_at": d.created_at.isoformat() if d.created_at else None}
        return {"ok": True, "doc": new}

    # ── remove a document ────────────────────────────────────────────────
    @app.delete("/platform/gc/subs/{sub_id}/docs/{doc_id}")
    def delete_doc(sub_id: str, doc_id: str, request: Request, gc: str = ""):
        gc_id, claims = _gc_context(request, gc)
        if not claims or claims.get("role") not in (ROLE_OWNER, ROLE_GC_ADMIN):
            return _deny()
        with db.session() as s:
            d = s.get(Document, doc_id)
            if not d or d.sub_id != sub_id or d.gc_id != gc_id:
                return JSONResponse({"error": "document not found"}, status_code=404)
            s.delete(d)
            s.commit()
        return {"ok": True}

    # ── the GC console page ──────────────────────────────────────────────
    @app.get("/platform/gc", response_class=HTMLResponse)
    def gc_page():
        return HTMLResponse(_GC_HTML)

_GC_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Origin — GC Workspace</title>
<style>
  :root { --brand:#1E7A46; --ink:#12211a; --muted:#5b6b63; --line:#e6ebe8;
          --bg:#f4f6f5; --card:#fff; --red:#c0392b; --amber:#d99200; --ok:#1E7A46; }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
         Helvetica,Arial,sans-serif; color:var(--ink); background:var(--bg); }
  header { color:#fff; padding:16px 26px; display:flex; align-items:center; justify-content:space-between;
    box-shadow:0 1px 0 rgba(0,0,0,.06); }
  header .brand { display:flex; align-items:center; gap:12px; }
  header .logo { width:34px; height:34px; border-radius:9px; background:rgba(255,255,255,.2);
    display:flex; align-items:center; justify-content:center; font-weight:800; font-size:15px; }
  header h1 { margin:0; font-size:18px; font-weight:600; letter-spacing:.2px; }
  header .r { display:flex; gap:10px; align-items:center; font-size:13px; }
  header .badge { background:rgba(255,255,255,.18); padding:5px 10px; border-radius:20px; font-weight:600; }
  header a, header button { color:#fff; text-decoration:none; background:rgba(255,255,255,.16);
    border:0; padding:8px 13px; border-radius:8px; cursor:pointer; font-size:13px; font-weight:600; }
  header a:hover, header button:hover { background:rgba(255,255,255,.3); }
  main { max-width:1080px; margin:0 auto; padding:24px 22px 70px; }

  .tiles { display:grid; grid-template-columns:repeat(5,1fr); gap:12px; margin-bottom:20px; }
  .tile { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .tile .n { font-size:26px; font-weight:700; line-height:1.1; }
  .tile .l { font-size:12px; color:var(--muted); margin-top:2px; font-weight:600; text-transform:uppercase; letter-spacing:.3px; }
  .tile.green .n{color:var(--ok)} .tile.amber .n{color:var(--amber)} .tile.red .n{color:var(--red)} .tile.warn .n{color:var(--amber)}
  @media(max-width:760px){ .tiles{grid-template-columns:repeat(2,1fr)} }

  .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:20px 22px; margin-bottom:18px; }
  .card h2 { font-size:15px; margin:0 0 14px; display:flex; align-items:center; justify-content:space-between; }
  .card h2 .hint { font-weight:400; font-size:12px; color:var(--muted); }
  label { display:block; font-size:12px; color:var(--muted); margin:0 0 4px; font-weight:600; }
  input[type=text],input[type=email],select,textarea { width:100%; padding:9px 11px; border:1px solid var(--line);
    border-radius:9px; font-size:14px; font-family:inherit; background:#fff; }
  textarea { resize:vertical; min-height:60px; }
  .row { display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end; }
  .row > div { flex:1; min-width:150px; }
  button.primary { background:var(--brand); color:#fff; border:0; padding:10px 16px; border-radius:9px;
    font-size:14px; font-weight:600; cursor:pointer; }
  button.primary:hover { filter:brightness(1.07); }
  button.link { background:none; border:0; color:var(--red); cursor:pointer; font-size:13px; padding:0; font-weight:600; }

  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:12px 10px; border-bottom:1px solid var(--line); font-size:14px; vertical-align:middle; }
  th { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.4px; }
  tbody tr { cursor:pointer; }
  tbody tr:hover { background:#fafbfa; }
  .dot { display:inline-block; width:11px; height:11px; border-radius:50%; margin-right:8px; vertical-align:middle; }
  .dot.green{background:var(--ok)} .dot.amber{background:var(--amber)} .dot.red{background:var(--red)}
  .grade { display:inline-block; min-width:24px; text-align:center; padding:3px 7px; border-radius:7px;
    font-size:12px; font-weight:700; background:#eef2f0; margin-right:4px; }
  .grade.f,.grade.d { background:#f7dede; color:var(--red); }
  .grade.c { background:#fbf0d3; color:#8a6400; }
  .grade.a,.grade.b { background:#dff0e6; color:var(--ok); }
  .grade.none { background:#f0f0f0; color:#aaa; }
  .coi-ok{color:var(--ok);font-weight:600} .coi-soon{color:var(--amber);font-weight:600} .coi-exp{color:var(--red);font-weight:600}
  .scope { font-size:12px; color:var(--muted); }
  .empty { color:var(--muted); font-size:14px; padding:6px 0; }
  .note { font-size:12px; color:var(--muted); margin-top:8px; }
  .err{color:var(--red);font-weight:600}.ok{color:var(--ok);font-weight:600}
  .hidden{display:none!important}
  .open-btn { background:#eef2f0; color:var(--brand); border:0; padding:6px 12px; border-radius:7px; font-size:13px; font-weight:600; cursor:pointer; }

  /* slide-over drawer */
  .scrim { position:fixed; inset:0; background:rgba(10,20,15,.4); opacity:0; pointer-events:none; transition:opacity .2s; z-index:40; }
  .scrim.show { opacity:1; pointer-events:auto; }
  .drawer { position:fixed; top:0; right:0; height:100vh; width:min(560px,94vw); background:var(--bg);
    box-shadow:-8px 0 40px rgba(0,0,0,.18); transform:translateX(100%); transition:transform .22s ease; z-index:50;
    display:flex; flex-direction:column; }
  .drawer.show { transform:translateX(0); }
  .drawer .dhead { color:#fff; padding:18px 22px; }
  .drawer .dhead .x { float:right; background:rgba(255,255,255,.2); border:0; color:#fff; width:30px; height:30px;
    border-radius:8px; cursor:pointer; font-size:16px; }
  .drawer .dhead h2 { margin:0; font-size:19px; }
  .drawer .dhead .sub { font-size:13px; opacity:.9; margin-top:3px; }
  .tabs { display:flex; gap:4px; padding:0 16px; background:#fff; border-bottom:1px solid var(--line); }
  .tabs button { background:none; border:0; padding:13px 14px; font-size:14px; font-weight:600; color:var(--muted);
    cursor:pointer; border-bottom:2px solid transparent; }
  .tabs button.active { color:var(--brand); border-bottom-color:var(--brand); }
  .dbody { flex:1; overflow-y:auto; padding:20px 22px; }
  .panel { display:none; } .panel.active { display:block; }
  .kv { display:flex; justify-content:space-between; padding:9px 0; border-bottom:1px solid var(--line); font-size:14px; }
  .kv .k { color:var(--muted); }
  .gridchips { display:grid; grid-template-columns:repeat(2,1fr); gap:10px; margin-top:6px; }
  .chipcard { border:1px solid var(--line); border-radius:10px; padding:10px 12px; background:#fff; cursor:pointer; }
  .chipcard:hover{ border-color:var(--brand); }
  .chipcard .p { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.3px; font-weight:600; }
  .chipcard .v { font-size:20px; font-weight:700; margin-top:2px; }
  .doc { display:flex; align-items:center; gap:10px; padding:10px 0; border-bottom:1px solid var(--line); }
  .doc .name { font-weight:600; font-size:14px; }
  .doc .tag { font-size:11px; padding:2px 7px; border-radius:12px; background:#eef2f0; color:var(--muted); font-weight:600; }
  .doc .tag.library{ background:#dff0e6; color:var(--ok); }
  .thread { display:flex; flex-direction:column; gap:10px; margin-bottom:14px; }
  .msg { max-width:80%; padding:9px 13px; border-radius:12px; font-size:14px; }
  .msg.gc_admin { align-self:flex-end; background:var(--brand); color:#fff; border-bottom-right-radius:4px; }
  .msg.sub { align-self:flex-start; background:#fff; border:1px solid var(--line); border-bottom-left-radius:4px; }
  .msg .t { font-size:11px; opacity:.7; margin-top:3px; }
  .composer { display:flex; gap:8px; align-items:flex-end; }
  .composer textarea { flex:1; }
  .dfoot { padding:14px 22px; border-top:1px solid var(--line); background:#fff; }

  dialog { border:0; border-radius:12px; padding:22px; max-width:420px; box-shadow:0 10px 40px rgba(0,0,0,.25); }
  dialog h3 { margin:0 0 10px; font-size:16px; }
  dialog .actions { display:flex; gap:10px; justify-content:flex-end; margin-top:16px; }
  dialog button { padding:8px 14px; border-radius:8px; border:1px solid var(--line); background:#fff; cursor:pointer; font-size:14px; }
  dialog button.danger { background:var(--red); color:#fff; border:0; font-weight:600; }
</style>
</head>
<body>
<header id="hdr" style="background:#1E7A46">
  <div class="brand">
    <div class="logo" id="logo">GC</div>
    <h1 id="gc-name">GC Workspace</h1>
  </div>
  <div class="r">
    <span id="owner-badge" class="badge hidden">Owner view</span>
    <a id="back" class="hidden" href="/platform">← Owner console</a>
    <button id="signout" class="hidden" onclick="signout()">Sign out</button>
  </div>
</header>

<main id="main" class="hidden">
  <div class="tiles" id="tiles"></div>

  <div class="card">
    <h2>Add a subcontractor</h2>
    <div class="row">
      <div style="flex:2"><label>Company name</label><input id="s-name" type="text" placeholder="e.g. Lone Star Electric"></div>
      <div style="flex:2"><label>Scope of work (comma-separated)</label><input id="s-scope" type="text" placeholder="electrical, loto"></div>
      <div><label>Contact email</label><input id="s-email" type="email" placeholder="office@sub.com"></div>
      <div style="flex:0 0 auto"><button class="primary" onclick="addSub()">Add</button></div>
    </div>
    <div id="add-msg" class="note"></div>
  </div>

  <div class="card">
    <h2>Subcontractor roster <span class="hint">Click a row to manage grades, documents & messages</span></h2>
    <div id="roster"><div class="empty">Loading…</div></div>
  </div>
</main>

<div class="scrim" id="scrim" onclick="closeDrawer()"></div>
<aside class="drawer" id="drawer" aria-hidden="true">
  <div class="dhead" id="dhead" style="background:#1E7A46">
    <button class="x" onclick="closeDrawer()">×</button>
    <h2 id="d-name">—</h2>
    <div class="sub" id="d-scope"></div>
  </div>
  <div class="tabs">
    <button id="tab-overview" class="active" onclick="showTab('overview')">Overview</button>
    <button id="tab-docs" onclick="showTab('docs')">Documents</button>
    <button id="tab-messages" onclick="showTab('messages')">Messages</button>
  </div>
  <div class="dbody">
    <div class="panel active" id="panel-overview"></div>
    <div class="panel" id="panel-docs"></div>
    <div class="panel" id="panel-messages"></div>
  </div>
  <div class="dfoot">
    <button class="link" onclick="askDelete()">Remove this subcontractor & revoke their login</button>
  </div>
</aside>

<dialog id="dlg-del">
  <h3>Remove subcontractor</h3>
  <div id="del-text"></div>
  <div class="note">This deletes the subcontractor, all of their records, and any login they have — so they lose access. This cannot be undone.</div>
  <div class="actions">
    <button onclick="document.getElementById('dlg-del').close()">Cancel</button>
    <button class="danger" id="del-confirm">Remove</button>
  </div>
</dialog>

<script>
const PARAMS = new URLSearchParams(location.search);
const GC = PARAMS.get('gc') || '';
let PLATFORMS = ['isn','avetta','veriforce','pec'];
let STATE = null, CUR = null, BRAND = '#1E7A46';
function q(id){ return document.getElementById(id); }
function esc(s){ return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function withGc(p){ return GC ? (p+(p.includes('?')?'&':'?')+'gc='+encodeURIComponent(GC)) : p; }
async function api(path, opts){
  const r = await fetch(path, Object.assign({headers:{'Content-Type':'application/json'}}, opts||{}));
  let d={}; try{ d=await r.json(); }catch(e){}
  return {ok:r.ok, status:r.status, data:d};
}

async function boot(){
  const me = (await api('/platform/me')).data;
  if(!me.authenticated){ location.href='/platform'; return; }
  const {ok, data, status} = await api(withGc('/platform/gc/roster'));
  if(status===400 && me.role==='owner'){ q('main').innerHTML='<div class="card err">Open a workspace from the owner console.</div>'; q('main').classList.remove('hidden'); return; }
  if(!ok){ q('gc-name').textContent='Workspace'; q('main').innerHTML='<div class="card err">'+esc(data.error||'Could not load')+'</div>'; q('main').classList.remove('hidden'); return; }
  render(data);
}

function render(data){
  STATE = data;
  PLATFORMS = data.platforms || PLATFORMS;
  const gc = data.gc;
  BRAND = gc.brand_primary || '#1E7A46';
  q('hdr').style.background = BRAND;
  q('dhead').style.background = BRAND;
  document.documentElement.style.setProperty('--brand', BRAND);
  q('logo').textContent = (gc.name||'GC').trim().slice(0,2).toUpperCase();
  q('gc-name').textContent = gc.name + ' — Workspace';
  if(data.is_owner){ q('owner-badge').classList.remove('hidden'); q('back').classList.remove('hidden'); }
  else { q('signout').classList.remove('hidden'); }
  q('main').classList.remove('hidden');
  drawTiles(); drawRoster();
}

function drawTiles(){
  const subs = STATE.subs||[];
  let green=0, amber=0, red=0, coiSoon=0;
  subs.forEach(s=>{ if(s.health==='green')green++; else if(s.health==='amber')amber++; else if(s.health==='red')red++;
    if(s.coi && s.coi.days_left!==null && s.coi.days_left<=30) coiSoon++; });
  const tiles = [
    {n:subs.length, l:'Subcontractors', c:''},
    {n:green, l:'Green', c:'green'},
    {n:amber, l:'Watch', c:'amber'},
    {n:red, l:'At risk', c:'red'},
    {n:coiSoon, l:'COI ≤30d', c:'warn'},
  ];
  q('tiles').innerHTML = tiles.map(t=>`<div class="tile ${t.c}"><div class="n">${t.n}</div><div class="l">${t.l}</div></div>`).join('');
}

function coiCell(coi){
  if(!coi || coi.days_left===null) return '<span class="scope">—</span>';
  const d = coi.days_left;
  const cls = d<0?'coi-exp':(d<=30?'coi-soon':'coi-ok');
  const txt = d<0?('expired '+(-d)+'d ago'):(d+'d left');
  return '<span class="'+cls+'">'+txt+'</span>';
}
function gradeCells(sub){
  return PLATFORMS.map(p=>{
    const g = (sub.grades[p]||'').toUpperCase();
    const cls = g?('grade '+g.toLowerCase()):'grade none';
    return '<span class="'+cls+'" title="'+p.toUpperCase()+'">'+(g||'·')+'</span>';
  }).join('');
}
function drawRoster(){
  const el = q('roster');
  const subs = STATE.subs||[];
  if(!subs.length){ el.innerHTML='<div class="empty">No subcontractors yet. Add your first one above.</div>'; return; }
  el.innerHTML = `<table><thead><tr>
      <th>Subcontractor</th><th>Prequal grades</th><th>COI</th><th>TRIR</th><th></th>
    </tr></thead><tbody>`+
    subs.map(s=>`<tr onclick="openDrawer('${s.id}')">
      <td><span class="dot ${esc(s.health)}"></span><b>${esc(s.name)}</b>
        <div class="scope">${(s.scope||[]).map(esc).join(', ')}</div></td>
      <td>${gradeCells(s)}</td>
      <td>${coiCell(s.coi)}</td>
      <td>${s.trir!=null?esc(String(s.trir)):'—'}</td>
      <td style="text-align:right"><button class="open-btn" onclick="event.stopPropagation();openDrawer('${s.id}')">Open</button></td>
    </tr>`).join('')+`</tbody></table>`;
}

async function addSub(){
  const name=q('s-name').value.trim(), scope=q('s-scope').value, email=q('s-email').value.trim();
  const msg=q('add-msg');
  if(!name){ msg.innerHTML='<span class="err">Enter a company name.</span>'; return; }
  msg.textContent='Adding…';
  const {ok,data}=await api('/platform/gc/subs',{method:'POST',body:JSON.stringify({gc:GC,name,scope_of_work:scope,contact_email:email})});
  if(!ok){ msg.innerHTML='<span class="err">'+esc(data.error||'Could not add')+'</span>'; return; }
  msg.innerHTML='<span class="ok">Added '+esc(data.sub.name)+'.</span>';
  q('s-name').value='';q('s-scope').value='';q('s-email').value='';
  reloadRoster();
}

// ── drawer ──
let TAB='overview';
function showTab(t){ TAB=t; ['overview','docs','messages'].forEach(x=>{
  q('tab-'+x).classList.toggle('active', x===t); q('panel-'+x).classList.toggle('active', x===t); }); }
async function openDrawer(id){
  CUR = null;
  q('scrim').classList.add('show'); q('drawer').classList.add('show'); q('drawer').setAttribute('aria-hidden','false');
  q('d-name').textContent='Loading…'; q('d-scope').textContent='';
  q('panel-overview').innerHTML=''; q('panel-docs').innerHTML=''; q('panel-messages').innerHTML='';
  showTab('overview');
  const {ok,data}=await api(withGc('/platform/gc/subs/'+id));
  if(!ok){ q('d-name').textContent='Not found'; return; }
  CUR = data;
  drawDrawer();
}
function closeDrawer(){ q('scrim').classList.remove('show'); q('drawer').classList.remove('show'); q('drawer').setAttribute('aria-hidden','true'); CUR=null; }

function drawDrawer(){
  const s = CUR.sub;
  q('d-name').textContent = s.name;
  q('d-scope').textContent = (s.scope||[]).join(' · ') || 'No scope set';
  // overview
  const grades = PLATFORMS.map(p=>{
    const g=(s.grades[p]||'').toUpperCase();
    return `<div class="chipcard" onclick="editGrade('${p}')">
      <div class="p">${p.toUpperCase()}</div><div class="v">${g||'<span style=\"color:#bbb\">Set</span>'}</div></div>`;
  }).join('');
  const coi = s.coi;
  const coiLine = coi ? `${esc(coi.carrier||'—')} · ${esc(coi.coverage||'')} · ${coiCell(coi)}` : 'No COI on file';
  q('panel-overview').innerHTML = `
    <div class="kv"><span class="k">Health</span><span><span class="dot ${esc(s.health)}"></span>${esc(s.health)}</span></div>
    <div class="kv"><span class="k">Contact</span><span>${esc(s.contact_email||'—')}</span></div>
    <div class="kv"><span class="k">TRIR</span><span>${s.trir!=null?esc(String(s.trir)):'—'}</span></div>
    <div class="kv"><span class="k">EMR</span><span>${s.emr!=null?esc(String(s.emr)):'—'}</span></div>
    <div class="kv"><span class="k">COI</span><span>${coiLine}</span></div>
    <h3 style="font-size:13px;margin:18px 0 6px;color:var(--muted)">PREQUAL GRADES <span style="font-weight:400">— click to set</span></h3>
    <div class="gridchips">${grades}</div>`;
  // docs
  drawDocs();
  // messages
  drawMessages();
}

function drawDocs(){
  const docs = CUR.sub.docs||[];
  const lib = CUR.library||[];
  const list = docs.length ? docs.map(d=>`<div class="doc">
      <span class="name">${esc(d.name)}</span>
      <span class="tag ${d.source==='library'?'library':''}">${d.source==='library'?'Library':(esc(d.category)||'Doc')}</span>
      <span style="margin-left:auto"><button class="link" onclick="delDoc('${d.id}')">Remove</button></span>
    </div>`).join('') : '<div class="empty">No documents yet.</div>';
  const libOpts = lib.length
    ? `<label>Send a program from your library</label>
       <div class="row"><div><select id="lib-sel"><option value="">Choose a program…</option>`+
       lib.map(p=>`<option value="${p.id}">${esc(p.title)}</option>`).join('')+
       `</select></div><div style="flex:0 0 auto"><button class="primary" onclick="sendLib()">Send</button></div></div>`
    : `<div class="note">Your document library is empty. Add a custom document below, or stock the library from the owner console later.</div>`;
  q('panel-docs').innerHTML = `
    <div>${list}</div>
    <div style="margin-top:18px">${libOpts}</div>
    <div style="margin-top:16px"><label>Add a custom document</label>
      <div class="row">
        <div style="flex:2"><input id="doc-name" type="text" placeholder="e.g. Safety Manual 2026"></div>
        <div><input id="doc-cat" type="text" placeholder="Category (optional)"></div>
        <div style="flex:0 0 auto"><button class="primary" onclick="addDoc()">Add</button></div>
      </div>
      <div id="doc-msg" class="note"></div>
    </div>`;
}

function drawMessages(){
  const msgs = CUR.sub.messages||[];
  const thread = msgs.length ? msgs.map(m=>`<div class="msg ${esc(m.role)}">${esc(m.body)}
      <div class="t">${m.role==='gc_admin'?'You':'Subcontractor'} · ${fmt(m.created_at)}</div></div>`).join('')
    : '<div class="empty">No messages yet. Start the conversation below.</div>';
  q('panel-messages').innerHTML = `
    <div class="thread" id="thread">${thread}</div>
    <div class="composer">
      <textarea id="msg-body" placeholder="Message ${esc(CUR.sub.name)}…"></textarea>
      <button class="primary" onclick="sendMsg()">Send</button>
    </div>
    <div class="note">The subcontractor sees these in their own portal (coming soon). You can post now.</div>`;
  const th=q('thread'); if(th) th.scrollTop=th.scrollHeight;
}
function fmt(iso){ if(!iso) return ''; const d=new Date(iso); return d.toLocaleDateString()+' '+d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}); }

async function editGrade(platform){
  const cur = (CUR.sub.grades[platform]||'');
  const g = prompt('Grade for '+platform.toUpperCase()+' (A, B, C, D, F — blank to clear):', cur);
  if(g===null) return;
  const {ok,data}=await api('/platform/gc/subs/'+CUR.sub.id+'/grade',{method:'POST',
    body:JSON.stringify({gc:GC,platform,grade:g.trim()})});
  if(!ok){ alert(data.error||'Could not save grade'); return; }
  await refreshCur(); reloadRoster();
}
async function addDoc(){
  const name=q('doc-name').value.trim(), cat=q('doc-cat').value.trim();
  if(!name){ q('doc-msg').innerHTML='<span class="err">Enter a document name.</span>'; return; }
  const {ok,data}=await api('/platform/gc/subs/'+CUR.sub.id+'/docs',{method:'POST',
    body:JSON.stringify({gc:GC,name,category:cat})});
  if(!ok){ q('doc-msg').innerHTML='<span class="err">'+esc(data.error||'Could not add')+'</span>'; return; }
  await refreshCur();
}
async function sendLib(){
  const id=q('lib-sel').value; if(!id) return;
  const {ok,data}=await api('/platform/gc/subs/'+CUR.sub.id+'/docs',{method:'POST',
    body:JSON.stringify({gc:GC,library_id:id})});
  if(!ok){ alert(data.error||'Could not send'); return; }
  await refreshCur();
}
async function delDoc(docId){
  const {ok,data}=await api(withGc('/platform/gc/subs/'+CUR.sub.id+'/docs/'+docId),{method:'DELETE'});
  if(!ok){ alert(data.error||'Could not remove'); return; }
  await refreshCur();
}
async function sendMsg(){
  const body=q('msg-body').value.trim(); if(!body) return;
  const {ok,data}=await api('/platform/gc/subs/'+CUR.sub.id+'/messages',{method:'POST',
    body:JSON.stringify({gc:GC,body})});
  if(!ok){ alert(data.error||'Could not send'); return; }
  await refreshCur();
}
async function refreshCur(){
  if(!CUR) return;
  const {ok,data}=await api(withGc('/platform/gc/subs/'+CUR.sub.id));
  if(ok){ CUR=data; const keep=TAB; drawDrawer(); showTab(keep); }
}

let PENDING_DEL=null;
function askDelete(){
  if(!CUR) return;
  PENDING_DEL=CUR.sub.id;
  q('del-text').innerHTML='Remove <b>'+esc(CUR.sub.name)+'</b> from your roster?';
  q('dlg-del').showModal();
}
q('del-confirm').onclick = async ()=>{
  if(!PENDING_DEL) return;
  const {ok,data}=await api(withGc('/platform/gc/subs/'+PENDING_DEL),{method:'DELETE'});
  q('dlg-del').close();
  if(!ok){ alert(data.error||'Could not remove'); return; }
  PENDING_DEL=null; closeDrawer(); reloadRoster();
};

async function reloadRoster(){ const {ok,data}=await api(withGc('/platform/gc/roster')); if(ok){ STATE=data; drawTiles(); drawRoster(); } }
async function signout(){ await api('/platform/logout',{method:'POST'}); location.href='/platform'; }
boot();
</script>
</body>
</html>
"""
