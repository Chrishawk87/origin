"""Citations resolver — the verifiable-accuracy layer (Phase A "Bulletproof").

Given a regulation string that already lives somewhere in Origin's output
(a gap's `citation`, a program's `citation`, a portal required-item's standard,
a dashboard recommendation), this module resolves it against the Compliance
Knowledge Base and returns a single uniform record the UI can render:

    {ok, citation, title, url, part, verbatim, kind}

`verbatim` is the exact CFR body text when the KB has it stored, so the UI can
offer a click-to-expand "read the actual standard" block. When no verbatim body
exists we still return the official title + source URL so every citation is at
least traceable.

House rules (identical to every other Origin module):
  * Deterministic. No LLM. Pure KB lookups.
  * Offline. Only touches the already-loaded compliance_kb.
  * Never fabricates. If nothing in the KB resolves the string, ok=False and no
    title/text is invented.
  * Isolated / non-fatal. Every KB call is wrapped; a resolver bug can never
    raise into the caller (the live app must never crash over a citation).
"""

from __future__ import annotations

from typing import Dict, List, Optional

from . import compliance_kb as kb


def _safe(fn, *args):
    """Call a KB resolver, swallowing any error so citations never crash a page."""
    try:
        return fn(*args)
    except Exception:
        return None


def _empty(reg: str) -> Dict[str, object]:
    return {
        "ok": False,
        "citation": (reg or "").strip(),
        "title": "",
        "url": "",
        "part": "",
        "verbatim": "",
        "kind": "",
    }


def cite(reg: str) -> Dict[str, object]:
    """Resolve one regulation string to a uniform citation record.

    Fallback chain (richest first):
      1. verbatim_text     -> kind='verbatim', full CFR body
      2. training_requirement -> kind='training', verbatim training text
      3. osha_section      -> kind='osha', title + URL (no body)
      4. fmcsa_section     -> kind='fmcsa', title + URL (no body)
      5. by_citation       -> kind='program', corpus title (no body)
      6. nothing           -> ok=False (never fabricated)
    """
    reg = (reg or "").strip()
    out = _empty(reg)
    if not reg:
        return out

    # 1. Verbatim CFR body — the best case, powers the expand-to-read block.
    rec = _safe(kb.verbatim_text, reg)
    if rec:
        out.update(
            ok=True,
            kind="verbatim",
            citation=rec.get("citation") or reg,
            title=rec.get("title") or "",
            url=rec.get("url") or rec.get("source") or "",
            part=rec.get("part") or "",
            verbatim=rec.get("text") or "",
        )
        return out

    # 2. OSHA 2254 training requirement — also carries verbatim body text.
    rec = _safe(kb.training_requirement, reg)
    if rec:
        out.update(
            ok=True,
            kind="training",
            citation=rec.get("citation") or rec.get("section") or reg,
            title=rec.get("standard_title") or rec.get("title") or "",
            url=rec.get("source") or rec.get("url") or "",
            part=rec.get("part") or "",
            verbatim=rec.get("training_requirement") or "",
        )
        return out

    # 3. OSHA structural index — official title + source URL, no body.
    rec = _safe(kb.osha_section, reg)
    if rec:
        out.update(
            ok=True,
            kind="osha",
            citation=rec.get("citation") or rec.get("section") or reg,
            title=rec.get("title") or "",
            url=rec.get("url") or "",
            part=rec.get("part") or "",
            verbatim="",
        )
        return out

    # 4. FMCSA index — title + URL, no body.
    rec = _safe(kb.fmcsa_section, reg)
    if rec:
        out.update(
            ok=True,
            kind="fmcsa",
            citation=rec.get("citation") or rec.get("section") or reg,
            title=rec.get("title") or "",
            url=rec.get("url") or "",
            part=rec.get("part") or "",
            verbatim="",
        )
        return out

    # 5. Written-program corpus — last-resort title match, no body.
    rec = _safe(kb.by_citation, reg)
    if rec:
        out.update(
            ok=True,
            kind="program",
            citation=rec.get("citation") or reg,
            title=rec.get("title") or "",
            url=rec.get("url") or rec.get("source") or "",
            part=rec.get("part") or "",
            verbatim="",
        )
        return out

    # 6. EPA environmental layer — resolves rule NAMES and 40 CFR references that
    #    aren't verbatim CFR sections (e.g. "RCRA 90/180-Day Rule", "SPCC",
    #    "40 CFR 112"). Only consulted when the string looks environmental, so a
    #    stray token can't false-match. Grounded in the EPA reference records.
    if _looks_epa(reg):
        hits = _safe(kb.epa_search, reg) or []
        if hits:
            rec = hits[0]
            out.update(
                ok=True,
                kind="epa",
                citation=rec.get("citation") or reg,
                title=rec.get("title") or "",
                url=rec.get("url") or rec.get("source") or "",
                part=rec.get("part") or "",
                verbatim=rec.get("summary") or "",
            )
            return out

    # 7. Nothing in the KB — never fabricate.
    return out


_EPA_SIGNALS = (
    "epa", "rcra", "cwa", "caa", "cercla", "spcc", "npdes", "swppp",
    "stormwater", "epcra", "sdwa", "uic", " opa", "clean air", "clean water",
    "hazardous waste", "40 cfr", "tier ii", "tier 2", "ldar", "oooo",
    "reportable quantity", "generator", "underground injection", "methane",
)


def _looks_epa(reg: str) -> bool:
    """True when a citation string is environmental enough to consult the EPA
    layer. Keeps the EPA fallback from false-matching OSHA/DOT strings."""
    s = (reg or "").lower()
    return any(sig in s for sig in _EPA_SIGNALS)


def attach(items, *, key: str = "citation", into: str = "citation_record"):
    """Enrich a list of dicts in place with a resolved citation record.

    For every dict that carries a truthy `[key]`, resolve it and, only if it
    resolves (ok=True), set `item[into]` to the citation record. Items whose
    citation can't be verified are left untouched (never-fabricate). Returns the
    same list for chaining. Non-fatal: any per-item error is skipped.
    """
    if not items:
        return items
    for item in items:
        try:
            if not isinstance(item, dict):
                continue
            reg = item.get(key)
            if not reg:
                continue
            rec = cite(reg if isinstance(reg, str) else str(reg))
            if rec.get("ok"):
                item[into] = rec
        except Exception:
            continue
    return items
