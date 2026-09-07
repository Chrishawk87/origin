"""
ecfr_adapter.py — the Universal Regulatory Router's ingest adapter for the eCFR.

The router's parser (checklist_engine) is source-agnostic: it turns regulatory
*text* into field requirements no matter where the text came from. This module is
one *ingest adapter* — it pulls a real CFR section's current text from the
Electronic Code of Federal Regulations (eCFR) and writes it into the same
file-based ``knowledge_store`` the OSHA corpus uses. Once ingested, that section
generates a dynamic checklist and AHA matrix exactly like the EM 385-1-1 seed —
the live "the law updates, the app updates" pipeline, for every CFR agency
(MSHA 30 CFR, EPA 40 CFR, FMCSA 49 CFR, and so on).

Design rules (same philosophy as the rest of Origin):
  * Deterministic + offline-safe to IMPORT. The network is only touched when a
    fetch is explicitly requested (an owner action), never at import or boot.
  * No LLM. Cleaning eCFR XML → plain text is pure string work; parsing is
    checklist_engine's deterministic rules.
  * Never fabricates. A failed fetch or an empty section returns an explicit
    ``{"ok": False, ...}`` — it does NOT invent regulatory text.
  * Pluggable fetcher. Every network call goes through a ``fetcher`` callable so
    the offline self-test injects a cached fixture and stays fully network-free.
"""

import re
from datetime import date as _date
from typing import Any, Callable, Dict, List, Optional
from urllib import request as _urlrequest
from urllib.parse import urlencode

try:  # package import (normal runtime + self-test)
    from . import knowledge_store
except ImportError:  # bare-script fallback
    import knowledge_store  # type: ignore


ECFR_API_ROOT = "https://www.ecfr.gov/api/versioner/v1/full"
ECFR_HUMAN_ROOT = "https://www.ecfr.gov/current"
_UA = "Origin-Compliance/1.0 (regulatory ingest; contact info@originmanagementsolutions.com)"

# A ``Fetcher`` takes a URL and returns the response body as text (or raises).
Fetcher = Callable[[str], str]


# ── URL construction ─────────────────────────────────────────────────────────
def api_url(title: int, part: str, section: str, on_date: str = "") -> str:
    """Build the eCFR versioner full-text XML URL for a single section.

    ``on_date`` (YYYY-MM-DD) selects the point-in-time version; default = today,
    so the pull reflects the law as it currently stands.
    """
    d = (on_date or _date.today().isoformat()).strip()
    qs = urlencode({"part": str(part), "section": str(section)})
    return f"{ECFR_API_ROOT}/{d}/title-{int(title)}.xml?{qs}"


def human_url(title: int, part: str, section: str) -> str:
    """The citizen-facing eCFR page for a section — used as the record's source_url."""
    return f"{ECFR_HUMAN_ROOT}/title-{int(title)}/part-{part}/section-{section}"


def citation_for(title: int, section: str) -> str:
    """Canonical CFR citation string, e.g. '29 CFR 1926.651'."""
    return f"{int(title)} CFR {section}".strip()


# ── default network fetcher (only called on explicit request) ────────────────
def _default_fetcher(url: str, timeout: int = 20) -> str:
    req = _urlrequest.Request(url, headers={"User-Agent": _UA, "Accept": "application/xml"})
    with _urlrequest.urlopen(req, timeout=timeout) as resp:  # nosec - fixed eCFR host
        raw = resp.read()
    try:
        return raw.decode("utf-8")
    except Exception:
        return raw.decode("latin-1", "ignore")


# ── XML → plain regulatory text ──────────────────────────────────────────────
# eCFR/GPO XML wraps section prose in <P>…</P> paragraphs (with inline markup).
# We pull the paragraph text out deterministically. We try the stdlib XML parser
# first; if the payload isn't clean XML we fall back to a tag-strip so a slightly
# malformed response still yields usable text rather than nothing.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_HEAD_RE = re.compile(r"<HEAD[^>]*>(.*?)</HEAD>", re.I | re.S)


def _clean(fragment: str) -> str:
    txt = _TAG_RE.sub(" ", fragment or "")
    txt = (txt.replace("&#167;", "§").replace("&sect;", "§")
              .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
              .replace("&#8212;", "—").replace("&#8217;", "'").replace("&#8201;", " "))
    return _WS_RE.sub(" ", txt).strip()


def extract_text(xml: str) -> Dict[str, Any]:
    """Turn an eCFR section XML payload into {title, body_text}.

    Deterministic. Returns empty strings if no paragraph text is present — the
    caller treats that as an unverifiable section, never a fabricated one.
    """
    if not xml or not xml.strip():
        return {"title": "", "body_text": ""}

    title = ""
    paras: List[str] = []

    # Preferred path: real XML parse of <P> elements.
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        # section head → title
        for h in root.iter():
            tag = h.tag.split("}")[-1].upper()
            if tag == "HEAD" and not title:
                title = _clean("".join(h.itertext()))
                break
        for p in root.iter():
            if p.tag.split("}")[-1].upper() == "P":
                t = _clean("".join(p.itertext()))
                if t:
                    paras.append(t)
    except Exception:
        # Fallback: regex over the raw markup.
        m = _HEAD_RE.search(xml)
        if m:
            title = _clean(m.group(1))
        for pm in re.findall(r"<P[^>]*>(.*?)</P>", xml, re.I | re.S):
            t = _clean(pm)
            if t:
                paras.append(t)

    body = " ".join(paras).strip()
    return {"title": title, "body_text": body}


