"""Photo walk-through audit — an OSHA-style visual inspector.

The user uploads photos of a worksite, a machine, or a condition. A vision model
looks at each image the way an OSHA compliance officer would and reports the
hazards it sees. Then — and this is the bulletproof part — Origin (not the model)
maps every hazard to the exact OSHA standard from its own Compliance Knowledge
Base, attaches the standard's official title, its osha.gov URL, and the VERBATIM
CFR law text where Origin has it on file, and (when a written program exists)
the required program elements.

Design principles (carried over from the citation-guard / verifiable-accuracy
work so this tool is provably accurate and never misrepresents OSHA):

  * The vision model describes hazards in PLAIN LANGUAGE ONLY. It is explicitly
    forbidden from citing CFR/OSHA numbers. That removes the one place a model
    could hallucinate a standard — the model never emits a citation at all.
  * Origin resolves each plain-language hazard to a citation deterministically
    through the KB (verbatim_search → brain_search). A citation only appears in
    the report if it resolves to a real KB record. Every citation is then
    re-verified through osha_section() before it is shown.
  * If a hazard maps to nothing, it is reported honestly as "no specific
    standard matched" rather than being force-fit to a guess.
  * The verbatim law text shown is exact federal-CFR text from the KB, with the
    official source URL, so the user can always click through and confirm.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from . import compliance_kb as kb

# The model is told to describe hazards in plain English and return strict JSON.
# It is forbidden from writing any regulation number — Origin supplies those.
_VISION_SYSTEM = (
    "You are a veteran OSHA-style safety inspector doing a visual walk-through. "
    "You are given one or more photographs of a worksite, machine, or condition. "
    "Examine them the way a compliance officer would and identify EVERY visible "
    "safety hazard.\n\n"
    "CRITICAL RULES:\n"
    "1. Describe each hazard in PLAIN LANGUAGE only. Do NOT cite, guess, or write "
    "any OSHA standard, CFR number, section number, or regulation number of any "
    "kind. Numbers like '1926.501' or '29 CFR' must NEVER appear in your output. "
    "Another system attaches the exact standard — your job is only to SEE and "
    "DESCRIBE.\n"
    "2. Only report hazards you can actually see in the image. Do not speculate "
    "about things that are not visible. If the image is unclear or shows no "
    "hazard, say so with an empty hazards list.\n"
    "3. Be specific about what and where (e.g. 'the rotating pulley on the left "
    "of the pump has no guard', not just 'machine hazard').\n\n"
    "Return ONLY strict JSON in exactly this shape (no markdown, no commentary):\n"
    "{\n"
    '  "scene": "one short sentence describing what the photo shows",\n'
    '  "hazards": [\n'
    "    {\n"
    '      "title": "short hazard name",\n'
    '      "description": "what is visible and why it is dangerous",\n'
    '      "location": "where in the image",\n'
    '      "severity": "low" | "medium" | "high",\n'
    '      "hazard_category": "a few keywords naming the hazard type for lookup, '
    'e.g. \'unguarded rotating machine part\' or \'unprotected trench cave-in\'",\n'
    '      "recommended_action": "plain-language fix — NO regulation numbers"\n'
    "    }\n"
    "  ]\n"
    "}"
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _extract_json(text: str) -> Dict[str, Any]:
    """Pull the JSON object out of a model reply, tolerating code fences / prose."""
    if not text:
        return {}
    t = text.strip()
    # strip ```json ... ``` fences
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    # last resort: grab the outermost {...}
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}
    return {}


def _vision_call(provider, images: List[Tuple[bytes, str]]) -> str:
    """Send images + the inspector prompt to whichever provider is configured.

    `images` is a list of (raw_bytes, media_type). Returns the raw model text.
    Supports Anthropic (image source blocks) and OpenAI-compatible providers
    (image_url data URIs — covers OpenAI, Gemini, Grok, Ollama vision models).
    """
    name = getattr(provider, "name", "") or ""
    model = getattr(provider, "model", "")
    client = getattr(provider, "client", None)
    if client is None:
        raise RuntimeError("The configured AI provider has no vision client available.")

    instruction = (
        "Inspect the following photo(s) and return the JSON described in your "
        "instructions. Remember: describe hazards in plain language and never "
        "write any OSHA or CFR number."
    )

    if name == "anthropic":
        content: List[Dict[str, Any]] = []
        for data, mt in images:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mt or "image/jpeg",
                           "data": _b64(data)},
            })
        content.append({"type": "text", "text": instruction})
        resp = client.messages.create(
            model=model,
            max_tokens=2000,
            system=_VISION_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
        out = ""
        for block in resp.content:
            if getattr(block, "type", "") == "text":
                out += block.text
        return out

    # OpenAI-compatible (openai / gemini / grok / ollama)
    content = [{"type": "text", "text": instruction}]
    for data, mt in images:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mt or 'image/jpeg'};base64,{_b64(data)}"},
        })
    resp = client.chat.completions.create(
        model=model,
        max_tokens=2000,
        messages=[
            {"role": "system", "content": _VISION_SYSTEM},
            {"role": "user", "content": content},
        ],
    )
    return resp.choices[0].message.content or ""


# Generic regulatory filler words that carry no hazard-specific meaning. They are
# excluded from the fallback overlap test so two standards can't be judged
# "related" merely because both say "requirements", "general", or "protection".
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "not", "are", "was",
    "has", "have", "you", "your", "requirements", "requirement", "general",
    "protection", "protective", "standard", "standards", "safety", "hazard",
    "hazards", "osha", "cfr", "employee", "employees", "worker", "workers",
    "equipment", "system", "systems", "use", "used", "using", "all", "other",
    "must", "shall", "when", "where", "which", "than", "into", "near", "onto",
})


def _meaningful_words(text: str) -> set:
    """Lower-cased content words (>2 chars, non-stopword) as a set — used to test
    whether a hazard and a candidate standard's title genuinely overlap."""
    return {
        t for t in re.split(r"\W+", (text or "").lower())
        if len(t) > 2 and t not in _STOPWORDS
    }


