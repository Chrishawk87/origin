"""Origin Abatement — submission / package builder (Phase 2, final engine link).

    SOURCE → FACT/CONDITION → REQUIREMENT → ACTION → EVIDENCE → VERIFICATION → **DECISION**

This module assembles the DECISION-stage artifact: a single, organized package
the attorney can review and — if THEY decide it is complete — file with OSHA. It
does NOT decide anything and it does NOT transmit anything. Every generated
document is watermarked **"DRAFT — FOR REVIEW"** and the only way a matter ever
becomes "submitted" is the attorney manually attesting "I filed this myself."

What the package gathers (all from existing engines, nothing re-derived):
  * The matter header (client, inspection, workflow status).
  * Each citation item with its regulatory source, requirement, and deadlines.
  * The corrective actions opened for each item (from capa.py).
  * The evidence linked to each item, with its verification state (evidence_vault).
  * The abatement Readiness indicator (clearly labeled NOT an OSHA determination).
  * A DRAFT abatement-certification letter anchored on 29 CFR 1903.19, with every
    attorney-owned field left blank for the attorney to complete and sign.

Hard guardrails enforced here:
  * Never auto-submits. There is no code path that contacts OSHA.
  * The "mark as submitted" route requires a firm actor with the 'submit'
    capability (attorney / firm admin) and is an ATTESTATION, not a transmission.
  * Every rendered document carries the DRAFT watermark and the not-legal-advice
    and readiness disclaimers.

House rules: deterministic, offline, file-based, isolated + non-fatal.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    from starlette.requests import Request
except Exception:  # pragma: no cover
    Request = Any  # type: ignore

from . import abatement_access as _access
from . import abatement_matter as _mm

DRAFT_WATERMARK = "DRAFT — FOR REVIEW"
DRAFT_NOTICE = (
    "This is a DRAFT compiled for attorney review. It has NOT been submitted to "
    "OSHA. Origin does not file anything on your behalf. The attorney is "
    "responsible for reviewing, completing, signing, and submitting any document."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _corrective_actions(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        from . import capa as _capa
    except Exception:
        return out
    for cid in item.get("corrective_action_ids") or []:
        try:
            rec = _capa.get(cid)
        except Exception:
            rec = None
        if rec:
            out.append(rec)
    return out


def build_package(matter_id: str) -> Dict[str, Any]:
    """Assemble the full review package for a matter. Read-only; never mutates."""
    rec = _mm.get(matter_id)
    if not rec:
        return {"ok": False, "error": "not found"}

    try:
        from . import evidence_vault as _ev
        ev_all = _ev.list_evidence(matter_id)
    except Exception:
        ev_all = []
    ev_by_item: Dict[str, List[Dict[str, Any]]] = {}
    for e in ev_all:
        ev_by_item.setdefault(e.get("citation_item_id", ""), []).append(e)

    items_out: List[Dict[str, Any]] = []
    for it in rec.get("citation_items", []):
        iid = it.get("item_id", "")
        items_out.append({
            "item_id": iid,
            "standard": it.get("standard", ""),
            "classification": it.get("classification", ""),
            "alleged_condition": it.get("alleged_condition", ""),
            "proposed_penalty": it.get("proposed_penalty", ""),
            "verified": bool(it.get("verified")),
            "extraction_flag": it.get("extraction_flag", ""),
            "regulatory": it.get("regulatory", {}),
            "requirement": it.get("requirement", {}),
            "deadlines": it.get("deadlines", {}),
            "corrective_actions": _corrective_actions(it),
            "evidence": ev_by_item.get(iid, []),
        })

    try:
        ready = _mm.readiness(matter_id)
    except Exception as exc:  # pragma: no cover
        ready = {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "watermark": DRAFT_WATERMARK,
        "draft_notice": DRAFT_NOTICE,
        "disclaimer": _mm.DISCLAIMER_NOT_LEGAL,
        "abatement_standard": _mm.ABATEMENT_STANDARD,
        "matter": {
            "id": rec.get("id"),
            "client_name": rec.get("client_name", ""),
            "osha_inspection_number": rec.get("osha_inspection_number", ""),
            "citation_issued_date": rec.get("citation_issued_date", ""),
            "citation_received_date": rec.get("citation_received_date", ""),
            "status": rec.get("status", ""),
            "status_label": _mm.MATTER_STAGE_LABELS.get(rec.get("status", ""), rec.get("status", "")),
        },
        "items": items_out,
        "readiness": ready,
        "certification_draft": _certification_fields(rec),
        "built_at": _now(),
    }


def _certification_fields(rec: Dict[str, Any]) -> Dict[str, Any]:
    """The blank-field skeleton of a 1903.19 abatement certification letter. Every
    attorney/employer-owned value is intentionally left empty to be completed and
    signed by a human — Origin never fills legal signatory fields."""
    return {
        "anchored_on": _mm.ABATEMENT_STANDARD,
        "employer_name": rec.get("client_name", ""),
        "inspection_number": rec.get("osha_inspection_number", ""),
        "citation_number": "",         # attorney completes
        "signatory_name": "",          # attorney/employer completes
        "signatory_title": "",
        "date_signed": "",
        "statement": ("The employer certifies that the cited condition(s) have "
                      "been abated. [Attorney to review, complete, and sign.]"),
        "note": "DRAFT skeleton — verify against the citation and 29 CFR 1903.19.",
    }


# ── printable HTML render (for the attorney to review / print to PDF) ────────────
def _esc(s: Any) -> str:
    return html.escape("" if s is None else str(s))


def render_html(pkg: Dict[str, Any]) -> str:
    if not pkg.get("ok"):
        return "<!doctype html><meta charset=utf-8><p>Matter not found.</p>"
    m = pkg["matter"]
    rows = []
    for it in pkg["items"]:
        reg = it.get("regulatory", {}) or {}
        dl = it.get("deadlines", {}) or {}
        ev = it.get("evidence", []) or []
        cas = it.get("corrective_actions", []) or []
        ev_html = "".join(
            f"<li>{_esc(e.get('original_filename'))} — "
            f"<b>{_esc(e.get('verification_status'))}</b>"
            f"{' (conflicting)' if e.get('conflicting') else ''}</li>"
            for e in ev) or "<li><i>No evidence attached.</i></li>"
        ca_html = "".join(
            f"<li>{_esc(c.get('corrective_action') or c.get('title') or c.get('id'))}</li>"
            for c in cas) or "<li><i>No corrective action recorded.</i></li>"
        rows.append(f"""
        <div class="item">
          <div class="std">{_esc(it.get('standard'))}
            <span class="cls">{_esc(it.get('classification'))}</span>
            {'<span class="vf">verified</span>' if it.get('verified') else '<span class="uv">unverified</span>'}
          </div>
          <div class="cond">{_esc(it.get('alleged_condition'))}</div>
          <div class="src">Source: {_esc(reg.get('title') or reg.get('anchor_title') or '—')}
            {('· ' + _esc(reg.get('jurisdiction'))) if reg.get('jurisdiction') else ''}</div>
          <div class="dl">Abatement date: {_esc(dl.get('abatement_date') or '—')} ·
            Certification due: {_esc(dl.get('certification_due') or '—')} ·
            Contest deadline: {_esc(dl.get('contest_deadline') or '—')}
            <div class="tiny">{_esc(dl.get('disclaimer') or _mm.DEADLINE_DISCLAIMER)}</div></div>
          <div class="sub">Corrective actions</div><ul>{ca_html}</ul>
          <div class="sub">Evidence</div><ul>{ev_html}</ul>
        </div>""")

    ready = pkg.get("readiness", {}) or {}
    cert = pkg.get("certification_draft", {}) or {}
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Abatement package (DRAFT) — {_esc(m.get('client_name'))}</title>
<style>
  body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#111;max-width:800px;margin:24px auto;padding:0 16px;}}
  .wm{{position:fixed;top:8px;right:8px;background:#b00020;color:#fff;font-weight:800;
       padding:6px 12px;border-radius:6px;font-size:12px;letter-spacing:1px;}}
  .banner{{background:#fff3cd;border:1px solid #e0c96b;border-radius:8px;padding:10px 12px;font-size:13px;margin:10px 0;}}
  h1{{font-size:20px;margin:8px 0;}} h2{{font-size:16px;border-bottom:1px solid #ddd;padding-bottom:4px;margin-top:22px;}}
  .item{{border:1px solid #ddd;border-radius:8px;padding:12px;margin:10px 0;}}
  .std{{font-weight:700;}} .cls{{color:#555;font-weight:400;margin-left:6px;}}
  .vf{{color:#137a3a;font-size:11px;margin-left:6px;}} .uv{{color:#a06a00;font-size:11px;margin-left:6px;}}
  .cond{{margin:4px 0;}} .src,.dl{{color:#444;font-size:12px;margin:3px 0;}}
  .tiny{{color:#888;font-size:11px;}} .sub{{font-weight:600;margin-top:8px;font-size:13px;}}
  ul{{margin:4px 0 4px 18px;}} .cert td{{padding:4px 8px;border-bottom:1px solid #eee;}}
  .foot{{color:#888;font-size:11px;margin-top:24px;}}
</style></head><body>
<div class="wm">{_esc(pkg.get('watermark'))}</div>
<h1>OSHA Abatement Package — {_esc(m.get('client_name'))}</h1>
<div class="banner"><b>{_esc(pkg.get('watermark'))}.</b> {_esc(pkg.get('draft_notice'))}</div>
<div class="banner">{_esc(pkg.get('disclaimer'))}</div>
<p>Inspection: <b>{_esc(m.get('osha_inspection_number') or '—')}</b> ·
   Citation issued: {_esc(m.get('citation_issued_date') or '—')} ·
   Received: {_esc(m.get('citation_received_date') or '—')} ·
   Workflow status: {_esc(m.get('status_label'))}</p>

<h2>Readiness (internal indicator — not an OSHA determination)</h2>
<p>Score: <b>{_esc(ready.get('score'))}</b> — {_esc(ready.get('band'))}
   <span class="tiny">{_esc(ready.get('disclaimer') or _mm.READINESS_DISCLAIMER)}</span></p>

<h2>Citation items ({len(pkg.get('items', []))})</h2>
{''.join(rows) if rows else '<p><i>No citation items.</i></p>'}

<h2>Draft abatement certification (29 CFR 1903.19)</h2>
<p class="tiny">{_esc(cert.get('note'))}</p>
<table class="cert">
  <tr><td>Employer</td><td>{_esc(cert.get('employer_name') or '________')}</td></tr>
  <tr><td>Inspection #</td><td>{_esc(cert.get('inspection_number') or '________')}</td></tr>
  <tr><td>Citation #</td><td>________ <span class="tiny">(attorney completes)</span></td></tr>
  <tr><td>Statement</td><td>{_esc(cert.get('statement'))}</td></tr>
  <tr><td>Signatory</td><td>________________________ <span class="tiny">(name / title)</span></td></tr>
  <tr><td>Date signed</td><td>____________</td></tr>
</table>

<div class="foot">Compiled {_esc(pkg.get('built_at'))}. {_esc(pkg.get('draft_notice'))}</div>
</body></html>"""


