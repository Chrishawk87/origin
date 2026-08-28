"""Compliance document management for Origin.

Design (v2):
- The Asset Library is a PERSISTENT, EDITABLE store of master documents kept in
  the data dir (DATA_DIR/compliance_library) so edits survive restarts and the
  user can add their own masters. It is seeded once from the 24 templates that
  ship inside the package (compliance_library/ next to this file).
- Editing a master edits the master itself (saved back to the library).
- "Use in a customer job" copies a master into a project's workdir (copy-on-
  assign) so the master is never touched by client-specific edits.
- Documents are self-contained HTML (styled), editable inline in the browser and
  renderable to PDF for dispatch.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import DATA_DIR

DEFAULTS_DIR = Path(__file__).parent / "compliance_library"   # shipped seeds
LIBRARY_DIR = DATA_DIR / "compliance_library"                 # live, editable
ASSIGN_SUBDIR = "Compliance"                                  # inside a project


# ── Brand / document styling ─────────────────────────────────────────────────
# Bump STYLE_VERSION whenever the look changes; ensure_library() re-renders the
# stored masters so existing documents on the Railway volume pick up the change.
STYLE_VERSION = "2026-08-26-premium-cover-2"

# Bump LIBRARY_SET_VERSION to re-run the library consolidation migration on the
# Railway volume (retire duplicate trade starters, fold the unique ones into the
# canonical KB categories). Runs once per version, then records a marker.
LIBRARY_SET_VERSION = "2026-08-26-consolidate-1"

# Legacy trade starter templates that duplicate a canonical KB program-* master.
# The KB set is authoritative, so these are retired from the live library.
_DUPLICATE_TRADE_IDS = {
    "general-construction-01", "general-construction-02", "general-construction-03",
    "general-construction-04", "general-construction-05", "general-construction-06",
    "general-construction-07", "general-construction-08",
    "oilfield-01", "oilfield-02", "oilfield-03", "oilfield-04", "oilfield-05",
    "oilfield-06", "oilfield-07",
    "trucking-07", "trucking-08",
}

# Unique trade programs with NO KB equivalent — kept, but regrouped into the
# existing canonical categories so the library reads as one clean set.
_KEEP_TRADE_REGROUP = {
    "trucking-01": "04 - DOT and FMCSA (49 CFR)",
    "trucking-02": "04 - DOT and FMCSA (49 CFR)",
    "trucking-03": "04 - DOT and FMCSA (49 CFR)",
    "trucking-04": "04 - DOT and FMCSA (49 CFR)",
    "trucking-05": "04 - DOT and FMCSA (49 CFR)",
    "trucking-06": "04 - DOT and FMCSA (49 CFR)",
    "oilfield-08": "09 - Management Systems & Prequal Programs",
}

# IMPORTANT: these documents are the CLIENT's own written programs — the client
# uploads them to ISNetworld/Avetta as their company's program. So Origin
# Management Solutions must NOT appear anywhere on them. The letterhead is driven
# by the CLIENT's fields ({{COMPANY_NAME}}/{{COMPANY_ADDRESS}}) inside each
# template body; the document shell here stays neutral (premium styling + a
# page-number footer with no service-provider branding).

# Markers delimit the auto-generated chrome so a re-style can strip and replace
# it without touching the document body.
_CHROME_START = "<!--OMS:CHROME-START-->"
_CHROME_END = "<!--OMS:CHROME-END-->"

# Palette: navy #0f2c4c, accent #1f4e79, ink #1f2933, slate #475569,
# muted #64748b, hair #e2e8f0. Premium program-document look (cover page +
# control table + controlled-doc notice + callouts), ported from the approved
# Excavation & Trenching sample so every program master renders submission-ready.
_DOC_CSS = """
<style>
  @page {
    size: letter;
    margin: 108px 64px 92px 64px;
    @frame footer_frame {
      -pdf-frame-content: omsFooter;
      bottom: 34px; margin-left: 64px; margin-right: 64px; height: 36px;
    }
  }
  body{font-family:'Helvetica','Arial',sans-serif;color:#1f2933;line-height:1.6;
       font-size:12.5px;margin:0;padding:0;background:#ffffff}
  .oms-doc{max-width:770px;margin:0 auto;padding:28px 30px 40px}
  /* Client letterhead (driven by the client's own company fields) */
  table.oms-lh{width:100%;border-collapse:collapse;margin:0 0 4px}
  table.oms-lh td{border:none;padding:0;vertical-align:middle}
  .client-name{font-size:22px;font-weight:bold;color:#0f2c4c;letter-spacing:0.3px}
  .client-addr{font-size:10px;color:#64748b;letter-spacing:0.3px;padding-top:4px}
  .client-logo{height:50px}
  .doc-meta{font-size:10px;color:#475569;text-align:right;line-height:1.6}
  .doc-meta b{color:#0f2c4c;font-size:11px}
  .oms-rule{font-size:1px;line-height:1px;border-bottom:3px solid #0f2c4c;margin:8px 0 0}
  .oms-rule2{font-size:1px;line-height:1px;border-bottom:1px solid #cbd5e1;margin:2px 0 20px}
  /* ---- Premium cover page ---- */
  .cover{text-align:center}
  .cover .brandbar{border-top:3px solid #0f2c4c;border-bottom:1px solid #0f2c4c;
       padding:14px 0 12px 0;margin-bottom:6px}
  .cover .client{font-size:23px;font-weight:bold;color:#0f2c4c;letter-spacing:0.5px}
  .cover .client-addr{font-size:10px;color:#64748b;margin-top:5px}
  .cover .kicker{font-size:11px;color:#7a8a99;letter-spacing:3px;margin-top:20px}
  .cover .title{font-size:28px;font-weight:bold;color:#0f2c4c;margin:10px 30px 6px 30px;line-height:1.15}
  .cover .std{font-size:12.5px;color:#1f4e79;font-weight:bold;margin-top:5px}
  .cover .coverrule{border-top:1px solid #c8d2dc;margin:16px 40px}
  .ctrl{margin:0 40px}
  table.ctrl-t{width:100%;border-collapse:collapse}
  table.ctrl-t td{border:1px solid #c8d2dc;padding:5px 11px;font-size:10px;text-align:left}
  table.ctrl-t td.k{background:#0f2c4c;color:#ffffff;font-weight:bold;width:38%}
  table.ctrl-t td.v{background:#f5f8fb;color:#1c2733}
  .notice{margin:14px 40px 0 40px;border:1px solid #d9c26a;background:#fbf6e6;
       padding:9px 12px;font-size:9px;color:#6b5a1f;text-align:left;line-height:1.45}
  /* Body typography */
  h1{font-size:19px;color:#0f2c4c;font-weight:bold;margin:16px 0 6px}
  h2{font-size:13.5px;color:#0f2c4c;font-weight:bold;text-transform:uppercase;
     letter-spacing:0.6px;border-bottom:1px solid #e2e8f0;padding-bottom:4px;margin:22px 0 8px}
  h3{font-size:12.5px;color:#1f3a5f;font-weight:bold;margin:15px 0 4px}
  p{margin:7px 0}
  strong,b{color:#0f2c4c}
  em{color:#64748b;font-style:italic}
  ul,ol{margin:7px 0 7px 18px;padding:0}
  li{margin:3px 0}
  table{border-collapse:collapse;width:100%;margin:12px 0;font-size:11.5px}
  th,td{border:1px solid #cbd5e1;padding:6px 9px;vertical-align:top;text-align:left}
  thead th,th{background:#0f2c4c;color:#ffffff;font-weight:bold}
  tbody tr:nth-child(even) td{background:#f5f8fc}
  hr{border:none;border-top:1px solid #e2e8f0;margin:18px 0}
  /* ---- Callout boxes ---- */
  .callout{padding:9px 12px;margin:11px 0;font-size:11px;border-left:4px solid #999}
  .callout .lbl{font-weight:bold;font-size:10px;letter-spacing:1px;display:block;margin-bottom:3px}
  .callout.warning{background:#fdecea;border-left-color:#c0392b}
  .callout.warning .lbl{color:#c0392b}
  .callout.caution{background:#fef6e7;border-left-color:#e67e22}
  .callout.caution .lbl{color:#b9600f}
  .callout.note{background:#eef4fb;border-left-color:#3a6ea5}
  .callout.note .lbl{color:#2c5f97}
  /* ---- Job Hazard Analysis (JHA companion) ---- */
  .cover .subtitle{font-size:15px;color:#1f4e79;margin:2px 30px 6px 30px}
  .jobhead{background:#0f2c4c;color:#ffffff;font-size:12px;font-weight:bold;padding:6px 9px}
  table.jha{width:100%;border-collapse:collapse;margin:0 0 14px 0}
  table.jha th{background:#1f4e79;color:#ffffff;font-size:9px;text-align:left;padding:5px 7px}
  table.jha td{border:1px solid #cdd8e2;padding:5px 7px;font-size:9px;vertical-align:top}
  table.jha tr.alt td{background:#f5f8fb}
  .jha .hz{color:#8a3221}
  .jha .step{font-weight:bold;color:#0f2c4c}
  table.legend{width:100%;border-collapse:collapse;margin:8px 0}
  table.legend td{border:1px solid #cdd8e2;padding:5px 8px;font-size:10px;vertical-align:top}
  table.legend td.k{background:#eef4fb;font-weight:bold;color:#1f4e79;width:150px}
  table.specs{width:100%;border-collapse:collapse;margin:8px 0}
  table.specs td{border:1px solid #cdd8e2;padding:6px 8px;font-size:10px;vertical-align:top}
  table.specs td.k{background:#eef4fb;font-weight:bold;color:#1f4e79;width:24%}
  table.specs td.fill{background:#ffffff}
  .subhead{font-size:12px;color:#1f4e79;font-weight:bold;margin:14px 0 4px}
  table.ack{width:100%;border-collapse:collapse;margin:8px 0}
  table.ack td{border:1px solid #c8d2dc;padding:6px 7px;font-size:9px}
  table.ack th{background:#0f2c4c;color:#ffffff;font-size:9px;padding:6px 7px;text-align:left}
  .pb{page-break-before:always}
  .oms-footer{font-size:8.5px;color:#94a3b8;text-align:center;
              border-top:1px solid #e2e8f0;padding-top:6px}
</style>
"""


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _footer_html() -> str:
    # Neutral footer — NO service-provider branding, since the client uploads
    # this as their own document. <pdf:pagenumber>/<pdf:pagecount> render page
    # numbers in the PDF; browsers ignore the unknown tags.
    return (
        f"{_CHROME_START}"
        f"<div id='omsFooter' class='oms-footer'>"
        f"Confidential &nbsp;·&nbsp; Page <pdf:pagenumber> of <pdf:pagecount>"
        f"</div>"
        f"{_CHROME_END}"
    )


_CHROME_RE = re.compile(re.escape(_CHROME_START) + r".*?" + re.escape(_CHROME_END), re.S)
_BODY_RE = re.compile(r"<body[^>]*>(.*)</body>", re.S | re.I)
_DOCWRAP_RE = re.compile(r"<div class=['\"]oms-doc['\"]>(.*)</div>\s*$", re.S | re.I)


def _content_only(html: str) -> str:
    """Reduce any stored document back to its pure body content: strip the
    <html>/<head>, unwrap the .oms-doc container, and remove prior chrome."""
    s = html or ""
    m = _BODY_RE.search(s)
    if m:
        s = m.group(1)
    s = _CHROME_RE.sub("", s)          # remove old letterhead + footer blocks
    m2 = _DOCWRAP_RE.search(s.strip())
    if m2:
        s = m2.group(1)
    return s.strip()


def wrap_document(inner_html: str, title: str = "") -> str:
    """Wrap content in a neutral, print-ready document shell (premium styling +
    page-number footer, no service-provider branding — the client's own
    letterhead comes from the template body). Idempotent: re-wrapping an
    already-wrapped document restyles it rather than nesting."""
    content = _content_only(inner_html)
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_esc(title or 'Written Compliance Program')}</title>{_DOC_CSS}</head>"
        f"<body><div class='oms-doc'>"
        f"{content}"
        f"</div>{_footer_html()}</body></html>"
    )


# ── Premium program cover page ───────────────────────────────────────────────
# Program masters get a full cover page (client letterhead + control table +
# controlled-document notice) that replaces the inline letterhead + title block
# in the template body. Every field is either a value known from the KB (the
# regulatory basis) or an EXISTING client placeholder token ({{COMPANY_NAME}},
# {{EFFECTIVE_DATE}}, etc.) that the portal already fills on assign — plus a few
# sensible literal defaults. No new tokens are introduced.

# Strip everything before the first real section heading so the cover isn't
# duplicated by the template's own inline letterhead / <h1> title / admin line.
_INTRO_RE = re.compile(r"^.*?(?=<h2)", re.S | re.I)


def _program_cover_html(title: str, citation: str) -> str:
    """Build the premium cover page for a written-program master."""
    std = _esc(citation or "")
    rows = [
        ("Revision", "1.0"),
        ("Effective Date", "{{EFFECTIVE_DATE}}"),
        ("Regulatory Basis", std or "See program body"),
        ("Review Cycle", "Annual"),
        ("Last Reviewed", "{{LAST_REVIEW_DATE}}"),
        ("Next Review Due", "{{NEXT_REVIEW_DATE}}"),
        ("Program Administrator", "{{PROGRAM_ADMINISTRATOR}}, {{ADMIN_TITLE}}"),
        ("Classification", "Controlled &mdash; Internal Use"),
    ]
    ctrl = "".join(
        f"<tr><td class='k'>{k}</td><td class='v'>{v}</td></tr>" for k, v in rows
    )
    std_line = f"<div class='std'>{std}</div>" if std else ""
    return (
        "<div class='cover'>"
        "<div class='brandbar'>"
        "<div class='client'>{{COMPANY_NAME}}</div>"
        "<div class='client-addr'>{{COMPANY_ADDRESS}}</div>"
        "</div>"
        "<div class='kicker'>WRITTEN SAFETY PROGRAM</div>"
        f"<div class='title'>{_esc(title)}</div>"
        f"{std_line}"
        "<div class='coverrule'></div>"
        f"<div class='ctrl'><table class='ctrl-t'>{ctrl}</table></div>"
        "<div class='notice'><b>CONTROLLED DOCUMENT.</b> This document is the property of "
        "{{COMPANY_NAME}} and is maintained under its Health, Safety &amp; Environmental "
        "management system. Printed copies are uncontrolled and valid only on the date "
        "printed. Verify the current revision before use.</div>"
        "</div>"
    )


def wrap_program_document(inner_html: str, title: str = "",
                          citation: str = "") -> str:
    """Wrap a written-program body in the premium shell: a client-branded cover
    page (letterhead + control table + controlled-doc notice) on page 1, then
    the program sections. The template's own inline letterhead / title block is
    stripped so nothing is duplicated. Client fields stay as placeholder tokens
    the portal fills on assign — the shell carries no service-provider branding."""
    content = _content_only(inner_html)
    body = _INTRO_RE.sub("", content, count=1).strip() or content
    cover = _program_cover_html(title, citation)
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_esc(title or 'Written Compliance Program')}</title>{_DOC_CSS}</head>"
        f"<body><div class='oms-doc'>"
        f"{cover}"
        f"<div class='pb'></div>"
        f"{body}"
        f"</div>{_footer_html()}</body></html>"
    )


# ── Legacy trade-template upgrade ────────────────────────────────────────────
# The original trade starter templates (general-construction-*, oilfield-*,
# trucking-*) predate both the premium cover and the portal auto-fill tokens.
# Their body opens with an inline "YOUR LOGO HERE" letterhead + a metadata table
# and uses spaced placeholder tokens ({{COMPANY NAME}}) the portal never fills.
# This upgrade strips that legacy intro, normalizes the tokens to the same
# underscore set the rest of the library uses, then applies the premium cover so
# the entire asset library shares one look and one fill flow.
_LEGACY_INTRO_RE = re.compile(r"^.*?(?=<p><strong>\s*1[.\s])", re.S | re.I)
_LEGACY_CITE_RE = re.compile(
    r"(?:29|30|40|49)\s*CFR\s*[\d.]+(?:\s*Subpart\s*[A-Z])?", re.I)


def _is_legacy_template(html: str) -> bool:
    """A stored master still in the pre-premium trade-template format."""
    return "YOUR LOGO HERE" in html or "{{COMPANY NAME}}" in html


def _normalize_legacy_tokens(s: str) -> str:
    """Bring the old spaced placeholders in line with the portal's token set."""
    s = s.replace("{{COMPANY NAME}}", "{{COMPANY_NAME}}")
    s = s.replace("{{0}}", "1.0")          # revision-history starter row
    s = s.replace("{{ }}", "&nbsp;")       # blank revision-history cells
    return s


def wrap_legacy_program_document(inner_html: str, title: str = "",
                                 citation: str = "") -> str:
    """Upgrade a legacy trade-template body to the premium program format:
    strip its inline letterhead + metadata table, normalize its placeholder
    tokens, then wrap it in the same client-branded cover the KB programs use."""
    content = _content_only(inner_html)
    body = _LEGACY_INTRO_RE.sub("", content, count=1).strip() or content
    body = _normalize_legacy_tokens(body)
    if not citation:
        m = _LEGACY_CITE_RE.search(body)
        if m:
            citation = re.sub(r"\s+", " ", m.group(0)).strip()
    cover = _program_cover_html(title, citation)
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_esc(title or 'Written Compliance Program')}</title>{_DOC_CSS}</head>"
        f"<body><div class='oms-doc'>"
        f"{cover}"
        f"<div class='pb'></div>"
        f"{body}"
        f"</div>{_footer_html()}</body></html>"
    )


# ── Library persistence ──────────────────────────────────────────────────────
def _index_path() -> Path:
    return LIBRARY_DIR / "index.json"


def ensure_library() -> None:
    """Create the live library from shipped defaults, then merge in the KB
    program templates. Idempotent — user edits and existing masters are never
    overwritten; only missing masters are added (so this also back-fills the
    program templates on a volume that was seeded before they existed)."""
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    if not _index_path().is_file():
        seeds = []
        seed_index = DEFAULTS_DIR / "index.json"
        if seed_index.is_file():
            seeds = json.loads(seed_index.read_text(encoding="utf-8"))
        records = []
        for s in seeds:
            src = DEFAULTS_DIR / s["file"]
            if not src.is_file():
                continue
            title = s.get("title", s["id"])
            raw = src.read_text(encoding="utf-8")
            if _is_legacy_template(raw):
                html = wrap_legacy_program_document(raw, title)
            else:
                html = wrap_document(raw, title)
            fname = f"{s['id']}.html"
            (LIBRARY_DIR / fname).write_text(html, encoding="utf-8")
            records.append({"id": s["id"], "trade": s.get("trade", ""),
                            "num": s.get("num", ""), "title": title, "file": fname})
        _write_index(records)
    _sync_program_masters()
    _sync_jha_masters()
    _restyle_masters_if_needed()
    _consolidate_library_if_needed()


def _style_marker_path() -> Path:
    return LIBRARY_DIR / ".style_version"


def _library_set_marker_path() -> Path:
    return LIBRARY_DIR / ".library_set_version"


def _consolidate_library_if_needed() -> None:
    """Retire the trade starter templates that duplicate a canonical KB program
    master, and regroup the few unique ones (trucking/DOT + SSE) into existing
    canonical categories so the Asset Library reads as one clean set. Guarded by
    a marker so it runs once per LIBRARY_SET_VERSION on the Railway volume."""
    marker = _library_set_marker_path()
    try:
        current = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    except Exception:
        current = ""
    if current == LIBRARY_SET_VERSION:
        return
    records = _read_index_raw()
    kept: List[Dict[str, Any]] = []
    changed = False
    for rec in records:
        rid = rec.get("id", "")
        if rid in _DUPLICATE_TRADE_IDS:
            # remove the duplicate: drop from index and delete its file
            f = LIBRARY_DIR / rec.get("file", "")
            try:
                if f.is_file():
                    f.unlink()
            except Exception:
                pass
            changed = True
            continue
        if rid in _KEEP_TRADE_REGROUP:
            new_trade = _KEEP_TRADE_REGROUP[rid]
            if rec.get("trade") != new_trade:
                rec["trade"] = new_trade
                changed = True
        kept.append(rec)
    if changed:
        _write_index(kept)
    try:
        marker.write_text(LIBRARY_SET_VERSION, encoding="utf-8")
    except Exception:
        pass


def _restyle_masters_if_needed() -> None:
    """When STYLE_VERSION changes, re-wrap every stored master so existing
    documents pick up the new letterhead/style. Re-wrapping preserves each
    master's body content (and any user edits) — only the branded chrome and
    CSS are refreshed. Runs once per version, then records the marker."""
    marker = _style_marker_path()
    try:
        current = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    except Exception:
        current = ""
    if current == STYLE_VERSION:
        return
    try:
        from . import compliance_kb as _kb
    except Exception:
        _kb = None
    for rec in _read_index_raw():
        f = LIBRARY_DIR / rec.get("file", "")
        if not f.is_file():
            continue
        mid = rec.get("id", "")
        try:
            # Program masters: regenerate from the KB so any change to the
            # template body (e.g. the new client letterhead) flows into the
            # already-stored masters — not just the CSS chrome.
            if _kb is not None and mid.startswith("program-"):
                kid = mid[len("program-"):]
                md = _kb.render_program(kid)
                if md:
                    krec = _kb.get(kid) or {}
                    f.write_text(
                        wrap_program_document(_md_to_html(md), rec.get("title", ""),
                                              krec.get("citation", "")),
                        encoding="utf-8")
                    continue
            # JHA companions: regenerate from the JHA engine so the authored
            # matrices + the new premium shell flow into stored masters.
            if mid.startswith("jha-"):
                try:
                    from . import compliance_jha as _jha
                    inner = _jha.render_jha(mid[len("jha-"):])
                except Exception:
                    inner = None
                if inner:
                    f.write_text(wrap_document(inner, rec.get("title", mid)),
                                 encoding="utf-8")
                    continue
            # Legacy trade starter templates: strip the old inline letterhead,
            # normalize the placeholder tokens, and apply the premium cover so
            # the whole asset library shares one look and one fill flow.
            old = f.read_text(encoding="utf-8", errors="replace")
            if _is_legacy_template(old):
                f.write_text(
                    wrap_legacy_program_document(old, rec.get("title", mid)),
                    encoding="utf-8")
                continue
            # Everything else: re-wrap in place (preserves body, refreshes shell).
            new = wrap_document(old, rec.get("title", mid))
            if new != old:
                f.write_text(new, encoding="utf-8")
        except Exception:
            continue
    try:
        marker.write_text(STYLE_VERSION, encoding="utf-8")
    except Exception:
        pass


def _md_to_html(md: str) -> str:
    """Convert a program-template markdown body (frontmatter stripped) to HTML."""
    body = md
    if body.lstrip().startswith("---"):
        parts = body.split("---", 2)
        if len(parts) == 3:
            body = parts[2]
    try:
        import markdown as _md
        inner = _md.markdown(body, extensions=["tables", "sane_lists"])
    except Exception:
        # Fallback so a missing lib never renders raw markdown/HTML as literal
        # text. Handles headings, bold, and lists; passes through lines that are
        # already HTML (e.g. an embedded letterhead <table>) instead of escaping.
        inner = _md_to_html_fallback(body)
    return inner


def _md_to_html_fallback(body: str) -> str:
    """Best-effort markdown->HTML without the `markdown` package. Not a full
    parser — just enough that a bodies never renders as escaped source."""
    def _inline(t: str) -> str:
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", t)
        return t
    out: list[str] = []
    in_list = False
    for ln in body.splitlines():
        s = ln.rstrip()
        stripped = s.strip()
        if not stripped:
            if in_list:
                out.append("</ul>"); in_list = False
            continue
        # Line is already HTML (raw table, div, etc.) — pass through untouched.
        if stripped.startswith("<"):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(s)
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            if in_list:
                out.append("</ul>"); in_list = False
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(_esc(m.group(2)))}</h{lvl}>")
            continue
        m = re.match(r"^[-*+]\s+(.*)$", stripped)
        if m:
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{_inline(_esc(m.group(1)))}</li>")
            continue
        if in_list:
            out.append("</ul>"); in_list = False
        out.append(f"<p>{_inline(_esc(stripped))}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


def _sync_program_masters() -> None:
    """Add a library master for every KB written-program template not already
    present. Master id is ``program-<kb_id>``; grouped in the Library by the
    standard's KB category. Safe to run on every startup."""
    try:
        from . import compliance_kb as _kb
    except Exception:
        return
    records = _read_index_raw()
    have = {r["id"] for r in records}
    added = False
    for r in _kb.all_records():
        wp = str(r.get("written_program", "")).strip().lower()
        if wp not in ("yes", "conditional"):
            continue
        mid = f"program-{r['id']}"
        if mid in have:
            continue
        md = _kb.render_program(r["id"])
        if not md:
            continue
        title = r.get("title", r["id"])
        html = wrap_program_document(_md_to_html(md), title, r.get("citation", ""))
        fname = f"{mid}.html"
        (LIBRARY_DIR / fname).write_text(html, encoding="utf-8")
        records.append({"id": mid, "trade": r.get("category", "Compliance Programs"),
                        "num": "", "title": title, "file": fname})
        have.add(mid)
        added = True
    if added:
        _write_index(records)


def _sync_jha_masters() -> None:
    """Add a library master for every authored Job Hazard Analysis (from the JHA
    engine) whose companion isn't already present. Master id is ``jha-<kb_id>``,
    grouped alongside its written program. Safe to run on every startup — this
    also back-fills JHAs onto a volume that was seeded before they existed."""
    try:
        from . import compliance_jha as _jha
        from . import compliance_kb as _kb
    except Exception:
        return
    records = _read_index_raw()
    have = {r["id"] for r in records}
    added = False
    for pid in _jha.list_jha_ids():
        mid = f"jha-{pid}"
        if mid in have:
            continue
        inner = _jha.render_jha(pid)
        if not inner:
            continue
        rec = _kb.get(pid) or {}
        title = _jha.jha_title(pid)
        html = wrap_document(inner, title)
        fname = f"{mid}.html"
        (LIBRARY_DIR / fname).write_text(html, encoding="utf-8")
        records.append({"id": mid, "trade": rec.get("category", "Compliance Programs"),
                        "num": "", "title": title, "file": fname})
        have.add(mid)
        added = True
    if added:
        _write_index(records)


def _read_index_raw() -> List[Dict[str, Any]]:
    """Read the live index without triggering ensure_library (avoids recursion)."""
    try:
        return json.loads(_index_path().read_text(encoding="utf-8"))
    except Exception:
        return []


def _read_index() -> List[Dict[str, Any]]:
    ensure_library()
    try:
        return json.loads(_index_path().read_text(encoding="utf-8"))
    except Exception:
        return []


def _write_index(records: List[Dict[str, Any]]) -> None:
    _index_path().write_text(json.dumps(records, indent=2), encoding="utf-8")


def list_templates() -> List[Dict[str, Any]]:
    return [{"id": t["id"], "trade": t.get("trade", ""), "num": t.get("num", ""),
             "title": t.get("title", t["id"])} for t in _read_index()]


def _record(mid: str) -> Optional[Dict[str, Any]]:
    for t in _read_index():
        if t["id"] == mid:
            return t
    return None


def read_master_html(mid: str) -> Optional[str]:
    rec = _record(mid)
    if not rec:
        return None
    f = LIBRARY_DIR / rec["file"]
    return f.read_text(encoding="utf-8", errors="replace") if f.is_file() else None


def master_title(mid: str) -> str:
    return (_record(mid) or {}).get("title", mid)


def save_master_html(mid: str, html: str) -> bool:
    rec = _record(mid)
    if not rec:
        return False
    (LIBRARY_DIR / rec["file"]).write_text(html, encoding="utf-8")
    return True


def _slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", s or "").strip("-").lower() or "master"


def add_master(title: str, html: Optional[str] = None,
               trade: str = "My Library") -> Dict[str, Any]:
    """Create a new master in the library (blank or from provided HTML)."""
    ensure_library()
    records = _read_index()
    base = _slug(title)
    mid = base
    existing = {r["id"] for r in records}
    n = 2
    while mid in existing:
        mid = f"{base}-{n}"
        n += 1
    body = html if (html and html.strip()) else (
        f"<h1>{_esc(title)}</h1><p>Start writing your program here…</p>")
    doc = wrap_document(body, title)
    fname = f"{mid}.html"
    (LIBRARY_DIR / fname).write_text(doc, encoding="utf-8")
    rec = {"id": mid, "trade": trade, "num": "", "title": title, "file": fname}
    records.append(rec)
    _write_index(records)
    return {"id": mid, "trade": trade, "num": "", "title": title}


# ── Ingest uploaded files (HTML or Word .docx) ───────────────────────────────
def html_from_docx(raw: bytes) -> str:
    """Convert a Word .docx (raw bytes) into an HTML fragment."""
    import io
    import mammoth  # added to Dockerfile pip line
    result = mammoth.convert_to_html(io.BytesIO(raw))
    return result.value or ""


def ingest_upload(filename: str, raw: bytes) -> Dict[str, Any]:
    """Add an uploaded file to the library. Supports .docx and .html/.htm.

    For files named like "Trucking - Corporate - 07 Hazard Communication
    Program.docx" the first two segments become the library group (trade) so
    related programs cluster together; the remainder becomes the title.
    """
    name = filename or "Uploaded document"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    stem = name.rsplit(".", 1)[0]
    parts = [x.strip() for x in stem.split(" - ")]
    trade, title = "My Library", stem
    # "Industry - Style - NN Program"  ->  group by INDUSTRY, keep style in title
    if len(parts) >= 3:
        trade = parts[0]
        title = " - ".join(parts[2:]) + f" — {parts[1]}"
    elif len(parts) == 2:
        trade, title = parts[0], parts[1]
    if ext == "docx":
        try:
            inner = html_from_docx(raw)
        except Exception as e:  # pragma: no cover
            inner = (f"<h1>{_esc(title)}</h1>"
                     f"<p>Could not read this Word file: {_esc(str(e))}</p>")
    else:
        inner = raw.decode("utf-8", errors="replace")
    return add_master(title, inner, trade=trade)


# ── Assign master -> customer project (copy-on-assign) ───────────────────────
def safe_filename(title: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]+', "", title).strip() or "Compliance Document"
    return f"{name}.html"


def unique_path(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(filename).stem, (Path(filename).suffix or ".html")
    cand = directory / f"{stem}{suffix}"
    n = 2
    while cand.exists():
        cand = directory / f"{stem} ({n}){suffix}"
        n += 1
    return cand


# ── PDF rendering ────────────────────────────────────────────────────────────
def render_pdf(html: str, out_path: Path, title: str = "") -> None:
    try:
        from xhtml2pdf import pisa  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "PDF rendering needs the 'xhtml2pdf' package. Add it to the "
            "Dockerfile pip install line and redeploy."
        ) from e
    doc = wrap_document(html, title)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        result = pisa.CreatePDF(src=doc, dest=fh)
    if result.err:
        raise RuntimeError("PDF rendering failed while converting the document.")


# ── Email dispatch ───────────────────────────────────────────────────────────
def smtp_configured() -> bool:
    return bool(os.environ.get("ORIGIN_SMTP_HOST") and os.environ.get("ORIGIN_SMTP_USER")
                and os.environ.get("ORIGIN_SMTP_PASS"))


def resend_configured() -> bool:
    return bool(os.environ.get("RESEND_API_KEY"))


def _mail_from() -> str:
    """The verified sender address for the email envelope. This is the From on
    the EMAIL (Chris's own business writing to his client) — separate from the
    document content, which stays unbranded."""
    return (os.environ.get("ORIGIN_MAIL_FROM")
            or os.environ.get("ORIGIN_SMTP_FROM")
            or os.environ.get("ORIGIN_SMTP_USER")
            or "info@originmanagementsolutions.com")


def _mail_reply_to() -> str:
    """Where client replies should land. The From address must be on a Resend-
    VERIFIED domain (e.g. originprequal.com on Cloudflare), but replies should go
    to the real business inbox (info@originmanagementsolutions.com on Wix).
    Defaults to that inbox so replies reach Chris even if the From is a different
    domain; override with ORIGIN_MAIL_REPLY_TO."""
    return (os.environ.get("ORIGIN_MAIL_REPLY_TO")
            or "info@originmanagementsolutions.com")


def _collect_files(attachment=None, attachments=None) -> List[Path]:
    """Normalize a single attachment and/or a list into one de-duplicated list of
    existing files. Keeps the old single-attachment callers working while letting
    newer callers (e.g. emailing a client all their finished docs) pass many."""
    out: List[Path] = []
    seen = set()
    for item in list(attachments or []) + ([attachment] if attachment else []):
        if not item:
            continue
        p = Path(item)
        try:
            if not p.is_file():
                continue
        except Exception:
            continue
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _send_via_resend(to: str, subject: str, body: str,
                     attachment: Optional[Path] = None,
                     attachments=None) -> Dict[str, Any]:
    """Send over Resend's HTTPS API (port 443). Works on every Railway plan —
    unlike raw SMTP, which Railway blocks on Free/Trial/Hobby plans."""
    import json as _json
    import base64 as _b64
    import urllib.request as _url
    payload: Dict[str, Any] = {
        "from": _mail_from(),
        "to": [to],
        "subject": subject or "Compliance document",
        "text": body or "Please find the attached compliance document.",
    }
    reply_to = _mail_reply_to()
    if reply_to and reply_to != _mail_from():
        payload["reply_to"] = [reply_to]
    files = _collect_files(attachment, attachments)
    if files:
        payload["attachments"] = [{
            "filename": p.name,
            "content": _b64.b64encode(p.read_bytes()).decode("ascii"),
        } for p in files]
    req = _url.Request(
        "https://api.resend.com/emails",
        data=_json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
            "Content-Type": "application/json",
            # Resend's API is behind Cloudflare, which 403s (error 1010) the
            # default "Python-urllib" agent as a bot. A normal UA clears it.
            "User-Agent": "OriginManagementSolutions/1.0 (+https://originmanagementsolutions.com)",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with _url.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read().decode("utf-8") or "{}")
        return {"sent": True, "to": to, "id": data.get("id")}
    except _url.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            pass
        return {"sent": False,
                "error": f"Resend API error ({e.code}): {detail or e.reason}. "
                         "Check that RESEND_API_KEY is valid and the From domain "
                         "is verified in Resend."}
    except Exception as e:
        return {"sent": False, "error": f"Resend send failed: {e}"}


def send_email(to: str, subject: str, body: str,
               attachment: Optional[Path] = None,
               attachments=None) -> Dict[str, Any]:
    # Prefer Resend (HTTPS) — reliable on every Railway plan. Fall back to SMTP
    # if only SMTP is configured (works on Railway Pro or off-Railway hosts).
    if resend_configured():
        return _send_via_resend(to, subject, body, attachment, attachments)
    if not smtp_configured():
        return {"sent": False,
                "error": "Email isn't set up yet. Add a RESEND_API_KEY (recommended — "
                         "works on any Railway plan), or set ORIGIN_SMTP_HOST/USER/PASS "
                         "(SMTP needs Railway Pro). The PDF was still saved to the project."}
    host = os.environ["ORIGIN_SMTP_HOST"]
    port = int(os.environ.get("ORIGIN_SMTP_PORT", "587"))
    user = os.environ["ORIGIN_SMTP_USER"]
    pw = os.environ["ORIGIN_SMTP_PASS"]
    sender = os.environ.get("ORIGIN_SMTP_FROM", user)
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    reply_to = _mail_reply_to()
    if reply_to and reply_to != sender:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject or "Compliance document"
    msg.set_content(body or "Please find the attached compliance document.")
    for p in _collect_files(attachment, attachments):
        data = p.read_bytes()
        sub = "pdf" if p.suffix.lower() == ".pdf" else "octet-stream"
        msg.add_attachment(data, maintype="application", subtype=sub,
                           filename=p.name)
    # Railway containers often have an IPv6 address but no routable IPv6 egress,
    # so smtplib picking the AAAA record fails with "[Errno 101] Network is
    # unreachable". Force IPv4 resolution for the duration of the send, and try
    # STARTTLS:587 first, then SSL:465 as a fallback (some networks block 587).
    import socket as _socket
    _real_gai = _socket.getaddrinfo

    def _ipv4_only(*a, **k):
        res = _real_gai(*a, **k)
        v4 = [r for r in res if r[0] == _socket.AF_INET]
        return v4 or res

    def _attempt(p: int, use_ssl: bool):
        if use_ssl:
            with smtplib.SMTP_SSL(host, p, timeout=30) as s:
                s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, p, timeout=30) as s:
                s.ehlo()
                s.starttls()
                s.login(user, pw)
                s.send_message(msg)

    _socket.getaddrinfo = _ipv4_only
    try:
        try:
            _attempt(port, use_ssl=(port == 465))
        except OSError:
            # Primary transport unreachable/blocked — try the other common port.
            alt_port, alt_ssl = (465, True) if port != 465 else (587, False)
            _attempt(alt_port, use_ssl=alt_ssl)
        return {"sent": True, "to": to}
    except Exception as e:
        return {"sent": False, "error": f"SMTP send failed: {e}"}
    finally:
        _socket.getaddrinfo = _real_gai
