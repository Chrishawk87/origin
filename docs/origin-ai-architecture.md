# Origin — AI Architecture

**Document:** ORIGIN-AI-001 · **Revision:** A · **Date:** 2026-09-07
**Purpose:** Define how models are used in Origin — the abstraction layer, the hard boundary between deterministic compliance and model output, human review, and auditability. Read `origin-system-map.md` and `origin-architecture-target.md` first.

---

## 1. The one rule that defines Origin's AI

> **The model is an explainer, never the source of truth.**

Compliance conclusions — what applies, what's a violation, what the corrective action is, whether it's verified — are produced by **deterministic Python over curated knowledge**. The model's only jobs are: (a) turn an already-computed result into readable prose, and (b) describe images for the inspector. If every external model key is unset, the compliance product still fully works. This is enforced today and is the product's entire trust story.

---

## 2. The provider abstraction (as-built, keep it)

`origin/llm/` already implements a clean seam: `build_provider(cfg)` returns one interface backed by any of **anthropic · openai · grok · gemini · ollama · llamacpp**. Consequences:

- Models are swappable by config — no code change to switch vendors.
- Fully **local operation is possible** (ollama / llama.cpp), so Origin can run with zero external AI vendor. This is a compliance and privacy asset, not just a cost lever.
- The photo-audit vision chain is **self-healing**: ordered providers with automatic fallback.

**Directive:** build on this seam. Do not introduce direct SDK calls to a vendor anywhere outside `origin/llm/`.

---

## 3. Where the model is allowed to run (and where it is forbidden)

| Surface | Model allowed? | Role |
|---|---|---|
| Applicable Requirements Engine | **No** | pure deterministic resolution |
| Citation analysis (12-part) | **No** | sourced from KB |
| Risk scoring | **No** | fixed formula, explainable |
| CAPA generation / status | **No** | rule-driven |
| Program / training / JHA package | **No** | assembled from sourced KB |
| Prequal readiness | **No** | KB-record traceable |
| Monitoring / alerts | **No** | deterministic rule monitors |
| Photo/vision **description** | Yes | describe image → deterministic mapping decides meaning |
| Prose explanation of a computed result | Yes | narrate, never decide |
| General chat / agent surfaces | Yes, but | guarded by `citation_guard.py` to stay grounded in KB |

The boundary is bright: a model may *describe* and *narrate*; it may never *determine* a compliance fact.

---

## 4. Grounding and guardrails (as-built)

- `citation_guard.py` keeps chat answers grounded in the knowledge base rather than free-associating.
- The four-way classification (OSHA-required / Origin recommendation / Best practice / Customer requirement) is attached by the deterministic layer, never by the model — so the model can't quietly promote a "best practice" into an "OSHA requirement."
- Copyright discipline is enforced in data, not left to the model: restricted standards are stored as citation + summary, so the model has nothing verbatim to reproduce.

---

## 5. Human review (target — first-class)

Today, review *flags* exist on findings (unmatched audit findings, low-confidence detections) but there is no queue. Target:

- A **ReviewItem** store on the evidence spine (see `origin-data-model.md`): queue entry, reviewer, decision, timestamp, resulting action.
- **Routing rule:** anything a model authored that will be shown to a customer as authoritative, plus any unmatched or low-confidence finding, routes to the queue before it becomes a CAPA or a customer-facing claim.
- **Console:** a review inbox where a safety professional approves / edits / rejects, and the decision is written back into the chain as evidence of human oversight.

Net effect: the model can draft, but a human decision is always on record before model output is treated as authoritative.

---

## 6. Confidence handling

- Deterministic detections carry a confidence value. **Low confidence → human review**, not → auto-CAPA. High confidence with a KB match → sourced CAPA.
- Confidence is surfaced to the user, never hidden. The product would rather say "needs review" than assert a wrong requirement.

---

## 7. Auditability (target, building on as-built)

Every AI-touched output must be reconstructable after the fact:

- **What produced this?** — deterministic result vs model narration is distinguishable in the record.
- **On what source?** — the requirement/finding links to the corpus citation that grounds it.
- **Who reviewed it?** — the ReviewItem records the human decision when one was required.
- **Could it be reproduced offline?** — because the compliance core is deterministic, re-running with keys unset yields the same conclusions; only the prose wording depends on a model.

The spine's rebuild contract (`rebuild_spine()` reconstructs from source JSON) means the audit trail is not dependent on the model or on a mutable database.

---

## 8. Model-swap and local-first posture

- Default to swappable hosted providers for prose/vision quality; support full local (ollama/llamacpp) for customers who require no data to leave their environment.
- Because no compliance decision depends on the model, switching or losing a provider degrades *wording and image description*, never *correctness*.

---

## 9. What NOT to do

- **Do not** call a model to decide a compliance fact, applicability, severity, or verification.
- **Do not** bypass `origin/llm/` with a direct vendor SDK call.
- **Do not** show model-authored authoritative content to a customer without a review path.
- **Do not** let the model assign the four-way classification or emit verbatim restricted standards.
- **Do not** remove the offline no-LLM guarantee — it is the difference between Origin and a chatbot with a compliance skin.
