"""Origin — Applicable Requirements Engine (Stage 2).

"What applies to THIS company, and WHY" — as data, not a checklist.

`scoping.scope_company()` already resolves a company's tailored required-standard
set (NAICS baseline UNION activity-triggered standards, each with an applicability
reason, a citation, and the OSHA jurisdiction). This engine is the thin, explicit
layer on top that the vision demands: it turns that set into first-class
**Requirement** records, and it does the one thing scoping deliberately does not —
it assigns every line exactly one of the four classifications, which are NEVER
blended:

    osha_required        — a federal OSHA regulation (29 CFR) is the hook
    origin_recommendation — Origin's own guidance where no regulation compels it
    best_practice        — a consensus standard (NFPA / ANSI / NEC / ASME / …)
    customer_requirement  — a hiring client / prequal platform (ISN, Avetta, …)

Every requirement carries: the classification, the applicability reason (why it
applies to this company specifically), the citation, and the jurisdiction
(federal vs. state-plan). That is the "no checklist hand-waving" bar from the
roadmap: given a company profile, Origin produces its tailored requirement set
with a citation and an applicability reason for every line.

Design rules (identical to the rest of the SIE):
  * DERIVED, not a new source of truth. Requirements are resolved on demand from
    the company profile via scoping; nothing new is persisted. If the profile
    changes, the requirements change — there is no second copy to drift.
  * Deterministic, offline, no LLM. Same profile in → same requirements out.
  * Isolated + non-fatal registration, so a bug here can't break the live app.

The spine (Stage 1) consumes `requirements_from_profile()` to materialize
Requirement nodes and `company --has_requirement--> requirement --cited_by-->
source` edges, so a requirement and the evidence that satisfies it meet at the
shared source-standard node. `gaps_for()` reads that same spine to answer "which
of these requirements has no evidence on file yet."
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from . import scoping


# ── the four-way classification (never blended) ──────────────────────────────
CLASS_OSHA = "osha_required"
CLASS_ORIGIN = "origin_recommendation"
CLASS_BEST = "best_practice"
CLASS_CUSTOMER = "customer_requirement"

_CLASS_LABEL = {
    CLASS_OSHA: "OSHA-required",
    CLASS_ORIGIN: "Origin recommendation",
    CLASS_BEST: "Best practice",
    CLASS_CUSTOMER: "Customer requirement",
}

# Ranking priority — the regulatory hook comes first, then customer mandates,
# then Origin's guidance, then consensus best practice. Within a class, baseline
# standards precede activity-triggered ones, then alphabetical by category.
_CLASS_RANK = {CLASS_OSHA: 0, CLASS_CUSTOMER: 1, CLASS_ORIGIN: 2, CLASS_BEST: 3}
_BASIS_RANK = {"baseline": 0, "triggered": 1, "customer": 2}

# Consensus / industry standards bodies. A citation that names one of these but
# no 29 CFR regulation is a best practice, not a legal requirement.
_CONSENSUS = ("NFPA", "ANSI", "NEC", "ASME", "ASTM", "NIOSH", "IEEE", "API ", "UL ")


def classify(citation: str) -> Tuple[str, str]:
    """Assign exactly one of the four classifications from a citation string.

    The federal regulation governs when present: a line citing both 29 CFR and a
    consensus standard (e.g. '29 CFR 1910.331-335 / NFPA 70E') is OSHA-required,
    because the CFR is the enforceable hook. Only when there is no CFR citation
    does the consensus body decide best_practice; absent both, it is Origin's own
    recommendation.
    """
    c = (citation or "").upper()
    if "29 CFR" in c or "29CFR" in c or "CFR" in c:
        return CLASS_OSHA, _CLASS_LABEL[CLASS_OSHA]
    if any(tok in c for tok in _CONSENSUS):
        return CLASS_BEST, _CLASS_LABEL[CLASS_BEST]
    return CLASS_ORIGIN, _CLASS_LABEL[CLASS_ORIGIN]


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or "x"


def _rank_key(r: Dict[str, Any]) -> Tuple[int, int, str]:
    return (
        _CLASS_RANK.get(r.get("classification", ""), 9),
        _BASIS_RANK.get(r.get("basis", ""), 9),
        (r.get("category") or r.get("title") or "").lower(),
    )


def requirements_from_profile(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the full, classified requirement set for one company profile.

    `rec` is a company_profile record (company / industry / naics / state /
    headcount / activities / operators / customer_platforms). Returns the ranked
    requirement list plus per-class counts and the jurisdiction. Pure function of
    the profile — no persistence, no model.
    """
    cid = (rec.get("company_id") or _slug(rec.get("company", ""))).strip()
    sc = scoping.scope_company({
        "company": rec.get("company", ""),
        "industry": rec.get("industry", ""),
        "naics": rec.get("naics", ""),
        "state": rec.get("state", ""),
        "headcount": rec.get("headcount"),
        "activities": rec.get("activities", {}),
        "operators": rec.get("operators", []),
    })

    jurisdiction = sc.get("jurisdiction")
    reqs: List[Dict[str, Any]] = []

    # 1) Regulatory / recommended standards, straight from scoping.
    for s in sc.get("required_standards", []):
        cls, label = classify(s.get("citation", ""))
        reqs.append({
            "requirement_id": f"{cid}::{s['id']}",
            "company_id": cid,
            "standard_id": s["id"],
            "title": s.get("title", s["id"]),
            "citation": s.get("citation", ""),
            "category": s.get("category", ""),
            "written_program": s.get("written_program", ""),
            "classification": cls,
            "classification_label": label,
            "basis": s.get("source", "baseline"),   # baseline | triggered
            "why": s.get("why", ""),
            "jurisdiction": jurisdiction,
        })

    # 2) Customer requirements — one per hiring client / prequal platform. These
    #    are obligations imposed by who the company works for (ISN, Avetta, PEC,
    #    Veriforce, an operator), not by a regulation. No CFR citation by nature.
    seen_cust: Set[str] = set()
    customers = list(sc.get("operators") or []) + list(rec.get("customer_platforms") or [])
    for op in customers:
        name = (op or "").strip() if isinstance(op, str) else ""
        if not name:
            continue
        key = _slug(name)
        if key in seen_cust:
            continue
        seen_cust.add(key)
        reqs.append({
            "requirement_id": f"{cid}::cust:{key}",
            "company_id": cid,
            "standard_id": None,
            "title": f"{name} — contractor prequalification requirements",
            "citation": "",
            "category": "Customer requirement",
            "written_program": "",
            "classification": CLASS_CUSTOMER,
            "classification_label": _CLASS_LABEL[CLASS_CUSTOMER],
            "basis": "customer",
            "why": f"Required by hiring client / prequalification platform: {name}",
            "jurisdiction": jurisdiction,
        })

    reqs.sort(key=_rank_key)

    counts: Dict[str, int] = {c: 0 for c in _CLASS_LABEL}
    for r in reqs:
        counts[r["classification"]] = counts.get(r["classification"], 0) + 1

    return {
        "company_id": cid,
        "company": rec.get("company", ""),
        "industry": sc.get("industry", ""),
        "state": sc.get("state"),
        "sector": sc.get("sector"),
        "sector_label": sc.get("sector_label"),
        "jurisdiction": jurisdiction,
        "recordkeeping": sc.get("recordkeeping"),
        "requirements": reqs,
        "required_count": len(reqs),
        "counts": counts,
    }


