# Build plan — GC-scoped Prequal Readiness + Citation→Abatement

**Decided 2026-09-08.** Both features. GCs run them self-serve for their own subs.

## The problem we're fixing

Two powerful engines already exist but are locked to the owner (`/sie`) side:

- `prequal_engine.assess()` / `assess_all()` — real ISN/Avetta/Veriforce readiness grade + sourced gap list (detected vs. checklist).
- `citation_engine.analyze()` — 12-part sourced OSHA-citation analysis with an abatement ladder.

On the live GC console today, a GC gets only a **manual** ISN/Avetta grade card (they type the grade in by hand) and **no** citation tool at all. And even the owner-side citation output gives *generic* corrective-action text — it never pulls the specific library program that actually abates the cited standard.

Goal: put both engines in the GC's hands, scoped to their own subs, and make the citation tool hand back the exact fixing document from our library.

## Pattern we're mirroring

The GC-scoped Gap Finder is the template. It already does exactly the shape we want:

- Route: `POST /portal/api/gc/sub/{sub_slug}/gap` in `portal.py`.
- Guard: `_gc_owned_sub(request, sub_slug)` — verifies the acting GC (or owner via `?gc=`) owns that sub; returns `(rec, slug, err)`.
- Runs the engine, stores the result on the sub's record (`gap_report`, `gap_run_at`), returns it.

Both new features copy this: guard → run engine → store on sub → return.

## Phase 1 — GC-scoped Prequal Readiness (per sub)

**New backend**
- `POST /portal/api/gc/sub/{sub_slug}/prequal` — guard with `_gc_owned_sub`, accept optional user-supplied `metrics` (EMR, TRIR, DART, insurance, MSQ/training %), call `prequal_engine.assess_all(company_id, metrics)`, store `prequal_report` + `prequal_run_at` on the sub, return the report.
- `GET` variant to read the last stored report.

**Key dependency (must build first).** `prequal_engine.assess()` needs a `company_profile` (`company.get(company_id)`), but portal subs are **not** linked to a company_profile today. So we need a small bridge: an `_ensure_profile_for_sub(rec)` helper that creates/syncs a company_profile from the sub's data (industry/NAICS from its scope of work, its `gc_slug` for ownership — that field already exists on company_profiles) and returns the `company_id`. Without this, prequal can't run for a sub.

**Frontend (`gc.html`)**
- In the sub detail pane, a "Prequal readiness" panel: pick platform(s), enter the handful of user-only metrics, click Run. Show the readiness verdict, the estimated grade, detected gaps (from the sub's own data), and checklist gaps (things the sub must self-supply).
- This replaces/augments the manual grade card so a GC sees a real, sourced readiness picture instead of a text field.

## Phase 2 — GC-scoped Citation → Abatement (with the fixing doc)

**The real new build: a citation→library-program map.** Given a cited standard (e.g. `1926.501`), return the library master(s) that abate it plus their required elements. Source it from the programs already in `compliance_kb` (many carry citation references) by building a `standard → mid` index at load. `knowledge_store.resolve(citation)` supplies the standard's meta; the compliance_kb program supplies the actual document. Today no clean map exists — this is the missing piece behind "tell me what's needed to abate and hand me the doc."

**New backend**
- `POST /portal/api/gc/sub/{sub_slug}/citation` — guard with `_gc_owned_sub`, take the citation text/standard, call `citation_engine.analyze(payload)`, then **enrich** the corrective-action section with the resolved library master(s): title + a one-click link to pull that program into this sub's vault. Store on the sub (`citations[]`), return analysis + fixing-doc links + the abatement ladder (Draft → Human Approved → Uploaded to ISN → Verified Passed).

**Reuse, don't rebuild.** Pulling the fixing program into the sub's vault uses the existing GC route `POST /portal/api/gc/sub/{sub_slug}/from-library`. No new document plumbing.

**Frontend (`gc.html`)**
- A "Citation → Abatement" panel in the sub detail: paste a citation, get the 12-part analysis, one click to load the exact program into the sub's vault, and track the abatement status through the ladder.

## Guardrails (unchanged Origin rules)

- Deterministic, fully offline — no external LLM. Both engines already honor this; the new routes must too.
- Isolated + non-fatal registration, same as the other portal routes.
- File-based storage on the persistent volume; results live on the sub's record.
- Tenant-scoped: a GC only ever touches its own subs (enforced by `_gc_owned_sub`).

## Verification (before deploy)

- Offline run with all LLM keys unset.
- Headless route smoke test (TestClient): GC session runs prequal + citation for an owned sub; confirm a GC **cannot** run them against a sub it doesn't own.
- Extend `selftest_sie.py` with a prequal-per-sub check and a citation→program-map check.
- `node --check` on the `gc.html` script blocks; `ast.parse` on `portal.py`.

## Suggested order

1. `_ensure_profile_for_sub` bridge (unblocks prequal).
2. Prequal route + `gc.html` panel (Phase 1).
3. citation→library-program index + resolver (the missing map).
4. Citation route + `gc.html` panel, wired to `from-library` (Phase 2).
5. Self-tests + offline verify + deploy from Mac.

## Decided — metric inputs

**The GC enters the metrics.** EMR, TRIR, DART, insurance limits, and MSQ/training % are typed by the GC in the prequal panel when they run readiness for a sub. The sub does not enter them in their own portal. So the metric inputs live entirely in the GC-side "Prequal readiness" panel; the sub portal needs no change for this.