def _fix_from_program(section: str) -> Tuple[Optional[str], List[str]]:
    """If the KB has a written-program record for this section, return its
    (title, required_elements) so the report can show what a compliant program
    must contain — useful remediation guidance. Grounded in corpus.jsonl."""
    for cit in ("29 CFR " + section, section):
        rec = kb.by_citation(cit)
        if rec:
            return rec.get("title"), list(rec.get("required_elements", []) or [])[:8]
    return None, []


def _resolve_citation(hazard: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Deterministically map ONE plain-language hazard to an OSHA standard using
    the KB. Returns a fully-populated citation block, or None if nothing in the
    KB matched (reported honestly rather than guessed). The model never sees or
    supplies the citation — it is resolved here from Origin's own knowledge."""
    query = " ".join([
        hazard.get("hazard_category", ""),
        hazard.get("title", ""),
        hazard.get("description", ""),
    ]).strip()
    if not query:
        return None

    section = None
    verbatim = None

    # 1) Prefer a verbatim-text hit — those are the curated walk-through hazards
    #    and give us exact law text.
    vhits = kb.verbatim_search(query, limit=1)
    if vhits:
        verbatim = vhits[0]
        section = verbatim.get("section")

    # 2) Otherwise fall back to the full brain (structural OSHA index + programs)
    #    so we can still cite a real standard even without stored verbatim text.
    #    The fallback is deliberately STRICT: brain_search scores on loose
    #    substring overlap, which will happily return a wrong-but-word-adjacent
    #    section (e.g. a respirator hazard matching "Head protection" on the
    #    shared word "protection"). Force-fitting like that misrepresents OSHA —
    #    the one thing this tool must never do — so we only accept a fallback hit
    #    whose official TITLE shares at least two meaningful words with the
    #    hazard. Anything weaker is reported honestly as no-match.
    if not section:
        q_words = _meaningful_words(query)
        for hit in kb.brain_search(query, limit=8,
                                   kinds=["osha_section", "program"]):
            cit = hit.get("citation", "")
            m = re.search(r"\b(\d{3,4}\.\d+[A-Za-z]?)", cit)
            if not m:
                continue
            title_words = _meaningful_words(hit.get("title", ""))
            if len(q_words & title_words) >= 2:
                section = m.group(1)
                break
    if not section:
        return None

    # 3) Re-verify the section resolves to a real OSHA record before showing it
    #    (belt-and-suspenders — never surface a citation the KB can't confirm).
    idx_rec = kb.osha_section(section)
    if not idx_rec and not verbatim:
        return None

    title = (verbatim or {}).get("title") or (idx_rec or {}).get("title", "")
    url = (verbatim or {}).get("url") or (idx_rec or {}).get("url", "")
    citation = (verbatim or {}).get("citation") or ("29 CFR " + section)

    prog_title, required_elements = _fix_from_program(section)

    return {
        "section": section,
        "citation": citation,
        "standard_title": (title or "").rstrip("."),
        "url": url,
        "verbatim_text": (verbatim or {}).get("text"),
        "verbatim_source": (verbatim or {}).get("source"),
        "has_verbatim": bool(verbatim),
        "program_title": prog_title,
        "required_elements": required_elements,
    }


def analyze(images: List[Tuple[bytes, str]], provider=None) -> Dict[str, Any]:
    """Run the full photo walk-through audit.

    `images`   : list of (raw_bytes, media_type) — the uploaded photos.
    `provider` : an LLM provider exposing .name/.model/.client (vision-capable).

    Returns a report dict:
      {
        scene, image_count, model,
        findings: [ { <hazard fields>, standard: {citation block} | None } ],
        unmatched: int,           # hazards with no KB standard
        disclaimer: str
      }
    Raises RuntimeError only if the vision call itself fails; hazard mapping is
    always best-effort and never fabricates.
    """
    if not images:
        return {"scene": "", "image_count": 0, "findings": [], "unmatched": 0,
                "model": getattr(provider, "model", ""),
                "disclaimer": _DISCLAIMER}

    raw = _vision_call(provider, images)
    parsed = _extract_json(raw)
    hazards = parsed.get("hazards") or []
    if not isinstance(hazards, list):
        hazards = []

    findings: List[Dict[str, Any]] = []
    unmatched = 0
    for h in hazards:
        if not isinstance(h, dict):
            continue
        # Defensive scrub: strip any stray CFR-looking token the model may have
        # slipped into its plain-language fields, so a hallucinated number can
        # never reach the user. Origin's own resolved citation is added below.
        for k in ("title", "description", "location", "recommended_action",
                  "hazard_category"):
            if isinstance(h.get(k), str):
                h[k] = _strip_reg_numbers(h[k])
        standard = _resolve_citation(h)
        if standard is None:
            unmatched += 1
        findings.append({
            "title": h.get("title", "").strip() or "Unspecified hazard",
            "description": h.get("description", "").strip(),
            "location": h.get("location", "").strip(),
            "severity": (h.get("severity") or "medium").lower(),
            "recommended_action": h.get("recommended_action", "").strip(),
            "standard": standard,
        })

    return {
        "scene": (parsed.get("scene") or "").strip(),
        "image_count": len(images),
        "model": getattr(provider, "model", ""),
        "findings": findings,
        "unmatched": unmatched,
        "disclaimer": _DISCLAIMER,
    }


_REG_TOKEN_RE = re.compile(
    r"\b(?:29\s*C\.?F\.?R\.?\.?\s*)?§?\s*\d{3,4}\.\d+[A-Za-z]?(?:\([0-9A-Za-z]+\))*\b"
    r"|\b\d{3,4}\s+Subpart\s+[A-Z]+\b",
    re.I,
)


def _strip_reg_numbers(text: str) -> str:
    """Remove any regulation-number-shaped token from model-written prose. Origin
    supplies the authoritative citation separately, so the model's plain-language
    text should carry none — this guarantees it even if the model disobeys."""
    if not text:
        return text
    cleaned = _REG_TOKEN_RE.sub("the applicable OSHA standard", text)
    # collapse doubled spaces left behind
    return re.sub(r"\s{2,}", " ", cleaned).strip()


_DISCLAIMER = (
    "This automated walk-through flags visible conditions and maps them to OSHA "
    "standards from Origin's knowledge base for your review. It is a screening "
    "aid, not an official OSHA inspection or a substitute for a qualified safety "
    "professional. Verify each standard at the linked osha.gov source before "
    "acting."
)
