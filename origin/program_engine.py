"""Program Engine — the Safety Intelligence Engine's written-program, training,
and JHA/JSA assembler (Phase 3).

Phase 2 tells you *which* standards apply to a company and how risky its open
findings are. Phase 3 answers the next question every contractor and prequal
reviewer asks: **"show me the actual written program, the training, and the job
hazard analysis for each of those standards."**

This module does not author new safety content from scratch — Origin already has a
deep, hand-built library (compliance_kb written programs, the OSHA 2254 training
package, and the compliance_jha JHA/JSA generator, all sector-aware). Phase 3's job
is to put a **deterministic, sourced, never-fabricate envelope** around that library
and bind it to a company:

  * For each required standard in a company's scope it assembles a package entry:
      - program   : the fillable written program (render_program, sector-specific),
                    classified OSHA-required when the standard mandates a written
                    program, else Origin recommendation.
      - training  : the verbatim OSHA 2254 training language when one exists
                    (OSHA-required), else an Origin training recommendation.
      - jsa       : the job hazard analysis (render_jha) when the library has one,
                    else honestly marked "not in library".
  * Every entry carries the traceable source refs from knowledge_store.resolve, so
    the chain program → standard → knowledge version is never broken.
  * Never fabricates. If the library has no program / training / JSA for a standard,
    the entry says so plainly instead of inventing a document.

Same house rules as the other SIE modules: deterministic, fully offline (no LLM),
isolated + non-fatal registration.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import compliance_kb as kb
from . import compliance_jha as jha
from . import knowledge_store as ks
from . import company_profile as company

try:
    from . import sector_content as _sc
except Exception:  # pragma: no cover
    _sc = None

try:
    from . import citations as _citations
except Exception:  # citation layer must never break document render
    _citations = None

# ── classification labels (single, never mixed — same vocabulary as Phase 1) ──
OSHA_REQUIRED = "OSHA-required"
ORIGIN_REC = "Origin recommendation"
BEST_PRACTICE = "Best practice"


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "n", "none")
    return bool(v)


def _source_refs(citation: str) -> List[Dict[str, str]]:
    """Traceable source envelope for a standard, via the versioned knowledge store."""
    rec = ks.resolve(citation) if citation else None
    if not rec:
        return []
    return [{
        "citation": rec.get("regulation_number", ""),
        "title": rec.get("title", ""),
        "url": rec.get("source_url", ""),
        "version": rec.get("version_label", ""),
        "jurisdiction": rec.get("jurisdiction", ""),
    }]


def _availability(entry_id: str, citation: str, sector: Optional[str]) -> Dict[str, Any]:
    """Which artifacts the library actually has for this standard (booleans only —
    fast, no document rendering)."""
    program_md = kb.render_program(entry_id, sector) if entry_id else None
    training = kb.training_requirement(citation) if citation else None
    return {
        "program_available": program_md is not None,
        "training_mandate": training is not None,
        "jsa_available": bool(entry_id) and jha.has_jha(entry_id),
    }


def _classify_program(stub: Dict[str, Any], available: bool) -> str:
    """A written program is OSHA-required only when the standard mandates one
    (the scope stub's `written_program` flag). Otherwise it is an Origin
    recommendation. Availability doesn't change the legal classification — a
    mandated program that's missing from the library is still OSHA-required
    (and that's exactly the gap the company must close)."""
    return OSHA_REQUIRED if _truthy(stub.get("written_program")) else ORIGIN_REC


def build_entry(stub: Dict[str, Any], sector: Optional[str]) -> Dict[str, Any]:
    """One package row for one required standard — classifications + availability +
    source, but NOT the full rendered document bodies (kept light for list views)."""
    entry_id = stub.get("id", "")
    citation = stub.get("citation", "") or ""
    avail = _availability(entry_id, citation, sector)
    refs = _source_refs(citation)
    resolved = bool(refs)

    program_class = _classify_program(stub, avail["program_available"])
    training_class = OSHA_REQUIRED if avail["training_mandate"] else ORIGIN_REC

    return {
        "id": entry_id,
        "citation": citation,
        "title": stub.get("title", ""),
        "category": stub.get("category", ""),
        "why": stub.get("why", ""),
        "source": stub.get("source", ""),          # baseline vs triggered
        "resolved": resolved,                       # standard verified in knowledge store
        "program": {
            "classification": program_class,
            "available": avail["program_available"],
            "note": "" if avail["program_available"]
                    else "No written program in the Origin library for this standard yet.",
        },
        "training": {
            "classification": training_class,
            "available": True,   # a training row is always produced (verbatim or Origin-rec)
            "verbatim_mandate": avail["training_mandate"],
        },
        "jsa": {
            "classification": BEST_PRACTICE,   # a JHA/JSA is a best-practice tool, not a mandate
            "available": avail["jsa_available"],
            "note": "" if avail["jsa_available"]
                    else "No job hazard analysis in the library for this standard.",
        },
        "source_refs": refs,
    }


def build_package(company_id: str) -> Optional[Dict[str, Any]]:
    """Assemble the full compliance-program package for a profiled company.

    Returns per-standard entries plus a rollup of how complete the company's
    program set is (mandated programs present vs. missing, training mandates,
    JSAs available). None if the company has no profile.
    """
    scope = company.scope(company_id)
    if scope is None:
        return None

    sector = scope.get("sector")
    stubs = scope.get("required_standards", []) or []
    entries = [build_entry(s, sector) for s in stubs]

    mandated = [e for e in entries if e["program"]["classification"] == OSHA_REQUIRED]
    mandated_missing = [e for e in mandated if not e["program"]["available"]]
    training_mandates = [e for e in entries if e["training"]["verbatim_mandate"]]
    jsas = [e for e in entries if e["jsa"]["available"]]

    return {
        "company_id": company._slug(company_id),
        "company": scope.get("company", ""),
        "sector": sector,
        "sector_label": scope.get("sector_label"),
        "required_count": len(entries),
        "summary": {
            "programs_mandated": len(mandated),
            "programs_mandated_missing": len(mandated_missing),
            "programs_available": sum(1 for e in entries if e["program"]["available"]),
            "training_mandates": len(training_mandates),
            "jsas_available": len(jsas),
        },
        "mandated_missing_ids": [e["id"] for e in mandated_missing],
        "classification_legend": {
            OSHA_REQUIRED: "The standard mandates this (written program or training).",
            ORIGIN_REC: "Origin's professional recommendation, not a specific mandate.",
            BEST_PRACTICE: "Industry best-practice tool (e.g. a JHA/JSA).",
        },
        "entries": entries,
    }


def render_document(company_id: str, standard_id: str) -> Optional[Dict[str, Any]]:
    """Full rendered artifacts for one standard in a company's context: the written
    program markdown (sector-specific), the verbatim/Origin training text, and the
    JHA/JSA HTML — each with its classification and source. None if the standard
    isn't in the company's scope."""
    pkg = build_package(company_id)
    if not pkg:
        return None
    entry = next((e for e in pkg["entries"] if e["id"] == standard_id), None)
    if entry is None:
        return None

    sector = pkg.get("sector")
    citation = entry["citation"]

    program_md = kb.render_program(standard_id, sector)
    tr = kb.training_requirement(citation) if citation else None
    if tr:
        training_body = tr.get("training_requirement", "")
        training_class = OSHA_REQUIRED
        training_src = [{"citation": tr.get("citation", ""), "title": tr.get("standard_title", ""),
                         "url": tr.get("source", ""), "version": "OSHA-2254"}]
    else:
        training_body = ("No verbatim OSHA training mandate is on file for this exact section. "
                         "Origin recommendation: train and document the affected employees and "
                         "supervisors on this hazard and the governing procedure, and retain a "
                         "dated sign-in sheet.")
        training_class = ORIGIN_REC
        training_src = []

    jsa_html = jha.render_jha(standard_id, sector) if jha.has_jha(standard_id) else None

    # Bulletproof layer: the verbatim CFR text of the standard this whole package
    # is built on, so the document can show the actual regulation it cites.
    # Never-fabricate: only included when the KB resolves it; isolated.
    reg_text = None
    if _citations is not None and citation:
        try:
            rec = _citations.cite(citation)
            if rec.get("ok"):
                reg_text = rec
        except Exception:
            reg_text = None

    return {
        "company_id": pkg["company_id"],
        "company": pkg["company"],
        "sector": sector,
        "standard": {
            "id": standard_id, "citation": citation, "title": entry["title"],
            "resolved": entry["resolved"], "source_refs": entry["source_refs"],
        },
        "regulatory_text": reg_text,
        "program": {
            "classification": entry["program"]["classification"],
            "available": program_md is not None,
            "markdown": program_md or "",
            "note": entry["program"]["note"],
        },
        "training": {
            "classification": training_class,
            "verbatim_mandate": tr is not None,
            "text": training_body,
            "source_refs": training_src,
        },
        "jsa": {
            "classification": BEST_PRACTICE,
            "available": jsa_html is not None,
            "html": jsa_html or "",
            "note": entry["jsa"]["note"],
        },
    }


def build_for_standard(citation_or_id: str, sector: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Standalone: the program/training/JSA availability for a single standard,
    without a company context. Accepts a KB program id or a citation. None if the
    standard resolves to nothing in the library or knowledge store."""
    q = (citation_or_id or "").strip()
    if not q:
        return None
    # Try as a KB program id first, then as a citation.
    rec = kb.by_citation(q)
    entry_id = q if kb.render_program(q, sector) is not None else (
        # if a citation was passed, find its program id from the corpus record
        (rec or {}).get("id", "") if rec else "")
    citation = q if q.upper().startswith(("29 CFR", "49 CFR")) or "." in q.split()[0] else (rec or {}).get("citation", "")
    # Enrich title/citation from the versioned knowledge store when the corpus
    # record didn't match on an exact citation string (honest, still sourced).
    resolved = ks.resolve(citation or q)
    if not entry_id and not citation and not resolved:
        return None
    stub = {"id": entry_id,
            "citation": citation or (resolved or {}).get("regulation_number", "") or q,
            "title": (rec or {}).get("title", "") or (resolved or {}).get("title", ""),
            "category": (rec or {}).get("category", ""),
            "written_program": (rec or {}).get("written_program", True), "source": "standalone"}
    return build_entry(stub, sector)


# ── routes ────────────────────────────────────────────────────────────────────
def register_program(app) -> None:
    """Attach Program Engine routes. Isolated + non-fatal, mirroring the other SIE
    modules. Fully offline — no route consults an external model."""
    from fastapi.responses import JSONResponse

    @app.get("/api/program/package/{company_id}")
    def program_package(company_id: str):
        pkg = build_package(company_id)
        if pkg is None:
            return JSONResponse({"error": "company not found"}, status_code=404)
        return pkg

    @app.get("/api/program/{company_id}/{standard_id}")
    def program_document(company_id: str, standard_id: str):
        doc = render_document(company_id, standard_id)
        if doc is None:
            return JSONResponse({"error": "standard not in this company's scope"},
                                status_code=404)
        return doc

    @app.get("/api/program/standard")
    def program_standard(q: str = "", sector: str = ""):
        if not q:
            return JSONResponse({"error": "provide q (a citation or program id)"},
                                status_code=400)
        out = build_for_standard(q, sector or None)
        if out is None:
            return JSONResponse(
                {"error": "Unable to resolve this standard in the Origin library."},
                status_code=404)
        return {"ok": True, "entry": out}
