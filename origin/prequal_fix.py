"""Prequal "Fix it" generator — turns a diagnosed prequal gap into a finished,
company-specific document that Origin drops straight into the sub's vault.

The whole point (Chris): the readiness report must not just *diagnose* a gap — it
must *close* it. For every fixable gap id this module deterministically builds the
exact artifact an ISN / Avetta / Veriforce reviewer expects, pre-filled with the
sub's company name and scope. If Origin's library already has the content we use
it; if it doesn't, we author a baseline so nothing is ever left blank.

Fully offline and deterministic — no external LLM. Each builder returns
``(title, inner_html)``; the caller wraps it with ``compliance.wrap_document`` and
writes it into the sub's documents folder.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from . import compliance as _cmp


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _esc(s: Any) -> str:
    return _cmp._esc(str(s or ""))


def _md(md: str) -> str:
    """Markdown -> HTML using the app's own renderer (keeps program styling)."""
    try:
        return _cmp._md_to_html(md or "")
    except Exception:
        return "<pre style=\"white-space:pre-wrap;font:inherit\">" + _esc(md) + "</pre>"


def _doc_head(company: str, subtitle: str, scope: str = "") -> str:
    """A neutral client-letterhead header block (no Origin branding — the sub owns
    the document once it lands in their vault)."""
    scope_line = (f"<div style='color:#555;margin-top:2px'>Scope of work: "
                  f"{_esc(scope)}</div>") if scope else ""
    return (
        "<div style='border-bottom:2px solid #111;padding-bottom:10px;margin-bottom:18px'>"
        f"<div style='font-size:22px;font-weight:800'>{_esc(company)}</div>"
        f"<div style='font-size:15px;color:#111;margin-top:4px'>{_esc(subtitle)}</div>"
        f"{scope_line}"
        f"<div style='color:#777;font-size:12px;margin-top:6px'>Prepared {_today()} "
        "&middot; Editable &mdash; complete the highlighted fields before upload.</div>"
        "</div>"
    )


def _fill(label: str) -> str:
    """A visible fill-in blank the sub completes after generation."""
    return (f"<span style='background:#fff6c9;border-bottom:1px solid #caa500;"
            f"padding:0 28px'>&nbsp;</span> <span style='color:#777;font-size:11px'>"
            f"({_esc(label)})</span>")


# ── 1. combined written-programs manual ─────────────────────────────────────────
def _combined_manual(company: str, company_id: str, scope: str) -> Tuple[str, str]:
    """Assemble EVERY mandated written program into one company-specific safety
    manual — the single uploaded manual ISN/Avetta reviewers expect. Uses the
    Origin library where a program exists; authors a baseline section where it
    doesn't, so no mandated program is left blank."""
    from . import program_engine as _pe

    title = f"{company} — Company Safety Manual"
    pkg = _pe.build_package(company_id) or {}
    entries = pkg.get("entries", []) or []
    mandated = [e for e in entries
                if (e.get("program", {}) or {}).get("classification") == _pe.OSHA_REQUIRED]

    toc: List[str] = []
    sections: List[str] = []
    for i, e in enumerate(mandated, 1):
        sid = e.get("id", "")
        heading = e.get("title", "") or sid
        citation = e.get("citation", "") or ""
        anchor = f"prog-{i}"
        cite_txt = f" &mdash; {_esc(citation)}" if citation else ""
        toc.append(f"<li><a href='#{anchor}'>{_esc(heading)}</a>{cite_txt}</li>")

        body_html = ""
        try:
            doc = _pe.render_document(company_id, sid) or {}
            program_md = (doc.get("program", {}) or {}).get("markdown", "")
            if program_md:
                body_html = _md(program_md)
        except Exception:
            body_html = ""

        if not body_html:
            # Nothing in the library for this mandated standard — author a baseline
            # so the manual is complete rather than showing a hole.
            body_html = _baseline_program(company, heading, citation)

        sections.append(
            f"<section id='{anchor}' style='margin-top:26px;padding-top:14px;"
            f"border-top:1px solid #ddd'>"
            f"<h2 style='margin:0 0 4px'>{i}. {_esc(heading)}</h2>"
            + (f"<div style='color:#777;font-size:12px;margin-bottom:8px'>"
               f"Regulatory basis: {_esc(citation)}</div>" if citation else "")
            + body_html + "</section>"
        )

    if not mandated:
        sections.append("<p>No mandated written programs are in scope for this "
                         "company's current scope of work. Add the scope of work to "
                         "generate the required programs.</p>")

    intro = (
        f"<p>This manual consolidates the {len(mandated)} written safety "
        f"program(s) mandated for {_esc(company)}'s scope of work into a single, "
        "company-specific document for prequalification upload. Review each program, "
        "confirm the named responsible parties and inspection frequencies, then adopt "
        "it under your HSE management system.</p>"
    )
    toc_html = ("<div style='background:#f6f7f9;border:1px solid #e3e6ea;"
                "border-radius:8px;padding:14px 18px;margin:8px 0 6px'>"
                "<div style='font-weight:700;margin-bottom:6px'>Contents</div>"
                f"<ol style='margin:0;padding-left:20px'>{''.join(toc)}</ol></div>")
    inner = _doc_head(company, "Company Safety Manual", scope) + intro + toc_html + "".join(sections)
    return title, inner


