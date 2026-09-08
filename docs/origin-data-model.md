# Origin — Data Model

**Document:** ORIGIN-DATA-001 · **Revision:** A · **Date:** 2026-09-07
**Purpose:** Describe Origin's data model as it exists (file-based JSON + a gated SQLAlchemy layer) and the target evidence-spine model it evolves toward — additively, without duplicating what already works. Read `origin-system-map.md` and `origin-architecture-target.md` first.

> Principle: the file-based JSON collections remain the **source of truth**. The spine is a **derived, rebuildable index** over them. We do not migrate truth into a database; we index it.

---

## 1. As-built: file-based collections (the live path, source of truth)

All under `ORIGIN_DATA_DIR` (Railway `/data` volume). One JSON per record unless noted.

| Collection | Path | Key | Notes |
|---|---|---|---|
| Company profile | `companies/` | `company_id` (slug) | industry/NAICS, state, headcount, activities, customer_platforms; carries deterministic risk score + band |
| CAPA | `capa/` | `capa_id` | status ladder Draft→Approved→Uploaded→Verified→Closed; links `company_id`, `source_refs` |
| Audit (walk-through) | `audits/` | `audit_id` | company-bound; findings[]; HIGH → auto-CAPA; matched→source_refs, unmatched→human_review |
| Knowledge versions | `knowledge/*.jsonl` | version | append-only versioned regulatory store (`resolve()`) |
| Monitoring | `monitoring/*.json` | alert_id | stable IDs, delta-aware (new/continuing/auto-resolved), meta + last-run |
| Prequal readiness | prequal store | `company_id`+platform | detected vs checklist gaps, each traceable to a KB prequal record |
| Program package | program store | `company_id` | sourced written programs + OSHA-2254 training + JHA/JSA |
| Portal entities | portal store | slug | clients, GCs, subs, messages, documents, folders, COIs |

Joins today are **string keys** (`company_id`), not enforced relations. Cross-entity "why" works within an engine, not across all of them — that is the gap the spine closes.

---

## 2. As-built: SQLAlchemy layer (gated, not the live path)

`platform_db.py` models: `Tenant, User, Subcontractor, ComplianceStatus, COI, Document, Message, GcMessage, LibraryProgram, ActivityLog`. Postgres on Railway, sqlite local fallback. AI features walled OFF via `PLATFORM_AI_WALL`. This is the intended multi-tenant identity home; it is **not** duplicated into by the SIE today.

---

## 3. As-built: knowledge corpus (read-only)

`corpus.jsonl` (single corpus powering chat + Gap Finder + docs), `osha_verbatim.jsonl`, `osha_index.jsonl`, `naics_2022.jsonl` + `naics_map.json`, `sic_naics.jsonl`, `state_plans.jsonl` (jurisdiction resolver), `fmcsa_index.jsonl`, EPA/BLM records, prequal-platform process docs, `abatement_playbook.jsonl`, training requirements. Copyright discipline: restricted standards (e.g., NFPA 70E) stored as citation + summary, never full body.

---

## 4. Target: the evidence spine (derived, additive)

A rebuildable index expressing the compliance chain as typed nodes and edges. First materialization: append-only index files on the volume (preserves offline guarantee); later, optionally, a `platform_db` read-model behind a flag.

### Node types

- **Company** — from `companies/`; carries DNA attributes (industry, NAICS, size, activities, materials, equipment, jurisdiction).
- **Requirement** — a specific applicable obligation resolved for a company, tagged with the four-way classification (OSHA-required / Origin recommendation / Best practice / Customer requirement) and a citation into the corpus.
- **Finding** — an observed condition (from audit/photo/gap analysis) with confidence.
- **CAPA** — corrective action, with its status ladder.
- **Evidence** — a document, photo, training record, or upload that satisfies a requirement or closes a CAPA.
- **Training** — role/employee requirement, assignment, completion, expiration.
- **ReviewItem** — a human-review queue entry (decision, reviewer, timestamp, resulting action).

### Edge types (all directional, all traceable)

```
Company    ──has_applicable──►  Requirement
Requirement──cited_by────────►  Source (corpus record)
Finding    ──violates────────►  Requirement
Finding    ──triggers────────►  CAPA
CAPA       ──satisfied_by────►  Evidence
Requirement──satisfied_by────►  Evidence
Requirement──needs───────────►  Training
Training   ──proven_by───────►  Evidence
Finding    ──flagged_to──────►  ReviewItem   (when unmatched/low-confidence)
```

This is exactly the "source → requirement → evidence → confidence → corrective action → verification" chain, made queryable.

### Rebuild contract

`rebuild_spine()` reconstructs every node and edge purely from the source JSON collections. The spine is therefore **never authoritative and never permanently drift-able** — if it's ever wrong, delete and rebuild. This is what lets us add a graph/relational capability without betraying the file-based source of truth or the offline test.

---

## 5. Target: Company Safety DNA (the input to "what matters")

A company's DNA is not new storage — it is the *typed attribute set already on the company profile*, elevated to first-class inputs for the Applicable Requirements Engine:

`industry / NAICS · headcount band · activities · materials & chemicals · equipment · jurisdiction (state-plan vs federal, from state_plans.jsonl) · customer prequal platforms`

The requirements engine reads DNA and emits the tailored Requirement nodes with an applicability reason per line. No industry forks — the difference is entirely in this data.

---

## 6. What NOT to do

- **Do not** duplicate the file-based collections into `platform_db` as a second source of truth. Index, don't fork.
- **Do not** introduce foreign-key constraints that make the file path require a database to boot.
- **Do not** store restricted copyrighted standard bodies anywhere; keep citation + summary.
- **Do not** blend the four-way classification into a single "requirement" flag — the tag is part of the Requirement node's identity.
- **Do not** let any spine query bypass tenant scoping once tenancy lands.

---

## 7. Migration posture

Additive and reversible at every step: (1) add the spine as derived index files; (2) build the Applicable Requirements Engine reading DNA + corpus; (3) add ReviewItem + Training nodes as those workflows ship; (4) only materialize the spine into `platform_db` if multi-tenant scale demands it, behind a flag, with the offline file path still green. At no point does the live SIE stop working with all external LLM keys unset.
