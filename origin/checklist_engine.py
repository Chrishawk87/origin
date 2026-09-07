"""
checklist_engine.py — the Universal Regulatory Router's field layer.

The thesis of the multi-brain platform: the *text of the law* should generate the
mobile checklist and field requirements automatically. When the law changes, the
knowledge store updates and the checklist changes — with no developer writing a
line of code. This module is that machinery, built the Origin way:

  * Source of truth is the file-based versioned ``knowledge_store`` (same store
    the OSHA database uses) — never a SQL table.
  * Parsing is deterministic and offline — no external model. An imperative in
    the regulatory text ("shall", "must", "minimum of X feet", "before each
    shift") is turned into a structured field requirement by rule, not by guess.
  * It is source-agnostic. The same parser runs on OSHA 29 CFR verbatim text,
    on an eCFR API pull, or (as seeded here) on the USACE EM 385-1-1 manual.
    Only the *ingest adapter* differs per agency; this layer does not care.

The first reference "brain" is USACE EM 385-1-1 Section 25 (Excavation). Selecting
the activity "Excavation > 5 ft" pulls the seeded controls and generates a
multi-row Activity Hazard Analysis (AHA) matrix.

Everything here is isolated and non-fatal: if the store is empty or a citation
resolves to nothing, callers get an explicit "unverifiable" answer rather than a
fabricated requirement.
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:  # package import (normal runtime + self-test)
    from . import knowledge_store
except ImportError:  # bare-script fallback
    import knowledge_store  # type: ignore


# ── the USACE EM 385-1-1 excavation seed ─────────────────────────────────────
# Faithful, representative text of the excavation requirements in EM 385-1-1
# Section 25 (which itself aligns with 29 CFR 1926 Subpart P). This is a SEED so
# the machinery has real text to parse; before production the verbatim manual
# text should replace it (fetched from the official EM 385-1-1 PDF the same way
# the OSHA verbatim corpus was built). Each sentence is a single imperative so
# the parser maps it cleanly to one field requirement.
EM385_CITATION = "EM 385-1-1 Section 25"
EM385_TITLE = "USACE EM 385-1-1 — Excavation and Trenching"
EM385_SOURCE = "USACE EM 385-1-1"
EM385_BODY = (
    "Excavations 5 feet or more in depth shall be protected from cave-in by an "
    "adequate protective system, such as sloping, shoring, or a trench shield, "
    "unless the excavation is made entirely in stable rock. "
    "Excavations less than 5 feet in depth shall be protected when a competent "
    "person finds indications of a potential cave-in. "
    "Spoil piles, excavated material, and equipment shall be kept a minimum of 2 "
    "feet from the edge of the excavation. "
    "A competent person shall inspect the excavation daily before the start of "
    "each shift, after every rainstorm, and after any occurrence that increases "
    "hazard. "
    "A stairway, ladder, ramp, or other safe means of egress shall be provided in "
    "trench excavations 4 feet or more in depth and shall be located within 25 "
    "feet of workers. "
    "The atmosphere in excavations greater than 4 feet in depth shall be tested "
    "before entry where an oxygen-deficient or hazardous atmosphere could "
    "reasonably be expected to exist. "
    "Employees shall wear high-visibility warning vests when exposed to public "
    "vehicular traffic. "
    "Underground installations shall be located and marked before excavation "
    "begins."
)
EM385_HAZARDS = ["excavation", "cave-in", "struck-by", "atmospheric", "egress"]
EM385_INDUSTRY = ["construction", "federal-construction", "usace"]


def seed_em385_excavation(force: bool = False) -> Dict[str, Any]:
    """Idempotently place the EM 385 excavation record in the knowledge store.

    Returns the resolved record. Safe to call on every boot: if a current
    override already exists for this section we leave it alone (unless force).
    """
    if not force:
        existing = knowledge_store.resolve(EM385_CITATION)
        if existing and existing.get("body_text"):
            return existing
    return knowledge_store.ingest(
        regulation_number=EM385_CITATION,
        title=EM385_TITLE,
        body_text=EM385_BODY,
        source=EM385_SOURCE,
        source_url="https://www.publications.usace.army.mil/",
        jurisdiction="Federal (USACE EM 385-1-1)",
        industry_scope=EM385_INDUSTRY,
        hazard_category=EM385_HAZARDS,
        version_label="em385-25-seed",
        ingested_by="system-seed",
        confidence=0.9,
    )


# ── the deterministic imperative parser ──────────────────────────────────────
_IMPERATIVE_RE = re.compile(r"\b(shall|must|is required to|are required to|required)\b", re.I)

# numeric threshold: "5 feet", "2 ft", "18 inches", "1.5 m"
_MEASURE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(feet|foot|ft|inches|inch|in|meters|meter|m)\b",
    re.I,
)
_UNIT_CANON = {
    "feet": "ft", "foot": "ft", "ft": "ft",
    "inches": "in", "inch": "in", "in": "in",
    "meters": "m", "meter": "m", "m": "m",
}

# comparator cues around a measurement
_GTE_CUES = ("or more", "or greater", "or deeper", "greater than", "at least",
             "minimum of", "minimum", "deeper than", "exceeding")
_LTE_CUES = ("or less", "less than", "no more than", "not to exceed",
             "maximum of", "maximum", "up to")
_WITHIN_CUES = ("within",)

# recurring-inspection cadence cues
_CADENCE_PATTERNS = [
    (re.compile(r"before the start of each shift|before each shift|each shift", re.I), "per_shift"),
    (re.compile(r"\bdaily\b|every day", re.I), "daily"),
    (re.compile(r"after (?:every )?rain(?:storm|fall)?", re.I), "after_rain"),
    (re.compile(r"annually|each year|every year", re.I), "annual"),
    (re.compile(r"every (\d+)\s*(day|days|month|months|hour|hours|week|weeks)", re.I), "every_n"),
]

# hazard tag by keyword — used to group AHA rows
_HAZARD_TAGS = [
    (("protective system", "cave-in", "cave in", "sloping", "shoring", "shield", "stable rock"), "Cave-in / soil collapse"),
    (("egress", "ladder", "stairway", "ramp", "means of egress"), "Entrapment / egress"),
    (("atmosphere", "oxygen", "hazardous atmosphere", "tested before entry"), "Hazardous atmosphere"),
    (("spoil", "edge of the excavation", "excavated material", "falling"), "Struck-by / falling material"),
    (("vehicular traffic", "warning vest", "high-visibility", "traffic"), "Struck-by / traffic"),
    (("underground installation", "utilities", "located and marked"), "Underground utilities"),
]


def _split_sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.;])\s+", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def _comparator_for(sentence: str, span_start: int) -> str:
    window = sentence[max(0, span_start - 40): span_start + 30].lower()
    if any(c in window for c in _WITHIN_CUES):
        return "within"
    if any(c in window for c in _GTE_CUES):
        return "gte"
    if any(c in window for c in _LTE_CUES):
        return "lte"
    return "eq"


def _extract_threshold(sentence: str) -> Optional[Dict[str, Any]]:
    m = _MEASURE_RE.search(sentence)
    if not m:
        return None
    value = float(m.group(1))
    unit = _UNIT_CANON.get(m.group(2).lower(), m.group(2).lower())
    return {
        "value": int(value) if value.is_integer() else value,
        "unit": unit,
        "comparator": _comparator_for(sentence, m.start()),
    }


def _extract_cadence(sentence: str) -> Optional[Dict[str, Any]]:
    for rx, kind in _CADENCE_PATTERNS:
        m = rx.search(sentence)
        if not m:
            continue
        out: Dict[str, Any] = {"kind": kind}
        if kind == "every_n":
            out["interval"] = int(m.group(1))
            out["period"] = m.group(2).lower().rstrip("s")
        return out
    return None


def _hazard_of(sentence: str) -> str:
    low = sentence.lower()
    for cues, tag in _HAZARD_TAGS:
        if any(c in low for c in cues):
            return tag
    return "General"


def _short_label(sentence: str, limit: int = 96) -> str:
    s = re.sub(r"\s+", " ", sentence).strip().rstrip(".;")
    if len(s) <= limit:
        return s
    return s[:limit].rsplit(" ", 1)[0] + "…"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def parse_imperatives(body_text: str, citation: str = "") -> List[Dict[str, Any]]:
    """Turn regulatory prose into structured field requirements — deterministically.

    Every returned field is derived from one imperative sentence containing
    "shall"/"must"/"required". Nothing is invented: the citation attached is the
    source section's own citation, and the requirement text is the clause itself.

    Field types:
      * ``inspection``  — a recurring check (carries a ``cadence``)
      * ``measurement`` — a numeric condition (carries a ``threshold``)
      * ``attestation`` — a yes/no/na confirmation (the default)
    """
    fields: List[Dict[str, Any]] = []
    for idx, sentence in enumerate(_split_sentences(body_text)):
        if not _IMPERATIVE_RE.search(sentence):
            continue
        threshold = _extract_threshold(sentence)
        cadence = _extract_cadence(sentence)
        if cadence:
            ftype, response = "inspection", "date"
        elif threshold:
            ftype, response = "measurement", "number"
        else:
            ftype, response = "attestation", "yes_no_na"
        fields.append({
            "id": (_slug(citation) or "field") + f"-{idx:02d}",
            "type": ftype,
            "label": _short_label(sentence),
            "requirement": re.sub(r"\s+", " ", sentence).strip(),
            "citation": citation,
            "hazard": _hazard_of(sentence),
            "threshold": threshold,
            "cadence": cadence,
            "response": response,
            "required": True,
        })
    return fields


# ── checklist generation (the dynamic form spec) ─────────────────────────────
def checklist_from_regulation(citation: str) -> Dict[str, Any]:
    """Resolve a citation from the knowledge store and render a dynamic form spec.

    The returned spec is what a mobile client renders with zero hardcoded fields.
    If the citation resolves to nothing, ``ok`` is False and ``fields`` is empty —
    the caller must not fabricate.
    """
    rec = knowledge_store.resolve(citation)
    if not rec or not rec.get("body_text"):
        return {
            "ok": False,
            "citation": citation,
            "error": "Unable to verify this citation from the current knowledge base.",
            "fields": [],
        }
    fields = parse_imperatives(rec.get("body_text", ""), rec.get("regulation_number", citation))
    return {
        "ok": True,
        "citation": rec.get("regulation_number", citation),
        "title": rec.get("title", ""),
        "source": rec.get("source", ""),
        "source_url": rec.get("source_url", ""),
        "jurisdiction": rec.get("jurisdiction", ""),
        "version_label": rec.get("version_label", ""),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "field_count": len(fields),
        "fields": fields,
    }


# ── Activity Hazard Analysis (AHA) matrix ────────────────────────────────────
# Activities map to the citation whose controls govern them. As more brains land
# this table grows (or becomes data); today it proves the machinery on excavation.
_ACTIVITY_MAP = {
    "excavation": EM385_CITATION,
    "excavation > 5 ft": EM385_CITATION,
    "trenching": EM385_CITATION,
}


def aha_matrix(activity: str = "excavation") -> Dict[str, Any]:
    """Generate an Activity Hazard Analysis matrix for an activity.

    Rows are DERIVED from the parsed field requirements of the governing citation,
    grouped by hazard — so the controls in the AHA are exactly the controls in the
    live regulation. Change the regulation, and the AHA changes.
    """
    key = (activity or "").strip().lower()
    citation = _ACTIVITY_MAP.get(key)
    if not citation:
        return {
            "ok": False,
            "activity": activity,
            "error": f"No governing citation mapped for activity '{activity}'.",
            "rows": [],
        }
    spec = checklist_from_regulation(citation)
    if not spec.get("ok"):
        return {"ok": False, "activity": activity, "error": spec.get("error"), "rows": []}

    # Group the derived requirements into AHA rows by hazard.
    by_hazard: Dict[str, List[Dict[str, Any]]] = {}
    for f in spec["fields"]:
        by_hazard.setdefault(f["hazard"], []).append(f)

    rows: List[Dict[str, Any]] = []
    for hazard, group in by_hazard.items():
        controls = [g["requirement"] for g in group]
        rows.append({
            "hazard": hazard,
            "controls": controls,
            "control_count": len(controls),
            "citation": citation,
        })
    return {
        "ok": True,
        "activity": activity,
        "title": f"Activity Hazard Analysis — {activity}",
        "citation": citation,
        "source": spec.get("source", ""),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "row_count": len(rows),
        "rows": rows,
    }


# ── status ───────────────────────────────────────────────────────────────────
def status() -> Dict[str, Any]:
    """A quick health read: is the reference brain seeded and parseable?"""
    rec = knowledge_store.resolve(EM385_CITATION)
    seeded = bool(rec and rec.get("body_text"))
    field_count = 0
    if seeded:
        field_count = len(parse_imperatives(rec.get("body_text", ""), EM385_CITATION))
    return {
        "ok": True,
        "reference_brain": "USACE EM 385-1-1 Section 25 (Excavation)",
        "seeded": seeded,
        "field_count": field_count,
        "activities": sorted(set(_ACTIVITY_MAP.keys())),
    }


# ── route registration (isolated, owner-gated) ───────────────────────────────
def register_checklists(app) -> None:
    """Register the dynamic-checklist routes on the app.

    Owner-gated by default (these routes are NOT added to the GC allowlist in
    server.py, so a GC session can't reach them until we deliberately scope
    them). Imports are function-local so the module stays importable without
    FastAPI. This module does NOT use ``from __future__ import annotations``,
    so the ``request: Request`` annotation resolves fine with a local import.
    """
    from fastapi import Body, Request
    from fastapi.responses import JSONResponse

    # Make sure the reference brain exists the moment the routes come up.
    try:
        seed_em385_excavation()
    except Exception:
        pass

    @app.get("/api/checklist/status")
    def checklist_status():
        try:
            return status()
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.post("/api/checklist/seed")
    def checklist_seed(body: dict = Body(default=None)):
        try:
            rec = seed_em385_excavation(force=bool((body or {}).get("force")))
            return {"ok": True, "seeded": bool(rec.get("body_text")),
                    "citation": rec.get("regulation_number", EM385_CITATION)}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.post("/api/checklist/aha")
    def checklist_aha(body: dict = Body(default=None)):
        activity = ((body or {}).get("activity") or "excavation")
        try:
            return aha_matrix(activity)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    # Keep the wildcard citation route LAST so it never shadows the paths above.
    @app.get("/api/checklist/{citation:path}")
    def checklist_get(citation: str):
        try:
            return checklist_from_regulation(citation)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)
