# Origin — System Map (as-built)

**Document:** ORIGIN-MAP-001 · **Revision:** A · **Date:** 2026-09-07
**Purpose:** A faithful map of what the Origin application *actually is today* — not what it should become. Written from a direct read of the repository (`Chrishawk87/origin`), not from the aspirational architecture package. Read this before changing anything.

> **Golden rule for anyone building on Origin:** this is a working, deployed product with paying-path features live at `app.originprequal.com`. Investigate, then extend. Do not rewrite.

---

## 1. What Origin is, in one paragraph

Origin is a single **Python FastAPI monolith** (repo `Chrishawk87/origin`, deployed on Railway at `app.originprequal.com`, formerly `origin-production-1352.up.railway.app`). It is built around one app factory — `create_app(config=None, engine=None, token=None)` in `origin/server.py` (~1,995 lines) — with **no module-level `app`**. Every feature area registers its own routes into that factory inside an isolated, non-fatal `try/except` block, so a bug in one subsystem can never take down the live site. The product has evolved through several eras that still coexist: (1) an original **local AI compliance agent/CLI**, (2) a **customer/admin/GC/sub portal** (`portal.py`, 4,549 lines, file-based), (3) a **white-label platform** foundation (`platform_*.py`, SQLAlchemy/Postgres, AI-wall gated OFF), and (4) the current focus — the **Safety Intelligence Engine (SIE)**, six deterministic, file-based, no-external-LLM engines with a converged console at `/sie`.

---

## 2. Current architecture

### 2.1 Runtime shape

```
run_app.py / origin-serve.sh
        │
        ▼
origin/server.py :: create_app(config, engine, token)   ← the ONE factory
        │
        ├── HTTP middleware _auth  (gates /api/* by access token OR admin portal session)
        ├── exception handler → always returns JSON (UI never sees an HTML 500)
        │
        ├── core AI/agent routes        (chat, tools, research — orchestra.py, agent.py)
        ├── compliance routes           (gaps.py, rescue.py, recordability.py, hazcom.py)
        ├── knowledge routes            (compliance_kb.py — corpus, verbatim, NAICS, states)
        ├── lead tools                  (leadradar.py, osha_export.py — internal/admin)
        ├── portal.register_portal      (customer + admin + GC + sub, file-based)   portal.py
        ├── abatement.register_abatement
        ├── citation_engine.register_citation      ── SIE P1
        └── SIE block (isolated try/except, ~line 1936):
              capa.register_capa                    ── P2
              company_profile.register_company      ── P2
              program_engine.register_program       ── P3
              audit_engine.register_audit           ── P4
              prequal_engine.register_prequal       ── P5
              monitor_engine.register_monitor       ── P6
```

### 2.2 Layers as they exist today

- **Presentation** — server-rendered static HTML in `origin/webui/` (vanilla JS, no framework/build step). Key pages: `sie.html` (the converged SIE console, 7 tabs), `photo_audit.html`, `admin.html`, `gc.html`, `portal.html`, `dashboard.html`, `gaps.html`, `rescue.html`, `recordability.html`, `hazcom.html`, `index.html`, `app.html` (PWA shell). Installable PWA (`manifest.webmanifest`, `sw.js`, `pwa-install.js`).
- **API** — one FastAPI factory; ~61 `/api/*` routes plus page routes. All `/api/*` gated by `_auth`.
- **Reasoning core** — deterministic Python modules. No LLM in the compliance path. This is real and enforced today for the SIE engines.
- **Knowledge** — file-based: `compliance_kb.py` (2,276 lines) over `compliance_kb_data/Compliance Knowledge Base/` (`corpus.jsonl`, `osha_verbatim.jsonl`, `osha_index.jsonl`, `naics_2022.jsonl`, `naics_map.json`, `sic_naics.jsonl`, `state_plans.jsonl`, `fmcsa_index.jsonl`, `abatement_playbook.jsonl`, plus category folders of `.md` programs).
- **Model layer** — `origin/llm/` provider abstraction: `anthropic`, `openai`, `grok`, `gemini`, `ollama`, `llamacpp` (local). Built via `build_provider(cfg)`. Already swappable.
- **Data** — TWO parallel realities (see §5). SIE + portal features persist as **JSON files on `ORIGIN_DATA_DIR`** (Railway volume, `/data`). A separate `platform_db.py` SQLAlchemy layer (Postgres on Railway, sqlite fallback) backs the white-label platform foundation only.

