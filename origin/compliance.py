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


# ── Styling / document wrapper ───────────────────────────────────────────────
_DOC_CSS = """
<style>
  body{font-family:'Segoe UI',Arial,sans-serif;color:#1a1a1a;line-height:1.5;
       max-width:800px;margin:0 auto;padding:24px;font-size:13px}
  h1,h2,h3{color:#0f2c4c;margin:0.6em 0 0.3em}
  h1{font-size:20px;border-bottom:2px solid #0f2c4c;padding-bottom:6px}
  h2{font-size:16px} h3{font-size:14px}
  table{border-collapse:collapse;width:100%;margin:10px 0}
  td,th{border:1px solid #cbd5e1;padding:6px 8px;vertical-align:top}
  p{margin:0.4em 0}
  strong{color:#0f2c4c}
</style>
"""


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def wrap_document(inner_html: str, title: str = "") -> str:
    if "<html" in (inner_html or "").lower():
        return inner_html
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{_esc(title)}</title>{_DOC_CSS}</head>"
            f"<body>{inner_html}</body></html>")


# ── Library persistence ──────────────────────────────────────────────────────
def _index_path() -> Path:
    return LIBRARY_DIR / "index.json"


def ensure_library() -> None:
    """Create the live library from shipped defaults on first use. Idempotent."""
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    if _index_path().is_file():
        return
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
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        return {"sent": True, "to": to}
    except Exception as e:
        return {"sent": False, "error": f"SMTP send failed: {e}"}
