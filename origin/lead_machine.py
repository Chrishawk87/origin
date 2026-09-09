"""Origin Lead Machine — the daily "who should I call today" brief.

Lead Radar (``leadradar.py``) already finds contractors who just took a public
compliance hit (penalty-bearing OSHA / state-OSHA / MSHA citations, plus
Unsatisfactory/Conditional FMCSA carriers) and scores each for callability. The
Lead Machine sits on top of that and turns the raw radar hits into a ranked,
Houston-first *call list* — for each lead it answers, in Chris's words:

    "Here's who to call, here's their problem, here's the exact service to
     offer, and here's the 30-second pitch."

What it adds on top of the radar:

  * **Houston-first, fill-to-N.** Start with the greater-Houston / Fort Bend
    metro; if there aren't enough fresh citations there, widen to the rest of
    Texas, then nearby states, then nationwide — so the morning list always
    fills toward the target (default 25) without ever fabricating a lead.
  * **Plain-English problem + service map.** Every cited OSHA standard is mapped
    to the real problem it represents and the written program / training / doc
    package Origin sells to fix it. This map is deterministic regulatory
    knowledge, not a guess — an unrecognized standard degrades to a generic
    (but still honest) service list.
  * **"Needs help fast" signal.** Small establishments (few employees) with a
    fresh serious citation are the ones least likely to have an in-house safety
    department and most likely to buy quickly. Mega-penalty serial violators
    ($100k+) are deprioritized — they're already lawyered up. (Both learned the
    hard way from real call feedback.)
  * **Call-prep links, never fabricated phone numbers.** OSHA's data has the
    company, address, inspection #, cited standard, severity and penalty — but
    NO phone number or contact name. Rather than invent one, each lead carries
    one-click links to the OSHA establishment page and a Google / Maps lookup so
    the number is a few seconds away.
  * **A ready-to-read 30-second pitch** filled from the lead's own facts.

House rules (identical to the rest of Origin): deterministic, fully offline
except the same public gov APIs the radar already uses, never-fabricate,
file-based caching on the data volume, and isolated/defensive so a failure here
never takes down the caller.

Public surface:
    build_brief(...)   -> assemble a fresh ranked brief (hits the gov APIs)
    todays_brief(...)  -> today's cached brief, building + caching it if absent
    enrich_lead(lead)  -> turn one raw radar lead into a call card
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .paths import DATA_DIR

# Cache today's brief so the tab shows a stable list that refreshes once a day.
CACHE_DIR = DATA_DIR / "lead_machine"

DEFAULT_TARGET = 25
DEFAULT_SINCE_DAYS = 45          # Houston fresh-citation volume is thin; widen the window a bit.
DEFAULT_MIN_PENALTY = 1000.0
NEARBY_STATES = ("LA", "OK", "NM", "AR")

# Greater-Houston / Fort Bend detection. ZIP-3 covers the metro; the city list is
# a fallback for rows whose ZIP is blank or mis-keyed.
_HOUSTON_ZIP3 = {"770", "771", "772", "773", "774", "775"}
_HOUSTON_CITIES = {
    "houston", "pasadena", "pearland", "baytown", "sugar land", "richmond",
    "rosenberg", "katy", "cypress", "spring", "the woodlands", "conroe",
    "humble", "tomball", "stafford", "missouri city", "deer park", "la porte",
    "channelview", "friendswood", "league city", "texas city", "galveston",
    "dickinson", "alvin", "webster", "bellaire", "jersey village", "fresno",
    "needville", "brookshire", "fulshear", "manvel", "santa fe", "porter",
    "new caney", "crosby", "highlands", "seabrook", "kemah", "hitchcock",
    "sealy", "wallis", "east bernard", "beasley", "wharton", "el campo",
}


# ---------------------------------------------------------------------------
# Cited-standard -> real problem + the program Origin sells to fix it
# ---------------------------------------------------------------------------
# Keyed by "part.section" (e.g. "1926.501"). Deterministic regulatory knowledge:
# every entry names the standard's real subject and the corresponding written
# program. An unmapped standard falls back to a generic (still honest) service
# list rather than an invented specific.

_STANDARD_INFO: Dict[str, Dict[str, str]] = {
    # ---- 29 CFR 1926 (Construction) ----
    "1926.20": {"problem": "no adequate written safety & health program",
                "program": "Accident Prevention / Safety & Health Program"},
    "1926.21": {"problem": "employees not trained on the hazards of the job",
                "program": "Employee Safety Training Program"},
    "1926.59": {"problem": "hazard communication (SDS/labeling) gaps",
                "program": "Hazard Communication Program"},
    "1926.95": {"problem": "personal protective equipment not provided or used",
                "program": "PPE Program"},
    "1926.100": {"problem": "head protection not enforced",
                 "program": "PPE / Head Protection Program"},
    "1926.102": {"problem": "eye & face protection not provided",
                 "program": "PPE Program"},
    "1926.152": {"problem": "flammable-liquid storage & handling violations",
                 "program": "Flammable & Combustible Liquids Program"},
    "1926.350": {"problem": "gas welding & cutting hazards",
                 "program": "Welding, Cutting & Hot Work Program"},
    "1926.353": {"problem": "welding ventilation & protection gaps",
                 "program": "Welding, Cutting & Hot Work Program"},
    "1926.404": {"problem": "electrical wiring design/protection hazards",
                 "program": "Electrical Safety Program"},
    "1926.405": {"problem": "electrical wiring methods violations",
                 "program": "Electrical Safety Program"},
    "1926.416": {"problem": "working on or near energized electrical parts",
                 "program": "Electrical Safety Program"},
    "1926.417": {"problem": "circuits not locked/tagged out",
                 "program": "Electrical Safety / Lockout-Tagout Program"},
    "1926.451": {"problem": "unsafe scaffolding",
                 "program": "Scaffolding Safety Program"},
    "1926.452": {"problem": "unsafe scaffolding (specific-type)",
                 "program": "Scaffolding Safety Program"},
    "1926.453": {"problem": "aerial lift hazards",
                 "program": "Aerial Lift Program"},
    "1926.501": {"problem": "fall protection not provided at height",
                 "program": "Fall Protection Program"},
    "1926.502": {"problem": "defective or missing fall-arrest systems",
                 "program": "Fall Protection Program"},
    "1926.503": {"problem": "no fall-protection training",
                 "program": "Fall Protection Training"},
    "1926.550": {"problem": "crane / rigging operation hazards",
                 "program": "Crane & Rigging Program"},
    "1926.62": {"problem": "lead exposure controls missing",
                "program": "Lead Exposure Program"},
    "1926.651": {"problem": "unprotected excavation / trenching",
                 "program": "Excavation & Trenching Program"},
    "1926.652": {"problem": "trench without cave-in protection",
                 "program": "Excavation & Trenching Program"},
    "1926.760": {"problem": "steel-erection fall hazards",
                 "program": "Steel Erection / Fall Protection Program"},
    "1926.1052": {"problem": "unsafe stairways",
                  "program": "Stairway & Ladder Safety Program"},
    "1926.1053": {"problem": "unsafe ladder use",
                  "program": "Ladder Safety Program"},
    "1926.1101": {"problem": "asbestos exposure controls missing",
                  "program": "Asbestos Program"},
    "1926.1153": {"problem": "respirable crystalline silica overexposure",
                  "program": "Silica Exposure Control Program"},
    "1926.1400": {"problem": "crane operation in construction",
                  "program": "Crane & Rigging Program"},
    "1926.1408": {"problem": "cranes working near power lines",
                  "program": "Crane & Rigging Program"},
    # ---- 29 CFR 1910 (General Industry) ----
    "1910.22": {"problem": "walking-working surface / housekeeping hazards",
                "program": "Walking-Working Surfaces Program"},
    "1910.23": {"problem": "unguarded floor / wall openings",
                "program": "Walking-Working Surfaces / Fall Program"},
    "1910.28": {"problem": "fall hazards on walking-working surfaces",
                "program": "Fall Protection Program"},
    "1910.29": {"problem": "missing guardrails / fall-protection systems",
                "program": "Fall Protection Program"},
    "1910.38": {"problem": "no emergency action plan",
                "program": "Emergency Action Plan"},
    "1910.95": {"problem": "overexposure to noise",
                "program": "Hearing Conservation Program"},
    "1910.119": {"problem": "process safety management (highly hazardous chemicals) gaps",
                 "program": "Process Safety Management Program"},
    "1910.120": {"problem": "hazardous-waste operations / emergency response gaps",
                 "program": "HAZWOPER Program"},
    "1910.132": {"problem": "PPE hazard assessment missing",
                 "program": "PPE Program"},
    "1910.133": {"problem": "eye & face protection gaps",
                 "program": "PPE Program"},
    "1910.134": {"problem": "respirator program deficiencies",
                 "program": "Respiratory Protection Program"},
    "1910.146": {"problem": "permit-required confined space hazards",
                 "program": "Confined Space Entry Program"},
    "1910.147": {"problem": "hazardous energy not locked/tagged out",
                 "program": "Lockout/Tagout (LOTO) Program"},
    "1910.157": {"problem": "portable fire extinguisher deficiencies",
                 "program": "Fire Extinguisher / Emergency Program"},
    "1910.178": {"problem": "forklift / powered industrial truck violations",
                 "program": "Powered Industrial Truck (Forklift) Program"},
    "1910.212": {"problem": "machine not guarded",
                 "program": "Machine Guarding Program"},
    "1910.213": {"problem": "woodworking machine guarding gaps",
                 "program": "Machine Guarding Program"},
    "1910.219": {"problem": "mechanical power-transmission guarding gaps",
                 "program": "Machine Guarding Program"},
    "1910.242": {"problem": "hand & portable powered tool hazards",
                 "program": "Hand & Power Tool Program"},
    "1910.303": {"problem": "electrical installation hazards",
                 "program": "Electrical Safety Program"},
    "1910.304": {"problem": "electrical wiring / grounding hazards",
                 "program": "Electrical Safety Program"},
    "1910.305": {"problem": "electrical wiring methods violations",
                 "program": "Electrical Safety Program"},
    "1910.1025": {"problem": "lead exposure controls missing",
                  "program": "Lead Exposure Program"},
    "1910.1030": {"problem": "bloodborne pathogen exposure controls missing",
                  "program": "Bloodborne Pathogens Program"},
    "1910.1053": {"problem": "respirable crystalline silica overexposure",
                  "program": "Silica Exposure Control Program"},
    "1910.1200": {"problem": "hazard communication (SDS/labeling) gaps",
                  "program": "Hazard Communication Program"},
}

_RECORDKEEPING = {"problem": "injury & illness recordkeeping violations",
                  "program": "OSHA 300 Recordkeeping Program"}

# NAICS 2-digit prefix -> the sector_content key we can label it with.
_NAICS_SECTOR_KEY = {
    "11": "11", "21": "21", "22": "22", "23": "23",
    "31": "31-33", "32": "31-33", "33": "31-33",
    "42": "42", "44": "42", "45": "42",
    "48": "48-49", "49": "48-49",
    "51": "51", "56": "56", "62": "62",
    "81": "81",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _to_float(v: Any) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip() or 0)
    except Exception:
        return 0.0


def _to_int(v: Any) -> Optional[int]:
    try:
        s = re.sub(r"[^\d]", "", str(v))
        return int(s) if s else None
    except Exception:
        return None


def _days_since(datestr: str) -> Optional[int]:
    if not datestr:
        return None
    s = str(datestr).strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return max(0, (datetime.utcnow() - dt).days)
        except Exception:
            continue
    return None


def _industry_label(naics: str) -> str:
    naics = (naics or "").strip()
    if not naics:
        return ""
    key = _NAICS_SECTOR_KEY.get(naics[:2])
    if key:
        try:
            from . import sector_content as _sc
            lbl = _sc.label(key)
            if lbl:
                return lbl
        except Exception:
            pass
    return f"NAICS {naics}"


def _standard_lookup(std: str) -> Optional[Dict[str, str]]:
    """Map a cited standard like '1926.501(b)(1)' to its problem + program."""
    m = re.match(r"(19\d\d)\.(\d+)", std or "")
    if not m:
        return None
    part, section = m.group(1), m.group(2)
    if part == "1904":
        return _RECORDKEEPING
    return _STANDARD_INFO.get(f"{part}.{section}")


def _primary_standard(standards: List[str]) -> tuple:
    """Pick the most actionable cited standard: first one we can map, else the
    first cited. Returns (standard_string, info_or_None)."""
    stds = [s for s in (standards or []) if s]
    for s in stds:
        info = _standard_lookup(s)
        if info:
            return s, info
    return (stds[0] if stds else ""), None


# ---------------------------------------------------------------------------
# Enrichment — one raw radar lead -> a call card
# ---------------------------------------------------------------------------

_BASE_SERVICES_TAIL = [
    "Employee training",
    "Corrective action plan",
    "Abatement evidence package",
    "Safety inspection / gap check",
]


def _region_tier(lead: Dict[str, Any]) -> int:
    """0 = greater Houston, 1 = rest of Texas, 2 = nearby state, 3 = elsewhere."""
    state = (lead.get("state") or "").upper()
    if state == "TX":
        zip3 = (str(lead.get("zip") or "")[:3])
        city = (lead.get("city") or "").strip().lower()
        if zip3 in _HOUSTON_ZIP3 or city in _HOUSTON_CITIES:
            return 0
        return 1
    if state in NEARBY_STATES:
        return 2
    return 3


def _region_label(tier: int) -> str:
    return {0: "Greater Houston", 1: "Texas", 2: "Nearby state",
            3: "Out of area"}.get(tier, "")


def _call_prep(lead: Dict[str, Any]) -> Dict[str, str]:
    """One-click links to find the number — no fabricated phone."""
    company = (lead.get("company") or "").strip()
    where = " ".join(p for p in (lead.get("city"), lead.get("state")) if p)
    q = urllib.parse.quote(f'"{company}" {where} phone')
    mq = urllib.parse.quote(f'{company} {where}')
    links = {
        "google": f"https://www.google.com/search?q={q}",
        "maps": f"https://www.google.com/maps/search/{mq}",
    }
    if lead.get("url"):
        links["osha"] = lead["url"]
    return links


def _osha_problem(lead: Dict[str, Any], info: Optional[Dict[str, str]]) -> str:
    if info:
        return info["problem"]
    vt = lead.get("viol_types") or []
    sev = ("/".join(vt) + " ") if vt else ""
    return f"an open {sev}OSHA safety violation".strip()


def _osha_services(info: Optional[Dict[str, str]]) -> List[str]:
    program = (info or {}).get("program", "Written safety program")
    return ["Citation analysis", program] + list(_BASE_SERVICES_TAIL)


def _services_for(lead: Dict[str, Any], info: Optional[Dict[str, str]]) -> List[str]:
    kind = lead.get("kind")
    if kind == "fmcsa_rating":
        return ["FMCSA safety-rating review", "DOT/FMCSA Safety Management Program",
                "Driver qualification files", "Corrective action plan",
                "ISN / Avetta / Veriforce cleanup", "Mock DOT audit"]
    if kind == "msha_violation":
        return ["Citation analysis", "MSHA Part 46/48 training records",
                "Corrective action plan", "Abatement evidence package",
                "Workplace exam program", "Mock MSHA inspection"]
    return _osha_services(info)


def _problem_for(lead: Dict[str, Any], info: Optional[Dict[str, str]]) -> str:
    kind = lead.get("kind")
    if kind == "fmcsa_rating":
        rate = {"U": "Unsatisfactory", "C": "Conditional"}.get(lead.get("rating", ""), "flagged")
        return (f"a {rate} FMCSA safety rating — the exact profile ISN / Avetta / "
                f"Veriforce reject")
    if kind == "msha_violation":
        return "an open MSHA violation with a proposed penalty"
    return _osha_problem(lead, info)


def _severity(lead: Dict[str, Any]) -> str:
    vt = lead.get("viol_types") or []
    return "/".join(vt) if vt else ""


def _needs_help_fast(lead: Dict[str, Any]) -> bool:
    """Small shop + fresh confirmed citation, not a lawyered-up serial violator.
    These are the least likely to have a safety department and the fastest to
    close. Employee count is a proxy; unknown counts are treated as 'maybe'."""
    if lead.get("kind") not in ("osha_citation", "msha_violation"):
        return False
    penalty = _to_float(lead.get("penalty"))
    if penalty >= 100000:            # serial violator — usually already lawyered up
        return False
    emp = _to_int(lead.get("num_employees"))
    if emp is not None and emp > 50:
        return False
    return True


def _machine_score(lead: Dict[str, Any], base: int) -> int:
    """Adjust the radar's callability score toward Chris's 'no safety department,
    needs help fast' target."""
    score = int(base or 0)
    penalty = _to_float(lead.get("penalty"))
    emp = _to_int(lead.get("num_employees"))
    if _needs_help_fast(lead):
        score += 8 if emp is not None else 4
    if penalty >= 100000:            # deprioritize the serial-violator giants
        score -= 15
    return max(0, min(100, score))


