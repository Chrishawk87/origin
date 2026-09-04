#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OSHA local citation export  --  Origin Management Solutions / Lead Radar

Pulls recent penalty-bearing OSHA citations for ONE AREA (default: greater
Houston, Texas) straight from the official U.S. Department of Labor v4 data API,
filters them to small employers (50 employees and under), sorts them into one
CSV per trade (roofing, construction, electrical, oil & gas, and so on), and
writes the files next to this script.

WHY THIS VERSION IS FAST
------------------------
The old version pulled every citation in the whole country (~29,000 rows), then
threw all but Texas away at the very end -- 100+ slow lookups, and DOL's rate
limiter kept dropping the Texas batches. This version works the other way
around: it first asks DOL for just the inspections in YOUR state and date
window, narrows those to your metro by city/ZIP, and only then looks up the
citations for that short list. A handful of calls instead of a hundred, so the
rate-limiting basically disappears.

Everything is standard-library Python -- no pip installs, no app, no deploy.

---------------------------------------------------------------------------
HOW TO RUN
---------------------------------------------------------------------------

  Greater Houston, last 12 months (this is the default -- just run it):

      cd ~/Desktop/origin/origin
      DOL_API_KEY="paste-key-here" python3 osha_export.py

  A different metro -- give it the state and the city list, e.g. Dallas-Fort Worth:

      DOL_API_KEY="..." STATES=TX ZIP_PREFIXES="" \
      CITIES="Dallas,Fort Worth,Arlington,Irving,Plano,Garland,Mesquite" \
      python3 osha_export.py

  Whole state of Texas (no metro narrowing):

      DOL_API_KEY="..." STATES=TX CITIES="" ZIP_PREFIXES="" python3 osha_export.py

The key is the same one sitting in your Railway variables -- open Railway, click
the Origin service, Variables tab, copy the DOL_API_KEY value.

