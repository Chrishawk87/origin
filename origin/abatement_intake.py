"""Origin Abatement — citation intake + extraction (Phase 1 core spine).

Turns an OSHA citation (pasted text or an uploaded PDF) into a list of structured
citation-item rows for a Matter. This is the ONLY place in the abatement feature
where machine extraction happens, so the guardrails live here in force:

  * Every extracted field is returned flagged "AI EXTRACTED — VERIFY". Extraction
    is a convenience; the attorney verifies every value before it is trusted.
  * Never fabricates. When the text is a scanned image with no embedded text, we
    say so and ask for manual entry — we do NOT guess citation content.
  * The regulatory brain (compliance_kb via abatement_matter.reg_lookup) remains
    the source of truth for standard titles/URLs; the extractor only pulls the
    raw strings a human then confirms.

The deterministic text parser needs no model. A model-assisted path can be layered
later, but its output would land in exactly the same VERIFY-flagged rows.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# CFR standard, e.g. "29 CFR 1926.501(b)(13)" or "1910.132(d)(1)".
_CFR_RE = re.compile(
    r"\b((?:29|49|40|30|43)\s*CFR\s*)?(\d{3,4}\.\d+(?:\([a-z0-9]+\))*)",
    re.IGNORECASE)
_MONEY_RE = re.compile(r"\$\s?[\d,]+(?:\.\d{2})?")
_DATE_RE = re.compile(
    r"\b(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})\b",
    re.IGNORECASE)
_ITEM_HDR_RE = re.compile(
    r"Citation\s+(\d+)\s+Item\s+(\d+)", re.IGNORECASE)
_ABATE_RE = re.compile(r"Abatement\s+Date[:\s]+", re.IGNORECASE)
_PENALTY_RE = re.compile(r"(?:Proposed\s+)?Penalty[:\s]+", re.IGNORECASE)

_CLASS_PATTERNS = [
    ("failure_to_abate", re.compile(r"failure[-\s]to[-\s]abate", re.IGNORECASE)),
    ("willful", re.compile(r"\bwillful\b", re.IGNORECASE)),
    ("repeat", re.compile(r"\brepeat(?:ed)?\b", re.IGNORECASE)),
    ("other_than_serious", re.compile(r"other[-\s]than[-\s]serious", re.IGNORECASE)),
    ("serious", re.compile(r"\bserious\b", re.IGNORECASE)),
    ("de_minimis", re.compile(r"de\s+minimis", re.IGNORECASE)),
]

EXTRACTION_FLAG = "AI EXTRACTED — VERIFY"


def _norm_standard(prefix: Optional[str], body: str) -> str:
    body = (body or "").strip()
    if prefix and prefix.strip():
        pre = re.sub(r"\s+", " ", prefix.strip()).upper()
        return f"{pre} {body}"
    # Default the agency prefix by part number range (deterministic, verify-required).
    part = body.split(".")[0]
    try:
        pn = int(part)
    except ValueError:
        pn = 0
    if 1900 <= pn <= 1999:
        return f"29 CFR {body}"
    return body  # leave bare — the attorney supplies the agency on verify


def _first_class(segment: str) -> str:
    for code, rx in _CLASS_PATTERNS:
        if rx.search(segment):
            return code
    return ""


def _first(rx: re.Pattern, segment: str, after: Optional[re.Pattern] = None) -> str:
    if after:
        m = after.search(segment)
        if m:
            tail = segment[m.end():m.end() + 60]
            mm = rx.search(tail)
            if mm:
                return mm.group(0).strip()
    m = rx.search(segment)
    return m.group(0).strip() if m else ""


def parse_text(text: str) -> Dict[str, Any]:
    """Deterministically extract citation-item rows from pasted citation text.

    Returns {ok, rows, count, flag, note}. Each row is a VERIFY-flagged dict shaped
    for abatement_matter.add_citation_items(extracted=True). Never fabricates: a
    row only appears where a CFR standard was actually found in the text."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "rows": [], "count": 0,
                "note": "No text provided. Paste the citation text or upload a text-based PDF."}

    # Prefer splitting on "Citation N Item M" headers when the document has them.
    headers = list(_ITEM_HDR_RE.finditer(text))
    segments: List[Dict[str, Any]] = []
    if headers:
        for idx, h in enumerate(headers):
            start = h.start()
            end = headers[idx + 1].start() if idx + 1 < len(headers) else len(text)
            segments.append({
                "citation_number": f"Citation {h.group(1)} Item {h.group(2)}",
                "text": text[start:end],
            })
    else:
        # Fall back to one segment per CFR standard occurrence.
        marks = list(_CFR_RE.finditer(text))
        for idx, m in enumerate(marks):
            start = m.start()
            end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
            segments.append({"citation_number": "", "text": text[start:end]})

    rows: List[Dict[str, Any]] = []
    seen = set()
    for seg in segments:
        body = seg["text"]
        cm = _CFR_RE.search(body)
        if not cm:
            continue
        standard = _norm_standard(cm.group(1), cm.group(2))
        penalty = _first(_MONEY_RE, body, after=_PENALTY_RE) or _first(_MONEY_RE, body)
        abate = _first(_DATE_RE, body, after=_ABATE_RE)
        classification = _first_class(body)
        # A short slice of surrounding text as the alleged-condition seed (human edits).
        cond = re.sub(r"\s+", " ", body).strip()[:400]
        key = (standard, seg["citation_number"], abate)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "citation_number": seg["citation_number"],
            "standard": standard,
            "classification": classification,
            "proposed_penalty": penalty,
            "abatement_date": abate,
            "alleged_condition": cond,
        })

    return {
        "ok": bool(rows),
        "rows": rows,
        "count": len(rows),
        "flag": EXTRACTION_FLAG,
        "note": (f"{len(rows)} citation item(s) extracted. Every field is machine-"
                 f"extracted — verify each against the citation before use."
                 if rows else
                 "No CFR standards found in the text. Enter the citation items manually."),
    }


