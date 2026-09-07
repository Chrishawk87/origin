# Origin — Implementation Roadmap

**Document:** ORIGIN-ROADMAP-001 · **Revision:** A · **Date:** 2026-09-07
**Purpose:** The persistent, sequenced plan for evolving Origin from its as-built state (see `origin-system-map.md`) into the all-industry AI Safety & Compliance Operating System — **without a rewrite**. Every stage preserves the live product and ends at the same gate: *all external LLM keys unset, everything still works.*

> This roadmap is a living document. When a stage completes, mark it and record what shipped. When priorities change, edit here — do not fork the plan into memory or chat.

---

## Guiding constraints (carried from the mandate)

1. **Evolve the existing engine. Never rebuild from scratch.** One industry-agnostic Origin engine, not an app per industry.
2. **Determinism is the product.** Compliance decisions come from code + curated knowledge, never from a model. The model is an explainer.
3. **Resilience pattern is law.** Every new capability registers through `create_app` inside its own non-fatal `try/except`.
4. **Reconcile, don't replace.** The two persistence realities (file-based SIE/portal vs `platform_db` Postgres) get bridged deliberately, behind flags, with migrations that preserve the offline CI test.
5. **Four-way classification is never blended:** OSHA-required / Origin recommendation / Best practice / Customer requirement.
6. **Traceability is the north star:** every requirement and finding must chain source → requirement → evidence → confidence → corrective action → verification.

---

## Stage 0 — Stabilize the seams (foundation, no new surface)

**Goal:** remove the two structural ambiguities before building on top of them.

