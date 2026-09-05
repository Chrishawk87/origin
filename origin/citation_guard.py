"""Citation guard — verifiable-accuracy layer for AI chat answers.

Every regulation number the AI states in a chat answer is checked against the
Compliance Knowledge Base before the user ever sees it. A citation that resolves
to a real KB record (OSHA structural index, the written-program corpus, the FMCSA
index, or the OSHA 2254 training package) is kept and gets its official title and
source URL appended. A citation the model *asserts as a regulation* (i.e. it wrote
"29 CFR ...", "49 CFR ...", "§ ...", or "<part> Subpart X") but that does NOT exist
in the KB is refused: the number is struck from the text and replaced with a plain
"unverified" marker, and a short warning is prepended. Bare decimals with no
regulatory context that don't match anything are ignored (not every "3.14" is a
citation), so ordinary chat is never disturbed.

Design goals:
  * Deterministic. No LLM involved — this is the backstop that holds even if the
    model ignores its instructions.
  * Cheap and scoped. If the answer contains no citation-shaped tokens the guard
    returns immediately and unchanged, so non-compliance chat pays nothing.
  * Never fabricates and never "auto-corrects" — an unconfirmed standard is
    removed, not quietly swapped for a guess.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from . import compliance_kb as kb

# Matches a CFR-style citation inside free text. Named groups:
#   cfr      -> the "29 CFR" / "49 C.F.R." prefix, if the model wrote one
#   sec      -> a "§" section sign, if present
#   subpart  -> a subpart form like "1926 Subpart P" / "382 Subpart G"
#   section  -> a section form like "1910.147", "1926.501(b)(1)", "390.19"
# A 3- or 4-digit part is required before the dot so plain decimals ("3.14",
# "12.5") never match. 3-digit parts (FMCSA) are only *treated as a claim* when a
# "49 CFR"/"§" context is present or they resolve in the KB (see _classify).
_CFR_RE = re.compile(
    r"""(?ix)
    \b
    (?P<cfr>(?:29|49)\s*C\.?\s*F\.?\s*R\.?\.?\s*)?
    (?P<sec>\u00a7\s*)?
    (?:
        (?P<subpart>\d{3,4}\s+Subpart\s+[A-Z]+)
      | (?P<section>\d{3,4}\.\d+[A-Za-z]?(?:\([0-9A-Za-z]+\))*)
    )
    """,
)

_UNVERIFIED_MARK = "[standard unverified \u2014 not found in Origin\u2019s knowledge base]"
_WARN = (
    "> \u26a0\ufe0f Origin removed one or more regulation numbers below that it could "
    "not confirm against its knowledge base. Treat anything marked \u201cunverified\u201d "
    "as unconfirmed and check it directly with OSHA before relying on it.\n\n"
)


def _verify_one(raw: str) -> Tuple[Optional[str], Optional[dict]]:
    """Resolve one candidate citation string against the KB.

    Returns (kind, record) where kind is 'osha' | 'program' | 'fmcsa' | 'training'
    or (None, None) if the KB has no such standard. The KB resolvers never
    fabricate — None means it is genuinely not a known citation.
    """
    rec = kb.osha_section(raw)
    if rec:
        return "osha", rec
    rec = kb.by_citation(_canon(raw))
    if rec:
        return "program", rec
    rec = kb.fmcsa_section(raw)
    if rec:
        return "fmcsa", rec
    rec = kb.training_requirement(raw)
    if rec:
        return "training", rec
    return None, None


def _canon(raw: str) -> str:
    """Normalize a matched token to the corpus 'citation' form '29 CFR 1910.147'."""
    m = re.search(r"\b(\d{3,4}\.\d+[A-Za-z]?)", raw)
    if not m:
        return raw.strip()
    return "29 CFR " + m.group(1)


def _display(kind: str, rec: dict) -> Dict[str, str]:
    """Pull a uniform {citation, title, url} out of whichever KB record matched."""
    citation = rec.get("citation") or rec.get("section") or ""
    title = rec.get("title") or rec.get("standard_title") or ""
    url = rec.get("url") or rec.get("source") or ""
    return {"citation": citation, "title": title, "url": url}


def verify(text: str) -> Dict[str, object]:
    """Inspect answer text and classify every citation-shaped token it contains.

    Returns {verified: [{citation,title,url}...], unverified: [str...], matches:[...]}
    without modifying the text. `matches` carries (start, end, status) spans so
    guard_answer can rewrite in place.
    """
    verified: List[Dict[str, str]] = []
    unverified: List[str] = []
    seen_ok: set = set()
    seen_bad: set = set()
    matches: List[Tuple[int, int, str]] = []

    for m in _CFR_RE.finditer(text or ""):
        raw = m.group(0).strip()
        is_subpart = bool(m.group("subpart"))
        has_context = bool(m.group("cfr") or m.group("sec")) or is_subpart

        kind, rec = _verify_one(raw)
        if rec is not None:
            info = _display(kind, rec)
            key = (info["citation"] or raw).lower()
            if key not in seen_ok:
                seen_ok.add(key)
                verified.append(info)
            matches.append((m.start(), m.end(), "ok"))
        elif has_context:
            # The model presented this as a real regulation but it isn't one.
            if raw.lower() not in seen_bad:
                seen_bad.add(raw.lower())
                unverified.append(raw)
            matches.append((m.start(), m.end(), "bad"))
        else:
            # A bare decimal with no regulatory framing that matched nothing —
            # almost certainly not a citation. Leave it untouched.
            matches.append((m.start(), m.end(), "ignore"))

    return {"verified": verified, "unverified": unverified, "matches": matches}


def guard_answer(text: str) -> Tuple[str, Dict[str, object]]:
    """Return (guarded_text, report). Refuses unverifiable citations and appends a
    verified-standards source block. If no citations are present, returns the text
    unchanged with report {'changed': False}."""
    if not text or not _CFR_RE.search(text):
        return text, {"changed": False, "verified": [], "unverified": []}

    res = verify(text)
    verified = res["verified"]           # type: ignore[assignment]
    unverified = res["unverified"]       # type: ignore[assignment]
    matches = res["matches"]             # type: ignore[assignment]

    out = text
    # Strike unverifiable citations in place, right-to-left so spans stay valid.
    for start, end, status in sorted(matches, key=lambda t: t[0], reverse=True):
        if status == "bad":
            out = out[:start] + _UNVERIFIED_MARK + out[end:]

    if unverified:
        out = _WARN + out

    if verified:
        lines = ["", "", "---", "**Verified standards** (confirmed in Origin\u2019s knowledge base):"]
        for v in verified:
            bit = "- " + (v["citation"] or "").strip()
            if v["title"]:
                bit += " \u2014 " + v["title"].strip()
            if v["url"]:
                bit += "  \n  " + v["url"].strip()
            lines.append(bit)
        out = out + "\n".join(lines)

    changed = bool(unverified) or bool(verified)
    return out, {"changed": changed, "verified": verified, "unverified": unverified}
