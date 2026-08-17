"""Compliance document management — reusable master templates, assign-to-project
(copy-on-assign so masters are never mutated), PDF rendering, and direct email
dispatch. Built to fit Origin's projects-as-folders model: a template assigned
into a project becomes an editable file inside that project's workdir.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional

LIBRARY_DIR = Path(__file__).parent / "compliance_library"
# Where assigned copies live inside a project's workdir.
ASSIGN_SUBDIR = "Compliance"


# ── Master template library (read-only) ─────────────────────────────────────
def list_templates() -> List[Dict[str, Any]]:
    idx = LIBRARY_DIR / "index.json"
    if not idx.is_file():
        return []
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
    except Exception:
        return []
    # Never leak the on-disk filename to clients; expose id/title/trade/num only.
    return [{"id": t["id"], "trade": t.get("trade", ""), "num": t.get("num", ""),
             "title": t.get("title", t["id"])} for t in data]


def _template_record(tid: str) -> Optional[Dict[str, Any]]:
    idx = LIBRARY_DIR / "index.json"
    if not idx.is_file():
        return None
    for t in json.loads(idx.read_text(encoding="utf-8")):
        if t["id"] == tid:
            return t
    return None


def read_template_html(tid: str) -> Optional[str]:
    rec = _template_record(tid)
    if not rec:
        return None
    f = LIBRARY_DIR / rec["file"]
    return f.read_text(encoding="utf-8") if f.is_file() else None


def template_title(tid: str) -> str:
    rec = _template_record(tid)
    return (rec or {}).get("title", tid)


# ── HTML document wrapper (used for viewing/editing/printing) ────────────────
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


def wrap_document(inner_html: str, title: str = "") -> str:
    """Wrap raw template HTML into a standalone, styled document."""
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{_esc(title)}</title>{_DOC_CSS}</head>"
            f"<body>{inner_html}</body></html>")


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── PDF rendering (HTML -> PDF) ──────────────────────────────────────────────
def render_pdf(html: str, out_path: Path, title: str = "") -> None:
    """Render an HTML document to a PDF file. Uses xhtml2pdf (pure-Python, no
    system libraries — deploys cleanly on Railway). Raises RuntimeError with a
    clear message if the dependency is missing."""
    try:
        from xhtml2pdf import pisa  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "PDF rendering needs the 'xhtml2pdf' package. Add it to requirements "
            "and redeploy (pip install xhtml2pdf)."
        ) from e
    doc = html if "<html" in html.lower() else wrap_document(html, title)
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
    """Send an email with an optional file attachment via configured SMTP.
    Config comes from env vars so no credentials live in code:
      ORIGIN_SMTP_HOST, ORIGIN_SMTP_PORT (default 587),
      ORIGIN_SMTP_USER, ORIGIN_SMTP_PASS, ORIGIN_SMTP_FROM (default = user).
    """
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
        maintype = "application"
        msg.add_attachment(data, maintype=maintype, subtype=sub,
                           filename=attachment.name)
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        return {"sent": True, "to": to}
    except Exception as e:
        return {"sent": False, "error": f"SMTP send failed: {e}"}


def unique_path(directory: Path, filename: str) -> Path:
    """Return a non-colliding path in `directory` for `filename` so assigning a
    template twice keeps both copies (adds ' (2)', ' (3)', …)."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = Path(filename).stem
    suffix = Path(filename).suffix or ".html"
    cand = directory / f"{stem}{suffix}"
    n = 2
    while cand.exists():
        cand = directory / f"{stem} ({n}){suffix}"
        n += 1
    return cand


def safe_filename(title: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]+', "", title).strip() or "Compliance Document"
    return f"{name}.html"
