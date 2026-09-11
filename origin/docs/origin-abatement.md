# Origin Abatement — Implementation Map

**Status:** Phase 1 core spine BUILT + verified offline (2026-09-11). Back-end
complete: Matter, Citation Item, deadlines, regulatory mapping, corrective actions
(via capa), Evidence Vault, gap analysis, readiness, attorney verification.
Registered in server.py (3 isolated modules). Acceptance test passing — see
`abatement-test-plan.md`. Not yet deployed. UI + roles/portal are Phase 2.

Origin Abatement is OSHA abatement intelligence + evidence management for OSHA-defense
attorneys. It is **not** an AI lawyer. Every AI output is a draft for human review; the
attorney owns all legal strategy, conclusions, and submission.

The whole feature is one generalized engine:

```
SOURCE → FACT/CONDITION → REQUIREMENT → ACTION → EVIDENCE → VERIFICATION → DECISION
```

This doc maps that engine onto what Origin **already has**, so we build by extension —
not by rebuilding the app.

---

## 1. What Origin already gives us (reuse, don't rebuild)

| Abatement need | Existing Origin component | Reuse verdict |
|---|---|---|
| Regulatory lookup (SOURCE) | `citations.cite()` + `compliance_kb.py` + `cfr_corpus` (verbatim CFR, deterministic, no LLM) | **Reuse directly.** Add jurisdiction/effective-date/reasoning wrapper. |
| Corrective-action lifecycle (ACTION) | `capa.py` — full ladder (Open→Root Cause→Implemented→Verified), regulation-agnostic, `create_manual()` needs no reg hook | **Reuse as the abatement corrective-action engine.** |
| Evidence graph (the CHAIN) | `spine.py` — directed node/edge graph over all records; already does citation→condition→action→evidence→verification | **Reuse.** Add `abatement_matter` node type + edges. |
| Photo/video hazard check (VERIFICATION) | `photo_audit.py` + `audit_engine.py` — vision model forbidden from citing OSHA numbers, low-confidence (<0.6) auto-routes to human review | **Reuse.** Already hedges "describe only what you see," never "violation confirmed." |
| Human review stage | `review_engine.py` — pending→approved/rejected queue with human-in-the-loop | **Reuse as the attorney-review stage.** |
| Documents / PDF / OSHA forms | `form_vault.py` (deterministic form mapping, no LLM) + `pdf_render.py` | **Reuse.** Add abatement-certification form to catalog. |
| Client/company object | `company_profile.py` — name/industry/NAICS/state/size + `compute_risk()` 0–100 | **Reuse as the Abatement Client**, add `matter` linkage. |
| Requirements derivation (REQUIREMENT) | `requirements_engine.py` + `checklist_engine.py` (imperative parsing of "shall/must") | **Reuse** to turn a cited standard into an abatement requirement checklist. |
| Auth, roles, tenancy | portal.py cookie sessions keyed by `slug`; `sie_gate.py` login branches | **Extend** with attorney/paralegal/client roles. |
| Email/notifications | `compliance.send_email()` (Resend or SMTP) | **Reuse.** |
| Persistence | flat JSON/JSONL under `ORIGIN_DATA_DIR` (`/data` on Railway) | **Reuse the pattern.** New store: `data/abatement/`. |
| Route wiring | `server.py` isolated `try/except register_X(app)` blocks | **Reuse the pattern** — add `register_abatement_matter(app)`. |
| Front-end shell | `webui/sie.html` single-page app, sidebar tabs, cookie-auth JSON calls | **Extend** — add an Abatement nav group + panels. |

**Bottom line:** roughly 80% of the engine already exists. The genuinely new pieces are the
Matter object, citation OCR/extraction, the readiness score, the evidence-gap analyzer, and
the attorney-facing UI.

---

## 2. What is genuinely NEW (must be built)

1. **Abatement Matter object** — the top-level case container (citations, deadlines,
   evidence, corrective actions, submissions, client). New store `data/abatement/matters/`.
2. **Citation intake + OCR extraction** — upload an OSHA citation PDF, extract items,
   every extracted field flagged **"AI EXTRACTED — VERIFY."** (Uses existing vision layer.)
3. **Citation Item engine** — one record per cited standard within a citation.
4. **Deadline engine** — abatement dates from the citation, labeled
   **"System-calculated workflow date — verify against citation/order."**
5. **Abatement Readiness Score (0–100)** — labeled **"Internal workflow indicator.
   Not an OSHA determination."** (Derived from evidence-gap state, reuses `compute_risk` style.)
6. **Evidence Vault + classification** — durable image/doc storage (new; today's vision
   layer only base64s to the model, keeps no file). Before/after preserved, originals never
   altered.
7. **Evidence-gap analyzer** — MISSING / INCOMPLETE / CONFLICTING / UNVERIFIED / VERIFIED.
8. **Abatement package builder** — assembles the reviewed evidence chain into a
   **"DRAFT — FOR REVIEW"** package. Never auto-submits.
9. **New roles + client portal** — LAW_FIRM_ADMIN / ATTORNEY / PARALEGAL /
   ORIGIN_SAFETY_PRO / CLIENT_ADMIN / CLIENT_USER / READ_ONLY, with client tasks
   (FIX / DOCUMENT / UPLOAD / VERIFY).
10. **Attorney command-center UI** — dark enterprise dashboard + the Abatement nav
    (Dashboard / Matters / New Matter / Citations / Deadlines / Evidence / Corrective
    Actions / Submissions / Clients / Reports).

---

## 3. Legal guardrails (enforced everywhere, non-negotiable)

- No legal advice, conclusions, or strategy. Attorney owns those.
- Every AI/vision output → **"potential hazard detected" + confidence + human verification
  required**, never "OSHA violation confirmed."
- Every extracted field → **"AI EXTRACTED — VERIFY."**
- Every generated document → **"DRAFT — FOR REVIEW."** No auto-submit, no court filing.
- Every regulatory result carries Source / Title / Standard / Jurisdiction / Effective date /
  URL / Section / Reasoning / Confidence — regulatory brain is source of truth, LLM never is.
- Readiness score + deadlines explicitly labeled as internal indicators, not OSHA
  determinations.
- Client data never used for model training; tenant isolation by firm.

---

## 4. Build order (MVP, phased)

**Phase 1 (core spine):** Matter object → Citation upload → Citation extraction →
Citation Items → Deadlines → Regulatory mapping → Corrective Actions (capa reuse) →
Evidence Vault → Evidence-gap analysis → Readiness score → Attorney review.

**Phase 2:** Roles + client portal + client tasks, submissions, package builder, timeline.

**Phase 3:** AI case review, Safety DNA/profile, recurring-risk engine, reporting,
full audit trail, mobile SCAN/UPLOAD/FIX/VERIFY.

**Acceptance test:** realistic fall-protection matter (29 CFR 1926.501) walked end to end
through the generalized engine. Delivered with `/docs/abatement-test-plan.md`.

---

## 5. New files vs. touched files (planned)

**New:** `abatement_matter.py` (Matter + Citation Item + Deadline engine),
`evidence_vault.py` (durable file store + gap analyzer + before/after),
`abatement_intake.py` (citation OCR extraction wrapper over the vision layer),
`webui/abatement.html` (or new panels inside sie.html).

**Touched (additive only):** `server.py` (one register block), `sie_gate.py` (role branches),
`spine.py` (new node/edge type), `form_vault.py` (abatement cert form), `roles.py`,
`webui/sie.html` (nav).

No existing engine is replaced, no functionality removed.