def _baseline_program(company: str, heading: str, citation: str) -> str:
    """A defensible baseline written program when the library has none yet — so a
    mandated program is authored rather than missing. Assertive ('shall') voice,
    named-party placeholders, review cycle."""
    cite = f" ({citation})" if citation else ""
    return _md(
        f"**Purpose.** {company} establishes this {heading} program{cite} to protect "
        f"employees and comply with the governing OSHA requirement. This program "
        f"applies to all {company} employees, subcontractors, and visitors within the "
        f"scope of work.\n\n"
        f"**Responsibilities.** The Program Administrator (name/title: to be completed) "
        f"shall implement, maintain, and annually review this program. Supervisors shall "
        f"enforce it on the job; employees shall follow it and report deficiencies.\n\n"
        f"**Requirements.** {company} shall identify the hazards this standard addresses, "
        f"implement the required controls, train affected employees, and document "
        f"compliance. Specific procedures for this operation shall be completed by the "
        f"Program Administrator based on the company's actual tasks and equipment.\n\n"
        f"**Training.** Affected employees shall be trained before assignment and "
        f"retrained on change of task, equipment, or after any related incident. Training "
        f"shall be documented per individual with name, date, and competency verification.\n\n"
        f"**Recordkeeping & Review.** Records shall be retained per the governing standard. "
        f"This program shall be reviewed at least annually and updated when operations, "
        f"equipment, or regulations change.\n\n"
        f"> Baseline authored by Origin. Complete the company-specific procedure details "
        f"and named responsible parties before upload."
    )


# ── 2. Drug & Alcohol (DOT) written program ─────────────────────────────────────
def _dna_program(company: str, scope: str) -> Tuple[str, str]:
    title = f"{company} — Drug & Alcohol Program (DOT)"
    body = _md(
        f"**Policy.** {company} maintains a drug- and alcohol-free workplace and complies "
        f"with 49 CFR Part 40 and the applicable DOT operating-administration rule for its "
        f"covered work. The use, possession, or being under the influence of prohibited "
        f"substances while performing safety-sensitive functions is prohibited.\n\n"
        f"**Covered employees.** All {company} employees performing DOT safety-sensitive "
        f"functions within the scope of work.\n\n"
        f"**Testing program.** {company} conducts pre-employment, random, reasonable-"
        f"suspicion, post-accident, return-to-duty, and follow-up testing as required by "
        f"49 CFR Part 40. Collections, testing, and MRO review are performed by qualified "
        f"service agents.\n\n"
        f"**Consortium / C-TPA.** Random testing is administered through the company's "
        f"consortium/third-party administrator. Consortium name & contact: (to be completed).\n\n"
        f"**Designated Employer Representative (DER).** Name/title/phone: (to be completed).\n\n"
        f"**Consequences & return-to-duty.** A verified positive or refusal removes the "
        f"employee from safety-sensitive duty; return requires the SAP evaluation and "
        f"return-to-duty process in 49 CFR Part 40, Subpart O.\n\n"
        f"**Records.** Testing records are retained per 49 CFR Part 40 retention periods "
        f"and kept confidential.\n\n"
        f"> Complete the consortium/C-TPA and DER details, then adopt with an authorized "
        f"signature before upload."
    )
    inner = _doc_head(company, "Drug & Alcohol Program (49 CFR Part 40)", scope) + body
    return title, inner


