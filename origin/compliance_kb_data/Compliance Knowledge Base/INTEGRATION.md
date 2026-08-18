# Origin Integration Framework — Compliance Knowledge Base

How to wire this knowledge base into the Origin engine (Python / FastAPI AI-agent app) so it
generates first-time-pass compliance letters and written programs with zero guesswork. This is
the contract between the corpus and the code.

## 1. What Origin consumes

Three machine-readable artifacts, all in this folder:

| File | Shape | Use |
|---|---|---|
| `corpus.jsonl` | one JSON object per line, 75 records | RAG source — embed these |
| `index.json` | metadata + lightweight entry index | fast citation/title lookup, coverage listing |
| `Templates/templates_index.json` + `Templates/*.md` | 6 letter/document templates | letter generation |

## 2. Entry schema (schema_version 2.0)

Every record in `corpus.jsonl` has these fields. Fields added in v2 are optional and empty (`""`)
on standards that don't use them (they carry the EMR/TRIR/DART/insurance content).

```jsonc
{
  "id": "29-cfr-1910-119-...",        // stable slug, also the .md filename
  "title": "Process Safety Management ...",
  "category": "01 - General Industry (29 CFR 1910)",  // one of 8 categories
  "citation": "29 CFR 1910.119",       // exact regulatory/standard reference
  "related_citations": ["29 CFR 1910.119(h)"],
  "written_program": "Yes|Conditional|Reference",
  "applicability": "…trigger / scope…",
  "required_elements": ["…", "…"],     // the drafting skeleton (or metric components)
  "training": "…", "recordkeeping": "…",
  "failure_points": ["…", "…"],        // reviewer/RAVS rejection reasons — the QA checklist
  "agencies": {"ISN":"Y","Avetta":"Y","Veriforce":"C","PEC":""},  // Y=commonly, C=client-driven
  "source": "https://…",               // authoritative URL (always surface this)
  "template": "https://…",             // free public sample, or ""
  "notes": "…agent guidance…",
  // v2 additions (populated for category 07 Metrics and 08 Insurance):
  "formula": "TRIR = (cases × 200,000) / hours",
  "calc_example": "8 cases / 430,000 hrs → 3.72",
  "benchmarks": "BLS SOII 2024: construction ≈ 2.3–2.5 …"
}
```

Categories: `01` General Industry (1910) · `02` Construction (1926) · `03` Oil & Gas / Energy ·
`04` DOT / FMCSA (49 CFR) · `05` Prequalification Agencies · `06` Environmental (EPA) ·
`07` Safety Metrics & KPIs · `08` Insurance & COI Verification.

## 3. Ingestion — build the retrieval index

Embed one text blob per record, keep `id`/`citation`/`category`/`source` as metadata.

```python
import json

def to_document(rec: dict) -> dict:
    parts = [
        rec["title"], rec["citation"], rec["applicability"],
        ("Formula: " + rec["formula"]) if rec.get("formula") else "",
        ("Example: " + rec["calc_example"]) if rec.get("calc_example") else "",
        "Required elements: " + "; ".join(rec["required_elements"]),
        ("Benchmarks: " + rec["benchmarks"]) if rec.get("benchmarks") else "",
        ("Training: " + rec["training"]) if rec["training"] else "",
        ("Recordkeeping: " + rec["recordkeeping"]) if rec["recordkeeping"] else "",
        "Failure points: " + "; ".join(rec["failure_points"]),
        rec.get("notes", ""),
    ]
    return {
        "id": rec["id"],
        "text": "\n".join(p for p in parts if p),
        "meta": {k: rec[k] for k in ("citation", "category", "source", "agencies", "written_program")},
    }

docs = [to_document(json.loads(l)) for l in open("corpus.jsonl") if l.strip()]
# → embed doc["text"] into your vector store (pgvector, Chroma, FAISS, etc.),
#   storing doc["id"] and doc["meta"] alongside each vector.
```

For a small corpus (75 records) you can also skip embeddings and inject `index.json` + the
matched full record directly into the system prompt — it's compact enough.

## 4. Retrieval interface

A minimal loader Origin can import. No external deps; loads once at startup.

