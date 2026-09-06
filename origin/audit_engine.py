"""Walk-Through Audit Engine — the Safety Intelligence Engine's field-audit
posture layer (Phase 4).

The Photo Walk-Through tool (photo_audit.py) already does the hard, bulletproof
part: a vision model names every visible hazard in PLAIN LANGUAGE only, and
Origin — not the model — deterministically maps each hazard to the exact OSHA
standard from its own knowledge base, attaching the verbatim CFR text and the
official osha.gov link. What that tool did NOT do was remember the inspection or
connect it to a company's compliance posture. Each walk-through was a throwaway.

Phase 4 closes that loop. It takes a finished walk-through report and:

  * Persists it as a durable, company-bound AUDIT record on the /data volume,
    so a site's inspection history is a real asset (trend, re-inspect, prove
    remediation) instead of a one-off screen.
  * Auto-opens a CAPA (capa.open_from_audit_finding) for every high-severity
    finding, carrying the resolved standard's source refs forward so the chain
    "photo → hazard → OSHA standard → knowledge version" is never broken. A
    finding Origin couldn't source is opened too, but flagged for human review
    rather than force-fit to a guessed citation.
  * Feeds the existing deterministic risk engine (company_profile.compute_risk),
    which already reasons over open CAPAs — so a bad walk-through immediately and
    explainably moves the company's risk band, with no new scoring code.

Same house rules as every other SIE module: the ENGINE layer here is fully
deterministic and offline (the only AI is the vision description upstream, which
never emits a citation); file-based on the persistent volume; never fabricates;
isolated + non-fatal registration so a bug here can't break the live app.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import capa
from . import company_profile as company_mod

try:
    from .paths import DATA_DIR
except ImportError:  # bare import in ad-hoc scripts
    import os as _os
    DATA_DIR = Path(_os.environ.get("ORIGIN_DATA_DIR") or (Path.home() / ".origin"))

AUDIT_DIR = DATA_DIR / "audits"

# Severity ranking (lower number = more severe) — used to decide which findings
# clear the auto-CAPA threshold. Mirrors photo_audit's low/medium/high scale.
SEVERITY_RANK: Dict[str, int] = {"high": 0, "medium": 1, "low": 2}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "company"


def _ensure_dir() -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)


def _sev(finding: Dict[str, Any]) -> str:
    return (finding.get("severity") or "medium").strip().lower()


def _meets_threshold(severity: str, threshold: str) -> bool:
    """True when `severity` is at least as serious as `threshold`."""
    return SEVERITY_RANK.get(severity, 1) <= SEVERITY_RANK.get(threshold, 0)


def summarize(findings: List[Dict[str, Any]]) -> Dict[str, int]:
    """Deterministic rollup of a finding list — the numbers a dashboard row shows."""
    total = len(findings)
    high = sum(1 for f in findings if _sev(f) == "high")
    medium = sum(1 for f in findings if _sev(f) == "medium")
    low = sum(1 for f in findings if _sev(f) == "low")
    matched = sum(1 for f in findings if f.get("standard"))
    return {
        "total": total,
        "high": high,
        "medium": medium,
        "low": low,
        "matched": matched,          # findings mapped to a real OSHA standard
        "unmatched": total - matched,
    }


# ── creation ──────────────────────────────────────────────────────────────────
def record_audit(report: Dict[str, Any], *, company: str, company_id: str = "",
                 by: str = "system", auto_capa_severity: str = "high") -> Dict[str, Any]:
    """Persist a finished photo walk-through as a company-bound audit and open
    CAPAs for the findings that clear `auto_capa_severity`.

    `report` is exactly what photo_audit.analyze() returns
    (scene / image_count / model / findings[] / unmatched / disclaimer).
    Nothing is invented — every field comes straight off that report.
    """
    findings = report.get("findings") or []
    if not isinstance(findings, list):
        findings = []
    company = (company or "").strip()
    cid = _slug(company_id or company)

    # Make sure the company has a profile so its risk has somewhere to land
    # (get-or-create — never overwrites an existing profile).
    try:
        prof = company_mod.ensure(company or cid)
        company = company or prof.get("company", "")
        cid = prof.get("company_id", cid)
    except Exception:  # profiling must never block recording the audit
        pass

    audit_id = "audit-" + uuid.uuid4().hex[:10]
    annotated: List[Dict[str, Any]] = []
    capa_ids: List[str] = []

    for f in findings:
        if not isinstance(f, dict):
            continue
        sev = _sev(f)
        entry = dict(f)
        entry["finding_id"] = "f-" + uuid.uuid4().hex[:6]
        entry["severity_weight"] = capa.audit_severity_weight(sev)
        entry["capa_id"] = ""
        if _meets_threshold(sev, auto_capa_severity):
            try:
                c = capa.open_from_audit_finding(
                    f, company=company, company_id=cid, audit_id=audit_id, by=by)
                entry["capa_id"] = c["id"]
                capa_ids.append(c["id"])
            except Exception as exc:  # one bad finding never sinks the audit
                entry["capa_error"] = str(exc)
        annotated.append(entry)

    record = {
        "id": audit_id,
        "company_id": cid,
        "company": company,
        "created_at": _now(),
        "updated_at": _now(),
        "by": by,
        "source": "photo_audit",
        "scene": (report.get("scene") or "").strip(),
        "image_count": report.get("image_count", 0),
        "model": report.get("model", ""),
        "auto_capa_severity": auto_capa_severity,
        "summary": summarize(findings),
        "findings": annotated,
        "capa_ids": capa_ids,
        "disclaimer": report.get("disclaimer", ""),
    }
    save(record)
    return record


def promote_finding(audit_id: str, finding_id: str, *,
                    by: str = "owner") -> Optional[Dict[str, Any]]:
    """Open a CAPA for a single finding on demand (e.g. a medium/low finding the
    auto-threshold skipped). Idempotent: if that finding already has a CAPA, the
    existing one is returned rather than a duplicate opened."""
    rec = get(audit_id)
    if not rec:
        return None
    target = None
    for f in rec.get("findings", []):
        if f.get("finding_id") == finding_id:
            target = f
            break
    if target is None:
        return None
    if target.get("capa_id"):
        return {"audit": rec, "capa": capa.get(target["capa_id"]), "created": False}
    c = capa.open_from_audit_finding(
        target, company=rec.get("company", ""), company_id=rec.get("company_id", ""),
        audit_id=audit_id, by=by)
    target["capa_id"] = c["id"]
    rec.setdefault("capa_ids", []).append(c["id"])
    rec["updated_at"] = _now()
    save(rec)
    return {"audit": rec, "capa": c, "created": True}


# ── persistence ───────────────────────────────────────────────────────────────
def save(record: Dict[str, Any]) -> str:
    _ensure_dir()
    path = AUDIT_DIR / f"{record['id']}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record["id"]


def get(audit_id: str) -> Optional[Dict[str, Any]]:
    path = AUDIT_DIR / f"{(audit_id or '').strip()}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_all() -> List[Dict[str, Any]]:
    if not AUDIT_DIR.exists():
        return []
    out: List[Dict[str, Any]] = []
    for p in AUDIT_DIR.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _view(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Compact list-row view of an audit."""
    return {
        "id": rec.get("id"),
        "company": rec.get("company"),
        "company_id": rec.get("company_id"),
        "scene": rec.get("scene", "")[:160],
        "image_count": rec.get("image_count", 0),
        "summary": rec.get("summary", {}),
        "capa_count": len(rec.get("capa_ids", [])),
        "created_at": rec.get("created_at"),
    }