# ── routes ─────────────────────────────────────────────────────────────────────
def register_abatement_submissions(app) -> None:
    """Attach submission routes. Isolated + non-fatal."""
    from fastapi import Body
    from fastapi.responses import HTMLResponse, JSONResponse

    @app.get("/api/abatement/matters/{matter_id}/submission")
    def ab_submission(matter_id: str):
        pkg = build_package(matter_id)
        if not pkg.get("ok"):
            return JSONResponse(pkg, status_code=404)
        return pkg

    @app.get("/api/abatement/matters/{matter_id}/submission.html",
             response_class=HTMLResponse)
    def ab_submission_html(matter_id: str):
        pkg = build_package(matter_id)
        return HTMLResponse(render_html(pkg),
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/abatement/matters/{matter_id}/mark-submitted")
    def ab_mark_submitted(matter_id: str, request: Request, body: dict = Body(default=None)):
        # ATTESTATION ONLY — never transmits to OSHA. Requires the 'submit' cap
        # (attorney / firm admin). Records that a human filed it themselves.
        a = _access.resolve_actor(request)
        if a["kind"] != "firm" or not _access.can(a["role"], "submit"):
            return JSONResponse(
                {"error": "Only an attorney or firm admin can mark a matter as filed."},
                status_code=403)
        p = body if isinstance(body, dict) else {}
        rec = _mm.set_status(matter_id, "submitted", by=a.get("name") or a["role"])
        if not rec:
            return JSONResponse({"error": "not found or invalid status"}, status_code=400)
        note = (p.get("note") or "").strip()
        _mm.add_note(matter_id,
                     f"Attorney attested the package was filed with OSHA. {note}".strip(),
                     by=a.get("name") or a["role"])
        return {"ok": True, "matter": rec, "note": "Marked as filed (attestation only). "
                "Origin did not submit anything to OSHA."}
