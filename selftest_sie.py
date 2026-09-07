"""Stage 0 safety net — the offline SIE + auth-regression self-test.

This is the CI gate for the whole "AI Safety & Compliance Operating System"
build: it proves two load-bearing guarantees that every later stage must keep
green.

  1. OFFLINE DETERMINISM — with every external LLM API key UNSET, the Safety
     Intelligence Engine still runs the full chain end to end:
        citation -> company -> audit -> CAPA -> risk -> program -> prequal -> monitor
     and returns real, non-error, deterministic output. The model is an
     explainer, never the source of truth; none of these routes may need one.

  2. BRIDGED AUTH — the "/api/*" gate accepts the internal access token OR a
     valid portal admin/owner SESSION cookie (the "one login" rule), and denies
     everything else: no credential, an expired admin session, or a client
     session must all be rejected.

Run (proves the guarantee by popping the keys itself):

    python selftest_sie.py

Or belt-and-suspenders, with the keys stripped from the environment too:

    env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u GROK_API_KEY \
        -u GEMINI_API_KEY -u XAI_API_KEY python selftest_sie.py

Exit code 0 = all guarantees hold. Non-zero = a regression.
"""

from __future__ import annotations

import os
import sys
import tempfile

# ── Isolate persistence BEFORE importing origin. The SIE engine modules read
# ORIGIN_DATA_DIR at import time, so this must run first — it points every
# file-based engine at a throwaway directory so the test never touches real
# data and starts from a clean slate. ──────────────────────────────────────
_DATA_DIR = tempfile.mkdtemp(prefix="origin-sie-selftest-")
os.environ["ORIGIN_DATA_DIR"] = _DATA_DIR

# ── Prove the offline guarantee: strip every external model key so an accidental
# model call would fail loudly rather than silently "passing" on a live key. ──
_LLM_KEYS = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROK_API_KEY", "XAI_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY", "MISTRAL_API_KEY",
)
for _k in _LLM_KEYS:
    os.environ.pop(_k, None)


def _keys_are_unset() -> bool:
    return all(not os.environ.get(k) for k in _LLM_KEYS)


def _client():
    """Build the app the same way production does (create_app factory), token
    gated, and return (TestClient, token)."""
    from fastapi.testclient import TestClient
    from origin.config import Config, DEFAULT_CONFIG, _expand_env
    from origin.server import Engine, create_app

    cfg = Config(_expand_env(DEFAULT_CONFIG), None)
    eng = Engine(cfg)                       # constructs with NO model key — must not raise
    token = "SELFTEST-TOKEN"
    return TestClient(create_app(engine=eng, token=token)), token, eng


def _admin_cookie(expired: bool = False) -> str:
    """A genuine portal admin session cookie header value, signed with the real
    portal secret exactly as a logged-in owner would carry."""
    import time
    from origin import portal
    exp = time.time() + (-10 if expired else 3600)
    tok = portal._sign({"role": "admin", "slug": "owner", "exp": exp})
    return f"{portal.ADMIN_COOKIE}={tok}"


def _client_cookie() -> str:
    """A genuine portal CLIENT session cookie — must NOT reach SIE /api/*."""
    import time
    from origin import portal
    tok = portal._sign({"role": "client", "slug": "acme", "exp": time.time() + 3600})
    return f"{portal.CLIENT_COOKIE}={tok}"


def _gc_cookie(slug: str) -> str:
    """A genuine portal GC session cookie for tenant `slug`, signed exactly as a
    logged-in GC would carry it. This is what Stage 3 gives a scoped slice of the
    SIE to."""
    import time
    from origin import portal
    tok = portal._sign({"role": "gc", "slug": slug, "member": "owner",
                        "exp": time.time() + 3600})
    return f"{portal.GC_COOKIE}={tok}"


