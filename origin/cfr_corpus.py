"""CFR corpus builder — the batch-ingest + verification harness that folds
pulled-and-verified regulatory sections into the shipping verbatim corpus.

This is the durable "brain factory" loader. A verified CFR section that lands in
``osha_verbatim.jsonl`` auto-flows through the whole stack with ZERO new code:

    cfr_corpus.ingest()  →  compliance_kb.verbatim_text()  →
    knowledge_store.resolve()  →  checklist_engine.checklist_from_regulation()
    →  dynamic mobile checklist + AHA matrix.

Design rules (identical philosophy to the rest of Origin):
  * Deterministic. No LLM anywhere. Same staging record in → same corpus row out.
  * Never fabricates. Every record must carry real verbatim text that contains
    its own section number, or it is REJECTED (not guessed, not patched).
  * Idempotent / resumable. Re-running on the same staging file is a no-op unless
    a section's text actually changed (longer/newer verbatim supersedes).
  * File-based. Writes the in-code shipping corpus JSONL that deploys via git —
    so a pulled section survives redeploys and works fully offline.
  * Honest provenance. The ``source`` label is derived from the record's own URL
    so the verification log can never claim a source the text didn't come from.

Staging format (what a research pull writes to outputs/cfr_staging/*.jsonl):
    one JSON object per line, keys:
      section, citation, title, hazard, keywords[], part, url, text
    (title_num optional/ignored; chars is computed here).

Run standalone:
    python3 -m origin.cfr_corpus outputs/cfr_staging/1926_subpartP.jsonl
    python3 -m origin.cfr_corpus --report    # inventory only, no writes
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

KB_DIR = Path(__file__).parent / "compliance_kb_data" / "Compliance Knowledge Base"
CORPUS_PATH = KB_DIR / "osha_verbatim.jsonl"

# The canonical shipping-record schema, in order.
_FIELDS = ["section", "citation", "title", "hazard", "keywords", "part", "url",
           "text", "source", "chars"]

_SECTION_RE = re.compile(r"\b(\d{2,4}\.\d+[A-Za-z]?)")


# ── provenance ───────────────────────────────────────────────────────────────
def source_for(url: str) -> str:
    """Derive an honest source label from the record's own URL host.

    We never let a caller assert a source the text did not come from; the label
    is a function of where the text actually lives.
    """
    u = (url or "").lower()
    if "ecfr.gov" in u:
        return "eCFR (GPO) — current authoritative CFR"
    if "govinfo.gov" in u:
        return "GovInfo (GPO) — published CFR"
    if "law.cornell.edu" in u:
        return "Cornell LII — 29/49/40/30/43 CFR (public-domain federal law)"
    if "osha.gov" in u:
        return "OSHA.gov — standard text (public-domain federal law)"
    if "ecfr" in u or "cfr" in u:
        return "CFR (public-domain federal law)"
    return "Public-domain federal law"


# ── validation ───────────────────────────────────────────────────────────────
def _section_of(s: str) -> str:
    m = _SECTION_RE.search(s or "")
    return m.group(1) if m else (s or "").strip()


def validate(rec: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Return (ok, problems). A record is only accepted if it carries real,
    self-consistent verbatim text. Nothing is patched or invented here."""
    problems: List[str] = []
    section = (rec.get("section") or "").strip()
    citation = (rec.get("citation") or "").strip()
    title = (rec.get("title") or "").strip()
    text = (rec.get("text") or "").strip()
    url = (rec.get("url") or "").strip()

    if not section:
        problems.append("missing section number")
    if not title:
        problems.append("missing title")
    if not url:
        problems.append("missing source url")
    # Floor is deliberately low: several CFR sections are legitimate one-line
    # cross-references (e.g. 1926.33 → 1910.1020, 1926.59 → 1910.1200). Those are
    # complete verbatim text, just short. The section-number check below is the
    # real anti-fabrication gate; this only catches empty/truncated pulls.
    if len(text) < 80:
        problems.append(f"text too short ({len(text)} chars) — not real verbatim")
    # The text must contain its own section number — the anti-fabrication check.
    if section and section not in text:
        problems.append(f"text does not contain its own section number {section!r}")
    # citation and section must agree.
    if citation and section and _section_of(citation) != section:
        problems.append(f"citation {citation!r} disagrees with section {section!r}")
    return (not problems), problems


