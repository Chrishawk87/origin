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


# ── deterministic hazard-category → OSHA-section battery (Stage 4) ────────────
# The vision model names a hazard in plain language; this table gives the most
# common real-site hazards a DIRECT, deterministic anchor to the exact OSHA
# section — the high-confidence resolution path that widens the tool well past
# its old handful of curated verbatim hazards. It NEVER fabricates: a mapped
# section is accepted only after it re-verifies against the KB (osha_section or
# stored verbatim text), exactly like every other path. Ordered most-specific
# first because the first keyword match wins — put narrow hazards (silica,
# trench, LOTO) ahead of broad ones (machine guarding, generic PPE).
_HAZARD_MAP_RAW: List[Tuple[str, str]] = [
    # — construction (29 CFR 1926) —
    (r"trench|excavat|cave[- ]?in|shoring|trench box|sloping|benching", "1926.652"),
    (r"silica|respirable|crystalline|concrete dust|masonry dust|cutting concrete", "1926.1153"),
    (r"scaffold|scaffolding|staging platform|mud ?sill", "1926.451"),
    (r"aerial lift|boom lift|scissor lift|man ?lift|bucket truck", "1926.453"),
    (r"steel erection|structural steel|decking|column|beam bolt", "1926.760"),
    (r"crane|hoist|rigging|load line|outrigger", "1926.1400"),
    (r"leading edge|unprotected edge|roof edge|open[- ]?side|floor hole|"
     r"fall from|fall protection|no guardrail|without guardrail|fall arrest|"
     r"tie[- ]?off|unanchored harness|near the edge", "1926.501"),
    (r"guardrail system|guard ?rail|midrail|top ?rail|toeboard", "1926.502"),
    (r"fall protection train", "1926.503"),
    (r"step ?ladder|extension ladder|\bladder\b|ladder rung", "1926.1053"),
    (r"stair|stairway|stairwell|temporary stair", "1926.1052"),
    (r"gfci|ground fault|temporary power|extension cord|power cord|damaged cord", "1926.404"),
    (r"hard ?hat|head protection|no helmet", "1926.100"),
    (r"debris|housekeeping|clutter|scattered material|material pile|trip hazard on site", "1926.25"),
    (r"rebar|impalement|protruding steel|concrete form|masonry wall|block wall", "1926.701"),
    # — general industry (29 CFR 1910) —
    (r"lockout|tagout|\bloto\b|energy control|de[- ]?energ|stored energy", "1910.147"),
    (r"abrasive wheel|bench grinder|grinding wheel|pedestal grinder", "1910.215"),
    (r"power transmission|flywheel|shafting|line shaft|coupling|v[- ]?belt", "1910.219"),
    (r"machine guard|unguarded|rotating|pulley|pinch point|nip point|"
     r"exposed gear|point of operation|missing guard|belt drive", "1910.212"),
    (r"forklift|powered industrial truck|\bpit\b|pallet jack|order picker|lift truck", "1910.178"),
    (r"respirator|respiratory protection|dust mask|\bscba\b|air[- ]?purifying", "1910.134"),
    (r"noise|hearing|decibel|ear ?plug|ear ?muff|hearing protection", "1910.95"),
    (r"hazard communication|hazcom|safety data sheet|\bsds\b|\bmsds\b|"
     r"unlabel|chemical label|secondary container|unmarked container", "1910.1200"),
    (r"confined space|permit space|manhole|tank entry|vault|silo entry", "1910.146"),
    (r"bloodborne|biohazard|sharps|needle|exposure control", "1910.1030"),
    (r"compressed gas|gas cylinder|oxygen cylinder|acetylene|uncapped cylinder|unsecured cylinder", "1910.101"),
    (r"flammable liquid|combustible liquid|fuel storage|solvent storage|flammable storage", "1910.106"),
    (r"fire extinguisher|extinguisher|blocked extinguisher", "1910.157"),
    (r"blocked exit|exit route|egress|exit sign|obstructed exit", "1910.37"),
    (r"emergency action plan|evacuation plan|emergency plan", "1910.38"),
    (r"exposed wire|live part|energized part|open panel|electrical panel|"
     r"missing knockout|junction box|open breaker", "1910.303"),
    (r"electrical safe work|working on live|arc flash|hot work on circuit", "1910.333"),
    (r"slip|trip|wet floor|uneven surface|walking[- ]?working surface|floor opening|standing water", "1910.22"),
    (r"welding|cutting|hot work|torch|brazing|weld spark", "1910.252"),
    (r"eye protection|face shield|safety glasses|goggles|no eye pro", "1910.133"),
    (r"foot protection|steel toe|safety boots|open[- ]?toe", "1910.136"),
    (r"hand protection|cut[- ]?resistant|\bglove", "1910.138"),
    (r"personal protective|no ppe|missing ppe|not wearing.*protect", "1910.132"),
]
_HAZARD_MAP: List[Tuple[Any, str]] = [
    (re.compile(p, re.I), s) for p, s in _HAZARD_MAP_RAW
]