- Confirm and document the bridged auth model (token OR admin session) as the single owner path; write a regression test that both paths reach `/api/*` and that a client cookie does not.
- Decide the persistence spine (see `origin-architecture-target.md` §"The spine decision"): the recommendation is a **thin relational/graph index built *from* the file-based JSON**, not a migration away from files. Ratify or revise that decision here before anything writes to a new store.
- Lock the offline CI gate as an actual committed test (not just an ad-hoc `TestClient` run): with `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/etc. unset, boot the app, run citation → company → program → audit → prequal → monitor, assert non-empty deterministic output.

**Exit:** one documented auth path, one ratified spine decision, one committed offline test.

---

## Stage 1 — The evidence spine (unlocks cross-entity "why")

**Goal:** make the source→requirement→evidence→CAPA→verification chain queryable across engines, which today it is not.

- Introduce a **derived index** (`spine/` on `DATA_DIR`, or a read-model table behind a flag) that the existing engines write *through* — company, requirement, finding, CAPA, evidence, training, each with stable IDs and typed edges. The JSON files remain the source of truth; the index is rebuildable from them.
- Add a `rebuild_spine()` that reconstructs the index from the JSON collections (guarantees the index is never authoritative and can't drift permanently).
- Expose one read API: "for company X, show the full chain behind finding Y" and "which requirements have no evidence."

**Exit:** a single query answers "why does Origin say this company must do X, and what proves it's done" across all six engines.

---

## Stage 2 — Applicable Requirements Engine as data (the "what matters" core)

**Goal:** turn the implicit "what applies to this company" logic into an explicit, inspectable rule set.

- Formalize the **rule table as data**: (company DNA attributes: industry/NAICS, size, activities, materials, equipment, jurisdiction) → (applicable requirement records in KB) with the classification tag. Deterministic resolution, fully sourced.
- Feed it from the existing `state_plans.jsonl` jurisdiction resolver, NAICS map, and activity scoping already in the Companies tab.
- Output for any company: the ranked set of what actually applies and *why it applies*, separating federal vs state-plan divergence.

**Exit:** given a company profile, Origin produces its tailored requirement set with a citation and an applicability reason for every line — no checklist hand-waving.

---

## Stage 3 — Tenant-scoping SIE (turns the internal tool into the product) — ✅ DONE (2026-09-07)

**Shipped:** single `gc_slug` marker on the company record (audits/CAPAs/programs inherit tenancy via `company_id`); `gc_slug=""` = owner-only, so every pre-existing/system company is safe-by-default. `_auth` middleware in server.py now sets `request.state.sie_owner` / `sie_gc_slug`: owner (internal token OR admin session) sees all and can scope via `?gc=<slug>`; a GC portal session (reusing portal.py's signed `origin_gc` cookie) sees only its own companies. GC-reachable SIE routes are a safe-by-default allowlist; cross-tenant read/list/overwrite is refused (403). GCs self-create their own companies from day one (auto-stamped with their slug). `selftest_sie.py check_tenancy()` green offline; full engine suite green.

**Goal:** SIE today is portfolio-wide (all companies visible). Scope it so it can be safely exposed to GCs and become the multi-company "safety professional."

- Add tenant ownership to company/audit/CAPA/program records (deliberate decision flagged in the system map §8).
- Reuse `portal.py`'s session-slug isolation model rather than inventing a new one; decide whether SIE tenancy rides on `portal.py` or on `platform_db` tenants (this is where the two realities finally meet — see target arch).
- Gate SIE console views by tenant; keep the owner/admin global view as a superuser lens.

**Exit:** a GC logs in and sees only their own companies' intelligence; the owner still sees everything.

---

## Stage 4 — Broaden the perception layer (AI Safety Inspector) — ✅ DONE (2026-09-07)

**Shipped:** `photo_audit.py` gained a deterministic hazard-category→OSHA-section battery (`_HAZARD_MAP`, 39 categories spanning 1926 construction + 1910 general industry) as the high-confidence first resolver, ahead of the curated-verbatim and strict brain-search paths. Every resolution is KB-verified (`osha_section`/verbatim) — no fabrication. Each finding now carries `confidence` + `confidence_band` + `match_method` + `route`: a deterministic table hit or curated verbatim = high confidence (actionable); a loose brain-search overlap = low-confidence candidate. Routing (`CONFIDENCE_REVIEW_BELOW = 0.6`): in `audit_engine.record_audit`, only a matched + confident finding clearing the severity bar auto-opens a CAPA; every unmatched OR low-confidence finding goes to a `review_queue` on the audit record (reason: unmatched / low_confidence) and NEVER auto-generates a corrective action. `promote_finding` is the manual reviewer counterpart (clears the queue entry, opens the CAPA). `selftest_sie.py check_perception()` proves both the widened battery and the review routing offline; full engine suite green.

**Goal:** widen `photo_audit.py` from its current narrower battery toward the 40+ hazard-category vision, keeping every detection KB-cited.

- Expand the detection→requirement mapping table (still deterministic mapping from a detected condition to a sourced requirement + auto-CAPA).
- Keep matched findings → `source_refs`, unmatched → `human_review`. No silent guesses.
- Add confidence surfacing so low-confidence detections route to review rather than to a CAPA.

**Exit:** a photo walk-through covers materially more real-site hazards, each traceable to a citation, with honest confidence and review routing.

---

## Stage 5 — Human-review queue as a first-class store

**Goal:** today review *flags* exist on findings but there is no queue. Make review a real workflow.

- Add a review store (queue records, reviewer, decision, timestamp, resulting action) on the spine.
- Route: unmatched audit findings, low-confidence detections, and any model-authored prose that will be shown as authoritative.
- Console tab: an inbox where a safety professional approves/edits/rejects, with the decision written back to the chain.

**Exit:** nothing model-derived reaches a customer as authoritative without a logged human decision available.

---

## Stage 6 — Training intelligence as live data

**Goal:** grow training from KB-static into a living matrix.

- Employee/role → required training (derived from the requirements engine) → assignment → completion → expiration.
- Expiration drives `monitor_engine.py` alerts (reuse the existing deterministic monitor pattern).

**Exit:** Origin can say, per company, who needs what training, what's expired, and why it's required.

---

## Stage 7 — Reconcile persistence (only when justified by scale)

**Goal:** decide the long-term home once tenancy + spine + review exist.

- If file-based + derived index holds at scale, keep it (it's the trust story).
- If multi-tenant query load demands it, migrate the *index* (not the source-of-truth JSON) into `platform_db` behind a flag, with the offline test still green against the file path.

**Exit:** one deliberate, documented persistence decision — reached with data, not by default.

---

## Sequencing summary

Stage 0 (stabilize) → Stage 1 (spine) → Stage 2 (requirements-as-data) → Stage 3 (tenant scope) → Stage 4 (perception) → Stage 5 (review queue) → Stage 6 (training) → Stage 7 (persistence reconciliation).

Stages 1–3 are the critical path to the all-industry vision and the highest-leverage customer value. Stages 4–6 deepen the moat. Stage 7 is deferred on purpose.

Every stage: register through `create_app` in isolation, keep the four-way classification intact, and finish green on the offline no-LLM CI gate.
