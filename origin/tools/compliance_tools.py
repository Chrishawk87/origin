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
from .. import compliance_grading as grading
from .base import Tool


def _to_float(v: Any) -> Any:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Any:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


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

    def hiring_client_list(args: Dict[str, Any]) -> str:
        clients = kb.list_hiring_clients()
        if not clients:
            return "No hiring-client profiles loaded."
        by_arch: Dict[str, list] = {}
        for c in clients:
            by_arch.setdefault(c.get("archetype", "other"), []).append(c)
        out = [f"HIRING-CLIENT CATALOG — {len(clients)} operators profiled:"]
        for arch in sorted(by_arch):
            out.append(f"\n{arch}:")
            for c in by_arch[arch]:
                tick = "confirmed" if c["confirmed"] else "archetype (verify)"
                out.append(f"  - {c['hiring_client']}  [{tick}]")
        return "\n".join(out)

    def hiring_client_gaps(args: Dict[str, Any]) -> str:
        client = (args.get("hiring_client") or args.get("operator")
                  or args.get("client") or "").strip()
        industry = (args.get("industry") or args.get("naics")
                    or args.get("code") or "").strip()
        state = (args.get("state") or "").strip() or None
        if not client:
            return ("ERROR: 'hiring_client' (the operator, e.g. 'Chevron') is required. "
                    "Use hiring_client_list to see the catalog.")
        if not industry:
            return ("ERROR: 'industry' or 'naics' (the contractor's trade) is required "
                    "so I can compute the baseline the operator sits on top of.")
        rep = kb.hiring_client_gaps(client, industry, state=state)
        if rep.get("error"):
            return f"ERROR: {rep['error']}"

        ov = rep["overlay"]; ins = ov.get("insurance", {}); perf = ov.get("performance", {})
        pt = ov.get("programs_training", {})
        L = [
            f"GAP REPORT — contractor ({industry}) working under {rep['hiring_client']}",
            f"Archetype: {rep['archetype']}  |  Platforms: {', '.join(ov.get('prequal_platforms', []))}"
            f"  |  Grade target: {ov.get('isn_grade_target')}",
        ]
        if not rep["confirmed"]:
            L.append(f"*** UNVERIFIED PROFILE — {rep['note']} ***")
        base = rep["baseline"]
        L.append(f"\nISN baseline (industry): {base.get('count', 0)} standards "
                 f"(use compliance_profile('{industry}') for the full list).")
        overlay_tag = ("" if rep["confirmed"]
                       else "  [UNVERIFIED — typical values, confirm against live ISN scorecard]")
        L.append(f"\nOPERATOR OVERLAY — extra to close for this client:{overlay_tag}")
        L.append("  Insurance:")
        for k, v in ins.items():
            if isinstance(v, list):
                v = ", ".join(v)
            L.append(f"    - {k}: {v}")
        L.append("  Performance ceilings:")
        for k, v in perf.items():
            L.append(f"    - {k}: {v}")
        if pt.get("extra_written_programs"):
            L.append("  Extra written programs: " + "; ".join(pt["extra_written_programs"]))
        if pt.get("required_training"):
            L.append("  Required training: " + "; ".join(pt["required_training"]))
        if pt.get("drug_alcohol"):        L.append(f"  Drug & alcohol: {pt['drug_alcohol']}")
        if pt.get("background_sse"):       L.append(f"  Background/SSE: {pt['background_sse']}")
        if pt.get("subcontractor_mgmt"):   L.append(f"  Subcontractor mgmt: {pt['subcontractor_mgmt']}")
        if ov.get("grading_flags"):        L.append(f"  Grading flags: {ov['grading_flags']}")
        L.append(f"\nSource: {rep.get('source')}")
        return "\n".join(L)

    def compliance_intake(args: Dict[str, Any]) -> str:
        """Read an uploaded prequal report (PDF/scan/image/docx) and turn it into a
        punch list: platform + operator + required written programs, and — when
        draft=True — auto-generate every needed program document into outputs/."""
        import os
        import re
        import time
        from . import document_tools

        path = (args.get("path") or args.get("file") or "").strip()
        if not path:
            return ("ERROR: 'path' (the uploaded ISN/Avetta/Veriforce/PEC report to "
                    "analyze) is required.")

        text = document_tools.extract_text(path)
        if text.startswith("ERROR"):
            return text
        if not text.strip() or text.lstrip().startswith("(No text"):
            return (f"Could not extract readable text from '{path}'. If it's a scanned "
                    "PDF, OCR (Tesseract) may not be available in this environment.")
        low = text.lower()

        # 1. which prequal platform is this report from
        platforms = {
            "ISNetworld": ["isnetworld", "isnet", "isn grade", "isn®", "isn "],
            "Avetta": ["avetta"],
            "Veriforce": ["veriforce", "pec safety", "pecsafety"],
            "PEC": ["pec premier", "pec basic", "pec "],
            "BROWZ": ["browz"],
            "ComplyWorks": ["complyworks", "comply works"],
        }
        detected = [n for n, kws in platforms.items() if any(k in low for k in kws)]

        # 2. which operator (hiring client) is driving the requirement.
        #    Profiles carry full legal names ("Chevron Oil, Products and Gas") but a
        #    report usually names the brand ("Chevron"), so match the full name OR a
        #    distinctive leading brand token.
        _COMMON = {"oil", "gas", "energy", "pipeline", "power", "products", "company",
                   "corporation", "corp", "inc", "llc", "lp", "group", "americas",
                   "north", "global", "solutions", "services", "the", "and", "of"}
        operator = None
        for c in kb.list_hiring_clients():
            full = (c.get("hiring_client") or "").strip()
            fl = full.lower()
            if len(fl) > 2 and fl in low:
                operator = full
                break
            toks = re.findall(r"[a-z0-9]+", fl)
            brand = next((t for t in toks if len(t) > 3 and t not in _COMMON), "")
            if brand and re.search(r"\b" + re.escape(brand) + r"\b", low):
                operator = full
                break

        # 3. the required standards = industry baseline (if given) ∪ standards the
        #    report itself references by citation/title
        industry = (args.get("industry") or args.get("naics")
                    or args.get("code") or "").strip()
        state = (args.get("state") or "").strip() or None

        required: Dict[str, dict] = {}
        prof = None
        if industry:
            prof = kb.naics_applicable(industry, state=state)
            for s in prof.get("standards", []):
                required[s["id"]] = s
        for r in kb.resolve_standards(low, limit=25):
            required.setdefault(r["id"], {
                "id": r["id"],
                "title": r.get("title", ""),
                "citation": r.get("citation", ""),
                "category": r.get("category", ""),
                "written_program": r.get("written_program", ""),
            })

        if not required:
            return ("Read the document, but couldn't map it to any KB standard. "
                    "Pass 'industry' (e.g. 'oilfield services') or 'naics' so I can "
                    "scope the baseline, or the report may not cite recognizable "
                    "OSHA/DOT standards.")

        # which of those require a written program
        need_program = [
            r for r in required.values()
            if (r.get("written_program") or "").strip().lower() in ("yes", "conditional")
        ]
        need_program.sort(key=lambda r: (r.get("category", ""), r.get("title", "")))

        draft = args.get("draft", True)
        if isinstance(draft, str):
            draft = draft.strip().lower() not in ("false", "no", "0", "off")

        # 4. report header
        L: List[str] = []
        L.append("COMPLIANCE INTAKE")
        L.append(f"Source file: {os.path.basename(path)}")
        L.append(f"Platform detected: {', '.join(detected) if detected else 'unclear (no platform keyword found)'}")
        if operator:
            L.append(f"Hiring client detected: {operator}  (run hiring_client_gaps for their overlay)")
        if industry:
            sect = (prof or {}).get("sector_label") or (prof or {}).get("sector") or industry
            L.append(f"Industry baseline: {industry} → {(prof or {}).get('count', 0)} standards ({sect})")
        L.append(f"Standards implicated: {len(required)}  |  Written programs required: {len(need_program)}")

        # 5. the punch list
        L.append("\nWRITTEN PROGRAMS TO SUBMIT:")
        if not need_program:
            L.append("  (none of the implicated standards require a written program)")
        else:
            by_cat: Dict[str, list] = {}
            for r in need_program:
                by_cat.setdefault(r.get("category", "Other"), []).append(r)
            for cat in sorted(by_cat):
                L.append(f"\n  {cat}")
                for r in by_cat[cat]:
                    cite = f" [{r['citation']}]" if r.get("citation") else ""
                    L.append(f"    - {r.get('title', '')}{cite}  (id: {r['id']})")

        # 6. optionally draft every needed program to disk
        if draft and need_program:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            out_dir = os.path.join("outputs", f"intake_{stamp}")
            os.makedirs(out_dir, exist_ok=True)
            written: List[str] = []
            failed: List[str] = []
            for r in need_program:
                doc = kb.render_program(r["id"])
                if not doc:
                    failed.append(r["id"])
                    continue
                slug = re.sub(r"[^a-z0-9]+", "-", r["id"].lower()).strip("-")
                fn = os.path.join(out_dir, f"program-{slug}.md")
                try:
                    with open(fn, "w", encoding="utf-8") as fh:
                        fh.write(doc)
                    written.append(fn)
                except Exception as e:
                    failed.append(f"{r['id']} ({e})")

            # index / punch-list report alongside the drafts
            report = [
                f"# Compliance intake — {os.path.basename(path)}",
                "",
                f"- Platform: {', '.join(detected) if detected else 'unclear'}",
                f"- Hiring client: {operator or 'not detected'}",
                f"- Industry baseline: {industry or 'not provided'}",
                f"- Written programs drafted: {len(written)} of {len(need_program)}",
                "",
                "## Programs in this folder",
            ]
            for fn in written:
                report.append(f"- {os.path.basename(fn)}")
            report.append("")
            report.append("Each program has {{PLACEHOLDER}} fields and [[...]] prompts to fill "
                          "with the contractor's specifics. After filling, run compliance_check "
                          "on each before submitting.")
            try:
                with open(os.path.join(out_dir, "00_INTAKE_REPORT.md"), "w", encoding="utf-8") as fh:
                    fh.write("\n".join(report))
            except Exception:
                pass

            L.append(f"\nDRAFTED {len(written)} program document(s) → {out_dir}/")
            L.append("  (each is pre-filled from the KB with fillable placeholders; "
                     "complete the [[...]] prompts, then run compliance_check before submitting)")
            if failed:
                L.append(f"  Could not draft: {', '.join(failed)}")
        elif need_program:
            L.append("\n(Set draft=true to auto-generate every program document above into outputs/.)")

        L.append("\nNext: fill each program's placeholders, run compliance_check per document, "
                 "and if an operator was detected, run hiring_client_gaps for their extra overlay.")
        return "\n".join(L)

    def safety_metrics(args: Dict[str, Any]) -> str:
        """Compute TRIR / DART / LTIR / severity from OSHA-300 counts + hours (or
        accept the rates directly), and benchmark against the BLS industry average."""
        industry = (args.get("industry") or args.get("naics") or "").strip() or None
        m = grading.compute_metrics(
            hours=_to_float(args.get("hours")),
            recordables=_to_float(args.get("recordables")),
            dart_cases=_to_float(args.get("dart_cases")),
            days_away_cases=_to_float(args.get("days_away_cases")),
            lost_days=_to_float(args.get("lost_days")),
            fatalities=_to_float(args.get("fatalities")),
            industry=industry,
            emr=_to_float(args.get("emr")),
        )
        # let a caller pass precomputed rates when they don't have raw counts
        for k in ("trir", "dart", "ltir"):
            if m.get(k) is None and args.get(k) is not None:
                m[k] = _to_float(args.get(k))
        b = m["benchmark"]
        L = ["SAFETY METRICS (OSHA 29 CFR 1904 formula: cases x 200,000 / hours worked)"]
        L.append(f"Benchmark: {b['label']} — BLS avg TRIR {b['trir']}, DART {b['dart']}")
        if m.get("hours"):
            L.append(f"Hours worked: {m['hours']:g}")
        def _line(label, val, verdict):
            if val is None:
                return None
            return f"  {label}: {val}" + (f"  — {verdict}" if verdict else "")
        for label, key, vkey in (("TRIR", "trir", "trir_vs_bls"),
                                  ("DART", "dart", "dart_vs_bls"),
                                  ("LTIR", "ltir", None),
                                  ("Severity rate", "severity_rate", None)):
            ln = _line(label, m.get(key), m.get(vkey) if vkey else None)
            if ln:
                L.append(ln)
        if m.get("emr") is not None:
            L.append(f"  EMR: {m['emr']:g}  — {m.get('emr_verdict','')}")
        if m.get("fatalities"):
            L.append(f"  Fatalities: {m['fatalities']:g}")
        for n in m.get("notes", []):
            L.append(f"  NOTE: {n}")
        L.append("\nEMR uses NCCI 3-yr workers'-comp loss data (1.00 = industry baseline); "
                 "most operators cap EMR at 1.0. BLS averages shift yearly — verify the "
                 "current figure for the exact NAICS before quoting.")
        return "\n".join(L)

    def grade_estimate(args: Dict[str, Any]) -> str:
        """Estimate a contractor's grade on a prequal platform from component inputs."""
        platform = (args.get("platform") or "ISN").strip()
        inputs = {
            "programs_required": _to_int(args.get("programs_required")),
            "programs_complete": _to_int(args.get("programs_complete")),
            "insurance_required": _to_int(args.get("insurance_required")),
            "insurance_met": _to_int(args.get("insurance_met")),
            "insurance_ok": args.get("insurance_ok"),
            "emr": _to_float(args.get("emr")),
            "emr_cap": args.get("emr_cap"),
            "trir": _to_float(args.get("trir")),
            "trir_cap": args.get("trir_cap"),
            "dart": _to_float(args.get("dart")),
            "dart_cap": args.get("dart_cap"),
            "industry": (args.get("industry") or "").strip() or None,
            "open_citations": _to_int(args.get("open_citations")),
            "msq_complete": _to_float(args.get("msq_complete")),
            "training_complete": _to_float(args.get("training_complete")),
            "fatalities": _to_int(args.get("fatalities")),
        }
        r = grading.estimate_grade(platform, inputs)
        return _fmt_grade(r)

    def _fmt_grade(r: Dict[str, Any]) -> str:
        if r.get("score") is None:
            return "GRADE ESTIMATE — insufficient inputs\n" + "\n".join(
                f"  - {d}" for d in r.get("drivers", []))
        L = [f"ESTIMATED {r['platform']} GRADE: {r['grade']}  (score {r['score']}/100)",
             f"Status: {r.get('traffic_light','')}"]
        if r.get("components"):
            L.append("Component scores (0-1): " + ", ".join(
                f"{k} {v}" for k, v in r["components"].items()))
        if r.get("drivers"):
            L.append("\nWhat's driving it:")
            L += [f"  - {d}" for d in r["drivers"]]
        if r.get("gates"):
            L.append("\nBlocking gates:")
            L += [f"  ! {g}" for g in r["gates"]]
        L.append("\n" + grading.CAVEAT)
        return "\n".join(L)

    def compliance_audit(args: Dict[str, Any]) -> str:
        """Full scored readiness audit: score the contractor against a target
        operator/industry, project the prequal grade, and list prioritized fixes."""
        industry = (args.get("industry") or args.get("naics") or args.get("code") or "").strip()
        state = (args.get("state") or "").strip() or None
        client = (args.get("hiring_client") or args.get("operator") or "").strip()
        if not industry:
            return ("ERROR: 'industry' or 'naics' (the contractor's trade) is required so I can "
                    "scope the required programs. Optionally add 'hiring_client' for the "
                    "operator overlay, plus stats (emr, trir, dart) and programs_complete.")

        # 1. required written programs = industry baseline (+ operator extras)
        prof = kb.naics_applicable(industry, state=state)
        base_std = prof.get("standards", []) if isinstance(prof, dict) else []
        base_programs = [s for s in base_std
                         if (s.get("written_program") or "").strip().lower() in ("yes", "conditional")]
        programs_required = len(base_programs)

        overlay = None
        platform = (args.get("platform") or "").strip()
        emr_cap = args.get("emr_cap")
        trir_cap = args.get("trir_cap")
        dart_cap = args.get("dart_cap")
        grade_target = None
        extra_programs: List[str] = []
        if client:
            rep = kb.hiring_client_gaps(client, industry, state=state)
            if not rep.get("error"):
                overlay = rep
                ov = rep.get("overlay", {})
                perf = ov.get("performance", {})
                emr_cap = emr_cap if emr_cap is not None else perf.get("emr_max")
                trir_cap = trir_cap if trir_cap is not None else perf.get("trir_threshold")
                dart_cap = dart_cap if dart_cap is not None else perf.get("dart_threshold")
                grade_target = ov.get("isn_grade_target")
                extra_programs = list((ov.get("programs_training", {}) or {}).get("extra_written_programs", []) or [])
                programs_required += len(extra_programs)
                if not platform:
                    plats = ov.get("prequal_platforms") or []
                    platform = (plats[0].split()[0] if plats else "ISN")
        platform = platform or "ISN"

        # thresholds that are pure BLS-relative ("<= NAICS avg") carry no number;
        # grading falls back to BLS automatically when the cap parses to None.
        inputs = {
            "programs_required": programs_required,
            "programs_complete": _to_int(args.get("programs_complete")) or 0,
            "insurance_required": _to_int(args.get("insurance_required")),
            "insurance_met": _to_int(args.get("insurance_met")),
            "insurance_ok": args.get("insurance_ok"),
            "emr": _to_float(args.get("emr")),
            "emr_cap": emr_cap,
            "trir": _to_float(args.get("trir")),
            "trir_cap": trir_cap,
            "dart": _to_float(args.get("dart")),
            "dart_cap": dart_cap,
            "industry": industry,
            "open_citations": _to_int(args.get("open_citations")),
            "msq_complete": _to_float(args.get("msq_complete")),
            "training_complete": _to_float(args.get("training_complete")),
            "fatalities": _to_int(args.get("fatalities")),
        }
        # if raw counts + hours given, compute the rates first
        if args.get("hours") and (args.get("recordables") is not None or args.get("dart_cases") is not None):
            m = grading.compute_metrics(
                hours=_to_float(args.get("hours")),
                recordables=_to_float(args.get("recordables")),
                dart_cases=_to_float(args.get("dart_cases")),
                industry=industry, emr=inputs["emr"])
            inputs["trir"] = inputs["trir"] if inputs["trir"] is not None else m.get("trir")
            inputs["dart"] = inputs["dart"] if inputs["dart"] is not None else m.get("dart")

        r = grading.estimate_grade(platform, inputs)

        L = [f"COMPLIANCE READINESS AUDIT — {industry}"
             + (f" under {overlay['hiring_client']}" if overlay else "") + ""]
        L.append(f"Target platform: {platform}"
                 + (f"  |  Operator grade target: {grade_target}" if grade_target else ""))
        if overlay and not overlay.get("confirmed", False):
            L.append("")
            L.append("*** UNVERIFIED OPERATOR PROFILE — the insurance limits, EMR/TRIR/DART "
                     f"caps, and required programs used below are TYPICAL values for the "
                     f"'{overlay.get('archetype','?')}' archetype, NOT confirmed against "
                     f"{overlay['hiring_client']}'s live ISN requirement list. Treat the projected "
                     "grade as indicative only, and confirm every limit against the client's actual "
                     "scorecard before quoting it to anyone. ***")
        L.append("")
        L.append(_fmt_grade(r))

        # prioritized remediation
        fixes: List[str] = []
        pc = inputs["programs_complete"] or 0
        if programs_required and pc < programs_required:
            fixes.append(f"Draft/submit {programs_required - pc} of {programs_required} required "
                         f"written programs (run compliance_profile('{industry}') for the list"
                         + (", plus operator extras below" if extra_programs else "") + ").")
        if inputs.get("emr") is not None and grading.parse_cap(emr_cap):
            cap = grading.parse_cap(emr_cap)
            if inputs["emr"] > cap:
                fixes.append(f"EMR {inputs['emr']:g} exceeds the {cap:g} cap — attach an EMR "
                             "explanation letter and corrective-action history; it rolls off over 3 yrs.")
        if inputs.get("insurance_ok") is not True and inputs.get("insurance_met") is not None \
                and inputs.get("insurance_required") and inputs["insurance_met"] < inputs["insurance_required"]:
            fixes.append("Close the insurance gap — get COIs with the required limits + endorsements "
                         "(additional insured, waiver of subrogation, primary & non-contributory).")
        if extra_programs:
            fixes.append("Operator-specific extra programs to confirm/submit: " + ", ".join(extra_programs))
        if overlay and not overlay.get("confirmed", False):
            fixes.append("NOTE: operator overlay is archetype-seeded — verify against this client's "
                         "live requirement list before quoting.")
        if fixes:
            L.append("\nPRIORITIZED REMEDIATION:")
            L += [f"  {i}. {f}" for i, f in enumerate(fixes, 1)]
        return "\n".join(L)

    def grade_calibrate(args: Dict[str, Any]) -> str:
        """Record a REAL scorecard (platform + inputs + the actual grade earned) so
        the estimator can be tuned to reality over time."""
        platform = (args.get("platform") or "").strip().upper()
        actual = (args.get("actual_grade") or "").strip().upper()
        if not platform or not actual:
            return ("ERROR: 'platform' (e.g. 'ISN') and 'actual_grade' (the real letter/score, "
                    "e.g. 'B') are required to record a calibration sample.")
        cal = grading.load_calibration()
        samples = cal.setdefault("samples", [])
        sample = {"platform": platform, "actual_grade": actual,
                  "inputs": {k: v for k, v in args.items()
                             if k not in ("platform", "actual_grade")}}
        samples.append(sample)
        ok = grading.save_calibration(cal)
        if not ok:
            return ("Recorded in memory but could NOT persist the calibration file "
                    "(read-only filesystem?). Set ORIGIN_DATA_DIR to a writable volume.")
        return (f"Recorded calibration sample #{len(samples)} for {platform} (actual grade "
                f"{actual}). With enough samples the grade bands/weights can be tuned to match "
                "your real scorecards. Samples stored at the data volume.")

    return [
        Tool(
            name="compliance_intake",
            description=(
                "THE upload-and-analyze tool. Point it at an uploaded prequalification "
                "report — an ISNetworld / Avetta / Veriforce / PEC / BROWZ requirement "
                "list, scorecard, or REJECTION notice, including a SCANNED PDF or image "
                "(it OCRs) — and it tells the contractor exactly what to submit to pass. "
                "It reads the document, detects the platform and hiring client, maps it "
                "to the required KB written programs, and (draft=true, the default) "
                "auto-generates every needed program document into outputs/ ready to "
                "fill. Give 'industry' or 'naics' too for a complete baseline. Use this "
                "FIRST whenever the user uploads a compliance PDF/scan and asks what it "
                "needs or how to pass."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "The uploaded report to analyze (file name in the workspace or absolute path)."},
                    "industry": {"type": "string",
                                 "description": "Contractor trade for the baseline, e.g. 'oilfield services', 'construction'"},
                    "naics": {"type": "string", "description": "Or a NAICS code, e.g. '213112'"},
                    "state": {"type": "string", "description": "Two-letter state for jurisdiction overlays, e.g. 'CA'"},
                    "draft": {"type": "boolean",
                              "description": "Auto-generate every required program document (default true)"},
                },
                "required": ["path"],
            },
            handler=compliance_intake,
            source="builtin",
        ),
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
        Tool(
            name="hiring_client_list",
            description=(
                "List the hiring clients (operators like Chevron, Energy Transfer, Dow) that "
                "Origin has prequalification requirement profiles for. Call this when the user "
                "asks which operators are covered, or before hiring_client_gaps if unsure of the "
                "exact name."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=hiring_client_list,
            source="builtin",
        ),
        Tool(
            name="hiring_client_gaps",
            description=(
                "Produce a per-OPERATOR gap report for a contractor. Give the hiring client "
                "(e.g. 'Chevron', 'Energy Transfer') and the contractor's industry/NAICS. "
                "Returns the ISN baseline for that trade PLUS the operator's EXTRA requirements "
                "— insurance limits and endorsements, EMR/TRIR/DART ceilings, extra written "
                "programs, and required training — i.e. the additional items to close so the "
                "contractor stays hireable by that specific operator. Values flagged "
                "'archetype' must be confirmed against the client's live ISN list before "
                "quoting. Use compliance_profile for the industry baseline and compliance_"
                "template to draft any missing written program."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hiring_client": {"type": "string", "description": "Operator name, e.g. 'Chevron'"},
                    "industry": {"type": "string", "description": "Contractor trade, e.g. 'oilfield services'"},
                    "naics": {"type": "string", "description": "Or a NAICS code, e.g. '213112'"},
                    "state": {"type": "string", "description": "Two-letter state for jurisdiction overlays"},
                },
                "required": ["hiring_client"],
            },
            handler=hiring_client_gaps,
            source="builtin",
        ),
        Tool(
            name="safety_metrics",
            description=(
                "Calculate and interpret a contractor's OSHA safety metrics — TRIR, DART, "
                "LTIR, and severity rate — from their OSHA-300 counts and hours worked, and "
                "benchmark TRIR/DART against the BLS industry average. Also reads EMR against "
                "the 1.0 baseline. Use this whenever a contractor gives incident numbers or "
                "asks 'is my TRIR/EMR good enough to pass?'. Give raw counts (recordables, "
                "dart_cases, days_away_cases, lost_days) + hours, or pass the rates directly."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hours": {"type": "number", "description": "Total employee hours worked in the year"},
                    "recordables": {"type": "number", "description": "OSHA-recordable cases (for TRIR)"},
                    "dart_cases": {"type": "number", "description": "Days-away/restricted/transfer cases (for DART)"},
                    "days_away_cases": {"type": "number", "description": "Days-away-from-work cases (for LTIR)"},
                    "lost_days": {"type": "number", "description": "Total lost workdays (for severity rate)"},
                    "fatalities": {"type": "number", "description": "Number of fatalities"},
                    "emr": {"type": "number", "description": "Experience Modification Rate (NCCI), e.g. 0.85"},
                    "trir": {"type": "number", "description": "Pass TRIR directly if you don't have raw counts"},
                    "dart": {"type": "number", "description": "Pass DART directly if you don't have raw counts"},
                    "ltir": {"type": "number", "description": "Pass LTIR directly if you don't have raw counts"},
                    "industry": {"type": "string", "description": "Trade/NAICS to pick the right BLS benchmark"},
                },
                "required": [],
            },
            handler=safety_metrics,
            source="builtin",
        ),
        Tool(
            name="grade_estimate",
            description=(
                "Estimate the letter grade / traffic-light a contractor would earn on a prequal "
                "platform (ISNetworld, Avetta, Veriforce, PEC, BROWZ) from their component "
                "status: written-program completeness, insurance completeness, safety stats "
                "(EMR/TRIR/DART), MSQ and training completion, and open citations. Returns an "
                "ESTIMATED grade with the drivers and any blocking gates. This is a research-"
                "grounded projection (the platforms' exact math is proprietary and per-client), "
                "so always present it as an estimate. For a full trade-scoped audit against a "
                "specific operator, use compliance_audit instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "description": "ISN | Avetta | Veriforce | PEC | BROWZ (default ISN)"},
                    "programs_required": {"type": "integer", "description": "Written programs the client requires"},
                    "programs_complete": {"type": "integer", "description": "How many are done + verified"},
                    "insurance_required": {"type": "integer", "description": "Insurance requirements count"},
                    "insurance_met": {"type": "integer", "description": "How many insurance requirements are met"},
                    "insurance_ok": {"type": "boolean", "description": "Shortcut: all insurance met"},
                    "emr": {"type": "number"}, "emr_cap": {"type": "number", "description": "Operator EMR cap (default 1.0)"},
                    "trir": {"type": "number"}, "trir_cap": {"type": "number"},
                    "dart": {"type": "number"}, "dart_cap": {"type": "number"},
                    "industry": {"type": "string", "description": "Trade/NAICS for the BLS benchmark"},
                    "open_citations": {"type": "integer"},
                    "msq_complete": {"type": "number", "description": "0..1 fraction of MSQ complete"},
                    "training_complete": {"type": "number", "description": "0..1 fraction of training records complete"},
                    "fatalities": {"type": "integer"},
                },
                "required": [],
            },
            handler=grade_estimate,
            source="builtin",
        ),
        Tool(
            name="compliance_audit",
            description=(
                "THE auditor tool. Run a full scored readiness audit for a contractor: give "
                "their trade ('industry'/'naics') and optionally the target 'hiring_client' "
                "(e.g. 'Chevron') plus their stats (emr, trir, dart or raw hours+counts), how "
                "many required programs they've completed (programs_complete), and insurance "
                "status. It scopes the required written programs from the KB, layers the "
                "operator's EMR/TRIR/insurance ceilings on top, PROJECTS the prequal grade / "
                "traffic-light, and returns a prioritized remediation punch list. Use this when "
                "asked 'will we pass?', 'what grade will we get?', or 'audit us for <operator>'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "industry": {"type": "string", "description": "Contractor trade, e.g. 'oilfield services'"},
                    "naics": {"type": "string", "description": "Or a NAICS code, e.g. '213112'"},
                    "state": {"type": "string", "description": "Two-letter state for jurisdiction overlays"},
                    "hiring_client": {"type": "string", "description": "Target operator, e.g. 'Chevron' (adds their overlay)"},
                    "platform": {"type": "string", "description": "Prequal platform to grade against (defaults from operator)"},
                    "programs_complete": {"type": "integer", "description": "Required written programs already done + verified"},
                    "insurance_required": {"type": "integer"}, "insurance_met": {"type": "integer"},
                    "insurance_ok": {"type": "boolean"},
                    "emr": {"type": "number"}, "trir": {"type": "number"}, "dart": {"type": "number"},
                    "hours": {"type": "number", "description": "Hours worked (with recordables/dart_cases to compute rates)"},
                    "recordables": {"type": "number"}, "dart_cases": {"type": "number"},
                    "open_citations": {"type": "integer"},
                    "msq_complete": {"type": "number"}, "training_complete": {"type": "number"},
                    "fatalities": {"type": "integer"},
                },
                "required": [],
            },
            handler=compliance_audit,
            source="builtin",
        ),
        Tool(
            name="grade_calibrate",
            description=(
                "Record a REAL prequal scorecard so the grade estimator can be tuned to reality. "
                "Give the 'platform' and the 'actual_grade' the contractor actually received, "
                "plus whatever component inputs you know (same fields as grade_estimate). Stored "
                "to the data volume; over time these samples let the estimator's bands/weights be "
                "calibrated to match how the platforms really grade. Use whenever the user shares "
                "an actual grade result."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "description": "ISN | Avetta | Veriforce | PEC | BROWZ"},
                    "actual_grade": {"type": "string", "description": "The real grade earned, e.g. 'A', 'B', or a score"},
                    "programs_required": {"type": "integer"}, "programs_complete": {"type": "integer"},
                    "emr": {"type": "number"}, "trir": {"type": "number"}, "dart": {"type": "number"},
                    "insurance_ok": {"type": "boolean"},
                    "industry": {"type": "string"},
                },
                "required": ["platform", "actual_grade"],
            },
            handler=grade_calibrate,
            source="builtin",
        ),
    ]
