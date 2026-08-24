"""seed_dashboard.py — load demo contractors onto the dashboard.

Chris has no live clients yet, so this seeds four realistic sample contractors
(matching the mockup he designed) so the board shows a full green/yellow/red
mix before real client documents exist. Every seeded row is tagged
``sample: true`` and can be deleted like any other; re-seeding replaces them.

The reports here are hand-built (not produced by find_gaps) but use REAL
compliance-KB standards for the drill-down breakdown, so a sample contractor
looks and reads exactly like an analyzed one.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import compliance_kb as kb
from . import contractors as C


def _gap(std_id: str, status: str, flagged: bool = False,
         reason: str = "") -> Optional[dict]:
    """Build one gap dict from a real KB record. Returns None if the id is gone
    (KB can change), so a seed never crashes on a renamed standard."""
    rec = kb.get(std_id)
    if not rec:
        # fall back to a keyword search so the sample still populates
        hits = kb.search(std_id, limit=1)
        rec = hits[0] if hits else None
    if not rec:
        return None
    wp = (rec.get("written_program", "") or "").strip().lower()
    needs_prog = wp in ("yes", "conditional")
    return {
        "id": rec["id"],
        "title": rec.get("title", ""),
        "citation": rec.get("citation", ""),
        "category": rec.get("category", ""),
        "written_program": rec.get("written_program", ""),
        "needs_program": needs_prog,
        "status": status,
        "coverage_ratio": 0.0 if status == "MISSING" else 0.45,
        "elements_total": len(rec.get("required_elements", [])),
        "elements_covered": 0,
        "missing_elements": list(rec.get("required_elements", []))[:4],
        "failure_points": rec.get("failure_points", [])[:3],
        "platform_flagged": flagged,
        "platform_reason": reason,
    }


def _report(pct: int, programs_missing: int, gaps: List[dict],
            industry: str, state: str,
            deficiency: Optional[dict] = None) -> dict:
    gaps = [g for g in gaps if g]
    return {
        "meta": {"industry": industry, "state": state, "sector_label": industry},
        "documents": [],
        "platforms_detected": (["ISNetworld"] if deficiency else []),
        "deficiency_report": deficiency or {"detected": False, "sources": [],
                                            "flagged_total": 0, "matched": [], "unmatched": []},
        "summary": {
            "readiness_pct": pct,
            "programs_missing": programs_missing,
            "programs_total": 45,
            "programs_present": max(0, 45 - programs_missing),
            "programs_failing": 0,
            "references_open": 0,
            "headline": f"Sample contractor · {pct}% program readiness.",
        },
        "gaps": gaps,
        "also_covered": [],
    }


# ── the four sample contractors (match Chris's mockup) ───────────────────────
def _samples() -> List[Dict[str, Any]]:
    G, Y, R = "green", "yellow", "red"
    return [
        {
            "name": "ABC Mechanical", "industry": "mechanical contractor",
            "state": "TX", "operators": ["Phillips 66"],
            "pct": 94, "programs_missing": 0,
            "dots": {"insurance": G, "coi": G, "workers_comp": G, "emr": G,
                     "trir": G, "osha": G, "safety_program": G, "isn": G,
                     "training": G, "owner_requirements": G},
            "gaps": [], "deficiency": None,
        },
        {
            "name": "Gulf Industrial", "industry": "industrial services",
            "state": "TX", "operators": ["ExxonMobil", "Chevron"],
            "pct": 97, "programs_missing": 0,
            "dots": {"insurance": G, "coi": G, "workers_comp": G, "emr": G,
                     "trir": G, "osha": G, "safety_program": G, "isn": G,
                     "training": G, "owner_requirements": G},
            "gaps": [], "deficiency": None,
        },
        {
            "name": "Smith Electric", "industry": "electrical contractor",
            "state": "TX", "operators": ["Oncor"],
            "pct": 81, "programs_missing": 0,
            "dots": {"insurance": G, "coi": G, "workers_comp": G, "emr": Y,
                     "trir": Y, "osha": G, "safety_program": G, "isn": G,
                     "training": G, "owner_requirements": R},
            "gaps": [
                ("dropped-objects-prevention-program", "MISSING", False, ""),
                ("hand-finger-safety-program", "FAILING", False, ""),
            ],
            "deficiency": None,
        },
        {
            "name": "Texas Pipeline", "industry": "oil and gas",
            "state": "TX", "operators": ["ExxonMobil", "Kinder Morgan", "Energy Transfer"],
            "pct": 68, "programs_missing": 4,
            "dots": {"insurance": Y, "coi": Y, "workers_comp": G, "emr": R,
                     "trir": R, "osha": Y, "safety_program": R, "isn": R,
                     "training": Y, "owner_requirements": R},
            "gaps": [
                ("29-cfr-1910-134-respiratory-protection-program", "FAILING", True,
                 "Respiratory Protection ... Rejected — missing fit-test records"),
                ("29-cfr-1910-147-control-of-hazardous-energy-lockout-tagout", "MISSING", True,
                 "Lockout/Tagout ... Not Submitted"),
                ("acord-25-certificate-of-liability-insurance-certificate-of-insurance-coi-acord-25",
                 "FAILING", False, ""),
                ("dropped-objects-prevention-program", "MISSING", False, ""),
            ],
            "deficiency": {
                "detected": True, "sources": ["isn_ravs_review.pdf"],
                "flagged_total": 3,
                "matched": [
                    {"id": "29-cfr-1910-134-respiratory-protection-program",
                     "title": "Respiratory Protection Program", "citation": "29 CFR 1910.134",
                     "status": "rejected", "raw": "Respiratory Protection ... Rejected", "source": "isn_ravs_review.pdf"},
                    {"id": "29-cfr-1910-147-control-of-hazardous-energy-lockout-tagout",
                     "title": "Control of Hazardous Energy (Lockout/Tagout)", "citation": "29 CFR 1910.147",
                     "status": "not submitted", "raw": "Lockout/Tagout ... Not Submitted", "source": "isn_ravs_review.pdf"},
                ],
                "unmatched": [
                    {"raw": "Operator orientation — Not on file", "status": "not on file",
                     "topic": "Operator orientation", "source": "isn_ravs_review.pdf"},
                ],
            },
        },
    ]


def seed_samples(force: bool = True) -> List[str]:
    """Create the sample contractors. When force is False and the board already
    has contractors, do nothing. Returns the slugs written."""
    if not force and C.list_contractors():
        return []
    written: List[str] = []
    for s in _samples():
        gaps = [_gap(*g) for g in s["gaps"]]
        report = _report(s["pct"], s["programs_missing"], gaps,
                         s["industry"], s["state"], s.get("deficiency"))
        slug = C.save_snapshot(s["name"], report, industry=s["industry"],
                               state=s["state"], operators=s["operators"])
        # tag as a sample so it's easy to spot / bulk-remove later
        meta_path = C._dir(slug) / "contractor.json"
        try:
            import json as _json
            m = _json.loads(meta_path.read_text())
            m["sample"] = True
            meta_path.write_text(_json.dumps(m, indent=2))
        except Exception:
            pass
        for dim, val in s["dots"].items():
            C.set_status(slug, dim, val)
        written.append(slug)
    return written