# Confidence by HOW the citation was resolved. A deterministic table hit or a
# curated verbatim hit is trustworthy enough to auto-open a corrective action;
# a loose brain-search overlap is a CANDIDATE only, and is routed to human
# review rather than acted on automatically. Nothing below the review line ever
# auto-generates a CAPA — honest confidence in, honest routing out.
_CONF_MAP = 0.9          # explicit hazard-category table, KB-verified
_CONF_VERBATIM = 0.78    # curated verbatim-text section match
_CONF_BRAIN = 0.5        # loose brain-search title overlap (candidate only)
_CONF_NONE = 0.0
CONFIDENCE_REVIEW_BELOW = 0.6    # confidence < this ⇒ route to review, never auto-CAPA


def _band(conf: float) -> str:
    """Human-readable confidence band for the UI + routing."""
    if conf >= 0.75:
        return "high"
    if conf >= CONFIDENCE_REVIEW_BELOW:
        return "medium"
    if conf > 0:
        return "low"
    return "none"


def _map_section(query: str) -> Optional[str]:
    """First hazard-category keyword match → its OSHA section, else None."""
    for rx, section in _HAZARD_MAP:
        if rx.search(query):
            return section
    return None


def _resolve_citation(hazard: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Deterministically map ONE plain-language hazard to an OSHA standard using
    the KB. Returns a fully-populated citation block (now carrying how it matched
    and how confident that match is), or None if nothing in the KB matched
    (reported honestly rather than guessed). The model never sees or supplies the
    citation — it is resolved here from Origin's own knowledge."""
    query = " ".join([
        hazard.get("hazard_category", ""),
        hazard.get("title", ""),
        hazard.get("description", ""),
    ]).strip()
    if not query:
        return None

    section = None
    verbatim = None
    method = ""
    confidence = _CONF_NONE

    # 0) Deterministic hazard-category table — the high-confidence anchor and the
    #    thing that widens the battery. A mapped section is trusted only after it
    #    re-verifies against the KB (never fabricate), and we still attach the
    #    exact verbatim law text when the KB has it for that section.
    mapped = _map_section(query)
    if mapped:
        vhit = kb.verbatim_search(query, limit=1)
        if vhit and vhit[0].get("section") == mapped:
            verbatim = vhit[0]
        if kb.osha_section(mapped) or verbatim:
            section = mapped
            method = "hazard_map"
            confidence = _CONF_MAP

    # 1) Prefer a verbatim-text hit — those are the curated walk-through hazards
    #    and give us exact law text.
    if not section:
        vhits = kb.verbatim_search(query, limit=1)
        if vhits:
            verbatim = vhits[0]
            section = verbatim.get("section")
            method = "verbatim"
            confidence = _CONF_VERBATIM

    # 2) Otherwise fall back to the full brain (structural OSHA index + programs)
    #    so we can still cite a real standard even without stored verbatim text.
    #    The fallback is deliberately STRICT: brain_search scores on loose
    #    substring overlap, which will happily return a wrong-but-word-adjacent
    #    section (e.g. a respirator hazard matching "Head protection" on the
    #    shared word "protection"). Force-fitting like that misrepresents OSHA —
    #    the one thing this tool must never do — so we only accept a fallback hit
    #    whose official TITLE shares at least two meaningful words with the
    #    hazard, and we mark it a low-confidence CANDIDATE so it is routed to
    #    human review rather than auto-acted-on.
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
                method = "brain"
                confidence = _CONF_BRAIN
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
        "match_method": method,
        "confidence": round(confidence, 2),
        "confidence_band": _band(confidence),
    }