---

## 3. Current features (by subsystem)

### 3.1 Safety Intelligence Engine (the current product — all 6 phases DONE, deployed)

| Phase | Module | What it does | Persistence |
|---|---|---|---|
| P1 | `knowledge_store.py` + `citation_engine.py` | Versioned regulatory KB (`resolve()`), 12-part sourced citation analyzer | `DATA_DIR/knowledge/*.jsonl` |
| P2 | `capa.py` + `company_profile.py` | CAPA tracker (Draft→Approved→Uploaded→Verified→Closed); company profiles + deterministic explainable risk score `Σ(severity_weight×penalty_factor)×20`, bands Critical≥70/High≥40/Moderate≥15/Low | `DATA_DIR/capa`, `DATA_DIR/companies` |
| P3 | `program_engine.py` | Sourced written programs + verbatim OSHA-2254 training + JHA/JSA package bound to a company; never fabricates | file-based |
| P4 | `audit_engine.py` + `photo_audit.py` | Persists a photo walk-through as a company-bound audit; auto-opens CAPAs for HIGH findings (matched→source_refs, unmatched→human_review); feeds risk | `DATA_DIR/audits` |
| P5 | `prequal_engine.py` | Per-platform ISN/Avetta/Veriforce readiness; honest gap split `detected` vs `checklist`; every gap traceable to a KB prequal record | file-based |
| P6 | `monitor_engine.py` | Autonomous portfolio monitoring; deterministic rule monitors over persisted state; stable alert IDs, delta-aware (new/continuing/auto-resolved); token-guarded cron-sweep twin | `DATA_DIR/monitoring/*.json` |

Console: `webui/sie.html` — one page, 7 tabs (Monitor / Companies / Programs / Prequal / Audits / Citation / Photo Audit). Companies tab has selectable industry dropdown (36 trades → auto-NAICS), state dropdown, and a **jurisdiction resolver** (State Plan / Federal / both) from `state_plans.jsonl`. Photo Audit is an embedded tab. Served at `/sie`; PWA `/app` lands owner/admin here.

### 3.2 Compliance analysis tools (older, still live)

- `gaps.py` (878 lines) — Gap Finder: extract text from uploaded docs, parse ISN deficiency reports, find required-vs-present gaps, draft programs, emit branded `.docx`.
- `rescue.py` (726 lines) — Citation Rescue flow: rejection decoder, readiness engines, abatement plan generation.
- `recordability.py` (506 lines) — OSHA 1904 recordability determination engine → client 300 Log.
- `hazcom.py` (582 lines) — HazCom chemical-inventory module + `hazcom.html`.
- `abatement.py` (340 lines) — ISN upload tracker / abatement status ladder.
- `citation_guard.py` — verification module that keeps chat answers grounded in KB.

### 3.3 Knowledge base (`compliance_kb.py`)

Single-corpus design: `corpus.jsonl` powers chat + Gap Finder + doc generation. Layers: verbatim OSHA text, OSHA index (part/subpart/section), NAICS 2022 catalog + `naics_map`, SIC→NAICS crosswalk, state plans (30 records → jurisdiction resolver), FMCSA/DOT index, EPA/BLM, prequal-platform process docs, abatement playbook, training requirements. Baseline ~153 recommendations; ~116 canonical KB programs (+JHAs). Copyright discipline in place (e.g., NFPA 70E — citation + summary, never full body).

### 3.4 Portal (`portal.py`, file-based — the live customer/admin/GC/sub system)

Customer portal, Origin admin console (`admin.html`), GC workspace (`gc.html`), subcontractor portal. Two-way messaging + attachments, document vault + folders, doc request/fulfillment, compliance status board, COI tracking, logo upload, live client roster with monitoring rollup. Auth: portal **session cookie** (`origin_admin` admin, `origin_client` client), HMAC-signed, PBKDF2 PINs, login lockout, fail-closed admin. Tenant isolation is **session-slug based**.

