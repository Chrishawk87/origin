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
from starlette.requests import Request

from . import platform_db as db
from . import platform_auth as auth
from .platform_db import (
    Tenant, User, Subcontractor, ComplianceStatus, COI,
    ROLE_OWNER, ROLE_GC_ADMIN, PLATFORMS,
)


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
  :root { --green:#1E7A46; --ink:#12211a; --muted:#5b6b63; --line:#e3e9e5;
          --bg:#f5f7f6; --card:#fff; --red:#c0392b; --amber:#e0a100; --ok:#1E7A46; }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
         Helvetica,Arial,sans-serif; color:var(--ink); background:var(--bg); }
  header { color:#fff; padding:15px 22px; display:flex; align-items:center; justify-content:space-between; }
  header h1 { margin:0; font-size:17px; font-weight:600; }
  header .r { display:flex; gap:12px; align-items:center; font-size:13px; }
  header a, header button { color:#fff; text-decoration:none; background:rgba(255,255,255,.16);
    border:0; padding:7px 12px; border-radius:7px; cursor:pointer; font-size:13px; }
  header a:hover, header button:hover { background:rgba(255,255,255,.3); }
  main { max-width:1000px; margin:0 auto; padding:24px 20px 60px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px 22px; margin-bottom:18px; }
  h2 { font-size:15px; margin:0 0 14px; }
  label { display:block; font-size:12px; color:var(--muted); margin:0 0 4px; font-weight:600; }
  input[type=text],input[type=email] { width:100%; padding:9px 11px; border:1px solid var(--line);
    border-radius:8px; font-size:14px; }
  .row { display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end; }
  .row > div { flex:1; min-width:150px; }
  button.primary { background:var(--green); color:#fff; border:0; padding:10px 16px; border-radius:8px;
    font-size:14px; font-weight:600; cursor:pointer; }
  button.primary:hover { filter:brightness(1.06); }
  button.link { background:none; border:0; color:var(--red); cursor:pointer; font-size:13px; padding:0; font-weight:600; }
  table { width:100%; border-collapse:collapse; }
  th,td { text-align:left; padding:11px 10px; border-bottom:1px solid var(--line); font-size:14px; vertical-align:middle; }
  th { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.3px; }
  .dot { display:inline-block; width:11px; height:11px; border-radius:50%; margin-right:7px; vertical-align:middle; }
  .dot.green{background:var(--ok)} .dot.amber{background:var(--amber)} .dot.red{background:var(--red)}
  .grade { display:inline-block; min-width:22px; text-align:center; padding:2px 6px; border-radius:6px;
    font-size:12px; font-weight:700; background:#eef2f0; margin-right:4px; cursor:pointer; }
  .grade.f,.grade.d { background:#f7dede; color:var(--red); }
  .grade.c { background:#fbf0d3; color:#8a6400; }
  .grade.a,.grade.b { background:#dff0e6; color:var(--ok); }
  .grade.none { background:#f0f0f0; color:#999; font-weight:600; }
  .coi-ok{color:var(--ok);font-weight:600} .coi-soon{color:var(--amber);font-weight:600} .coi-exp{color:var(--red);font-weight:600}
  .scope { font-size:12px; color:var(--muted); }
  .empty { color:var(--muted); font-size:14px; }
  .note { font-size:12px; color:var(--muted); margin-top:8px; }
  .err{color:var(--red);font-weight:600}.ok{color:var(--ok);font-weight:600}
  .hidden{display:none!important}
  dialog { border:0; border-radius:12px; padding:22px; max-width:420px; box-shadow:0 10px 40px rgba(0,0,0,.2); }
  dialog h3 { margin:0 0 10px; font-size:16px; }
  dialog .actions { display:flex; gap:10px; justify-content:flex-end; margin-top:16px; }
  dialog button { padding:8px 14px; border-radius:8px; border:1px solid var(--line); background:#fff; cursor:pointer; font-size:14px; }
  dialog button.danger { background:var(--red); color:#fff; border:0; font-weight:600; }
</style>
</head>
<body>
<header id="hdr" style="background:#1E7A46">
  <h1 id="gc-name">GC Workspace</h1>
  <div class="r">
    <span id="owner-badge" class="hidden">Owner view</span>
    <a id="back" class="hidden" href="/platform">← Owner console</a>
    <button id="signout" class="hidden" onclick="signout()">Sign out</button>
  </div>
</header>
<main id="main" class="hidden">
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
    <h2>Subcontractor roster</h2>
    <div id="roster"><div class="empty">Loading…</div></div>
    <div class="note">Click any grade cell to set or update that platform's grade. Health dot rolls up automatically (F/D = red, C = amber).</div>
  </div>
</main>

<dialog id="dlg-del">
  <h3>Remove subcontractor</h3>
  <div id="del-text"></div>
  <div class="note">This deletes the subcontractor and any login they have, so they lose access. This cannot be undone.</div>
  <div class="actions">
    <button onclick="document.getElementById('dlg-del').close()">Cancel</button>
    <button class="danger" id="del-confirm">Remove</button>
  </div>
</dialog>

<script>
const PARAMS = new URLSearchParams(location.search);
const GC = PARAMS.get('gc') || '';
let PLATFORMS = ['isn','avetta','veriforce','pec'];
function q(id){return document.getElementById(id);}
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function withGc(p){ return GC ? (p+(p.includes('?')?'&':'?')+'gc='+encodeURIComponent(GC)) : p; }
async function api(path, opts){
  const r = await fetch(path, Object.assign({headers:{'Content-Type':'application/json'}}, opts||{}));
  let d={}; try{d=await r.json();}catch(e){}
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

let STATE = null;
function render(data){
  STATE = data;
  PLATFORMS = data.platforms || PLATFORMS;
  const gc = data.gc;
  q('hdr').style.background = gc.brand_primary || '#1E7A46';
  q('gc-name').textContent = gc.name + ' — Workspace';
  if(data.is_owner){ q('owner-badge').classList.remove('hidden'); q('back').classList.remove('hidden'); }
  else { q('signout').classList.remove('hidden'); }
  q('main').classList.remove('hidden');
  drawRoster();
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
    return '<span class="'+cls+'" title="'+p.toUpperCase()+'" onclick="editGrade(\''+sub.id+'\',\''+p+'\')">'+(g||'+')+'</span>';
  }).join('');
}
function drawRoster(){
  const el = q('roster');
  const subs = STATE.subs||[];
  if(!subs.length){ el.innerHTML='<div class="empty">No subcontractors yet. Add your first one above.</div>'; return; }
  el.innerHTML = `<table><thead><tr>
      <th>Subcontractor</th><th>Grades ('+'')</th><th>COI</th><th>TRIR</th><th></th>
    </tr></thead><tbody>`+
    subs.map(s=>`<tr>
      <td><span class="dot ${esc(s.health)}"></span><b>${esc(s.name)}</b>
        <div class="scope">${(s.scope||[]).map(esc).join(', ')}</div></td>
      <td>${gradeCells(s)}</td>
      <td>${coiCell(s.coi)}</td>
      <td>${s.trir!=null?esc(String(s.trir)):'—'}</td>
      <td style="text-align:right"><button class="link" onclick="askDelete('${s.id}','${esc(s.name).replace(/'/g,"\\'")}')">Remove</button></td>
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
  reload();
}

async function editGrade(subId, platform){
  const cur = (STATE.subs.find(s=>s.id===subId).grades[platform]||'');
  const g = prompt('Grade for '+platform.toUpperCase()+' (A, B, C, D, F — blank to clear):', cur);
  if(g===null) return;
  const {ok,data}=await api('/platform/gc/subs/'+subId+'/grade',{method:'POST',
    body:JSON.stringify({gc:GC,platform,grade:g.trim()})});
  if(!ok){ alert(data.error||'Could not save grade'); return; }
  reload();
}

let PENDING_DEL=null;
function askDelete(id,name){
  PENDING_DEL=id;
  q('del-text').innerHTML='Remove <b>'+esc(name)+'</b> from your roster?';
  q('dlg-del').showModal();
}
q('del-confirm').onclick = async ()=>{
  if(!PENDING_DEL) return;
  const {ok,data}=await api(withGc('/platform/gc/subs/'+PENDING_DEL),{method:'DELETE'});
  q('dlg-del').close();
  if(!ok){ alert(data.error||'Could not remove'); return; }
  PENDING_DEL=null; reload();
};

async function reload(){ const {ok,data}=await api(withGc('/platform/gc/roster')); if(ok){ STATE=data; drawRoster(); } }
async function signout(){ await api('/platform/logout',{method:'POST'}); location.href='/platform'; }
boot();
</script>
</body>
</html>
"""
