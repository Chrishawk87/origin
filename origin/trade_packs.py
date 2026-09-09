"""Trade packs — one-click compliance kits per trade/sector.

A "trade pack" is the full compliance kit a general contractor needs to stand up
a subcontractor (or its own vault) for a given trade: the written programs, the
job-hazard analyses (JSAs/JHAs), the required-training list, and a prequal
document checklist — all drawn from the EXISTING knowledge base, never authored
fresh here. Packs are keyed to the sectors already defined in ``sector_content``
so they stay perfectly in sync with the sector-specific content the app already
builds; there is no separate trade list to maintain.

House rules (identical to the rest of Origin):
  * Deterministic + fully offline. No external LLM, no network.
  * Never-fabricate. Every item in a pack traces to a real KB master or a real
    OSHA 2254 training record. If a sector has no authored content, its pack is
    honestly empty rather than invented.
  * File-based / read-only here. This module only READS the asset-library index
    and the KB; it never writes. Persistence (dropping a pack onto a sub or into
    the GC's vault) is owned by the portal route, mirroring how injury_risk /
    prequal_loop / compliance_health split engine from persistence.
  * Isolated. Imports are lazy and defensive so a missing dependency degrades to
    an empty pack instead of crashing the caller.

Public surface:
    pack_keys()            -> ["11", "23", "31-33", ...]
    list_packs()           -> [{key, label, program_count, jha_count,
                                training_count, item_count}]
    manifest(sector_key)   -> {ok, key, label, programs[], jsas[], training[],
                                checklist[], counts, generated_at}
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


# ── Sector catalog (the trade list = the existing sectors) ───────────────────
def _sector_content():
    try:
        from . import sector_content as _sc
        return _sc
    except Exception:
        return None


def pack_keys() -> List[str]:
    """Every trade-pack key — one per authored sector in sector_content."""
    sc = _sector_content()
    if sc is None:
        return []
    try:
        return list(sc.sector_keys())
    except Exception:
        return []


def _label_for(key: str) -> str:
    sc = _sector_content()
    if sc is not None:
        try:
            lbl = sc.label(key)
            if lbl:
                return str(lbl)
        except Exception:
            pass
    return key


# ── Asset-library masters for a sector (programs + JSAs) ─────────────────────
def _library_index() -> List[Dict[str, Any]]:
    """The live asset-library index, ensuring the library is built first so the
    sector masters exist. Falls back to the raw read, then an empty list."""
    try:
        from . import compliance as _cmp
    except Exception:
        return []
    # Prefer the raw read (no rebuild); if the library hasn't been synced yet,
    # ensure it once and re-read.
    try:
        idx = _cmp._read_index_raw()
    except Exception:
        idx = []
    if not idx:
        try:
            _cmp.ensure_library()
            idx = _cmp._read_index_raw()
        except Exception:
            idx = []
    return idx or []


def _sector_masters(key: str, kind: str) -> List[Dict[str, str]]:
    """Program or JSA masters authored for a sector, as [{mid, title}]."""
    out: List[Dict[str, str]] = []
    seen = set()
    for r in _library_index():
        if r.get("sector") != key or r.get("kind") != kind:
            continue
        mid = r.get("id")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append({"mid": mid, "title": r.get("title", "") or mid})
    out.sort(key=lambda d: d["title"].lower())
    return out


# ── Required-training list for a sector (never-fabricate) ────────────────────
def _sector_training(key: str) -> List[Dict[str, str]]:
    """The OSHA training duties that apply to this sector, kept ONLY when a
    verbatim OSHA 2254 training record backs the citation. Absence = omitted."""
    try:
        from . import compliance_kb as _kb
    except Exception:
        return []
    try:
        scope = _kb.naics_applicable(key)
    except Exception:
        return []
    out: List[Dict[str, str]] = []
    seen = set()
    for s in scope.get("standards", []):
        citation = (s.get("citation") or "").strip()
        if not citation:
            continue
        try:
            req = _kb.training_requirement(citation)
        except Exception:
            req = None
        if not req:
            continue
        cid = citation
        if cid in seen:
            continue
        seen.add(cid)
        out.append({
            "citation": citation,
            "title": s.get("title", "") or citation,
            "requirement": req.get("requirement", "") or req.get("summary", "") or "",
        })
    out.sort(key=lambda d: d["citation"])
    return out


# ── Prequal document checklist (derived from the pack itself) ────────────────
def _checklist(programs: List[Dict[str, str]],
               jsas: List[Dict[str, str]],
               training: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """A prequal document checklist built purely from the pack contents — every
    line traces to a real KB item, nothing is invented. These are the artifacts a
    sub must have on file to clear prequal for this trade."""
    items: List[Dict[str, str]] = []
    for p in programs:
        items.append({"kind": "program", "label": f"Written program: {p['title']}"})
    for j in jsas:
        items.append({"kind": "jsa", "label": f"Job hazard analysis: {j['title']}"})
    for t in training:
        items.append({"kind": "training",
                      "label": f"Training records: {t['title']} ({t['citation']})"})
    return items


# ── Public: manifest + roster ────────────────────────────────────────────────
def manifest(sector_key: str) -> Dict[str, Any]:
    """Full trade-pack manifest for a sector: programs, JSAs, training list, and a
    prequal checklist. Never-fabricate — an unauthored sector yields empty lists,
    not guesses. Safe: any internal failure degrades to an empty, ok=False pack."""
    key = (sector_key or "").strip()
    if not key or key not in pack_keys():
        return {"ok": False, "error": "unknown trade pack",
                "key": key, "label": _label_for(key),
                "programs": [], "jsas": [], "training": [], "checklist": [],
                "counts": {"programs": 0, "jsas": 0, "training": 0, "items": 0},
                "generated_at": _now()}
    try:
        programs = _sector_masters(key, "program")
        jsas = _sector_masters(key, "jha")
        training = _sector_training(key)
        checklist = _checklist(programs, jsas, training)
        counts = {
            "programs": len(programs),
            "jsas": len(jsas),
            "training": len(training),
            "items": len(programs) + len(jsas),
        }
        return {"ok": True, "key": key, "label": _label_for(key),
                "programs": programs, "jsas": jsas, "training": training,
                "checklist": checklist, "counts": counts,
                "generated_at": _now()}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "error": str(exc), "key": key,
                "label": _label_for(key),
                "programs": [], "jsas": [], "training": [], "checklist": [],
                "counts": {"programs": 0, "jsas": 0, "training": 0, "items": 0},
                "generated_at": _now()}


def list_packs() -> List[Dict[str, Any]]:
    """A lightweight catalog of every trade pack for the picker UI — label plus
    program / JSA / training counts, no document bodies."""
    out: List[Dict[str, Any]] = []
    for key in pack_keys():
        try:
            m = manifest(key)
            c = m.get("counts", {})
            out.append({
                "key": key,
                "label": m.get("label", key),
                "program_count": c.get("programs", 0),
                "jha_count": c.get("jsas", 0),
                "training_count": c.get("training", 0),
                "item_count": c.get("items", 0),
            })
        except Exception:
            out.append({"key": key, "label": _label_for(key),
                        "program_count": 0, "jha_count": 0,
                        "training_count": 0, "item_count": 0})
    out.sort(key=lambda d: str(d["label"]).lower())
    return out


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