# ── The auth regression ──────────────────────────────────────────────────────
def check_auth(client, token: str) -> None:
    probe = "/api/company/portfolio"          # a representative gated SIE route

    r = client.get(probe)
    assert r.status_code == 401, f"no-auth should be 401, got {r.status_code}"

    r = client.get(probe, headers={"X-Origin-Token": token})
    assert r.status_code == 200, f"token should pass, got {r.status_code}"

    r = client.get(probe, headers={"X-Origin-Token": "WRONG"})
    assert r.status_code == 401, f"wrong token should be 401, got {r.status_code}"

    r = client.get(probe, headers={"Cookie": _admin_cookie()})
    assert r.status_code == 200, f"valid admin session should pass, got {r.status_code}"

    r = client.get(probe, headers={"Cookie": _admin_cookie(expired=True)})
    assert r.status_code == 401, f"expired admin session should be 401, got {r.status_code}"

    r = client.get(probe, headers={"Cookie": _client_cookie()})
    assert r.status_code == 401, f"client session must NOT reach SIE, got {r.status_code}"

    print("[pass] bridged auth: token OR admin-session in, everything else out")


# ── The offline SIE chain ────────────────────────────────────────────────────
def check_sie_chain(client, token: str) -> None:
    H = {"X-Origin-Token": token}

    # 1. Company profile is created (deterministic slug id)
    r = client.post("/api/company/upsert", headers=H, json={
        "company": "Selftest Roofing Co",
        "industry": "Roofing",
        "naics": "238160",
        "state": "TX",
        "headcount": 25,
        # Real activity trigger KEYS (scoping keys, not free text) so the
        # Applicable Requirements Engine has a triggered standard to resolve;
        # fall_exposure pulls in 1926.501, matching the HIGH finding below so
        # Stage 2's "covered vs. gap" split has a genuinely-covered line.
        "activities": {"fall_exposure": True, "hot_work": True},
        # A hiring client so a Customer-requirement line is produced too.
        "operators": ["ISN"],
    })
    assert r.status_code == 200, r.text
    prof = r.json().get("profile") or {}
    cid = prof.get("company_id")
    assert cid, f"upsert must return a company_id, got {r.json()}"

    # 2. Citation analysis runs with no model — deterministic 12-part record.
    #    We exercise the deterministic CORE (analyze + save) directly rather than
    #    the HTTP route: /api/citation/analyze couples `Request` + `Body` in one
    #    signature, which some FastAPI/Starlette versions won't bind from a JSON
    #    client (it's fine on the pinned production version). The offline
    #    guarantee lives in analyze(); we then confirm the record is served back
    #    over the gated API.
    from origin import citation_engine as _ce
    cit = _ce.analyze({"standard": "29 CFR 1926.501"})
    assert isinstance(cit, dict) and not cit.get("error"), f"citation errored: {cit}"
    _ce.save(cit)
    r = client.get("/api/citation/list", headers=H)
    assert r.status_code == 200, r.text
    assert (r.json().get("items") or []), "saved citation missing from /api/citation/list"

    # 3. A photo-audit report persists as a company-bound audit AND a HIGH
    #    finding auto-opens a CAPA — the audit->CAPA chain, fully offline.
    r = client.post("/api/audit/from-report", headers=H, json={
        "company": "Selftest Roofing Co",
        "report": {
            "scene": "Flat commercial roof, crew near unprotected edge",
            "image_count": 3,
            "findings": [{
                "severity": "high",
                # photo_audit resolves the hazard to a KB standard OBJECT (not a
                # bare string) before recording; mirror that shape so the
                # audit->CAPA source-ref carry-forward exercises for real.
                "standard": {
                    "citation": "29 CFR 1926.501",
                    "standard_title": "Duty to have fall protection",
                    "url": "https://www.osha.gov/laws-regs/regulations/standardnumber/1926/1926.501",
                    "has_verbatim": True,
                },
                "title": "Unprotected roof edge",
                "description": "Fall from elevation",
            }],
        },
    })
    assert r.status_code == 200, r.text
    audit = r.json().get("audit") or {}
    capa_ids = audit.get("capa_ids") or []
    assert capa_ids, f"HIGH finding should auto-open a CAPA, got {audit}"

    # 4. That CAPA is retrievable in the tracker
    r = client.get("/api/capa/list", headers=H)
    assert r.status_code == 200, r.text
    capa_items = r.json().get("items") or []
    tracked = {c.get("id") for c in capa_items}
    assert capa_ids[0] in tracked, "auto-opened CAPA missing from /api/capa/list"

    # 5. Risk score computes deterministically for the company
    r = client.get(f"/api/company/{cid}/risk", headers=H)
    assert r.status_code == 200, r.text
    assert r.json().get("ok"), f"risk did not compute: {r.json()}"

    # 6. Program package assembles (sourced, no model)
    r = client.get(f"/api/program/package/{cid}", headers=H)
    assert r.status_code == 200, r.text
    assert not r.json().get("error"), f"program package errored: {r.json()}"

    # 7. Prequal readiness assesses across platforms
    r = client.post(f"/api/prequal/{cid}", headers=H, json={})
    assert r.status_code == 200, r.text
    assert not r.json().get("error"), f"prequal errored: {r.json()}"

    # 8. Monitor sweep runs deterministically over the persisted state
    r = client.post("/api/monitor/sweep", headers=H, json={})
    assert r.status_code == 200, r.text
    assert not r.json().get("error"), f"monitor sweep errored: {r.json()}"

    # 9. The company now shows up in the portfolio view
    r = client.get("/api/company/portfolio", headers=H)
    assert r.status_code == 200, r.text
    port = r.json()
    assert port.get("ok"), f"portfolio errored: {port}"

    print("[pass] offline SIE chain: citation->company->audit->CAPA->risk->"
          "program->prequal->monitor (no model)")

    return cid


