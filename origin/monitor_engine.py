"""Monitor Engine — the Safety Intelligence Engine's autonomous monitoring layer
(Phase 6, the capstone).

Phases 1-5 answer questions when someone asks them: analyze this citation, score
this company, build this program set, audit these photos, estimate this prequal
grade. Phase 6 is the layer that watches the whole portfolio WITHOUT being asked
and raises a hand when something needs a human — the difference between a tool you
open and a system that keeps working while you don't.

It is a deterministic SWEEP. On each run it walks every profiled company and
applies a fixed set of rule-based monitors to the state the earlier phases already
persisted (open CAPAs, abatement deadlines, human-review flags, risk band, missing
mandated programs). Each monitor emits zero or more ALERTS. Alerts have stable ids,
so the feed is deduplicated and DELTA-AWARE across runs:

    new        — a condition that wasn't flagged on the previous sweep
    continuing — still true since a prior sweep (first_seen preserved)
    resolved   — was open, no longer true → auto-closed this sweep

Nothing is invented. Every alert is derived by rule from stored records and carries
the finding's source refs forward where they exist. There is no model in the loop,
no network call, and no background thread — the sweep is triggered explicitly (an
endpoint the schedule feature or an external cron can hit on a cadence), so it can
never spin up work that breaks the live app.

House rules, identical to every other SIE module: deterministic, fully offline,
never-fabricate, single-classification, file-based on the persistent volume, and
isolated + non-fatal registration.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import company_profile as company
from . import capa
from . import program_engine as program

try:
    from .paths import DATA_DIR
except ImportError:  # bare import in ad-hoc scripts
    import os as _os
    DATA_DIR = Path(_os.environ.get("ORIGIN_DATA_DIR") or (Path.home() / ".origin"))

MONITOR_DIR = DATA_DIR / "monitoring"
ALERTS_PATH = MONITOR_DIR / "alerts.json"
META_PATH = MONITOR_DIR / "meta.json"

# Severity ordering for sorting/rollups (lower = more urgent).
SEVERITY_RANK = {"high": 0, "medium": 1, "info": 2}

# Default window (days) for "abatement deadline approaching" alerts.
DEFAULT_DUE_SOON_DAYS = 14


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_dir() -> None:
    MONITOR_DIR.mkdir(parents=True, exist_ok=True)


def _alert_id(cid: str, rule: str, part: str = "") -> str:
    raw = f"{cid}::{rule}::{part}".strip(":")
    return re.sub(r"[^A-Za-z0-9:._-]+", "-", raw)


# ── persistence (a single keyed store of every alert we've ever raised) ───────
def _load_store() -> Dict[str, Dict[str, Any]]:
    if not ALERTS_PATH.exists():
        return {}
    try:
        data = json.loads(ALERTS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_store(store: Dict[str, Dict[str, Any]]) -> None:
    _ensure_dir()
    ALERTS_PATH.write_text(json.dumps(store, indent=2), encoding="utf-8")


def _load_meta() -> Dict[str, Any]:
    if not META_PATH.exists():
        return {}
    try:
        return json.loads(META_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_meta(meta: Dict[str, Any]) -> None:
    _ensure_dir()
    META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")


# ── the monitors (each returns a list of current-state alert dicts) ───────────
def _mk(cid: str, comp: str, rule: str, part: str, severity: str, title: str,
        detail: str, *, capa_id: str = "", standard: str = "",
        source_refs: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    return {
        "id": _alert_id(cid, rule, part),
        "company_id": cid,
        "company": comp,
        "rule": rule,
        "severity": severity if severity in SEVERITY_RANK else "medium",
        "title": title,
        "detail": detail,
        "capa_id": capa_id,
        "standard": standard,
        "source_refs": source_refs or [],
    }


def _days_until(date_str: str) -> Optional[int]:
    d = capa._parse_date(date_str)
    if not d:
        return None
    return (d - datetime.now(timezone.utc).date()).days


def _monitor_company(prof: Dict[str, Any], *, due_soon_days: int) -> List[Dict[str, Any]]:
    """Every current-state alert for one company. Pure function of stored state."""
    cid = prof.get("company_id", "")
    comp = prof.get("company", "")
    alerts: List[Dict[str, Any]] = []

    open_capas = capa.list_open_for_company(cid)

    # 1) CAPA past its OSHA abatement deadline — the most urgent, per CAPA.
    for c in open_capas:
        if capa.is_overdue(c):
            alerts.append(_mk(
                cid, comp, "capa_overdue", c.get("id", ""), "high",
                "Corrective action past its OSHA abatement deadline",
                f"\"{(c.get('title') or '')[:120]}\" was due {c.get('abatement_date','')} "
                f"and is not yet Verified Effective.",
                capa_id=c.get("id", ""), standard=c.get("standard", ""),
                source_refs=c.get("source_refs", []),
            ))
        else:
            # 2) Deadline approaching within the window (not yet overdue).
            dleft = _days_until(c.get("abatement_date", ""))
            if dleft is not None and 0 <= dleft <= due_soon_days:
                alerts.append(_mk(
                    cid, comp, "capa_due_soon", c.get("id", ""), "medium",
                    f"Corrective action due in {dleft} day(s)",
                    f"\"{(c.get('title') or '')[:120]}\" is due {c.get('abatement_date','')}.",
                    capa_id=c.get("id", ""), standard=c.get("standard", ""),
                    source_refs=c.get("source_refs", []),
                ))
        # 3) Finding awaiting human review — never let it sit silently.
        if c.get("human_review_required"):
            alerts.append(_mk(
                cid, comp, "capa_human_review", c.get("id", ""), "high",
                "Finding awaiting human review",
                f"\"{(c.get('title') or '')[:120]}\" could not be deterministically "
                "matched to a source and needs a human decision.",
                capa_id=c.get("id", ""), standard=c.get("standard", ""),
            ))

    # 4) Elevated risk band from the deterministic risk engine.
    risk = company.compute_risk(cid)
    band = risk.get("band")
    if band in ("High", "Critical"):
        alerts.append(_mk(
            cid, comp, "risk_elevated", "band",
            "high" if band == "Critical" else "medium",
            f"Risk band is {band}",
            f"Deterministic risk score {risk.get('score')} ({band}) from "
            f"{risk.get('open_capas')} open corrective action(s), "
            f"{risk.get('overdue_capas')} overdue.",
        ))

    # 5) Mandated written programs the library still can't supply.
    try:
        pkg = program.build_package(cid) or {}
        missing = int((pkg.get("summary", {}) or {}).get("programs_mandated_missing", 0) or 0)
        mandated = int((pkg.get("summary", {}) or {}).get("programs_mandated", 0) or 0)
        if missing > 0:
            alerts.append(_mk(
                cid, comp, "programs_missing", "package", "high",
                f"{missing} of {mandated} mandated written program(s) not assembled",
                "OSHA-mandated written programs are in scope but missing from the "
                "Origin package — a direct prequal program-review deficiency.",
            ))
    except Exception:
        pass  # a scoping hiccup on one company must never abort the sweep

    return alerts


# ── the sweep (delta-aware; deduplicated by stable alert id) ──────────────────
def sweep(*, due_soon_days: int = DEFAULT_DUE_SOON_DAYS, by: str = "system") -> Dict[str, Any]:
    """Run every monitor over every profiled company, reconcile against the stored
    feed, and return a delta summary (new / continuing / resolved) plus the full
    open feed. Deterministic: same stored state in → same alerts out."""
    store = _load_store()
    prior_open = {aid for aid, a in store.items() if a.get("status") == "open"}

    # Current-state alerts across the whole portfolio.
    current: Dict[str, Dict[str, Any]] = {}
    companies = 0
    for prof in company.list_all():
        companies += 1
        for a in _monitor_company(prof, due_soon_days=due_soon_days):
            current[a["id"]] = a  # stable id dedupes

    ts = _now()
    new_ids: List[str] = []
    continuing_ids: List[str] = []

    # Upsert current alerts.
    for aid, a in current.items():
        existing = store.get(aid)
        if existing and existing.get("status") == "open":
            existing.update({
                "severity": a["severity"], "title": a["title"], "detail": a["detail"],
                "capa_id": a["capa_id"], "standard": a["standard"],
                "source_refs": a["source_refs"], "last_seen": ts,
            })
            existing.pop("resolved_at", None)
            continuing_ids.append(aid)
        else:
            a.update({"status": "open", "first_seen": ts, "last_seen": ts})
            a.pop("resolved_at", None)
            store[aid] = a
            new_ids.append(aid)

    # Auto-resolve anything that was open but is no longer a current condition.
    resolved_ids: List[str] = []
    for aid in prior_open:
        if aid not in current:
            rec = store.get(aid)
            if rec:
                rec["status"] = "resolved"
                rec["resolved_at"] = ts
                resolved_ids.append(aid)

    _save_store(store)

    open_feed = _sorted([a for a in store.values() if a.get("status") == "open"])
    meta = {
        "last_run": ts,
        "by": by,
        "due_soon_days": due_soon_days,
        "companies_scanned": companies,
        "open_total": len(open_feed),
        "new": len(new_ids),
        "continuing": len(continuing_ids),
        "resolved": len(resolved_ids),
    }
    _save_meta(meta)

    return {
        "ok": True,
        "run_at": ts,
        "companies_scanned": companies,
        "counts": {
            "open": len(open_feed),
            "new": len(new_ids),
            "continuing": len(continuing_ids),
            "resolved_this_run": len(resolved_ids),
        },
        "new": [store[i] for i in new_ids],
        "resolved_this_run": [store[i] for i in resolved_ids],
        "open_feed": open_feed,
        "method": "Deterministic portfolio sweep: per-company rule monitors "
                  "(overdue CAPA, deadline approaching, human-review, elevated "
                  "risk band, missing mandated program); stable ids dedupe; "
                  "conditions clearing auto-resolve. No model, no network.",
    }


def _sorted(alerts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(alerts, key=lambda a: (SEVERITY_RANK.get(a.get("severity"), 9),
                                         a.get("company", ""), a.get("rule", "")))


# ── read models ───────────────────────────────────────────────────────────────
def alerts(*, status: str = "open", company_id: str = "") -> List[Dict[str, Any]]:
    store = _load_store()
    out = list(store.values())
    if status and status != "all":
        out = [a for a in out if a.get("status") == status]
    if company_id:
        cid = company._slug(company_id)
        out = [a for a in out if a.get("company_id") == cid]
    return _sorted(out)


def company_alerts(company_id: str, *, status: str = "open") -> Dict[str, Any]:
    cid = company._slug(company_id)
    rows = alerts(status=status, company_id=cid)
    return {
        "company_id": cid,
        "open_total": len(rows),
        "by_severity": _severity_counts(rows),
        "alerts": rows,
    }


def _severity_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"high": 0, "medium": 0, "info": 0}
    for a in rows:
        s = a.get("severity", "medium")
        counts[s] = counts.get(s, 0) + 1
    return counts


def digest() -> Dict[str, Any]:
    """Portfolio-wide rollup of the current open alert feed — the 'what needs
    attention right now' view, worst companies first."""
    open_rows = alerts(status="open")
    per_company: Dict[str, Dict[str, Any]] = {}
    for a in open_rows:
        cid = a.get("company_id", "")
        pc = per_company.setdefault(cid, {
            "company_id": cid, "company": a.get("company", ""),
            "high": 0, "medium": 0, "info": 0, "total": 0})
        pc[a.get("severity", "medium")] = pc.get(a.get("severity", "medium"), 0) + 1
        pc["total"] += 1
    companies = sorted(per_company.values(), key=lambda x: (-x["high"], -x["total"]))
    return {
        "ok": True,
        "open_total": len(open_rows),
        "by_severity": _severity_counts(open_rows),
        "by_rule": _rule_counts(open_rows),
        "companies": companies,
        "last_run": _load_meta().get("last_run"),
    }