# ── fetch + ingest ───────────────────────────────────────────────────────────
def fetch_section(
    title: int,
    part: str,
    section: str,
    *,
    on_date: str = "",
    fetcher: Optional[Fetcher] = None,
) -> Dict[str, Any]:
    """Fetch and clean one CFR section. Network only if ``fetcher`` is not given.

    Returns {ok, citation, title, body_text, source_url, ...}. On any failure
    (network, empty payload, no paragraph text) ``ok`` is False with an ``error``
    — the section is treated as unverifiable, never invented.
    """
    cit = citation_for(title, section)
    url = api_url(title, part, section, on_date)
    fx = fetcher or _default_fetcher
    try:
        xml = fx(url)
    except Exception as exc:
        return {"ok": False, "citation": cit, "error": f"eCFR fetch failed: {exc}",
                "url": url, "title": "", "body_text": ""}
    parsed = extract_text(xml)
    if not parsed.get("body_text"):
        return {"ok": False, "citation": cit,
                "error": "No regulatory text found for this section in the eCFR response.",
                "url": url, "title": parsed.get("title", ""), "body_text": ""}
    return {
        "ok": True,
        "citation": cit,
        "title": parsed.get("title") or cit,
        "body_text": parsed["body_text"],
        "source": "eCFR (GPO)",
        "source_url": human_url(title, part, section),
        "url": url,
        "on_date": (on_date or _date.today().isoformat()),
    }


def ingest_section(
    title: int,
    part: str,
    section: str,
    *,
    on_date: str = "",
    industry_scope: Optional[List[str]] = None,
    hazard_category: Optional[List[str]] = None,
    fetcher: Optional[Fetcher] = None,
    ingested_by: str = "owner",
) -> Dict[str, Any]:
    """Fetch a CFR section and write it into the versioned knowledge store.

    Returns {ok, citation, record?, field_count?} — on success the section is now
    resolvable and generates a checklist through the same machinery as every other
    brain. On failure returns {ok: False, error}.
    """
    fetched = fetch_section(title, part, section, on_date=on_date, fetcher=fetcher)
    if not fetched.get("ok"):
        return {"ok": False, "citation": fetched.get("citation"),
                "error": fetched.get("error")}
    rec = knowledge_store.ingest(
        regulation_number=fetched["citation"],
        title=fetched["title"],
        body_text=fetched["body_text"],
        source=fetched["source"],
        source_url=fetched["source_url"],
        effective_date=fetched.get("on_date", ""),
        jurisdiction=f"Federal ({int(title)} CFR)",
        industry_scope=industry_scope or [],
        hazard_category=hazard_category or [],
        version_label=f"ecfr-{int(title)}cfr-{section}-{fetched.get('on_date','')}",
        ingested_by=ingested_by,
        confidence=0.95,
    )
    # Count the field requirements the router will derive from it (best-effort).
    field_count = 0
    try:
        from . import checklist_engine  # deferred to avoid import cycles
        field_count = len(checklist_engine.parse_imperatives(
            fetched["body_text"], fetched["citation"]))
    except Exception:
        pass
    return {
        "ok": True,
        "citation": fetched["citation"],
        "title": fetched["title"],
        "source_url": fetched["source_url"],
        "version_label": rec.get("version_label", ""),
        "field_count": field_count,
    }


def status() -> Dict[str, Any]:
    """Adapter health: it's import-clean and knows how to reach the eCFR."""
    return {
        "ok": True,
        "adapter": "eCFR (GPO) live-sync",
        "api_root": ECFR_API_ROOT,
        "network_required_for": "ingest only (never at import/boot)",
    }


# ── route registration (isolated, owner-gated) ───────────────────────────────
def register_ecfr(app) -> None:
    """Register the eCFR ingest routes. Owner-gated (NOT added to the GC allowlist).

    POST /api/ecfr/ingest performs a LIVE fetch — a deliberate owner action — and
    then the section is served by the existing /api/checklist/* routes. Imports are
    function-local; this module intentionally avoids
    ``from __future__ import annotations`` so ``request: Request`` resolves.
    """
    from fastapi import Body
    from fastapi.responses import JSONResponse

    @app.get("/api/ecfr/status")
    def ecfr_status():
        try:
            return status()
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=200)

    @app.post("/api/ecfr/ingest")
    def ecfr_ingest(body: dict = Body(default=None)):
        b = body or {}
        try:
            title = int(b.get("title"))
            part = str(b.get("part")).strip()
            section = str(b.get("section")).strip()
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "title, part, and section are required "
                 "(e.g. title=29, part=1926, section=1926.651)."},
                status_code=200)
        try:
            return ingest_section(
                title, part, section,
                on_date=str(b.get("on_date") or "").strip(),
                industry_scope=b.get("industry_scope") or None,
                hazard_category=b.get("hazard_category") or None,
            )
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=200)