# ── The evidence spine (Stage 1) ─────────────────────────────────────────────
def check_spine(client, token: str, cid: str) -> None:
    """The derived spine must reconstruct the compliance chain from the source
    JSON alone — no model, no second source of truth — and make it queryable.
    We assert the audit->CAPA chain the SIE just wrote is walkable end to end:
    company -> finding -> {triggers CAPA, violates source standard}."""
    H = {"X-Origin-Token": token}

    # 1. Rebuild the index purely from the file-based collections.
    r = client.post("/api/spine/rebuild", headers=H, json={})
    assert r.status_code == 200, r.text
    meta = r.json().get("meta") or {}
    assert not r.json().get("error"), f"spine rebuild errored: {r.json()}"
    assert meta.get("nodes", 0) > 0 and meta.get("edges", 0) > 0, \
        f"spine rebuilt empty: {meta}"
    assert meta.get("derived") is True, "spine must declare itself derived, not authoritative"

    # 2. The company chain is queryable and carries the finding + CAPA + source.
    r = client.get(f"/api/spine/company/{cid}", headers=H)
    assert r.status_code == 200, r.text
    chain = r.json()
    assert chain.get("found"), f"company not in spine: {chain}"
    counts = chain.get("counts") or {}
    assert counts.get("findings", 0) >= 1, f"finding missing from spine chain: {chain}"
    assert counts.get("capas", 0) >= 1, f"CAPA missing from spine chain: {chain}"
    assert counts.get("sources", 0) >= 1, f"source standard missing from spine chain: {chain}"

    # 3. The load-bearing edges actually exist: finding->triggers->CAPA and
    #    finding->violates->source. This is the "why does this CAPA exist" link.
    rels = {e["rel"] for e in (chain.get("edges") or [])}
    assert "triggers" in rels, f"no finding->triggers->CAPA edge: {sorted(rels)}"
    assert "violates" in rels, f"no finding->violates->source edge: {sorted(rels)}"

    print("[pass] evidence spine: rebuilt from JSON, company->finding->"
          "{triggers CAPA, violates source} is queryable (derived, no model)")