CSV files land in:   ~/Desktop/origin/origin/osha_leads/
"""

import os
import re
import csv
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ===========================================================================
# SETTINGS  --  change these if you want a wider net
# ===========================================================================

# >>> Paste your DOL API key between the quotes (or leave blank and pass it on
#     the command line as shown in the header above). <<<
DOL_API_KEY = ""

# How far back to look, in days. This now applies to the INSPECTION open date
# (when OSHA opened the case). A local metro produces far fewer citations than
# the whole country, so the default is a full year -- widen or narrow freely.
# Command line:  LOOKBACK_DAYS=180 python3 osha_export.py
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "365") or "365")

# Only keep companies whose total penalty is at least this many dollars. A real
# penalty is what makes a lead callable; raise this to skip tiny ones.
# Command line:  MIN_PENALTY=1000 python3 osha_export.py
MIN_PENALTY = float(os.environ.get("MIN_PENALTY", "1") or "1")

# State(s) to pull, postal codes, comma-separated. Defaults to Texas. This is
# the ONLY server-side geography filter DOL offers, so it must be set.
# Command line:  STATES="TX,LA" python3 osha_export.py
STATES = [s.strip().upper() for s in os.environ.get("STATES", "TX").split(",") if s.strip()]

# ---- Metro narrowing (applied to the state results, on your machine) -------
# A company is kept if its city is in CITIES  OR  its ZIP starts with any of
# ZIP_PREFIXES. Leave BOTH blank to keep the whole state.
#
# Defaults below = the greater Houston metro. The ZIP prefixes 770/772/773/774/
# 775 blanket Harris, Fort Bend, Montgomery, Brazoria, Galveston, Liberty,
# Chambers, Waller and Austin counties -- so even oddly-spelled small towns get
# caught by ZIP even if they're not in the city list.
# Command line:  CITIES="Houston,Katy,Sugar Land" ZIP_PREFIXES="770,774" ...
CITIES = [c.strip().upper() for c in os.environ.get(
    "CITIES",
    "HOUSTON,KATY,SUGAR LAND,MISSOURI CITY,STAFFORD,RICHMOND,ROSENBERG,"
    "PEARLAND,FRIENDSWOOD,LEAGUE CITY,WEBSTER,PASADENA,DEER PARK,LA PORTE,"
    "BAYTOWN,CHANNELVIEW,HUMBLE,KINGWOOD,ATASCOCITA,SPRING,THE WOODLANDS,"
    "CONROE,TOMBALL,CYPRESS,FULSHEAR,BELLAIRE,GALENA PARK,SOUTH HOUSTON,"
    "TEXAS CITY,DICKINSON,LA MARQUE,SANTA FE,ALVIN,ANGLETON,LAKE JACKSON,"
    "FREEPORT,CLUTE,GALVESTON,SEABROOK,KEMAH,MANVEL,ROSHARON,WALLER,"
    "HOCKLEY,MAGNOLIA,PORTER,NEW CANEY,SPLENDORA,DAYTON,LIBERTY,MONT BELVIEU"
).split(",") if c.strip()]

ZIP_PREFIXES = [z.strip() for z in os.environ.get(
    "ZIP_PREFIXES", "770,772,773,774,775").split(",") if z.strip()]

# "50 employees and lower." Rows whose employee count is unknown/blank are KEPT
# (many OSHA rows don't list a size) and marked so you can see which is which.
MAX_EMPLOYEES = 50
KEEP_UNKNOWN_SIZE = True

# Safety caps so a pull can't run forever.
MAX_INSPECTION_ROWS = 40000
# Rows per page. Inspection rows are wide, and asking DOL's gateway for 5,000 of
# them at once makes it time out with a 502. 1,500 is comfortably under the limit.
PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "1500") or "1500")
# Activity numbers per citation-lookup call. Kept small on purpose: every id
# lengthens the request URL, and DOL's gateway rejects any URL of 2048+ chars
# with a 403. Measured: 80 ids -> ~1,560-char URL (safe); 150 ids -> ~2,680 (403).
LOOKUP_CHUNK = 80

# ===========================================================================
# DOL v4 endpoint  --  the real, official OSHA enforcement API
# ===========================================================================

DOL_V4_BASE = "https://apiprod.dol.gov/v4/get"
DOL_AGENCY = "OSHA"
VIOLATION_DATASET = "violation"     # penalty + issuance_date, no company name
INSPECTION_DATASET = "inspection"   # company/location/naics/employee count
PENALTY_FIELD = "current_penalty"
ISSUANCE_FIELD = "issuance_date"
OPEN_DATE_FIELD = "open_date"

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "osha_leads")

# ---------------------------------------------------------------------------
# Trade buckets.  A citation is assigned to the FIRST bucket whose NAICS prefix
# matches -- so the order matters (specific trades before the general catch-all).
# ---------------------------------------------------------------------------
TRADE_BUCKETS = [
    ("Oil_and_Gas",              ("211", "213", "486", "2212", "32411", "237120")),
    ("Roofing",                  ("238160",)),
    ("Metal_Welding_Fabrication",("331", "332", "238120")),
    ("Electrical",               ("238210",)),
    ("Concrete_Masonry",         ("238110", "238140", "327")),
    ("Excavation_Demolition",    ("238910",)),
    ("HVAC_Mechanical_Plumbing", ("238220",)),
    ("Construction_General",     ("236", "237", "238", "23")),
    ("Trucking_Hauling",         ("484", "488")),
    ("Manufacturing_Industrial", ("31", "32", "33")),
    ("Waste_Environmental",      ("562",)),
]
OTHER_BUCKET = "Other"

# State-plan authority names, so a lead reads "Cal/OSHA" not just "OSHA".
_STATE_PLAN_AUTHORITY = {
    "AZ": "Arizona ADOSH", "CA": "Cal/OSHA", "CT": "Connecticut OSHA",
    "HI": "Hawaii HIOSH", "IN": "Indiana IOSHA", "IA": "Iowa OSHA",
    "KY": "Kentucky OSH", "MD": "Maryland MOSH", "MI": "Michigan MIOSHA",
    "MN": "Minnesota MNOSHA", "NV": "Nevada OSHA", "NM": "New Mexico OHSB",
    "NY": "New York PESH", "NC": "North Carolina OSH", "OR": "Oregon OSHA",
    "SC": "South Carolina OSHA", "TN": "Tennessee TOSHA", "UT": "Utah UOSH",
    "VT": "Vermont VOSHA", "VA": "Virginia VOSH", "WA": "Washington DOSH (L&I)",
    "WY": "Wyoming OSHA", "PR": "Puerto Rico OSHA",
}

VIOL_TYPE_LABEL = {"S": "Serious", "W": "Willful", "R": "Repeat", "O": "Other", "U": "Unclassified"}

CSV_COLUMNS = [
    "company", "employees", "trade", "naics", "authority", "state", "city",
    "address", "zip", "penalty", "citations", "violation_types", "issued",
    "activity_nr", "osha_url",
]


# ===========================================================================
# small helpers
# ===========================================================================

def _authority(state):
    return _STATE_PLAN_AUTHORITY.get((state or "").strip().upper(), "Federal OSHA")


def _pick(rec, *names, default=""):
    for n in names:
        v = rec.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return default


def _to_float(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").strip() or 0)
    except Exception:
        return 0.0


def _to_int(v):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except Exception:
        return None


def _chunk(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _trade_for(naics):
    n = (naics or "").strip()
    if n:
        for name, prefixes in TRADE_BUCKETS:
            for p in prefixes:
                if n.startswith(p):
                    return name
    return OTHER_BUCKET


def _as_records(payload):
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "records", "rows", "items"):
            val = payload.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
        return [payload]
    return []


def in_target_area(insp):
    """True if this inspection is in the metro we want. Blank filters = whole state."""
    if not CITIES and not ZIP_PREFIXES:
        return True
    city = _pick(insp, "site_city", "city").upper()
    zc = _pick(insp, "site_zip", "zip", "zip_code")
    if CITIES and city in CITIES:
        return True
    if ZIP_PREFIXES and any(zc.startswith(p) for p in ZIP_PREFIXES):
        return True
    return False


# ===========================================================================
# DOL v4 fetch
# ===========================================================================

def _dol_url(dataset, *, filt, limit, offset, sort_by, sort="desc"):
    params = {
        "limit": str(limit),
        "offset": str(offset),
        "sort": sort,
        "sort_by": sort_by,
        "filter_object": json.dumps(filt, separators=(",", ":")),
    }
    if DOL_API_KEY:
        params["X-API-KEY"] = DOL_API_KEY
    base = f"{DOL_V4_BASE}/{DOL_AGENCY}/{dataset}/json"
    return base + "?" + urllib.parse.urlencode(params)


def _http_get(url, timeout=60):
    """Return (status, body, error). status is None if the host never answered."""
    req = urllib.request.Request(url, headers={"User-Agent": "OriginLeadExport/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace"), None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return e.code, body, None
    except Exception as e:  # DNS failure, timeout, SSL, etc.
        return None, "", f"{type(e).__name__}: {e}"


def _explain_failure(status, body, error):
    if status is None:
        return (f"Could not reach the DOL data portal ({error}). Check your internet "
                f"connection and that apiprod.dol.gov is reachable.")
    if status in (401, 403):
        return ("DOL rejected the request (HTTP %s). Usually the API key is wrong "
                "(check DOL_API_KEY for stray spaces) or the request URL got too "
                "long -- lower LOOKUP_CHUNK if you changed it." % status)
    if status == 429:
        return "DOL is rate-limiting the key (HTTP 429). Wait a minute and run again."
    if status == 400:
        return "DOL rejected the query (HTTP 400): " + " ".join(body.split())[:200]
    if status in (500, 502, 503, 504):
        return (f"DOL's server is having a moment (HTTP {status}) -- it stayed down "
                f"through every retry. This is on their end; wait a few minutes and "
                f"run the exact same command again.")
    if status and status >= 400:
        return f"DOL returned HTTP {status}."
    return f"Unexpected response (HTTP {status})."


def _dol_fetch(dataset, *, filt, limit, offset, sort_by, sort="desc"):
    """Return (records, error_message_or_None).

    We back off and retry the SAME request on transient failures instead of
    giving up -- this is what keeps a pull from dropping batches:
      * 429 = rate-limited (their limiter)
      * 502/503/504/500 = their gateway hiccuped (Bad Gateway / server busy)
    """
    url = _dol_url(dataset, filt=filt, limit=limit, offset=offset,
                   sort_by=sort_by, sort=sort)
    delay = 15
    status, body, error = None, "", None
    for attempt in range(6):
        status, body, error = _http_get(url)
        if status == 429:
            print(f"   ... DOL is rate-limiting us; waiting {delay}s then retrying "
                  f"(attempt {attempt + 1}/6)")
            time.sleep(delay)
            delay = min(delay * 2, 120)
            continue
        if status in (500, 502, 503, 504):
            print(f"   ... DOL gateway hiccup (HTTP {status}); waiting {delay}s then "
                  f"retrying (attempt {attempt + 1}/6)")
            time.sleep(delay)
            delay = min(delay * 2, 120)
            continue
        break
    if status == 204:
        return [], None  # valid query, just zero matching records
    if status != 200:
        return [], _explain_failure(status, body, error)
    body_stripped = body.lstrip()
    if not body_stripped or body_stripped[0] not in "[{":
        return [], "DOL returned a non-JSON body: " + " ".join(body.split())[:200]
    try:
        return _as_records(json.loads(body)), None
    except Exception:
        return [], "DOL returned malformed JSON."


def fetch_area_inspections(states, since_open):
    """Page through inspections in `states` opened on/after `since_open`.

    This is the narrow, up-front pull: DOL filters by state and date server-side,
    so we get a few hundred to a few thousand rows instead of the whole country.
    """
    clauses = [{"field": OPEN_DATE_FIELD, "operator": "gt", "value": since_open}]
    if states:
        clauses.append({"field": "site_state", "operator": "in", "value": states})
    filt = {"and": clauses}
    rows, offset = [], 0
    while offset < MAX_INSPECTION_ROWS:
        page, err = _dol_fetch(INSPECTION_DATASET, filt=filt, limit=PAGE_SIZE,
                               offset=offset, sort_by=OPEN_DATE_FIELD, sort="desc")
        if err:
            return rows, err
        if not page:
            break
        rows.extend(page)
        print(f"   ... pulled {len(rows):,} inspections so far")
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.3)
    return rows, None


def fetch_violations_for(activity_nrs):
    """Pull the citations (penalty + type + date) for a short list of inspections."""
    rows = []
    chunks = _chunk(activity_nrs, LOOKUP_CHUNK)
    for i, chunk in enumerate(chunks, 1):
        filt = {"and": [{"field": "activity_nr", "operator": "in", "value": chunk}]}
        page, err = _dol_fetch(VIOLATION_DATASET, filt=filt, limit=5000,
                               offset=0, sort_by="activity_nr", sort="asc")
        if err:
            print(f"   ! citation lookup {i}/{len(chunks)} failed: {err}")
            continue
        rows.extend(page)
        print(f"   ... pulled citations for chunk {i}/{len(chunks)} ({len(rows):,} rows)")
        time.sleep(0.5)
    return rows


# ===========================================================================
# aggregate + join + bucket
# ===========================================================================

def aggregate_violations(rows):
    """Roll multiple citations from one inspection into a single penalty total."""
    by_insp = {}
    for rec in rows:
        if str(_pick(rec, "delete_flag")).upper() in ("X", "D"):
            continue
        activity = _pick(rec, "activity_nr", "activity_number")
        if not activity:
            continue
        penalty = _to_float(_pick(rec, PENALTY_FIELD, "current_penalty",
                                  "initial_penalty", default="0"))
        issued = _pick(rec, ISSUANCE_FIELD, "issuance_date", "date")
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


def build_lead(insp, agg):
    company = _pick(insp, "estab_name", "establishment_name", "company", "name")
    if not company:
        return None
    state = _pick(insp, "site_state", "state", "mail_state")
    naics = _pick(insp, "naics_code", "naics")
    emp = _to_int(_pick(insp, "nr_in_estab", "number_in_estab", "num_employees"))
    types = sorted(agg.get("types") or [], key=lambda t: "WRSO".find(t) if t in "WRSO" else 9)
    activity = agg["activity_nr"]
    return {
        "company": company,
        "employees": emp if emp is not None else "",
        "trade": _trade_for(naics),
        "naics": naics,
        "authority": _authority(state),
        "state": state.upper(),
        "city": _pick(insp, "site_city", "city"),
        "address": _pick(insp, "site_address", "address"),
        "zip": _pick(insp, "site_zip", "zip", "zip_code"),
        "penalty": round(agg.get("penalty", 0.0), 2),
        "citations": agg.get("citations", 0),
        "violation_types": "/".join(VIOL_TYPE_LABEL.get(t, t) for t in types),
        "issued": agg.get("issued") or _pick(insp, "open_date"),
        "activity_nr": activity,
        "osha_url": (f"https://www.osha.gov/ords/imis/establishment.inspection_detail?id={activity}"
                     if activity else ""),
        "_emp_raw": emp,
    }


def size_ok(lead):
    emp = lead.get("_emp_raw")
    if emp is None:
        return KEEP_UNKNOWN_SIZE
    return 0 < emp <= MAX_EMPLOYEES


# ===========================================================================
# write CSVs
# ===========================================================================

def write_csvs(leads):
    os.makedirs(OUT_DIR, exist_ok=True)
    buckets = {}
    for lead in leads:
        buckets.setdefault(lead["trade"], []).append(lead)

    written = []
    for trade in [b[0] for b in TRADE_BUCKETS] + [OTHER_BUCKET]:
        rows = buckets.get(trade, [])
        if not rows:
            continue
        rows.sort(key=lambda l: (l.get("issued") or "", l.get("penalty") or 0), reverse=True)
        path = os.path.join(OUT_DIR, f"osha_{trade}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        written.append((trade, len(rows), path))

    all_rows = sorted(leads, key=lambda l: (l.get("issued") or "", l.get("penalty") or 0),
                      reverse=True)
    combined = os.path.join(OUT_DIR, "osha_ALL_leads.csv")
    with open(combined, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_rows)
    return written, combined, len(all_rows)


# ===========================================================================
# freshness diagnostic (runs only when the area pull comes back empty)
# ===========================================================================

def diagnose_freshness(states):
    """Find the newest inspection date actually present for these states."""
    clauses = [{"field": OPEN_DATE_FIELD, "operator": "gt", "value": "1900-01-01"}]
    if states:
        clauses.append({"field": "site_state", "operator": "in", "value": states})
    filt = {"and": clauses}
    sample, err = _dol_fetch(INSPECTION_DATASET, filt=filt, limit=5, offset=0,
                             sort_by=OPEN_DATE_FIELD, sort="desc")
    if err:
        print("   Could not probe the dataset: " + err)
        return
    if not sample:
        print("   No inspections at all for that state -- check the STATES value.")
        return
    latest = _pick(sample[0], OPEN_DATE_FIELD, default="?")
    print(f"   Newest inspection open date for {', '.join(states) or 'ALL'}: {latest}")
    try:
        d = datetime.strptime(str(latest)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - d).days
        print(f"   => The freshest data is about {days} days old. Try "
              f"LOOKBACK_DAYS={days + 30}.")
    except Exception:
        pass


# ===========================================================================
# main
# ===========================================================================

def main():
    global DOL_API_KEY
    DOL_API_KEY = (DOL_API_KEY or os.environ.get("DOL_API_KEY", "")).strip()
    if not DOL_API_KEY:
        print("\nERROR: No DOL API key found.\n"
              "  Paste your key on the DOL_API_KEY line near the top of this file,\n"
              "  or run:  DOL_API_KEY=\"your-key\" python3 osha_export.py\n"
              "  (Copy it from Railway -> Origin service -> Variables -> DOL_API_KEY.)\n")
        return 1

    since_open = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    if CITIES or ZIP_PREFIXES:
        area = "metro filter ON"
        if ZIP_PREFIXES:
            area += f" (ZIPs {', '.join(ZIP_PREFIXES)}x)"
        if CITIES:
            area += f" ({len(CITIES)} cities)"
    else:
        area = "whole state (no metro narrowing)"

    print(f"\nOSHA local citation export")
    print(f"  states:      {', '.join(STATES) if STATES else 'ALL'}")
    print(f"  area:        {area}")
    print(f"  window:      inspections opened on/after {since_open} ({LOOKBACK_DAYS} days)")
    print(f"  size limit:  {MAX_EMPLOYEES} employees and under"
          f"{' (unknown-size kept)' if KEEP_UNKNOWN_SIZE else ''}")
    print(f"  min penalty: ${MIN_PENALTY:,.0f}\n")

    print("1/4  Pulling inspections for your state + date window...")
    insp_rows, err = fetch_area_inspections(STATES, since_open)
    if err:
        print("\nSTOPPED: " + err + "\n")
        return 2
    if not insp_rows:
        print("\n   No inspections matched. Checking how fresh the data is...\n")
        diagnose_freshness(STATES)
        return 0
    print(f"     got {len(insp_rows):,} inspections in {', '.join(STATES) or 'ALL'}.")

    print("2/4  Narrowing to your metro...")
    by_nr = {}
    for insp in insp_rows:
        if not in_target_area(insp):
            continue
        nr = _pick(insp, "activity_nr", "activity_number")
        if nr:
            by_nr[nr] = insp
    print(f"     {len(by_nr):,} inspections inside the metro filter.")
    if not by_nr:
        print("     Nothing inside the metro filter -- widen CITIES/ZIP_PREFIXES "
              "or set them blank for the whole state.\n")
        return 0

    print("3/4  Looking up the citations (penalties) for those inspections...")
    viol_rows = fetch_violations_for(list(by_nr.keys()))
    by_insp = aggregate_violations(viol_rows)
    print(f"     {len(by_insp):,} of those inspections carry citations.")

    leads = []
    for nr, insp in by_nr.items():
        agg = by_insp.get(nr)
        if not agg:
            continue  # inspection with no penalty citation -- not a callable lead
        lead = build_lead(insp, agg)
        if not (lead and size_ok(lead) and (lead["penalty"] or 0) >= MIN_PENALTY):
            continue
        leads.append(lead)
    print(f"     {len(leads):,} callable leads after penalty + {MAX_EMPLOYEES}-employee filter.")

    print("4/4  Writing CSVs...")
    written, combined, total = write_csvs(leads)
    print(f"\nDONE.  {total:,} leads written to:\n  {OUT_DIR}\n")
    for trade, n, _ in sorted(written, key=lambda x: -x[1]):
        print(f"    {n:6,}  osha_{trade}.csv")
    print(f"    {total:6,}  osha_ALL_leads.csv  (everything combined)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
