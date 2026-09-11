"""Origin Abatement — client tasks & client portal (Phase 2).

The firm turns each abatement obligation into concrete WORK ITEMS the cited
employer (the client) can actually do, and gives the client a single, no-account
page to do them on. Four kinds of task mirror the engine's ACTION → EVIDENCE
links:

    fix       — physically correct the cited condition.
    document  — produce a record/policy that proves compliance.
    upload    — attach a photo/document as evidence.
    verify    — (firm-only close-out) confirm the item is abated. Clients never
                perform this — verification is the attorney's call.

Task lifecycle:  open → submitted (client did their part) → verified (firm
confirmed) → closed. A client can only move a task to 'submitted' and attach
evidence; they can NEVER verify, change legal status, or submit to OSHA. Those
guardrails live in abatement_access.py and are enforced on every route here.

House rules: deterministic, offline, file-based (ORIGIN_DATA_DIR/abatement/tasks
/{matter_id}.json), isolated + non-fatal registration. Evidence is stored through
the existing Evidence Vault — this module never re-implements file storage.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from starlette.requests import Request
except Exception:  # pragma: no cover
    Request = Any  # type: ignore

# Under `from __future__ import annotations` FastAPI resolves route type hints
# against this module's globals, so UploadFile/File/Form must live at module
# scope (not merely inside register_*) or the multipart upload route can't build.
try:
    from fastapi import UploadFile, File, Form
except Exception:  # pragma: no cover
    UploadFile = Any  # type: ignore
    def File(*a, **k):  # type: ignore
        return None
    def Form(*a, **k):  # type: ignore
        return None

from . import abatement_access as _access

try:
    from .paths import DATA_DIR
except ImportError:  # pragma: no cover
    import os as _os
    DATA_DIR = Path(_os.environ.get("ORIGIN_DATA_DIR") or (Path.home() / ".origin"))

TASKS_DIR = DATA_DIR / "abatement" / "tasks"

TASK_KINDS = ("fix", "document", "upload", "verify")
TASK_STATES = ("open", "submitted", "verified", "closed")
TASK_KIND_LABELS = {
    "fix": "Fix the condition",
    "document": "Produce a document",
    "upload": "Upload evidence",
    "verify": "Firm verification",
}
CLIENT_DISCLAIMER = (
    "This page organizes the work your attorney has asked you to complete and "
    "lets you attach documents and photos. It is not legal advice, and nothing "
    "here is submitted to OSHA automatically — your attorney reviews everything."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(matter_id: str) -> Path:
    return TASKS_DIR / f"{matter_id}.json"


def _read(matter_id: str) -> List[Dict[str, Any]]:
    f = _path(matter_id)
    if not f.is_file():
        return []
    try:
        return json.loads(f.read_text() or "[]")
    except Exception:
        return []


def _write(matter_id: str, rows: List[Dict[str, Any]]) -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    _path(matter_id).write_text(json.dumps(rows, indent=2))


# ── core operations ────────────────────────────────────────────────────────────
def create_task(matter_id: str, *, kind: str = "fix", title: str = "",
                detail: str = "", citation_item_id: str = "",
                assigned_role: str = _access.CLIENT_ADMIN, by: str = "") -> Dict[str, Any]:
    if kind not in TASK_KINDS:
        kind = "fix"
    rec = {
        "task_id": "task-" + uuid.uuid4().hex[:10],
        "matter_id": matter_id,
        "citation_item_id": (citation_item_id or "").strip(),
        "kind": kind,
        "title": (title or TASK_KIND_LABELS.get(kind, "Task")).strip(),
        "detail": (detail or "").strip(),
        "status": "open",
        "assigned_role": assigned_role,
        "evidence_ids": [],
        "client_note": "",
        "created_at": _now(),
        "created_by": by or "",
        "updated_at": _now(),
        "submitted_at": "",
        "submitted_by": "",
        "verified_at": "",
        "verified_by": "",
    }
    rows = _read(matter_id)
    rows.insert(0, rec)
    _write(matter_id, rows)
    return rec


def list_tasks(matter_id: str) -> List[Dict[str, Any]]:
    return _read(matter_id)


def get_task(matter_id: str, task_id: str) -> Optional[Dict[str, Any]]:
    for r in _read(matter_id):
        if r.get("task_id") == task_id:
            return r
    return None


def _update(matter_id: str, task_id: str, mutate) -> Optional[Dict[str, Any]]:
    rows = _read(matter_id)
    for i, r in enumerate(rows):
        if r.get("task_id") == task_id:
            mutate(r)
            r["updated_at"] = _now()
            rows[i] = r
            _write(matter_id, rows)
            return r
    return None


def client_submit_task(matter_id: str, task_id: str, *, note: str = "",
                       evidence_ids: Optional[List[str]] = None,
                       by: str = "client") -> Optional[Dict[str, Any]]:
    """A client marks their part done. This can NEVER verify — it only moves the
    task to 'submitted' and records the client's note/evidence for firm review."""
    def _m(r):
        r["status"] = "submitted"
        r["submitted_at"] = _now()
        r["submitted_by"] = by or "client"
        if note:
            r["client_note"] = note.strip()
        if evidence_ids:
            have = set(r.get("evidence_ids") or [])
            r["evidence_ids"] = list(have.union(evidence_ids))
    return _update(matter_id, task_id, _m)


