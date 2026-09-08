# Origin — Target Architecture

**Document:** ORIGIN-ARCH-001 · **Revision:** A · **Date:** 2026-09-07
**Purpose:** Define where Origin's architecture is going and, critically, *how it gets there from the as-built state without a rewrite*. This reconciles the two persistence realities and the aspirational `SAFETY_INTELLIGENCE_ENGINE_ARCHITECTURE.md` against what actually ships today. Read `origin-system-map.md` first.

---

## 1. Design principles (non-negotiable)

- **One engine, all industries.** Industry differences are *data* (NAICS/activity/jurisdiction → applicable requirements), never forked code or separate apps.
- **Determinism at the core, model at the edge.** Compliance conclusions are produced by code over curated knowledge. The model explains, narrates, and describes images — it is never the source of truth.
- **Resilience by construction.** One `create_app` factory; every subsystem self-registers in an isolated `try/except`; failures degrade one feature, never the app.
- **Traceability end to end.** Every requirement and finding carries: source → requirement → evidence → confidence → corrective action → verification.
- **Reconcile, don't replace.** New structure is additive and flag-guarded; the live file-based path keeps working and stays offline-verifiable throughout.

---

## 2. The reconciliation problem (the central decision)

Origin has **two persistence realities** today:

1. **File-based JSON on `ORIGIN_DATA_DIR`** — powers the live SIE (6 engines) and `portal.py`. Deterministic, offline-verifiable, Postgres deliberately retired here.
2. **`platform_db.py` (SQLAlchemy/Postgres)** — real models for the white-label platform, but AI-walled OFF and not the live customer path.

The published architecture package proposes **PostgreSQL + pgvector + knowledge graph** as the target. That is aspirational. Adopting it as-written would mean rewriting the live, trusted, offline SIE — which violates the mandate.

### The spine decision (recommended)

Introduce a **derived spine** — a queryable index of the compliance chain — that is *built from* the file-based JSON, never authoritative over it:

```
Source of truth  ──►  Derived spine (index/read-model)  ──►  Cross-entity queries
(JSON on volume)      rebuildable via rebuild_spine()        ("why", "what's missing")
```

- The spine can live first as append-only index files on the volume (keeps the offline guarantee), and later, *if and only if* multi-tenant query load demands it, be materialized into `platform_db` behind a flag — with the file path still passing the no-LLM offline test.
- This gives the "knowledge graph / evidence graph" capability the vision wants **without** moving compliance logic or the source of truth into a database.

**Why this over "just adopt Postgres":** it preserves determinism, the offline CI gate, and the retirement-of-Postgres decision that the whole SIE was built on, while still unlocking cross-company querying. The database, if it arrives, indexes truth — it doesn't become truth.

---

## 3. Target layered architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Presentation:  webui/ static HTML + PWA  (sie.html console,  │
│                photo_audit, portal/admin/gc/sub)             │
├─────────────────────────────────────────────────────────────┤
│ API:  ONE create_app() factory · _auth (token OR session)    │
│       every subsystem self-registers in isolated try/except  │
├─────────────────────────────────────────────────────────────┤
│ Intelligence core (DETERMINISTIC, no model calls):           │
│   Applicable Requirements Engine  (DNA → what applies + why) │
│   Citation · CAPA · Company/Risk · Program · Audit · Prequal │
│   Monitor · Recordability · Gap Finder                       │
├─────────────────────────────────────────────────────────────┤
│ Evidence spine (derived, rebuildable):                       │
│   company·requirement·finding·CAPA·evidence·training edges   │
├─────────────────────────────────────────────────────────────┤
│ Knowledge (read-only corpus):  corpus.jsonl, osha_verbatim,  │
│   osha_index, naics, sic_naics, state_plans, fmcsa, epa/blm, │
│   prequal-process, abatement playbook, training reqs         │
├─────────────────────────────────────────────────────────────┤
│ Model layer (EDGE ONLY):  origin/llm/ build_provider(cfg)    │
│   anthropic·openai·grok·gemini·ollama·llamacpp — explainer   │
├─────────────────────────────────────────────────────────────┤
│ Persistence:  file-based JSON on ORIGIN_DATA_DIR (source of  │
│   truth)  +  platform_db (tenants/index, flag-guarded)       │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. Tenancy (target)

- SIE becomes **tenant-scoped**. Recommended: ride tenancy on the existing `portal.py` session-slug isolation for the live path, and treat `platform_db` tenants as the same identity space when the index is materialized — so the two realities converge on *one* tenant concept rather than two.
- Owner/admin retains a global superuser lens; GCs see only their own companies.
- No cross-tenant bleed: every spine query is tenant-filtered at the read model.

---

## 5. Auth (target)

- Keep the bridged model: `_auth` accepts internal token **OR** valid admin/owner portal session — one login for the owner, no second credential (the "professional safety department for every contractor without needing one" principle).
- Long term: unify the two auth systems behind one session concept rather than maintaining two indefinitely, but only after tenancy lands. Bridged-then-unified, never a big-bang swap.

---

## 6. AI boundary (target — detail in `origin-ai-architecture.md`)

- The intelligence core makes **zero** model calls. The model is used for: prose/explanation of an already-computed result, and image description in the inspector. Anything a model produces that will be shown as authoritative passes through the human-review queue.
- `origin/llm/` stays the single provider seam; providers are swappable and can be fully local (ollama/llamacpp) so the whole product can run with no external vendor.

---

## 7. What the target explicitly rejects

- **No app-per-industry.** Ever.
- **No moving compliance logic into a model.**
- **No big-bang migration off files.** The spine is additive and rebuildable.
- **No abandoning the offline CI gate.** Every target-state change keeps "keys unset → still works" green.
- **No blending the four-way classification.**

---

## 8. How we know we've arrived

For any company in any covered industry, Origin can: resolve exactly what applies and why (federal vs state-plan), inspect a site by photo, open sourced corrective actions, track them to verification, monitor the portfolio deterministically, and answer "show me the whole chain of evidence behind this" — all with every external model key unset, and all through the one resilient factory the app already uses.
