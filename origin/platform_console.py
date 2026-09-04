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

    @app.delete("/platform/gcs/{gc_id}")
    def delete_gc(gc_id: str, request: Request):
        """Remove a GC entirely — used when Chris stops working with a client.

        Deletes the tenant and, by ORM cascade, every one of its subcontractors
        (with their grades, COIs, documents, messages) AND every login tied to
        the GC — both the gc_admin accounts and any sub logins — so no one from
        that GC can sign in again. Owner-only, irreversible.
        """
        if not _require_owner(request):
            return JSONResponse({"error": "owner only"}, status_code=403)
        with db.session() as s:
            t = s.get(Tenant, gc_id)
            if not t:
                return JSONResponse({"error": "GC not found"}, status_code=404)
            name = t.name
            sub_n = s.scalar(select(func.count(Subcontractor.id)).where(
                Subcontractor.gc_id == gc_id)) or 0
            login_n = s.scalar(select(func.count(User.id)).where(
                User.gc_id == gc_id)) or 0
            revoked = [u.email for u in s.scalars(
                select(User).where(User.gc_id == gc_id)).all()]
            # ORM cascade (Tenant.subs / Tenant.users are delete-orphan) removes
            # every sub, their grades/COIs/docs/messages, and every login.
            s.delete(t)
            s.commit()
        return {"ok": True, "deleted": name,
                "subs_removed": int(sub_n), "logins_revoked": int(login_n),
                "revoked_emails": revoked}

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
  :root { --brand:#1E7A46; --ink:#12211a; --muted:#5b6b63; --line:#e6ebe8;
          --bg:#f4f6f5; --card:#fff; --red:#c0392b; --amber:#d99200; --ok:#1E7A46; }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
         Helvetica,Arial,sans-serif; color:var(--ink); background:var(--bg); }
  header { color:#fff; padding:16px 26px; display:flex; align-items:center; justify-content:space-between;
    box-shadow:0 1px 0 rgba(0,0,0,.06); background:var(--brand); }
  header .brand { display:flex; align-items:center; gap:12px; }
  header .logo { width:34px; height:34px; border-radius:9px; background:rgba(255,255,255,.2);
    display:flex; align-items:center; justify-content:center; font-weight:800; font-size:15px; }
  header h1 { margin:0; font-size:18px; font-weight:600; letter-spacing:.2px; }
  header .r { display:flex; gap:10px; align-items:center; font-size:13px; }
  header .badge { background:rgba(255,255,255,.18); padding:5px 10px; border-radius:20px; font-weight:600; }
  header button { color:#fff; background:rgba(255,255,255,.16);
    border:0; padding:8px 13px; border-radius:8px; cursor:pointer; font-size:13px; font-weight:600; }
  header button:hover { background:rgba(255,255,255,.3); }
  main { max-width:1080px; margin:0 auto; padding:24px 22px 70px; }

  .tiles { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:20px; }
  .tile { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }
  .tile .n { font-size:26px; font-weight:700; line-height:1.1; }
  .tile .l { font-size:12px; color:var(--muted); margin-top:2px; font-weight:600; text-transform:uppercase; letter-spacing:.3px; }
  .tile.green .n{color:var(--ok)} .tile.warn .n{color:var(--amber)}
  @media(max-width:760px){ .tiles{grid-template-columns:repeat(2,1fr)} }

  .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:20px 22px; margin-bottom:18px; }
  .card h2 { font-size:15px; margin:0 0 14px; display:flex; align-items:center; justify-content:space-between; }
  .card h2 .hint { font-weight:400; font-size:12px; color:var(--muted); }
  label { display:block; font-size:12px; color:var(--muted); margin:0 0 4px; font-weight:600; }
  input[type=text], input[type=email], input[type=password] {
    width:100%; padding:9px 11px; border:1px solid var(--line); border-radius:9px;
    font-size:14px; font-family:inherit; background:#fff; }
  input[type=color] { width:46px; height:40px; padding:2px; border:1px solid var(--line);
    border-radius:9px; background:#fff; cursor:pointer; vertical-align:bottom; }
  .row { display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end; }
  .row > div { flex:1; min-width:150px; }
  button.primary { background:var(--brand); color:#fff; border:0; padding:10px 16px; border-radius:9px;
    font-size:14px; font-weight:600; cursor:pointer; }
  button.primary:hover { filter:brightness(1.07); }
  button.ghost, a.ghost { background:#eef2f0; color:var(--brand); border:0;
    padding:8px 13px; border-radius:8px; font-size:13px; font-weight:600; cursor:pointer;
    text-decoration:none; display:inline-block; line-height:1.3; }
  a.ghost:hover, button.ghost:hover { background:#e2ece6; }
  button.danger-ghost { background:#fff; color:var(--red); border:1px solid var(--red);
    padding:8px 13px; border-radius:8px; font-size:13px; font-weight:600; cursor:pointer; }
  button.danger-ghost:hover { background:#fbecec; }

  dialog { border:0; border-radius:12px; padding:22px; max-width:440px; box-shadow:0 10px 40px rgba(0,0,0,.25); }
  dialog h3 { margin:0 0 10px; font-size:16px; }
  dialog .actions { display:flex; gap:10px; justify-content:flex-end; margin-top:16px; }
  dialog button { padding:8px 14px; border-radius:8px; border:1px solid var(--line); background:#fff; cursor:pointer; font-size:14px; }
  dialog button.danger { background:var(--red); color:#fff; border:0; font-weight:600; }
  dialog button.danger:disabled { opacity:.45; cursor:not-allowed; }

  .gc { border:1px solid var(--line); border-radius:12px; padding:16px 18px; margin-bottom:12px; background:#fff; }
  .gc .top { display:flex; align-items:center; gap:12px; }
  .gc .glogo { width:34px; height:34px; border-radius:9px; color:#fff; display:flex; align-items:center;
    justify-content:center; font-weight:800; font-size:14px; flex:0 0 auto; }
  .gc .name { font-weight:600; font-size:16px; }
  .gc .slug { font-size:12px; color:var(--muted); }
  .gc .meta { margin-left:auto; text-align:right; font-size:12px; color:var(--muted); }
  .gc .pill { display:inline-block; background:#eef2f0; border-radius:20px; padding:3px 10px; font-weight:600; margin-left:6px; }
  .gc .pill.warn { background:#fbf0d3; color:#8a6400; }
  .gc .emails { font-size:12px; color:var(--muted); margin-top:8px; }
  .gc .actions-row { margin-top:12px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .issue { margin-top:12px; padding-top:12px; border-top:1px dashed var(--line); display:none; }
  .issue.open { display:block; }
  .note { font-size:12px; color:var(--muted); margin-top:8px; }
  .ok { color:var(--ok); font-weight:600; }
  .err { color:var(--red); font-weight:600; }
  .empty { color:var(--muted); font-size:14px; padding:6px 0; }
  .hidden { display:none !important; }
  .stack > * + * { margin-top:12px; }
  /* login */
  #login { max-width:390px; margin:9vh auto 0; }
  #login h2 { text-align:center; }
  .brandline { text-align:center; color:var(--muted); font-size:13px; margin:-6px 0 18px; }
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
    <div class="brand">
      <div class="logo">OM</div>
      <h1>Origin — Owner Console</h1>
    </div>
    <div class="r">
      <span class="badge">Owner</span>
      <span id="who-email"></span>
      <button onclick="doLogout()">Sign out</button>
    </div>
  </header>
  <main>
    <div class="tiles" id="tiles"></div>

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
      <h2>Your General Contractors <span class="hint">Open a workspace to manage subs, or issue an admin login</span></h2>
      <div id="gc-list"><div class="empty">Loading…</div></div>
    </div>
  </main>
</div>

<dialog id="dlg-delgc">
  <h3>Delete GC</h3>
  <div id="delgc-text"></div>
  <div class="note">This permanently removes the GC, all of its subcontractors and their records, and <b>every login</b> for this GC (admins and subs) — so no one from it can sign in again. This cannot be undone.</div>
  <div style="margin-top:12px">
    <label>Type the GC's name to confirm</label>
    <input id="delgc-input" type="text" autocomplete="off" oninput="delgcCheck()">
  </div>
  <div id="delgc-msg" class="note"></div>
  <div class="actions">
    <button onclick="document.getElementById('dlg-delgc').close()">Cancel</button>
    <button class="danger" id="delgc-confirm" disabled onclick="confirmDeleteGC()">Delete GC</button>
  </div>
</dialog>

<script>
async function api(path, opts){
  const r = await fetch(path, Object.assign({headers:{'Content-Type':'application/json'}}, opts||{}));
  let data = {}; try { data = await r.json(); } catch(e){}
  return {ok:r.ok, status:r.status, data};
}
function esc(s){ return (s||'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function initials(name){ return (name||'GC').trim().slice(0,2).toUpperCase(); }

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
  drawTiles(gcs);
  if(!gcs.length){ el.innerHTML='<div class="empty">No GCs yet. Create your first one above.</div>'; return; }
  el.innerHTML = gcs.map(renderGC).join('');
}

function drawTiles(gcs){
  const totalGCs = gcs.length;
  const totalSubs = gcs.reduce((a,g)=>a+(g.subs||0), 0);
  const totalAdmins = gcs.reduce((a,g)=>a+(g.admins||0), 0);
  const needAdmin = gcs.filter(g=>!(g.admins>0)).length;
  const tiles = [
    {n:totalGCs, l:'General Contractors', c:''},
    {n:totalSubs, l:'Subcontractors', c:'green'},
    {n:totalAdmins, l:'Admin logins', c:''},
    {n:needAdmin, l:'Need an admin', c:needAdmin?'warn':''},
  ];
  document.getElementById('tiles').innerHTML = tiles.map(t=>
    `<div class="tile ${t.c}"><div class="n">${t.n}</div><div class="l">${t.l}</div></div>`).join('');
}

function renderGC(g){
  const emails = g.admin_emails && g.admin_emails.length
    ? 'Admin logins: '+g.admin_emails.map(esc).join(', ')
    : '<span style="color:var(--amber);font-weight:600">No admin login yet</span>';
  const adminPill = g.admins>0
    ? `<span class="pill">${g.admins} admin${g.admins===1?'':'s'}</span>`
    : `<span class="pill warn">Needs admin</span>`;
  const brand = esc(g.brand_primary||'#1E7A46');
  return `
  <div class="gc">
    <div class="top">
      <div class="glogo" style="background:${brand}">${initials(g.name)}</div>
      <div>
        <div class="name">${esc(g.name)}</div>
        <div class="slug">/${esc(g.slug)}</div>
      </div>
      <div class="meta">
        <span class="pill">${g.subs} sub${g.subs===1?'':'s'}</span>${adminPill}
      </div>
    </div>
    <div class="emails">${emails}</div>
    <div class="actions-row">
      <a class="ghost" href="/platform/gc?gc=${encodeURIComponent(g.id)}">Open workspace →</a>
      <button class="ghost" onclick="toggleIssue('${g.id}')">Issue admin login</button>
      <button class="danger-ghost" onclick="askDeleteGC('${g.id}','${esc(g.name).replace(/'/g,"\\'")}')">Delete GC</button>
    </div>
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

let DEL_GC = null;
function askDeleteGC(id, name){
  DEL_GC = {id, name};
  document.getElementById('delgc-text').innerHTML = 'Permanently delete <b>'+esc(name)+'</b>?';
  const inp = document.getElementById('delgc-input');
  inp.value=''; document.getElementById('delgc-msg').textContent='';
  document.getElementById('delgc-confirm').disabled = true;
  document.getElementById('dlg-delgc').showModal();
  setTimeout(()=>inp.focus(), 50);
}
function delgcCheck(){
  const typed = document.getElementById('delgc-input').value.trim();
  document.getElementById('delgc-confirm').disabled =
    !DEL_GC || typed.toLowerCase() !== DEL_GC.name.trim().toLowerCase();
}
async function confirmDeleteGC(){
  if(!DEL_GC) return;
  const msg = document.getElementById('delgc-msg');
  msg.textContent = 'Deleting…';
  const {ok, data} = await api('/platform/gcs/'+DEL_GC.id, {method:'DELETE'});
  if(!ok){ msg.innerHTML='<span class="err">'+esc(data.error||'Could not delete')+'</span>'; return; }
  document.getElementById('dlg-delgc').close();
  DEL_GC = null;
  loadGCs();
}

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