def verify_task(matter_id: str, task_id: str, *, by: str = "attorney") -> Optional[Dict[str, Any]]:
    """Firm-only: confirm the task is satisfied. Caller must hold 'verify'."""
    def _m(r):
        r["status"] = "verified"
        r["verified_at"] = _now()
        r["verified_by"] = by or "attorney"
    return _update(matter_id, task_id, _m)


def close_task(matter_id: str, task_id: str, *, by: str = "") -> Optional[Dict[str, Any]]:
    def _m(r):
        r["status"] = "closed"
    return _update(matter_id, task_id, _m)


def reopen_task(matter_id: str, task_id: str, *, by: str = "") -> Optional[Dict[str, Any]]:
    def _m(r):
        r["status"] = "open"
        r["submitted_at"] = ""
        r["submitted_by"] = ""
        r["verified_at"] = ""
        r["verified_by"] = ""
    return _update(matter_id, task_id, _m)


def summarize(matter_id: str) -> Dict[str, int]:
    rows = _read(matter_id)
    out = {s: 0 for s in TASK_STATES}
    for r in rows:
        st = r.get("status", "open")
        out[st] = out.get(st, 0) + 1
    out["total"] = len(rows)
    return out


# ── client portal page (no account, self-contained) ─────────────────────────────
_CLIENT_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Your abatement tasks — ORIGIN</title>
<style>
  :root{ --bg:#0b1220; --card:#111a2e; --line:#2a3a5c; --ink:#f2f6ff;
         --muted:#aab9d6; --accent:#1E7A46; --danger:#ff7b7b; --ok:#5fd39a; --warn:#e8b04b; }
  *{ box-sizing:border-box; }
  html,body{ margin:0; min-height:100%; background:var(--bg); color:var(--ink);
    font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  .wrap{ max-width:720px; margin:0 auto; padding:20px 16px 60px; }
  .word{ font-size:22px; font-weight:800; letter-spacing:2px; }
  .word span{ color:var(--accent); }
  h1{ font-size:20px; margin:14px 0 2px; }
  .sub{ color:var(--muted); font-size:13px; }
  .note{ background:#10203a; border:1px solid var(--line); border-radius:12px;
         padding:12px 14px; font-size:13px; color:var(--muted); margin:16px 0; }
  .card{ background:var(--card); border:1px solid var(--line); border-radius:14px;
         padding:16px; margin:12px 0; }
  .kind{ font-size:11px; text-transform:uppercase; letter-spacing:.7px; color:var(--muted); }
  .title{ font-weight:700; font-size:16px; margin:3px 0; }
  .detail{ color:var(--muted); font-size:14px; white-space:pre-wrap; }
  .pill{ display:inline-block; font-size:11px; font-weight:700; padding:3px 9px;
         border-radius:999px; border:1px solid var(--line); }
  .pill.open{ color:var(--warn); border-color:#5a4a1e; }
  .pill.submitted{ color:#8fb7ff; border-color:#274067; }
  .pill.verified{ color:var(--ok); border-color:#1f5a3a; }
  .pill.closed{ color:var(--muted); }
  label{ display:block; font-size:12px; color:var(--muted); margin:10px 0 5px; }
  textarea,input[type=file]{ width:100%; padding:10px; border-radius:10px;
    border:1px solid var(--line); background:#0c1526; color:var(--ink); font-size:14px; }
  .btn{ margin-top:10px; padding:11px 16px; border:0; border-radius:10px;
        background:var(--accent); color:#fff; font-weight:700; font-size:14px; cursor:pointer; }
  .btn.cam{ display:block; width:100%; text-align:center; font-size:16px; padding:14px; }
  .btn.ghost{ background:transparent; border:1px solid var(--line); color:var(--ink); font-weight:600; }
  .btn[disabled]{ opacity:.5; }
  .msg{ font-size:13px; margin-top:8px; color:var(--ok); }
  .empty{ color:var(--muted); text-align:center; padding:30px 0; }
  .done{ opacity:.7; }
  .thumbs{ display:flex; flex-wrap:wrap; gap:10px; margin:10px 0; }
  .thumb{ width:100px; }
  .thumb img{ width:100px; height:100px; object-fit:cover; border-radius:10px;
    border:1px solid var(--line); background:#0c1526; display:block; }
  .audit{ font-size:10px; color:var(--muted); margin-top:3px; line-height:1.35; }
  .seal{ color:var(--ok); font-weight:700; }
  .hide{ position:absolute; width:1px; height:1px; opacity:0; pointer-events:none; }
  .filename{ font-size:12px; color:var(--muted); margin-top:6px; }
</style></head>
<body><div class="wrap">
  <div class="word">ORIGIN<span>.</span></div>
  <h1 id="mtitle">Your abatement tasks</h1>
  <div class="sub" id="msub"></div>
  <div class="note" id="disc"></div>
  <div id="list"><div class="empty">Loading…</div></div>
</div>
<script>
(function(){
  var listEl=document.getElementById("list");
  function esc(s){var d=document.createElement("div");d.textContent=s==null?"":String(s);return d.innerHTML;}
  function api(path,opts){return fetch(path,Object.assign({credentials:"same-origin"},opts||{}))
    .then(function(r){return r.json().then(function(j){return {ok:r.ok,status:r.status,j:j};});});}

  function fmtWhen(s){ if(!s) return ""; try{ var d=new Date(s); if(!isNaN(d)) return d.toLocaleString(); }catch(e){} return s; }

  function thumbs(ev){
    if(!ev||!ev.length) return "";
    var h='<div class="thumbs">';
    ev.forEach(function(e){
      var isImg=(e.kind==="photo");
      var seal=e.sealed?'<span class="seal">&#128274; Sealed</span>':'';
      var when=e.captured_at?(' &middot; '+esc(fmtWhen(e.captured_at))):'';
      var gps=(e.gps&&e.gps.lat!=null)?('<br>&#128205; '+esc(e.gps.lat)+', '+esc(e.gps.lng)):'';
      var vr=(e.verification_status==="verified")?'<br>&#10003; Verified by attorney':'';
      h+='<div class="thumb">';
      if(isImg){ h+='<a href="'+esc(e.file_url)+'" target="_blank"><img src="'+esc(e.file_url)+'" alt="evidence"></a>'; }
      else { h+='<a class="filename" href="'+esc(e.file_url)+'" target="_blank">&#128196; '+esc(e.original_filename||"file")+'</a>'; }
      h+='<div class="audit">'+seal+when+gps+vr+'</div>';
      h+='</div>';
    });
    h+='</div>';
    return h;
  }

  function card(t){
    var done = (t.status==="verified"||t.status==="closed");
    var can = !done;
    var h='<div class="card'+(done?' done':'')+'" id="c-'+esc(t.task_id)+'">';
    h+='<div class="kind">'+esc(t.kind_label||t.kind)+'</div>';
    h+='<div class="title">'+esc(t.title)+'</div>';
    if(t.detail) h+='<div class="detail">'+esc(t.detail)+'</div>';
    h+='<div style="margin:8px 0"><span class="pill '+esc(t.status)+'">'+esc(t.status)+'</span></div>';
    if(t.client_note) h+='<div class="detail"><b>Your note:</b> '+esc(t.client_note)+'</div>';
    h+=thumbs(t.evidence);
    if(can){
      // Rear-camera capture on phones. Each photo is date-stamped, GPS-located,
      // and sealed into a tamper-evident audit trail the moment it's uploaded.
      h+='<input class="hide" type="file" accept="image/*" capture="environment" id="cam-'+esc(t.task_id)+'" onchange="fileChosen(\\''+esc(t.task_id)+'\\',\\'cam\\')">';
      h+='<input class="hide" type="file" id="doc-'+esc(t.task_id)+'" onchange="fileChosen(\\''+esc(t.task_id)+'\\',\\'doc\\')">';
      h+='<button class="btn cam" onclick="document.getElementById(\\'cam-'+esc(t.task_id)+'\\').click()">&#128247; Take photo of the completed fix</button>';
      h+='<button class="btn ghost" onclick="document.getElementById(\\'doc-'+esc(t.task_id)+'\\').click()">Attach a document instead</button>';
      h+='<div class="filename" id="chosen-'+esc(t.task_id)+'"></div>';
      h+='<label>Add a note for your attorney (optional)</label>';
      h+='<textarea id="n-'+esc(t.task_id)+'" rows="2" placeholder="e.g. Guardrail installed 9/12"></textarea>';
      h+='<button class="btn" onclick="submitTask(\\''+esc(t.task_id)+'\\')">Mark done &amp; send to attorney</button>';
      h+='<div class="msg" id="m-'+esc(t.task_id)+'"></div>';
    }
    h+='</div>';
    return h;
  }

  // Which input holds the pending file for a task, and its intended role.
  var pending={};
  window.fileChosen=function(id,which){
    var inp=document.getElementById((which==="cam"?"cam-":"doc-")+id);
    if(!inp||!inp.files||!inp.files.length){ return; }
    pending[id]={input:inp, role:(which==="cam"?"after":"supporting")};
    var lbl=document.getElementById("chosen-"+id);
    if(lbl) lbl.textContent="Ready to send: "+inp.files[0].name;
  };

  // Grab a live GPS fix at the moment of capture. Non-blocking: if the client
  // denies location or it times out, we still upload (GPS just won't be stamped).
  function getGeo(){
    return new Promise(function(resolve){
      if(!navigator.geolocation){ resolve(""); return; }
      var done=false;
      var t=setTimeout(function(){ if(!done){ done=true; resolve(""); } }, 8000);
      navigator.geolocation.getCurrentPosition(function(p){
        if(done) return; done=true; clearTimeout(t);
        resolve(JSON.stringify({lat:p.coords.latitude, lng:p.coords.longitude, accuracy:p.coords.accuracy}));
      }, function(){ if(!done){ done=true; clearTimeout(t); resolve(""); } },
      {enableHighAccuracy:true, timeout:7000, maximumAge:0});
    });
  }

  window.submitTask=function(id){
    var msg=document.getElementById("m-"+id);
    var note=(document.getElementById("n-"+id)||{}).value||"";
    var btn=document.querySelector("#c-"+id+" .btn:not(.cam):not(.ghost)"); if(btn) btn.disabled=true;
    msg.textContent="Getting location…";
    var p=pending[id];
    var upload;
    if(p && p.input && p.input.files && p.input.files.length){
      upload=getGeo().then(function(geo){
        msg.textContent="Uploading & sealing…";
        var fd=new FormData();
        fd.append("file",p.input.files[0]);
        fd.append("caption",note);
        fd.append("role",p.role);
        fd.append("capture_source", p.role==="after"?"in_app_camera":"in_app_upload");
        fd.append("captured_at", new Date().toISOString());
        if(geo) fd.append("gps",geo);
        return api("/api/abatement/client/upload",{method:"POST",body:fd})
          .then(function(r){return (r.j&&r.j.evidence_id)?[r.j.evidence_id]:[];});
      });
    } else { upload=Promise.resolve([]); }
    upload.then(function(evIds){
      msg.textContent="Sending to attorney…";
      return api("/api/abatement/client/tasks/"+id+"/submit",{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({note:note,evidence_ids:evIds})});
    }).then(function(r){
      if(r.ok&&r.j&&r.j.ok){ delete pending[id]; load(); }
      else { msg.textContent=(r.j&&r.j.error)||"Could not send."; if(btn)btn.disabled=false; }
    }).catch(function(){ msg.textContent="Network error."; if(btn)btn.disabled=false; });
  };

  function load(){
    api("/api/abatement/client/matter").then(function(r){
      if(!r.ok||!r.j||!r.j.ok){
        listEl.innerHTML='<div class="empty">Your access link has expired or is invalid. Please ask your attorney for a new link.</div>';
        document.getElementById("disc").textContent="";
        return;
      }
      document.getElementById("mtitle").textContent=r.j.client_name?("Abatement tasks — "+r.j.client_name):"Your abatement tasks";
      document.getElementById("msub").textContent=r.j.osha_inspection_number?("OSHA inspection "+r.j.osha_inspection_number):"";
      document.getElementById("disc").textContent=r.j.disclaimer||"";
      var tasks=r.j.tasks||[];
      if(!tasks.length){ listEl.innerHTML='<div class="empty">No tasks assigned yet. Your attorney will add items here.</div>'; return; }
      listEl.innerHTML=tasks.map(card).join("");
    });
  }
  load();
})();
</script>
</body></html>"""


# ── routes ─────────────────────────────────────────────────────────────────────
def register_abatement_tasks(app) -> None:
    """Attach client-task + client-portal routes. Isolated + non-fatal."""
    from fastapi import Body
    from fastapi.responses import HTMLResponse, JSONResponse

    from . import abatement_matter as _mm
    from . import evidence_vault as _vault

    _NO_STORE = {"Cache-Control": "no-store, must-revalidate",
                 "Pragma": "no-cache", "Expires": "0"}

    def _view(r: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(r)
        out["kind_label"] = TASK_KIND_LABELS.get(r.get("kind", ""), r.get("kind", ""))
        return out

    # ---- firm side: create / list / verify / close ----
    @app.post("/api/abatement/matters/{matter_id}/tasks")
    def ab_task_create(matter_id: str, request: Request, body: dict = Body(default=None)):
        a = _access.resolve_actor(request)
        if a["kind"] != "firm" or not _access.can(a["role"], "manage"):
            return JSONResponse({"error": "Only firm staff can create tasks."}, status_code=403)
        if not _mm.get(matter_id):
            return JSONResponse({"error": "matter not found"}, status_code=404)
        p = body if isinstance(body, dict) else {}
        rec = create_task(matter_id, kind=p.get("kind", "fix"), title=p.get("title", ""),
                          detail=p.get("detail", ""),
                          citation_item_id=p.get("citation_item_id", ""),
                          assigned_role=p.get("assigned_role", _access.CLIENT_ADMIN),
                          by=a.get("name") or a["role"])
        return {"ok": True, "task": _view(rec)}

    @app.get("/api/abatement/matters/{matter_id}/tasks")
    def ab_task_list(matter_id: str, request: Request):
        a = _access.resolve_actor(request)
        # Firm sees any matter; a client only their own token matter.
        if a["kind"] == "client" and a["matter_id"] != matter_id:
            return JSONResponse({"error": "not authorized for this matter"}, status_code=403)
        return {"ok": True, "tasks": [_view(t) for t in list_tasks(matter_id)],
                "summary": summarize(matter_id)}

    @app.post("/api/abatement/matters/{matter_id}/tasks/{task_id}/verify")
    def ab_task_verify(matter_id: str, task_id: str, request: Request):
        a = _access.resolve_actor(request)
        if not _access.can(a["role"], "verify"):
            return JSONResponse(
                {"error": "Only an attorney or firm admin can verify a task."},
                status_code=403)
        rec = verify_task(matter_id, task_id, by=a.get("name") or a["role"])
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"ok": True, "task": _view(rec)}

    @app.post("/api/abatement/matters/{matter_id}/tasks/{task_id}/close")
    def ab_task_close(matter_id: str, task_id: str, request: Request):
        a = _access.resolve_actor(request)
        if a["kind"] != "firm" or not _access.can(a["role"], "manage"):
            return JSONResponse({"error": "Only firm staff can close tasks."}, status_code=403)
        rec = close_task(matter_id, task_id, by=a.get("name") or a["role"])
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"ok": True, "task": _view(rec)}

    # ---- client side (per-matter token) ----
    @app.get("/abatement/client", response_class=HTMLResponse)
    def ab_client_page():
        return HTMLResponse(_CLIENT_PAGE, headers=_NO_STORE)

    def _ev_view(e: Dict[str, Any]) -> Dict[str, Any]:
        """The safe, client-facing shape of an evidence record (their own matter).
        Exposes the audit facts that make it deposition-ready, hides nothing
        sensitive — it's the client's own upload."""
        a = e.get("audit", {}) or {}
        return {
            "evidence_id": e.get("evidence_id", ""),
            "citation_item_id": e.get("citation_item_id", ""),
            "kind": e.get("kind", ""),
            "role": e.get("role", ""),
            "original_filename": e.get("original_filename", ""),
            "caption": e.get("caption", ""),
            "verification_status": e.get("verification_status", "unverified"),
            "file_url": f"/api/abatement/client/evidence/{e.get('evidence_id','')}/file",
            "captured_at": a.get("captured_at", ""),
            "gps": a.get("gps"),
            "sha256": e.get("sha256", ""),
            "sealed": bool(a.get("seal")),
        }

    @app.get("/api/abatement/client/matter")
    def ab_client_matter(request: Request):
        mid = _access.client_scope_matter(request)
        if not mid:
            return JSONResponse({"error": "no client session"}, status_code=401)
        rec = _mm.get(mid)
        if not rec:
            return JSONResponse({"error": "matter not found"}, status_code=404)
        # Group the client's own uploaded evidence by task so photos are visible.
        ev_by_task: Dict[str, List[Dict[str, Any]]] = {}
        all_ev = {e.get("evidence_id"): e for e in _vault.list_evidence(mid)}
        tasks = list_tasks(mid)
        for t in tasks:
            shown = [all_ev[i] for i in (t.get("evidence_ids") or []) if i in all_ev]
            ev_by_task[t["task_id"]] = [_ev_view(e) for e in shown]
        return {"ok": True, "matter_id": mid,
                "client_name": rec.get("client_name", ""),
                "osha_inspection_number": rec.get("osha_inspection_number", ""),
                "disclaimer": CLIENT_DISCLAIMER,
                "tasks": [dict(_view(t), evidence=ev_by_task.get(t["task_id"], []))
                          for t in tasks]}

    @app.get("/api/abatement/client/evidence/{evidence_id}/file")
    def ab_client_evidence_file(evidence_id: str, request: Request):
        # A client may only fetch files that belong to their own token matter.
        mid = _access.client_scope_matter(request)
        if not mid:
            return JSONResponse({"error": "no client session"}, status_code=401)
        data, rec = _vault.get_file(mid, evidence_id)
        if data is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        import mimetypes
        from fastapi.responses import Response
        ctype = mimetypes.guess_type(rec.get("original_filename", ""))[0] or "application/octet-stream"
        return Response(content=data, media_type=ctype, headers=_NO_STORE)

    @app.post("/api/abatement/client/upload")
    async def ab_client_upload(request: Request, file: UploadFile = File(...),
                               caption: str = Form(""),
                               citation_item_id: str = Form(""),
                               role: str = Form("after"),
                               gps: str = Form(""),
                               captured_at: str = Form(""),
                               capture_source: str = Form("")):
        mid = _access.client_scope_matter(request)
        if not mid:
            return JSONResponse({"error": "no client session"}, status_code=401)
        content = await file.read()
        # A photo of a corrected hazard is "after" evidence by default; a plain
        # document upload can pass role=supporting. Client uploads always land
        # UNVERIFIED — only the firm can verify. The audit block (hash + capture
        # time + GPS + device) is sealed at write time so the trail is tamper-evident.
        rec = _vault.store_evidence(
            mid, content=content, filename=file.filename or "upload",
            citation_item_id=citation_item_id,
            role=(role or "after"), caption=caption, by="client",
            gps=_vault._parse_gps(gps), captured_at=captured_at,
            capture_source=(capture_source or "in_app_camera"),
            device=_vault._ua(request), client_ip=_vault._ip(request))
        return {"ok": True, "evidence_id": rec["evidence_id"],
                "captured_at": rec.get("audit", {}).get("captured_at", ""),
                "gps": rec.get("audit", {}).get("gps"),
                "sealed": bool(rec.get("audit", {}).get("seal"))}

    @app.post("/api/abatement/client/tasks/{task_id}/submit")
    def ab_client_submit(task_id: str, request: Request, body: dict = Body(default=None)):
        mid = _access.client_scope_matter(request)
        if not mid:
            return JSONResponse({"error": "no client session"}, status_code=401)
        p = body if isinstance(body, dict) else {}
        rec = client_submit_task(mid, task_id, note=p.get("note", ""),
                                 evidence_ids=p.get("evidence_ids") or [],
                                 by="client")
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"ok": True, "task": _view(rec)}
