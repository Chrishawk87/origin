"""Origin Compliance Knowledge Base — retrieval + OSHA validation gate.

Loads the codified compliance corpus (OSHA 1910/1926, DOT/FMCSA, oil & gas,
prequalification agencies, EPA, safety metrics, insurance/COI) that ships in
``compliance_kb_data/Compliance Knowledge Base/`` and exposes:

* retrieval helpers (``get``, ``by_citation``, ``search``, ``templates``) so the
  agent can ground drafts on the exact required elements + citations, and
* ``validate_document()`` — the checklist gate that every compliance document
  must pass before it is sent to a client. It resolves the standards a document
  invokes, checks the draft against each standard's ``required_elements``, and
  surfaces the reviewer ``failure_points`` that must be affirmed.

The corpus is static data; regenerate it with ``kb_engine.py`` when standards
change. Nothing here has external dependencies beyond the stdlib.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

# Data ships inside the package so it deploys with Origin.
KB_DIR = Path(__file__).parent / "compliance_kb_data" / "Compliance Knowledge Base"

_STOP = {
    "the", "and", "for", "with", "that", "this", "each", "from", "into", "your",
    "are", "not", "all", "any", "per", "its", "over", "under", "must", "shall",
    "written", "program", "plan", "procedure", "procedures", "employee",
    "employees", "used", "using", "least", "every", "incl", "etc", "core",
}


# ── corpus loading ──────────────────────────────────────────────────────────
@functools.lru_cache(maxsize=1)
def _records() -> Dict[str, dict]:
    recs: Dict[str, dict] = {}
    path = KB_DIR / "corpus.jsonl"
    if not path.exists():
        return recs
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            r = json.loads(line)
            recs[r["id"]] = r
    return recs


def all_records() -> List[dict]:
    return list(_records().values())


def get(entry_id: str) -> Optional[dict]:
    return _records().get(entry_id)


def by_citation(citation: str) -> Optional[dict]:
    citation = (citation or "").strip()
    return next((r for r in _records().values() if r["citation"] == citation), None)


def search(query: str, limit: int = 5) -> List[dict]:
    """Keyword fallback ranking by term overlap (no vector store required)."""
    q = {t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 2}
    scored = []
    for r in _records().values():
        hay = " ".join([
            r.get("title", ""), r.get("citation", ""), r.get("applicability", ""),
            " ".join(r.get("required_elements", [])),
        ]).lower()
        scored.append((sum(t in hay for t in q), r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for s, r in scored[:limit] if s > 0]


def templates() -> dict:
    path = KB_DIR / "Templates" / "templates_index.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def template_body(template_id: str) -> str:
    path = KB_DIR / "Templates" / f"{template_id}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ── text helpers ────────────────────────────────────────────────────────────
def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (text.replace("&amp;", "&").replace("&nbsp;", " ")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    return re.sub(r"\s+", " ", text).strip()


def _keywords(label: str) -> List[str]:
    """Significant tokens from the leading label of a required element."""
    head = re.split(r"[—\-–(:]", label, 1)[0]
    toks = [t for t in re.split(r"\W+", head.lower()) if len(t) > 3 and t not in _STOP]
    return toks or [t for t in re.split(r"\W+", label.lower()) if len(t) > 3 and t not in _STOP]


def _citation_num(citation: str) -> str:
    m = re.search(r"(\d{3,4}\.\d+)", citation or "")
    return m.group(1) if m else ""


def _citation_present(citation: str, text: str) -> bool:
    if not citation:
        return False
    if citation.lower() in text:
        return True
    num = _citation_num(citation)
    return bool(num and num in text)


# ── the validation gate ─────────────────────────────────────────────────────
def resolve_standards(text: str, limit: int = 6) -> List[dict]:
    """Detect which KB standards a document invokes, by citation or title."""
    low = text.lower()
    hits: List[dict] = []
    for r in _records().values():
        title = r.get("title", "")
        exact_cite = (r.get("citation", "").lower() in low) if r.get("citation") else False
        title_hit = len(title) > 8 and title.lower() in low
        if _citation_present(r.get("citation", ""), low) or title_hit:
            hits.append((r, exact_cite or title_hit))

    # When several records share a citation number (e.g. a specialty program
    # reuses the base standard's citation), keep only the strongest match for
    # that number: prefer an exact citation/title hit, then the shortest
    # (canonical) citation string, so the base standard wins over a variant.
    by_num: Dict[str, list] = {}
    for r, strong in hits:
        key = _citation_num(r.get("citation", "")) or r["id"]
        by_num.setdefault(key, []).append((r, strong))

    out: List[dict] = []
    for key, group in by_num.items():
        strong = [r for r, s in group if s]
        pool = strong or [r for r, _ in group]
        pool.sort(key=lambda r: len(r.get("citation", "")))
        out.append(pool[0])
    return out[:limit]


def check_standard(rec: dict, text: str, element_threshold: float = 0.5) -> dict:
    """Score one standard's required_elements against the document text."""
    low = text.lower()
    elements = []
    for el in rec.get("required_elements", []):
        kws = _keywords(el)
        present = [k for k in kws if k in low]
        ratio = (len(present) / len(kws)) if kws else 0.0
        elements.append({
            "element": el,
            "covered": ratio >= element_threshold,
            "coverage": round(ratio, 2),
        })
    total = len(elements)
    covered = sum(1 for e in elements if e["covered"])
    return {
        "id": rec["id"],
        "title": rec.get("title", ""),
        "citation": rec.get("citation", ""),
        "source": rec.get("source", ""),
        "written_program": rec.get("written_program", ""),
        "elements_total": total,
        "elements_covered": covered,
        "coverage_ratio": round(covered / total, 2) if total else 1.0,
        "missing_elements": [e["element"] for e in elements if not e["covered"]],
        "failure_points": rec.get("failure_points", []),
        "training": rec.get("training", ""),
        "recordkeeping": rec.get("recordkeeping", ""),
    }