def _why(lead: Dict[str, Any], days: Optional[int]) -> str:
    reasons: List[str] = []
    sev = _severity(lead)
    if days is not None:
        reasons.append(f"Fresh {sev + ' ' if sev else ''}citation ({days}d ago)".replace("  ", " "))
    elif sev:
        reasons.append(f"{sev} citation on record")
    pen = _to_float(lead.get("penalty"))
    if pen:
        reasons.append(f"${pen:,.0f} penalty on the table")
    if lead.get("trade_match"):
        reasons.append("in a trade you serve")
    if _needs_help_fast(lead):
        emp = _to_int(lead.get("num_employees"))
        if emp is not None:
            reasons.append(f"small shop (~{emp} employees) — likely no in-house safety team")
        else:
            reasons.append("likely a small shop with no in-house safety team")
    return " \u00b7 ".join(reasons)


def _pitch(lead: Dict[str, Any], info: Optional[Dict[str, str]], problem: str) -> str:
    company = (lead.get("company") or "your company").strip()
    authority = lead.get("authority") or "OSHA"
    date = lead.get("opened") or ""
    sev = _severity(lead)
    pen = _to_float(lead.get("penalty"))
    program = (info or {}).get("program", "the written program")
    when = f"on {date} " if date else ""
    sev_txt = f"a {sev.lower()} violation" if sev else "a violation"
    pen_txt = f" with a ${pen:,.0f} penalty" if pen else ""
    return (
        f"Hi, this is Chris with Origin Management Solutions. I saw {authority} "
        f"cited {company} {when}for {problem} — {sev_txt}{pen_txt}. We help "
        f"contractors close these out fast: we do the citation analysis, build "
        f"the {program} and the training records, and put together the exact "
        f"abatement evidence OSHA wants back. Most clients turn this around in "
        f"days, not weeks. Do you have five minutes this week to walk through "
        f"what OSHA's going to expect?"
    ).replace("  ", " ")


