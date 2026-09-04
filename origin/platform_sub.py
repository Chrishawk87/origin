"""The Subcontractor Dashboard — where a sub logs in on the white-label platform.

A sub user (role='sub', scoped to their gc_id + sub_id) signs in here and sees
only their own world: their prequal grades and safety metrics, the documents
their GC has sent them, and a two-way message thread with that GC. Posting a
reply here is what makes GC↔sub messaging two-way.

Hard-scoped: every route reads the sub's identity from their signed session
(claims sub_id + gc_id) — never from a URL parameter — so a sub can only ever
touch their own records. Isolated module, registered under try/except, imports
only platform_db + platform_auth. A bug here can't touch anything else.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import select

from . import platform_db as db
from . import platform_auth as auth
from .platform_db import (
    Tenant, Subcontractor, ComplianceStatus, COI, Document, Message,
    ROLE_SUB, PLATFORMS,
)

try:
    from starlette.requests import Request
except Exception:  # pragma: no cover
    Request = None  # type: ignore


def _sub_claims(request):
    """Return claims iff the caller is a valid sub, else None."""
    claims = auth.read_session(request)
    if not claims or claims.get("role") != ROLE_SUB:
        return None
    if not claims.get("sub_id") or not claims.get("gc_id"):
        return None
    return claims


def register_sub(app) -> None:
    from fastapi import Body
    from fastapi.responses import JSONResponse, HTMLResponse

    def _deny():
        return JSONResponse({"error": "subcontractor sign-in required"},
                            status_code=403)

    @app.get("/platform/sub/home")
    def sub_home(request: Request):
        claims = _sub_claims(request)
        if not claims:
            return _deny()
        sub_id = claims["sub_id"]
        gc_id = claims["gc_id"]
        with db.session() as s:
            sub = s.get(Subcontractor, sub_id)
            gc = s.get(Tenant, gc_id)
            if not sub or not gc or sub.gc_id != gc_id:
                return _deny()
            grades = {g.platform: g.grade for g in s.scalars(
                select(ComplianceStatus).where(
                    ComplianceStatus.sub_id == sub_id)).all()}
            docs = [{"id": d.id, "name": d.name, "category": d.category,
                     "source": d.source,
                     "created_at": d.created_at.isoformat() if d.created_at else None}
                    for d in s.scalars(select(Document).where(
                        Document.sub_id == sub_id).order_by(
                        Document.created_at.desc())).all()]
            msg_rows = s.scalars(select(Message).where(
                Message.sub_id == sub_id).order_by(Message.created_at)).all()
            msgs = [{"id": m.id, "role": m.sender_role, "body": m.body,
                     "created_at": m.created_at.isoformat() if m.created_at else None}
                    for m in msg_rows]
            # opening the dashboard marks the GC's messages as read by the sub
            for m in msg_rows:
                if not m.read_by_sub:
                    m.read_by_sub = True
            s.commit()
            cois = s.scalars(select(COI).where(COI.sub_id == sub_id)).all()
            coi = None
            if cois:
                latest = max(cois, key=lambda c: c.expiry or date.min)
                days = (latest.expiry - date.today()).days if latest.expiry else None
                coi = {"carrier": latest.carrier, "coverage": latest.coverage,
                       "expiry": latest.expiry.isoformat() if latest.expiry else None,
                       "days_left": days}
            out = {
                "sub": {
                    "id": sub.id, "name": sub.name, "logo_url": sub.logo_url,
                    "scope": sub.scope_of_work or [], "health": sub.health,
                    "trir": sub.trir, "emr": sub.emr,
                    "contact_email": sub.contact_email,
                },
                "gc": {"name": gc.name, "logo_url": gc.logo_url,
                       "brand_primary": gc.brand_primary},
                "grades": grades, "coi": coi, "docs": docs, "messages": msgs,
                "platforms": list(PLATFORMS),
            }
        return out

    @app.post("/platform/sub/messages")
    def sub_post_message(request: Request, body: dict = Body(...)):
        claims = _sub_claims(request)
        if not claims:
            return _deny()
        text = (body.get("body") or "").strip()
        if not text:
            return JSONResponse({"error": "message is empty"}, status_code=400)
        sub_id = claims["sub_id"]
        gc_id = claims["gc_id"]
        with db.session() as s:
            sub = s.get(Subcontractor, sub_id)
            if not sub or sub.gc_id != gc_id:
                return _deny()
            m = Message(gc_id=gc_id, sub_id=sub_id,
                        sender_user_id=claims.get("uid"),
                        sender_role=ROLE_SUB, body=text,
                        read_by_sub=True, read_by_gc=False)
            s.add(m)
            s.commit()
            new = {"id": m.id, "role": m.sender_role, "body": m.body,
                   "created_at": m.created_at.isoformat() if m.created_at else None}
        return {"ok": True, "message": new}

    @app.get("/platform/sub", response_class=HTMLResponse)
    def sub_page():
        return HTMLResponse(_SUB_HTML)


_SUB_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Origin — Subcontractor Dashboard</title>
<style>
  :root{
    --brand:#1E7A46; --bg:#f4f6f5; --card:#fff; --ink:#12211a; --muted:#5b6b63;
    --line:#e6ebe8; --red:#c0392b; --amber:#d99200; --ok:#1E7A46; --chip:#eef2f0;
    --msg-in:#fff; --msg-out:#1E7A46;
  }
  html[data-theme="dark"]{
    --bg:#0e1512; --card:#16201b; --ink:#e8efea; --muted:#9fb0a7; --line:#25332c;
    --chip:#1e2a24; --msg-in:#1e2a24; --red:#e06a5c; --amber:#e6b04a; --ok:#4ecb7e;
  }
  *{box-sizing:border-box}
  body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    color:var(--ink);background:var(--bg)}
  header{color:#fff;padding:16px 26px;display:flex;align-items:center;justify-content:space-between;background:var(--brand)}
  header .brand{display:flex;align-items:center;gap:12px}
  header .logo{width:38px;height:38px;border-radius:9px;background:rgba(255,255,255,.2);overflow:hidden;
    display:flex;align-items:center;justify-content:center;font-weight:800;font-size:15px}
  header .logo img{width:100%;height:100%;object-fit:cover}
  header h1{margin:0;font-size:17px;font-weight:600}
  header .sub{font-size:12px;opacity:.9}
  header .r{display:flex;gap:10px;align-items:center;font-size:13px}
  header button{color:#fff;background:rgba(255,255,255,.16);border:0;padding:8px 12px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600}
  header button:hover{background:rgba(255,255,255,.3)}
  main{max-width:920px;margin:0 auto;padding:24px 22px 70px}
  .tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
  .tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  .tile .n{font-size:24px;font-weight:700}
  .tile .l{font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.3px;margin-top:2px}
  @media(max-width:700px){.tiles{grid-template-columns:repeat(2,1fr)}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px 22px;margin-bottom:18px}
  .card h2{font-size:15px;margin:0 0 14px}
  .grade{display:inline-block;min-width:26px;text-align:center;padding:4px 9px;border-radius:8px;font-size:13px;font-weight:700;background:var(--chip);margin-right:6px}
  .grade.f,.grade.d{background:rgba(192,57,43,.16);color:var(--red)} .grade.c{background:rgba(217,146,0,.18);color:var(--amber)}
  .grade.a,.grade.b{background:rgba(30,122,70,.16);color:var(--ok)} .grade.none{opacity:.5}
  .gp{display:inline-block;font-size:11px;color:var(--muted);margin-right:14px}
  .doc{display:flex;align-items:center;gap:10px;padding:11px 0;border-bottom:1px solid var(--line)}
  .doc:last-child{border-bottom:0}
  .doc .name{font-weight:600}
  .doc .tag{font-size:11px;padding:2px 8px;border-radius:12px;background:var(--chip);color:var(--muted);font-weight:600}
  .doc .tag.library{background:rgba(30,122,70,.16);color:var(--ok)}
  .thread{display:flex;flex-direction:column;gap:10px;margin-bottom:14px;max-height:380px;overflow-y:auto}
  .msg{max-width:78%;padding:9px 13px;border-radius:13px;font-size:14px}
  .msg.sub{align-self:flex-end;background:var(--msg-out);color:#fff;border-bottom-right-radius:4px}
  .msg.gc_admin,.msg.owner{align-self:flex-start;background:var(--msg-in);border:1px solid var(--line);border-bottom-left-radius:4px}
  .msg .t{font-size:11px;opacity:.7;margin-top:3px}
  .composer{display:flex;gap:8px;align-items:flex-end}
  textarea{flex:1;padding:9px 11px;border:1px solid var(--line);border-radius:9px;font-size:14px;font-family:inherit;background:var(--card);color:var(--ink);resize:vertical;min-height:44px}
  button.primary{background:var(--brand);color:#fff;border:0;padding:10px 16px;border-radius:9px;font-size:14px;font-weight:600;cursor:pointer}
  .empty{color:var(--muted);font-size:14px;padding:6px 0}
  .muted{color:var(--muted);font-size:12px;margin-top:8px}
  .err{color:var(--red);font-weight:600}
  .hidden{display:none!important}
  #login{max-width:390px;margin:9vh auto 0}
  input[type=email],input[type=password]{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:9px;font-size:14px;background:var(--card);color:var(--ink)}
  label{display:block;font-size:12px;color:var(--muted);margin:0 0 4px;font-weight:600}
  .stack>*+*{margin-top:12px}
  .logo-btn{font-size:12px;color:var(--brand);cursor:pointer;text-decoration:underline;background:none;border:0;padding:0}
</style>
</head>
<body>

<div id="login" class="hidden">
  <div class="card stack">
    <h2 style="text-align:center">Subcontractor sign in</h2>
    <div class="muted" style="text-align:center;margin:-6px 0 8px">Use the login your general contractor sent you</div>
    <div><label>Email</label><input id="li-email" type="email" autocomplete="username" placeholder="you@company.com"></div>
    <div><label>Password</label><input id="li-pass" type="password" autocomplete="current-password" placeholder="••••••••"></div>
    <button class="primary" style="width:100%" onclick="doLogin()">Sign in</button>
    <div id="li-msg" class="muted"></div>
  </div>
</div>

<div id="app" class="hidden">
  <header>
    <div class="brand">
      <div class="logo" id="gc-logo"></div>
      <div>
        <h1 id="gc-name">—</h1>
        <div class="sub" id="sub-name">Subcontractor dashboard</div>
      </div>
    </div>
    <div class="r">
      <button onclick="toggleTheme()" id="theme-btn" title="Toggle light/dark">🌙</button>
      <input id="logo-file" type="file" accept="image/*" class="hidden" onchange="uploadLogo(event)">
      <button onclick="signout()">Sign out</button>
    </div>
  </header>
  <main>
    <div class="tiles" id="tiles"></div>

    <div class="card">
      <h2>Your prequal grades</h2>
      <div id="grades"></div>
      <div class="muted">Grades are set by your general contractor from what they see on each platform.</div>
    </div>

    <div class="card">
      <h2>Documents your GC sent you</h2>
      <div id="docs"><div class="empty">Nothing yet.</div></div>
    </div>

    <div class="card">
      <h2>Messages with <span id="gc-name2">your GC</span></h2>
      <div class="thread" id="thread"></div>
      <div class="composer">
        <textarea id="msg-box" placeholder="Write a message…"></textarea>
        <button class="primary" onclick="sendMsg()">Send</button>
      </div>
      <div class="muted">Your GC sees these replies in your record.</div>
    </div>

    <div class="card">
      <h2>Your company logo</h2>
      <div id="logo-preview" class="muted">No logo set.</div>
      <button class="primary" style="margin-top:10px" onclick="document.getElementById('logo-file').click()">Upload logo</button>
      <div id="logo-msg" class="muted"></div>
    </div>
  </main>
</div>

<script>
let DATA=null;
function q(id){return document.getElementById(id)}
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function fmtTime(iso){ if(!iso)return''; try{return new Date(iso).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}catch(e){return''} }
async function api(path,opts){
  const r=await fetch(path,Object.assign({headers:{'Content-Type':'application/json'}},opts||{}));
  let d={};try{d=await r.json()}catch(e){}
  return {ok:r.ok,status:r.status,data:d};
}
function initTheme(){
  const t=localStorage.getItem('origin-theme')||'light';
  document.documentElement.setAttribute('data-theme',t);
  const b=q('theme-btn'); if(b) b.textContent=(t==='dark'?'☀️':'🌙');
}
function toggleTheme(){
  const cur=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
  document.documentElement.setAttribute('data-theme',cur);
  localStorage.setItem('origin-theme',cur);
  q('theme-btn').textContent=(cur==='dark'?'☀️':'🌙');
}
function show(w){q('login').classList.toggle('hidden',w!=='login');q('app').classList.toggle('hidden',w!=='app')}

async function boot(){
  const me=(await api('/platform/me')).data;
  if(!me.authenticated){ show('login'); return; }
  if(me.role!=='sub'){ show('login'); q('li-msg').innerHTML='<span class="err">This dashboard is for subcontractors.</span>'; return; }
  show('app'); load();
}
async function doLogin(){
  const email=q('li-email').value.trim(), password=q('li-pass').value;
  q('li-msg').textContent='Signing in…';
  const {ok,data}=await api('/platform/login',{method:'POST',body:JSON.stringify({email,password})});
  if(!ok){ q('li-msg').innerHTML='<span class="err">'+esc(data.error||'Sign in failed')+'</span>'; return; }
  q('li-msg').textContent=''; boot();
}
async function signout(){ await api('/platform/logout',{method:'POST'}); show('login'); }

async function load(){
  const {ok,data}=await api('/platform/sub/home');
  if(!ok){ q('app').innerHTML='<div class="card err" style="margin:24px">Could not load your dashboard.</div>'; return; }
  DATA=data;
  const gc=data.gc, sub=data.sub;
  document.documentElement.style.setProperty('--brand', gc.brand_primary||'#1E7A46');
  q('gc-name').textContent=gc.name||'Your GC';
  q('gc-name2').textContent=gc.name||'your GC';
  q('sub-name').textContent=sub.name||'Subcontractor dashboard';
  const gl=q('gc-logo');
  gl.innerHTML = gc.logo_url ? '<img src="'+esc(gc.logo_url)+'">' : esc((gc.name||'GC').slice(0,2).toUpperCase());
  drawTiles(); drawGrades(); drawDocs(); drawThread();
  q('logo-preview').innerHTML = sub.logo_url ? '<img src="'+esc(sub.logo_url)+'" style="height:54px;border-radius:8px">' : '<span class="muted">No logo set.</span>';
}
function drawTiles(){
  const s=DATA.sub, coi=DATA.coi;
  const health={green:'Good standing',amber:'Needs attention',red:'Action required'}[s.health]||s.health;
  const coiTxt = coi&&coi.days_left!=null ? (coi.days_left<0?'Expired':coi.days_left+'d left') : '—';
  const tiles=[
    {n:health,l:'Standing'},
    {n:coiTxt,l:'COI'},
    {n:s.trir!=null?s.trir:'—',l:'TRIR'},
    {n:s.emr!=null?s.emr:'—',l:'EMR'},
  ];
  q('tiles').innerHTML=tiles.map(t=>`<div class="tile"><div class="n">${esc(String(t.n))}</div><div class="l">${t.l}</div></div>`).join('');
}
function drawGrades(){
  const g=DATA.grades||{};
  q('grades').innerHTML=(DATA.platforms||[]).map(p=>{
    const v=(g[p]||'').toUpperCase();
    const cls=v?('grade '+v.toLowerCase()):'grade none';
    return `<span class="gp"><span class="${cls}">${v||'·'}</span>${p.toUpperCase()}</span>`;
  }).join('');
}
function drawDocs(){
  const docs=DATA.docs||[];
  q('docs').innerHTML = docs.length ? docs.map(d=>`<div class="doc">
    <span class="name">${esc(d.name)}</span>
    <span class="tag ${d.source==='library'?'library':''}">${d.source==='library'?'From library':(esc(d.category)||'Document')}</span>
    <span style="margin-left:auto;color:var(--muted);font-size:12px">${fmtTime(d.created_at)}</span>
  </div>`).join('') : '<div class="empty">Your GC hasn\'t sent any documents yet.</div>';
}
function drawThread(){
  const msgs=DATA.messages||[];
  const el=q('thread');
  el.innerHTML = msgs.length ? msgs.map(m=>`<div class="msg ${esc(m.role)}">${esc(m.body)}
    <div class="t">${m.role==='sub'?'You':'GC'} · ${fmtTime(m.created_at)}</div></div>`).join('')
    : '<div class="empty">No messages yet.</div>';
  el.scrollTop=el.scrollHeight;
}
async function sendMsg(){
  const box=q('msg-box'); const body=box.value.trim();
  if(!body) return;
  box.value='';
  const {ok,data}=await api('/platform/sub/messages',{method:'POST',body:JSON.stringify({body})});
  if(!ok){ alert(data.error||'Could not send'); return; }
  DATA.messages.push(data.message); drawThread();
}
async function uploadLogo(ev){
  const f=ev.target.files[0]; if(!f) return;
  const msg=q('logo-msg'); msg.textContent='Uploading…';
  const reader=new FileReader();
  reader.onload=async()=>{
    const {ok,data}=await api('/platform/media/logo',{method:'POST',
      body:JSON.stringify({scope:'sub',id:DATA.sub.id,image:reader.result})});
    if(!ok){ msg.innerHTML='<span class="err">'+esc(data.error||'Upload failed')+'</span>'; return; }
    msg.textContent='Logo updated.';
    q('logo-preview').innerHTML='<img src="'+esc(data.logo_url)+'" style="height:54px;border-radius:8px">';
    DATA.sub.logo_url=data.logo_url;
  };
  reader.readAsDataURL(f);
}
initTheme(); boot();
</script>
</body>
</html>
"""
