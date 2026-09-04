"""The Owner Console — Chris's private control room for the white-label platform.

This is the screen only the OWNER logs into. From here Chris can:
  * see every GC (tenant) on the platform,
  * create a new GC (name + brand colour), and
  * issue that GC its admin login.

It is a thin, self-contained layer on top of the data models in platform_db and
the auth/session helpers in platform_auth. Like the rest of the platform it is
standalone and registered under a try/except, so a bug here can never break the
AI app, the portal, or the existing platform auth routes.

Every data route below is OWNER-ONLY, enforced server-side on each request via
_require_owner(). The console page itself is public HTML, but it shows nothing
until /platform/me confirms an owner session — and the APIs it calls refuse any
non-owner regardless of what the browser does.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import select, func
from starlette.requests import Request

from . import platform_db as db
from . import platform_auth as auth
from .platform_db import Tenant, User, Subcontractor, ROLE_OWNER, ROLE_GC_ADMIN


def _require_owner(request):
    """Return the owner's claims, or None if the caller is not a valid owner."""
    claims = auth.read_session(request)
    if not claims or claims.get("role") != ROLE_OWNER:
        return None
    return claims


def _unique_slug(sess, base: str) -> str:
    """Slugify `base` and make it unique among tenants by adding a suffix."""
    root = auth.slugify(base)
    slug = root
    n = 2
    while sess.scalar(select(Tenant).where(Tenant.slug == slug)) is not None:
        slug = f"{root}-{n}"
        n += 1
    return slug


def register_console(app) -> None:
    """Wire the owner console page + owner-only GC routes onto the app.

    Wrapped in try/except by the caller so any failure leaves the app untouched.
    """
    from fastapi import Body
    from fastapi.responses import JSONResponse, HTMLResponse

    # ── owner-only data routes ───────────────────────────────────────────
    @app.get("/platform/gcs")
    def list_gcs(request: Request):
        if not _require_owner(request):
            return JSONResponse({"error": "owner only"}, status_code=403)
        with db.session() as s:
            gcs = s.scalars(select(Tenant).order_by(Tenant.created_at.desc())).all()
            out = []
            for t in gcs:
                sub_n = s.scalar(
                    select(func.count(Subcontractor.id)).where(
                        Subcontractor.gc_id == t.id)) or 0
                admin_n = s.scalar(
                    select(func.count(User.id)).where(
                        User.gc_id == t.id, User.role == ROLE_GC_ADMIN)) or 0
                admins = s.scalars(
                    select(User).where(User.gc_id == t.id,
                                       User.role == ROLE_GC_ADMIN)).all()
                out.append({
                    "id": t.id, "name": t.name, "slug": t.slug,
                    "brand_primary": t.brand_primary, "brand_text": t.brand_text,
                    "active": t.active,
                    "subs": int(sub_n), "admins": int(admin_n),
                    "admin_emails": [a.email for a in admins],
                })
        return {"gcs": out}

    @app.post("/platform/gcs")
    def create_gc(request: Request, body: dict = Body(...)):
        if not _require_owner(request):
            return JSONResponse({"error": "owner only"}, status_code=403)
        name = (body.get("name") or "").strip()
        primary = (body.get("brand_primary") or "#1E7A46").strip()
        text = (body.get("brand_text") or "#FFFFFF").strip()
        if not name:
            return JSONResponse({"error": "GC name is required"}, status_code=400)
        with db.session() as s:
            slug = _unique_slug(s, name)
            t = Tenant(name=name, slug=slug,
                       brand_primary=primary or "#1E7A46",
                       brand_text=text or "#FFFFFF")
            s.add(t)
            s.commit()
            gc = {"id": t.id, "name": t.name, "slug": t.slug,
                  "brand_primary": t.brand_primary, "brand_text": t.brand_text}
        return {"ok": True, "gc": gc}

    @app.post("/platform/gcs/{gc_id}/admin")
    def issue_gc_admin(gc_id: str, request: Request, body: dict = Body(...)):
        if not _require_owner(request):
            return JSONResponse({"error": "owner only"}, status_code=403)
        email = (body.get("email") or "").strip().lower()
        password = body.get("password") or ""
        name = (body.get("name") or "").strip()
        if not email or not password:
            return JSONResponse({"error": "email and password are required"},
                                status_code=400)
        if len(password) < 8:
            return JSONResponse(
                {"error": "password must be at least 8 characters"},
                status_code=400)
        with db.session() as s:
            t = s.get(Tenant, gc_id)
            if not t:
                return JSONResponse({"error": "GC not found"}, status_code=404)
            exists = s.scalar(select(User).where(User.email == email))
            if exists:
                return JSONResponse(
                    {"error": "that email already has a login"}, status_code=400)
            u = User(email=email, password_hash=auth.hash_password(password),
                     role=ROLE_GC_ADMIN, gc_id=t.id,
                     name=name or f"{t.name} Admin")
            s.add(u)
            s.commit()
            created = {"email": u.email, "name": u.name, "gc": t.name}
        return {"ok": True, "admin": created}

    # ── the console page (public HTML; content gated by owner session) ────
    @app.get("/platform", response_class=HTMLResponse)
    def console_page():
        return HTMLResponse(_CONSOLE_HTML)

    @app.get("/platform/", response_class=HTMLResponse)
    def console_page_slash():
        return HTMLResponse(_CONSOLE_HTML)


