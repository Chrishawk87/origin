"""Lead Radar — surface contractors who just took a public compliance hit.

The idea: the best time to call a contractor about fixing their safety /
prequal program is right after they've been cited or fined, while the pain is
fresh and the budget is unlocked. Lead Radar pulls two public streams:

  1. OSHA enforcement — issued citations that carry a *penalty*, pulled from the
     U.S. DOL public enforcement dataset. This dataset INCLUDES state-plan
     (state-OSHA) inspections, so a Cal/OSHA or Oregon-OSHA citation shows up
     here too — we tag the citing authority from the establishment's state.
     Only penalty-bearing citations are surfaced (a bare "inspection opened"
     record has no pain and no budget yet — that lesson is baked in here).

  2. News monitoring — recent U.S. news about safety violations, OSHA fines and
     workplace-safety penalties, via the free GDELT DOC 2.0 API (no key). These
     are labelled "reported incident" (lower confidence — no confirmed penalty).

Every hit is scored for *callability* (how worth-a-call it is) and written into
the same ``rescue_leads.jsonl`` the rest of Origin's tools feed, tagged
``source="lead-radar"`` so it shows up in the admin Leads view alongside the
inbound leads.

Design constraints (why this file looks the way it does):
  * The deployed app has no access to any developer's local web tools, so this
    module talks to the public APIs directly with the Python standard library
    (``urllib``) — nothing to install, works on Railway out of the box.
  * Everything degrades gracefully. No DOL API key? Skip OSHA, still run news.
    A source is down or rate-limited? Log it, return what we have, never crash
    the admin request.
  * The DOL endpoint/agency/dataset and the news query are all overridable by
    env var so nothing here is hard-wired to break on a schema change.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .paths import DATA_DIR

# We write into the same leads file the public tools use, so radar leads land in
# the admin Leads view with everything else.
LEADS_FILE = DATA_DIR / "rescue_leads.jsonl"

RADAR_SOURCE = "lead-radar"
USER_AGENT = "OriginComplianceRadar/1.0 (+https://originmanagementsolutions.com)"

# ---------------------------------------------------------------------------
# Config (all env-overridable so a schema change never requires a code change)
# ---------------------------------------------------------------------------

import os

# DOL public enforcement (v4). Data endpoints authenticate with a free API key
# passed as the X-API-KEY query parameter. Register at
# https://dataportal.dol.gov/registration — set the key on Railway as DOL_API_KEY.
DOL_API_KEY = os.environ.get("DOL_API_KEY", "").strip()
# Full override wins; otherwise we build v4 from agency/dataset.
DOL_INSPECTION_URL = os.environ.get("DOL_INSPECTION_URL", "").strip()
DOL_V4_BASE = os.environ.get("DOL_V4_BASE", "https://apiprod.dol.gov/v4/get").strip()
# v4 paths are case-sensitive; the agency abbreviation is uppercase (OSHA, WHD, ILAB).
DOL_AGENCY = os.environ.get("DOL_AGENCY", "OSHA").strip()
# The OSHA data splits across two tables: `violation` carries the penalty and the
# citation issuance_date (available ~5 days after a federal citation, 30 for
# state — this is the *fresh* signal), but has no company name. `inspection`
# carries estab_name/location/naics but no penalty and only appears once the case
# is closed (months of lag). So we pull recent penalty-bearing violations, then
# look up their establishments in inspection by activity_nr and merge.
DOL_VIOLATION_DATASET = os.environ.get("DOL_VIOLATION_DATASET", "violation").strip()
DOL_DATASET = os.environ.get("DOL_DATASET", "inspection").strip()
# Field names on the violation table (overridable if the schema is renamed).
DOL_PENALTY_FIELD = os.environ.get("DOL_PENALTY_FIELD", "current_penalty").strip()
DOL_ISSUANCE_FIELD = os.environ.get("DOL_ISSUANCE_FIELD", "issuance_date").strip()

# GDELT DOC 2.0 — free, no key. Rate-limited to a handful of calls/hour, which
# is why radar is on-demand, not a tight polling loop.
GDELT_URL = os.environ.get("GDELT_URL", "https://api.gdeltproject.org/api/v2/doc/doc").strip()

# ---------------------------------------------------------------------------
# State-plan authority map — the DOL dataset carries state-OSHA citations too;
# we name the citing authority from the establishment's state so a lead reads
# "Cited by Cal/OSHA" not just "OSHA".
# ---------------------------------------------------------------------------

_STATE_PLAN_AUTHORITY = {
    "AZ": "Arizona Div. of OSH (ADOSH)",
    "CA": "Cal/OSHA",
    "CT": "Connecticut OSHA (state/local only)",
    "HI": "Hawaii OSH (HIOSH)",
    "IN": "Indiana OSHA (IOSHA)",
    "IA": "Iowa OSHA",
    "KY": "Kentucky OSH (KY OSH)",
    "MD": "Maryland OSH (MOSH)",
    "MI": "Michigan OSHA (MIOSHA)",
    "MN": "Minnesota OSHA (MNOSHA)",
    "NV": "Nevada OSHA (Nevada OSHA)",
    "NM": "New Mexico OHSB",
    "NY": "New York PESH (public only)",
    "NC": "North Carolina OSH (NC DOL)",
    "OR": "Oregon OSHA (Oregon OSHA)",
    "SC": "South Carolina OSHA (SC OSHA)",
    "TN": "Tennessee OSHA (TOSHA)",
    "UT": "Utah OSH (UOSH)",
    "VT": "Vermont OSHA (VOSHA)",
    "VA": "Virginia OSH (VOSH)",
    "WA": "Washington DOSH (L&I)",
    "WY": "Wyoming OSHA",
    "PR": "Puerto Rico OSHA",
}


def citing_authority(state: str) -> str:
    """Human-readable citing authority for a US state postal code."""
    return _STATE_PLAN_AUTHORITY.get((state or "").strip().upper(), "Federal OSHA")


# ---------------------------------------------------------------------------
# Trade / NAICS matching — Origin's wheelhouse is field contractors (oil & gas,
# construction, trucking, industrial services). A citation in one of these is a
# stronger lead than, say, a hospital or a school.
# ---------------------------------------------------------------------------

# NAICS 2-3 digit prefixes for the trades Origin sells into.
_TARGET_NAICS_PREFIXES = (
    "21",   # Mining, quarrying, oil & gas extraction
    "213",  # Support activities for mining (oilfield services)
    "23",   # Construction
    "48",   # Transportation (trucking)
    "484",  # Truck transportation
    "31", "32", "33",  # Manufacturing / industrial
    "562",  # Waste / remediation services
    "811",  # Repair & maintenance
)

_TRADE_KEYWORDS = re.compile(
    r"\b(construction|contractor|drilling|oilfield|oil field|energy|pipeline|"
    r"electric|roofing|concrete|welding|trucking|hauling|excavat|"
    r"industrial|refinery|plant|mechanical|hvac|scaffold|demolition|"
    r"fabricat|steel|utility|utilities)\b",
    re.IGNORECASE,
)


def _naics_is_target(naics: str) -> bool:
    naics = (naics or "").strip()
    return any(naics.startswith(p) for p in _TARGET_NAICS_PREFIXES)


# ---------------------------------------------------------------------------
# HTTP — stdlib only, defensive. Never raises to the caller.
# ---------------------------------------------------------------------------

def _http_get(url: str, headers: Optional[Dict[str, str]] = None,
              timeout: int = 25) -> Optional[str]:
    """GET a URL and return the body text, or None on any failure."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except Exception:
        return None


