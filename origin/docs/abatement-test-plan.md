# Origin Abatement — Test Plan (Phase 1 core spine)

Deterministic, offline verification of the Phase 1 engine. Everything below runs
with no network (the regulatory brain, extraction, deadlines, evidence, and
readiness are all file-based/offline). Live-only concerns (real vision OCR of a
scanned citation) are explicitly out of scope for Phase 1 and degrade gracefully.

## What Phase 1 covers

The generalized engine, end to end:

```
SOURCE → FACT/CONDITION → REQUIREMENT → ACTION → EVIDENCE → VERIFICATION → DECISION
```

Modules under test: `abatement_matter.py`, `evidence_vault.py`,
`abatement_intake.py`, and their registration in `server.py`.

## Guardrails asserted (must always hold)

- Regulatory results come from the deterministic Origin brain (`citations.cite` →
  `compliance_kb`), never an LLM. `reg_lookup` returns `confidence: 0` and a
  "not found / nothing fabricated" reasoning string when the corpus misses.
- Every extracted citation field is returned with `extraction_flag = "AI EXTRACTED — VERIFY"`.
- Every deadline carries `System-calculated workflow date — verify against citation/order.`
- Every readiness number carries `Internal workflow indicator. Not an OSHA determination.`
- No AI/vision output is stored as a confirmed violation — evidence `ai_observation`
  always ships with the "potential hazard only … human verification required" disclaimer.
- Scanned/image-only citation PDF → `reason: needs_ocr_or_manual`, no content guessed.

## Acceptance test — fall protection (29 CFR 1926.501)

Realistic OSHA citation: a roofing sub cited for an unprotected 6-ft edge
(Serious, $4,500, abate 12/01/2026) plus a missing-training item (503(a)(1),
Serious, $2,000). Citation received 11/03/2026.

Steps and expected results (all verified offline on 2026-09-11):

1. **SOURCE** — `reg_lookup("29 CFR 1926.501")` →
   `ok=True`, title "Duty to have fall protection", jurisdiction "Federal — OSHA",
   `confidence 1.0`. ✅
2. **FACT/CONDITION (extraction)** — `parse_text(citation)` → 2 rows, each with
   standard/classification/penalty/abatement date and `flag="AI EXTRACTED — VERIFY"`. ✅
3. **Matter** — `create_matter` + `add_citation_items(extracted=True)` → status
   flips `intake→active`, items carry the extraction flag, `verified=False`. ✅
4. **REQUIREMENT** — each item's requirement anchors on `29 CFR 1903.19`. ✅
5. **DEADLINES** — abatement `2026-12-01`; certification due `2026-12-11`
   (abatement + 10 calendar days per 1903.19(c)); contest deadline `2026-11-24`
   (received + 15 working days). `abatement_overdue=False`. ✅
6. **Attorney verify** — `verify_citation_item` sets `verified=True`, clears the
   extraction flag, records `verified_by`. ✅
7. **ACTION** — `open_corrective_action` opens a CAPA via the existing `capa.py`
   engine (`source="abatement"`) and links its id back onto the citation item. ✅
8. **EVIDENCE + gap** — store a `before` and an `after` photo; gap analysis reports
   item0 `unverified`, item1 `missing`. ✅
9. **VERIFICATION** — `verify_evidence` on the after-photo moves item0 to
   `verified`; gap summary updates. ✅
10. **Readiness** — 0 (no evidence) → 30 (after uploaded, unverified) → 50 (after
    human-verified); labeled as an internal indicator throughout. ✅
11. **Dashboard** — rolls up matters, citation items, unverified items, overdue
    items, and upcoming (≤30-day) deadlines. ✅

## How to re-run

From the repo parent (`~/Desktop/origin`), with a scratch data dir:

```
ORIGIN_DATA_DIR=$(mktemp -d) python3 -c "import importlib; \
  am=importlib.import_module('origin.abatement_matter'); \
  print(am.reg_lookup('29 CFR 1926.501')['title'])"
```

(The full scripted walk lives in the build session; every step above passed.)

## Out of scope for Phase 1 (planned next)

- Real OCR of a scanned citation image (Phase 1 handles pasted text + text-based PDFs;
  scanned images are detected and routed to manual entry, never guessed).
- Dedicated spine node/edges for the Matter object (today the linked CAPAs already
  appear in the client's existing evidence graph via `company_id`).
- `review_engine.py` formal queue integration (Phase 1 uses citation-item + evidence
  verification + matter `in_review` status as the human-review gate).
- Roles/client portal, submissions, package builder, reporting (Phase 2/3).
```