def validate_document(
    html: str,
    entry_ids: Optional[List[str]] = None,
    *,
    pass_ratio: float = 0.8,
) -> dict:
    """Gate a compliance document against the KB before it goes to a client.

    Resolves the relevant standard(s) — either the explicit ``entry_ids`` or by
    auto-detecting citations/titles in the text — then checks the draft's
    coverage of each standard's ``required_elements``.

    A document PASSES only when at least one standard is identified and every
    identified standard covers >= ``pass_ratio`` of its required elements.
    ``failure_points`` are always returned so a human/agent can affirm them.
    """
    text = _strip_html(html)
    low = text.lower()

    if entry_ids:
        recs = [get(e) for e in entry_ids]
        recs = [r for r in recs if r]
    else:
        recs = resolve_standards(low)

    if not recs:
        return {
            "passed": False,
            "status": "unverified",
            "reason": ("No codified standard could be matched to this document. "
                       "Specify the governing citation (e.g. 29 CFR 1910.119) or "
                       "assign the standard before sending."),
            "standards": [],
            "checked": 0,
        }

    results = [check_standard(r, low, ) for r in recs]
    failing = [r for r in results if r["coverage_ratio"] < pass_ratio]
    passed = len(failing) == 0

    return {
        "passed": passed,
        "status": "pass" if passed else "fail",
        "pass_ratio": pass_ratio,
        "checked": len(results),
        "failing": [r["citation"] for r in failing],
        "standards": results,
        "summary": _summary(results, passed),
    }


def _summary(results: List[dict], passed: bool) -> str:
    lines = []
    for r in results:
        tag = "OK" if r["coverage_ratio"] >= 0.8 else "GAP"
        lines.append(
            f"[{tag}] {r['citation']} — {r['elements_covered']}/{r['elements_total']} "
            f"required elements present"
            + (f"; missing: {', '.join(m.split('—')[0].strip() for m in r['missing_elements'])}"
               if r["missing_elements"] else "")
        )
    head = ("PASS — document covers the required elements of every identified standard."
            if passed else
            "FAIL — document is missing required elements for one or more standards.")
    return head + "\n" + "\n".join(lines)
