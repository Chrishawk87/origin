"""Origin Compliance Knowledge Base — retrieval + OSHA validation gate.

Loads the codified compliance corpus (OSHA 1910/1926, DOT/FMCSA, oil & gas,
prequalification agencies, EPA, safety metrics, insurance/COI) that ships in
``compliance_kb_data/Compliance Knowledge Base/`` and exposes:

* retrieval helpers (``get``, ``by_citation``, ``search``, ``templates``) so the
  agent can ground drafts on the exact required elements + citations, and
* ``validate_document()`` — the checklist gate that every compliance document
  must pass before it is sent to a client. It resolves the standards a document
  invokes, checks the draft against each standard's ``required_elements``, and
  surfaces the reviewer ``failure_points`` that must be affirmed.

The corpus is static data; regenerate it with ``kb_engine.py`` when standards
change. Nothing here has external dependencies beyond the stdlib.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

# Data ships inside the package so it deploys with Origin.
KB_DIR = Path(__file__).parent / "compliance_kb_data" / "Compliance Knowledge Base"

_STOP = {
    "the", "and", "for", "with", "that", "this", "each", "from", "into", "your",
    "are", "not", "all", "any", "per", "its", "over", "under", "must", "shall",
    "written", "program", "plan", "procedure", "procedures", "employee",
    "employees", "used", "using", "least", "every", "incl", "etc", "core",
}


# ── corpus loading ──────────────────────────────────────────────────────────
@functools.lru_cache(maxsize=1)
def _records() -> Dict[str, dict]:
    recs: Dict[str, dict] = {}
    path = KB_DIR / "corpus.jsonl"
    if not path.exists():
        return recs
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            r = json.loads(line)
            recs[r["id"]] = r
    return recs


def all_records() -> List[dict]:
    return list(_records().values())


def get(entry_id: str) -> Optional[dict]:
    return _records().get(entry_id)


def by_citation(citation: str) -> Optional[dict]:
    citation = (citation or "").strip()
    return next((r for r in _records().values() if r["citation"] == citation), None)


# ── OSHA 2254 verbatim training-requirement corpus ───────────────────────────
# The full "Training Requirements in OSHA Standards" (OSHA 2254-09R 2015) package,
# 172 sections of exact regulatory training language keyed by CFR citation. The
# 153-record corpus above cross-links the relevant ones into a `training_verbatim`
# field; this loader exposes ALL of them so the agent can quote the precise wording
# for any section, not just the ones that map to a written program.
@functools.lru_cache(maxsize=1)
def _training_reqs() -> Dict[str, dict]:
    idx: Dict[str, dict] = {}
    path = KB_DIR / "training_requirements_osha2254.jsonl"
    if not path.exists():
        return idx
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        # key by bare section ("1910.147") and full citation ("29 CFR 1910.147")
        idx[r["section"]] = r
        idx[r["citation"]] = r
    return idx


def training_requirement(citation: str) -> Optional[dict]:
    """Return the verbatim OSHA 2254 training-requirement record for a citation.

    Accepts a bare section ('1910.147'), a full citation ('29 CFR 1910.147'), or
    any string containing one. Returns None if the section isn't in the package
    (e.g. post-2015 standards like silica 1910.1053, or standards with no training
    requirement). Never fabricates — absence means the package has no entry.
    """
    q = (citation or "").strip()
    if not q:
        return None
    idx = _training_reqs()
    if q in idx:
        return idx[q]
    m = re.search(r"\b(19\d\d)\.(\d+[A-Za-z]?)", q)
    if m:
        return idx.get(m.group(1) + "." + m.group(2))
    return None


# ── OSHA full structural index (every part / subpart / section) ──────────────
# osha_index.jsonl is the complete table of contents of the OSHA regulatory tree
# scraped from OSHA's standardnumber index: Parts 1904, 1910, 1915/1917/1918/1919
# (Maritime), 1926, 1928 — every subpart, section, and appendix with its title and
# canonical osha.gov URL (~1,400 rows). This does NOT carry each section's full
# verbatim body; it is the map, so the agent can resolve ANY citation to its
# official title/subpart/URL, list a part's tree, and know a citation is real even
# when it has no written-program record. Two companion reference lists ship too:
# the 25 whistleblower statutes OSHA administers, and recent Preambles to Final Rules.
@functools.lru_cache(maxsize=1)
def _osha_index_records() -> List[dict]:
    path = KB_DIR / "osha_index.jsonl"
    if not path.exists():
        return []
    out: List[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


@functools.lru_cache(maxsize=1)
def _osha_index() -> Dict[str, dict]:
    idx: Dict[str, dict] = {}
    for r in _osha_index_records():
        cit = r.get("citation")
        sec = r.get("section")
        if cit:
            idx[cit] = r
        if sec:
            idx.setdefault(sec, r)  # bare section falls back to its base record
    return idx


_OSHA_PART_NAMES = {
    "1904": "Recording and Reporting Occupational Injuries and Illnesses",
    "1910": "General Industry",
    "1915": "Shipyard Employment (Maritime)",
    "1917": "Marine Terminals (Maritime)",
    "1918": "Longshoring (Maritime)",
    "1919": "Gear Certification (Maritime)",
    "1926": "Construction",
    "1928": "Agriculture",
}


def osha_section(citation: str) -> Optional[dict]:
    """Resolve any OSHA citation to its official title, subpart, part, and URL.

    Accepts a bare section ('1926.501'), a full citation ('29 CFR 1926.501'), an
    appendix ('1910.7 App A'), or a subpart ('1926 Subpart M'), or any string
    containing one. Returns the structural-index record (part/part_name/subpart/
    subpart_title/citation/section/title/type/url) or None if it isn't a real
    OSHA citation in the indexed parts. Never fabricates.
    """
    q = (citation or "").strip()
    if not q:
        return None
    idx = _osha_index()
    if q in idx:
        return idx[q]
    # subpart form, e.g. "1926 Subpart M"
    sm = re.search(r"\b(\d{4})\s+Subpart\s+([A-Z]+)\b", q, re.I)
    if sm:
        key = "%s Subpart %s" % (sm.group(1), sm.group(2).upper())
        if key in idx:
            return idx[key]
    # appendix, e.g. "1910.7 App A"
    am = re.search(r"\b(\d{4}\.\w+)\s+App\s+([A-Za-z0-9]+)\b", q, re.I)
    if am:
        key = "%s App %s" % (am.group(1), am.group(2).upper())
        if key in idx:
            return idx[key]
    # bare section number
    m = re.search(r"\b(\d{4}\.\w+)\b", q)
    if m and m.group(1) in idx:
        return idx[m.group(1)]
    return None


def osha_part_tree(part: str) -> Optional[dict]:
    """Return the full subpart→section tree for an OSHA part (e.g. '1926').

    Accepts '1926', '29 CFR 1926', or any string containing the part number.
    Returns {part, part_name, subparts:[{subpart, subpart_title, url, sections:[
    {citation, title, type, url}...]}...], counts} or None if the part isn't
    indexed. Sections without a subpart (rare) are grouped under ''.
    """
    digits = re.sub(r"\D", "", str(part or ""))
    p = digits[:4]
    if p not in _OSHA_PART_NAMES:
        return None
    recs = [r for r in _osha_index_records() if r.get("part") == p]
    if not recs:
        return None
    order: List[str] = []
    groups: Dict[str, dict] = {}
    for r in recs:
        sp = r.get("subpart", "")
        if sp not in groups:
            groups[sp] = {"subpart": sp, "subpart_title": "", "url": "", "sections": []}
            order.append(sp)
        if r.get("type") == "subpart":
            groups[sp]["subpart_title"] = r.get("title", "")
            groups[sp]["url"] = r.get("url", "")
        else:
            groups[sp]["sections"].append({
                "citation": r.get("citation", ""),
                "title": r.get("title", ""),
                "type": r.get("type", ""),
                "url": r.get("url", ""),
            })
    subparts = [groups[k] for k in order]
    n_sec = sum(1 for r in recs if r.get("type") == "section")
    return {
        "part": p,
        "part_name": _OSHA_PART_NAMES[p],
        "subparts": subparts,
        "counts": {"subparts": sum(1 for s in subparts if s["subpart"]),
                   "sections": n_sec, "total_rows": len(recs)},
    }


def osha_search(query: str, limit: int = 15) -> List[dict]:
    """Keyword search over the OSHA structural index by section title.

    Ranks indexed sections/subparts whose title or citation contains the query
    terms. Returns lightweight hits (citation/title/part/subpart/url) so the agent
    can find 'the fall protection sections' or 'excavation' across the whole tree.
    """
    q = {t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 2}
    if not q:
        return []
    scored = []
    for r in _osha_index_records():
        hay = (r.get("title", "") + " " + r.get("citation", "") + " "
               + r.get("subpart_title", "")).lower()
        s = sum(t in hay for t in q)
        if s:
            scored.append((s, r))
    scored.sort(key=lambda x: (-x[0], len(x[1].get("citation", ""))))
    return [{
        "citation": r.get("citation", ""), "title": r.get("title", ""),
        "part": r.get("part", ""), "part_name": r.get("part_name", ""),
        "subpart": r.get("subpart", ""), "type": r.get("type", ""),
        "url": r.get("url", ""),
    } for _, r in scored[:limit]]


@functools.lru_cache(maxsize=1)
def _osha_reference(name: str) -> List[dict]:
    path = KB_DIR / name
    if not path.exists():
        return []
    out: List[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def osha_whistleblower_statutes() -> List[dict]:
    """The 25 statutes OSHA administers whistleblower/anti-retaliation provisions
    for, each with its U.S.C. citation, title, and whistleblowers.gov URL."""
    return _osha_reference("osha_whistleblower_statutes.jsonl")


def osha_preambles() -> List[dict]:
    """OSHA Preambles to Final Rules (Federal Register rulemaking records) — the
    complete index, ~900 rulemakings from 1971 to present, each with date, title,
    affected standard numbers, and URL, newest first."""
    return _osha_reference("osha_preambles.jsonl")


def osha_index_stats() -> dict:
    """Inventory of the OSHA structural index: total rows, and per-part counts of
    subparts and sections. Lets the agent state exactly how much of the OSHA tree
    it has mapped."""
    recs = _osha_index_records()
    parts: Dict[str, dict] = {}
    for r in recs:
        p = r.get("part", "")
        d = parts.setdefault(p, {"part_name": r.get("part_name", ""),
                                 "subparts": set(), "sections": 0, "appendices": 0})
        if r.get("type") == "section":
            d["sections"] += 1
        elif r.get("type") in ("appendix", "subpart_appendix"):
            d["appendices"] += 1
        if r.get("subpart"):
            d["subparts"].add(r["subpart"])
    return {
        "total_rows": len(recs),
        "parts": {p: {"part_name": d["part_name"], "subparts": len(d["subparts"]),
                      "sections": d["sections"], "appendices": d["appendices"]}
                  for p, d in sorted(parts.items())},
        "whistleblower_statutes": len(osha_whistleblower_statutes()),
        "preambles_indexed": len(osha_preambles()),
    }


def search(query: str, limit: int = 5) -> List[dict]:
    """Keyword fallback ranking by term overlap (no vector store required)."""
    q = {t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 2}
    scored = []
    for r in _records().values():
        hay = " ".join([
            r.get("title", ""), r.get("citation", ""), r.get("applicability", ""),
            " ".join(r.get("required_elements", [])),
        ]).lower()
        scored.append((sum(t in hay for t in q), r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for s, r in scored[:limit] if s > 0]


def templates() -> dict:
    path = KB_DIR / "Templates" / "templates_index.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def template_body(template_id: str) -> str:
    path = KB_DIR / "Templates" / f"{template_id}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def kb_stats() -> dict:
    """A plain-English inventory of the compliance knowledge base: how many
    standards are loaded, how they break down by category, how many require a
    written program, how many of those already have a fillable template, and
    exactly which standards still need a template built (the 'what to add' list).
    """
    recs = all_records()
    by_cat: Dict[str, int] = {}
    needs_program: List[dict] = []
    for r in recs:
        cat = r.get("category") or "(uncategorized)"
        by_cat[cat] = by_cat.get(cat, 0) + 1
        if (r.get("written_program") or "").strip().lower() in ("yes", "conditional"):
            needs_program.append(r)

    # Which written-program standards already ship a fillable template file?
    prog_dir = KB_DIR / "Templates" / "programs"
    have: set = set()
    if prog_dir.exists():
        for f in prog_dir.glob("program-*.md"):
            have.add(f.stem[len("program-"):])

    missing = [
        {
            "id": r["id"],
            "title": r.get("title", ""),
            "citation": r.get("citation", ""),
            "category": r.get("category", ""),
        }
        for r in needs_program
        if r["id"] not in have
    ]
    missing.sort(key=lambda m: (m["category"], m["title"]))

    return {
        "total_standards": len(recs),
        "by_category": dict(sorted(by_cat.items())),
        "written_program_required": len(needs_program),
        "program_templates": len(have),
        "missing_templates": missing,
    }


# ── fillable written-program renderer ───────────────────────────────────────
_PROGRAM_PLACEHOLDERS = [
    "COMPANY_NAME", "COMPANY_ADDRESS", "PROGRAM_ADMINISTRATOR", "ADMIN_TITLE",
    "ADMIN_PHONE", "ADMIN_EMAIL", "EFFECTIVE_DATE", "SCOPE", "LAST_REVIEW_DATE",
    "NEXT_REVIEW_DATE", "SIGNATURE_NAME", "SIGNATURE_TITLE", "SIGNATURE_DATE",
]


def render_program(entry_id: str) -> Optional[str]:
    """Return a fillable, editable written-program document for a KB standard.

    Serves the pre-generated file at ``Templates/programs/program-<id>.md`` when
    it exists; otherwise builds the same document on the fly from the record so
    any standard (including ones added later) yields a workable template. Section
    headings, training and recordkeeping lines come verbatim from the record, so
    the template can never drift from the KB. Returns ``None`` if no such record.
    """
    static = KB_DIR / "Templates" / "programs" / f"program-{entry_id}.md"
    if static.exists():
        return static.read_text(encoding="utf-8")

    r = get(entry_id)
    if not r:
        return None

    cite = (r.get("citation") or "").strip()
    elems = r.get("required_elements") or []
    training = (r.get("training") or "").strip()
    record = (r.get("recordkeeping") or "").strip()
    appl = (r.get("applicability") or "").strip()
    fails = r.get("failure_points") or []

    L = [
        f"# {r.get('title','')}",
        "**{{COMPANY_NAME}} — Written Safety Program**  ",
        f"Governing standard: {cite}  ",
        "Effective date: {{EFFECTIVE_DATE}}  ",
        "Program administrator: {{PROGRAM_ADMINISTRATOR}}, {{ADMIN_TITLE}} "
        "({{ADMIN_PHONE}} / {{ADMIN_EMAIL}})",
        "",
        "## 1. Purpose and Scope",
    ]
    if appl:
        L += [f"*Applicability (from the standard):* {appl}", ""]
    L += [
        f"This program establishes {{{{COMPANY_NAME}}}}'s procedures to comply with {cite}. "
        "It applies to: {{SCOPE}}.",
        "",
        "## 2. Responsibilities",
        "{{PROGRAM_ADMINISTRATOR}} ({{ADMIN_TITLE}}) is accountable for implementing this "
        "program, training affected employees, keeping the records in Section 5, and reviewing "
        "the program on the cadence in Section 6. Supervisors enforce it; employees follow it "
        "and report hazards.",
        "",
        "## 3. Program Elements",
        f"*Every element required by {cite} is addressed below. Replace each prompt with your "
        "company-specific procedure.*",
        "",
    ]
    for i, el in enumerate(elems, 1):
        L += [f"### 3.{i}  {el}",
              "[[Describe how {{COMPANY_NAME}} does this — the procedure, who is responsible, "
              "what equipment/forms are used, and how it is documented.]]", ""]
    L += ["## 4. Training"]
    if training:
        L += [f"*Standard requirement:* {training}", ""]
    verbatim = (r.get("training_verbatim") or "").strip()
    if verbatim:
        vsrc = (r.get("training_verbatim_source") or {}).get("citation", cite)
        L += [f"*Verbatim training language (from {vsrc}, OSHA 2254 — Training Requirements "
              "in OSHA Standards):*", "", verbatim, ""]
    L += ["[[State who is trained, by whom, how often, the topics, and how training is "
          "documented.]]", "",
          "## 5. Recordkeeping and Retention"]
    if record:
        L += [f"*Standard requirement:* {record}", ""]
    L += ["[[List the records this program generates, where they are kept, who maintains them, "
          "and the retention period.]]", "",
          "## 6. Program Review",
          f"Reviewed at least annually and whenever operations, equipment, or {cite} change. "
          "Last reviewed: {{LAST_REVIEW_DATE}}. Next review due: {{NEXT_REVIEW_DATE}}.", ""]
    if fails:
        L += ["## 7. Reviewer Rejection Checklist — confirm NONE apply before submitting"]
        L += [f"- [ ] {f}" for f in fails]
        L += [""]
    L += ["## Management Certification",
          "I certify that this written program is implemented at {{COMPANY_NAME}} and reviewed "
          "on the cadence stated above.", "",
          "Signature: ______________________________  ",
          "Name: {{SIGNATURE_NAME}}  ",
          "Title: {{SIGNATURE_TITLE}}  ",
          "Date: {{SIGNATURE_DATE}}", ""]
    return "\n".join(L)


# ── NAICS → required-standards resolver ─────────────────────────────────────
@functools.lru_cache(maxsize=1)
def _naics_map() -> dict:
    path = KB_DIR / "naics_map.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _sector_for(code_or_industry: str) -> Optional[str]:
    """Resolve a NAICS sector key from a numeric code or an industry name."""
    m = _naics_map()
    sectors = m.get("sectors", {})
    raw = (code_or_industry or "").strip()
    if not raw:
        return None

    # numeric: match the 2-digit sector prefix (handles ranges like 31-33, 48-49)
    digits = re.sub(r"\D", "", raw)
    if digits:
        prefix = digits[:2]
        if prefix in sectors:
            return prefix
        for key in sectors:  # ranges e.g. "31-33", "48-49"
            if "-" in key:
                lo, hi = key.split("-", 1)
                if lo.isdigit() and hi.isdigit() and int(lo) <= int(prefix or 0) <= int(hi):
                    return key
        return None

    # text: score each sector's keywords against the industry name
    low = raw.lower()
    best, best_score = None, 0
    for key, sec in sectors.items():
        score = sum(1 for kw in sec.get("keywords", []) if kw in low)
        if score > best_score:
            best, best_score = key, score
    return best


def naics_applicable(code_or_industry: str, state: Optional[str] = None) -> dict:
    """Return the KB standards a prequal review typically requires for a client.

    Unions the ``universal`` standards, the matched NAICS sector's standards,
    and any ``state`` overlay (e.g. "CA" for Cal/OSHA IIPP + heat). Returns
    resolved records (id/title/citation/category) plus the sector and any
    coverage gap notes so the agent can auto-scope a client by industry.
    """
    m = _naics_map()
    if not m:
        return {"error": "naics_map.json not found", "sector": None, "standards": []}

    recs = _records()
    ids: List[str] = list(m.get("universal", []))
    buckets = {"universal": list(m.get("universal", []))}

    sector_key = _sector_for(code_or_industry)
    sector_label = None
    gap_note = None
    if sector_key:
        sec = m["sectors"][sector_key]
        sector_label = sec.get("label")
        gap_note = sec.get("gap_note")
        sec_ids = sec.get("standards", [])
        buckets["sector"] = sec_ids
        for i in sec_ids:
            if i not in ids:
                ids.append(i)

    state_key = (state or "").strip().upper() or None
    if state_key and state_key in m.get("state_overlays", {}):
        st = m["state_overlays"][state_key]
        st_ids = st.get("standards", [])
        buckets["state"] = st_ids
        for i in st_ids:
            if i not in ids:
                ids.append(i)

    standards = []
    for i in ids:
        r = recs.get(i)
        if r:
            standards.append({
                "id": i,
                "title": r.get("title", ""),
                "citation": r.get("citation", ""),
                "category": r.get("category", ""),
                "written_program": r.get("written_program", ""),
            })

    return {
        "input": code_or_industry,
        "sector": sector_key,
        "sector_label": sector_label,
        "state": state_key,
        "gap_note": gap_note,
        "count": len(standards),
        "buckets": {k: len(v) for k, v in buckets.items()},
        "standards": standards,
    }


# ── Hiring-client requirement overlay (operator profiles) ───────────────────
@functools.lru_cache(maxsize=1)
def _hiring_profiles() -> dict:
    path = KB_DIR / "hiring_client_profiles.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def list_hiring_clients() -> List[dict]:
    """The catalog: every operator we have a profile for."""
    doc = _hiring_profiles()
    return [
        {
            "hiring_client": p["hiring_client"],
            "sector": p.get("sector"),
            "archetype": p.get("archetype"),
            "confirmed": p.get("confirmed", False),
        }
        for p in doc.get("profiles", [])
    ]


def _match_hiring_client(name: str) -> Optional[dict]:
    """Resolve a loose name ('exxon', 'chevron pipeline') to a profile."""
    raw = (name or "").strip().lower()
    if not raw:
        return None
    profs = _hiring_profiles().get("profiles", [])
    for p in profs:                                   # exact
        if p["hiring_client"].lower() == raw:
            return p
    for p in profs:                                   # prefix / substring
        pl = p["hiring_client"].lower()
        if pl.startswith(raw) or raw in pl:
            return p
    toks = set(re.findall(r"[a-z0-9]+", raw))         # token overlap
    best, best_score = None, 0
    for p in profs:
        pt = set(re.findall(r"[a-z0-9]+", p["hiring_client"].lower()))
        score = len(toks & pt)
        if score > best_score:
            best, best_score = p, score
    return best if best_score else None


def hiring_client_gaps(client_name: str, industry: str,
                       state: Optional[str] = None) -> dict:
    """Overlay one operator's extra prequal requirements on the NAICS baseline.

    Returns the ISN/NAICS baseline the contractor already needs for their trade,
    PLUS the operator-specific layer (insurance limits, EMR/TRIR caps, extra
    written programs, training) this hiring client bolts on — i.e. the ADDITIONAL
    gaps to close so the contractor stays hireable by THIS client.
    """
    prof = _match_hiring_client(client_name)
    if not prof:
        return {"error": f"No profile for '{client_name}'. "
                         f"Call list_hiring_clients for the catalog.",
                "hiring_client": None}
    base = naics_applicable(industry, state=state)     # reuse existing resolver
    return {
        "hiring_client": prof["hiring_client"],
        "archetype": prof.get("archetype"),
        "confirmed": prof.get("confirmed", False),
        "baseline": base,
        "overlay": {
            "prequal_platforms": prof.get("prequal_platforms", []),
            "isn_grade_target": prof.get("isn_grade_target"),
            "insurance": prof.get("insurance", {}),
            "performance": prof.get("performance", {}),
            "programs_training": prof.get("programs_training", {}),
            "grading_flags": prof.get("grading_flags"),
        },
        "source": prof.get("source"),
        "note": ("Overlay values are archetype-seeded (confirmed=false) — verify "
                 "against this client's live ISN requirement list before quoting.")
                 if not prof.get("confirmed") else "",
    }


# ── text helpers ────────────────────────────────────────────────────────────
def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (text.replace("&amp;", "&").replace("&nbsp;", " ")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    return re.sub(r"\s+", " ", text).strip()


def _keywords(label: str) -> List[str]:
    """Significant tokens from the leading label of a required element."""
    head = re.split(r"[—\-–(:]", label, 1)[0]
    toks = [t for t in re.split(r"\W+", head.lower()) if len(t) > 3 and t not in _STOP]
    return toks or [t for t in re.split(r"\W+", label.lower()) if len(t) > 3 and t not in _STOP]


def _citation_num(citation: str) -> str:
    m = re.search(r"(\d{3,4}\.\d+)", citation or "")
    return m.group(1) if m else ""


def _citation_present(citation: str, text: str) -> bool:
    if not citation:
        return False
    if citation.lower() in text:
        return True
    num = _citation_num(citation)
    return bool(num and num in text)


# ── the validation gate ─────────────────────────────────────────────────────
def resolve_standards(text: str, limit: int = 6) -> List[dict]:
    """Detect which KB standards a document invokes, by citation or title."""
    low = text.lower()
    hits: List[dict] = []
    for r in _records().values():
        title = r.get("title", "")
        exact_cite = (r.get("citation", "").lower() in low) if r.get("citation") else False
        title_hit = len(title) > 8 and title.lower() in low
        if _citation_present(r.get("citation", ""), low) or title_hit:
            hits.append((r, exact_cite or title_hit))

    # When several records share a citation number (e.g. a specialty program
    # reuses the base standard's citation), keep only the strongest match for
    # that number: prefer an exact citation/title hit, then the shortest
    # (canonical) citation string, so the base standard wins over a variant.
    by_num: Dict[str, list] = {}
    for r, strong in hits:
        key = _citation_num(r.get("citation", "")) or r["id"]
        by_num.setdefault(key, []).append((r, strong))

    out: List[dict] = []
    for key, group in by_num.items():
        strong = [r for r, s in group if s]
        pool = strong or [r for r, _ in group]
        pool.sort(key=lambda r: len(r.get("citation", "")))
        out.append(pool[0])
    return out[:limit]


def check_standard(rec: dict, text: str, element_threshold: float = 0.5) -> dict:
    """Score one standard's required_elements against the document text."""
    low = text.lower()
    elements = []
    for el in rec.get("required_elements", []):
        kws = _keywords(el)
        present = [k for k in kws if k in low]
        ratio = (len(present) / len(kws)) if kws else 0.0
        elements.append({
            "element": el,
            "covered": ratio >= element_threshold,
            "coverage": round(ratio, 2),
        })
    total = len(elements)
    covered = sum(1 for e in elements if e["covered"])
    return {
        "id": rec["id"],
        "title": rec.get("title", ""),
        "citation": rec.get("citation", ""),
        "source": rec.get("source", ""),
        "written_program": rec.get("written_program", ""),
        "elements_total": total,
        "elements_covered": covered,
        "coverage_ratio": round(covered / total, 2) if total else 1.0,
        "missing_elements": [e["element"] for e in elements if not e["covered"]],
        "failure_points": rec.get("failure_points", []),
        "training": rec.get("training", ""),
        "recordkeeping": rec.get("recordkeeping", ""),
    }