```python
# compliance_kb.py
import json, functools
from pathlib import Path

KB_DIR = Path(__file__).parent / "Compliance Knowledge Base"

@functools.lru_cache(maxsize=1)
def _records() -> dict:
    recs = {}
    for line in (KB_DIR / "corpus.jsonl").read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            recs[r["id"]] = r
    return recs

def get(entry_id: str) -> dict | None:
    return _records().get(entry_id)

def by_citation(citation: str) -> dict | None:
    return next((r for r in _records().values() if r["citation"] == citation), None)

def search(query: str, limit: int = 5) -> list[dict]:
    """Keyword fallback when no vector store is wired. Ranks by term overlap."""
    q = {t for t in query.lower().split() if len(t) > 2}
    scored = []
    for r in _records().values():
        hay = f"{r['title']} {r['citation']} {r['applicability']} {' '.join(r['required_elements'])}".lower()
        scored.append((sum(t in hay for t in q), r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for s, r in scored[:limit] if s > 0]

def templates() -> dict:
    return json.loads((KB_DIR / "Templates" / "templates_index.json").read_text())

def template_body(template_id: str) -> str:
    return (KB_DIR / "Templates" / f"{template_id}.md").read_text()
```

## 5. FastAPI endpoints (drop into Origin's server)

```python
from fastapi import APIRouter, HTTPException
import compliance_kb as kb

router = APIRouter(prefix="/compliance", tags=["compliance"])

@router.get("/standards")
def list_standards(category: str | None = None):
    recs = kb._records().values()
    if category:
        recs = [r for r in recs if r["category"].startswith(category)]
    return [{"id": r["id"], "title": r["title"], "citation": r["citation"],
             "category": r["category"], "agencies": r["agencies"]} for r in recs]

@router.get("/standards/{entry_id}")
def get_standard(entry_id: str):
    r = kb.get(entry_id)
    if not r:
        raise HTTPException(404, "unknown standard")
    return r

@router.get("/search")
def search(q: str, limit: int = 5):
    return kb.search(q, limit)

@router.get("/templates")
def list_templates():
    return kb.templates()
```

Register with `app.include_router(router)` in Origin's main. This gives the agent (and the web
UI) live access to the codified standards and templates.

## 6. Letter-generation pipeline (the grounding rules)

When Origin drafts a written program or compliance letter, it MUST ground on the KB — this is
what makes output audit-ready instead of hallucinated:

1. **Resolve the standard.** Map the user's need to a KB entry via `by_citation()` or `search()`.
2. **Draft from `required_elements`.** For a written program, load template
   `06-written-program-skeleton`, insert the entry's `required_elements` as sections, and write
   company-specific procedure under each. Copy `training` and `recordkeeping` verbatim.
3. **Cite exactly.** Use the entry's `citation` string. Never claim any agency "approved" a
   template — phrase it as "meets the requirements of [citation]."
4. **Self-check against `failure_points`.** Before returning, verify the draft does not commit any
   listed failure. This is the single highest-value step for first-time-pass rate.
5. **Surface the `source`.** Include the authoritative URL so the human can verify.
6. **For metrics** (EMR/TRIR/DART), use the entry's `formula` and current `benchmarks`; for
   insurance letters, name the exact endorsement forms from category 08 and enforce
   "certificate ≠ coverage; the endorsement is the coverage."

Suggested system-prompt injection for the agent:

```
You draft OSHA/DOT/insurance compliance documents. You may ONLY assert requirements that appear
in the retrieved Compliance KB entries. Cite the entry's exact `citation`. Build written programs
from the entry's `required_elements`; copy `training` and `recordkeeping` verbatim. Before
finishing, check your draft against the entry's `failure_points` and fix any hit. Never state a
prequalification agency "approved" a template — say it "meets the requirements of [citation]."
Always include the entry's `source` URL. If no entry supports a requirement, say so rather than
inventing one.
```

## 7. Deploying the corpus with Origin

Copy the `Compliance Knowledge Base/` folder into the Origin repo (e.g. `origin/data/compliance/`)
and point `KB_DIR` at it. It's static data — no migration needed. Regenerate with `kb_engine.py`
(and `build_templates.py`) whenever standards change; both write in place. Bump `schema_version`
in `kb_engine.py` if the field set changes, and update this document's section 2.

## 8. Maintenance & accuracy guardrails

- **Benchmarks drift annually.** TRIR/DART/EMR benchmark figures are point-in-time (BLS SOII 2024,
  released Jan 2026). For any letter that quotes an industry average, pull the current BLS figure
  for the contractor's NAICS at generation time; treat the stored `benchmarks` as guidance, not a
  fixed value to print.
- **Endorsement editions matter.** ISO revises forms; the KB names the standard forms
  (CG 20 10 / 20 37 / 20 01 / CG 24 04 / WC 00 03 13). Confirm the edition date on the actual
  endorsement.
- **Client requirements layer on top.** The federal minimum in the KB is the floor; the hiring
  client's contract can require more. Always reconcile against the specific client's spec.
- **Not legal advice.** This corpus supports drafting and audit prep; it does not guarantee a grade.
