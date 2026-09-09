"""Prequal close-the-loop board — Feature #3.

The Prequal Readiness Engine (prequal_engine.assess_all) already answers "if my
hiring client runs me through ISN / Avetta / Veriforce today, do I pass, and what
do I still owe?" as a per-platform GAP LIST. That's the diagnosis. This module
closes the loop: it tracks every one of those gaps through the four states it
actually moves through on the way to acceptance —

    Needed  ->  Drafted  ->  Submitted  ->  Accepted

and lets the sub (or their GC) drive each item to done. It is a status BOARD, not
an auto-submitter: Origin never logs into a prequal network on anyone's behalf
(that's fragile and against several networks' terms). Instead:

  * Needed    — the gap is open, nothing produced yet.
  * Drafted   — auto-detected: Origin has generated a document for this gap and
                dropped it in the sub's vault (a Fix-it doc tagged with the gap_id,
                or a health-monitor renewal draft). No manual action needed.
  * Submitted — a human marked "I uploaded this to the platform." (manual)
  * Accepted  — a human marked "the platform accepted it." (manual)

House rules, identical to every other SIE / customer module: deterministic, fully
offline (no LLM anywhere), never-fabricate (no gap from the engine -> no board
item), and isolated + non-fatal registration so a bug here can never take down the
live app. This module never persists on its own — it mutates the passed record and
the caller (portal) saves it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Ordered ladder. Higher rank = further along. `needed` is the floor.
STATE_ORDER: List[str] = ["needed", "drafted", "submitted", "accepted"]
STATE_RANK: Dict[str, int] = {s: i for i, s in enumerate(STATE_ORDER)}
# The two states a human sets by hand; the other two are derived.
MANUAL_STATES = ("submitted", "accepted")
# Severity ordering so the board sorts the scariest open items to the top.
_SEV_RANK: Dict[str, int] = {"high": 0, "medium": 1, "info": 2}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Post-consolidation collapse: PEC/ComplyWorks route to Veriforce, BROWZ to
# Avetta. The grader keeps these legacy names as-is, so we collapse here to keep
# ONE board key per real network — a mark on "PEC" then lands on the Veriforce
# board the sub actually sees. Keyed by upper() of the grader's canonical name.
_COLLAPSE: Dict[str, str] = {
    "PEC": "Veriforce",
    "COMPLYWORKS": "Veriforce",
    "BROWZ": "Avetta",
    "ISNETWORLD": "ISN",
}


def _canon(platform: str) -> str:
    """Canonical board key for a platform. Routes through the grader's own
    canonicalizer (so ISN/Avetta/Veriforce stay stable and line up with what
    assess_all emits), then applies the acquisition collapse so legacy aliases
    (PEC, BROWZ, ComplyWorks) share the acquirer's single board. Falls back to the
    trimmed input so a bad import can never crash the board."""
    try:
        from . import compliance_grading as _grading
        canon = _grading.estimate_grade(platform or "ISN", {}).get("platform", "ISN")
    except Exception:
        canon = (platform or "ISN").strip() or "ISN"
    return _COLLAPSE.get(canon.upper(), canon)


# ── auto-detected "Drafted": has Origin produced a doc for this gap? ───────────
def _drafted_gap_ids(rec: Dict[str, Any]) -> set:
    """Gap ids that already have a generated document sitting in the sub's vault.
    Two producers tag their doc rows: the one-click prequal Fix (gap_id set,
    source='origin-draft') and the compliance-health renewal drafter
    (source='origin-health-draft', gap id carried on 'health_item'). Anything the
    sub uploaded by hand has no gap tag and correctly does NOT auto-advance."""
    out: set = set()
    for d in (rec.get("documents") or []):
        if not isinstance(d, dict):
            continue
        gid = d.get("gap_id") or d.get("health_item")
        if gid:
            out.add(str(gid))
    return out


def _stored_state(rec: Dict[str, Any], canon: str, gap_id: str) -> Optional[Dict[str, Any]]:
    loop = rec.get("prequal_loop")
    if isinstance(loop, dict):
        plat = loop.get(canon)
        if isinstance(plat, dict):
            entry = plat.get(gap_id)
            if isinstance(entry, dict):
                return entry
    return None


def _effective_state(auto: str, stored: Optional[Dict[str, Any]]) -> str:
    """The item's state is the furthest-along of (a) what Origin auto-detected and
    (b) what a human recorded. This means a human 'Submitted' never gets pushed
    back to 'Drafted', and an auto 'Drafted' shows even if nobody's touched it."""
    best = auto
    if stored and stored.get("state") in STATE_RANK:
        if STATE_RANK[stored["state"]] > STATE_RANK[best]:
            best = stored["state"]
    return best


