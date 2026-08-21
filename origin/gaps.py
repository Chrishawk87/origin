"""gaps.py — Origin Gap Finder (INTERNAL tool).

Load a contractor's documents plus their industry / state (and, optionally, the
operators they work for) and get back EVERY compliance gap:

  * MISSING  — a required standard nothing in their docs covers at all
  * FAILING  — a required standard they have SOMETHING for, but it's missing
               required elements a reviewer will bounce
  * PRESENT  — a required standard their docs already satisfy

Each gap carries the specific missing elements, the reviewer failure points that
will sink it, and whether Origin can auto-draft the fix (Phase 2).

This is the orchestration layer over machinery that ALREADY exists in Origin:
  - compliance_kb.naics_applicable()  -> required standard set by industry/state
  - compliance_kb.hiring_client_gaps()-> operator-specific extra requirements
  - document_tools.extract_text()     -> read each file (PDF/scan/docx/img, OCR)
  - compliance_kb.resolve_standards() -> which standards a document invokes
  - compliance_kb.check_standard()    -> element-by-element coverage of a standard
  - compliance_kb.render_program()    -> the fillable template (Phase 2 auto-draft)

Nothing here is client-facing. It runs behind the app access token.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from . import compliance_kb as kb

try:
    from .tools import document_tools as _doc
except Exception:  # pragma: no cover - tools package layout guard
    _doc = None

# A standard counts as PRESENT once its required elements are this well covered.
# Mirrors validate_document()'s default gate so the two agree.
PASS_RATIO = 0.8

# check_standard() is keyword-based, so generic safety words ("training",
# "program", "employee") bleed a little coverage into standards the contractor
# never actually wrote. To avoid mislabelling those as FAILING (they're really
# MISSING), a standard is only FAILING if the docs EITHER explicitly invoke it
# (matched by citation/title via resolve_standards) OR clear this coverage floor.
FAILING_FLOOR = 0.34

# Prequal platform fingerprints — same idea compliance_intake uses, so the
# report can say "these look like ISNetworld documents" without being asked.
_PLATFORM_HINTS = {
    "ISNetworld": ["isnetworld", "isn ", "isnet", " ravs", "review and verification"],
    "Avetta": ["avetta", "browz"],
    "Veriforce": ["veriforce", "pec safety", "pec premier"],
    "PEC": ["pec safety", "pec premier"],
    "ComplyWorks": ["complyworks"],
}


def extract_text(path: str) -> str:
    """Read one uploaded file to text (PDF/scan/docx/image/plain)."""
    if _doc is None:
        return f"ERROR: document reader unavailable (cannot read {path})"
    return _doc.extract_text(path)


def _detect_platforms(text: str) -> List[str]:
    low = text.lower()
    found = []
    for name, hints in _PLATFORM_HINTS.items():
        if any(h in low for h in hints):
            found.append(name)
    return found


def _severity(status: str, needs_program: bool, category: str) -> int:
    """Lower sorts first. Missing written programs are the most urgent."""
    if status == "MISSING":
        return 0 if needs_program else 1
    if status == "FAILING":
        return 2 if needs_program else 3
    return 5  # PRESENT


def _required_set(industry: str, state: Optional[str],
                  operators: Optional[List[str]]) -> Tuple[List[dict], dict]:
    """Union the NAICS/state baseline with any operator overlays.

    Returns (list of standard stubs {id,title,citation,category,written_program},
    meta) where meta records the sector, state, operators matched, and notes.
    """
    base = kb.naics_applicable(industry, state=state)
    stubs: Dict[str, dict] = {}
    for s in base.get("standards", []):
        stubs[s["id"]] = s

    op_meta: List[dict] = []
    for name in (operators or []):
        name = (name or "").strip()
        if not name:
            continue
        og = kb.hiring_client_gaps(name, industry, state=state)
        if og.get("error"):
            op_meta.append({"operator": name, "matched": False, "note": og["error"]})
            continue
        for s in og.get("baseline", {}).get("standards", []):
            stubs.setdefault(s["id"], s)
        op_meta.append({
            "operator": og.get("hiring_client", name),
            "matched": True,
            "confirmed": og.get("confirmed", False),
            "overlay": og.get("overlay", {}),
            "note": og.get("note", ""),
        })

    meta = {
        "industry": industry,
        "sector": base.get("sector"),
        "sector_label": base.get("sector_label"),
        "state": base.get("state"),
        "gap_note": base.get("gap_note"),
        "operators": op_meta,
        "baseline_count": base.get("count", 0),
    }
    return list(stubs.values()), meta


def _needs_program(stub: dict) -> bool:
    return (stub.get("written_program", "") or "").strip().lower() in ("yes", "conditional")


def find_gaps(
    industry: str,
    state: Optional[str] = None,
    operators: Optional[List[str]] = None,
    docs: Optional[List[Dict[str, str]]] = None,
) -> dict:
    """Compute the full gap report.

    ``docs`` is a list of ``{"name": <filename>, "text": <extracted text>}``.
    (The route extracts the text; the engine stays pure so it's easy to test.)
    """
    docs = docs or []
    corpus = "\n\n".join(d.get("text", "") for d in docs)

    required, meta = _required_set(industry, state, operators)

    # Which standards do the uploaded docs actually invoke? (informational —
    # surfaces things they cover that we didn't flag as required for the trade.)
    resolved_ids = {r["id"] for r in kb.resolve_standards(corpus, limit=50)} if corpus else set()

    gaps: List[dict] = []
    counts = {"MISSING": 0, "FAILING": 0, "PRESENT": 0}

    for stub in required:
        rec = kb.get(stub["id"])
        if not rec:
            continue
        needs_prog = _needs_program(stub)
        chk = kb.check_standard(rec, corpus) if corpus else {
            "elements_total": len(rec.get("required_elements", [])),
            "elements_covered": 0,
            "coverage_ratio": 0.0,
            "missing_elements": list(rec.get("required_elements", [])),
            "failure_points": rec.get("failure_points", []),
            "training": rec.get("training", ""),
            "recordkeeping": rec.get("recordkeeping", ""),
        }
        ratio = chk.get("coverage_ratio", 0.0)
        invoked = stub["id"] in resolved_ids  # cited/titled — a strong signal

        if ratio >= PASS_RATIO:
            status = "PRESENT"
        elif invoked or ratio >= FAILING_FLOOR:
            status = "FAILING"
        else:
            status = "MISSING"
        counts[status] += 1

        gaps.append({
            "id": stub["id"],
            "title": stub.get("title", rec.get("title", "")),
            "citation": stub.get("citation", rec.get("citation", "")),
            "category": stub.get("category", rec.get("category", "")),
            "written_program": stub.get("written_program", ""),
            "needs_program": needs_prog,
            "status": status,
            "coverage_ratio": ratio,
            "elements_total": chk.get("elements_total", 0),
            "elements_covered": chk.get("elements_covered", 0),
            "missing_elements": chk.get("missing_elements", []),
            "failure_points": chk.get("failure_points", []),
            "training": chk.get("training", ""),
            "recordkeeping": chk.get("recordkeeping", ""),
            # Phase 2: Origin can auto-draft any written-program standard from KB.
            "can_autodraft": needs_prog,
            "severity": _severity(status, needs_prog, stub.get("category", "")),
        })

    gaps.sort(key=lambda g: (g["severity"], g["category"], g["title"]))

    # Standards the docs cover that aren't in the required set (context only).
    extra = []
    required_ids = {s["id"] for s in required}
    for rid in resolved_ids - required_ids:
        r = kb.get(rid)
        if r:
            extra.append({"id": rid, "title": r.get("title", ""),
                          "citation": r.get("citation", "")})

    platforms = _detect_platforms(corpus)
    total = len(gaps)

    # Split the score by what Origin can actually act on. Written programs are
    # draftable (Phase 2); References (insurance COIs, TRIR/EMR benchmarks,
    # platform process pages) are context Chris verifies, not documents to draft.
    prog = [g for g in gaps if g["needs_program"]]
    prog_missing = sum(1 for g in prog if g["status"] == "MISSING")
    prog_failing = sum(1 for g in prog if g["status"] == "FAILING")
    prog_present = sum(1 for g in prog if g["status"] == "PRESENT")
    prog_total = len(prog)
    ref_open = sum(1 for g in gaps if not g["needs_program"] and g["status"] != "PRESENT")

    headline = _headline(prog_missing, prog_failing, prog_present, prog_total)

    return {
        "meta": meta,
        "documents": [{"name": d.get("name", ""),
                       "chars": len(d.get("text", "")),
                       "platforms": _detect_platforms(d.get("text", ""))}
                      for d in docs],
        "platforms_detected": platforms,
        "summary": {
            "required_total": total,
            "present": counts["PRESENT"],
            "failing": counts["FAILING"],
            "missing": counts["MISSING"],
            # Written-program breakdown — the draftable, actionable core.
            "programs_total": prog_total,
            "programs_present": prog_present,
            "programs_failing": prog_failing,
            "programs_missing": prog_missing,
            "references_open": ref_open,
            "readiness_pct": round(100 * prog_present / prog_total) if prog_total else 0,
            "headline": headline,
        },
        "gaps": gaps,
        "also_covered": extra,
    }


# ── Phase 2: auto-draft the missing / failing written programs ──────────────
def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60] or "program"


def draft_programs(
    ids: List[str],
    company: Optional[str] = None,
    effective_date: Optional[str] = None,
) -> List[dict]:
    """Render a fillable written program for each standard id.

    Reuses compliance_kb.render_program (which serves the pre-generated template
    or builds one on the fly from the KB record, so a draft can never drift from
    the standard). Pre-fills the company name / effective date when supplied;
    every other {{PLACEHOLDER}} and [[prompt]] is left for Chris to complete.
    """
    company = (company or "").strip()
    effective_date = (effective_date or "").strip()
    out: List[dict] = []
    seen: set = set()
    for eid in ids:
        if not eid or eid in seen:
            continue
        seen.add(eid)
        md = kb.render_program(eid)
        if not md:
            continue
        rec = kb.get(eid) or {}
        if company:
            md = md.replace("{{COMPANY_NAME}}", company)
        if effective_date:
            md = md.replace("{{EFFECTIVE_DATE}}", effective_date)
        title = rec.get("title", eid)
        out.append({
            "id": eid,
            "title": title,
            "citation": rec.get("citation", ""),
            "filename": f"program-{_slug(title)}.md",
            "markdown": md,
        })
    return out


def _headline(missing: int, failing: int, present: int, total: int) -> str:
    if total == 0:
        return "No required written programs resolved — check the industry/state inputs."
    if missing == 0 and failing == 0:
        return "Every required written program is covered. Ready for review."
    open_items = missing + failing
    return (f"{open_items} of {total} required written programs need work "
            f"({missing} missing, {failing} failing). Origin can draft all of them.")
