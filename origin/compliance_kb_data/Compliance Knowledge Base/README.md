# Origin Compliance Knowledge Base

A structured, retrieval-ready corpus of the written-program requirements behind OSHA
(29 CFR 1910 & 1926), DOT/FMCSA (49 CFR), oil & gas specialty standards, the major
prequalification platforms, and core EPA programs. Built so the Origin agent can pull
authoritative, write-ready facts when drafting or auditing a contractor's safety programs.

**75 entries · 8 categories · 6 letter templates · schema v2.0**

New in v2: safety metrics with formulas and current BLS benchmarks (EMR, TRIR, DART, LTIR,
Severity, Fatality), an Insurance & COI category (ACORD 25, CGL/WC/Auto/Umbrella, and the
AI / P&NC / Waiver-of-Subrogation / Notice-of-Cancellation endorsements with exact ISO form
numbers), a `Templates/` folder of ready-to-fill compliance letters, and `INTEGRATION.md`
wiring the corpus into Origin.

## The honest premise this KB is built on

No prequalification agency — ISNetworld, Avetta, Veriforce/PEC, BROWZ, ComplyWorks —
publishes an "approved" downloadable template. They **review each contractor's own written
program** against the underlying OSHA/DOT standard plus the hiring client's requirements.
ISN's review arm (RAVS) grades programs A / B / F. So the product is never "here's a
template" — it's a correctly written, company-specific program that cites the right standard
and contains every required element. This KB is the source of those elements and the reasons
reviewers reject programs.

## What's in each entry

Every standard is one Markdown file with YAML frontmatter, plus a mirrored record in
`corpus.jsonl`. Fields:

| Field | Meaning |
|---|---|
| `id` | Stable slug (also the filename) |
| `title` | Human name of the program |
| `category` | One of the 6 top-level folders |
| `citation` | Exact CFR/standard reference |
| `related_citations` | Sub-parts or linked standards |
| `written_program` | `Yes` / `Conditional` / `Reference` — is a written plan required |
| `applicability` | The trigger — who/what the standard covers |
| `required_elements` | Every element the written program must contain (the core drafting checklist) |
| `training` | Training + refresher obligations |
| `recordkeeping` | Retention requirements |
| `failure_points` | Why reviewers reject it — the RAVS/audit rejection reasons |
| `agencies` | Which platforms commonly require it (`Y` commonly, `C` client/scope-driven) |
| `source` | Authoritative source URL (OSHA/eCFR/FMCSA/API/EPA) |
| `template` | Free starter/sample link when a legitimate public one exists |
| `notes` | Agent-facing guidance on how contractors are actually graded |

## Files

```
Compliance Knowledge Base/
├── index.json          # metadata + lightweight entry index (id, title, citation, agencies, file path)
├── corpus.jsonl        # one full JSON record per line — the retrieval/embedding source
├── README.md           # this file
├── INTEGRATION.md      # schema + retrieval/FastAPI interface + letter-generation grounding rules for Origin
├── 01 - General Industry (29 CFR 1910)/     (24)
├── 02 - Construction (29 CFR 1926)/         (13)
├── 03 - Oil & Gas and Energy Specialty/      (7)
├── 04 - DOT and FMCSA (49 CFR)/              (9)
├── 05 - Prequalification Agencies (Process)/ (4)
├── 06 - Environmental (EPA)/                 (3)
├── 07 - Safety Metrics & KPIs/               (6)   EMR, TRIR, DART, LTIR, Severity, Fatality
├── 08 - Insurance & COI Verification/        (9)   ACORD 25, CGL/WC/Auto/Umbrella, AI/P&NC/WOS/NOC endorsements
└── Templates/                                (6 letter templates + templates_index.json)
```

Metric and insurance entries add three optional fields — `formula`, `calc_example`, `benchmarks`
— on top of the standard schema. See `INTEGRATION.md` §2 for the full field list.

## How the Origin agent should ingest it

**Option A — embed `corpus.jsonl` (recommended for semantic retrieval).**
Each line is a complete record. Concatenate the retrieval-relevant fields into one text blob
per record, embed it, store `id` + `citation` as metadata, and index in your vector store.

```python
import json

def to_document(rec):
    parts = [
        rec["title"], rec["citation"], rec["applicability"],
        "Required elements: " + "; ".join(rec["required_elements"]),
        "Failure points: " + "; ".join(rec["failure_points"]),
        rec.get("training",""), rec.get("recordkeeping",""), rec.get("notes",""),
    ]
    return {
        "id": rec["id"],
        "citation": rec["citation"],
        "category": rec["category"],
        "text": "\n".join(p for p in parts if p),
        "source": rec["source"],
    }

docs = [to_document(json.loads(l)) for l in open("corpus.jsonl")]
# embed docs[i]["text"] -> your vector DB, keep id/citation/source as metadata
```

**Option B — keyword / citation lookup.** Load `index.json`, match the user's need to a
`citation` or `title`, then read the full Markdown file named in the entry's `file` field.

**Grounding rule for the agent:** when drafting a written program, pull the matching entry,
copy the `required_elements` as the section skeleton, cite the `citation`, and check the draft
against `failure_points` before returning it. Always surface the `source` URL so the human can
verify. Never claim a specific agency "approved" a template — frame it as "meets the
requirements of [citation] that RAVS/Avetta review against."

## Coverage

**General Industry (1910):** EAP, Fire Prevention, Hearing Conservation, PSM (all 14 elements),
HAZWOPER, PPE hazard assessment, Respiratory Protection, Permit-Required Confined Space,
Lockout/Tagout, Powered Industrial Trucks, Electrical safe work practices, Hot Work, Bloodborne
Pathogens, HazCom/GHS, Lab Chemical Hygiene, the expanded-standard substances (Asbestos, Lead,
Hexavalent Chromium, Cadmium, Formaldehyde, Benzene, Respirable Silica), Access to Records
(1910.1020), Recordkeeping (1904).

**Construction (1926):** general safety & health provisions, Fall Protection, Scaffolding,
Excavation, Steel Erection, Cranes & Derricks, Ladders, Assured Grounding/GFCI, Silica Exposure
Control Plan, Lead, Asbestos, Confined Spaces in Construction, Demolition.

**Oil & Gas / Energy:** H2S (ANSI/ASSP Z390.1), SIMOPS, SEMS/API RP 75, Short Service Employee,
Stop Work Authority, Journey Management, Well Control.

**DOT / FMCSA (49 CFR):** Part 40 & Part 382 drug/alcohol, Part 391 Driver Qualification,
Part 395 Hours of Service, Part 396 inspection/DVIR, Part 393 cargo securement, Part 380 ELDT,
Part 172 hazmat security, Pipeline Operator Qualification (192/195).

**Prequalification agencies (process, not standards):** ISNetworld/RAVS, Avetta, Veriforce/PEC,
BROWZ & ComplyWorks.

**Environmental (EPA):** SPCC (40 CFR 112), SWPPP, RCRA hazardous-waste generator.

## Maintaining it

Regenerate the whole corpus from the single source generator (`kb_engine.py`). Add or edit an
entry there via the `E(...)` helper, rerun, and `index.json` + `corpus.jsonl` + the Markdown
files all rebuild together. Bump `schema_version` in the generator if you change the field set.

## Disclaimer

This is a drafting and audit-support reference, not legal advice or a guarantee of any grade.
Requirements change and hiring clients layer their own rules on top of the federal minimums.
Always confirm against the linked authoritative source and the specific client's requirements
before submitting a program for review.