def enrich_lead(lead: Dict[str, Any]) -> Dict[str, Any]:
    """Turn one raw radar lead into a call card: problem, services, why, pitch,
    call-prep links, region tier, and an adjusted 'call today' score."""
    std, info = _primary_standard(lead.get("standards") or [])
    days = _days_since(lead.get("opened") or lead.get("seendate") or "")
    problem = _problem_for(lead, info)
    base_score = int(lead.get("score") or 0)
    tier = _region_tier(lead)
    card = {
        "company": lead.get("company", ""),
        "city": lead.get("city", ""),
        "state": lead.get("state", ""),
        "address": lead.get("address", ""),
        "zip": lead.get("zip", ""),
        "industry": _industry_label(lead.get("naics", "")),
        "naics": lead.get("naics", ""),
        "authority": lead.get("authority", ""),
        "kind": lead.get("kind", ""),
        "inspection": lead.get("activity_nr", ""),
        "status": "OPEN",
        "standard": std,
        "all_standards": lead.get("standards") or [],
        "severity": _severity(lead),
        "penalty": _to_float(lead.get("penalty")),
        "citations": lead.get("citations", 0),
        "issued": lead.get("opened", ""),
        "days_ago": days,
        "problem": problem,
        "services": _services_for(lead, info),
        "why": _why(lead, days),
        "pitch": _pitch(lead, info, problem),
        "call_prep": _call_prep(lead),
        "needs_help_fast": _needs_help_fast(lead),
        "region_tier": tier,
        "region": _region_label(tier),
        "score": _machine_score(lead, base_score),
        "radar_score": base_score,
        "summary": lead.get("summary", ""),
    }
    return card


