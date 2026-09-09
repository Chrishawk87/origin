"""Compliance Health — the "continuous, not one-shot" watch layer.

Prequal compliance is not a thing you pass once; it decays. Certificates of
insurance expire, EMR is re-rated every policy year, OSHA-required refresher
training lapses, DOT medical cards and Operator-Qualification records age out.
A contractor who was an "A" in January quietly slides to an "F" in June because
one dated thing rolled over and nobody was watching.

This module is the thing that watches. Given ONE subcontractor record (the flat
JSON the portal already persists) plus its bridged company_id, it produces a
ranked WATCH FEED across everything that carries an expiry date, each item tagged
with days-left, a state (expired / expiring / current, on a 30-day window), a
severity, its source, and a `fix` descriptor that maps straight to a
``prequal_fix`` generator so the renewal document can be built with one call.

It never invents a date. An item appears only when the record actually holds a
real, parseable date (or, for training, when the training engine — itself derived
from stored completion dates — raises a condition). No date on file → no item.
That is the whole discipline: warn about what is provably about to lapse, and
stay silent about what we cannot prove.

House rules, identical to every other Origin intelligence module: deterministic,
fully offline (no external model, no network), never-fabricate, file-based
(operates on the caller-owned record + docs directory), and isolated — a hiccup
reading one item can never abort the sweep.

Two public entry points:

    health_for_sub(rec, company_id=..., window_days=30)
        Pure read. Returns the ranked feed + rollup counts. Mutates nothing.

    sweep_sub(rec, company_id=..., docs_dir=..., window_days=30, do_email=True)
        The action sweep. For every item inside the window it auto-builds the
        renewal document into the sub's vault (once — deduped on a stable marker)
        and sends ONE digest email to the sub (once — so repeated sweeps don't
        spam). Mutates ``rec`` in place and returns a summary; the CALLER owns
        persistence (save the record after).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# 30-day warning window, matching the COI rollup the portal already uses.
DEFAULT_WINDOW_DAYS = 30

# Ordering for sorting/rollups (lower = more urgent).
STATE_RANK = {"expired": 0, "expiring": 1, "current": 2, "none": 3}
SEVERITY_FOR_STATE = {"expired": "high", "expiring": "medium", "current": "info"}

# Each watched item kind maps to the prequal_fix generator that renews it, plus a
# human label for the auto-built document. medical/OQ both ride the DOT
# Operator-Qualification tracker — the closest deterministic generator we have.
FIX_FOR_KIND: Dict[str, Dict[str, str]] = {
    "coi":      {"gap_id": "coi-endorsements", "label": "COI renewal & endorsements letter"},
    "training": {"gap_id": "training-records",  "label": "Training records matrix"},
    "emr":      {"gap_id": "emr-letter",        "label": "EMR verification letter"},
    "oq":       {"gap_id": "oq-individual",     "label": "Operator Qualification tracker"},
    "medical":  {"gap_id": "oq-individual",     "label": "DOT medical & qualification tracker"},
}


# ── date helpers (self-contained; no import of portal → no circular import) ────
def _parse_date(raw: Any) -> Optional[date]:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _days_until(d: Optional[date]) -> Optional[int]:
    if d is None:
        return None
    return (d - date.today()).days


def _state_for(days_left: Optional[int], window_days: int) -> str:
    """expired if past due, expiring within the window, else current."""
    if days_left is None:
        return "current"
    if days_left < 0:
        return "expired"
    if days_left <= window_days:
        return "expiring"
    return "current"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-") or "x"


def _mk_item(kind: str, part: str, label: str, *, expires: str = "",
             days_left: Optional[int] = None, state: str = "current",
             source: str = "", detail: str = "") -> Dict[str, Any]:
    fix = FIX_FOR_KIND.get(kind, {})
    return {
        "id": f"{kind}:{_slug(part)}",
        "kind": kind,
        "label": label,
        "expires": expires,
        "days_left": days_left,
        "state": state,
        "severity": SEVERITY_FOR_STATE.get(state, "info"),
        "source": source,
        "detail": detail,
        "fix": {"gap_id": fix.get("gap_id", ""), "label": fix.get("label", "")}
                if fix.get("gap_id") else None,
    }


# ── the individual watchers (each yields zero or more items; never fabricates) ─
def _coi_items(rec: Dict[str, Any], window_days: int) -> List[Dict[str, Any]]:
    """One item per COI line that carries a real expiry date."""
    out: List[Dict[str, Any]] = []
    for line in rec.get("coi", []) or []:
        d = _parse_date(line.get("expires"))
        if d is None:
            continue  # no date → not watched (never fabricate)
        dl = _days_until(d)
        state = _state_for(dl, window_days)
        name = (line.get("name") or "Insurance").strip()
        carrier = (line.get("carrier") or "").strip()
        out.append(_mk_item(
            "coi", f"{name}:{d.isoformat()}",
            name + (f" ({carrier})" if carrier else ""),
            expires=d.isoformat(), days_left=dl, state=state,
            source="Certificate of Insurance",
            detail=f"{name} expires {d.isoformat()}"
                   + (f" — {carrier}" if carrier else "") + ".",
        ))
    return out


def _emr_items(rec: Dict[str, Any], window_days: int) -> List[Dict[str, Any]]:
    """EMR is re-rated annually. Watch it only if the record carries a real
    renewal date: an explicit ``emr_expires``, or an ``emr_effective`` we can add
    twelve months to. The bare ``emr`` rate value has no date and is ignored."""
    d = _parse_date(rec.get("emr_expires"))
    if d is None:
        eff = _parse_date(rec.get("emr_effective"))
        if eff is not None:
            d = eff + timedelta(days=365)
    if d is None:
        return []
    dl = _days_until(d)
    state = _state_for(dl, window_days)
    rate = str(rec.get("emr", "") or "").strip()
    label = "Experience Modification Rate (EMR)" + (f" — current {rate}" if rate else "")
    return [_mk_item(
        "emr", d.isoformat(), label,
        expires=d.isoformat(), days_left=dl, state=state,
        source="Annual EMR / insurance re-rating",
        detail=f"EMR renews {d.isoformat()}. A fresh rating letter is required "
               "every policy year to keep prequal grades current.",
    )]


def _dated_list_items(rec: Dict[str, Any], key: str, kind: str, noun: str,
                      window_days: int) -> List[Dict[str, Any]]:
    """Generic watcher for a list of dated records (medical cards, OQ records).
    Each entry may name a person/asset via ``employee`` or ``name`` and MUST
    carry ``expires`` to be watched."""
    out: List[Dict[str, Any]] = []
    for row in rec.get(key, []) or []:
        if not isinstance(row, dict):
            continue
        d = _parse_date(row.get("expires"))
        if d is None:
            continue
        dl = _days_until(d)
        state = _state_for(dl, window_days)
        who = (row.get("employee") or row.get("name") or "").strip()
        label = f"{noun}" + (f" — {who}" if who else "")
        out.append(_mk_item(
            kind, f"{who or noun}:{d.isoformat()}", label,
            expires=d.isoformat(), days_left=dl, state=state,
            source=noun,
            detail=f"{noun}{(' for ' + who) if who else ''} expires {d.isoformat()}.",
        ))
    return out


def _renewal_items(rec: Dict[str, Any], window_days: int) -> List[Dict[str, Any]]:
    """Escape hatch for anything else the record wants watched: a ``renewals``
    list of {kind, label, expires, gap_id?} rows. Kind defaults to a generic
    'renewal'; a supplied gap_id (or a known kind) wires up the auto-fix."""
    out: List[Dict[str, Any]] = []
    for row in rec.get("renewals", []) or []:
        if not isinstance(row, dict):
            continue
        d = _parse_date(row.get("expires"))
        if d is None:
            continue
        dl = _days_until(d)
        state = _state_for(dl, window_days)
        kind = (row.get("kind") or "renewal").strip().lower()
        label = (row.get("label") or kind.title()).strip()
        item = _mk_item(
            kind if kind in FIX_FOR_KIND else "renewal",
            f"{label}:{d.isoformat()}", label,
            expires=d.isoformat(), days_left=dl, state=state,
            source="Scheduled renewal",
            detail=f"{label} expires {d.isoformat()}.",
        )
        gid = (row.get("gap_id") or "").strip()
        if gid and not item.get("fix"):
            item["fix"] = {"gap_id": gid, "label": f"Renew {label}"}
        out.append(item)
    return out


def _training_items(company_id: str) -> List[Dict[str, Any]]:
    """Expired / expiring OSHA-required training, via the training engine. That
    engine aggregates one row per condition (so a big roster can't flood the
    feed) and is itself derived from stored completion dates — no fabrication.
    Deferred + isolated import so a training bug can never break the feed."""
    if not company_id:
        return []
    try:
        from . import training_engine
        alerts = training_engine.training_alerts_for(company_id)
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for a in alerts or []:
        rule = a.get("rule", "")
        state = "expired" if rule == "training_expired" else "expiring"
        out.append(_mk_item(
            "training", a.get("part", rule), a.get("title", "Required training"),
            expires="", days_left=None, state=state,
            source="OSHA-required training",
            detail=a.get("detail", ""),
        ))
    return out


# ── the read model (pure; mutates nothing) ────────────────────────────────────
def _sorted(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def key(it: Dict[str, Any]):
        dl = it.get("days_left")
        return (
            STATE_RANK.get(it.get("state"), 9),
            dl if isinstance(dl, int) else 10**6,  # soonest first within a state
            it.get("kind", ""), it.get("label", ""),
        )
    return sorted(items, key=key)


def health_for_sub(rec: Dict[str, Any], *, company_id: str = "",
                   window_days: int = DEFAULT_WINDOW_DAYS) -> Dict[str, Any]:
    """The ranked compliance-health feed for one subcontractor. Pure function of
    the stored record (+ its bridged company_id for training). Deterministic and
    offline. Only items with a real date on file are included."""
    items: List[Dict[str, Any]] = []
    for producer in (
        lambda: _coi_items(rec, window_days),
        lambda: _emr_items(rec, window_days),
        lambda: _dated_list_items(rec, "medical", "medical", "DOT medical card", window_days),
        lambda: _dated_list_items(rec, "oq", "oq", "Operator Qualification", window_days),
        lambda: _renewal_items(rec, window_days),
        lambda: _training_items(company_id),
    ):
        try:
            items.extend(producer())
        except Exception:
            pass  # one bad producer must never sink the whole feed

    items = _sorted(items)
    counts = {"expired": 0, "expiring": 0, "current": 0}
    for it in items:
        counts[it["state"]] = counts.get(it["state"], 0) + 1
    if counts["expired"]:
        worst = "expired"
    elif counts["expiring"]:
        worst = "expiring"
    elif items:
        worst = "current"
    else:
        worst = "none"
    return {
        "ok": True,
        "sub": rec.get("slug", ""),
        "company": rec.get("company", ""),
        "company_id": company_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window_days": window_days,
        "counts": counts,
        "worst": worst,
        "action_required": bool(counts["expired"] or counts["expiring"]),
        "items": items,
    }


# ── the action sweep (auto-draft the renewal + auto-email once) ───────────────
def _actionable(feed: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [it for it in feed.get("items", [])
            if it.get("state") in ("expired", "expiring")]


def _digest_body(company: str, actionable: List[Dict[str, Any]],
                 drafted: List[Dict[str, Any]]) -> str:
    lines = [f"Hi {company or 'there'},", "",
             "Origin keeps an eye on everything in your compliance file that "
             "expires, so a lapse never quietly drops your prequal grade. A few "
             "items are coming due:", ""]
    for it in actionable:
        when = it.get("expires") or ""
        dl = it.get("days_left")
        if it.get("state") == "expired":
            tail = "EXPIRED" + (f" {abs(dl)} day(s) ago" if isinstance(dl, int) else "")
        elif isinstance(dl, int):
            tail = f"due in {dl} day(s)"
        else:
            tail = "due soon"
        lines.append(f"  \u2022 {it.get('label', 'Item')} \u2014 {tail}"
                     + (f" ({when})" if when else ""))
    if drafted:
        lines += ["", "We've already drafted the renewal paperwork for you and "
                  "placed it in your Documents, ready to review, sign and file:"]
        for d in drafted:
            lines.append(f"  \u2022 {d.get('title', 'Renewal document')}")
    lines += ["", "Sign in to your portal to review everything.", "",
              "\u2014 Origin Management Solutions",
              "info@originmanagementsolutions.com"]
    return "\n".join(lines)


def sweep_sub(rec: Dict[str, Any], *, company_id: str = "",
              docs_dir: Optional[Path] = None,
              window_days: int = DEFAULT_WINDOW_DAYS,
              do_email: bool = True) -> Dict[str, Any]:
    """Run the health feed and act on it. For each item inside the window:
    auto-build its renewal document into ``docs_dir`` (once — deduped on
    ``rec['health_drafts']``), attach it to ``rec['documents']``, then send ONE
    digest email to the sub covering the newly-actionable items (deduped on
    ``rec['health_notified']`` so repeated sweeps never spam).

    Mutates ``rec`` in place; the CALLER persists it afterward (save_client).
    Fully offline except the single outbound email, which is opt-out via
    ``do_email`` and no-ops cleanly when the mailer isn't configured."""
    feed = health_for_sub(rec, company_id=company_id, window_days=window_days)
    actionable = _actionable(feed)

    company = (rec.get("company") or "").strip() or "Company"
    scope = (rec.get("trade") or rec.get("scope") or "").strip()
    drafts_marker = rec.setdefault("health_drafts", {})
    drafted: List[Dict[str, Any]] = []

    # 1) Auto-build the renewal document for anything actionable with a known fix.
    if docs_dir is not None:
        try:
            from . import prequal_fix as _fix
            from . import compliance as _cmp
        except Exception:
            _fix = None  # type: ignore
        if _fix is not None:
            for it in actionable:
                fix = it.get("fix") or {}
                gid = fix.get("gap_id", "")
                iid = it["id"]
                if not gid or iid in drafts_marker:
                    continue
                try:
                    out = _fix.generate(gid, company=company,
                                        company_id=company_id, scope=scope)
                except Exception:
                    out = None
                if not out:
                    continue
                try:
                    docs_dir = Path(docs_dir)
                    docs_dir.mkdir(parents=True, exist_ok=True)
                    path = _cmp.unique_path(
                        docs_dir,
                        _cmp.safe_filename(out["title"]).rsplit(".", 1)[0] + ".html")
                    path.write_text(out["html"], encoding="utf-8")
                except Exception:
                    continue
                fname = path.name
                row = {
                    "name": out["title"],
                    "sub": "Built by Origin \u2014 renewal (compliance health)",
                    "file": fname, "source": "origin-health-draft",
                    "gap_id": gid, "health_item": iid,
                }
                rec.setdefault("documents", []).append(row)
                drafts_marker[iid] = {"file": fname, "title": out["title"],
                                      "drafted_at": datetime.now().isoformat(timespec="seconds")}
                drafted.append({"id": iid, "title": out["title"], "file": fname})

    # 2) One digest email — only when something is NEWLY actionable.
    notified = rec.setdefault("health_notified", {})
    prev_ids = set(notified.get("items", []) or [])
    actionable_ids = {it["id"] for it in actionable}
    new_ids = actionable_ids - prev_ids

    emailed = False
    email_error = ""
    to = (rec.get("email") or "").strip()
    if do_email and new_ids and "@" in to:
        try:
            from . import compliance as _cmp
            if _cmp.resend_configured() or _cmp.smtp_configured():
                subject = f"Compliance items coming due \u2014 {company}"
                res = _cmp.send_email(to=to, subject=subject,
                                      body=_digest_body(company, actionable, drafted))
                emailed = bool(res.get("sent"))
                email_error = res.get("error", "") if not emailed else ""
            else:
                email_error = "mailer not configured"
        except Exception as exc:  # never let email break the sweep
            email_error = str(exc)

    # Prune notified down to what's still actionable, then fold in what we just
    # covered — so a renewed item can legitimately re-notify if it lapses again.
    keep = (prev_ids & actionable_ids)
    if emailed:
        keep |= actionable_ids
    notified["items"] = sorted(keep)
    notified["at"] = datetime.now().isoformat(timespec="seconds")

    return {
        "ok": True,
        "sub": rec.get("slug", ""),
        "company": company,
        "drafted": drafted,
        "emailed": emailed,
        "email_error": email_error,
        "new_actionable": sorted(new_ids),
        "feed": feed,
    }
