"""Compliance connector — lets the agent pull from the Compliance Knowledge Base.

This is what makes Claude draft compliance documents "like a human brain that
knows the standards": instead of only being checked at send time, the agent can
actively look up a standard's required elements + citation while it writes, and
run the full OSHA/DOT checklist on a draft before finalizing it.

Both tools read the same codified corpus that backs the send-gate
(``compliance_kb.corpus.jsonl``), so what the agent sees while drafting and what
the gate enforces at send are always identical.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .. import compliance_kb as kb
from .base import Tool


def _fmt_standard(r: dict, full: bool = True) -> str:
    lines = [
        f"# {r.get('title','')}",
        f"Citation: {r.get('citation','')}",
        f"Written program required: {r.get('written_program','')}",
        f"Applies to: {r.get('applicability','')}".rstrip(),
    ]
    req = r.get("required_elements") or []
    if req:
        lines.append("Required elements:")
        lines += [f"  - {e}" for e in req]
    if full:
        if r.get("training"):
            lines.append(f"Training: {r['training']}")
        if r.get("recordkeeping"):
            lines.append(f"Recordkeeping: {r['recordkeeping']}")
        fp = r.get("failure_points") or []
        if fp:
            lines.append("Common rejection reasons (avoid these):")
            lines += [f"  - {e}" for e in fp]
        if r.get("formula"):
            lines.append(f"Formula: {r['formula']}")
        if r.get("benchmarks"):
            lines.append(f"Benchmarks: {r['benchmarks']}")
    if r.get("source"):
        lines.append(f"Source: {r['source']}")
    return "\n".join(lines)


def build_compliance_tools() -> List[Tool]:
    def compliance_lookup(args: Dict[str, Any]) -> str:
        query = (args.get("query") or args.get("citation") or "").strip()
        if not query:
            return "ERROR: 'query' (a topic, program name, or citation) is required."
        # exact citation first, then keyword search
        rec = kb.by_citation(query)
        if rec:
            return _fmt_standard(rec, full=True)
        hits = kb.search(query, limit=int(args.get("limit", 3)))
        if not hits:
            return (f"No codified standard matched '{query}'. Do NOT invent requirements — "
                    "say the KB has no entry for it.")
        # one strong hit → full detail; several → summaries so the agent can pick
        if len(hits) == 1:
            return _fmt_standard(hits[0], full=True)
        parts = [f"{len(hits)} matches — call compliance_lookup with the exact citation for full detail:"]
        parts += [_fmt_standard(h, full=False) for h in hits]
        return "\n\n".join(parts)

    def compliance_check(args: Dict[str, Any]) -> str:
        doc = args.get("document") or args.get("html") or args.get("text") or ""
        if not doc.strip():
            return "ERROR: 'document' (the draft text or HTML) is required."
        entry_ids = args.get("entry_ids") or args.get("standards") or None
        report = kb.validate_document(doc, entry_ids=entry_ids)
        head = f"COMPLIANCE CHECK — {report.get('status','?').upper()} " \
               f"(passed={report.get('passed')}, standards checked={report.get('checked',0)})"
        if report.get("status") == "unverified":
            return head + "\n" + report.get("reason", "")
        return head + "\n\n" + report.get("summary", "")

    return [
        Tool(
            name="compliance_lookup",
            description=(
                "Look up an OSHA/DOT/EPA/insurance compliance standard in Origin's Compliance "
                "Knowledge Base. Use this BEFORE writing or editing any compliance document, and "
                "whenever you need the authoritative requirements. Returns the standard's exact "
                "citation, required written-program elements, training and recordkeeping duties, "
                "the reviewer rejection reasons to avoid, and the source URL. Query by topic "
                "(e.g. 'fall protection', 'respiratory protection', 'EMR') or exact citation "
                "(e.g. '29 CFR 1910.119'). Only assert requirements this tool returns."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Topic, program name, or exact citation"},
                    "limit": {"type": "integer", "description": "Max matches when searching (default 3)"},
                },
                "required": ["query"],
            },
            handler=compliance_lookup,
            source="builtin",
        ),
        Tool(
            name="compliance_check",
            description=(
                "Run the OSHA/DOT compliance checklist on a draft document BEFORE it is finalized "
                "or sent to a client. Auto-detects the standard(s) the document invokes (or pass "
                "entry_ids) and reports pass/fail plus exactly which required elements are missing. "
                "This is the same gate enforced at send time — run it while editing so nothing "
                "goes out incomplete."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "document": {"type": "string", "description": "The draft text or HTML to check"},
                    "entry_ids": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Optional explicit KB standard ids to check against",
                    },
                },
                "required": ["document"],
            },
            handler=compliance_check,
            source="builtin",
        ),
    ]
