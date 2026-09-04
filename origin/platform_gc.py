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
    Document, Message, GcMessage, LibraryProgram,
    ROLE_OWNER, ROLE_GC_ADMIN, ROLE_SUB, PLATFORMS,
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
                # does this sub have a login yet?
                has_login = s.scalar(select(func.count(User.id)).where(
                    User.sub_id == sub.id, User.role == ROLE_SUB)) or 0
                out.append({
                    "id": sub.id, "name": sub.name, "logo_url": sub.logo_url,
                    "scope": sub.scope_of_work or [],
                    "contact_name": sub.contact_name, "contact_email": sub.contact_email,
                    "trir": sub.trir, "emr": sub.emr, "health": sub.health,
                    "grades": gmap, "coi": coi, "has_login": bool(has_login),
                })
            gc_info = {"id": t.id, "name": t.name, "slug": t.slug,
                       "logo_url": t.logo_url,
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
            has_login = s.scalar(select(func.count(User.id)).where(
                User.sub_id == sub_id, User.role == ROLE_SUB)) or 0
            login_email = None
            if has_login:
                lu = s.scalar(select(User).where(
                    User.sub_id == sub_id, User.role == ROLE_SUB))
                login_email = lu.email if lu else None
            out = {
                "id": sub.id, "name": sub.name, "logo_url": sub.logo_url,
                "scope": sub.scope_of_work or [],
                "contact_name": sub.contact_name, "contact_email": sub.contact_email,
                "trir": sub.trir, "emr": sub.emr, "health": sub.health,
                "grades": grades, "coi": coi, "docs": docs, "messages": msgs,
                "has_login": bool(has_login), "login_email": login_email,
            }
        return {"sub": out, "platforms": list(PLATFORMS), "library": library,
                "is_owner": claims.get("role") == ROLE_OWNER}

    # ── issue / reset a subcontractor's login (so they can sign in) ───────
    @app.post("/platform/gc/subs/{sub_id}/login")
    def issue_sub_login(sub_id: str, request: Request, body: dict = Body(...)):
        gc_id, claims = _gc_context(request, body.get("gc"))
        if not claims or claims.get("role") not in (ROLE_OWNER, ROLE_GC_ADMIN):
            return _deny()
        email = (body.get("email") or "").strip().lower()
        password = body.get("password") or ""
        if not email or not password:
            return JSONResponse({"error": "email and password are required"},
                                status_code=400)
        if len(password) < 8:
            return JSONResponse({"error": "password must be at least 8 characters"},
                                status_code=400)
        with db.session() as s:
            sub = s.get(Subcontractor, sub_id)
            if not sub or sub.gc_id != gc_id:
                return JSONResponse({"error": "subcontractor not found"},
                                    status_code=404)
            # an existing sub login for THIS sub is reset; a collision with any
            # other account's email is refused.
            existing = s.scalar(select(User).where(User.email == email))
            if existing and not (existing.role == ROLE_SUB
                                 and existing.sub_id == sub_id):
                return JSONResponse({"error": "that email already has a login"},
                                    status_code=400)
            cur = s.scalar(select(User).where(
                User.sub_id == sub_id, User.role == ROLE_SUB))
            if cur:
                cur.email = email
                cur.password_hash = auth.hash_password(password)
                cur.active = True
            else:
                s.add(User(email=email, password_hash=auth.hash_password(password),
                           role=ROLE_SUB, gc_id=gc_id, sub_id=sub_id,
                           name=sub.name))
            s.commit()
        return {"ok": True, "login": {"email": email, "sub": sub.name}}

    # ── owner ↔ GC message thread (no sub involved) ──────────────────────
    @app.get("/platform/gc-messages")
    def gc_messages(request: Request, gc: str = ""):
        gc_id, claims = _gc_context(request, gc)
        if not claims or claims.get("role") not in (ROLE_OWNER, ROLE_GC_ADMIN):
            return _deny()
        if not gc_id:
            return JSONResponse({"error": "no GC selected"}, status_code=400)
        role = claims.get("role")
        with db.session() as s:
            if not s.get(Tenant, gc_id):
                return JSONResponse({"error": "GC not found"}, status_code=404)
            rows = s.scalars(select(GcMessage).where(
                GcMessage.gc_id == gc_id).order_by(GcMessage.created_at)).all()
            out = [{"id": m.id, "role": m.sender_role, "body": m.body,
                    "created_at": m.created_at.isoformat() if m.created_at else None}
                   for m in rows]
            # opening the thread marks the other side's messages as read by me
            for m in rows:
                if role == ROLE_OWNER and not m.read_by_owner:
                    m.read_by_owner = True
                elif role == ROLE_GC_ADMIN and not m.read_by_gc:
                    m.read_by_gc = True
            s.commit()
        return {"messages": out, "me": role}

    @app.post("/platform/gc-messages")
    def gc_post_message(request: Request, body: dict = Body(...)):
        gc_id, claims = _gc_context(request, body.get("gc"))
        if not claims or claims.get("role") not in (ROLE_OWNER, ROLE_GC_ADMIN):
            return _deny()
        if not gc_id:
            return JSONResponse({"error": "no GC selected"}, status_code=400)
        text = (body.get("body") or "").strip()
        if not text:
            return JSONResponse({"error": "message is empty"}, status_code=400)
        role = claims.get("role")
        with db.session() as s:
            if not s.get(Tenant, gc_id):
                return JSONResponse({"error": "GC not found"}, status_code=404)
            m = GcMessage(gc_id=gc_id, sender_user_id=claims.get("uid"),
                          sender_role=role, body=text,
                          read_by_owner=(role == ROLE_OWNER),
                          read_by_gc=(role == ROLE_GC_ADMIN))
            s.add(m)
            s.commit()
            new = {"id": m.id, "role": m.sender_role, "body": m.body,
                   "created_at": m.created_at.isoformat() if m.created_at else None}
        return {"ok": True, "message": new}

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
  :root{
    --brand:#1E7A46; --bg:#f4f6f5; --panel:#ffffff; --card:#ffffff; --ink:#12211a;
    --muted:#5b6b63; --line:#e6ebe8; --chip:#eef2f0; --red:#c0392b; --amber:#d99200;
    --ok:#1E7A46; --sel:#eef5f0; --msg-in:#ffffff; --hover:#f7faf8;
  }
  html[data-theme="dark"]{
    --bg:#0e1512; --panel:#121b16; --card:#16201b; --ink:#e8efea; --muted:#9fb0a7;
    --line:#25332c; --chip:#1e2a24; --sel:#1b2822; --msg-in:#1e2a24; --hover:#1a241e;
    --red:#e06a5c; --amber:#e6b04a; --ok:#4ecb7e;
  }
  *{box-sizing:border-box}
  body{margin:0;height:100vh;overflow:hidden;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg)}
  header{color:#fff;padding:14px 22px;display:flex;align-items:center;justify-content:space-between;background:var(--brand)}
  header .brand{display:flex;align-items:center;gap:12px}
  header .logo{width:40px;height:40px;border-radius:10px;background:rgba(255,255,255,.2);overflow:hidden;cursor:pointer;
    display:flex;align-items:center;justify-content:center;font-weight:800;font-size:16px;flex:0 0 auto}
  header .logo img{width:100%;height:100%;object-fit:cover}
  header h1{margin:0;font-size:18px;font-weight:700}
  header h1 .tag{font-weight:400;font-size:14px;opacity:.9;margin-left:6px}
  header .r{display:flex;gap:10px;align-items:center;font-size:13px}
  header .who{opacity:.92;display:flex;gap:8px;align-items:center}
  header .dot-on{width:8px;height:8px;border-radius:50%;background:#7ee2a6;display:inline-block}
  header button,header a{color:#fff;text-decoration:none;background:rgba(255,255,255,.16);border:0;padding:8px 13px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600}
  header button:hover,header a:hover{background:rgba(255,255,255,.3)}
  header .badge{background:rgba(255,255,255,.18);padding:5px 10px;border-radius:20px;font-weight:600}

  .toolbar{display:flex;align-items:center;gap:10px;padding:12px 22px;border-bottom:1px solid var(--line);background:var(--panel)}
  .btn{background:var(--brand);color:#fff;border:0;padding:9px 15px;border-radius:9px;font-size:14px;font-weight:600;cursor:pointer}
  .btn:hover{filter:brightness(1.07)}
  .btn.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
  .btn.ghost:hover{background:var(--hover)}
  .btn.msg{margin-left:auto;position:relative}
  .btn .pip{background:#fff;color:var(--brand);border-radius:20px;font-size:11px;font-weight:800;padding:1px 7px;margin-left:6px}

  .cols{display:flex;height:calc(100vh - 60px - 57px)}
  .left{width:320px;flex:0 0 320px;border-right:1px solid var(--line);background:var(--panel);overflow-y:auto;padding:14px}
  .left h3{font-size:12px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted);margin:2px 6px 12px}
  .subitem{border:1px solid transparent;border-radius:11px;padding:12px 14px;cursor:pointer;margin-bottom:4px}
  .subitem:hover{background:var(--hover)}
  .subitem.sel{background:var(--sel);border-color:var(--line)}
  .subitem .row1{display:flex;align-items:center;gap:9px}
  .subitem .slogo{width:26px;height:26px;border-radius:7px;background:var(--chip);overflow:hidden;flex:0 0 auto;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:var(--muted)}
  .subitem .slogo img{width:100%;height:100%;object-fit:cover}
  .subitem .nm{font-weight:600}
  .subitem .st{font-size:12.5px;color:var(--muted);margin-top:4px}
  .subitem .st.red{color:var(--red);font-weight:600}
  .subitem .st.amber{color:var(--amber);font-weight:600}
  .hdot{width:9px;height:9px;border-radius:50%;margin-left:auto;flex:0 0 auto}
  .hdot.green{background:var(--ok)}.hdot.amber{background:var(--amber)}.hdot.red{background:var(--red)}

  .right{flex:1;overflow-y:auto;padding:24px 26px}
  .empty-right{color:var(--muted);padding:40px;text-align:center}
  .dhead{display:flex;align-items:flex-start;gap:14px}
  .dhead .dlogo{width:52px;height:52px;border-radius:12px;background:var(--chip);overflow:hidden;cursor:pointer;flex:0 0 auto;display:flex;align-items:center;justify-content:center;font-weight:800;color:var(--muted)}
  .dhead .dlogo img{width:100%;height:100%;object-fit:cover}
  .dhead h2{margin:0;font-size:24px}
  .dhead .scope{color:var(--muted);font-size:13px;margin-top:2px}
  .dhead .spacer{flex:1}
  .pill-btn{background:var(--chip);color:var(--ink);border:1px solid var(--line);padding:9px 13px;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer}
  .status-flag{padding:9px 13px;border-radius:10px;font-size:13px;font-weight:700}
  .status-flag.red{background:rgba(192,57,43,.14);color:var(--red)}
  .status-flag.amber{background:rgba(217,146,0,.16);color:var(--amber)}
  .status-flag.green{background:rgba(30,122,70,.14);color:var(--ok)}

  .metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:20px 0 8px}
  .metric{border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:var(--card);cursor:pointer}
  .metric.ro{cursor:default}
  .metric .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.3px;font-weight:600}
  .metric .v{font-size:22px;font-weight:800;margin-top:4px}
  .metric .v.f,.metric .v.d{color:var(--red)} .metric .v.c{color:var(--amber)} .metric .v.a,.metric .v.b{color:var(--ok)}
  .metric .v.set{font-size:13px;color:var(--muted);font-weight:600}
  @media(max-width:900px){.metrics{grid-template-columns:repeat(2,1fr)}}

  .section{margin-top:24px}
  .section .lbl{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;font-weight:600;margin-bottom:10px}
  .docrow{display:flex;align-items:center;gap:12px;border:1px solid var(--line);border-radius:11px;padding:13px 15px;margin-bottom:9px;background:var(--card)}
  .docrow .nm{font-weight:600}
  .docrow .tag{font-size:11px;padding:2px 8px;border-radius:12px;background:var(--chip);color:var(--muted);font-weight:600}
  .docrow .tag.library{background:rgba(30,122,70,.16);color:var(--ok)}
  .docrow .act{margin-left:auto;display:flex;gap:8px;align-items:center}
  .mini{background:var(--brand);color:#fff;border:0;padding:7px 12px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer}
  .mini.g{background:transparent;color:var(--red);border:1px solid var(--line)}
  .addrow{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-top:6px}
  .addrow>div{flex:1;min-width:140px}

  label{display:block;font-size:12px;color:var(--muted);margin:0 0 4px;font-weight:600}
  input[type=text],input[type=email],input[type=password],select,textarea{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:9px;font-size:14px;font-family:inherit;background:var(--card);color:var(--ink)}
  textarea{resize:vertical;min-height:44px}

  .thread{display:flex;flex-direction:column;gap:10px;margin:8px 0 12px;max-height:340px;overflow-y:auto;padding:4px}
  .msg{max-width:78%;padding:9px 13px;border-radius:13px;font-size:14px}
  .msg.gc_admin{align-self:flex-end;background:var(--brand);color:#fff;border-bottom-right-radius:4px}
  .msg.sub,.msg.owner{align-self:flex-start;background:var(--msg-in);border:1px solid var(--line);border-bottom-left-radius:4px}
  .msg .t{font-size:11px;opacity:.7;margin-top:3px}
  .composer{display:flex;gap:8px;align-items:flex-end}
  .composer textarea{flex:1}
  .hint{font-size:12px;color:var(--muted);margin-top:6px}
  .card{border:1px solid var(--line);border-radius:12px;padding:16px;background:var(--card);margin-top:12px}
  .err{color:var(--red);font-weight:600}.ok{color:var(--ok);font-weight:600}
  .hidden{display:none!important}

  .scrim{position:fixed;inset:0;background:rgba(8,16,12,.45);opacity:0;pointer-events:none;transition:.2s;z-index:40}
  .scrim.show{opacity:1;pointer-events:auto}
  .drawer{position:fixed;top:0;right:0;height:100vh;width:min(460px,94vw);background:var(--panel);box-shadow:-8px 0 40px rgba(0,0,0,.2);
    transform:translateX(100%);transition:transform .22s ease;z-index:50;display:flex;flex-direction:column}
  .drawer.show{transform:translateX(0)}
  .drawer .dh{color:#fff;padding:16px 20px;background:var(--brand);display:flex;justify-content:space-between;align-items:center}
  .drawer .dh h3{margin:0;font-size:16px}
  .drawer .dh button{background:rgba(255,255,255,.2);border:0;color:#fff;width:30px;height:30px;border-radius:8px;cursor:pointer;font-size:16px}
  .drawer .db{flex:1;overflow-y:auto;padding:18px 20px}
  .drawer .df{padding:14px 20px;border-top:1px solid var(--line)}

  dialog{border:0;border-radius:14px;padding:22px;max-width:440px;box-shadow:0 10px 40px rgba(0,0,0,.25);background:var(--panel);color:var(--ink)}
  dialog h3{margin:0 0 12px;font-size:17px}
  dialog .stack>*+*{margin-top:12px}
  dialog .actions{display:flex;gap:10px;justify-content:flex-end;margin-top:16px}
  dialog .actions button{padding:9px 15px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;font-size:14px}
  dialog .actions button.pri{background:var(--brand);color:#fff;border:0;font-weight:600}
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="logo" id="gc-logo" onclick="maybeUploadGcLogo()" title="Upload GC logo"></div>
    <h1 id="gc-title">Workspace<span class="tag">· contractor monitoring</span></h1>
  </div>
  <div class="r">
    <span class="who" id="who"></span>
    <button onclick="toggleTheme()" id="theme-btn" title="Light / dark">🌙</button>
    <span id="owner-badge" class="badge hidden">Owner view</span>
    <a id="back" class="hidden" href="/platform">← Owner console</a>
    <button id="signout" class="hidden" onclick="signout()">Sign out</button>
  </div>
</header>

<div id="main" class="hidden">
  <div class="toolbar">
    <button class="btn" onclick="openAddSub()">+ Add subcontractor</button>
    <button class="btn ghost" onclick="openIssueLogin()" id="issue-btn">Issue login</button>
    <button class="btn msg" onclick="openOwnerThread()" id="owner-msg-btn">Messages<span class="pip hidden" id="owner-msg-pip">0</span></button>
  </div>

  <div class="cols">
    <div class="left">
      <h3>Your subcontractors</h3>
      <div id="sublist"><div class="hint" style="padding:8px 6px">Loading…</div></div>
    </div>
    <div class="right" id="detail">
      <div class="empty-right">Select a subcontractor to view their compliance, documents, and messages.</div>
    </div>
  </div>
</div>

<input id="gc-logo-file" type="file" accept="image/*" class="hidden" onchange="doUploadLogo('gc', GC, event)">
<input id="sub-logo-file" type="file" accept="image/*" class="hidden" onchange="doUploadLogo('sub', CUR&&CUR.id, event)">

<!-- owner<->GC message drawer -->
<div class="scrim" id="scrim" onclick="closeDrawer()"></div>
<aside class="drawer" id="drawer">
  <div class="dh"><h3 id="drawer-title">Messages</h3><button onclick="closeDrawer()">×</button></div>
  <div class="db"><div class="thread" id="owner-thread" style="max-height:none"></div></div>
  <div class="df">
    <div class="composer">
      <textarea id="owner-msg-box" placeholder="Write a message…"></textarea>
      <button class="mini" onclick="sendOwnerMsg()">Send</button>
    </div>
    <div class="hint" id="owner-thread-hint"></div>
  </div>
</aside>

<!-- add subcontractor dialog -->
<dialog id="dlg-add">
  <h3>Add a subcontractor</h3>
  <div class="stack">
    <div><label>Company name</label><input id="as-name" type="text" placeholder="e.g. Rio Grande Welding"></div>
    <div><label>Scope of work (comma-separated)</label><input id="as-scope" type="text" placeholder="welding, hot work"></div>
    <div><label>Contact email</label><input id="as-email" type="email" placeholder="office@sub.com"></div>
    <div id="as-msg" class="hint"></div>
  </div>
  <div class="actions">
    <button onclick="document.getElementById('dlg-add').close()">Cancel</button>
    <button class="pri" onclick="submitAddSub()">Add subcontractor</button>
  </div>
</dialog>

<!-- issue sub login dialog -->
<dialog id="dlg-login">
  <h3 id="login-title">Issue a login</h3>
  <div class="stack">
    <div class="hint" id="login-sub"></div>
    <div><label>Sub's login email</label><input id="lg-email" type="email" placeholder="office@sub.com"></div>
    <div><label>Temporary password (min 8)</label><input id="lg-pass" type="password" placeholder="••••••••"></div>
    <div id="lg-msg" class="hint"></div>
  </div>
  <div class="actions">
    <button onclick="document.getElementById('dlg-login').close()">Cancel</button>
    <button class="pri" onclick="submitIssueLogin()">Create login</button>
  </div>
</dialog>

<script>
const PARAMS=new URLSearchParams(location.search);
const GC=PARAMS.get('gc')||'';
let PLATFORMS=['isn','avetta','veriforce','pec'];
const PLABEL={isn:'ISNetworld',avetta:'Avetta',veriforce:'Veriforce',pec:'PEC'};
let STATE=null, CUR=null, BRAND='#1E7A46', ROLE=null, IS_OWNER=false;
function q(id){return document.getElementById(id)}
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function withGc(p){return GC?(p+(p.includes('?')?'&':'?')+'gc='+encodeURIComponent(GC)):p}
function fmtTime(iso){if(!iso)return'';try{return new Date(iso).toLocaleString([],{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}catch(e){return''}}
async function api(path,opts){
  const r=await fetch(path,Object.assign({headers:{'Content-Type':'application/json'}},opts||{}));
  let d={};try{d=await r.json()}catch(e){}
  return {ok:r.ok,status:r.status,data:d};
}
function initTheme(){const t=localStorage.getItem('origin-theme')||'light';document.documentElement.setAttribute('data-theme',t);const b=q('theme-btn');if(b)b.textContent=(t==='dark'?'☀️':'🌙')}
function toggleTheme(){const c=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';document.documentElement.setAttribute('data-theme',c);localStorage.setItem('origin-theme',c);q('theme-btn').textContent=(c==='dark'?'☀️':'🌙')}

async function boot(){
  const me=(await api('/platform/me')).data;
  if(!me.authenticated){location.href='/platform';return;}
  ROLE=me.role;
  const {ok,data,status}=await api(withGc('/platform/gc/roster'));
  if(status===400 && me.role==='owner'){ q('detail').innerHTML='<div class="empty-right">Open a workspace from the owner console (choose a GC).</div>'; q('main').classList.remove('hidden'); return; }
  if(!ok){ q('main').innerHTML='<div class="empty-right err" style="padding:40px">'+esc(data.error||'Could not load')+'</div>'; q('main').classList.remove('hidden'); return; }
  render(data);
}

function render(data){
  STATE=data; PLATFORMS=data.platforms||PLATFORMS;
  IS_OWNER=!!data.is_owner;
  const gc=data.gc; BRAND=gc.brand_primary||'#1E7A46';
  document.documentElement.style.setProperty('--brand',BRAND);
  q('gc-title').innerHTML=esc(gc.name)+' <span class="tag">· contractor monitoring</span>';
  const gl=q('gc-logo'); gl.innerHTML=gc.logo_url?'<img src="'+esc(gc.logo_url)+'">':esc((gc.name||'GC').slice(0,2).toUpperCase());
  q('who').innerHTML='<span class="dot-on"></span> '+(IS_OWNER?'Owner':'GC admin');
  if(IS_OWNER){ q('owner-badge').classList.remove('hidden'); q('back').classList.remove('hidden'); q('owner-msg-btn').classList.add('hidden'); }
  else { q('signout').classList.remove('hidden'); }
  q('main').classList.remove('hidden');
  drawList();
  if(!IS_OWNER) refreshOwnerPip();
  if((STATE.subs||[]).length){ selectSub((CUR&&CUR.id)|| STATE.subs[0].id); }
}

function subStatusLine(s){
  // pick the most pressing thing to show under the name
  if(s.coi && s.coi.days_left!=null){
    if(s.coi.days_left<0) return {t:'COI expired',c:'red'};
    if(s.coi.days_left<=30) return {t:'COI expires in '+s.coi.days_left+' days',c:'amber'};
  }
  let worst=null;
  PLATFORMS.forEach(p=>{const g=(s.grades[p]||'').toUpperCase(); if(g==='F'||g==='D'){worst={p,g}} else if(g==='C'&&!worst){worst={p,g}}});
  if(worst) return {t:PLABEL[worst.p]+' grade '+worst.g,c:(worst.g==='C'?'amber':'red')};
  if(!s.has_login) return {t:'invited · no login yet',c:''};
  return {t:'Good standing',c:''};
}

function drawList(){
  const subs=STATE.subs||[];
  const el=q('sublist');
  if(!subs.length){ el.innerHTML='<div class="hint" style="padding:8px 6px">No subcontractors yet. Use “+ Add subcontractor”.</div>'; return; }
  el.innerHTML=subs.map(s=>{
    const st=subStatusLine(s);
    const logo=s.logo_url?'<img src="'+esc(s.logo_url)+'">':esc((s.name||'S').slice(0,2).toUpperCase());
    return `<div class="subitem${CUR&&CUR.id===s.id?' sel':''}" onclick="selectSub('${s.id}')">
      <div class="row1"><div class="slogo">${logo}</div><div class="nm">${esc(s.name)}</div><div class="hdot ${esc(s.health)}"></div></div>
      <div class="st ${st.c}">${esc(st.t)}</div>
    </div>`;
  }).join('');
}

async function selectSub(id){
  const {ok,data}=await api(withGc('/platform/gc/subs/'+id));
  if(!ok){ q('detail').innerHTML='<div class="empty-right err">Could not load subcontractor.</div>'; return; }
  CUR=data.sub; CUR._library=data.library||[];
  drawList(); drawDetail();
}

function metricTile(p){
  const g=(CUR.grades[p]||'').toUpperCase();
  const cls=g?g.toLowerCase():'set';
  return `<div class="metric" onclick="editGrade('${p}')" title="Click to set">
    <div class="k">${PLABEL[p]||p.toUpperCase()}</div><div class="v ${cls}">${g||'Set'}</div></div>`;
}
function coiTile(){
  const coi=CUR.coi; let v='—',cls='ro';
  if(coi&&coi.days_left!=null){ v=coi.days_left<0?'Expired':(coi.days_left<=30?coi.days_left+'d':'Current'); }
  return `<div class="metric ro"><div class="k">COI</div><div class="v" style="font-size:16px">${v}</div></div>`;
}

function drawDetail(){
  const s=CUR;
  const flag = s.health==='red'?{t:'Action required',c:'red'}:(s.health==='amber'?{t:'Needs attention',c:'amber'}:{t:'Good standing',c:'green'});
  const logo=s.logo_url?'<img src="'+esc(s.logo_url)+'">':esc((s.name||'S').slice(0,2).toUpperCase());
  q('detail').innerHTML=`
    <div class="dhead">
      <div class="dlogo" onclick="q('sub-logo-file').click()" title="Upload sub logo">${logo}</div>
      <div>
        <h2>${esc(s.name)}</h2>
        <div class="scope">Scope: ${(s.scope||[]).map(esc).join(' · ')||'not set'}${s.login_email?' · login: '+esc(s.login_email):''}</div>
      </div>
      <div class="spacer"></div>
      <button class="pill-btn" onclick="document.getElementById('msg-box').focus()">Message sub</button>
      <span class="status-flag ${flag.c}">${flag.t}</span>
    </div>

    <div class="metrics">
      ${metricTile('isn')}${metricTile('avetta')}
      <div class="metric ro"><div class="k">TRIR</div><div class="v" style="font-size:18px">${s.trir!=null?esc(String(s.trir)):'—'}</div></div>
      <div class="metric ro"><div class="k">EMR</div><div class="v" style="font-size:18px">${s.emr!=null?esc(String(s.emr)):'—'}</div></div>
      ${coiTile()}
    </div>
    <div class="hint">Veriforce & PEC grades: ${['veriforce','pec'].map(p=>PLABEL[p]+' '+((s.grades[p]||'—').toUpperCase())).join(' · ')} — click any tile above to set a grade.</div>

    <div class="section">
      <div class="lbl">Documents ${s._library.length?'· send from your library or add a custom one':'· your program library is empty, add custom docs below'}</div>
      <div id="doclist"></div>
      <div class="card">
        <div class="addrow">
          ${s._library.length?`<div><label>Send from library</label><select id="lib-sel"><option value="">Choose a program…</option>${s._library.map(p=>'<option value="'+p.id+'">'+esc(p.title)+'</option>').join('')}</select></div><div style="flex:0 0 auto"><button class="mini" onclick="sendLib()">Fill from library</button></div>`:''}
          <div><label>Add a custom document</label><input id="doc-name" type="text" placeholder="e.g. Hazard Communication program"></div>
          <div style="flex:0 0 auto"><button class="mini" onclick="addDoc()">Add doc</button></div>
        </div>
        <div id="doc-msg" class="hint"></div>
      </div>
    </div>

    <div class="section">
      <div class="lbl">Messages with ${esc(s.name)}</div>
      <div class="thread" id="thread"></div>
      <div class="composer">
        <textarea id="msg-box" placeholder="Write a message…"></textarea>
        <button class="mini" onclick="sendSubMsg()">Send</button>
      </div>
      <div class="hint">${s.has_login?'The sub can reply from their own dashboard — the thread stays with this sub only.':'Issue this sub a login so they can reply from their dashboard.'}</div>
    </div>`;
  drawDocs(); drawThread();
}

function drawDocs(){
  const docs=CUR.docs||[];
  q('doclist').innerHTML=docs.length?docs.map(d=>`<div class="docrow">
    <span class="nm">${esc(d.name)}</span>
    <span class="tag ${d.source==='library'?'library':''}">${d.source==='library'?'Library':(esc(d.category)||'Custom')}</span>
    <span class="act"><button class="mini g" onclick="delDoc('${d.id}')">Remove</button></span>
  </div>`).join(''):'<div class="hint" style="margin-bottom:10px">No documents sent yet.</div>';
}
function drawThread(){
  const msgs=CUR.messages||[];
  const el=q('thread');
  el.innerHTML=msgs.length?msgs.map(m=>`<div class="msg ${esc(m.role)}">${esc(m.body)}<div class="t">${m.role==='sub'?esc(CUR.name):'You'} · ${fmtTime(m.created_at)}</div></div>`).join(''):'<div class="hint">No messages yet.</div>';
  el.scrollTop=el.scrollHeight;
}

// ── grade editing ──
async function editGrade(p){
  const cur=(CUR.grades[p]||'').toUpperCase();
  const g=prompt('Set '+(PLABEL[p]||p)+' grade for '+CUR.name+' (A, B, C, D, F — blank to clear):', cur);
  if(g===null)return;
  const {ok,data}=await api('/platform/gc/subs/'+CUR.id+'/grade',{method:'POST',body:JSON.stringify({gc:GC,platform:p,grade:g.trim()})});
  if(!ok){ alert(data.error||'Could not set grade'); return; }
  CUR.grades[p]=g.trim().toUpperCase(); CUR.health=data.health;
  const sm=(STATE.subs||[]).find(x=>x.id===CUR.id); if(sm){sm.grades[p]=CUR.grades[p]; sm.health=data.health;}
  drawDetail(); drawList();
}

// ── documents ──
async function sendLib(){
  const sel=q('lib-sel'); const id=sel?sel.value:''; if(!id){return;}
  const {ok,data}=await api('/platform/gc/subs/'+CUR.id+'/docs',{method:'POST',body:JSON.stringify({gc:GC,library_id:id})});
  if(!ok){ q('doc-msg').innerHTML='<span class="err">'+esc(data.error||'Failed')+'</span>'; return; }
  CUR.docs.unshift(data.doc); drawDocs(); q('doc-msg').innerHTML='<span class="ok">Sent.</span>';
}
async function addDoc(){
  const name=q('doc-name').value.trim(); if(!name){return;}
  const {ok,data}=await api('/platform/gc/subs/'+CUR.id+'/docs',{method:'POST',body:JSON.stringify({gc:GC,name})});
  if(!ok){ q('doc-msg').innerHTML='<span class="err">'+esc(data.error||'Failed')+'</span>'; return; }
  CUR.docs.unshift(data.doc); q('doc-name').value=''; drawDocs(); q('doc-msg').innerHTML='<span class="ok">Added.</span>';
}
async function delDoc(id){
  const {ok}=await api('/platform/gc/subs/'+CUR.id+'/docs/'+id,{method:'DELETE',body:JSON.stringify({})});
  if(ok){ CUR.docs=CUR.docs.filter(d=>d.id!==id); drawDocs(); }
}

// ── sub messages ──
async function sendSubMsg(){
  const box=q('msg-box'); const body=box.value.trim(); if(!body)return; box.value='';
  const {ok,data}=await api('/platform/gc/subs/'+CUR.id+'/messages',{method:'POST',body:JSON.stringify({gc:GC,body})});
  if(!ok){ alert(data.error||'Could not send'); return; }
  CUR.messages.push(data.message); drawThread();
}

// ── add subcontractor ──
function openAddSub(){ q('as-name').value='';q('as-scope').value='';q('as-email').value='';q('as-msg').textContent=''; q('dlg-add').showModal(); }
async function submitAddSub(){
  const name=q('as-name').value.trim();
  if(!name){ q('as-msg').innerHTML='<span class="err">Enter a company name.</span>'; return; }
  const {ok,data}=await api('/platform/gc/subs',{method:'POST',body:JSON.stringify({gc:GC,name,scope_of_work:q('as-scope').value,contact_email:q('as-email').value.trim()})});
  if(!ok){ q('as-msg').innerHTML='<span class="err">'+esc(data.error||'Could not add')+'</span>'; return; }
  q('dlg-add').close();
  const r=await api(withGc('/platform/gc/roster')); if(r.ok){ STATE=r.data; drawList(); selectSub(data.sub.id); }
}

// ── issue sub login ──
function openIssueLogin(){
  if(!CUR){ alert('Select a subcontractor first.'); return; }
  q('login-title').textContent='Issue a login for '+CUR.name;
  q('login-sub').textContent = CUR.has_login ? ('Resets the existing login ('+(CUR.login_email||'')+').') : 'Creates the sign-in the sub uses at /platform/sub.';
  q('lg-email').value=CUR.login_email||CUR.contact_email||''; q('lg-pass').value=''; q('lg-msg').textContent='';
  q('dlg-login').showModal();
}
async function submitIssueLogin(){
  const email=q('lg-email').value.trim(), password=q('lg-pass').value;
  if(!email||!password){ q('lg-msg').innerHTML='<span class="err">Enter an email and password.</span>'; return; }
  const {ok,data}=await api('/platform/gc/subs/'+CUR.id+'/login',{method:'POST',body:JSON.stringify({gc:GC,email,password})});
  if(!ok){ q('lg-msg').innerHTML='<span class="err">'+esc(data.error||'Failed')+'</span>'; return; }
  q('dlg-login').close();
  CUR.has_login=true; CUR.login_email=email;
  const sm=(STATE.subs||[]).find(x=>x.id===CUR.id); if(sm)sm.has_login=true;
  drawDetail(); drawList();
  alert('Login created for '+email+'. Share it with the sub — they sign in at /platform/sub.');
}

// ── owner <-> GC message drawer (gc_admin messaging the platform owner) ──
async function openOwnerThread(){
  q('drawer-title').textContent='Messages with Origin (platform owner)';
  q('owner-thread-hint').textContent='This is your private line to the platform owner.';
  q('scrim').classList.add('show'); q('drawer').classList.add('show');
  const {ok,data}=await api(withGc('/platform/gc-messages'));
  const el=q('owner-thread');
  if(!ok){ el.innerHTML='<div class="hint err">Could not load.</div>'; return; }
  const meRole=data.me;
  el.innerHTML=(data.messages||[]).length?data.messages.map(m=>{
    const mine=m.role===meRole; const cls=mine?'gc_admin':'owner';
    return `<div class="msg ${cls}">${esc(m.body)}<div class="t">${mine?'You':(m.role==='owner'?'Origin':'GC')} · ${fmtTime(m.created_at)}</div></div>`;
  }).join(''):'<div class="hint">No messages yet. Say hello to the platform owner.</div>';
  el.scrollTop=el.scrollHeight;
  refreshOwnerPip();
}
function closeDrawer(){ q('scrim').classList.remove('show'); q('drawer').classList.remove('show'); }
async function sendOwnerMsg(){
  const box=q('owner-msg-box'); const body=box.value.trim(); if(!body)return; box.value='';
  const {ok,data}=await api('/platform/gc-messages',{method:'POST',body:JSON.stringify({gc:GC,body})});
  if(!ok){ alert(data.error||'Could not send'); return; }
  openOwnerThread();
}
async function refreshOwnerPip(){
  // (light touch) we don't track unread server-side for the pip; hide it.
  q('owner-msg-pip').classList.add('hidden');
}

// ── logo uploads ──
function maybeUploadGcLogo(){ if(IS_OWNER||ROLE==='gc_admin'){ q('gc-logo-file').click(); } }
async function doUploadLogo(scope,id,ev){
  const f=ev.target.files[0]; if(!f||!id)return;
  const reader=new FileReader();
  reader.onload=async()=>{
    const {ok,data}=await api('/platform/media/logo',{method:'POST',body:JSON.stringify({scope,id,image:reader.result})});
    if(!ok){ alert(data.error||'Upload failed'); return; }
    if(scope==='gc'){ STATE.gc.logo_url=data.logo_url; q('gc-logo').innerHTML='<img src="'+esc(data.logo_url)+'">'; }
    else { CUR.logo_url=data.logo_url; const sm=(STATE.subs||[]).find(x=>x.id===CUR.id); if(sm)sm.logo_url=data.logo_url; drawDetail(); drawList(); }
  };
  reader.readAsDataURL(f);
  ev.target.value='';
}

async function signout(){ await api('/platform/logout',{method:'POST'}); location.href='/platform'; }

initTheme(); boot();
</script>
</body>
</html>
"""