def _vision_with_fallback(images, providers) -> Tuple[str, str]:
    """Try each vision provider in order; return (raw_text, model_used) from the
    first that succeeds. Collect every failure so that if ALL of them fail we can
    report why.

    This is the resilience layer: the primary brain might be a Gemini model
    Google just retired, or a provider whose key expired, or one that timed out.
    Rather than hard-failing the tool for a customer, we fall through to the next
    vision-capable brain (e.g. Claude, then GPT). Only if none work do we raise.
    """
    errors: List[str] = []
    for p in providers:
        if p is None:
            continue
        label = f"{getattr(p, 'name', '?')}/{getattr(p, 'model', '?')}"
        try:
            raw = _vision_call(p, images)
            if raw and raw.strip():
                return raw, getattr(p, "model", "")
            errors.append(f"{label}: empty response")
        except Exception as e:  # dead model / bad key / timeout — try the next one
            errors.append(f"{label}: {e}")
    raise RuntimeError(
        "No available AI vision brain could analyze the photo. "
        "Tried: " + "; ".join(errors) if errors
        else "No AI vision brain is configured."
    )


def analyze(images: List[Tuple[bytes, str]], provider=None, fallbacks=None) -> Dict[str, Any]:
    """Run the full photo walk-through audit.

    `images`    : list of (raw_bytes, media_type) — the uploaded photos.
    `provider`  : the preferred vision-capable LLM provider (.name/.model/.client).
    `fallbacks` : optional ordered list of additional providers to try if the
                  preferred one fails, so a retired model or dead key never hard-
                  fails the tool for a customer.

    Returns a report dict:
      {
        scene, image_count, model,
        findings: [ { <hazard fields>, standard: {citation block} | None } ],
        unmatched: int,           # hazards with no KB standard
        disclaimer: str
      }
    Raises RuntimeError only if EVERY vision provider fails; hazard mapping is
    always best-effort and never fabricates.
    """
    if not images:
        return {"scene": "", "image_count": 0, "findings": [], "unmatched": 0,
                "needs_review": 0, "model": getattr(provider, "model", ""),
                "disclaimer": _DISCLAIMER}

    chain = [provider] + list(fallbacks or [])
    raw, used_model = _vision_with_fallback(images, chain)
    parsed = _extract_json(raw)
    hazards = parsed.get("hazards") or []
    if not isinstance(hazards, list):
        hazards = []

    findings: List[Dict[str, Any]] = []
    unmatched = 0
    needs_review = 0
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
            confidence = _CONF_NONE
            method = "none"
        else:
            confidence = float(standard.get("confidence", _CONF_VERBATIM))
            method = standard.get("match_method", "")
        # Honest routing: a sourced, confident hazard is actionable (→ CAPA
        # downstream); an unmatched or low-confidence one is a candidate that a
        # human reviews before it becomes a corrective action. No silent guesses.
        route = "review" if confidence < CONFIDENCE_REVIEW_BELOW else "capa"
        if route == "review":
            needs_review += 1
        findings.append({
            "title": h.get("title", "").strip() or "Unspecified hazard",
            "description": h.get("description", "").strip(),
            "location": h.get("location", "").strip(),
            "severity": (h.get("severity") or "medium").lower(),
            "recommended_action": h.get("recommended_action", "").strip(),
            "standard": standard,
            "confidence": round(confidence, 2),
            "confidence_band": _band(confidence),
            "match_method": method,
            "route": route,
            "needs_review": route == "review",
        })

    return {
        "scene": (parsed.get("scene") or "").strip(),
        "image_count": len(images),
        "model": used_model or getattr(provider, "model", ""),
        "findings": findings,
        "unmatched": unmatched,
        "needs_review": needs_review,
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
