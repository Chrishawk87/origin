"""Versioned Regulatory Knowledge Store — the Safety Intelligence Engine's
source of truth (Phase 1).

This is the layer that makes every regulatory answer *traceable* and *versioned*.
It does NOT replace the compliance knowledge base (compliance_kb.py); it wraps it:

  * The in-code compliance_kb corpus (OSHA 1910/1926 structural index, the
    verbatim CFR text, the written-program corpus, and the OSHA 2254 training
    package) is treated as knowledge **version 1 — "kb-base"**. It ships with the
    deploy and is always available, even fully offline.
  * Newer or corrected regulatory records (e.g. a standard revised after our base
    corpus was built, or an Origin-authored clarification) are written as
    *versioned override records* onto the persistent /data volume. An override
    supersedes the base record for the same regulation number and carries its own
    effective date, jurisdiction, and confidence.

Every record this module returns is a **sourced record**: it names where the fact
came from (source, source URL), which knowledge version it belongs to, its
effective date and jurisdiction, whether it has been superseded, and a retrieval
confidence. That envelope is what lets the Citation Engine say exactly *why* a
conclusion holds — and lets it refuse to answer ("Unable to verify this
requirement from the current knowledge base") when nothing resolves.

Design rules (identical philosophy to compliance_kb.py / abatement.py):
  * Deterministic. No LLM anywhere in this module. Same citation in → same
    sourced record out.
  * File-based on the persistent volume (ORIGIN_DATA_DIR/knowledge). No database
    dependency, so this can never break the live chat app, portal, or dashboard,
    and it deploys the same proven way everything else in Origin does.
  * Never fabricates. If a citation resolves in neither the volume overrides nor
    the base corpus, resolve() returns None — honest absence, not a guess.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import compliance_kb as kb

try:  # normal app runtime
    from .paths import DATA_DIR
except ImportError:  # bare import in ad-hoc scripts
    import os as _os
    DATA_DIR = Path(_os.environ.get("ORIGIN_DATA_DIR") or (Path.home() / ".origin"))

KNOWLEDGE_DIR = DATA_DIR / "knowledge"
VERSIONS_PATH = KNOWLEDGE_DIR / "versions.jsonl"      # every knowledge version, append-only
OVERRIDES_PATH = KNOWLEDGE_DIR / "regulations.jsonl"  # versioned override records

# The base corpus that ships in-code. This is always version 1 and always present.
BASE_VERSION_LABEL = "kb-base"
BASE_SOURCE_ID = "osha-cfr-base"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_dir() -> None:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)


# ── section-number extraction ────────────────────────────────────────────────
_SECTION_RE = re.compile(r"\b(\d{3,4}\.\d+[A-Za-z]?)")


def _section_of(citation: str) -> str:
    """Pull the bare section number ('1926.501') out of any citation string."""
    m = _SECTION_RE.search(citation or "")
    return m.group(1) if m else (citation or "").strip()


def _jurisdiction_for(citation: str) -> str:
    c = (citation or "")
    if "29 CFR" in c or re.search(r"\b19\d\d\.", c):
        return "Federal OSHA (29 CFR)"
    if "49 CFR" in c or re.search(r"\b39\d\.", c):
        return "Federal FMCSA (49 CFR)"
    return "Federal"


# ── version ledger ───────────────────────────────────────────────────────────
def ensure_base() -> None:
    """Idempotently record the base knowledge version. Safe to call on each boot.

    Writes a single 'kb-base' version row the first time. The base corpus itself
    lives in-code (compliance_kb); this row just registers it in the ledger so the
    version history is complete and every base-resolved record can point at it.
    """
    _ensure_dir()
    if VERSIONS_PATH.exists():
        for line in VERSIONS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and json.loads(line).get("version_label") == BASE_VERSION_LABEL:
                return  # already registered
    try:
        stats = kb.osha_index_stats()
    except Exception:
        stats = {}
    row = {
        "source_id": BASE_SOURCE_ID,
        "source_type": "regulation",
        "publisher": "OSHA",
        "version_label": BASE_VERSION_LABEL,
        "effective_date": "",           # base corpus is a point-in-time snapshot
        "jurisdiction": "Federal OSHA (29 CFR)",
        "superseded_by": None,
        "ingested_at": _now(),
        "ingested_by": "system",
        "is_current": True,
        "checksum": "",
        "note": "In-code compliance_kb corpus (structural index + verbatim CFR "
                "text + written-program corpus + OSHA 2254 training package).",
        "stats": stats,
    }
    with VERSIONS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def list_versions() -> List[Dict[str, Any]]:
    if not VERSIONS_PATH.exists():
        ensure_base()
    out: List[Dict[str, Any]] = []
    if VERSIONS_PATH.exists():
        for line in VERSIONS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _load_overrides() -> List[Dict[str, Any]]:
    if not OVERRIDES_PATH.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in OVERRIDES_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# ── ingest (add a newer/corrected versioned record) ──────────────────────────
def ingest(
    *,
    regulation_number: str,
    title: str,
    body_text: str,
    source: str,
    source_url: str = "",
    effective_date: str = "",
    jurisdiction: str = "",
    industry_scope: Optional[List[str]] = None,
    hazard_category: Optional[List[str]] = None,
    version_label: str = "",
    ingested_by: str = "owner",
    confidence: float = 0.95,
) -> Dict[str, Any]:
    """Add a newer, versioned regulatory record on the volume.

    If a live override already exists for the same regulation number it is marked
    superseded and the new record becomes current. The base corpus is never
    modified — an override simply wins over it at resolve() time. Returns the
    stored record.
    """
    _ensure_dir()
    section = _section_of(regulation_number)
    label = version_label or ("v-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))
    checksum = hashlib.sha256((regulation_number + body_text).encode("utf-8")).hexdigest()[:16]

    # Supersede any existing current override for this section.
    existing = _load_overrides()
    changed = False
    for rec in existing:
        if rec.get("section") == section and rec.get("is_current"):
            rec["is_current"] = False
            rec["superseded_by"] = label
            changed = True
    record = {
        "regulation_number": regulation_number.strip(),
        "section": section,
        "title": title.strip(),
        "body_text": body_text.strip(),
        "source": source.strip(),
        "source_url": source_url.strip(),
        "effective_date": effective_date.strip(),
        "jurisdiction": (jurisdiction or _jurisdiction_for(regulation_number)).strip(),
        "industry_scope": industry_scope or [],
        "hazard_category": hazard_category or [],
        "version_label": label,
        "is_current": True,
        "superseded_by": None,
        "ingested_at": _now(),
        "ingested_by": ingested_by,
        "confidence": float(confidence),
        "checksum": checksum,
    }
    existing.append(record)
    if changed or True:
        with OVERRIDES_PATH.open("w", encoding="utf-8") as f:
            for rec in existing:
                f.write(json.dumps(rec) + "\n")
    # Register the version in the ledger.
    with VERSIONS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "source_id": source or "override",
            "source_type": "regulation",
            "publisher": source or "override",
            "version_label": label,
            "effective_date": effective_date,
            "jurisdiction": record["jurisdiction"],
            "superseded_by": None,
            "ingested_at": record["ingested_at"],
            "ingested_by": ingested_by,
            "is_current": True,
            "checksum": checksum,
            "regulation_number": regulation_number,
        }) + "\n")
    return record


# ── resolve (the retrieval contract) ─────────────────────────────────────────
def _from_override(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "regulation_number": rec.get("regulation_number", ""),
        "section": rec.get("section", ""),
        "title": rec.get("title", ""),
        "body_text": rec.get("body_text", ""),
        "source": rec.get("source", ""),
        "source_url": rec.get("source_url", ""),
        "effective_date": rec.get("effective_date", ""),
        "jurisdiction": rec.get("jurisdiction", ""),
        "industry_scope": rec.get("industry_scope", []),
        "hazard_category": rec.get("hazard_category", []),
        "version_label": rec.get("version_label", ""),
        "superseded": not rec.get("is_current", True),
        "confidence": float(rec.get("confidence", 0.95)),
        "kind": "override",
        "required_elements": "",
        "training_text": "",
        "recordkeeping": "",
    }


def resolve(citation: str) -> Optional[Dict[str, Any]]:
    """Resolve a citation to a single sourced record, or None if unverifiable.

    Order of precedence:
      1. A current volume override for the section (newest ingested truth).
      2. Base corpus verbatim CFR text  (highest base confidence — actual law text).
      3. Base written-program record     (required elements + recordkeeping).
      4. Base OSHA structural index      (official title + URL only).
    Returns None if the citation exists in none of these — the caller must then
    emit "Unable to verify this requirement from the current knowledge base."
    """
    q = (citation or "").strip()
    if not q:
        return None
    section = _section_of(q)

    # 1) volume override (current wins)
    for rec in _load_overrides():
        if rec.get("section") == section and rec.get("is_current"):
            return _from_override(rec)

    ensure_base()
    jurisdiction = _jurisdiction_for(q)

    # 2) verbatim CFR text
    vb = kb.verbatim_text(q)
    # 3) written-program corpus
    prog = kb.by_citation("29 CFR " + section) or kb.by_citation(q)
    # 4) structural index
    idx = kb.osha_section(q)

    if not (vb or prog or idx):
        return None

    title = (vb or {}).get("title") or (prog or {}).get("title") or (idx or {}).get("title") or ""
    url = (vb or {}).get("url") or (idx or {}).get("url") or (prog or {}).get("source") or ""
    body = (vb or {}).get("text") or ""
    # confidence: actual law text > program record > index-only
    if vb:
        confidence = 0.95
    elif prog:
        confidence = 0.9
    else:
        confidence = 0.8

    return {
        "regulation_number": (vb or idx or {}).get("citation") or ("29 CFR " + section),
        "section": section,
        "title": title,
        "body_text": body,
        "source": (vb or {}).get("source") or "OSHA",
        "source_url": url,
        "effective_date": "",
        "jurisdiction": jurisdiction,
        "industry_scope": [(idx or {}).get("part_name", "")] if idx else [],
        "hazard_category": [(vb or {}).get("hazard", "")] if vb else [],
        "version_label": BASE_VERSION_LABEL,
        "superseded": False,
        "confidence": confidence,
        "kind": "verbatim" if vb else ("program" if prog else "index"),
        "required_elements": (prog or {}).get("required_elements", ""),
        "training_text": "",
        "recordkeeping": (prog or {}).get("recordkeeping", ""),
    }


def status() -> Dict[str, Any]:
    """Inventory of the knowledge store: version count + override count."""
    versions = list_versions()
    overrides = _load_overrides()
    return {
        "versions": len(versions),
        "base_version": BASE_VERSION_LABEL,
        "overrides_total": len(overrides),
        "overrides_current": sum(1 for r in overrides if r.get("is_current")),
        "kb_stats": (versions[0].get("stats") if versions else {}) or {},
    }