def validate_document(
    html: str,
    entry_ids: Optional[List[str]] = None,
    *,
    pass_ratio: float = 0.8,
) -> dict:
    """Gate a compliance document against the KB before it goes to a client.

    Resolves the relevant standard(s) — either the explicit ``entry_ids`` or by
    auto-detecting citations/titles in the text — then checks the draft's
    coverage of each standard's ``required_elements``.

    A document PASSES only when at least one standard is identified and every
    identified standard covers >= ``pass_ratio`` of its required elements.
    ``failure_points`` are always returned so a human/agent can affirm them.
    """
    text = _strip_html(html)
    low = text.lower()

    if entry_ids:
        recs = [get(e) for e in entry_ids]
        recs = [r for r in recs if r]
    else:
        recs = resolve_standards(low)

    if not recs:
        return {
            "passed": False,
            "status": "unverified",
            "reason": ("No codified standard could be matched to this document. "
                       "Specify the governing citation (e.g. 29 CFR 1910.119) or "
                       "assign the standard before sending."),
            "standards": [],
            "checked": 0,
        }

    results = [check_standard(r, low, ) for r in recs]
    failing = [r for r in results if r["coverage_ratio"] < pass_ratio]
    passed = len(failing) == 0

    return {
        "passed": passed,
        "status": "pass" if passed else "fail",
        "pass_ratio": pass_ratio,
        "checked": len(results),
        "failing": [r["citation"] for r in failing],
        "standards": results,
        "summary": _summary(results, passed),
    }


def _summary(results: List[dict], passed: bool) -> str:
    lines = []
    for r in results:
        tag = "OK" if r["coverage_ratio"] >= 0.8 else "GAP"
        lines.append(
            f"[{tag}] {r['citation']} — {r['elements_covered']}/{r['elements_total']} "
            f"required elements present"
            + (f"; missing: {', '.join(m.split('—')[0].strip() for m in r['missing_elements'])}"
               if r["missing_elements"] else "")
        )
    head = ("PASS — document covers the required elements of every identified standard."
            if passed else
            "FAIL — document is missing required elements for one or more standards.")
    return head + "\n" + "\n".join(lines)