# ── 3. individual training-record matrix ────────────────────────────────────────
def _training_matrix(company: str, company_id: str, scope: str) -> Tuple[str, str]:
    """A per-individual training matrix. Columns are the actual training mandates
    from the company's program package, so the matrix matches their real scope."""
    from . import program_engine as _pe

    title = f"{company} — Training Record Matrix"
    courses: List[str] = []
    try:
        pkg = _pe.build_package(company_id) or {}
        for e in pkg.get("entries", []) or []:
            if (e.get("training", {}) or {}).get("verbatim_mandate"):
                nm = e.get("title", "") or e.get("citation", "")
                if nm and nm not in courses:
                    courses.append(nm)
    except Exception:
        courses = []
    if not courses:
        courses = ["Hazard Communication", "Emergency Action Plan", "PPE",
                   "Site-Specific Orientation"]

    head_cells = "".join(
        f"<th style='writing-mode:vertical-rl;transform:rotate(180deg);"
        f"padding:6px 4px;font-size:11px;white-space:nowrap;border:1px solid #ccc;"
        f"background:#f2f4f7'>{_esc(c)}</th>" for c in courses)
    blank_row = ("<td style='border:1px solid #ccc;height:26px'></td>"
                 + "".join("<td style='border:1px solid #ccc'></td>" for _ in courses)
                 + "<td style='border:1px solid #ccc'></td>")
    rows = "".join(f"<tr>{blank_row}</tr>" for _ in range(15))

    table = (
        "<table style='border-collapse:collapse;width:100%;font-size:12px'>"
        "<thead><tr>"
        "<th style='border:1px solid #ccc;background:#f2f4f7;padding:6px;text-align:left'>"
        "Employee name</th>"
        f"{head_cells}"
        "<th style='border:1px solid #ccc;background:#f2f4f7;padding:6px'>Competency verified by</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    note = ("<p>Enter each employee on a row and record the <b>completion date</b> in each "
            "course column (not just a check). Sign-in sheets alone are rejected — every "
            "record needs a name, date, and competency verification.</p>")
    inner = _doc_head(company, "Individual Training Records", scope) + note + table
    return title, inner


# ── 4. Operator Qualification per-individual + task tracker ──────────────────────
def _oq_tracker(company: str, scope: str) -> Tuple[str, str]:
    title = f"{company} — Operator Qualification (OQ) Tracker"
    header = ("<tr>"
              + "".join(f"<th style='border:1px solid #ccc;background:#f2f4f7;"
                        f"padding:6px;font-size:12px'>{_esc(h)}</th>"
                        for h in ["Employee name", "Covered task", "OQ evaluation date",
                                  "Method (W/O/P/S)", "Re-eval due", "Evaluator"])
              + "</tr>")
    blank = ("<tr>" + "".join("<td style='border:1px solid #ccc;height:26px'></td>"
                              for _ in range(6)) + "</tr>")
    table = ("<table style='border-collapse:collapse;width:100%'>"
             f"<thead>{header}</thead><tbody>{blank * 18}</tbody></table>")
    note = ("<p>DOT-covered pipeline/midstream work requires OQ at the <b>individual worker "
            "&times; covered task</b> level per 49 CFR Part 192/195 Subpart N — not company "
            "level. Record each qualified individual against each covered task, the "
            "evaluation date, method, and re-evaluation due date.</p>")
    inner = _doc_head(company, "Operator Qualification Records (49 CFR Subpart N)", scope) + note + table
    return title, inner