def _normalize(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a staging record into the canonical shipping schema."""
    section = (rec.get("section") or "").strip()
    citation = (rec.get("citation") or "").strip() or (
        f"{rec.get('title_num', '29')} CFR {section}" if section else "")
    text = (rec.get("text") or "").strip()
    url = (rec.get("url") or "").strip()
    out = {
        "section": section,
        "citation": citation,
        "title": (rec.get("title") or "").strip(),
        "hazard": (rec.get("hazard") or "").strip(),
        "keywords": list(rec.get("keywords") or []),
        "part": str(rec.get("part") or "").strip(),
        "url": url,
        "text": text,
        "source": source_for(url),
        "chars": len(text),
    }
    return out


# ── corpus io ────────────────────────────────────────────────────────────────
def load_corpus() -> List[Dict[str, Any]]:
    if not CORPUS_PATH.exists():
        return []
    out = []
    for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _write_corpus(records: List[Dict[str, Any]]) -> None:
    tmp = CORPUS_PATH.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(CORPUS_PATH)


# ── ingest ───────────────────────────────────────────────────────────────────
def ingest(staging_path: str, *, dry_run: bool = False) -> Dict[str, Any]:
    """Fold a staging JSONL into the shipping corpus. Returns a verification
    report. Idempotent: an identical section is skipped; a longer/changed
    verbatim for an existing section supersedes it."""
    path = Path(staging_path)
    if not path.exists():
        return {"error": f"staging file not found: {staging_path}"}

    corpus = load_corpus()
    by_section: Dict[str, int] = {r.get("section"): i for i, r in enumerate(corpus)}

    added: List[str] = []
    superseded: List[str] = []
    skipped: List[str] = []
    rejected: List[Dict[str, Any]] = []

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            rec = json.loads(raw_line)
        except json.JSONDecodeError as e:
            rejected.append({"section": "?", "problems": [f"invalid JSON: {e}"]})
            continue

        ok, problems = validate(rec)
        if not ok:
            rejected.append({"section": rec.get("section", "?"), "problems": problems})
            continue

        norm = _normalize(rec)
        section = norm["section"]

        if section in by_section:
            existing = corpus[by_section[section]]
            if existing.get("text", "").strip() == norm["text"]:
                skipped.append(section)  # identical → no-op
            else:
                corpus[by_section[section]] = norm  # supersede
                superseded.append(section)
        else:
            corpus.append(norm)
            by_section[section] = len(corpus) - 1
            added.append(section)

    if not dry_run and (added or superseded):
        _write_corpus(corpus)

    return {
        "staging_file": str(path.name),
        "dry_run": dry_run,
        "added": sorted(added),
        "superseded": sorted(superseded),
        "skipped": sorted(skipped),
        "rejected": rejected,
        "corpus_total": len(corpus),
    }


def inventory() -> Dict[str, Any]:
    """Summarize the shipping corpus: count + section list by part."""
    corpus = load_corpus()
    parts: Dict[str, List[str]] = {}
    for r in corpus:
        parts.setdefault(str(r.get("part") or "?"), []).append(r.get("section", "?"))
    for p in parts:
        parts[p] = sorted(set(parts[p]))
    return {"total": len(corpus), "by_part": {p: len(v) for p, v in parts.items()},
            "sections_by_part": parts}


# ── cli ──────────────────────────────────────────────────────────────────────
def _main(argv: List[str]) -> int:
    if not argv or argv[0] in ("--report", "-r"):
        inv = inventory()
        print(json.dumps(inv, indent=2))
        return 0
    dry = "--dry-run" in argv
    files = [a for a in argv if not a.startswith("-")]
    for fp in files:
        report = ingest(fp, dry_run=dry)
        print(json.dumps(report, indent=2))
        if report.get("rejected"):
            print(f"  ⚠ {len(report['rejected'])} record(s) REJECTED (see above)",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