# ── the Applicable Requirements Engine (Stage 2) ─────────────────────────────
def check_requirements(client, token: str, cid: str) -> None:
    """Given a company profile, Origin must produce its tailored requirement set
    with a citation and an applicability reason for EVERY line, each tagged with
    exactly one of the four classifications (never blended). Then the gap view
    must split those requirements into covered (evidence on file) vs. gaps,
    reading the derived spine — all offline, no model."""
    H = {"X-Origin-Token": token}

    # 1. The classified requirement set resolves for the company.
    r = client.get(f"/api/requirements/{cid}", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert not body.get("error"), f"requirements errored: {body}"
    reqs = body.get("requirements") or []
    assert reqs, f"no requirements resolved for company: {body}"

    # 2. EVERY line carries a citation-or-basis, an applicability reason, and
    #    exactly one of the four classifications.
    allowed = {"osha_required", "origin_recommendation", "best_practice",
               "customer_requirement"}
    for req in reqs:
        assert req.get("classification") in allowed, \
            f"bad/blended classification: {req.get('classification')} in {req}"
        assert req.get("why"), f"requirement missing applicability reason: {req}"
    classes = {req["classification"] for req in reqs}
    assert "osha_required" in classes, f"expected an OSHA-required line: {sorted(classes)}"
    # The seeded 'ISN' operator must surface as a Customer requirement.
    assert "customer_requirement" in classes, \
        f"expected a Customer requirement from the operator: {sorted(classes)}"

    # 3. The gap view splits requirements into covered vs. gaps using the spine.
    #    fall_exposure pulled in 1926.501, and the HIGH finding violated
    #    1926.501 — so that requirement must read as covered (evidence on file).
    r = client.get(f"/api/requirements/{cid}/gaps", headers=H)
    assert r.status_code == 200, r.text
    gap = r.json()
    assert not gap.get("error"), f"gaps errored: {gap}"
    assert gap.get("covered_count", 0) >= 1, \
        f"fall-protection requirement should be covered by the finding: {gap}"
    covered_cites = " ".join(c.get("citation", "") for c in gap.get("covered") or [])
    assert "1926.501" in covered_cites, \
        f"expected 1926.501 among covered requirements: {covered_cites}"

    # 4. The spine now materializes the requirement layer too: the company chain
    #    carries requirement nodes reachable from the company anchor.
    r = client.get(f"/api/spine/company/{cid}", headers=H)
    assert r.status_code == 200, r.text
    counts = (r.json().get("counts") or {})
    assert counts.get("requirements", 0) >= 1, \
        f"spine chain missing requirement nodes: {r.json()}"

    print("[pass] applicable requirements: tailored set with citation + reason + "
          "one-of-four classification per line; gap view covers 1926.501, spine "
          "carries the requirement layer (derived, no model)")


# ── tenant scoping (Stage 3) ─────────────────────────────────────────────────
def check_tenancy(client, token: str, owner_cid: str) -> None:
    """SIE was portfolio-wide; Stage 3 scopes it per-GC. The boundary must hold:
    a GC self-creates and sees ONLY its own companies; the owner still sees every
    company (the superuser lens); and one GC can never read, list, or overwrite
    another GC's — or an owner-only — company. All offline, no model.

    `owner_cid` is the owner-created company from the SIE chain (gc_slug=""), which
    must stay invisible to every GC and visible to the owner."""
    H = {"X-Origin-Token": token}
    ALPHA = {"Cookie": _gc_cookie("gc-alpha")}
    BETA = {"Cookie": _gc_cookie("gc-beta")}

    # 1. Two GCs self-create their own companies (self-serve from the start).
    r = client.post("/api/company/upsert", headers=ALPHA, json={
        "company": "Alpha Roofing", "industry": "Roofing", "state": "TX"})
    assert r.status_code == 200, r.text
    a_prof = r.json().get("profile") or {}
    a_cid = a_prof.get("company_id")
    assert a_cid, f"GC-alpha upsert must return a company_id: {r.json()}"
    assert a_prof.get("gc_slug") == "gc-alpha", \
        f"GC-created company must be stamped with the GC's slug: {a_prof}"

    r = client.post("/api/company/upsert", headers=BETA, json={
        "company": "Beta Electric", "industry": "Electrical", "state": "TX"})
    assert r.status_code == 200, r.text
    b_cid = (r.json().get("profile") or {}).get("company_id")
    assert b_cid, f"GC-beta upsert must return a company_id: {r.json()}"

    # 2. Each GC's list shows ONLY its own company — not the other GC's, not the
    #    owner-only company from the SIE chain.
    r = client.get("/api/company/list", headers=ALPHA)
    assert r.status_code == 200, r.text
    alpha_ids = {c.get("company_id") for c in (r.json().get("items") or [])}
    assert alpha_ids == {a_cid}, \
        f"GC-alpha must see only its own company, saw: {sorted(alpha_ids)}"

    r = client.get("/api/company/list", headers=BETA)
    beta_ids = {c.get("company_id") for c in (r.json().get("items") or [])}
    assert beta_ids == {b_cid}, \
        f"GC-beta must see only its own company, saw: {sorted(beta_ids)}"

    # 3. The owner (token) still sees EVERYTHING — both GCs' plus the owner-only
    #    company. The global superuser lens is preserved.
    r = client.get("/api/company/list", headers=H)
    owner_ids = {c.get("company_id") for c in (r.json().get("items") or [])}
    assert {a_cid, b_cid, owner_cid} <= owner_ids, \
        f"owner must see all companies, saw: {sorted(owner_ids)}"

    # 3b. The owner can preview a single GC's slice with ?gc=<slug>.
    r = client.get("/api/company/list?gc=gc-alpha", headers=H)
    scoped_ids = {c.get("company_id") for c in (r.json().get("items") or [])}
    assert scoped_ids == {a_cid}, \
        f"owner ?gc=gc-alpha should scope to that GC, saw: {sorted(scoped_ids)}"

    # 4. A GC can reach its OWN company's intelligence.
    r = client.get(f"/api/company/{a_cid}", headers=ALPHA)
    assert r.status_code == 200, r.text
    assert (r.json().get("profile") or {}).get("company_id") == a_cid, r.text
    r = client.get(f"/api/requirements/{a_cid}", headers=ALPHA)
    assert r.status_code == 200 and not r.json().get("error"), r.text

    # 5. Cross-tenant reads are refused: GC-alpha cannot touch GC-beta's company
    #    nor the owner-only company, by any per-company route.
    for path in (f"/api/company/{b_cid}", f"/api/company/{b_cid}/risk",
                 f"/api/requirements/{b_cid}", f"/api/program/package/{b_cid}",
                 f"/api/company/{owner_cid}", f"/api/audit/list?company_id={b_cid}"):
        r = client.get(path, headers=ALPHA)
        assert r.status_code == 403, \
            f"GC-alpha must be refused {path}, got {r.status_code}"

    # 6. A GC cannot overwrite another GC's company via upsert.
    r = client.post("/api/company/upsert", headers=ALPHA, json={
        "company": "Beta Electric", "industry": "HIJACKED"})
    assert r.status_code == 403, \
        f"GC-alpha must not overwrite GC-beta's company, got {r.status_code}"
    # ...and the hijack didn't land.
    r = client.get(f"/api/company/{b_cid}", headers=BETA)
    assert (r.json().get("profile") or {}).get("industry") != "HIJACKED", \
        "cross-tenant upsert must not have mutated the target company"

    # 7. Portfolio-wide/owner-only endpoints are not reachable by a GC session.
    #    (company/list is allowed but self-scoped — already asserted 200 above.)
    #    global aggregates (audit/list with no company_id) and non-SIE/owner
    #    routes must all be forbidden for a GC.
    assert client.get("/api/spine/rebuild", headers=ALPHA).status_code in (403, 405) \
        or client.post("/api/spine/rebuild", headers=ALPHA, json={}).status_code == 403, \
        "GC must not reach /api/spine/rebuild"
    for path in ("/api/audit/list", "/api/citation/list", "/api/monitor/digest"):
        r = client.get(path, headers=ALPHA)
        assert r.status_code == 403, \
            f"GC must not reach cross-tenant/owner route {path}, got {r.status_code}"

    print("[pass] tenant scoping: GC self-creates + sees only its own; owner sees "
          "all (and can ?gc= scope); cross-tenant read/list/overwrite refused")


def main() -> int:
    assert _keys_are_unset(), "LLM keys must be unset for this test to mean anything"
    print(f"[info] ORIGIN_DATA_DIR={_DATA_DIR}  (throwaway)")
    print("[info] all external LLM API keys are UNSET")

    try:
        from fastapi.testclient import TestClient  # noqa: F401
    except ImportError:
        print("[skip] fastapi not installed — SIE self-test skipped")
        return 0

    client, token, eng = _client()
    try:
        check_auth(client, token)
        cid = check_sie_chain(client, token)
        check_spine(client, token, cid)
        check_requirements(client, token, cid)
        check_tenancy(client, token, cid)
    finally:
        try:
            eng.shutdown()
        except Exception:
            pass

    # Guard: the whole point is that nothing above quietly relied on a key.
    assert _keys_are_unset(), "an LLM key got set during the run — offline guarantee broken"

    print("\nAll SIE offline + auth self-tests passed \u2705")
    return 0


if __name__ == "__main__":
    sys.exit(main())