def requirements_for(company_id: str) -> Optional[Dict[str, Any]]:
    """Resolve the classified requirement set for a stored company by id."""
    from . import company_profile as cp
    rec = cp.get(company_id)
    if not rec:
        return None
    return requirements_from_profile(rec)


# ── CFR-token matching (requirement citation ⇄ evidence source standard) ──────
_CFR_TOKEN = re.compile(r"\b(\d{3,4}(?:\.\d+)?(?:-\d+)?)\b")


def _cfr_tokens(citation: str) -> Set[str]:
    """Extract the CFR part numbers from a citation string so a requirement's
    compound citation ('29 CFR 1910.1053 / 1926.1153') and a finding's single
    source ('29 CFR 1926.1153') can be matched on shared standards."""
    out: Set[str] = set()
    for m in _CFR_TOKEN.findall(citation or ""):
        # ignore the bare '29'/'1926'-less title words; keep dotted part numbers
        if "." in m or "-" in m:
            out.add(m)
    return out


def gaps_for(company_id: str) -> Optional[Dict[str, Any]]:
    """Split a company's requirements into those with evidence on file and those
    without. Evidence is read from the derived spine: any source standard the
    company already reaches (via a finding, CAPA, or citation) counts as evidence
    for a requirement that cites the same CFR part. Deterministic, offline.
    """
    data = requirements_for(company_id)
    if data is None:
        return None

    # Gather the CFR part numbers the company already has evidence for, from the
    # spine's per-company chain (finding→violates→source, capa→cited_by→source,
    # citation→cited_by→source). Deferred import avoids a load-time cycle.
    evidence: Set[str] = set()
    try:
        from . import spine
        chain = spine.chain_for_company(company_id)
        for n in chain.get("nodes", []):
            if n.get("type") == "source":
                evidence |= _cfr_tokens(n.get("label", ""))
    except Exception:
        evidence = set()

    covered: List[Dict[str, Any]] = []
    gaps: List[Dict[str, Any]] = []
    for r in data["requirements"]:
        toks = _cfr_tokens(r.get("citation", ""))
        r2 = dict(r)
        if toks and (toks & evidence):
            r2["evidence"] = "on_file"
            covered.append(r2)
        else:
            # No matching evidence. Customer requirements (no citation) and any
            # regulated standard with nothing uploaded/audited land here.
            r2["evidence"] = "none"
            gaps.append(r2)

    return {
        "company_id": data["company_id"],
        "company": data["company"],
        "required_count": data["required_count"],
        "covered_count": len(covered),
        "gap_count": len(gaps),
        "counts": data["counts"],
        "covered": covered,
        "gaps": gaps,
    }


# ── routes (gated by _auth under /api/*, isolated + non-fatal) ────────────────
def register_requirements(app) -> None:
    """Attach Applicable Requirements routes. Mirrors every other SIE module:
    isolated, non-fatal, fully offline. Requirements are derived from the company
    profile on every call — never a stored second source of truth."""
    from fastapi.responses import JSONResponse

    @app.get("/api/requirements/{company_id}")
    def requirements_get(company_id: str):
        try:
            data = requirements_for(company_id)
            if data is None:
                return JSONResponse({"error": "company not found"}, status_code=404)
            return {"ok": True, **data}
        except Exception as exc:  # never 500 the tool
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/requirements/{company_id}/gaps")
    def requirements_gaps(company_id: str):
        try:
            data = gaps_for(company_id)
            if data is None:
                return JSONResponse({"error": "company not found"}, status_code=404)
            return {"ok": True, **data}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)
