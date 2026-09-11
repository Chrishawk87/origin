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
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

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

# The Notice of Intent to Contest is the letter an employer sends the OSHA Area
# Director WITHIN 15 WORKING DAYS of receiving a citation to contest it — the
# document that PRECEDES abatement, because a timely contest stays the abatement
# and penalty obligations pending review. Origin drafts a blank skeleton only;
# the attorney owns every legal decision, the wording of what is contested, and
# the signature. Nothing here is filed with OSHA.
NOTICE_TITLE = "Notice of Intent to Contest"
NOTICE_DISCLAIMER = (
    "DRAFT skeleton of a Notice of Intent to Contest. The 15-working-day contest "
    "window is strict and jurisdiction-sensitive — the attorney must verify the "
    "deadline against the citation/order, decide what (if anything) to contest, "
    "and complete, sign, and file the letter. Origin does not file anything."
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


def _gps_line(audit: Dict[str, Any]) -> str:
    """Human-readable GPS string from an evidence audit block, or '' if none."""
    g = (audit or {}).get("gps") or {}
    lat, lng = g.get("lat"), g.get("lng")
    if lat is None or lng is None:
        return ""
    acc = g.get("accuracy_m")
    src = g.get("source") or ""
    out = f"{lat}, {lng}"
    if acc is not None:
        out += f" (±{acc} m)"
    if src:
        out += f" · {src}"
    return out


def _photo_stamp(e: Dict[str, Any], matter_id: str) -> Dict[str, Any]:
    """The tamper-evident capture facts for one evidence item, plus a live
    integrity check. This is what makes an embedded photo defensible: when and
    where it was taken, that the bytes are unaltered, and that the seal holds."""
    audit = e.get("audit", {}) or {}
    stamp = {
        "captured_at": audit.get("captured_at", ""),
        "captured_src": audit.get("captured_at_source", ""),
        "gps": _gps_line(audit),
        "sha256_16": (e.get("sha256", "") or "")[:16],
        "sealed": bool(audit.get("seal")),
        "role": e.get("role", ""),
        "verification_status": e.get("verification_status", ""),
        "integrity_ok": None,
    }
    try:
        from . import evidence_vault as _ev
        integ = _ev.verify_integrity(matter_id, e.get("evidence_id", ""))
        stamp["integrity_ok"] = bool(integ.get("tamper_evident_ok"))
    except Exception:
        stamp["integrity_ok"] = None
    return stamp


def _evidence_html(matter_id: str, ev: List[Dict[str, Any]]) -> str:
    """Render an item's evidence. Photos are EMBEDDED as images with their sealed
    capture stamp (time, GPS, hash, integrity); other files list by name + state."""
    if not ev:
        return "<p class='ev-none'><i>No evidence attached.</i></p>"
    blocks: List[str] = []
    for e in ev:
        is_photo = (e.get("kind") == "photo" or e.get("classification") == "photo")
        name = _esc(e.get("original_filename"))
        vs = _esc(e.get("verification_status"))
        conflict = " · <span class='conflict'>conflicting</span>" if e.get("conflicting") else ""
        if is_photo:
            st = _photo_stamp(e, matter_id)
            src = (f"/api/abatement/matters/{_esc(matter_id)}/evidence/"
                   f"{_esc(e.get('evidence_id'))}/file")
            integ = ("<span class='ok'>integrity verified</span>"
                     if st["integrity_ok"] else
                     ("<span class='warn'>integrity check unavailable</span>"
                      if st["integrity_ok"] is None else
                      "<span class='conflict'>INTEGRITY FAILED — do not rely</span>"))
            gps_html = (f"<div class='stamp'>GPS: {_esc(st['gps'])}</div>"
                        if st["gps"] else
                        "<div class='stamp muted'>GPS: not recorded</div>")
            blocks.append(f"""
          <figure class="ev-photo">
            <img src="{src}" alt="{name}" loading="lazy">
            <figcaption>
              <div class="stamp"><b>{name}</b> · role: {_esc(st['role'] or '—')} · <b>{vs}</b>{conflict}</div>
              <div class="stamp">Captured: {_esc(st['captured_at'] or '—')}
                {('· ' + _esc(st['captured_src'])) if st['captured_src'] else ''}</div>
              {gps_html}
              <div class="stamp muted">SHA-256: {_esc(st['sha256_16'])}… ·
                {'sealed' if st['sealed'] else 'not sealed'} · {integ}</div>
            </figcaption>
          </figure>""")
        else:
            blocks.append(
                f"<div class='ev-doc'>{name} — <b>{vs}</b>{conflict}</div>")
    return "".join(blocks)


def render_html(pkg: Dict[str, Any]) -> str:
    if not pkg.get("ok"):
        return "<!doctype html><meta charset=utf-8><p>Matter not found.</p>"
    m = pkg["matter"]
    mid = m.get("id") or ""
    rows = []
    for it in pkg["items"]:
        reg = it.get("regulatory", {}) or {}
        dl = it.get("deadlines", {}) or {}
        ev = it.get("evidence", []) or []
        cas = it.get("corrective_actions", []) or []
        ev_html = _evidence_html(mid, ev)
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
          <div class="sub">Evidence</div><div class="ev-list">{ev_html}</div>
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
  .ev-list{{margin:4px 0;}}
  .ev-photo{{margin:8px 0;padding:8px;border:1px solid #e2e2e2;border-radius:8px;
             page-break-inside:avoid;break-inside:avoid;}}
  .ev-photo img{{max-width:340px;max-height:260px;width:auto;height:auto;
                 border:1px solid #ccc;border-radius:4px;display:block;}}
  .ev-photo figcaption{{margin-top:6px;}}
  .ev-doc{{margin:3px 0;font-size:13px;}}
  .ev-none{{color:#888;font-size:12px;}}
  .stamp{{font-size:11px;color:#333;line-height:1.4;}}
  .stamp.muted{{color:#888;}}
  .ok{{color:#137a3a;font-weight:600;}} .warn{{color:#a06a00;}}
  .conflict{{color:#b00020;font-weight:700;}}
  .toolbar{{margin:10px 0;}}
  .toolbar a{{display:inline-block;background:#111;color:#fff;text-decoration:none;
              padding:7px 12px;border-radius:6px;font-size:12px;margin-right:8px;}}
  @media print{{.toolbar{{display:none;}}}}
</style></head><body>
<div class="wm">{_esc(pkg.get('watermark'))}</div>
<h1>OSHA Abatement Package — {_esc(m.get('client_name'))}</h1>
<div class="toolbar">
  <a href="/api/abatement/matters/{_esc(mid)}/submission.pdf">Download package PDF</a>
  <a href="/api/abatement/matters/{_esc(mid)}/notice-of-intent.html">Notice of Intent to Contest</a>
</div>
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


# ── Notice of Intent to Contest (the document that precedes abatement) ───────────
def _notice_of_intent_fields(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Blank-field skeleton of a Notice of Intent to Contest letter. Every legal
    decision — WHAT is contested (citation items, penalties, and/or abatement
    dates), and the signature — is left for the attorney. Origin fills only the
    factual identifiers already on the matter and never asserts a legal position."""
    return {
        "to": "OSHA Area Director",              # attorney confirms the correct office
        "area_office": "",                       # attorney completes (address)
        "employer_name": rec.get("client_name", ""),
        "inspection_number": rec.get("osha_inspection_number", ""),
        "citation_issued_date": rec.get("citation_issued_date", ""),
        "citation_received_date": rec.get("citation_received_date", ""),
        "citation_numbers": "",                  # attorney completes
        "contesting": {
            "citations": False,                  # attorney decides each — all default OFF
            "penalties": False,
            "abatement_dates": False,
        },
        "statement": ("The above-named employer hereby notifies the Area Director "
                      "of its intent to contest [the citation(s) / the proposed "
                      "penalt(ies) / the abatement date(s) — attorney to specify] "
                      "issued in connection with the referenced inspection. "
                      "[Attorney to review, complete, and sign.]"),
        "signatory_name": "",                    # attorney/employer completes
        "signatory_title": "",
        "date_signed": "",
        "note": ("DRAFT skeleton — verify the 15-working-day contest deadline "
                 "against the citation/order before relying on it."),
    }


def build_notice(matter_id: str) -> Dict[str, Any]:
    """Assemble the Notice-of-Intent-to-Contest review package. Read-only.

    Surfaces the contest deadline (computed on each citation item) prominently,
    because the window is short and strict, and lists the citation items so the
    attorney can decide what to contest — but takes no legal position itself."""
    rec = _mm.get(matter_id)
    if not rec:
        return {"ok": False, "error": "not found"}

    items: List[Dict[str, Any]] = []
    contest_deadlines: List[str] = []
    for it in rec.get("citation_items", []):
        dl = it.get("deadlines", {}) or {}
        cd = dl.get("contest_deadline") or ""
        if cd:
            contest_deadlines.append(cd)
        items.append({
            "standard": it.get("standard", ""),
            "classification": it.get("classification", ""),
            "alleged_condition": it.get("alleged_condition", ""),
            "proposed_penalty": it.get("proposed_penalty", ""),
            "contest_deadline": cd,
            "contest_basis": dl.get("contest_basis", ""),
        })
    # The earliest contest deadline governs the whole citation — surface it.
    earliest = min(contest_deadlines) if contest_deadlines else ""

    return {
        "ok": True,
        "kind": "notice_of_intent_to_contest",
        "title": NOTICE_TITLE,
        "watermark": DRAFT_WATERMARK,
        "draft_notice": DRAFT_NOTICE,
        "disclaimer": _mm.DISCLAIMER_NOT_LEGAL,
        "notice_disclaimer": NOTICE_DISCLAIMER,
        "matter": {
            "id": rec.get("id"),
            "client_name": rec.get("client_name", ""),
            "osha_inspection_number": rec.get("osha_inspection_number", ""),
            "citation_issued_date": rec.get("citation_issued_date", ""),
            "citation_received_date": rec.get("citation_received_date", ""),
            "status": rec.get("status", ""),
            "status_label": _mm.MATTER_STAGE_LABELS.get(rec.get("status", ""), rec.get("status", "")),
        },
        "earliest_contest_deadline": earliest,
        "deadline_disclaimer": _mm.DEADLINE_DISCLAIMER,
        "items": items,
        "letter": _notice_of_intent_fields(rec),
        "built_at": _now(),
    }


def render_notice_html(notice: Dict[str, Any]) -> str:
    if not notice.get("ok"):
        return "<!doctype html><meta charset=utf-8><p>Matter not found.</p>"
    m = notice["matter"]
    lt = notice.get("letter", {}) or {}
    mid = m.get("id") or ""
    item_rows = "".join(
        f"<tr><td>{_esc(i.get('standard'))}</td>"
        f"<td>{_esc(i.get('classification'))}</td>"
        f"<td>{_esc(i.get('contest_deadline') or '—')}</td></tr>"
        for i in notice.get("items", [])) or \
        "<tr><td colspan='3'><i>No citation items on file.</i></td></tr>"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{_esc(notice.get('title'))} (DRAFT) — {_esc(m.get('client_name'))}</title>
<style>
  body{{font:14px/1.6 -apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#111;max-width:780px;margin:24px auto;padding:0 16px;}}
  .wm{{position:fixed;top:8px;right:8px;background:#b00020;color:#fff;font-weight:800;
       padding:6px 12px;border-radius:6px;font-size:12px;letter-spacing:1px;}}
  .banner{{background:#fff3cd;border:1px solid #e0c96b;border-radius:8px;padding:10px 12px;font-size:13px;margin:10px 0;}}
  .deadline{{background:#fde7ea;border:1px solid #e6a2ae;border-radius:8px;padding:10px 12px;margin:12px 0;font-weight:600;}}
  h1{{font-size:20px;margin:8px 0;}} h2{{font-size:15px;border-bottom:1px solid #ddd;padding-bottom:4px;margin-top:22px;}}
  table{{border-collapse:collapse;width:100%;margin:8px 0;}} td,th{{border:1px solid #ddd;padding:5px 8px;font-size:12px;text-align:left;}}
  .letter{{border:1px solid #ccc;border-radius:8px;padding:16px;margin:12px 0;}}
  .blank{{color:#a06a00;}} .tiny{{color:#888;font-size:11px;}}
  .toolbar{{margin:10px 0;}} .toolbar a{{display:inline-block;background:#111;color:#fff;text-decoration:none;padding:7px 12px;border-radius:6px;font-size:12px;margin-right:8px;}}
  @media print{{.toolbar,.wm{{display:none;}}}}
  .foot{{color:#888;font-size:11px;margin-top:24px;}}
</style></head><body>
<div class="wm">{_esc(notice.get('watermark'))}</div>
<h1>{_esc(notice.get('title'))} — {_esc(m.get('client_name'))}</h1>
<div class="toolbar">
  <a href="/api/abatement/matters/{_esc(mid)}/notice-of-intent.pdf">Download PDF</a>
  <a href="/api/abatement/matters/{_esc(mid)}/submission.html">Back to package</a>
</div>
<div class="banner"><b>{_esc(notice.get('watermark'))}.</b> {_esc(notice.get('notice_disclaimer'))}</div>
<div class="banner">{_esc(notice.get('disclaimer'))}</div>
<div class="deadline">Earliest contest deadline on file: {_esc(notice.get('earliest_contest_deadline') or '— none computed —')}
  <div class="tiny">{_esc(notice.get('deadline_disclaimer'))}</div></div>

<h2>Citation items</h2>
<table><tr><th>Standard</th><th>Classification</th><th>Contest deadline</th></tr>{item_rows}</table>

<h2>Draft letter</h2>
<div class="letter">
  <p>To: {_esc(lt.get('to'))}<br>
     Area office: <span class="blank">{_esc(lt.get('area_office') or '________________________ (attorney completes)')}</span></p>
  <p>Re: Notice of Intent to Contest — Inspection No. {_esc(lt.get('inspection_number') or '________')}<br>
     Employer: {_esc(lt.get('employer_name') or '________')}<br>
     Citation issued: {_esc(lt.get('citation_issued_date') or '________')} ·
     Received: {_esc(lt.get('citation_received_date') or '________')}<br>
     Citation number(s): <span class="blank">________ (attorney completes)</span></p>
  <p>{_esc(lt.get('statement'))}</p>
  <p>Contesting (attorney to check): &nbsp;
     [ ] Citation(s) &nbsp; [ ] Proposed penalt(ies) &nbsp; [ ] Abatement date(s)</p>
  <p>Signatory: <span class="blank">________________________</span> <span class="tiny">(name / title)</span><br>
     Date: <span class="blank">____________</span></p>
  <p class="tiny">{_esc(lt.get('note'))}</p>
</div>
<div class="foot">Compiled {_esc(notice.get('built_at'))}. {_esc(notice.get('draft_notice'))}</div>
</body></html>"""


# ── real PDF export (reportlab) — package + notice, with embedded stamped photos ─
def _pdf_styles():
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    ss = getSampleStyleSheet()
    ink = colors.Color(0.10, 0.13, 0.18)
    muted = colors.Color(0.42, 0.47, 0.55)
    accent = colors.Color(0.949, 0.443, 0.184)
    red = colors.Color(0.69, 0.0, 0.13)
    return {
        "colors": colors,
        "title": ParagraphStyle("t", parent=ss["Title"], fontSize=16, leading=19,
                                 textColor=ink, alignment=TA_LEFT, spaceAfter=2),
        "h2": ParagraphStyle("h2", parent=ss["Normal"], fontSize=11.5, leading=14,
                             textColor=ink, fontName="Helvetica-Bold",
                             spaceBefore=12, spaceAfter=4),
        "body": ParagraphStyle("b", parent=ss["Normal"], fontSize=9.5, leading=13,
                              textColor=ink),
        "muted": ParagraphStyle("m", parent=ss["Normal"], fontSize=8, leading=11,
                               textColor=muted),
        "stamp": ParagraphStyle("s", parent=ss["Normal"], fontSize=8, leading=11,
                               textColor=ink),
        "red": ParagraphStyle("r", parent=ss["Normal"], fontSize=9, leading=12,
                             textColor=red, fontName="Helvetica-Bold"),
        "banner": ParagraphStyle("bn", parent=ss["Normal"], fontSize=8.5, leading=11,
                                textColor=muted),
        "_ink": ink, "_accent": accent, "_muted": muted, "_red": red,
    }


def _photo_flowable(matter_id: str, e: Dict[str, Any], width: float, sty):
    """Build the flowables for one embedded evidence photo: the image scaled to
    fit, then its sealed capture stamp. Never raises — a photo reportlab can't
    decode still contributes its stamp so the record stays complete."""
    from reportlab.platypus import Image as RLImage, Paragraph, Spacer, KeepTogether
    from reportlab.lib.utils import ImageReader
    flow: List[Any] = []
    st = _photo_stamp(e, matter_id)
    img_flow = None
    try:
        from . import evidence_vault as _ev
        data, _rec = _ev.get_file(matter_id, e.get("evidence_id", ""))
        if data:
            ir = ImageReader(BytesIO(data))
            iw, ih = ir.getSize()
            max_w = min(width, 300.0)
            max_h = 230.0
            scale = min(max_w / float(iw), max_h / float(ih), 1.0)
            img_flow = RLImage(BytesIO(data), width=iw * scale, height=ih * scale)
    except Exception:
        img_flow = None

    if img_flow is not None:
        flow.append(img_flow)
    else:
        flow.append(Paragraph(
            f"[Photo <b>{_esc(e.get('original_filename'))}</b> — preview not "
            f"renderable in PDF; original is preserved in the evidence vault.]",
            sty["muted"]))

    integ = ("integrity verified" if st["integrity_ok"] else
             ("integrity check unavailable" if st["integrity_ok"] is None
              else "INTEGRITY FAILED — do not rely"))
    stamp = (f"<b>{_esc(e.get('original_filename'))}</b> · role: "
             f"{_esc(st['role'] or '—')} · {_esc(st['verification_status'])}<br/>"
             f"Captured: {_esc(st['captured_at'] or '—')}"
             f"{(' · ' + _esc(st['captured_src'])) if st['captured_src'] else ''}<br/>"
             f"GPS: {_esc(st['gps']) if st['gps'] else 'not recorded'}<br/>"
             f"SHA-256: {_esc(st['sha256_16'])}… · "
             f"{'sealed' if st['sealed'] else 'not sealed'} · {integ}")
    flow.append(Paragraph(stamp, sty["stamp"]))
    flow.append(Spacer(1, 8))
    return KeepTogether(flow)


def render_pdf_package(matter_id: str) -> Dict[str, Any]:
    """Render the abatement package as a real PDF, embedding every evidence photo
    with its time/GPS/seal stamp. Returns {ok, pdf, filename} or {ok:False,error}."""
    pkg = build_package(matter_id)
    if not pkg.get("ok"):
        return {"ok": False, "error": pkg.get("error", "not found")}
    try:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, KeepTogether,
        )
    except Exception as exc:
        return {"ok": False, "error": f"reportlab unavailable: {exc}"}

    sty = _pdf_styles()
    colors = sty["colors"]
    m = pkg["matter"]
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER,
                            leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.7 * inch,
                            title=f"Abatement package (DRAFT) — {m.get('client_name','')}")
    flow: List[Any] = []
    flow.append(Paragraph(f"<font color='#b00020'><b>{_esc(pkg.get('watermark'))}</b></font>",
                          sty["red"]))
    flow.append(Paragraph(f"OSHA Abatement Package — {_esc(m.get('client_name'))}", sty["title"]))
    flow.append(Paragraph(_esc(pkg.get("draft_notice")), sty["banner"]))
    flow.append(Paragraph(_esc(pkg.get("disclaimer")), sty["banner"]))
    flow.append(Spacer(1, 4))
    flow.append(Paragraph(
        f"Inspection: <b>{_esc(m.get('osha_inspection_number') or '—')}</b> · "
        f"Citation issued: {_esc(m.get('citation_issued_date') or '—')} · "
        f"Received: {_esc(m.get('citation_received_date') or '—')} · "
        f"Workflow status: {_esc(m.get('status_label'))}", sty["body"]))

    ready = pkg.get("readiness", {}) or {}
    flow.append(Paragraph("Readiness (internal indicator — not an OSHA determination)", sty["h2"]))
    flow.append(Paragraph(
        f"Score: <b>{_esc(ready.get('score'))}</b> — {_esc(ready.get('band'))}. "
        f"{_esc(ready.get('disclaimer') or _mm.READINESS_DISCLAIMER)}", sty["body"]))

    flow.append(Paragraph(f"Citation items ({len(pkg.get('items', []))})", sty["h2"]))
    for it in pkg.get("items", []):
        reg = it.get("regulatory", {}) or {}
        dl = it.get("deadlines", {}) or {}
        block: List[Any] = []
        block.append(Paragraph(
            f"<b>{_esc(it.get('standard'))}</b> — {_esc(it.get('classification'))} "
            f"({'verified' if it.get('verified') else 'unverified'})", sty["body"]))
        block.append(Paragraph(_esc(it.get("alleged_condition")), sty["body"]))
        block.append(Paragraph(
            f"Source: {_esc(reg.get('title') or reg.get('anchor_title') or '—')} · "
            f"Abatement date: {_esc(dl.get('abatement_date') or '—')} · "
            f"Certification due: {_esc(dl.get('certification_due') or '—')} · "
            f"Contest deadline: {_esc(dl.get('contest_deadline') or '—')}", sty["muted"]))
        cas = it.get("corrective_actions", []) or []
        if cas:
            for c in cas:
                block.append(Paragraph(
                    "• " + _esc(c.get("corrective_action") or c.get("title") or c.get("id")),
                    sty["body"]))
        flow.append(KeepTogether(block))
        flow.append(Spacer(1, 4))
        ev = it.get("evidence", []) or []
        photos = [e for e in ev if (e.get("kind") == "photo" or e.get("classification") == "photo")]
        docs = [e for e in ev if e not in photos]
        for e in photos:
            flow.append(_photo_flowable(matter_id, e, doc.width, sty))
        for e in docs:
            flow.append(Paragraph(
                f"Document: {_esc(e.get('original_filename'))} — "
                f"<b>{_esc(e.get('verification_status'))}</b>"
                f"{' · conflicting' if e.get('conflicting') else ''}", sty["muted"]))
        if not ev:
            flow.append(Paragraph("No evidence attached.", sty["muted"]))
        flow.append(HRFlowable(width="100%", thickness=0.5, color=colors.Color(0.8, 0.84, 0.89)))
        flow.append(Spacer(1, 6))

    cert = pkg.get("certification_draft", {}) or {}
    flow.append(Paragraph("Draft abatement certification (29 CFR 1903.19)", sty["h2"]))
    flow.append(Paragraph(_esc(cert.get("note")), sty["muted"]))
    cert_rows = [
        ["Employer", cert.get("employer_name") or "________"],
        ["Inspection #", cert.get("inspection_number") or "________"],
        ["Citation #", "________ (attorney completes)"],
        ["Statement", cert.get("statement") or ""],
        ["Signatory", "________________________ (name / title)"],
        ["Date signed", "____________"],
    ]
    t = Table([[Paragraph(_esc(a), sty["muted"]), Paragraph(_esc(b), sty["body"])]
               for a, b in cert_rows], colWidths=[doc.width * 0.28, doc.width * 0.72])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.Color(0.85, 0.87, 0.9)),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    flow.append(t)
    flow.append(Spacer(1, 12))
    flow.append(Paragraph(f"Compiled {_esc(pkg.get('built_at'))}. {_esc(pkg.get('draft_notice'))}",
                          sty["muted"]))

    try:
        doc.build(flow)
    except Exception as exc:
        return {"ok": False, "error": f"PDF render failed: {exc}"}
    return {"ok": True, "pdf": buf.getvalue(),
            "filename": f"abatement-package-{_esc(m.get('id') or matter_id)}.pdf"}


def render_pdf_notice(matter_id: str) -> Dict[str, Any]:
    """Render the Notice of Intent to Contest as a real PDF (blank skeleton)."""
    notice = build_notice(matter_id)
    if not notice.get("ok"):
        return {"ok": False, "error": notice.get("error", "not found")}
    try:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        )
    except Exception as exc:
        return {"ok": False, "error": f"reportlab unavailable: {exc}"}

    sty = _pdf_styles()
    colors = sty["colors"]
    m = notice["matter"]
    lt = notice.get("letter", {}) or {}
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER,
                            leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                            topMargin=0.7 * inch, bottomMargin=0.8 * inch,
                            title=f"{notice.get('title')} (DRAFT) — {m.get('client_name','')}")
    flow: List[Any] = []
    flow.append(Paragraph(f"<font color='#b00020'><b>{_esc(notice.get('watermark'))}</b></font>",
                          sty["red"]))
    flow.append(Paragraph(f"{_esc(notice.get('title'))} — {_esc(m.get('client_name'))}", sty["title"]))
    flow.append(Paragraph(_esc(notice.get("notice_disclaimer")), sty["banner"]))
    flow.append(Paragraph(_esc(notice.get("disclaimer")), sty["banner"]))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(
        f"<b>Earliest contest deadline on file:</b> "
        f"{_esc(notice.get('earliest_contest_deadline') or '— none computed —')} "
        f"— {_esc(notice.get('deadline_disclaimer'))}", sty["red"]))
    flow.append(Spacer(1, 8))

    flow.append(Paragraph("Citation items", sty["h2"]))
    data = [["Standard", "Classification", "Contest deadline"]]
    for i in notice.get("items", []):
        data.append([i.get("standard", ""), i.get("classification", ""),
                     i.get("contest_deadline") or "—"])
    if len(data) == 1:
        data.append(["— no citation items on file —", "", ""])
    t = Table([[Paragraph(_esc(c), sty["muted"] if r == 0 else sty["body"]) for c in row]
               for r, row in enumerate(data)],
              colWidths=[doc.width * 0.4, doc.width * 0.3, doc.width * 0.3])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.Color(0.85, 0.87, 0.9)),
        ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.945, 0.957, 0.973)),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    flow.append(t)
    flow.append(Spacer(1, 10))

    flow.append(Paragraph("Draft letter", sty["h2"]))
    flow.append(Paragraph(f"To: {_esc(lt.get('to'))}", sty["body"]))
    flow.append(Paragraph(
        f"Area office: {_esc(lt.get('area_office') or '________________________ (attorney completes)')}",
        sty["body"]))
    flow.append(Spacer(1, 4))
    flow.append(Paragraph(
        f"Re: Notice of Intent to Contest — Inspection No. "
        f"{_esc(lt.get('inspection_number') or '________')}", sty["body"]))
    flow.append(Paragraph(
        f"Employer: {_esc(lt.get('employer_name') or '________')} · "
        f"Citation issued: {_esc(lt.get('citation_issued_date') or '________')} · "
        f"Received: {_esc(lt.get('citation_received_date') or '________')}", sty["body"]))
    flow.append(Paragraph("Citation number(s): ________ (attorney completes)", sty["body"]))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(_esc(lt.get("statement")), sty["body"]))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(
        "Contesting (attorney to check): &nbsp; [ ] Citation(s) &nbsp; "
        "[ ] Proposed penalt(ies) &nbsp; [ ] Abatement date(s)", sty["body"]))
    flow.append(Spacer(1, 10))
    flow.append(Paragraph("Signatory: ________________________  (name / title)", sty["body"]))
    flow.append(Paragraph("Date: ____________", sty["body"]))
    flow.append(Spacer(1, 10))
    flow.append(Paragraph(_esc(lt.get("note")), sty["muted"]))
    flow.append(Paragraph(f"Compiled {_esc(notice.get('built_at'))}. {_esc(notice.get('draft_notice'))}",
                          sty["muted"]))

    try:
        doc.build(flow)
    except Exception as exc:
        return {"ok": False, "error": f"PDF render failed: {exc}"}
    return {"ok": True, "pdf": buf.getvalue(),
            "filename": f"notice-of-intent-{_esc(m.get('id') or matter_id)}.pdf"}


# ── routes ─────────────────────────────────────────────────────────────────────
def register_abatement_submissions(app) -> None:
    """Attach submission routes. Isolated + non-fatal."""
    from fastapi import Body
    from fastapi.responses import HTMLResponse, JSONResponse, Response

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

    @app.get("/api/abatement/matters/{matter_id}/submission.pdf")
    def ab_submission_pdf(matter_id: str):
        out = render_pdf_package(matter_id)
        if not out.get("ok"):
            return JSONResponse({"error": out.get("error", "render failed")},
                                status_code=404 if out.get("error") == "not found" else 500)
        return Response(content=out["pdf"], media_type="application/pdf",
                        headers={"Content-Disposition":
                                 f'inline; filename="{out["filename"]}"',
                                 "Cache-Control": "no-store"})

    # ── Notice of Intent to Contest (precedes abatement) ──────────────────────
    @app.get("/api/abatement/matters/{matter_id}/notice-of-intent")
    def ab_notice(matter_id: str):
        notice = build_notice(matter_id)
        if not notice.get("ok"):
            return JSONResponse(notice, status_code=404)
        return notice

    @app.get("/api/abatement/matters/{matter_id}/notice-of-intent.html",
             response_class=HTMLResponse)
    def ab_notice_html(matter_id: str):
        notice = build_notice(matter_id)
        return HTMLResponse(render_notice_html(notice),
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/abatement/matters/{matter_id}/notice-of-intent.pdf")
    def ab_notice_pdf(matter_id: str):
        out = render_pdf_notice(matter_id)
        if not out.get("ok"):
            return JSONResponse({"error": out.get("error", "render failed")},
                                status_code=404 if out.get("error") == "not found" else 500)
        return Response(content=out["pdf"], media_type="application/pdf",
                        headers={"Content-Disposition":
                                 f'inline; filename="{out["filename"]}"',
                                 "Cache-Control": "no-store"})

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
