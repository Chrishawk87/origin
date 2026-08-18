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
STYLE_VERSION = "2026-08-18-premium-2"

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

# Palette: navy #0f2c4c, ink #1f2933, slate #475569, muted #64748b, hair #e2e8f0
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
            html = wrap_document(src.read_text(encoding="utf-8"), title)
            fname = f"{s['id']}.html"
            (LIBRARY_DIR / fname).write_text(html, encoding="utf-8")
            records.append({"id": s["id"], "trade": s.get("trade", ""),
                            "num": s.get("num", ""), "title": title, "file": fname})
        _write_index(records)
    _sync_program_masters()
    _restyle_masters_if_needed()


def _style_marker_path() -> Path:
    return LIBRARY_DIR / ".style_version"


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
                md = _kb.render_program(mid[len("program-"):])
                if md:
                    f.write_text(wrap_document(_md_to_html(md), rec.get("title", "")),
                                 encoding="utf-8")
                    continue
            # Everything else: re-wrap in place (preserves body, refreshes shell).
            old = f.read_text(encoding="utf-8", errors="replace")
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
        # minimal fallback so a missing lib never breaks the library
        inner = "".join(f"<p>{_esc(ln)}</p>" for ln in body.splitlines() if ln.strip())
    return inner


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
        html = wrap_document(_md_to_html(md), title)
        fname = f"{mid}.html"
        (LIBRARY_DIR / fname).write_text(html, encoding="utf-8")
        records.append({"id": mid, "trade": r.get("category", "Compliance Programs"),
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


# ── Email dispatch (SMTP) ────────────────────────────────────────────────────
def smtp_configured() -> bool:
    return bool(os.environ.get("ORIGIN_SMTP_HOST") and os.environ.get("ORIGIN_SMTP_USER")
                and os.environ.get("ORIGIN_SMTP_PASS"))


def send_email(to: str, subject: str, body: str,
               attachment: Optional[Path] = None) -> Dict[str, Any]:
    if not smtp_configured():
        return {"sent": False,
                "error": "Email isn't set up yet. Set ORIGIN_SMTP_HOST, "
                         "ORIGIN_SMTP_USER and ORIGIN_SMTP_PASS to send directly. "
                         "The PDF was still saved to the project."}
    host = os.environ["ORIGIN_SMTP_HOST"]
    port = int(os.environ.get("ORIGIN_SMTP_PORT", "587"))
    user = os.environ["ORIGIN_SMTP_USER"]
    pw = os.environ["ORIGIN_SMTP_PASS"]
    sender = os.environ.get("ORIGIN_SMTP_FROM", user)
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject or "Compliance document"
    msg.set_content(body or "Please find the attached compliance document.")
    if attachment and attachment.is_file():
        data = attachment.read_bytes()
        sub = "pdf" if attachment.suffix.lower() == ".pdf" else "octet-stream"
        msg.add_attachment(data, maintype="application", subtype=sub,
                           filename=attachment.name)
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