def _http_get_json(url: str, headers: Optional[Dict[str, str]] = None,
                   timeout: int = 25) -> Optional[Any]:
    body = _http_get(url, headers=headers, timeout=timeout)
    if not body:
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def _as_records(payload: Any) -> List[Dict[str, Any]]:
    """Normalize the many shapes an API might return into a list of dicts."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "records", "rows", "items", "articles"):
            val = payload.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
        # A single record object.
        return [payload]
    return []


# ---------------------------------------------------------------------------
# OSHA / DOL enforcement
# ---------------------------------------------------------------------------

def _dol_url(dataset: str, *, filt: Dict[str, Any], limit: int,
             sort_by: str, sort: str = "desc") -> str:
    """Build a DOL v4 query URL for any OSHA dataset.

    v4 supports gt/lt/in operators via a JSON ``filter_object`` query param and
    authenticates with the API key as the ``X-API-KEY`` query param. If
    DOL_INSPECTION_URL is set it overrides the base (for the inspection dataset).
    """
    params = {
        "limit": str(limit),
        "offset": "0",
        "sort": sort,
        "sort_by": sort_by,
        "filter_object": json.dumps(filt, separators=(",", ":")),
    }
    if DOL_API_KEY:
        params["X-API-KEY"] = DOL_API_KEY
    if dataset == DOL_DATASET and DOL_INSPECTION_URL:
        base = DOL_INSPECTION_URL
    else:
        base = f"{DOL_V4_BASE}/{DOL_AGENCY}/{dataset}/json"
    return base + "?" + urllib.parse.urlencode(params)


def _violation_url(*, since: str, min_penalty: float, limit: int) -> str:
    """Recent penalty-bearing citations from the violation table (the fresh signal)."""
    filt: Dict[str, Any] = {"and": [
        {"field": DOL_ISSUANCE_FIELD, "operator": "gt", "value": since},
        {"field": DOL_PENALTY_FIELD, "operator": "gt", "value": min_penalty},
        {"field": "delete_flag", "operator": "neq", "value": "D"},
    ]}
    return _dol_url(DOL_VIOLATION_DATASET, filt=filt, limit=limit,
                    sort_by=DOL_ISSUANCE_FIELD, sort="desc")


def _inspection_lookup_url(activity_nrs: List[str], *, limit: int) -> str:
    """Look up establishments/locations for a set of inspection activity numbers."""
    filt: Dict[str, Any] = {"and": [
        {"field": "activity_nr", "operator": "in", "value": activity_nrs},
    ]}
    return _dol_url(DOL_DATASET, filt=filt, limit=limit, sort_by="activity_nr", sort="asc")


def _pick(rec: Dict[str, Any], *names: str, default: str = "") -> str:
    """First non-empty value among candidate field names (schema-tolerant)."""
    for n in names:
        v = rec.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return default


def _to_float(v: Any) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip() or 0)
    except Exception:
        return 0.0


_VIOL_TYPE_LABEL = {"S": "Serious", "W": "Willful", "R": "Repeat", "O": "Other"}


def _aggregate_violations(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Group raw violation rows by parent inspection (activity_nr).

    One establishment can get several citations from a single inspection; we roll
    them up into one lead — summed penalty, latest issuance date, worst violation
    type, citation count.
    """
    by_insp: Dict[str, Dict[str, Any]] = {}
    for rec in rows:
        if str(_pick(rec, "delete_flag")).upper() == "D":
            continue
        activity = _pick(rec, "activity_nr", "activity_number")
        if not activity:
            continue
        penalty = _to_float(_pick(rec, DOL_PENALTY_FIELD, "current_penalty",
                                  "initial_penalty", default="0"))
        issued = _pick(rec, DOL_ISSUANCE_FIELD, "issuance_date", "date")
        vtype = _pick(rec, "viol_type", "violation_type").upper()[:1]
        agg = by_insp.setdefault(activity, {
            "activity_nr": activity, "penalty": 0.0, "issued": "",
            "citations": 0, "types": set()})
        agg["penalty"] += penalty
        agg["citations"] += 1
        if vtype:
            agg["types"].add(vtype)
        if issued and issued > agg["issued"]:
            agg["issued"] = issued
    return by_insp


