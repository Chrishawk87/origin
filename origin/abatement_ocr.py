"""Origin Abatement — client document reader (Phase 2, Gap #4).

When a cited employer (the client) uploads a document as evidence — a Certificate
of Insurance, a hazardous-waste manifest, a training certificate, a signed policy —
the attorney should not have to re-key it. This module READS the document and
returns the labeled fields it recognizes, so the firm side can show "here is what
this file says" next to the file itself.

The same house guardrails as abatement_intake.py apply, in force:

  * NEVER fabricate. A scanned image with no embedded text is flagged
    `needs_ocr_or_manual` — we do NOT guess its contents. Text-layer PDFs and
    plain-text uploads are read deterministically with a labeled-field regex pass.
  * Every extracted value is returned flagged "AI EXTRACTED — VERIFY". Extraction
    is a convenience for the attorney; the attorney confirms every value.
  * This module only READS. It writes nothing, verifies nothing, and its output is
    stored OUTSIDE the evidence's sealed audit block (see evidence_vault.attach_
    extraction) because it is derived, mutable data — not an immutable capture fact.

Deterministic, offline, no model required. A model-assisted OCR path can be layered
later; its output would land in exactly the same VERIFY-flagged shape.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

EXTRACTION_FLAG = "AI EXTRACTED — VERIFY"

# Document kinds we can recognize from their text. Order matters: the first whose
# signal patterns hit wins. Everything else is "document" (still readable, just not
# specially structured).
_DOC_SIGNALS: List[tuple] = [
    ("certificate_of_insurance", re.compile(
        r"certificate\s+of\s+(?:liability\s+)?insurance|ACORD|"
        r"\bcertificate\s+holder\b|\binsured\b.*\bpolicy\b", re.IGNORECASE | re.DOTALL)),
    ("waste_manifest", re.compile(
        r"uniform\s+hazardous\s+waste\s+manifest|manifest\s+tracking\s+number|"
        r"\bgenerator'?s?\b.*\bEPA\s*ID\b|designated\s+facility", re.IGNORECASE | re.DOTALL)),
    ("training_certificate", re.compile(
        r"certificate\s+of\s+completion|this\s+certifies\s+that|"
        r"has\s+(?:successfully\s+)?completed|course\s+completion", re.IGNORECASE | re.DOTALL)),
]

# Common date shapes (mm/dd/yyyy, yyyy-mm-dd, "Jan 3, 2026").
_DATE = (r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|"
         r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})")
_MONEY = r"\$\s?[\d,]+(?:\.\d{2})?"
_EPA_ID = r"[A-Z]{2}[A-Z0-9]{9,12}"  # e.g. TXR000012345 / TXD988091234


def _labeled(text: str, label_pat: str, value_pat: str,
             flags: int = re.IGNORECASE) -> str:
    """Find `label: value` on the same line. Returns the first hit, trimmed."""
    m = re.search(label_pat + r"\s*[:#]?\s*(" + value_pat + r")", text, flags)
    return (m.group(1).strip() if m else "")


def _first(text: str, value_pat: str, flags: int = re.IGNORECASE) -> str:
    m = re.search(value_pat, text, flags)
    return (m.group(0).strip() if m else "")


def _field(value: str) -> Dict[str, str]:
    """Wrap an extracted value with its mandatory verify flag."""
    return {"value": value, "flag": EXTRACTION_FLAG}


# ── per-doc-type field extractors ────────────────────────────────────────────────
def _extract_coi(text: str) -> Dict[str, Dict[str, str]]:
    """Certificate of Insurance — the fields a prequal/abatement reviewer needs."""
    out: Dict[str, Dict[str, str]] = {}
    insured = _labeled(text, r"insured", r"[A-Za-z0-9 .,&'\-]{3,80}")
    carrier = _labeled(text, r"(?:insurer\s*[a-e]?|carrier|company\s+affording\s+coverage)",
                       r"[A-Za-z0-9 .,&'\-]{3,80}")
    policy = _labeled(text, r"policy\s*(?:number|no|#)?", r"[A-Za-z0-9\-]{5,30}")
    # COIs list effective + expiration side by side; grab the two dates near a
    # policy period label, else the first two dates in the doc.
    dates = re.findall(_DATE, text, re.IGNORECASE)
    eff = _labeled(text, r"(?:policy\s+eff|eff(?:ective)?\s*date)", _DATE)
    exp = _labeled(text, r"(?:policy\s+exp|exp(?:iration|ires?)?\s*date)", _DATE)
    if not exp and len(dates) >= 2:
        exp = dates[1]
    if not eff and dates:
        eff = dates[0]
    each = _labeled(text, r"each\s+occurrence", _MONEY)
    genagg = _labeled(text, r"general\s+aggregate", _MONEY)
    if insured:
        out["insured"] = _field(insured)
    if carrier:
        out["carrier"] = _field(carrier)
    if policy:
        out["policy_number"] = _field(policy)
    if eff:
        out["effective_date"] = _field(eff)
    if exp:
        out["expiration_date"] = _field(exp)
    if each:
        out["each_occurrence_limit"] = _field(each)
    if genagg:
        out["general_aggregate_limit"] = _field(genagg)
    return out


def _extract_manifest(text: str) -> Dict[str, Dict[str, str]]:
    """Uniform Hazardous Waste Manifest — EPA/RCRA tracking fields."""
    out: Dict[str, Dict[str, str]] = {}
    tracking = _labeled(text, r"manifest\s+tracking\s+(?:number|no|#)?",
                        r"[A-Za-z0-9]{6,15}")
    epa_ids = re.findall(_EPA_ID, text)
    generator = _labeled(text, r"generator'?s?\s+(?:name|us\s+epa\s+id)",
                         r"[A-Za-z0-9 .,&'\-]{3,80}")
    transporter = _labeled(text, r"transporter\s*\d*\s*(?:company\s+name)?",
                           r"[A-Za-z0-9 .,&'\-]{3,80}")
    facility = _labeled(text, r"designated\s+facility(?:\s+name)?",
                        r"[A-Za-z0-9 .,&'\-]{3,80}")
    if tracking:
        out["manifest_tracking_number"] = _field(tracking)
    if epa_ids:
        out["epa_id_numbers"] = _field(", ".join(dict.fromkeys(epa_ids)))
    if generator:
        out["generator"] = _field(generator)
    if transporter:
        out["transporter"] = _field(transporter)
    if facility:
        out["designated_facility"] = _field(facility)
    return out


def _extract_training(text: str) -> Dict[str, Dict[str, str]]:
    """Training / completion certificate."""
    out: Dict[str, Dict[str, str]] = {}
    name = _labeled(text, r"(?:this\s+certifies\s+that|awarded\s+to|name)",
                    r"[A-Za-z .,'\-]{3,60}")
    course = _labeled(text, r"(?:course|training|program|completed)",
                      r"[A-Za-z0-9 .,&'/\-]{3,80}")
    completed = _labeled(text, r"(?:date\s+completed|completion\s+date|completed\s+on|date)", _DATE)
    expires = _labeled(text, r"(?:expires?|expiration|valid\s+(?:through|until))", _DATE)
    dates = re.findall(_DATE, text, re.IGNORECASE)
    if not completed and dates:
        completed = dates[0]
    if name:
        out["trainee_name"] = _field(name)
    if course:
        out["course"] = _field(course)
    if completed:
        out["completed_date"] = _field(completed)
    if expires:
        out["expiration_date"] = _field(expires)
    return out


_EXTRACTORS = {
    "certificate_of_insurance": _extract_coi,
    "waste_manifest": _extract_manifest,
    "training_certificate": _extract_training,
}


# ── text acquisition (text-layer PDF / plain text; never OCR-guesses images) ─────
def _pdf_text(content: bytes) -> Dict[str, Any]:
    """Pull the embedded text layer from a PDF. A scanned image PDF has none — we
    say so rather than guess. Mirrors abatement_intake.parse_pdf_bytes."""
    try:
        import io
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(io.BytesIO(content or b""))
        text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
    except Exception as exc:
        return {"ok": False, "text": "", "reason": "pdf_text_extract_failed",
                "note": f"Could not read text from this PDF ({exc})."}
    if len(text.strip()) < 20:
        return {"ok": False, "text": "", "reason": "needs_ocr_or_manual",
                "note": ("This looks like a scanned image with no embedded text. "
                         "OCR or manual entry required — no content was guessed.")}
    return {"ok": True, "text": text, "reason": ""}


def _plain_text(content: bytes) -> Dict[str, Any]:
    try:
        text = (content or b"").decode("utf-8", errors="replace")
    except Exception:
        return {"ok": False, "text": "", "reason": "decode_failed",
                "note": "Could not decode this file as text."}
    if len(text.strip()) < 3:
        return {"ok": False, "text": "", "reason": "empty",
                "note": "This file has no readable text."}
    return {"ok": True, "text": text, "reason": ""}


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".webp"}
_PDF_EXTS = {".pdf"}
_TEXT_EXTS = {".txt", ".csv", ".md", ".text"}


def classify_doc_type(text: str) -> str:
    for kind, pat in _DOC_SIGNALS:
        if pat.search(text or ""):
            return kind
    return "document"


def read_document(content: bytes, filename: str = "") -> Dict[str, Any]:
    """Read a client-uploaded document and return recognized, VERIFY-flagged fields.

    Returns a dict:
      {
        ok: bool,                     # True if any text was read
        readable: bool,               # False for image-only / undecodable files
        reason: str,                  # "" or a machine reason (needs_ocr_or_manual, ...)
        note: str,                    # human sentence for the attorney UI
        doc_type: str,                # certificate_of_insurance | waste_manifest | ...
        fields: {name: {value, flag}},# labeled fields, each flagged AI EXTRACTED — VERIFY
        text_preview: str,            # first ~600 chars, so the attorney can eyeball it
        char_count: int,
        flag: str,                    # the verify flag, echoed once for the UI
      }
    Never fabricates: an image with no text layer comes back readable=False with
    reason 'needs_ocr_or_manual' and empty fields."""
    ext = Path(filename or "").suffix.lower()
    base = {"ok": False, "readable": False, "reason": "", "note": "",
            "doc_type": "document", "fields": {}, "text_preview": "",
            "char_count": 0, "flag": EXTRACTION_FLAG}

    if ext in _IMAGE_EXTS:
        base["reason"] = "needs_ocr_or_manual"
        base["note"] = ("Image upload — no embedded text to read. The photo is "
                        "preserved as evidence; enter any needed values manually.")
        base["doc_type"] = "photo"
        return base

    if ext in _PDF_EXTS:
        got = _pdf_text(content)
    elif ext in _TEXT_EXTS or not ext:
        got = _plain_text(content)
    else:
        # Try plain-text decode as a last resort; if it's binary, flag for manual.
        got = _plain_text(content)
        if not got.get("ok"):
            got = {"ok": False, "text": "", "reason": "unsupported_type",
                   "note": f"Cannot read '{ext or 'this file'}' automatically. "
                           "Enter needed values manually."}

    if not got.get("ok"):
        base["reason"] = got.get("reason", "unreadable")
        base["note"] = got.get("note", "Could not read this file automatically.")
        return base

    text = got["text"]
    doc_type = classify_doc_type(text)
    extractor = _EXTRACTORS.get(doc_type)
    fields = extractor(text) if extractor else {}
    preview = re.sub(r"[ \t]+", " ", text.strip())[:600]

    return {
        "ok": True,
        "readable": True,
        "reason": "",
        "note": (f"Read {len(text)} characters. "
                 + (f"Recognized {len(fields)} field(s) — verify each."
                    if fields else "No structured fields recognized; see preview.")),
        "doc_type": doc_type,
        "fields": fields,
        "text_preview": preview,
        "char_count": len(text),
        "flag": EXTRACTION_FLAG,
    }