def _item_from_gap(rec: Dict[str, Any], canon: str, gap: Dict[str, Any],
                   drafted_ids: set) -> Dict[str, Any]:
    gid = gap.get("id", "")
    has_doc = gid in drafted_ids
    auto = "drafted" if has_doc else "needed"
    stored = _stored_state(rec, canon, gid)
    state = _effective_state(auto, stored)
    return {
        "id": gid,
        "title": gap.get("title", ""),
        "detail": gap.get("detail", ""),
        "severity": gap.get("severity", "info"),
        # 'detected' (Origin confirmed from data) vs 'checklist' (self-supply).
        "origin_status": gap.get("status", ""),
        "source": gap.get("source") or {},
        "fix": gap.get("fix"),          # present -> a one-click "Fix it" exists
        "has_doc": has_doc,
        "state": state,
        "state_note": (stored or {}).get("note", ""),
        "state_by": (stored or {}).get("by", ""),
        "state_updated_at": (stored or {}).get("updated_at", ""),
        "done": state == "accepted",
    }


def _counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    c = {s: 0 for s in STATE_ORDER}
    for it in items:
        c[it["state"]] = c.get(it["state"], 0) + 1
    c["total"] = len(items)
    c["open"] = sum(1 for it in items if it["state"] != "accepted")
    return c


def _sort_key(it: Dict[str, Any]):
    # Least-done first, then scariest severity, then stable by title.
    return (STATE_RANK.get(it["state"], 0),
            _SEV_RANK.get(it["severity"], 3),
            it.get("title", ""))


# ── the board ─────────────────────────────────────────────────────────────────
def board_for_sub(rec: Dict[str, Any], *, company_id: str = "") -> Dict[str, Any]:
    """Read-only per-platform status board for one sub. Never-fabricates: if the
    sub has no company profile yet (so the readiness engine has nothing to grade),
    returns ok with an empty platform list and a plain-English note. Mutates
    nothing; the caller need not persist after this."""
    company = (rec.get("company") or "").strip()
    base = {
        "ok": True,
        "sub": rec.get("slug", ""),
        "company": company,
        "company_id": company_id,
        "generated_at": _now(),
        "state_order": list(STATE_ORDER),
        "platforms": [],
        "totals": {s: 0 for s in STATE_ORDER},
    }
    if not company_id:
        base["note"] = "Add the subcontractor's company details to grade prequal readiness."
        return base

    try:
        from . import prequal_engine as _pq
    except Exception as exc:  # pragma: no cover
        base["note"] = f"prequal engine unavailable: {exc}"
        return base

    metrics = rec.get("prequal_metrics") if isinstance(rec.get("prequal_metrics"), dict) else {}
    report = _pq.assess_all(company_id, metrics) or {}
    assessments = report.get("assessments") or []
    drafted_ids = _drafted_gap_ids(rec)

    platforms: List[Dict[str, Any]] = []
    totals = {s: 0 for s in STATE_ORDER}
    for a in assessments:
        canon = _canon(a.get("platform", ""))
        items = [_item_from_gap(rec, canon, g, drafted_ids) for g in (a.get("gaps") or [])]
        items.sort(key=_sort_key)
        counts = _counts(items)
        for s in STATE_ORDER:
            totals[s] += counts.get(s, 0)
        grade = a.get("estimated_grade") or {}
        platforms.append({
            "platform": canon,
            "kb_platform": a.get("kb_platform", ""),
            "readiness": a.get("readiness", ""),
            "grade": grade.get("grade") or grade.get("letter") or "",
            "counts": counts,
            "complete": counts.get("open", 0) == 0 and counts.get("total", 0) > 0,
            "items": items,
        })

    base["platforms"] = platforms
    base["totals"] = totals
    base["totals"]["open"] = sum(1 for p in platforms for it in p["items"]
                                 if it["state"] != "accepted")
    base["platforms_assessed"] = [p["platform"] for p in platforms]
    return base


# ── the one write path ────────────────────────────────────────────────────────
def set_state(rec: Dict[str, Any], *, platform: str, item_id: str, state: str,
              note: str = "", by: str = "") -> Dict[str, Any]:
    """Record a human decision for one board item on one platform. Only the manual
    states ('submitted' / 'accepted') — plus 'needed'/'drafted' as an explicit
    undo — are accepted; anything else is rejected. Mutates rec['prequal_loop'];
    the caller persists. Returns the stored entry (or an error dict)."""
    item_id = (item_id or "").strip()
    state = (state or "").strip().lower()
    if not item_id:
        return {"ok": False, "error": "which item?"}
    if state not in STATE_RANK:
        return {"ok": False, "error": f"unknown state '{state}'",
                "valid": list(STATE_ORDER)}
    canon = _canon(platform)
    loop = rec.setdefault("prequal_loop", {})
    plat = loop.setdefault(canon, {})
    entry = {
        "state": state,
        "note": (note or "").strip(),
        "by": (by or "").strip(),
        "updated_at": _now(),
    }
    plat[item_id] = entry
    return {"ok": True, "platform": canon, "item_id": item_id, **entry}
