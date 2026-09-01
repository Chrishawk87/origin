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
import urllib.error
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

# FMCSA (trucking) — public Socrata "Company Census File" (dataset az4n-8mr2) on
# data.transportation.gov. No key needed; an optional Socrata app token raises the
# rate limit. safety_rating: S=Satisfactory, C=Conditional, U=Unsatisfactory. A U
# (or C) carrier is exactly the profile a prime contractor's ISN/Avetta/Veriforce
# screen rejects — a strong reverse-trigger lead.
FMCSA_SOCRATA_URL = os.environ.get(
    "FMCSA_SOCRATA_URL", "https://data.transportation.gov/resource/az4n-8mr2.json").strip()
SOCRATA_APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN", "").strip()

# MSHA (mining) — violations live on the same DOL v4 data portal as OSHA, so the
# same DOL_API_KEY works. We discover the exact dataset from the public catalog at
# runtime (no hard-wired dataset name to rot). Field names are overridable in case
# the v4 schema differs from the documented column names.
DOL_DATASETS_URL = os.environ.get("DOL_DATASETS_URL", "https://apiprod.dol.gov/v4/datasets").strip()
MSHA_AGENCY = os.environ.get("MSHA_AGENCY", "MSHA").strip()
MSHA_DATASET = os.environ.get("MSHA_DATASET", "").strip()  # blank => auto-discover
MSHA_ISSUE_FIELD = os.environ.get("MSHA_ISSUE_FIELD", "violation_issue_dt").strip()
MSHA_PENALTY_FIELD = os.environ.get("MSHA_PENALTY_FIELD", "proposed_penalty").strip()

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