# ── the single-page console (vanilla JS, no build step, no external deps) ──
_CONSOLE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Origin — Owner Console</title>
<style>
  :root { --green:#1E7A46; --ink:#12211a; --muted:#5b6b63; --line:#e3e9e5;
          --bg:#f5f7f6; --card:#ffffff; --danger:#b23b3b; }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",
         Roboto,Helvetica,Arial,sans-serif; color:var(--ink); background:var(--bg); }
  header { background:var(--green); color:#fff; padding:16px 22px; display:flex;
           align-items:center; justify-content:space-between; }
  header h1 { margin:0; font-size:17px; font-weight:600; letter-spacing:.2px; }
  header .who { font-size:13px; opacity:.9; display:flex; gap:12px; align-items:center; }
  header button { background:rgba(255,255,255,.16); color:#fff; border:0;
                  padding:7px 12px; border-radius:7px; cursor:pointer; font-size:13px; }
  header button:hover { background:rgba(255,255,255,.28); }
  main { max-width:920px; margin:0 auto; padding:26px 20px 60px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:20px 22px; margin-bottom:18px; }
  h2 { font-size:15px; margin:0 0 14px; }
  label { display:block; font-size:12px; color:var(--muted); margin:0 0 4px; font-weight:600; }
  input[type=text], input[type=email], input[type=password] {
    width:100%; padding:9px 11px; border:1px solid var(--line); border-radius:8px;
    font-size:14px; background:#fff; }
  input[type=color] { width:46px; height:38px; padding:2px; border:1px solid var(--line);
    border-radius:8px; background:#fff; cursor:pointer; vertical-align:bottom; }
  .row { display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end; }
  .row > div { flex:1; min-width:150px; }
  button.primary { background:var(--green); color:#fff; border:0; padding:10px 16px;
    border-radius:8px; font-size:14px; font-weight:600; cursor:pointer; }
  button.primary:hover { filter:brightness(1.06); }
  button.ghost { background:#fff; color:var(--green); border:1px solid var(--green);
    padding:8px 12px; border-radius:8px; font-size:13px; font-weight:600; cursor:pointer; }
  .gc { border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin-bottom:12px; }
  .gc .top { display:flex; align-items:center; gap:12px; }
  .swatch { width:26px; height:26px; border-radius:6px; border:1px solid rgba(0,0,0,.1); }
  .gc .name { font-weight:600; }
  .gc .meta { font-size:12px; color:var(--muted); margin-left:auto; text-align:right; }
  .gc .emails { font-size:12px; color:var(--muted); margin-top:6px; }
  .issue { margin-top:12px; padding-top:12px; border-top:1px dashed var(--line); display:none; }
  .issue.open { display:block; }
  .note { font-size:12px; color:var(--muted); margin-top:8px; }
  .ok { color:var(--green); font-weight:600; }
  .err { color:var(--danger); font-weight:600; }
  .empty { color:var(--muted); font-size:14px; }
  /* login */
  #login { max-width:380px; margin:9vh auto 0; }
  #login .card { text-align:left; }
  #login h2 { text-align:center; }
  .brandline { text-align:center; color:var(--muted); font-size:13px; margin:-6px 0 18px; }
  .hidden { display:none !important; }
  .stack > * + * { margin-top:12px; }
</style>
</head>
<body>

<div id="login" class="hidden">
  <div class="card stack">
    <h2>Origin — Owner Console</h2>
    <div class="brandline">Sign in to manage your platform</div>
    <div>
      <label>Email</label>
      <input id="li-email" type="email" autocomplete="username" placeholder="you@company.com">
    </div>
    <div>
      <label>Password</label>
      <input id="li-pass" type="password" autocomplete="current-password" placeholder="••••••••">
    </div>
    <button class="primary" style="width:100%" onclick="doLogin()">Sign in</button>
    <div id="li-msg" class="note"></div>
  </div>
</div>

<div id="app" class="hidden">
  <header>
    <h1>Origin — Owner Console</h1>
    <div class="who"><span id="who-email"></span><button onclick="doLogout()">Sign out</button></div>
  </header>
  <main>
    <div class="card">
      <h2>Add a General Contractor</h2>
      <div class="row">
        <div style="flex:2">
          <label>GC name</label>
          <input id="gc-name" type="text" placeholder="e.g. Redline Constructors">
        </div>
        <div style="flex:0 0 auto">
          <label>Brand colour</label>
          <input id="gc-color" type="color" value="#1E7A46">
        </div>
        <div style="flex:0 0 auto">
          <button class="primary" onclick="createGC()">Create GC</button>
        </div>
      </div>
      <div id="gc-msg" class="note"></div>
    </div>

    <div class="card">
      <h2>Your General Contractors</h2>
      <div id="gc-list"><div class="empty">Loading…</div></div>
    </div>
  </main>
</div>

<script>
async function api(path, opts){
  const r = await fetch(path, Object.assign({headers:{'Content-Type':'application/json'}}, opts||{}));
  let data = {}; try { data = await r.json(); } catch(e){}
  return {ok:r.ok, status:r.status, data};
}
function esc(s){ return (s||'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

async function boot(){
  const {data} = await api('/platform/me');
  if(!data.authenticated){ show('login'); return; }
  if(data.role !== 'owner'){
    show('login');
    document.getElementById('li-msg').innerHTML =
      '<span class="err">This console is for the platform owner only.</span>';
    return;
  }
  document.getElementById('who-email').textContent = data.email || '';
  show('app');
  loadGCs();
}
function show(which){
  document.getElementById('login').classList.toggle('hidden', which!=='login');
  document.getElementById('app').classList.toggle('hidden', which!=='app');
}

async function doLogin(){
  const email = document.getElementById('li-email').value.trim();
  const password = document.getElementById('li-pass').value;
  const msg = document.getElementById('li-msg');
  msg.textContent = 'Signing in…';
  const {ok, data} = await api('/platform/login', {method:'POST', body:JSON.stringify({email, password})});
  if(!ok){ msg.innerHTML = '<span class="err">'+esc(data.error||'Sign in failed')+'</span>'; return; }
  msg.textContent = '';
  boot();
}
async function doLogout(){ await api('/platform/logout', {method:'POST'}); show('login'); }

async function createGC(){
  const name = document.getElementById('gc-name').value.trim();
  const color = document.getElementById('gc-color').value;
  const msg = document.getElementById('gc-msg');
  if(!name){ msg.innerHTML='<span class="err">Enter a GC name.</span>'; return; }
  msg.textContent = 'Creating…';
  const {ok, data} = await api('/platform/gcs', {method:'POST',
    body:JSON.stringify({name, brand_primary:color})});
  if(!ok){ msg.innerHTML='<span class="err">'+esc(data.error||'Could not create GC')+'</span>'; return; }
  msg.innerHTML = '<span class="ok">Created '+esc(data.gc.name)+'.</span>';
  document.getElementById('gc-name').value='';
  loadGCs();
}

async function loadGCs(){
  const el = document.getElementById('gc-list');
  const {ok, data} = await api('/platform/gcs');
  if(!ok){ el.innerHTML='<div class="err">Could not load GCs.</div>'; return; }
  const gcs = data.gcs||[];
  if(!gcs.length){ el.innerHTML='<div class="empty">No GCs yet. Create your first one above.</div>'; return; }
  el.innerHTML = gcs.map(renderGC).join('');
}

function renderGC(g){
  const emails = g.admin_emails && g.admin_emails.length
    ? 'Admin logins: '+g.admin_emails.map(esc).join(', ')
    : 'No admin login yet';
  return `
  <div class="gc">
    <div class="top">
      <div class="swatch" style="background:${esc(g.brand_primary)}"></div>
      <div class="name">${esc(g.name)}</div>
      <div class="meta">${g.subs} sub${g.subs===1?'':'s'} · ${g.admins} admin${g.admins===1?'':'s'}<br>
        <span style="opacity:.7">/${esc(g.slug)}</span></div>
    </div>
    <div class="emails">${emails}</div>
    <button class="ghost" style="margin-top:10px" onclick="toggleIssue('${g.id}')">Issue admin login</button>
    <div class="issue" id="issue-${g.id}">
      <div class="row">
        <div><label>Admin email</label><input type="email" id="ie-${g.id}" placeholder="admin@gc.com"></div>
        <div><label>Temporary password</label><input type="password" id="ip-${g.id}" placeholder="min 8 characters"></div>
        <div style="flex:0 0 auto"><button class="primary" onclick="issueAdmin('${g.id}')">Create login</button></div>
      </div>
      <div class="note" id="im-${g.id}"></div>
    </div>
  </div>`;
}

function toggleIssue(id){ document.getElementById('issue-'+id).classList.toggle('open'); }

async function issueAdmin(id){
  const email = document.getElementById('ie-'+id).value.trim();
  const password = document.getElementById('ip-'+id).value;
  const msg = document.getElementById('im-'+id);
  if(!email || !password){ msg.innerHTML='<span class="err">Enter an email and password.</span>'; return; }
  msg.textContent='Creating…';
  const {ok, data} = await api('/platform/gcs/'+id+'/admin', {method:'POST',
    body:JSON.stringify({email, password})});
  if(!ok){ msg.innerHTML='<span class="err">'+esc(data.error||'Could not create login')+'</span>'; return; }
  msg.innerHTML='<span class="ok">Login created for '+esc(data.admin.email)+'. Share these credentials with the GC.</span>';
  document.getElementById('ie-'+id).value='';
  document.getElementById('ip-'+id).value='';
  loadGCs();
}

boot();
</script>
</body>
</html>
"""