def _rule_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for a in rows:
        out[a.get("rule", "")] = out.get(a.get("rule", ""), 0) + 1
    return out


def last_run() -> Dict[str, Any]:
    return _load_meta()


# ── routes ────────────────────────────────────────────────────────────────────
def register_monitor(app) -> None:
    """Attach Monitor routes. Isolated + non-fatal, mirroring the other SIE
    modules. Fully offline — no route consults an external model or the network."""
    from fastapi import Body, Header
    from fastapi.responses import JSONResponse

    @app.post("/api/monitor/sweep")
    def monitor_sweep(body: dict = Body(default=None)):
        payload = body if isinstance(body, dict) else {}
        try:
            days = int(payload.get("due_soon_days", DEFAULT_DUE_SOON_DAYS))
        except (TypeError, ValueError):
            days = DEFAULT_DUE_SOON_DAYS
        try:
            return sweep(due_soon_days=days, by=payload.get("by", "owner"))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    # ── Cadence hook ─────────────────────────────────────────────────────────
    # A token-guarded twin of /sweep for an EXTERNAL scheduler (Railway cron,
    # cron-job.org, GitHub Actions) to hit on a cadence. The UI button keeps
    # using the open /sweep above; this door only opens when the operator sets
    # MONITOR_SWEEP_TOKEN and the caller presents the matching secret, so a
    # public URL can't be triggered by a stranger. Still fully deterministic:
    # it runs the exact same in-process sweep(), no model, no outbound network.
    @app.post("/api/monitor/cron-sweep")
    def monitor_cron_sweep(token: str = "",
                           x_monitor_token: str = Header(default=""),
                           body: dict = Body(default=None)):
        secret = os.environ.get("MONITOR_SWEEP_TOKEN", "").strip()
        if not secret:
            return JSONResponse(
                {"error": "cron sweep not configured; set MONITOR_SWEEP_TOKEN"},
                status_code=503)
        presented = (token or x_monitor_token or "").strip()
        if not presented or not hmac.compare_digest(presented, secret):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        payload = body if isinstance(body, dict) else {}
        try:
            days = int(payload.get("due_soon_days", DEFAULT_DUE_SOON_DAYS))
        except (TypeError, ValueError):
            days = DEFAULT_DUE_SOON_DAYS
        try:
            return sweep(due_soon_days=days, by=payload.get("by", "cron"))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/monitor/alerts")
    def monitor_alerts(status: str = "open", company_id: str = ""):
        try:
            return {"ok": True, "items": alerts(status=status, company_id=company_id)}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/monitor/digest")
    def monitor_digest():
        try:
            return digest()
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/monitor/last-run")
    def monitor_last_run():
        return {"ok": True, "meta": last_run()}

    @app.get("/api/monitor/company/{company_id}")
    def monitor_company(company_id: str, status: str = "open"):
        try:
            return {"ok": True, **company_alerts(company_id, status=status)}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)
