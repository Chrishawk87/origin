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

    def compliance_profile(args: Dict[str, Any]) -> str:
        target = (args.get("industry") or args.get("naics") or args.get("code")
                  or args.get("query") or "").strip()
        if not target:
            return ("ERROR: provide 'industry' (e.g. 'oilfield services') or "
                    "'naics' (e.g. '213112') so I can scope the required standards.")
        state = (args.get("state") or "").strip() or None
        prof = kb.naics_applicable(target, state=state)
        if prof.get("error"):
            return f"ERROR: {prof['error']}"
        if not prof.get("standards"):
            return (f"No standards resolved for '{target}'. Try a NAICS code or a "
                    "clearer industry name (e.g. construction, trucking, manufacturing).")

        head = [
            f"COMPLIANCE PROFILE — {target}"
            + (f"  (state: {state})" if state else ""),
            f"Matched sector: {prof.get('sector')} — {prof.get('sector_label') or 'universal only'}",
            f"{prof['count']} standards required "
            f"(universal {prof['buckets'].get('universal',0)}"
            + (f" + sector {prof['buckets'].get('sector',0)}" if 'sector' in prof['buckets'] else "")
            + (f" + {state} {prof['buckets'].get('state',0)}" if 'state' in prof['buckets'] else "")
            + ").",
        ]
        if prof.get("gap_note"):
            head.append(f"NOTE: {prof['gap_note']}")
        head.append("")
        head.append("Required standards (look each up with compliance_lookup, then draft the written program):")
        # group by category for a readable checklist
        by_cat: Dict[str, list] = {}
        for s in prof["standards"]:
            by_cat.setdefault(s.get("category", "Other"), []).append(s)
        lines: List[str] = []
        for cat in sorted(by_cat):
            lines.append(f"\n{cat}")
            for s in by_cat[cat]:
                cite = f" [{s['citation']}]" if s.get("citation") else ""
                lines.append(f"  - {s['title']}{cite}  (id: {s['id']})")
        return "\n".join(head + lines)

    def compliance_template(args: Dict[str, Any]) -> str:
        entry_id = (args.get("entry_id") or args.get("id") or args.get("standard") or "").strip()
        query = (args.get("query") or "").strip()
        if not entry_id and query:
            rec = kb.by_citation(query)
            if not rec:
                hits = kb.search(query, limit=1)
                rec = hits[0] if hits else None
            if rec:
                entry_id = rec["id"]
        if not entry_id:
            return ("ERROR: provide 'entry_id' (a KB standard id) or 'query' (a topic/citation) "
                    "so I know which written program to generate.")
        doc = kb.render_program(entry_id)
        if doc is None:
            return (f"No KB standard '{entry_id}'. Use compliance_lookup to find the right id, or "
                    "compliance_profile to list a client's required standards.")
        return doc

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
            name="compliance_profile",
            description=(
                "Scope a client's required compliance standards by their industry. Call this "
                "FIRST when onboarding a new contractor or when asked 'what programs does this "
                "company need?'. Give a NAICS code (e.g. '213112') or an industry name (e.g. "
                "'oilfield services', 'construction', 'trucking') and optionally a state (e.g. "
                "'CA' adds Cal/OSHA IIPP, heat, workplace-violence). Returns the full checklist "
                "of KB standards that a prequalification review (ISNetworld/Avetta/Veriforce) "
                "typically requires for that industry — universal programs plus trade-specific "
                "ones. Then use compliance_lookup on each id to draft the written programs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "industry": {"type": "string", "description": "Industry name, e.g. 'oilfield services', 'construction'"},
                    "naics": {"type": "string", "description": "NAICS code, e.g. '213112' or '23'"},
                    "state": {"type": "string", "description": "Two-letter state for jurisdiction overlays, e.g. 'CA'"},
                },
                "required": [],
            },
            handler=compliance_profile,
            source="builtin",
        ),
        Tool(
            name="compliance_template",
            description=(
                "Generate a ready-to-fill, editable WRITTEN PROGRAM document for a compliance "
                "standard — the actual workable document a contractor submits, not just the "
                "requirements. Every required element becomes a fillable section (headings taken "
                "verbatim from the standard) with company placeholders ({{COMPANY_NAME}} etc.), "
                "training/recordkeeping language, a reviewer rejection checklist, and a signature "
                "block. Give 'entry_id' (a KB id, e.g. '29-cfr-1910-134-respiratory-protection-"
                "program') or 'query' (topic/citation). Use compliance_profile to get the ids a "
                "client needs, then call this per standard to produce each document, then fill "
                "the [[...]] prompts with the client's specifics and run compliance_check."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string", "description": "KB standard id to generate the program for"},
                    "query": {"type": "string", "description": "Alternatively, a topic or citation to resolve to a standard"},
                },
                "required": [],
            },
            handler=compliance_template,
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