### 3.5 White-label platform foundation (`platform_*.py` — built, AI-wall gated OFF)

`platform_db.py` (SQLAlchemy: Tenant/User/Subcontractor/ComplianceStatus/COI/Document/Message/GcMessage/LibraryProgram/ActivityLog), `platform_auth.py` (3 roles, tenant scoping), `platform_seed.py`, `platform_console.py`, `platform_gc.py`, `platform_sub.py`, `platform_media.py`. Postgres on Railway (sqlite local fallback). **The AI features are walled off behind `PLATFORM_AI_WALL`.** This is the intended multi-tenant future home but is not the live customer path today — `portal.py` is.

### 3.6 Internal growth tooling (not customer-facing)

`leadradar.py` (1,328 lines — OSHA/DOL/state-OSHA/MSHA/FMCSA citation leads, scored), `osha_export.py` (standalone Mac lead-pull), `contractors.py`, `compliance_grading.py` (estimate ISN/Avetta grade), `seed_dashboard.py`.

### 3.7 Original agent core (foundational, partly dormant)

`agent.py`, `orchestra.py`, `research.py`, `roles.py`, `memory.py`, `enhancer.py`, `executive.py`, `cli.py`, `tools/` (browser, document, media, rest, shell, web, youtube, mcp_client, registry). This is the general AI-agent substrate Origin grew out of.

---

## 4. Current data entities

**File-based (SIE + portal, the live path):** company profile (JSON per company), CAPA (JSON per CAPA), audit (JSON per walk-through), knowledge versions (append-only `versions.jsonl`), monitoring alerts + meta, portal clients/GCs/subs/messages/documents/folders (JSON on volume).

**SQLAlchemy (platform foundation, gated):** `Tenant, User, Subcontractor, ComplianceStatus, COI, Document, Message, GcMessage, LibraryProgram, ActivityLog`.

**Knowledge (read-only corpus):** regulation records, verbatim OSHA text, OSHA index, NAICS, SIC crosswalk, state plans, FMCSA index, abatement playbook, training requirements, prequal-platform process docs.

The key gap: there is **no shared relational spine** linking company → finding → requirement → CAPA → evidence → training. Today those live as separate JSON collections joined by `company_id` string keys. (This is the "evidence graph" the target architecture wants.)

---

## 5. The single most important thing to understand: two persistence realities

1. **SIE + portal = file-based JSON on `ORIGIN_DATA_DIR`** (`/data` Railway volume). Deterministic, offline-verifiable, no DB. **Postgres was deliberately retired for this path.** This is what's live and what every SIE phase was built on.
2. **`platform_db.py` = SQLAlchemy/Postgres**, for the (gated) white-label platform. Real code, real models, but not the live customer path.

The published `SAFETY_INTELLIGENCE_ENGINE_ARCHITECTURE.md` describes a **PostgreSQL + pgvector + knowledge-graph** target. That is **aspirational**, not as-built. Anyone reading only that doc will over-estimate the current persistence layer. Reconciling these two realities is the central architecture decision ahead (see `origin-architecture-target.md`).

Constraint that is genuinely enforced today: **with all LLM API keys unset, the SIE still analyzes citations, scores companies, generates programs, runs audits, and produces alerts.** Verified repeatedly via `ORIGIN_DATA_DIR=$(mktemp -d)` + FastAPI `TestClient` with keys unset.

---

## 6. Existing APIs (representative)

~61 `/api/*` routes. Live SIE families: `/api/citation/*`, `/api/company/*` (upsert/list/portfolio/{id}/risk/scope), `/api/capa/*`, `/api/program/*`, `/api/audit/*`, `/api/prequal/*` (+ `/platforms`), `/api/monitor/*` (sweep/alerts/digest/company/last-run + token-guarded `/cron-sweep`), `/api/scoping/*` (suggest/naics/industries/states/jurisdiction/resolve), `/api/photo-audit/*` (public, session-authed). Older: `/api/gaps/*`, `/rescue/*`, `/api/portal/*`, plus page routes `/sie /app /photo-audit /dashboard /gaps /rescue /recordability /hazcom /citation /scoping`.