def list_for_company(company_id: str) -> List[Dict[str, Any]]:
    cid = _slug(company_id)
    rows = [r for r in _load_all() if r.get("company_id") == cid]
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return [_view(r) for r in rows]


def list_recent(limit: int = 100) -> List[Dict[str, Any]]:
    rows = _load_all()
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return [_view(r) for r in rows[:limit]]


def overview() -> Dict[str, Any]:
    """Portfolio-wide audit rollup: totals, hazards found, CAPAs opened."""
    rows = _load_all()
    audits = len(rows)
    findings = sum(len(r.get("findings", [])) for r in rows)
    high = sum(r.get("summary", {}).get("high", 0) for r in rows)
    unmatched = sum(r.get("summary", {}).get("unmatched", 0) for r in rows)
    capas = sum(len(r.get("capa_ids", [])) for r in rows)
    per_company: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        cid = r.get("company_id", "")
        pc = per_company.setdefault(cid, {
            "company_id": cid, "company": r.get("company", ""),
            "audits": 0, "findings": 0, "high": 0, "capas": 0})
        pc["audits"] += 1
        pc["findings"] += len(r.get("findings", []))
        pc["high"] += r.get("summary", {}).get("high", 0)
        pc["capas"] += len(r.get("capa_ids", []))
    return {
        "audits": audits,
        "findings": findings,
        "high_severity": high,
        "unmatched": unmatched,
        "capas_opened": capas,
        "companies": sorted(per_company.values(),
                            key=lambda x: (-x["high"], -x["findings"])),
    }


# ── routes ────────────────────────────────────────────────────────────────────
def register_audit(app) -> None:
    """Attach Walk-Through Audit routes. Isolated + non-fatal, mirroring the other
    SIE modules. Fully offline — the persistence/CAPA/risk layer consults no model."""
    from fastapi import Body
    from fastapi.responses import JSONResponse

    @app.post("/api/audit/from-report")
    def audit_from_report(body: dict = Body(default=None)):
        """Persist a photo_audit report as a company-bound audit + auto-CAPAs.
        Body: {company, company_id?, report, by?, auto_capa_severity?}."""
        payload = body if isinstance(body, dict) else {}
        company = (payload.get("company") or "").strip()
        report = payload.get("report")
        if not company or not isinstance(report, dict):
            return JSONResponse(
                {"error": "company and a report object are required"}, status_code=400)
        try:
            rec = record_audit(
                report, company=company, company_id=payload.get("company_id", ""),
                by=payload.get("by", "owner"),
                auto_capa_severity=(payload.get("auto_capa_severity") or "high"))
            return {"ok": True, "audit": rec}
        except Exception as exc:  # never 500 the tool
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/audit/overview")
    def audit_overview():
        try:
            return {"ok": True, **overview()}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/audit/list")
    def audit_list(company_id: str = ""):
        try:
            items = list_for_company(company_id) if company_id else list_recent()
            return {"ok": True, "items": items}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.get("/api/audit/{audit_id}")
    def audit_get(audit_id: str):
        rec = get(audit_id)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return rec

    @app.post("/api/audit/{audit_id}/promote/{finding_id}")
    def audit_promote(audit_id: str, finding_id: str, body: dict = Body(default=None)):
        payload = body if isinstance(body, dict) else {}
        out = promote_finding(audit_id, finding_id, by=payload.get("by", "owner"))
        if out is None:
            return JSONResponse({"error": "audit or finding not found"}, status_code=404)
        return {"ok": True, **out}