def _build_osha_lead(insp: Dict[str, Any], agg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Merge an inspection record (who/where) with aggregated violation info (penalty/date)."""
    company = _pick(insp, "estab_name", "establishment_name", "company", "name")
    if not company:
        return None
    state = _pick(insp, "site_state", "state", "mail_state")
    naics = _pick(insp, "naics_code", "naics")
    activity = agg["activity_nr"]
    types = sorted(agg.get("types") or [], key=lambda t: "WRSO".find(t) if t in "WRSO" else 9)
    lead = {
        "kind": "osha_citation",
        "label": "confirmed citation",
        "company": company,
        "authority": citing_authority(state),
        "penalty": round(agg.get("penalty", 0.0), 2),
        "naics": naics,
        "state": state.upper(),
        "city": _pick(insp, "site_city", "city"),
        "address": _pick(insp, "site_address", "address"),
        "zip": _pick(insp, "site_zip", "zip", "zip_code"),
        "opened": agg.get("issued") or _pick(insp, "open_date"),
        "activity_nr": activity,
        "citations": agg.get("citations", 0),
        "viol_types": [_VIOL_TYPE_LABEL.get(t, t) for t in types],
        "trade_match": _naics_is_target(naics),
        "url": (f"https://www.osha.gov/ords/imis/establishment.inspection_detail?id={activity}"
                if activity else ""),
    }
    lead["summary"] = _osha_summary(lead)
    return lead


def _osha_summary(lead: Dict[str, Any]) -> str:
    parts = [f"{lead['authority']} citation"]
    if lead.get("viol_types"):
        parts[0] = f"{lead['authority']} {'/'.join(lead['viol_types'])} citation"
    if lead.get("penalty"):
        n = lead.get("citations") or 0
        cite = f" across {n} items" if n > 1 else ""
        parts.append(f"${lead['penalty']:,.0f} penalty{cite}")
    where = ", ".join([p for p in (lead.get("city"), lead.get("state")) if p])
    if where:
        parts.append(where)
    if lead.get("opened"):
        parts.append(f"issued {lead['opened']}")
    return " \u2014 ".join(parts)


def _chunk(seq: List[Any], n: int) -> List[List[Any]]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def fetch_osha_leads(*, states: Optional[List[str]] = None, since_days: int = 30,
                     min_penalty: float = 1000.0, limit: int = 200,
                     target_trades_only: bool = False) -> Dict[str, Any]:
    """Pull recent penalty-bearing OSHA/state-OSHA citations as radar leads.

    OSHA splits the data: the *violation* table has the penalty + issuance date
    (fresh — available ~5 days after a federal citation) but no company name; the
    *inspection* table has the establishment + location but no penalty. So we pull
    recent penalty-bearing violations, roll them up per inspection, then look up
    those establishments and merge. State filtering happens after the join, since
    the violation table carries no state (the inspection does).
    """
    if not (DOL_API_KEY or DOL_INSPECTION_URL):
        return {"ok": False, "reason": "no_dol_key", "leads": [],
                "note": "Set DOL_API_KEY (free from dataportal.dol.gov) to enable OSHA pulls."}

    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d")
    # Pull generously so aggregation + state filtering still leaves a full page.
    viol_payload = _http_get_json(
        _violation_url(since=since, min_penalty=min_penalty, limit=max(limit * 5, 500)),
        timeout=45)
    if viol_payload is None:
        return {"ok": False, "reason": "fetch_failed", "leads": [],
                "note": "DOL violation endpoint did not return data."}

    by_insp = _aggregate_violations(_as_records(viol_payload))
    if not by_insp:
        return {"ok": True, "reason": "", "count": 0, "leads": [], "since": since}

    # Look up the establishments for those inspections (chunked to keep URLs sane).
    want = set(states and [s.strip().upper() for s in states if s.strip()] or [])
    activity_nrs = list(by_insp.keys())
    insp_by_nr: Dict[str, Dict[str, Any]] = {}
    for chunk in _chunk(activity_nrs, 100):
        payload = _http_get_json(_inspection_lookup_url(chunk, limit=len(chunk)), timeout=45)
        for rec in _as_records(payload):
            nr = _pick(rec, "activity_nr", "activity_number")
            if nr:
                insp_by_nr[nr] = rec

    leads: List[Dict[str, Any]] = []
    for nr, agg in by_insp.items():
        insp = insp_by_nr.get(nr)
        if not insp:
            continue  # establishment not yet published (inspection still open) — skip
        lead = _build_osha_lead(insp, agg)
        if not lead:
            continue
        if want and lead["state"] not in want:
            continue
        if target_trades_only and not lead["trade_match"]:
            continue
        leads.append(lead)

    leads.sort(key=lambda l: (l.get("opened") or "", l.get("penalty") or 0), reverse=True)
    return {"ok": True, "reason": "", "count": len(leads),
            "leads": leads[:limit], "since": since}


# ---------------------------------------------------------------------------
# News monitoring (GDELT DOC 2.0)
# ---------------------------------------------------------------------------

_DEFAULT_NEWS_QUERY = (
    '(OSHA OR "safety violation" OR "workplace safety" OR "safety citation") '
    '(fined OR fine OR penalty OR citation OR cited OR violations) '
    'sourcecountry:US'
)

# Strip common corporate suffixes when we guess a company name from a headline.
_COMPANY_HINT = re.compile(
    r"\b([A-Z][A-Za-z&.,'\- ]{2,60}?(?:\s+(?:Inc|LLC|LP|LLP|Co|Corp|Company|"
    r"Construction|Contractors?|Services|Industries|Energy|Drilling|Trucking))\b)"
)


def _guess_company(title: str) -> str:
    m = _COMPANY_HINT.search(title or "")
    if m:
        return m.group(1).strip(" .,-")
    return ""


def normalize_news_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = _pick(rec, "title")
    url = _pick(rec, "url")
    if not title or not url:
        return None
    domain = _pick(rec, "domain")
    seendate = _pick(rec, "seendate")
    company = _guess_company(title)
    lead = {
        "kind": "news_incident",
        "label": "reported incident",
        "company": company,
        "authority": "News report",
        "penalty": 0.0,
        "title": title,
        "url": url,
        "domain": domain,
        "seendate": seendate,
        "trade_match": bool(_TRADE_KEYWORDS.search(title)),
        "summary": f"News: {title[:180]}" + (f" ({domain})" if domain else ""),
    }
    return lead


def fetch_news_leads(*, query: str = "", timespan: str = "1week",
                     max_records: int = 75) -> Dict[str, Any]:
    """Pull recent US safety-enforcement news via GDELT (free, keyless)."""
    q = query.strip() or _DEFAULT_NEWS_QUERY
    params = {
        "query": q,
        "mode": "artlist",
        "format": "json",
        "maxrecords": str(max(1, min(max_records, 250))),
        "timespan": timespan,
        "sort": "datedesc",
    }
    url = GDELT_URL + "?" + urllib.parse.urlencode(params)
    payload = _http_get_json(url, timeout=30)
    if payload is None:
        return {"ok": False, "reason": "fetch_failed", "leads": [],
                "note": "GDELT news endpoint did not return data (it may be rate-limited)."}

    leads: List[Dict[str, Any]] = []
    for rec in _as_records(payload):
        lead = normalize_news_record(rec)
        if lead:
            leads.append(lead)
    return {"ok": True, "reason": "", "count": len(leads), "leads": leads}


# ---------------------------------------------------------------------------
# Scoring — how worth-a-call is this lead?
# ---------------------------------------------------------------------------

def score_lead(lead: Dict[str, Any]) -> int:
    """0-100 callability score. Higher = call sooner.

    Weighs the things that actually predict a productive call: a real penalty
    (there's a budget and a reason), recency (pain is fresh), whether it's a
    trade Origin serves, and confirmation level (an issued citation beats a
    news mention)."""
    score = 0

    # Penalty size — the single biggest signal of a callable lead.
    pen = _to_float(lead.get("penalty"))
    if pen >= 100000:
        score += 45
    elif pen >= 40000:
        score += 40
    elif pen >= 15000:
        score += 32
    elif pen >= 5000:
        score += 24
    elif pen > 0:
        score += 16

    # Confirmation level.
    if lead.get("kind") == "osha_citation":
        score += 25
    elif lead.get("kind") == "news_incident":
        score += 8

    # Trade match — Origin's wheelhouse.
    if lead.get("trade_match"):
        score += 18

    # Recency.
    days = _days_since(lead.get("opened") or lead.get("seendate"))
    if days is not None:
        if days <= 14:
            score += 12
        elif days <= 30:
            score += 8
        elif days <= 60:
            score += 4

    return max(0, min(100, score))


def _days_since(datestr: str) -> Optional[int]:
    if not datestr:
        return None
    s = str(datestr).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y%m%dT%H%M%SZ", "%Y%m%d", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(s[:len(time.strftime(fmt))] if fmt.endswith("d") else s, fmt)
            return max(0, (datetime.utcnow() - dt).days)
        except Exception:
            continue
    # GDELT seendate like 20260830T120000Z
    m = re.match(r"(\d{8})", s)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y%m%d")
            return max(0, (datetime.utcnow() - dt).days)
        except Exception:
            return None
    return None


def _priority(score: int) -> str:
    if score >= 70:
        return "hot"
    if score >= 45:
        return "warm"
    return "watch"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _lead_dedupe_key(lead: Dict[str, Any]) -> str:
    if lead.get("activity_nr"):
        return "osha:" + str(lead["activity_nr"])
    if lead.get("url"):
        return "url:" + lead["url"]
    return "co:" + (lead.get("company", "").lower() + "|" + lead.get("state", "").lower())


def _write_lead(radar_lead: Dict[str, Any]) -> bool:
    """Append one scored radar lead to rescue_leads.jsonl in the shape the admin
    Leads view understands (source-tagged, no email so it never collides with a
    real inbound lead on the dedupe-by-email path)."""
    row = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": RADAR_SOURCE,
        "name": "",
        "company": radar_lead.get("company", ""),
        "email": "",
        "phone": "",
        "platform": "",
        # radar-specific fields, surfaced in the admin view:
        "radar_kind": radar_lead.get("kind", ""),
        "radar_label": radar_lead.get("label", ""),
        "radar_authority": radar_lead.get("authority", ""),
        "radar_penalty": radar_lead.get("penalty", 0),
        "radar_state": radar_lead.get("state", ""),
        "radar_city": radar_lead.get("city", ""),
        "radar_naics": radar_lead.get("naics", ""),
        "radar_opened": radar_lead.get("opened", "") or radar_lead.get("seendate", ""),
        "radar_score": radar_lead.get("score", 0),
        "radar_priority": radar_lead.get("priority", ""),
        "radar_url": radar_lead.get("url", ""),
        "radar_summary": radar_lead.get("summary", ""),
        "radar_trade_match": bool(radar_lead.get("trade_match")),
    }
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with LEADS_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return True
    except Exception:
        return False


def _existing_radar_keys() -> set:
    """Keys already in the leads file so a re-run doesn't duplicate leads."""
    keys: set = set()
    try:
        if not LEADS_FILE.exists():
            return keys
        with LEADS_FILE.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("source") != RADAR_SOURCE:
                    continue
                if row.get("radar_url"):
                    keys.add("url:" + row["radar_url"])
                key_co = ("co:" + (row.get("company", "").lower() + "|" +
                                   row.get("radar_state", "").lower()))
                keys.add(key_co)
    except Exception:
        pass
    return keys


def run_radar(*, states: Optional[List[str]] = None, since_days: int = 30,
              min_penalty: float = 1000.0, osha_limit: int = 200,
              news_query: str = "", news_timespan: str = "1week",
              news_max: int = 75, include_news: bool = True,
              target_trades_only: bool = False,
              min_score: int = 0, persist: bool = True) -> Dict[str, Any]:
    """Run the radar across all sources, score, dedupe, and (optionally) write
    the leads into the admin Leads view. Returns a summary + the scored leads."""
    sources: Dict[str, Any] = {}
    raw: List[Dict[str, Any]] = []

    osha = fetch_osha_leads(states=states, since_days=since_days,
                            min_penalty=min_penalty, limit=osha_limit,
                            target_trades_only=target_trades_only)
    sources["osha"] = {k: v for k, v in osha.items() if k != "leads"}
    raw.extend(osha.get("leads", []))

    if include_news:
        news = fetch_news_leads(query=news_query, timespan=news_timespan,
                                max_records=news_max)
        sources["news"] = {k: v for k, v in news.items() if k != "leads"}
        raw.extend(news.get("leads", []))

    # Score + in-run dedupe.
    seen: set = set()
    scored: List[Dict[str, Any]] = []
    for lead in raw:
        key = _lead_dedupe_key(lead)
        if key in seen:
            continue
        seen.add(key)
        lead["score"] = score_lead(lead)
        lead["priority"] = _priority(lead["score"])
        if lead["score"] >= min_score:
            scored.append(lead)

    scored.sort(key=lambda l: l["score"], reverse=True)

    written = 0
    skipped_dupe = 0
    if persist:
        existing = _existing_radar_keys()
        for lead in scored:
            k = _lead_dedupe_key(lead)
            url_k = ("url:" + lead["url"]) if lead.get("url") else None
            co_k = ("co:" + (lead.get("company", "").lower() + "|" +
                             lead.get("state", "").lower()))
            if (url_k and url_k in existing) or co_k in existing:
                skipped_dupe += 1
                continue
            if _write_lead(lead):
                written += 1
                if url_k:
                    existing.add(url_k)
                existing.add(co_k)

    counts = {
        "total": len(scored),
        "hot": sum(1 for l in scored if l["priority"] == "hot"),
        "warm": sum(1 for l in scored if l["priority"] == "warm"),
        "watch": sum(1 for l in scored if l["priority"] == "watch"),
        "osha": sum(1 for l in scored if l["kind"] == "osha_citation"),
        "news": sum(1 for l in scored if l["kind"] == "news_incident"),
    }
    return {
        "ok": True,
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sources": sources,
        "counts": counts,
        "written": written,
        "skipped_duplicates": skipped_dupe,
        "leads": scored,
    }


def radar_config_schema() -> Dict[str, Any]:
    """Describe the knobs the admin trigger exposes."""
    return {
        "states": {"type": "list[str]", "default": None,
                   "help": "US state postal codes to focus on, or empty for nationwide."},
        "since_days": {"type": "int", "default": 30,
                       "help": "How far back to look for citations."},
        "min_penalty": {"type": "float", "default": 1000.0,
                        "help": "Only citations with at least this penalty (callability filter)."},
        "include_news": {"type": "bool", "default": True,
                         "help": "Also scan recent US safety-enforcement news."},
        "target_trades_only": {"type": "bool", "default": False,
                               "help": "Restrict OSHA leads to Origin's trades (construction, oilfield, trucking, industrial)."},
        "min_score": {"type": "int", "default": 0,
                      "help": "Drop leads below this callability score."},
        "dol_key_configured": bool(DOL_API_KEY or DOL_INSPECTION_URL),
    }