# ---------------------------------------------------------------------------
# Assembly — Houston-first, fill toward the target
# ---------------------------------------------------------------------------

def _gather(states: Optional[List[str]], since_days: int, min_penalty: float) -> Dict[str, Any]:
    """One radar pass (no persistence, confirmed sources only — no news)."""
    from . import leadradar as _radar
    return _radar.run_radar(
        states=states, since_days=since_days, min_penalty=min_penalty,
        include_news=False, include_fmcsa=True, include_msha=True,
        target_trades_only=False, persist=False)


def build_brief(*, target: int = DEFAULT_TARGET, since_days: int = DEFAULT_SINCE_DAYS,
                min_penalty: float = DEFAULT_MIN_PENALTY,
                allow_national: bool = True, persist_cache: bool = True) -> Dict[str, Any]:
    """Assemble a fresh ranked call brief. Houston-first; widens to the rest of
    Texas, then nearby states, then nationwide only as needed to reach ``target``.
    Confirmed leads only (OSHA/state-OSHA/MSHA citations + Unsat/Conditional FMCSA
    carriers); news is excluded because it lacks the structured call fields."""
    from . import leadradar as _radar

    picked: Dict[str, Dict[str, Any]] = {}
    sources: Dict[str, Any] = {}

    def ingest(res: Dict[str, Any]) -> None:
        for k, v in (res.get("sources") or {}).items():
            # Keep the first (Texas) source note; it's the most relevant status.
            sources.setdefault(k, v)
        for lead in res.get("leads", []):
            if lead.get("kind") == "news_incident":
                continue
            key = _radar._lead_dedupe_key(lead)
            if key not in picked:
                picked[key] = lead

    # Tier 1+2 — Texas (Houston metro is split out later by region_tier).
    ingest(_gather(["TX"], since_days, min_penalty))
    # Tier 3 — nearby states, only if we're short.
    if len(picked) < target:
        ingest(_gather(list(NEARBY_STATES), since_days, min_penalty))
    # Tier 4 — nationwide, last resort to fill the list.
    if len(picked) < target and allow_national:
        ingest(_gather(None, since_days, min_penalty))

    cards = [enrich_lead(l) for l in picked.values()]
    # Houston first, then best 'call today' score, then biggest penalty.
    cards.sort(key=lambda c: (c["region_tier"], -c["score"], -c["penalty"]))
    top = cards[:target]

    counts = {
        "total": len(top),
        "available": len(cards),
        "houston": sum(1 for c in top if c["region_tier"] == 0),
        "texas": sum(1 for c in top if c["region_tier"] == 1),
        "nearby": sum(1 for c in top if c["region_tier"] == 2),
        "other": sum(1 for c in top if c["region_tier"] == 3),
        "hot": sum(1 for c in top if c["score"] >= 70),
        "needs_help_fast": sum(1 for c in top if c["needs_help_fast"]),
    }

    # Surface the OSHA data status so the tab can tell Chris to set DOL_API_KEY.
    note = ""
    osha_src = sources.get("osha") or {}
    if osha_src.get("reason") == "no_dol_key":
        note = osha_src.get("note", "")
    elif not top:
        note = ("No fresh penalty-bearing citations in the window. Widen the "
                "date range or lower the minimum penalty.")

    brief = {
        "ok": True,
        "date": _today(),
        "generated_at": _now(),
        "target": target,
        "since_days": since_days,
        "min_penalty": min_penalty,
        "counts": counts,
        "note": note,
        "sources": sources,
        "leads": top,
    }
    if persist_cache:
        _write_cache(brief)
    return brief


# ---------------------------------------------------------------------------
# Daily cache
# ---------------------------------------------------------------------------

def _cache_path(date: str) -> Any:
    return CACHE_DIR / f"brief-{date}.json"


def _write_cache(brief: Dict[str, Any]) -> bool:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with _cache_path(brief.get("date") or _today()).open("w", encoding="utf-8") as fh:
            json.dump(brief, fh)
        return True
    except Exception:
        return False


def _read_cache(date: str) -> Optional[Dict[str, Any]]:
    try:
        p = _cache_path(date)
        if not p.exists():
            return None
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def todays_brief(*, refresh: bool = False, target: int = DEFAULT_TARGET,
                 since_days: int = DEFAULT_SINCE_DAYS,
                 min_penalty: float = DEFAULT_MIN_PENALTY) -> Dict[str, Any]:
    """Today's cached brief, building + caching it on first request of the day
    (or when ``refresh`` is set). This is what the 'wake up every morning' tab
    reads — the first visit each day builds the list, later visits reuse it."""
    if not refresh:
        cached = _read_cache(_today())
        if cached:
            cached["cached"] = True
            return cached
    brief = build_brief(target=target, since_days=since_days, min_penalty=min_penalty)
    brief["cached"] = False
    return brief