def _http_fetch(url: str, headers: Optional[Dict[str, str]] = None,
                timeout: int = 25) -> Dict[str, Any]:
    """GET a URL and return a rich result so callers can tell *why* something
    failed instead of collapsing everything to None:
        {"status": int|None, "body": str, "error": str}
    status = HTTP status code (or the code from an HTTPError); None if the
    request never completed (DNS/timeout/connection). error = short reason.
    """
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return {"status": getattr(resp, "status", 200) or 200,
                    "body": resp.read().decode(charset, errors="replace"),
                    "error": ""}
    except urllib.error.HTTPError as e:  # 4xx / 5xx — the server answered
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return {"status": e.code, "body": body, "error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:  # DNS / connection / timeout
        return {"status": None, "body": "", "error": f"connection: {e.reason}"}
    except Exception as e:  # anything else — never raise to the caller
        return {"status": None, "body": "", "error": str(e)[:120]}


def _http_get(url: str, headers: Optional[Dict[str, str]] = None,
              timeout: int = 25) -> Optional[str]:
    """GET a URL and return the body text, or None on any failure."""
    res = _http_fetch(url, headers=headers, timeout=timeout)
    body = res.get("body") or ""
    if res.get("status") in (None,) or (res.get("status") or 0) >= 400:
        return None
    return body or None


def _http_get_json(url: str, headers: Optional[Dict[str, str]] = None,
                   timeout: int = 25) -> Optional[Any]:
    body = _http_get(url, headers=headers, timeout=timeout)
    if not body:
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def _looks_like_json(text: str) -> bool:
    t = (text or "").lstrip()
    return t.startswith("{") or t.startswith("[")


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
    # Note: we intentionally do NOT filter delete_flag here — a `neq` against
    # null/blank flags behaves inconsistently on the DOL API. Deleted rows ('X'
    # / 'D') are dropped client-side in _aggregate_violations instead.
    filt: Dict[str, Any] = {"and": [
        {"field": DOL_ISSUANCE_FIELD, "operator": "gt", "value": since},
        {"field": DOL_PENALTY_FIELD, "operator": "gt", "value": min_penalty},
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
        # OSHA flags removed citations with delete_flag 'X' (some legacy rows 'D').
        if str(_pick(rec, "delete_flag")).upper() in ("X", "D"):
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


def _dol_fetch_records(url: str, *, timeout: int = 45) -> Dict[str, Any]:
    """Call a DOL v4 URL and return {ok, records, reason, note}. Classifies the
    real HTTP failure (bad key, rate-limit, bad filter) so the admin sees a
    fix-it message instead of a silent blank."""
    res = _http_fetch(url, timeout=timeout)
    status, body = res.get("status"), res.get("body") or ""
    if status is None:
        return {"ok": False, "records": [], "reason": "unreachable",
                "note": f"DOL endpoint unreachable ({res.get('error','no response')})."}
    if status in (401, 403):
        return {"ok": False, "records": [], "reason": "bad_key",
                "note": "DOL rejected the API key (401/403). Re-check DOL_API_KEY on Railway "
                        "(free key from dataportal.dol.gov/registration)."}
    if status == 429:
        return {"ok": False, "records": [], "reason": "rate_limited",
                "note": "DOL is rate-limiting the API key. Wait a minute and re-run."}
    if status == 400:
        snippet = " ".join(body.split())[:160]
        return {"ok": False, "records": [], "reason": "bad_request",
                "note": f"DOL rejected the query (400): {snippet}"}
    if status >= 400:
        return {"ok": False, "records": [], "reason": "http_error",
                "note": f"DOL returned HTTP {status}."}
    if not _looks_like_json(body):
        snippet = " ".join(body.split())[:160]
        return {"ok": False, "records": [], "reason": "not_json",
                "note": f"DOL returned a non-JSON body: {snippet}" if snippet
                        else "DOL returned an empty body."}
    try:
        payload = json.loads(body)
    except Exception:
        return {"ok": False, "records": [], "reason": "parse_error",
                "note": "DOL returned malformed JSON."}
    return {"ok": True, "records": _as_records(payload), "reason": "", "note": ""}


def _recent_inspections_url(*, since: str, limit: int) -> str:
    """Recent OSHA inspections by open_date (fallback signal when the
    violations-first pull comes back empty)."""
    filt: Dict[str, Any] = {"and": [
        {"field": "open_date", "operator": "gt", "value": since},
    ]}
    return _dol_url(DOL_DATASET, filt=filt, limit=limit,
                    sort_by="open_date", sort="desc")


def _violations_for_url(activity_nrs: List[str], *, limit: int) -> str:
    """Penalty-bearing violations for a set of inspection activity numbers."""
    filt: Dict[str, Any] = {"and": [
        {"field": "activity_nr", "operator": "in", "value": activity_nrs},
    ]}
    return _dol_url(DOL_VIOLATION_DATASET, filt=filt, limit=limit,
                    sort_by="activity_nr", sort="asc")


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
                "note": "OSHA needs a free DOL_API_KEY on Railway. The key needs only a basic "
                        "login.gov account (email + phone code, NO ID/driver's-license upload) — "
                        "get it at dataportal.dol.gov, then add DOL_API_KEY in Railway variables."}

    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d")
    want = set(states and [s.strip().upper() for s in states if s.strip()] or [])

    # --- Primary path: violations-first (freshest signal — penalties post ~5 days
    # after a federal citation, long before the inspection case closes). ---
    viol = _dol_fetch_records(
        _violation_url(since=since, min_penalty=min_penalty, limit=max(limit * 5, 500)))
    if not viol["ok"]:
        # A hard failure (bad key / rate-limit / rejected query) — surface it as-is
        # so Chris gets an actionable message, not a silent blank.
        return {"ok": False, "reason": viol["reason"], "leads": [],
                "note": viol["note"], "since": since}

    by_insp = _aggregate_violations(viol["records"])

    leads: List[Dict[str, Any]] = []
    fallback_used = False
    if by_insp:
        leads = _join_inspections(by_insp, want=want,
                                  target_trades_only=target_trades_only)

    # --- Fallback path: if the violations-first pull returned nothing joinable
    # (common when recent citations' inspections aren't published yet), pull
    # recent inspections directly and attach their penalty-bearing violations. ---
    if not leads:
        fb = _inspection_first_leads(since=since, min_penalty=min_penalty,
                                     want=want, target_trades_only=target_trades_only,
                                     limit=limit)
        if fb.get("leads"):
            leads = fb["leads"]
            fallback_used = True
        elif not fb.get("ok"):
            # Only report the fallback error if the primary also produced nothing.
            return {"ok": False, "reason": fb["reason"], "leads": [],
                    "note": fb["note"], "since": since}

    leads.sort(key=lambda l: (l.get("opened") or "", l.get("penalty") or 0), reverse=True)
    out = {"ok": True, "reason": "", "count": len(leads),
           "leads": leads[:limit], "since": since}
    if fallback_used:
        out["note"] = "Used recent-inspection fallback (few citations posted in this window yet)."
    return out


def _join_inspections(by_insp: Dict[str, Dict[str, Any]], *, want: set,
                      target_trades_only: bool) -> List[Dict[str, Any]]:
    """Look up establishments for aggregated inspections and build leads."""
    activity_nrs = list(by_insp.keys())
    insp_by_nr: Dict[str, Dict[str, Any]] = {}
    for chunk in _chunk(activity_nrs, 100):
        res = _dol_fetch_records(_inspection_lookup_url(chunk, limit=len(chunk)))
        for rec in res.get("records", []):
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
    return leads


def _inspection_first_leads(*, since: str, min_penalty: float, want: set,
                            target_trades_only: bool, limit: int) -> Dict[str, Any]:
    """Fallback: start from recent inspections, then attach their penalty-bearing
    violations by activity_nr. Returns {ok, leads, reason, note}."""
    insp_res = _dol_fetch_records(_recent_inspections_url(since=since, limit=max(limit * 5, 500)))
    if not insp_res["ok"]:
        return {"ok": False, "leads": [], "reason": insp_res["reason"],
                "note": insp_res["note"]}
    insp_by_nr: Dict[str, Dict[str, Any]] = {}
    for rec in insp_res["records"]:
        nr = _pick(rec, "activity_nr", "activity_number")
        if not nr:
            continue
        state = _pick(rec, "site_state", "state", "mail_state").upper()
        if want and state not in want:
            continue
        insp_by_nr[nr] = rec
    if not insp_by_nr:
        return {"ok": True, "leads": [], "reason": "", "note": ""}

    # Pull the violations for those inspections and roll them up.
    all_rows: List[Dict[str, Any]] = []
    for chunk in _chunk(list(insp_by_nr.keys()), 50):
        vres = _dol_fetch_records(_violations_for_url(chunk, limit=len(chunk) * 20))
        all_rows.extend(vres.get("records", []))
    by_insp = _aggregate_violations(all_rows)

    leads: List[Dict[str, Any]] = []
    for nr, agg in by_insp.items():
        if agg.get("penalty", 0.0) < min_penalty:
            continue
        insp = insp_by_nr.get(nr)
        if not insp:
            continue
        lead = _build_osha_lead(insp, agg)
        if not lead:
            continue
        if target_trades_only and not lead["trade_match"]:
            continue
        leads.append(lead)
    return {"ok": True, "leads": leads, "reason": "", "note": ""}


# ---------------------------------------------------------------------------
# News monitoring (GDELT DOC 2.0)
# ---------------------------------------------------------------------------

# GDELT is picky: long multi-group boolean queries are silently rejected (it
# answers HTTP 200 with a plain-text error, not JSON). Keep the primary query
# lean, and hold a couple of progressively simpler fallbacks in reserve so a
# rejected/empty query still yields news instead of a blank panel.
_DEFAULT_NEWS_QUERY = 'OSHA (fined OR penalty OR citation) sourcecountry:US'

_NEWS_FALLBACK_QUERIES = [
    '"safety violation" (fine OR penalty) sourcecountry:US',
    'OSHA citation sourcecountry:US',
    'OSHA fine',
]

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


def _classify_gdelt_body(body: str) -> str:
    """GDELT signals errors as an HTTP-200 *plain-text* body. Turn that prose
    into a machine reason so we can react (retry vs give up) and tell the admin
    what actually happened."""
    t = (body or "").strip().lower()
    if not t:
        return "empty"
    if "rate" in t and "limit" in t:
        return "rate_limited"
    if "too many" in t or "please wait" in t or "try again" in t:
        return "rate_limited"
    if "maximum" in t and "records" in t:
        return "maxrecords"
    if ("query" in t and ("too short" in t or "too long" in t or "invalid" in t
                          or "specified" in t or "syntax" in t)):
        return "query_error"
    if "phrase" in t:
        return "query_error"
    return "query_error"  # any other prose GDELT returns is a rejected query


def _gdelt_once(q: str, *, timespan: str, max_records: int) -> Dict[str, Any]:
    """One GDELT call. Returns {ok, leads, reason, note, raw}."""
    params = {
        "query": q,
        "mode": "artlist",
        "format": "json",
        # GDELT caps ArtList at 250; asking for more triggers a prose error.
        "maxrecords": str(max(1, min(max_records, 250))),
        "timespan": timespan,
        "sort": "datedesc",
    }
    url = GDELT_URL + "?" + urllib.parse.urlencode(params)
    res = _http_fetch(url, timeout=30)
    status, body = res.get("status"), res.get("body") or ""

    if status is None:  # never connected
        return {"ok": False, "reason": "unreachable", "leads": [],
                "note": f"GDELT unreachable ({res.get('error','no response')})."}
    if status == 429:
        return {"ok": False, "reason": "rate_limited", "leads": [],
                "note": "GDELT is rate-limiting us (free tier — a few calls/hour). Try again shortly."}
    if status >= 400:
        return {"ok": False, "reason": "http_error", "leads": [],
                "note": f"GDELT returned HTTP {status}."}

    # Status 200 — but GDELT may have answered with a plain-text error instead of JSON.
    if not _looks_like_json(body):
        reason = _classify_gdelt_body(body)
        snippet = " ".join(body.split())[:160]
        if reason == "empty":
            return {"ok": True, "reason": "", "count": 0, "leads": []}
        note = {
            "rate_limited": "GDELT is rate-limiting us (free tier). Try again in a few minutes.",
            "maxrecords": "GDELT record cap hit — lower the news count.",
            "query_error": f"GDELT rejected the news query ({snippet}).",
        }.get(reason, f"GDELT error: {snippet}")
        return {"ok": False, "reason": reason, "leads": [], "note": note}

    try:
        payload = json.loads(body)
    except Exception:
        return {"ok": False, "reason": "parse_error", "leads": [],
                "note": "GDELT returned malformed JSON."}

    leads: List[Dict[str, Any]] = []
    for rec in _as_records(payload):
        lead = normalize_news_record(rec)
        if lead:
            leads.append(lead)
    return {"ok": True, "reason": "", "count": len(leads), "leads": leads}


def fetch_news_leads(*, query: str = "", timespan: str = "1week",
                     max_records: int = 75) -> Dict[str, Any]:
    """Pull recent US safety-enforcement news via GDELT (free, keyless).

    Robust against GDELT's two quirks: it reports errors as HTTP-200 plain text
    (not JSON), and its free tier rate-limits aggressively. We try the caller's
    query (or a lean default), retry once after a short pause on a rate-limit,
    and fall back through progressively simpler queries if GDELT rejects one —
    so the panel returns news instead of a blank error whenever possible.
    """
    primary = query.strip() or _DEFAULT_NEWS_QUERY
    # If the caller passed a custom query, only fall back to the defaults.
    candidates = [primary] + [q for q in _NEWS_FALLBACK_QUERIES if q != primary]

    last: Dict[str, Any] = {"ok": False, "reason": "fetch_failed", "leads": [],
                            "note": "GDELT news endpoint did not return data."}
    for i, q in enumerate(candidates):
        res = _gdelt_once(q, timespan=timespan, max_records=max_records)
        if res.get("ok") and res.get("leads"):
            if i > 0:
                res["note"] = "Used a simpler news query (GDELT rejected the detailed one)."
            return res
        if res.get("ok"):  # ok but zero leads — keep as the answer, try a broader query next
            last = res
            continue
        if res.get("reason") == "rate_limited":
            time.sleep(2)
            res2 = _gdelt_once(q, timespan=timespan, max_records=max_records)
            if res2.get("ok") and res2.get("leads"):
                return res2
            last = res2 if res2.get("reason") else res
            # Don't hammer a rate-limited endpoint through every fallback.
            break
        last = res  # query_error / http_error — try the next simpler candidate
    return last


def _norm_date(s: str) -> str:
    """Normalize a date string (mm/dd/yyyy or ISO) to YYYY-MM-DD for scoring/sorting."""
    s = (s or "").strip()
    if not s:
        return ""
    for cand, fmt in ((s[:10], "%Y-%m-%d"), (s[:10], "%m/%d/%Y"),
                      (s[:19], "%Y-%m-%dT%H:%M:%S"), (s[:19], "%m/%d/%Y %H:%M:%S")):
        try:
            return datetime.strptime(cand, fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    return s[:10]


# ---------------------------------------------------------------------------
# FMCSA (trucking) — a poor safety rating is a direct ISN/Avetta drop trigger
# ---------------------------------------------------------------------------

_FMCSA_RATING_LABEL = {"U": "Unsatisfactory", "C": "Conditional", "S": "Satisfactory"}


def _fmcsa_url(*, ratings: tuple, states: Optional[List[str]], limit: int) -> str:
    """Build a Socrata SODA query for carriers with the given safety ratings."""
    where = "safety_rating in(%s)" % ",".join("'%s'" % r for r in ratings)
    st = [s.strip().upper() for s in (states or []) if s.strip()]
    if st:
        where += " AND phy_state in(%s)" % ",".join("'%s'" % s for s in st)
    params = {"$where": where, "$order": "safety_rating_date DESC", "$limit": str(limit)}
    if SOCRATA_APP_TOKEN:
        params["$$app_token"] = SOCRATA_APP_TOKEN
    return FMCSA_SOCRATA_URL + "?" + urllib.parse.urlencode(params)


def _fmcsa_summary(lead: Dict[str, Any]) -> str:
    rate = _FMCSA_RATING_LABEL.get(lead.get("rating", ""), "flagged")
    parts = [f"FMCSA {rate} safety rating"]
    where = ", ".join(p for p in (lead.get("city"), lead.get("state")) if p)
    if where:
        parts.append(where)
    if lead.get("opened"):
        parts.append(f"rated {lead['opened']}")
    parts.append("likely dropped by ISN/Avetta/Veriforce")
    return " \u2014 ".join(parts)


def _build_fmcsa_lead(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    company = _pick(rec, "legal_name", "dba_name", "name")
    if not company:
        return None
    dot = _pick(rec, "dot_number", "usdot", "dot")
    lead = {
        "kind": "fmcsa_rating",
        "label": "safety rating",
        "company": company,
        "authority": "FMCSA",
        "penalty": 0.0,
        "rating": _pick(rec, "safety_rating").upper()[:1],
        "state": _pick(rec, "phy_state", "state").upper(),
        "city": _pick(rec, "phy_city", "city"),
        "address": _pick(rec, "phy_street", "address"),
        "zip": _pick(rec, "phy_zip", "zip"),
        "opened": _norm_date(_pick(rec, "safety_rating_date", "review_date")),
        "dot_number": dot,
        "power_units": _pick(rec, "power_units"),
        "trade_match": True,  # trucking is squarely an Origin trade
        "url": (f"https://safer.fmcsa.dot.gov/query.asp?searchtype=ANY&query_type="
                f"queryCarrierSnapshot&query_param=USDOT&query_string={dot}" if dot else ""),
    }
    lead["summary"] = _fmcsa_summary(lead)
    return lead


def fetch_fmcsa_leads(*, states: Optional[List[str]] = None,
                      ratings: tuple = ("U", "C"), limit: int = 200) -> Dict[str, Any]:
    """Carriers with Unsatisfactory/Conditional FMCSA safety ratings — the exact
    profile a prime contractor's ISN/Avetta/Veriforce screen rejects. Pulled from
    the public FMCSA census on data.transportation.gov (no key needed)."""
    url = _fmcsa_url(ratings=ratings, states=states, limit=max(1, min(limit * 3, 1000)))
    res = _http_fetch(url, timeout=40)
    status, body = res.get("status"), res.get("body") or ""
    if status is None:
        return {"ok": False, "reason": "unreachable", "leads": [],
                "note": f"FMCSA data portal unreachable ({res.get('error', 'no response')})."}
    if status == 429:
        return {"ok": False, "reason": "rate_limited", "leads": [],
                "note": "FMCSA data portal rate-limited us. Set SOCRATA_APP_TOKEN for a higher limit."}
    if status == 400:
        snippet = " ".join(body.split())[:160]
        return {"ok": False, "reason": "bad_request", "leads": [],
                "note": f"FMCSA query rejected (field names may have changed): {snippet}"}
    if status >= 400:
        return {"ok": False, "reason": "http_error", "leads": [],
                "note": f"FMCSA data portal returned HTTP {status}."}
    if not _looks_like_json(body):
        return {"ok": False, "reason": "not_json", "leads": [],
                "note": "FMCSA data portal returned a non-JSON body."}
    try:
        payload = json.loads(body)
    except Exception:
        return {"ok": False, "reason": "parse_error", "leads": [],
                "note": "FMCSA data portal returned malformed JSON."}
    want = set(s.strip().upper() for s in (states or []) if s.strip())
    leads: List[Dict[str, Any]] = []
    for rec in _as_records(payload):
        lead = _build_fmcsa_lead(rec)
        if not lead:
            continue
        if want and lead["state"] not in want:
            continue
        leads.append(lead)
    leads.sort(key=lambda l: l.get("opened") or "", reverse=True)
    return {"ok": True, "reason": "", "count": len(leads), "leads": leads[:limit]}


# ---------------------------------------------------------------------------
# MSHA (mining) — recent violations via the DOL v4 data portal (same key as OSHA)
# ---------------------------------------------------------------------------

_MSHA_CACHE: Dict[str, Any] = {}


def _dol_datasets() -> List[Dict[str, Any]]:
    """The public DOL v4 dataset catalog (no key). Cached for the process life."""
    if "list" in _MSHA_CACHE:
        return _MSHA_CACHE["list"]
    payload = _http_get_json(DOL_DATASETS_URL, timeout=30)
    lst = _as_records(payload)
    _MSHA_CACHE["list"] = lst
    return lst


def _discover_msha_dataset() -> str:
    """Find the current MSHA violations dataset's api_url from the catalog, or ''."""
    if MSHA_DATASET:
        return MSHA_DATASET
    for rec in _dol_datasets():
        agency = _pick(rec, "agency", "agency_abbr", "agencyAbbreviation").upper()
        api_url = _pick(rec, "api_url", "apiUrl", "endpoint", "url", "name")
        blob = (_pick(rec, "name", "dataset", "title") + " " + api_url).lower()
        if agency == MSHA_AGENCY.upper() and "violation" in blob:
            if "archive" in blob or "1978" in blob:
                continue  # skip the 1978-2000 archive; we want the current file
            return api_url or _pick(rec, "name")
    return ""


def _msha_summary(lead: Dict[str, Any]) -> str:
    parts = ["MSHA violation"]
    if lead.get("penalty"):
        parts.append(f"${lead['penalty']:,.0f} penalty")
    if lead.get("mine_name"):
        parts.append(f"at {lead['mine_name']}")
    if lead.get("state"):
        parts.append(lead["state"])
    if lead.get("opened"):
        parts.append(f"issued {lead['opened']}")
    return " \u2014 ".join(parts)


def _build_msha_lead(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    company = _pick(rec, "violator_name", "operator_name", "contractor_name", "mine_name")
    if not company:
        return None
    penalty = _to_float(_pick(rec, MSHA_PENALTY_FIELD, "proposed_penalty",
                              "amount_due", default="0"))
    issued = _pick(rec, MSHA_ISSUE_FIELD, "violation_issue_dt", "violation_occur_dt")
    lead = {
        "kind": "msha_violation",
        "label": "confirmed violation",
        "company": company,
        "authority": "MSHA",
        "penalty": round(penalty, 2),
        "state": _pick(rec, "state", "mine_state", "fips_state_cd").upper()[:2],
        "city": "",
        "address": "",
        "zip": "",
        "opened": _norm_date(issued),
        "mine_name": _pick(rec, "mine_name"),
        "mine_id": _pick(rec, "mine_id"),
        "trade_match": True,
        # No reliable per-mine deep link; leave url empty so dedupe keys on company.
        "url": "",
    }
    lead["summary"] = _msha_summary(lead)
    return lead


def fetch_msha_leads(*, states: Optional[List[str]] = None, since_days: int = 30,
                     min_penalty: float = 1000.0, limit: int = 200) -> Dict[str, Any]:
    """Recent penalty-bearing MSHA violations as radar leads. Uses the same DOL
    v4 API + key as OSHA; the exact dataset is discovered from the public catalog.
    Dates on this table are strings, so recency is filtered client-side."""
    if not DOL_API_KEY:
        return {"ok": False, "reason": "no_dol_key", "leads": [],
                "note": "Set DOL_API_KEY (free from dataportal.dol.gov) to enable MSHA pulls."}
    api_url = _discover_msha_dataset()
    if not api_url:
        return {"ok": False, "reason": "unavailable", "leads": [],
                "note": "MSHA violations dataset not found in the DOL catalog "
                        "(set MSHA_DATASET on Railway to override)."}
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d")
    filt = {"and": [{"field": MSHA_PENALTY_FIELD, "operator": "gt", "value": min_penalty}]}
    base_params = {"limit": str(max(limit * 5, 500)), "offset": "0",
                   "filter_object": json.dumps(filt, separators=(",", ":")),
                   "X-API-KEY": DOL_API_KEY}

    def _url(sort_by: str) -> str:
        p = dict(base_params)
        if sort_by:
            p["sort"] = "desc"
            p["sort_by"] = sort_by
        return f"{DOL_V4_BASE}/{MSHA_AGENCY}/{api_url}/json?" + urllib.parse.urlencode(p)

    res = _dol_fetch_records(_url(MSHA_ISSUE_FIELD))
    if not res["ok"] and res["reason"] == "bad_request":
        res = _dol_fetch_records(_url(""))  # field not sortable — retry unsorted
    if not res["ok"]:
        return {"ok": False, "reason": res["reason"], "leads": [], "note": res["note"]}

    want = set(s.strip().upper() for s in (states or []) if s.strip())
    leads: List[Dict[str, Any]] = []
    for rec in res["records"]:
        lead = _build_msha_lead(rec)
        if not lead:
            continue
        days = _days_since(lead["opened"])
        if days is not None and days > since_days:
            continue
        if want and lead["state"] and lead["state"] not in want:
            continue
        leads.append(lead)
    leads.sort(key=lambda l: l.get("opened") or "", reverse=True)
    return {"ok": True, "reason": "", "count": len(leads), "leads": leads[:limit], "since": since}


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
    elif lead.get("kind") == "msha_violation":
        score += 22
    elif lead.get("kind") == "fmcsa_rating":
        # No fine attached, but a poor rating is a direct prequal-failure trigger —
        # the exact reason ISN/Avetta/Veriforce drop a carrier.
        r = (lead.get("rating") or "").upper()
        score += 34 if r == "U" else 22 if r == "C" else 10
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
    if lead.get("dot_number"):
        return "fmcsa:" + str(lead["dot_number"])
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
        "radar_address": radar_lead.get("address", ""),
        "radar_naics": radar_lead.get("naics", ""),
        "radar_rating": radar_lead.get("rating", ""),
        "radar_mine": radar_lead.get("mine_name", ""),
        "radar_dot": radar_lead.get("dot_number", ""),
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
              include_fmcsa: bool = True, include_msha: bool = True,
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

    if include_fmcsa:
        fmcsa = fetch_fmcsa_leads(states=states, limit=osha_limit)
        sources["fmcsa"] = {k: v for k, v in fmcsa.items() if k != "leads"}
        raw.extend(fmcsa.get("leads", []))

    if include_msha:
        msha = fetch_msha_leads(states=states, since_days=since_days,
                                min_penalty=min_penalty, limit=osha_limit)
        sources["msha"] = {k: v for k, v in msha.items() if k != "leads"}
        raw.extend(msha.get("leads", []))

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
        "fmcsa": sum(1 for l in scored if l["kind"] == "fmcsa_rating"),
        "msha": sum(1 for l in scored if l["kind"] == "msha_violation"),
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
        "include_fmcsa": {"type": "bool", "default": True,
                          "help": "Also pull trucking carriers with Unsat/Conditional FMCSA safety ratings (ISN/Avetta drop triggers)."},
        "include_msha": {"type": "bool", "default": True,
                         "help": "Also pull recent penalty-bearing MSHA (mining) violations."},
        "target_trades_only": {"type": "bool", "default": False,
                               "help": "Restrict OSHA leads to Origin's trades (construction, oilfield, trucking, industrial)."},
        "min_score": {"type": "int", "default": 0,
                      "help": "Drop leads below this callability score."},
        "dol_key_configured": bool(DOL_API_KEY or DOL_INSPECTION_URL),
    }


# ---------------------------------------------------------------------------
# CSV export — a clean, one-row-per-lead sheet for outreach / mail-merge
# ---------------------------------------------------------------------------

_CSV_COLUMNS = ["Company Name", "Street Address", "City", "State", "Zip",
                "Violation Type", "Penalty", "Date Issued", "Source"]


def _lead_source_label(lead: Dict[str, Any]) -> str:
    k = lead.get("kind")
    if k == "osha_citation":
        return lead.get("authority", "") or "OSHA"
    if k == "msha_violation":
        return "MSHA (Federal)"
    if k == "fmcsa_rating":
        return "FMCSA (Federal)"
    if k == "news_incident":
        return "News report"
    return "Lead Radar"


def _lead_violation_label(lead: Dict[str, Any]) -> str:
    k = lead.get("kind")
    if k == "osha_citation":
        vt = lead.get("viol_types") or []
        return ("/".join(vt) + " citation") if vt else "OSHA citation"
    if k == "msha_violation":
        return "MSHA violation"
    if k == "fmcsa_rating":
        return _FMCSA_RATING_LABEL.get(lead.get("rating", ""), "Flagged") + " safety rating"
    if k == "news_incident":
        return (lead.get("title", "") or "")[:120]
    return ""


def leads_to_csv(leads: List[Dict[str, Any]]) -> str:
    """Render scored leads as a CSV string with Chris's exact column set."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_CSV_COLUMNS)
    for lead in leads:
        pen = _to_float(lead.get("penalty"))
        w.writerow([
            lead.get("company", ""),
            lead.get("address", ""),
            lead.get("city", ""),
            lead.get("state", ""),
            lead.get("zip", ""),
            _lead_violation_label(lead),
            (f"{pen:.0f}" if pen else ""),
            lead.get("opened", "") or lead.get("seendate", ""),
            _lead_source_label(lead),
        ])
    return buf.getvalue()


def run_radar_csv(**kwargs: Any) -> str:
    """Convenience: run the radar (no persistence by default) and return a CSV."""
    kwargs.setdefault("persist", False)
    result = run_radar(**kwargs)
    return leads_to_csv(result.get("leads", []))