# ── 5. carrier EMR request letter ───────────────────────────────────────────────
def _emr_letter(company: str, scope: str) -> Tuple[str, str]:
    title = f"{company} — EMR Request Letter (to carrier)"
    body = (
        f"<p>Date: {_today()}</p>"
        f"<p>To: {_fill('insurance carrier / underwriter')}</p>"
        f"<p>Re: Experience Modification Rate (EMR) confirmation letter for "
        f"{_esc(company)}</p>"
        f"<p>To whom it may concern,</p>"
        f"<p>{_esc(company)} is completing contractor prequalification and requires a letter "
        f"issued on your (the carrier's) letterhead stating our current Experience "
        f"Modification Rate. Prequalification reviewers do not accept a broker summary or "
        f"premium statement &mdash; the letter must come from the rating carrier.</p>"
        f"<p>Please include, on carrier letterhead:</p>"
        f"<ul>"
        f"<li>Named insured exactly as: {_esc(company)}</li>"
        f"<li>Current EMR / experience modifier and its effective date</li>"
        f"<li>The rating bureau (e.g., NCCI or state bureau)</li>"
        f"<li>Prior two to three years of EMR values, if available</li>"
        f"</ul>"
        f"<p>Please send the signed letter to {_fill('our email / address')}. "
        f"Thank you for your assistance.</p>"
        f"<p>Sincerely,<br>{_fill('name & title')}<br>{_esc(company)}</p>"
    )
    inner = _doc_head(company, "Request for Carrier-Issued EMR Letter", scope) + body
    return title, inner


# ── 6. COI + endorsements broker request letter ─────────────────────────────────
def _coi_letter(company: str, scope: str) -> Tuple[str, str]:
    title = f"{company} — COI & Endorsements Request (to broker)"
    body = (
        f"<p>Date: {_today()}</p>"
        f"<p>To: {_fill('insurance broker / agent')}</p>"
        f"<p>Re: Certificate of Insurance and endorsements for {_esc(company)}</p>"
        f"<p>Please issue a Certificate of Insurance for {_esc(company)} meeting our "
        f"client's contract limits, and provide the following endorsements. A COI without "
        f"the endorsements attached is the most common prequalification rejection.</p>"
        f"<ul>"
        f"<li><b>Additional insured</b> &mdash; CG 20 10 (ongoing operations) and "
        f"CG 20 37 (completed operations)</li>"
        f"<li><b>Blanket waiver of subrogation</b> (GL, auto, and workers' comp)</li>"
        f"<li><b>Primary &amp; non-contributory</b> wording</li>"
        f"<li>Named insured matching our legal name exactly: {_esc(company)}</li>"
        f"<li>Policy effective dates <b>before</b> the work start date</li>"
        f"<li>Limits meeting the client requirement: {_fill('required limits')}</li>"
        f"</ul>"
        f"<p>Please email the COI with all endorsement forms attached to "
        f"{_fill('our email')}. Thank you.</p>"
        f"<p>Sincerely,<br>{_fill('name & title')}<br>{_esc(company)}</p>"
    )
    inner = _doc_head(company, "Request for COI + Endorsements", scope) + body
    return title, inner


