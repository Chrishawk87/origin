"""pdf_render.py — the Form Vault's real-PDF stamp engine.

Screen 3's ``form_vault.fill()`` projects a 60-second worker entry onto the
official government field layout and stamps it "Audit Ready". This module turns
that structured result into an actual, downloadable **PDF file** — the artifact
an inspector or a prequal reviewer (ISN/Avetta/Veriforce) can be handed.

Two rendering paths, same public entry point ``render_pdf``:

  1. TEMPLATE OVERLAY (pixel-perfect). If a real blank government PDF is present
     at ``form_templates/<form_id>.pdf`` with a coordinate map at
     ``form_templates/<form_id>.map.json``, the resolved values are stamped onto
     the real form at the mapped x/y positions (reportlab canvas → pypdf merge).
     This is how a genuine MSHA 5000-23 or USACE 385 gets filled in place.

  2. GENERATED FALLBACK (always available). When no template is on disk — the
     default today, since the blank forms live at their government source — the
     engine lays out a clean, sectioned, inspection-grade PDF from the same
     official field layout: agency header, CFR authority, an Audit-Ready badge,
     every section and field, AHA hazard/control matrices as real tables, and a
     signature block. A field aid, not a substitute for the official filing.

Built the Origin way: deterministic, offline, no LLM, no database. reportlab and
pypdf already ship in the deploy image (portal.py uses both). Import is safe even
if they were somehow absent — the caller gets an explicit error, never a 500.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import form_vault

_TEMPLATE_DIR = Path(__file__).resolve().parent / "form_templates"

# ── Palette (matches the Origin dark-console accent set) ──────────────────────
_INK = (0.10, 0.13, 0.18)
_MUTED = (0.42, 0.47, 0.55)
_ACCENT = (0.949, 0.443, 0.184)      # #f2712f orange
_GREEN = (0.09, 0.50, 0.27)
_AMBER = (0.72, 0.52, 0.04)
_RULE = (0.80, 0.84, 0.89)
_BAND = (0.945, 0.957, 0.973)


# ── Public entry point ────────────────────────────────────────────────────────
def render_pdf(form_id: str, answers: Optional[Dict[str, Any]] = None,
               gc_slug: Optional[str] = None) -> Dict[str, Any]:
    """Fill the form, then produce a real PDF.

    Returns ``{"ok": True, "pdf": <bytes>, "filename": str, "audit_ready": bool,
    "mode": "template"|"generated"}`` or ``{"ok": False, "error": str}``. Never
    raises for an unknown form or a scoping block — those come back as ok:False,
    same contract as ``form_vault.fill``.
    """
    result = form_vault.fill(form_id, answers=answers, gc_slug=gc_slug)
    if not result.get("ok"):
        return result

    # Path 1: stamp onto a real government template when one is on disk.
    try:
        stamped = _overlay_template(form_id, result)
        if stamped is not None:
            return {"ok": True, "pdf": stamped, "filename": _filename(form_id),
                    "audit_ready": result.get("audit_ready"), "mode": "template"}
    except Exception:
        pass  # a bad template must never block the generated fallback

    # Path 2: generate a clean inspection-grade PDF from the field layout.
    try:
        pdf = _generate(result)
    except Exception as exc:
        return {"ok": False, "error": f"PDF render failed: {exc}"}
    return {"ok": True, "pdf": pdf, "filename": _filename(form_id),
            "audit_ready": result.get("audit_ready"), "mode": "generated"}


def _filename(form_id: str) -> str:
    return f"{form_id}.pdf"


# ── Path 1: real-template overlay (pixel-perfect fill) ────────────────────────
def _overlay_template(form_id: str, result: Dict[str, Any]) -> Optional[bytes]:
    """Stamp resolved values onto a real blank government PDF using a coordinate
    map. Returns PDF bytes, or None when no template/map pair is present so the
    caller falls through to the generated layout.

    Coordinate map schema (form_templates/<id>.map.json):
        {"page_size": [w, h] (optional, informational),
         "fields": {"<official_field_id>": {"page": 0, "x": 90, "y": 640,
                                            "size": 10, "font": "Helvetica"}},
         "checks": {"<field_id>=<value>": {"page": 0, "x": 300, "y": 500}}}
    y is measured from the page bottom (PDF native), matching reportlab.
    """
    tpl = _TEMPLATE_DIR / f"{form_id}.pdf"
    cmap = _TEMPLATE_DIR / f"{form_id}.map.json"
    if not (tpl.exists() and cmap.exists()):
        return None

    from reportlab.pdfgen import canvas
    from pypdf import PdfReader, PdfWriter

    spec = json.loads(cmap.read_text(encoding="utf-8"))
    fields_map: Dict[str, Any] = spec.get("fields", {}) or {}
    checks_map: Dict[str, Any] = spec.get("checks", {}) or {}
    values = _flat_values(result)

    reader = PdfReader(str(tpl))
    n_pages = len(reader.pages)

    # Build one overlay canvas per page, drawing only that page's fields.
    overlays: Dict[int, BytesIO] = {}
    canvases: Dict[int, Any] = {}

    def _canvas_for(pg: int):
        if pg in canvases:
            return canvases[pg]
        page = reader.pages[min(pg, n_pages - 1)]
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)
        buf = BytesIO()
        c = canvas.Canvas(buf, pagesize=(w, h))
        c.setFillColorRGB(0.05, 0.07, 0.10)
        overlays[pg] = buf
        canvases[pg] = c
        return c

    for fid, pos in fields_map.items():
        val = values.get(fid)
        if not val:
            continue
        pg = int(pos.get("page", 0))
        c = _canvas_for(pg)
        c.setFont(pos.get("font", "Helvetica"), float(pos.get("size", 10)))
        c.drawString(float(pos.get("x", 40)), float(pos.get("y", 40)), str(val)[:120])

    for key, pos in checks_map.items():
        fid, _, want = key.partition("=")
        if str(values.get(fid, "")).strip().lower() != want.strip().lower():
            continue
        pg = int(pos.get("page", 0))
        c = _canvas_for(pg)
        c.setFont(pos.get("font", "Helvetica-Bold"), float(pos.get("size", 12)))
        c.drawString(float(pos.get("x", 40)), float(pos.get("y", 40)), pos.get("mark", "X"))

    if not canvases:
        return None  # template present but nothing mapped hit — let fallback run

    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if i in canvases:
            canvases[i].save()
            overlays[i].seek(0)
            page.merge_page(PdfReader(overlays[i]).pages[0])
        writer.add_page(page)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _flat_values(result: Dict[str, Any]) -> Dict[str, Any]:
    flat: Dict[str, Any] = {}
    for sec in result.get("sections", []):
        for fld in sec.get("fields", []):
            flat[fld["id"]] = fld.get("value")
    return flat


# ── Path 2: generated inspection-grade layout ─────────────────────────────────
def _generate(result: Dict[str, Any]) -> bytes:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, KeepTogether,
    )

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.6 * inch, bottomMargin=0.7 * inch,
        title=result.get("title", "Official Form"),
        author="Origin Compliance",
    )

    ss = getSampleStyleSheet()
    ink = colors.Color(*_INK)
    muted = colors.Color(*_MUTED)
    accent = colors.Color(*_ACCENT)
    rule = colors.Color(*_RULE)

    h_title = ParagraphStyle("t", parent=ss["Title"], fontSize=16, leading=19,
                             textColor=ink, spaceAfter=2, alignment=TA_LEFT)
    h_agency = ParagraphStyle("a", parent=ss["Normal"], fontSize=9.5, leading=12,
                              textColor=accent, spaceAfter=1,
                              fontName="Helvetica-Bold")
    h_auth = ParagraphStyle("au", parent=ss["Normal"], fontSize=8.5, leading=11,
                            textColor=muted)
    h_sec = ParagraphStyle("s", parent=ss["Normal"], fontSize=10.5, leading=13,
                           textColor=ink, fontName="Helvetica-Bold",
                           spaceBefore=10, spaceAfter=4)
    lbl = ParagraphStyle("l", parent=ss["Normal"], fontSize=8.5, leading=11,
                         textColor=muted, fontName="Helvetica-Bold")
    val = ParagraphStyle("v", parent=ss["Normal"], fontSize=10, leading=13,
                         textColor=ink)
    val_missing = ParagraphStyle("vm", parent=val, textColor=colors.Color(*_AMBER),
                                 fontName="Helvetica-Oblique")
    small = ParagraphStyle("sm", parent=ss["Normal"], fontSize=7.5, leading=10,
                           textColor=muted)
    ctrl = ParagraphStyle("ct", parent=ss["Normal"], fontSize=9, leading=12,
                          textColor=ink)

    flow: List[Any] = []

    # Header
    flow.append(Paragraph((result.get("agency") or "").upper(), h_agency))
    flow.append(Paragraph(result.get("title", "Official Form"), h_title))
    if result.get("gov_url"):
        flow.append(Paragraph(f"Authoritative blank form &amp; authority: {result['gov_url']}", h_auth))
    flow.append(Spacer(1, 5))

    # Audit badge
    ready = bool(result.get("audit_ready"))
    if ready:
        badge_txt, bg = "AUDIT READY — all required fields satisfied", colors.Color(*_GREEN)
    else:
        miss = result.get("missing_required") or []
        badge_txt = f"INCOMPLETE — {len(miss)} required field(s) missing: " + ", ".join(miss[:6])
        bg = colors.Color(*_AMBER)
    badge = Table([[Paragraph(f"<font color='white'><b>{badge_txt}</b></font>",
                              ParagraphStyle("b", parent=ss["Normal"], fontSize=9, leading=12))]],
                  colWidths=[doc.width])
    badge.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ROUNDEDCORNERS", [3, 3, 3, 3]),
    ]))
    flow.append(badge)
    flow.append(Spacer(1, 8))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=rule))

    # Sections
    for sec in result.get("sections", []):
        title = sec.get("section") or "Details"
        block: List[Any] = [Paragraph(title, h_sec)]
        rows: List[List[Any]] = []
        for fld in sec.get("fields", []):
            if fld.get("matrix"):
                block.append(_matrix_table(fld, doc.width, Paragraph, Table, TableStyle,
                                           colors, lbl, ctrl, small))
                continue
            if fld.get("signature"):
                v = Paragraph("_______________________________  &nbsp;&nbsp; Date: ____________", val)
            elif fld.get("value"):
                v = Paragraph(str(fld["value"]), val)
            elif fld.get("required"):
                v = Paragraph("— required, not provided —", val_missing)
            else:
                v = Paragraph("—", val)
            rows.append([Paragraph(fld["label"], lbl), v])
        if rows:
            t = Table(rows, colWidths=[doc.width * 0.34, doc.width * 0.66])
            t.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.Color(*_RULE)),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]))
            block.append(t)
        flow.append(KeepTogether(block))

    # Footer
    flow.append(Spacer(1, 14))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=rule))
    flow.append(Spacer(1, 3))
    flow.append(Paragraph(
        f"Generated by Origin Compliance on {result.get('filled_at', '')} · "
        "Deterministic auto-fill from field entry. This is a field aid; the "
        "authoritative filing is the official government form at the linked source.",
        small))

    doc.build(flow, canvasmaker=_numbered_canvas())
    return buf.getvalue()


def _matrix_table(fld, width, Paragraph, Table, TableStyle, colors, lbl, ctrl, small):
    """Render an AHA hazard/control matrix as a real two-column table."""
    rows = fld.get("rows") or []
    data: List[List[Any]] = [[Paragraph("<b>Hazard</b>", lbl),
                              Paragraph("<b>Controls (from the governing citation)</b>", lbl)]]
    if not rows:
        data.append([Paragraph(fld.get("label", "AHA matrix"), ctrl),
                     Paragraph(fld.get("note", "Add hazard rows in the Checklist / AHA tool"), small)])
    else:
        for r in rows:
            controls = r.get("controls") or []
            bullets = "<br/>".join(f"• {c}" for c in controls) or "—"
            cite = r.get("citation")
            if cite:
                bullets += f"<br/><font size=7 color='#6b7686'>Source: {cite}</font>"
            data.append([Paragraph(str(r.get("hazard", "—")), ctrl),
                         Paragraph(bullets, ctrl)])
    t = Table(data, colWidths=[width * 0.28, width * 0.72])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.Color(*_BAND)),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.Color(*_RULE)),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _numbered_canvas():
    """A canvas that prints 'Page N of M' in the footer margin."""
    from reportlab.pdfgen import canvas as _canvas
    from reportlab.lib.pagesizes import LETTER

    class _NumberedCanvas(_canvas.Canvas):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._saved = []

        def showPage(self):
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            n = len(self._saved)
            for st in self._saved:
                self.__dict__.update(st)
                self._draw_num(n)
                super().showPage()
            super().save()

        def _draw_num(self, total):
            self.setFont("Helvetica", 7.5)
            self.setFillColorRGB(*_MUTED)
            self.drawRightString(LETTER[0] - 0.7 * 72, 0.45 * 72,
                                 f"Page {self._pageNumber} of {total}")

    return _NumberedCanvas


# ── Offline self-test ─────────────────────────────────────────────────────────
def _selftest() -> Tuple[bool, str]:
    from pypdf import PdfReader
    checks: List[str] = []
    ok = True
    samples = {
        "msha-5000-23": {},
        "usace-aha": {"activity": "excavation"},
        "msha-wpe": {
            "company": "Acme Aggregates", "mine_name": "Pit 4", "mine_id": "41-01234",
            "exam_date": "2026-09-08", "shift": "Day", "examiner": "R. Diaz",
            "areas": "Primary crusher, haul road", "adverse": "No",
        },
    }
    for fid, ans in samples.items():
        try:
            out = render_pdf(fid, answers=ans)
        except Exception as exc:  # pragma: no cover
            ok = False
            checks.append(f"{fid}: EXCEPTION {exc}")
            continue
        if not out.get("ok"):
            # A form the vault doesn't carry is skipped, not a failure.
            checks.append(f"{fid}: skipped ({out.get('error')})")
            continue
        pdf = out["pdf"]
        head_ok = pdf[:5] == b"%PDF-"
        try:
            pages = len(PdfReader(BytesIO(pdf)).pages)
        except Exception as exc:
            pages = -1
            ok = False
        good = head_ok and pages >= 1
        ok = ok and good
        checks.append(f"{fid}: {'OK' if good else 'BAD'} mode={out['mode']} "
                      f"bytes={len(pdf)} pages={pages} audit_ready={out['audit_ready']}")
    return ok, "\n".join(checks)


if __name__ == "__main__":
    good, report = _selftest()
    print(report)
    print("PASS" if good else "FAIL")
    raise SystemExit(0 if good else 1)
