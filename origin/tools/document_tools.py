"""Document reader — turn an uploaded file (incl. scanned PDFs) into text.

Origin can accept an upload and read plain-text files, but had no way to read a
PDF — and especially not a *scanned* one. This module fills that gap so the
agent can ingest an ISNetworld / Avetta / Veriforce / PEC / BROWZ report the
customer drops in and actually understand it.

Extraction strategy (best-effort, degrades gracefully if a library is absent):
  - PDF  : PyMuPDF pulls the embedded text layer per page (digital exports).
           A page with almost no text is treated as a *scan*: the page is
           rendered to an image and run through Tesseract OCR.
  - Image: (png/jpg/jpeg/tiff/bmp/webp) → Tesseract OCR directly.
  - DOCX : mammoth extracts the raw text.
  - Text : (txt/md/html/csv/json/log) read straight off disk.

Everything is optional at import time: if PyMuPDF or Tesseract isn't installed
the reader returns a clear message instead of raising, so the rest of Origin
keeps working.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from .base import Tool

# Pages with fewer than this many extracted characters are assumed to be scans
# and sent to OCR instead of trusting the (near-empty) text layer.
_SCAN_CHAR_THRESHOLD = 25
# Hard cap on returned text so a huge document can't blow up the context.
_MAX_CHARS = 60000

_TEXT_EXT = {"txt", "md", "markdown", "html", "htm", "csv", "tsv", "json", "log", "yaml", "yml"}
_IMAGE_EXT = {"png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp", "gif"}


def _ext(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if "." in os.path.basename(path) else ""


def _resolve(path: str) -> str:
    """Accept absolute paths, ~ paths, or names relative to the project
    workspace (the process cwd, which the server sets on project open)."""
    p = os.path.expanduser(path or "")
    if os.path.isabs(p) and os.path.exists(p):
        return p
    # relative to cwd (the open project's workspace)
    if os.path.exists(p):
        return os.path.abspath(p)
    # common upload landing spots inside a workspace
    for base in (".", "uploads", "outputs"):
        cand = os.path.join(base, path)
        if os.path.exists(cand):
            return os.path.abspath(cand)
    return p  # let the caller report "not found"


def _ocr_image(image) -> str:
    """OCR a PIL image with Tesseract. Returns '' if OCR isn't available."""
    try:
        import pytesseract  # type: ignore
    except Exception:
        return ""
    try:
        return pytesseract.image_to_string(image) or ""
    except Exception:
        return ""


def _read_pdf(path: str) -> str:
    try:
        import fitz  # PyMuPDF
    except Exception:
        try:
            import pymupdf as fitz  # type: ignore
        except Exception:
            return ("ERROR: can't read PDFs — PyMuPDF isn't installed. "
                    "Add `pymupdf` to the image (it ships in the Docker build).")
    try:
        from io import BytesIO
        try:
            from PIL import Image  # noqa: F401
            _have_pil = True
        except Exception:
            _have_pil = False

        doc = fitz.open(path)
        out: List[str] = []
        scanned_pages = 0
        for i, page in enumerate(doc):
            text = (page.get_text() or "").strip()
            if len(text) < _SCAN_CHAR_THRESHOLD and _have_pil:
                # Looks like a scan — rasterize and OCR.
                try:
                    from PIL import Image
                    pix = page.get_pixmap(dpi=220)
                    img = Image.open(BytesIO(pix.tobytes("png")))
                    ocr = _ocr_image(img).strip()
                    if ocr:
                        text = ocr
                        scanned_pages += 1
                except Exception:
                    pass
            out.append(f"----- page {i + 1} -----\n{text}".rstrip())
        doc.close()
        body = "\n\n".join(out).strip()
        if not body:
            return ("(No text could be extracted. If this is a scanned document, "
                    "OCR (Tesseract) may not be installed in this environment.)")
        head = ""
        if scanned_pages:
            head = f"[read via OCR on {scanned_pages} scanned page(s)]\n\n"
        return (head + body)[:_MAX_CHARS]
    except Exception as e:
        return f"ERROR reading PDF {path}: {e}"


def _read_image(path: str) -> str:
    try:
        from PIL import Image
    except Exception:
        return "ERROR: can't OCR images — Pillow isn't installed."
    try:
        txt = _ocr_image(Image.open(path)).strip()
        return txt[:_MAX_CHARS] if txt else (
            "(No text found in the image. OCR (Tesseract) may not be installed.)")
    except Exception as e:
        return f"ERROR reading image {path}: {e}"


def _read_docx(path: str) -> str:
    try:
        import mammoth
        with open(path, "rb") as fh:
            return (mammoth.extract_raw_text(fh).value or "")[:_MAX_CHARS]
    except Exception as e:
        return f"ERROR reading docx {path}: {e}"


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()[:_MAX_CHARS]
    except Exception as e:
        return f"ERROR reading {path}: {e}"


def extract_text(path: str) -> str:
    """Public helper other tools (e.g. compliance_intake) call to get the text
    of any supported document, including scanned PDFs."""
    real = _resolve(path)
    if not os.path.exists(real):
        return f"ERROR: file not found: {path}"
    ext = _ext(real)
    if ext == "pdf":
        return _read_pdf(real)
    if ext in _IMAGE_EXT:
        return _read_image(real)
    if ext == "docx":
        return _read_docx(real)
    if ext in _TEXT_EXT or ext == "":
        return _read_text(real)
    # Unknown binary type — try text as a last resort.
    return _read_text(real)


def build_document_tools() -> List[Tool]:
    def read_document(args: Dict[str, Any]) -> str:
        path = (args.get("path") or args.get("file") or "").strip()
        if not path:
            return "ERROR: 'path' (the file to read) is required."
        return extract_text(path)

    return [
        Tool(
            name="read_document",
            description=(
                "Read the text out of an uploaded document — including a SCANNED "
                "PDF or an image — so you can analyze it. Handles PDF (digital or "
                "scanned, via OCR), images (png/jpg/tiff), Word (.docx), and plain "
                "text. Use this to ingest an ISNetworld / Avetta / Veriforce / PEC "
                "requirement report, scorecard, or rejection notice before deciding "
                "what the contractor needs. For a compliance report specifically, "
                "prefer compliance_intake, which reads it AND maps it to the fixes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "File name in the project workspace, or an absolute path."},
                },
                "required": ["path"],
            },
            handler=read_document,
            source="builtin",
        ),
    ]