# ── 7. TRIR / DART reconciliation worksheet ─────────────────────────────────────
def _rates_worksheet(company: str, scope: str) -> Tuple[str, str]:
    title = f"{company} — TRIR / DART Reconciliation Worksheet"
    def row(label: str, cell: str) -> str:
        return (f"<tr><td style='border:1px solid #ccc;padding:6px;background:#f7f8fa'>"
                f"{label}</td><td style='border:1px solid #ccc;padding:6px'>{cell}</td></tr>")
    tbl = (
        "<table style='border-collapse:collapse;width:100%;max-width:560px'>"
        + row("Reporting year", _fill("YYYY"))
        + row("Total hours worked (from payroll)", _fill("hours"))
        + row("Recordable cases (300 log, column count)", _fill("count"))
        + row("Cases with days away/restricted/transfer (DART)", _fill("count"))
        + row("<b>TRIR</b> = (recordables &times; 200,000) / hours",
              "<b>= " + _fill("computed") + "</b>")
        + row("<b>DART</b> = (DART cases &times; 200,000) / hours",
              "<b>= " + _fill("computed") + "</b>")
        + "</table>"
    )
    note = ("<p>Compute TRIR and DART directly from your OSHA 300/300A hours and case counts, "
            "then confirm the values match what you report on the platform. Rates that don't "
            "reconcile to the uploaded logs (usually a hours-worked error) are a top "
            "cross-platform rejection. The 200,000 factor = 100 employees working a full year.</p>")
    inner = _doc_head(company, "TRIR / DART Reconciliation (OSHA 300/300A)", scope) + note + tbl
    return title, inner


# ── 8. CAPA closure record ──────────────────────────────────────────────────────
def _capa_record(company: str, scope: str) -> Tuple[str, str]:
    title = f"{company} — Corrective Action (CAPA) Closure Record"
    def field(label: str) -> str:
        return (f"<tr><td style='border:1px solid #ccc;padding:6px;background:#f7f8fa;"
                f"width:34%'>{label}</td><td style='border:1px solid #ccc;padding:6px;"
                f"height:30px'></td></tr>")
    tbl = ("<table style='border-collapse:collapse;width:100%'>"
           + field("Citation / finding reference")
           + field("Standard cited")
           + field("Hazard / deficiency described")
           + field("Root cause")
           + field("Corrective action taken")
           + field("Interim protective measures")
           + field("Responsible person")
           + field("Date abated / completed")
           + field("Verification method & evidence attached")
           + field("Verified by / date")
           + "</table>")
    note = ("<p>An open citation with no documented, closed corrective action is a rejection "
            "trigger on every platform. Complete one record per finding: what was fixed, when, "
            "by whom, and the evidence (photo, receipt, revised procedure) attached.</p>")
    inner = _doc_head(company, "Corrective Action Closure Record", scope) + note + tbl
    return title, inner


# ── dispatch ────────────────────────────────────────────────────────────────────
def generate(gap_id: str, *, company: str, company_id: str,
             scope: str = "") -> Optional[Dict[str, str]]:
    """Build the fix document for one gap id. Returns {'title', 'html'} (html is a
    finished, print-ready page) or None if the gap has no auto-fix."""
    company = (company or "").strip() or "Company"
    scope = (scope or "").strip()
    gid = (gap_id or "").strip()

    if gid in ("programs-missing", "programs-specificity"):
        title, inner = _combined_manual(company, company_id, scope)
    elif gid == "dna-program":
        title, inner = _dna_program(company, scope)
    elif gid == "training-records":
        title, inner = _training_matrix(company, company_id, scope)
    elif gid == "oq-individual":
        title, inner = _oq_tracker(company, scope)
    elif gid == "emr-letter":
        title, inner = _emr_letter(company, scope)
    elif gid == "coi-endorsements":
        title, inner = _coi_letter(company, scope)
    elif gid == "rates-reconcile":
        title, inner = _rates_worksheet(company, scope)
    elif gid in ("capa-overdue", "capa-open"):
        title, inner = _capa_record(company, scope)
    else:
        return None

    html = _cmp.wrap_document(inner, title)
    return {"title": title, "html": html}