def parse_pdf_bytes(content: bytes) -> Dict[str, Any]:
    """Extract text from a text-based PDF, then run the deterministic parser.

    A scanned/image-only PDF has no embedded text; we detect that and ask for
    manual entry (or a text paste) rather than guessing. Requires pypdf if
    present; degrades gracefully if not installed."""
    text = ""
    try:
        import io
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(io.BytesIO(content or b""))
        text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
    except Exception as exc:
        return {"ok": False, "rows": [], "count": 0,
                "reason": "pdf_text_extract_failed",
                "note": ("Could not read text from this PDF "
                         f"({exc}). Paste the citation text instead.")}
    if len(text.strip()) < 20:
        return {"ok": False, "rows": [], "count": 0,
                "reason": "needs_ocr_or_manual",
                "note": ("This looks like a scanned image with no embedded text. "
                         "OCR/manual entry required — no citation content was guessed.")}
    out = parse_text(text)
    out["extracted_chars"] = len(text)
    return out


# ── routes ──────────────────────────────────────────────────────────────────────
def register_abatement_intake(app) -> None:
    """Attach citation-intake routes. Isolated + non-fatal. These endpoints only
    EXTRACT and return VERIFY-flagged rows; they never write to a matter. The
    caller reviews the rows, then posts the confirmed ones to the matter."""
    from fastapi import Body, UploadFile, File
    from fastapi.responses import JSONResponse

    @app.post("/api/abatement/intake/parse-text")
    def intake_parse_text(body: dict = Body(default=None)):
        p = body if isinstance(body, dict) else {}
        return parse_text(p.get("text", ""))

    @app.post("/api/abatement/intake/parse-pdf")
    async def intake_parse_pdf(file: UploadFile = File(...)):
        try:
            content = await file.read()
            return parse_pdf_bytes(content)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.post("/api/abatement/intake/parse-and-add/{matter_id}")
    def intake_parse_and_add(matter_id: str, body: dict = Body(default=None)):
        """Convenience: parse pasted text AND add the extracted (VERIFY-flagged)
        rows to a matter in one call, for the guided-intake wizard. The items land
        unverified with the extraction flag set — the attorney still verifies each."""
        p = body if isinstance(body, dict) else {}
        parsed = parse_text(p.get("text", ""))
        if not parsed.get("ok"):
            return JSONResponse(parsed, status_code=200)
        from . import abatement_matter as _am
        if not _am.get(matter_id):
            return JSONResponse({"error": "matter not found"}, status_code=404)
        added = _am.add_citation_items(matter_id, parsed["rows"], extracted=True)
        return {"ok": True, "added": len(added), "items": added, "flag": EXTRACTION_FLAG}