Auth model (important, recently fixed): `_auth` middleware gates `/api/*` by the internal access token **OR** a valid admin portal session cookie — so an owner logged into the app reaches SIE endpoints with one login. `/api/photo-audit/` and `/api/monitor/cron-sweep` are exempt (they have their own gates).

---

## 7. Existing AI architecture

`origin/llm/` already implements the "swappable model" idea: `build_provider(cfg)` returns Anthropic/OpenAI/Grok/Gemini/Ollama/llama.cpp behind one interface. The photo-audit vision chain is self-healing (ordered providers, auto-fallback). **The SIE compliance path makes zero model calls** — the model is used only for prose/explanation and optional vision description. This matches the target's "model is an explainer, not source of truth" principle *for the SIE*; older chat surfaces (agent/orchestra) are more model-driven.

---

## 8. Existing unfinished / incomplete functionality

- **No relational spine / evidence graph** — cross-entity links are string-keyed JSON, not a queryable graph. "Show me why" works within one engine but not across the whole chain.
- **SIE is portfolio-wide, not tenant-scoped** — `/sie` and `/api/*` SIE routes expose *all* companies. GCs are deliberately kept on `/gc`. Per-tenant audit ownership is an open, deliberate decision before exposing SIE to real GCs.
- **Two auth systems** (portal session cookie vs internal token) bridged, not unified.
- **Two persistence layers** (file-based vs `platform_db`) not reconciled; `platform_db` AI features gated OFF.
- **No human-review queue** as a first-class store (review *flags* exist on findings; no queue UI/table).
- **Knowledge graph, pgvector semantic retrieval, formal rule table** — all described in the arch package, none built (retrieval today is keyword/verbatim + curated maps).
- **Training engine is partial** — training requirements exist in KB and flow into programs, but there is no employee/role training matrix, assignment, or expiration tracking as live data.
- **Vision detection breadth** — `photo_audit.py` works and is KB-cited, but the detection battery is narrower than the 40+ hazard categories the vision targets.

---

## 9. What should NOT be changed (preserve deliberately)

1. **The `create_app` factory pattern + isolated `try/except` registration.** This is why the live app is resilient. Every new engine must register the same way.
2. **The deterministic, file-based, no-external-LLM SIE.** It is the product's trust story and its offline guarantee. Do not move compliance logic into the model, and do not casually swap the SIE to a DB without a migration that preserves the offline CI test.
3. **The single-corpus KB design** and its copyright discipline (citation + summary for restricted standards).
4. **`portal.py` as the live customer path** and its session-slug tenant isolation + security hardening (auto secret, PBKDF2 PINs, lockout, fail-closed admin).
5. **The `origin/llm/` provider abstraction.** It already satisfies the "swappable model" requirement — build on it, don't replace it.
6. **The offline verification workflow** (`ORIGIN_DATA_DIR=$(mktemp -d)` + `TestClient`, keys unset, `node --check` on inline JS) and **deploy-from-Mac** discipline (never push from the sandbox).
7. **The four-way classification** (OSHA-required / Origin recommendation / Best practice / Customer requirement) — never blend these.

---

## 10. Recommended evolution path (summary — full detail in the roadmap)

Evolve, don't rewrite. In order: (1) stabilize the two-auth / two-persistence seams and decide the spine; (2) introduce a thin **relational/graph spine** that the file-based engines write *through* (not a rewrite — an index built from the same JSON, or a migration behind a flag) to unlock the evidence graph and cross-company "why"; (3) make SIE **tenant-scoped** so it can be exposed to GCs and become the multi-company "safety professional" product; (4) broaden the vision detection battery and formalize the rule table as data; (5) add the human-review queue as a first-class store; (6) grow training into live matrix/assignment/expiration data. Each step ends with the same CI gate: **all external LLM keys unset, everything still works.**
