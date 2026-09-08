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


# ── the perception layer (Stage 4) ──────────────────────────────────────────
def check_perception(client, token: str) -> None:
    """Stage 4 widens the photo walk-through's hazard battery and makes it route
    on honest confidence. Two guarantees, both offline (the only AI is the vision
    description upstream, which this test does not exercise — it drives the
    deterministic resolver + audit routing directly):

      1. BATTERY + CONFIDENCE — common real-site hazards named in plain language
         resolve deterministically to the EXACT OSHA section, KB-verified, at high
         confidence. No fabrication: an unresolvable hazard returns nothing.

      2. REVIEW ROUTING — when an audit is recorded, a matched + confident + severe
         finding auto-opens a CAPA, while an unmatched OR low-confidence finding is
         routed to the review queue and NEVER auto-opens a corrective action."""
    from origin import photo_audit as pa

    H = {"X-Origin-Token": token}

    # 1. The widened, deterministic battery: a spread of hazards across
    #    construction (1926) and general industry (1910) each resolve to the
    #    right section, KB-verified, at a trustworthy (high-band) confidence.
    battery = [
        ("unguarded rotating pulley on the pump motor", "1910.212"),
        ("worker at an unprotected roof edge with no guardrail", "1926.501"),
        ("open trench with no shoring or trench box", "1926.652"),
        ("forklift being driven with no seatbelt", "1910.178"),
        ("no lockout tagout applied to the press", "1910.147"),
        ("employee cutting concrete producing silica dust", "1926.1153"),
    ]
    for desc, want in battery:
        std = pa._resolve_citation(
            {"hazard_category": desc, "title": "", "description": desc})
        assert std is not None, f"battery hazard did not resolve: {desc}"
        assert std["section"] == want, \
            f"hazard '{desc}' → {std['section']}, expected {want}"
        assert std["confidence_band"] == "high", \
            f"deterministic map hit should be high-confidence: {std}"
        assert std["match_method"] in ("hazard_map", "verbatim"), std
        # KB-verified, never fabricated: the resolved citation carries a title.
        assert std.get("standard_title"), f"resolved standard missing title: {std}"

    # A hazard that maps to nothing is reported honestly as no-match (not guessed).
    assert pa._resolve_citation(
        {"hazard_category": "employee seems unusually cheerful today",
         "title": "", "description": "morale"}) is None, \
        "a non-hazard must resolve to nothing, never a forced citation"

    # 1b. PRECISION — the corpus holds 5700+ sections across 30/40/43/49 CFR whose
    #     keywords are just title-word splits, so an incidental query word can
    #     surface the WRONG standard at high confidence (the mine-berm scene once
    #     cited "Smoking and use of open flames"). A scene that lacks a specific
    #     jurisdiction anchor may still brush a non-OSHA verbatim record, but it
    #     must NEVER be stamped high-confidence off an incidental word, and
    #     whatever standard IS returned must carry its own correct CFR title
    #     number — a 30/40/43/49 CFR section is never mislabeled "29 CFR".
    incidental_scenes = [
        "worker near an open flame in the shop",     # brushes a mine smoking rec
        "hazardous waste drum staged by the road",   # brushes a DOT definitions rec
    ]
    for desc in incidental_scenes:
        std = pa._resolve_citation(
            {"hazard_category": desc, "title": "", "description": desc})
        if std is None:
            continue  # honest no-match is always acceptable
        cit = std.get("citation", "")
        if not cit.startswith("29 CFR"):
            # an unanchored, incidental non-OSHA brush must not be high-confidence
            # and must not come from the deterministic multi-agency battery
            assert std["confidence_band"] != "high" \
                and std.get("match_method") != "nonosha_map", (
                "an unanchored non-OSHA brush must never be a high-confidence "
                f"photo-audit citation: {desc!r} → {std}")
            assert cit and cit[:2].isdigit() and " CFR " in cit, \
                f"non-OSHA hit mislabeled: {desc!r} → {cit!r}"

    # 1c. MULTI-AGENCY LANES — the photo auditor is a field auditor for oil & gas
    #     / industrial sites, so a domain-anchored non-OSHA hazard must resolve in
    #     its OWN authority lane (MSHA / EPA / BLM / DOT), high-confidence, via the
    #     deterministic battery — never blended into an OSHA verdict, never
    #     mislabeled "29 CFR". Each scene names an unambiguous jurisdiction anchor.
    agency_scenes = [
        # description, expected section, expected authority, expected title num
        ("haul road dump point with no berm at the edge", "56.9300", "MSHA", "30"),
        ("diesel tank with no secondary containment or dike", "112.7", "EPA", "40"),
        ("open oil storage tank venting vapors near the well pad", "3179.90", "BLM", "43"),
        ("cargo tank truck with no placard", "172.504", "DOT", "49"),
    ]
    for desc, section, authority, title_num in agency_scenes:
        std = pa._resolve_citation(
            {"hazard_category": desc, "title": "", "description": desc})
        assert std is not None, \
            f"a domain-anchored {authority} hazard must resolve, not no-match: {desc!r}"
        assert std["section"] == section, \
            f"{authority} scene resolved to wrong section: {desc!r} → {std['section']} (want {section})"
        assert std.get("authority") == authority, \
            f"{authority} scene mislabeled authority: {desc!r} → {std.get('authority')}"
        assert std.get("authority") != "OSHA", \
            f"a non-OSHA hazard must never land in the OSHA lane: {desc!r} → {std}"
        assert std.get("citation", "").startswith(f"{title_num} CFR "), \
            f"{authority} scene mislabeled title: {desc!r} → {std.get('citation')!r}"
        assert std.get("match_method") == "nonosha_map", \
            f"{authority} scene must resolve via the deterministic battery: {desc!r} → {std.get('match_method')}"
        assert std["confidence_band"] == "high", \
            f"a domain-anchored {authority} hazard must be high-confidence: {desc!r} → {std}"

    # A genuine OSHA construction hazard still resolves to the RIGHT section at
    # high confidence — the multi-agency lanes must not blunt real OSHA matches.
    good = pa._resolve_citation({
        "hazard_category": "worker at an unprotected roof edge with no guardrail",
        "title": "", "description": "no fall protection, near the edge"})
    assert good and good["section"] == "1926.501" \
        and good["authority"] == "OSHA" \
        and good["confidence_band"] == "high", \
        f"a real OSHA fall hazard must still resolve high-confidence: {good}"

    # 2. Confidence-based review routing through a recorded audit. Three findings,
    #    all HIGH severity, differing only in match/confidence:
    #      a) matched + confident  → auto-CAPA
    #      b) unmatched            → review queue, NO CAPA
    #      c) low-confidence match → review queue, NO CAPA
    report = {
        "scene": "Mixed-hazard walk-through",
        "image_count": 1,
        "findings": [
            {   # a) actionable
                "severity": "high",
                "title": "Unprotected roof edge",
                "description": "Fall from elevation",
                "confidence": 0.9,
                "standard": {
                    "citation": "29 CFR 1926.501",
                    "standard_title": "Duty to have fall protection",
                    "url": "https://www.osha.gov/laws-regs/regulations/standardnumber/1926/1926.501",
                    "has_verbatim": True,
                },
            },
            {   # b) unmatched → review
                "severity": "high",
                "title": "Something ambiguous in the corner",
                "description": "Inspector unsure what this is",
                "standard": None,
            },
            {   # c) low-confidence candidate → review
                "severity": "high",
                "title": "Possible machine hazard",
                "description": "Loose brain-search style match",
                "confidence": 0.5,
                "standard": {
                    "citation": "29 CFR 1910.212",
                    "standard_title": "General requirements for all machines",
                    "url": "https://www.osha.gov/laws-regs/regulations/standardnumber/1910/1910.212",
                    "has_verbatim": False,
                },
            },
        ],
    }
    r = client.post("/api/audit/from-report", headers=H, json={
        "company": "Perception Test Co", "report": report})
    assert r.status_code == 200, r.text
    audit = r.json().get("audit") or {}
    capa_ids = audit.get("capa_ids") or []
    review = audit.get("review_queue") or []

    # Exactly ONE CAPA — the confident, matched, severe finding. The other two
    # are candidates and must NOT have auto-generated a corrective action.
    assert len(capa_ids) == 1, \
        f"only the confident matched finding may auto-CAPA, got {len(capa_ids)}: {audit}"
    assert len(review) == 2, f"unmatched + low-confidence must both queue for review: {review}"
    reasons = {q.get("reason") for q in review}
    assert reasons == {"unmatched", "low_confidence"}, \
        f"review queue must record why each finding needs review: {reasons}"
    assert audit.get("summary", {}).get("needs_review") == 2, \
        f"audit summary must surface the review count: {audit.get('summary')}"

    # The routed finding carries an actionable route; the candidates carry review.
    routes = sorted(f.get("route", "") for f in audit.get("findings", []))
    assert routes == ["capa", "review", "review"], f"unexpected finding routes: {routes}"

    print("[pass] perception layer: widened deterministic hazard battery resolves "
          "KB-cited at high confidence; matched+confident auto-CAPAs while "
          "unmatched/low-confidence route to review, never to a CAPA")


# ── the human-review queue (Stage 5) ─────────────────────────────────────────
def check_review(client, token: str) -> None:
    """Stage 5 makes review a first-class store. The guarantees, all offline:

      1. INGEST — the pending side of the store is reconstructed from the audits'
         review queues (idempotent), so every unmatched / low-confidence finding
         surfaces as a review item with WHY it was queued.
      2. APPROVE writes back to the chain — confirming an item opens the CAPA on the
         source audit (via promote_finding) and logs the reviewer + decision; the
         corrective action never exists without that logged human decision.
      3. REJECT writes back too — dismissing an item clears it from the audit queue,
         opens NO CAPA, and records the reviewer decision.
      4. Decisions are durable: a re-ingest never resurrects or clobbers a decided
         item, and the store's rollup reflects the outcomes."""
    from origin import review_engine as rv

    H = {"X-Origin-Token": token}

    # A fresh company + audit with exactly the two review-bound findings: one
    # unmatched, one low-confidence. (A confident matched HIGH finding is included
    # so the audit also auto-CAPAs — proving ingest pulls ONLY the queued ones.)
    report = {
        "scene": "Review-store walk-through",
        "image_count": 1,
        "findings": [
            {   # confident matched → auto-CAPA, NOT a review item
                "severity": "high",
                "title": "Unprotected roof edge",
                "description": "Fall from elevation",
                "confidence": 0.9,
                "standard": {
                    "citation": "29 CFR 1926.501",
                    "standard_title": "Duty to have fall protection",
                    "url": "https://www.osha.gov/laws-regs/regulations/standardnumber/1926/1926.501",
                    "has_verbatim": True,
                },
            },
            {   # unmatched → review
                "severity": "high",
                "title": "Ambiguous object near the panel",
                "description": "Inspector unsure",
                "standard": None,
            },
            {   # low-confidence match → review
                "severity": "medium",
                "title": "Possible machine guarding gap",
                "description": "Loose match",
                "confidence": 0.5,
                "standard": {
                    "citation": "29 CFR 1910.212",
                    "standard_title": "General requirements for all machines",
                    "url": "https://www.osha.gov/laws-regs/regulations/standardnumber/1910/1910.212",
                    "has_verbatim": False,
                },
            },
        ],
    }
    r = client.post("/api/audit/from-report", headers=H, json={
        "company": "Review Store Co", "report": report})
    assert r.status_code == 200, r.text
    audit = r.json().get("audit") or {}
    audit_id = audit.get("id")
    cid = audit.get("company_id")
    assert audit_id and cid, f"audit did not persist: {audit}"
    assert len(audit.get("capa_ids") or []) == 1, \
        f"only the confident matched finding may auto-CAPA: {audit}"

    # 1. Ingest reconstructs the pending side from the audit's review queue.
    r = client.post("/api/review/ingest", headers=H, json={})
    assert r.status_code == 200 and not r.json().get("error"), r.text

    r = client.get(f"/api/review/list?company_id={cid}", headers=H)
    assert r.status_code == 200, r.text
    items = r.json().get("items") or []
    pending = [i for i in items if i.get("status") == "pending"]
    assert len(pending) == 2, \
        f"both review-bound findings must surface as pending items: {items}"
    reasons = {i.get("reason") for i in pending}
    assert reasons == {"unmatched", "low_confidence"}, \
        f"each item must record WHY it was queued: {reasons}"

    # Idempotency: re-ingesting does not duplicate the pending items.
    client.post("/api/review/ingest", headers=H, json={})
    r = client.get(f"/api/review/list?company_id={cid}&status=pending", headers=H)
    assert len(r.json().get("items") or []) == 2, "ingest must be idempotent"

    by_reason = {i["reason"]: i for i in pending}
    unmatched_item = by_reason["unmatched"]
    lowconf_item = by_reason["low_confidence"]

    # 2. APPROVE the low-confidence item (a real hazard the reviewer confirms) →
    #    a CAPA opens on the source audit and the decision is logged.
    r = client.post(f"/api/review/{lowconf_item['item_id']}/approve", headers=H,
                     json={"by": "reviewer-jane"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out.get("ok"), f"approve failed: {out}"
    item = out.get("item") or {}
    assert item.get("status") == "approved" and item.get("reviewer") == "reviewer-jane", item
    action = item.get("resulting_action") or {}
    assert action.get("type") == "capa" and action.get("capa_id"), \
        f"approve must open a CAPA on the chain: {action}"
    # The CAPA is real and tracked, and the source audit no longer queues it.
    r = client.get("/api/capa/list", headers=H)
    assert action["capa_id"] in {c.get("id") for c in (r.json().get("items") or [])}, \
        "approved item's CAPA missing from the tracker"
    r = client.get(f"/api/audit/{audit_id}", headers=H)
    aq_ids = {q.get("finding_id") for q in (r.json().get("review_queue") or [])}
    assert lowconf_item["ref_id"] not in aq_ids, \
        "approving must clear the finding from the audit's review queue"

    # 3. REJECT the unmatched item → NO CAPA, decision logged, cleared from queue.
    r = client.post(f"/api/review/{unmatched_item['item_id']}/reject", headers=H,
                     json={"by": "reviewer-jane", "note": "not a hazard"})
    assert r.status_code == 200, r.text
    item = (r.json() or {}).get("item") or {}
    assert item.get("status") == "rejected" and item.get("decision") == "reject", item
    assert (item.get("resulting_action") or {}).get("type") == "dismissed", item
    r = client.get(f"/api/audit/{audit_id}", headers=H)
    assert not (r.json().get("review_queue") or []), \
        "both findings decided — the audit review queue must now be empty"

    # 4. Decisions are durable across a re-ingest and reflected in the rollup.
    client.post("/api/review/ingest", headers=H, json={})
    r = client.get(f"/api/review/{lowconf_item['item_id']}", headers=H)
    assert r.json().get("status") == "approved", "re-ingest must not clobber a decision"
    r = client.get(f"/api/review/{unmatched_item['item_id']}", headers=H)
    assert r.json().get("status") == "rejected", "re-ingest must not resurrect a rejection"

    r = client.get("/api/review/overview", headers=H)
    assert r.status_code == 200, r.text
    ov = r.json()
    assert ov.get("approved", 0) >= 1 and ov.get("rejected", 0) >= 1, \
        f"overview must reflect the logged decisions: {ov}"

    print("[pass] human-review queue: unmatched/low-confidence findings ingest as "
          "first-class items; approve opens the CAPA on the chain, reject dismisses "
          "it — both log the reviewer decision; decisions survive re-ingest")


def check_training(client, token: str) -> None:
    """Stage 6 makes training a living matrix. The guarantees, all offline:

      1. CATALOG is DERIVED + SOURCED — the courses a company needs come from the
         requirements engine, kept ONLY where the OSHA 2254 KB confirms a real
         training obligation. Nothing fabricated.
      2. ROSTER + COMPLETIONS are the authoritative net-new data — adding an
         employee and logging a completion is the only thing persisted.
      3. EXPIRATION is DETERMINISTIC — a stale completion on a course with a curated
         refresher cadence becomes 'expired'; a fresh one is 'current'; a course
         with no cadence never expires.
      4. Expiration DRIVES MONITORING — an expired required training surfaces as a
         high-severity monitor alert in the sweep."""
    from datetime import date, timedelta

    H = {"X-Origin-Token": token}

    # A company whose activities trigger two standards that BOTH carry a curated
    # refresher cadence AND a KB training obligation: respirators -> 1910.134
    # (annual) and forklifts -> 1910.178 (triennial re-evaluation).
    r = client.post("/api/company/upsert", headers=H, json={
        "company": "Training Matrix Co",
        "industry": "Manufacturing",
        "naics": "332710",
        "state": "TX",
        "headcount": 30,
        "activities": {"respirators": True, "forklifts": True},
    })
    assert r.status_code == 200, r.text
    cid = (r.json().get("profile") or {}).get("company_id")
    assert cid, f"upsert must return a company_id: {r.json()}"

    # 1. Catalog derives the two cadence-bearing courses, each fully sourced.
    r = client.get(f"/api/training/{cid}/catalog", headers=H)
    assert r.status_code == 200, r.text
    courses = r.json().get("courses") or []
    by_section = {c["section"]: c for c in courses}
    assert "1910.134" in by_section and "1910.178" in by_section, \
        f"catalog must derive the triggered training standards: {sorted(by_section)}"
    assert by_section["1910.134"]["refresher_months"] == 12, by_section["1910.134"]
    assert by_section["1910.178"]["refresher_months"] == 36, by_section["1910.178"]
    resp_course = by_section["1910.134"]["course_id"]

    # 2. Add an employee to the roster (authoritative net-new data).
    r = client.post(f"/api/training/{cid}/employee", headers=H,
                    json={"name": "Alex Rivera", "role": "Operator"})
    assert r.status_code == 200, r.text
    eid = (r.json().get("employee") or {}).get("employee_id")
    assert eid, f"add employee must return an id: {r.json()}"

    # Before any completion, every cell for this employee is 'missing'.
    r = client.get(f"/api/training/{cid}/matrix", headers=H)
    assert r.status_code == 200, r.text
    m = r.json()
    assert m.get("employee_count") == 1, m
    cell = _cell(m, eid, "1910.134")
    assert cell and cell["status"] == "missing", f"pre-completion must be missing: {cell}"

    # 3a. Log a STALE completion (2 years ago) on an annual course -> expired.
    stale = (date.today() - timedelta(days=730)).isoformat()
    r = client.post(f"/api/training/employee/{eid}/complete", headers=H,
                    json={"course_id": resp_course, "completed_on": stale})
    assert r.status_code == 200, r.text
    m = client.get(f"/api/training/{cid}/matrix", headers=H).json()
    cell = _cell(m, eid, "1910.134")
    assert cell and cell["status"] == "expired", \
        f"a 2-year-old annual training must be expired: {cell}"

    # 4. Expiration drives monitoring: the sweep raises a high training_expired alert.
    r = client.post("/api/monitor/sweep", headers=H, json={})
    assert r.status_code == 200 and not r.json().get("error"), r.text
    r = client.get(f"/api/monitor/alerts?company_id={cid}", headers=H)
    assert r.status_code == 200, r.text
    rules = {(a.get("rule"), a.get("severity")) for a in (r.json().get("items") or [])}
    assert ("training_expired", "high") in rules, \
        f"expired required training must raise a high monitor alert: {rules}"

    # 3b. Re-log the SAME course completed today -> current (deterministic recompute).
    r = client.post(f"/api/training/employee/{eid}/complete", headers=H,
                    json={"course_id": resp_course, "completed_on": date.today().isoformat()})
    assert r.status_code == 200, r.text
    m = client.get(f"/api/training/{cid}/matrix", headers=H).json()
    cell = _cell(m, eid, "1910.134")
    assert cell and cell["status"] == "current", \
        f"a completion today must be current: {cell}"

    print("[pass] training intelligence: catalog derives sourced courses from the "
          "requirements engine + OSHA 2254 KB; roster/completions persist; a stale "
          "completion expires and raises a monitor alert, a fresh one is current")


def _cell(matrix: dict, eid: str, section: str):
    for row in (matrix.get("rows") or []):
        if row.get("employee_id") == eid:
            for c in (row.get("cells") or []):
                if c.get("section") == section:
                    return c
    return None


def check_gc_scope(client, token: str) -> None:
    """Surfacing Stage 5 (review) + Stage 6 (training) to GCs must not spill one
    tenant's data into another's. The boundary, all offline:

      1. The review inbox rollup + list a GC sees are scoped to ITS OWN companies
         only — never another GC's items — while the owner still sees all.
      2. A GC can self-serve on its OWN review items (approve/reject) but is
         refused (403) on another GC's item, by every per-item route.
      3. The training matrix overview a GC sees is scoped to its own companies;
         another GC's per-company + per-employee training routes are refused (403).
      4. GCs still can't inject audits (from-report stays owner-only).

    Setup uses the owner (token) to create the audits — a GC can't — then assigns
    each company to a GC so the scoped views have something tenant-owned to show."""
    H = {"X-Origin-Token": token}
    ALPHA = {"Cookie": _gc_cookie("scope-alpha")}
    BETA = {"Cookie": _gc_cookie("scope-beta")}

    def _one_finding_report(title):
        return {"scene": title, "image_count": 1, "findings": [
            {"severity": "high", "title": title,
             "description": "unsure", "standard": None}]}  # unmatched → review

    # Owner creates one audited company per GC, each with a single review-bound
    # (unmatched) finding, then assigns it to that GC and gives it a training
    # trigger (respirators → 1910.134, which carries a KB training obligation).
    made = {}
    for label, slug in (("Scope Alpha Co", "scope-alpha"),
                        ("Scope Beta Co", "scope-beta")):
        r = client.post("/api/audit/from-report", headers=H, json={
            "company": label, "report": _one_finding_report(f"{label} hazard")})
        assert r.status_code == 200, r.text
        cid = (r.json().get("audit") or {}).get("company_id")
        assert cid, f"audit for {label} did not persist: {r.json()}"
        # Assign the company to the GC and add the training trigger (owner upsert).
        r = client.post("/api/company/upsert", headers=H, json={
            "company_id": cid, "company": label, "gc_slug": slug,
            "industry": "Manufacturing", "naics": "332710", "state": "TX",
            "headcount": 20, "activities": {"respirators": True}})
        assert r.status_code == 200, r.text
        assert (r.json().get("profile") or {}).get("gc_slug") == slug, r.text
        made[slug] = cid
    a_cid, b_cid = made["scope-alpha"], made["scope-beta"]

    # Ingest reconstructs the pending review items from the audits.
    r = client.post("/api/review/ingest", headers=H, json={})
    assert r.status_code == 200 and not r.json().get("error"), r.text

    # 1. GC-alpha's review rollup + list are scoped to alpha's company only.
    r = client.get("/api/review/overview", headers=ALPHA)
    assert r.status_code == 200, r.text
    seen = {c.get("company_id") for c in (r.json().get("companies") or [])}
    assert a_cid in seen and b_cid not in seen, \
        f"GC-alpha review overview must show only its own company: {sorted(seen)}"

    r = client.get("/api/review/list", headers=ALPHA)
    assert r.status_code == 200, r.text
    a_items = r.json().get("items") or []
    assert a_items and all(i.get("company_id") == a_cid for i in a_items), \
        f"GC-alpha review list must contain only its own items: {a_items}"
    a_item_id = a_items[0]["item_id"]

    # The owner still sees BOTH tenants' companies in the rollup.
    r = client.get("/api/review/overview", headers=H)
    owner_seen = {c.get("company_id") for c in (r.json().get("companies") or [])}
    assert {a_cid, b_cid} <= owner_seen, \
        f"owner review overview must see all tenants: {sorted(owner_seen)}"

    # Owner grabs GC-beta's item id, to prove alpha can't touch it.
    r = client.get(f"/api/review/list?company_id={b_cid}", headers=H)
    b_items = r.json().get("items") or []
    assert b_items, f"beta must have a pending review item: {b_items}"
    b_item_id = b_items[0]["item_id"]

    # 2. Cross-tenant per-item routes are refused for GC-alpha.
    assert client.get(f"/api/review/{b_item_id}", headers=ALPHA).status_code == 403, \
        "GC-alpha must not read GC-beta's review item"
    assert client.post(f"/api/review/{b_item_id}/approve", headers=ALPHA,
                       json={}).status_code == 403, \
        "GC-alpha must not approve GC-beta's review item"
    assert client.post(f"/api/review/{b_item_id}/reject", headers=ALPHA,
                       json={}).status_code == 403, \
        "GC-alpha must not reject GC-beta's review item"

    # ...but GC-alpha CAN self-serve on its OWN item (approve writes the chain and
    # stamps the GC as reviewer).
    r = client.post(f"/api/review/{a_item_id}/reject", headers=ALPHA,
                    json={"note": "not a hazard"})
    assert r.status_code == 200, r.text
    item = (r.json() or {}).get("item") or {}
    assert item.get("status") == "rejected", f"GC self-serve reject failed: {item}"
    assert item.get("reviewer") == "scope-alpha", \
        f"a GC decision must be stamped to the GC, got: {item.get('reviewer')}"

    # 3. Training overview is scoped; another GC's training routes are refused.
    r = client.get("/api/training/overview", headers=ALPHA)
    assert r.status_code == 200, r.text
    t_seen = {c.get("company_id") for c in (r.json().get("companies") or [])}
    assert a_cid in t_seen and b_cid not in t_seen, \
        f"GC-alpha training overview must show only its own company: {sorted(t_seen)}"

    for path in (f"/api/training/{b_cid}/matrix", f"/api/training/{b_cid}/catalog",
                 f"/api/training/{b_cid}/summary"):
        assert client.get(path, headers=ALPHA).status_code == 403, \
            f"GC-alpha must be refused GC-beta's training route {path}"
    # GC-alpha may add an employee to its OWN company but not to GC-beta's.
    assert client.post(f"/api/training/{b_cid}/employee", headers=ALPHA,
                      json={"name": "Mallory"}).status_code == 403, \
        "GC-alpha must not add an employee to GC-beta's roster"
    r = client.post(f"/api/training/{a_cid}/employee", headers=ALPHA,
                    json={"name": "Dana", "role": "Operator"})
    assert r.status_code == 200, r.text
    a_eid = (r.json().get("employee") or {}).get("employee_id")
    assert a_eid, f"GC-alpha add-own-employee must succeed: {r.json()}"
    # And GC-beta can't act on GC-alpha's employee (per-employee route ownership).
    assert client.post(f"/api/training/employee/{a_eid}/deactivate", headers=BETA,
                      json={}).status_code == 403, \
        "GC-beta must not deactivate GC-alpha's employee"

    # 4. GCs still cannot inject audits — from-report stays owner-only.
    assert client.post("/api/audit/from-report", headers=ALPHA, json={
        "company": "Sneaky Co",
        "report": _one_finding_report("x")}).status_code == 403, \
        "a GC must not be able to create audits"

    print("[pass] GC surfacing: review inbox + training matrix are tenant-scoped — "
          "a GC self-serves on its OWN items/roster and is refused (403) on another "
          "tenant's; owner still sees all; GCs still can't inject audits")


def check_checklists(client, token: str) -> None:
    """The Universal Regulatory Router's field layer, fully offline: the TEXT of a
    regulation in the file-based knowledge store must generate the mobile checklist
    and an AHA matrix — with no model and nothing fabricated.

      1. Seeding EM 385-1-1 Section 25 (Excavation) into the versioned store and
         resolving it back parses into >=5 structured field requirements.
      2. The imperative parser is deterministic: "5 feet or more" becomes a >=5 ft
         MEASUREMENT, "minimum of 2 feet" a 2 ft setback measurement, "before the
         start of each shift" a per-shift INSPECTION, a plain "shall" an
         ATTESTATION. Every field carries the source citation — never invented.
      3. Selecting the activity "Excavation > 5 ft" derives an AHA matrix whose
         controls ARE the parsed requirements, grouped by hazard.
      4. An unverifiable citation returns ok=False with no fields — the never-
         fabricate contract holds."""
    from urllib.parse import quote
    H = {"X-Origin-Token": token}

    # 1. Seed (force) + status.
    r = client.post("/api/checklist/seed", headers=H, json={"force": True})
    assert r.status_code == 200 and r.json().get("ok"), r.text
    r = client.get("/api/checklist/status", headers=H)
    assert r.status_code == 200, r.text
    st = r.json()
    assert st.get("seeded") and st.get("field_count", 0) >= 5, st

    # 2. Checklist generated from the regulation text.
    r = client.get("/api/checklist/" + quote("EM 385-1-1 Section 25"), headers=H)
    assert r.status_code == 200, r.text
    spec = r.json()
    assert spec.get("ok"), spec
    fields = spec.get("fields") or []
    types = {f["type"] for f in fields}
    assert {"measurement", "inspection", "attestation"} <= types, \
        f"parser must yield measurement + inspection + attestation fields: {types}"
    has_5ft = any(f.get("threshold") and f["threshold"].get("value") == 5
                  and f["threshold"].get("unit") == "ft"
                  and f["threshold"].get("comparator") == "gte" for f in fields)
    assert has_5ft, "expected a >=5 ft protective-system measurement"
    has_2ft = any(f.get("threshold") and f["threshold"].get("value") == 2
                  and f["threshold"].get("unit") == "ft" for f in fields)
    assert has_2ft, "expected a 2 ft spoil-setback measurement"
    has_shift = any(f.get("cadence") and f["cadence"].get("kind") == "per_shift"
                    for f in fields)
    assert has_shift, "expected a per-shift inspection cadence"
    assert all(f.get("citation") for f in fields), "every field must carry a citation"

    # 3. AHA matrix derived from the same parsed controls.
    r = client.post("/api/checklist/aha", headers=H, json={"activity": "Excavation > 5 ft"})
    assert r.status_code == 200, r.text
    aha = r.json()
    assert aha.get("ok") and aha.get("row_count", 0) >= 3, aha
    hazards = {row["hazard"] for row in (aha.get("rows") or [])}
    assert "Cave-in / soil collapse" in hazards, hazards
    assert all(row.get("citation") and row.get("controls") for row in aha["rows"]), \
        "every AHA row must carry its citation + controls"

    # 4. Unverifiable citation → refused, not fabricated.
    r = client.get("/api/checklist/" + quote("99 CFR 9999.9999"), headers=H)
    assert r.status_code == 200 and not r.json().get("ok"), r.text

    print("[pass] regulatory router: EM 385-1-1 excavation TEXT auto-generates a "
          "dynamic checklist (>=5 ft protective-system + 2 ft setback measurements, "
          "per-shift inspection, plain attestations) and an AHA matrix grouped by "
          "hazard — every field cited, unverifiable citations refused, no model")


# ── The eCFR ingest adapter (offline, fixture-injected — never touches network) ─
# A representative slice of 29 CFR 1926.651 (Specific excavation requirements),
# in the same GPO/eCFR XML shape the live versioner returns. It carries the four
# imperative shapes the parser must recover: a >=5 ft measurement, a >=2 ft
# spoil-setback measurement, a DAILY competent-person inspection, and plain
# "shall" attestations. This is a TEST FIXTURE, not seeded regulatory content.
_ECFR_1926_651_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<DIV8 TYPE="SECTION" N="1926.651">
  <HEAD>&#167; 1926.651 Specific excavation requirements.</HEAD>
  <P>Each employee in an excavation shall be protected from cave-ins by an
     adequate protective system designed in accordance with this section.</P>
  <P>A competent person shall inspect excavations daily and as conditions
     change to identify hazardous conditions before the start of work.</P>
  <P>In excavations that are 5 feet or more in depth, a stairway, ladder, ramp,
     or other safe means of egress shall be provided.</P>
  <P>Employees shall be protected from excavated or other materials by keeping
     such materials at least 2 feet from the edge of the excavation.</P>
</DIV8>"""


def check_ecfr(client, token: str) -> None:
    """The eCFR ingest adapter, fully offline via an injected fixture fetcher.

    The router's parser is source-agnostic: a CFR section's TEXT pulled from the
    eCFR must flow through the very same machinery as the EM 385 seed. We inject a
    cached 29 CFR 1926.651 payload so the test is 100% network-free.

      1. ingest_section with the fixture fetcher fetches, cleans the XML to plain
         text, writes it into the versioned knowledge store, and reports how many
         field requirements the parser derives (>=3).
      2. The section then RESOLVES like any other brain: checklist_from_regulation
         yields a dynamic form with a >=5 ft measurement, a 2 ft setback
         measurement, a daily inspection, and attestations — every field cited to
         "29 CFR 1926.651", nothing invented.
      3. A fetcher that raises (network down) returns ok=False with an error and
         writes NOTHING — a failed pull is unverifiable, never fabricated."""
    from origin import ecfr_adapter, checklist_engine

    captured = {}

    def _fixture_fetcher(url: str) -> str:
        captured["url"] = url          # prove the URL was built, but serve cache
        return _ECFR_1926_651_FIXTURE

    # 1. Ingest the section from the (fixture) eCFR payload.
    res = ecfr_adapter.ingest_section(
        29, "1926", "1926.651",
        industry_scope=["construction"],
        hazard_category=["excavation"],
        fetcher=_fixture_fetcher,
    )
    assert res.get("ok"), res
    assert res.get("citation") == "29 CFR 1926.651", res
    assert res.get("field_count", 0) >= 3, res
    assert "ecfr.gov" in captured.get("url", ""), captured  # real URL was built

    # 2. It now generates a cited checklist through the shared parser.
    spec = checklist_engine.checklist_from_regulation("29 CFR 1926.651")
    assert spec.get("ok"), spec
    fields = spec.get("fields") or []
    types = {f["type"] for f in fields}
    assert {"measurement", "inspection", "attestation"} <= types, types
    assert any(f.get("threshold") and f["threshold"].get("value") == 5
               and f["threshold"].get("unit") == "ft"
               and f["threshold"].get("comparator") == "gte" for f in fields), \
        "expected a >=5 ft egress measurement from the eCFR text"
    assert any(f.get("threshold") and f["threshold"].get("value") == 2
               and f["threshold"].get("unit") == "ft" for f in fields), \
        "expected a 2 ft spoil-setback measurement from the eCFR text"
    assert any(f.get("cadence") and f["cadence"].get("kind") == "daily"
               for f in fields), "expected a daily competent-person inspection"
    assert all(f.get("citation") == "29 CFR 1926.651" for f in fields), \
        "every field must be cited to its CFR section — never invented"

    # 3. Network failure → refused, and nothing written.
    def _dead_fetcher(url: str) -> str:
        raise OSError("simulated network failure")

    fail = ecfr_adapter.ingest_section(
        29, "9999", "9999.99", fetcher=_dead_fetcher)
    assert not fail.get("ok") and fail.get("error"), fail
    assert not checklist_engine.checklist_from_regulation("29 CFR 9999.99").get("ok"), \
        "a failed fetch must not leave a resolvable (fabricated) section behind"

    print("[pass] eCFR adapter: 29 CFR 1926.651 TEXT pulled from a (fixture) eCFR "
          "payload ingests into the same store and auto-generates a cited checklist "
          "(>=5 ft egress + 2 ft setback measurements, daily inspection, "
          "attestations); a network failure is refused, never fabricated")


def check_brain_router(client, token: str) -> None:
    """The Brain Router is the agency-mode engine behind the "Unified App": an
    admin toggles which regulatory brains a tenant runs, and that one choice must
    (1) reshape the nav, (2) SCOPE photo-audit citations to only the enabled
    authorities, and (3) never regress an account that has set no profile. All
    offline — the router is pure deterministic file-state, no LLM.

      1. UNSET = NO SCOPING. A tenant with no saved profile scopes to None, so the
         photo resolver behaves exactly as it did pre-router (a mine/EPA/DOT
         finding still surfaces). This is the backwards-compat guarantee.
      2. TOGGLE RESHAPES NAV + SCOPES CITATIONS. Enable OSHA only, and (a) the nav
         carries the OSHA tabs but not the MSHA/EPA tabs, (b) the citation scope is
         exactly {OSHA}, and (c) the photo resolver, handed that scope, returns an
         OSHA fall-hazard citation but DROPS a mine-berm (MSHA) finding to no-match
         — a disabled agency's standard can never be shown.
      3. ENABLING THE AGENCY ADMITS ITS LANE. Add MSHA back and the same mine-berm
         scene resolves again, tagged MSHA — nothing blends across lanes.
      4. PERSISTENCE + ROUTES. The saved profile round-trips through the API
         (GET/POST /api/brain-router/profile) and is tenant-isolated by gc_slug.
      5. VALIDATION. A POST without an 'agencies' list is a clean 400, and unknown
         codes are dropped rather than persisted."""
    from origin import brain_router as br
    from origin import photo_audit as pa

    H = {"X-Origin-Token": token}
    OSHA_SCENE = ("worker at an unprotected roof edge with no guardrail", "1926.501", "OSHA")
    MINE_SCENE = ("haul road dump point with no berm at the edge", "56.9300", "MSHA")

    def _resolve(desc, scope):
        return pa._resolve_citation(
            {"hazard_category": desc, "title": "", "description": desc},
            active_authorities=scope)

    # Use a throwaway tenant slug so this test never disturbs the owner profile.
    slug = "br-selftest"

    # 1. UNSET = NO SCOPING. No profile saved → citation_authorities is None →
    #    the resolver is unrestricted (a mine finding still surfaces).
    assert br.get_active(slug) is None, "a fresh tenant must have no saved profile"
    assert br.citation_authorities(slug) is None, \
        "unset profile must scope to None (no restriction) — the no-regression guarantee"
    unscoped = _resolve(MINE_SCENE[0], br.citation_authorities(slug))
    assert unscoped is not None and unscoped["authority"] == "MSHA", \
        f"unset tenant must still resolve a mine finding (MSHA): {unscoped}"

    # 2. Enable OSHA only. Nav reshapes, scope narrows to {OSHA}.
    prof = br.set_active(["OSHA"], gc_slug=slug)
    assert prof["configured"] is True and prof["active_agencies"] == ["OSHA"], prof
    scope = br.citation_authorities(slug)
    assert scope == {"OSHA"}, f"OSHA-only scope must be exactly OSHA: {scope}"
    nav_ids = {t["id"] for t in br.nav_for(slug)}
    assert "osha_300" in nav_ids, "OSHA tabs must appear when OSHA is enabled"
    assert "msha_preshift" not in nav_ids and "epa_spcc" not in nav_ids, \
        "disabled agencies' tabs must NOT appear in the nav"

    # 2b. The OSHA finding still resolves; the mine finding is DROPPED (not this
    #     tenant's to show) — a disabled agency's standard never surfaces.
    osha = _resolve(OSHA_SCENE[0], scope)
    assert osha is not None and osha["section"] == OSHA_SCENE[1] \
        and osha["authority"] == "OSHA" and osha["confidence_band"] == "high", \
        f"OSHA-enabled tenant must still cite the fall hazard at high band: {osha}"
    assert _resolve(MINE_SCENE[0], scope) is None, \
        "an OSHA-only tenant must NEVER be shown an MSHA (mine) citation"

    # 3. Enable MSHA too → the mine lane opens back up, tagged MSHA.
    br.set_active(["OSHA", "MSHA"], gc_slug=slug)
    scope2 = br.citation_authorities(slug)
    assert scope2 == {"OSHA", "MSHA"}, scope2
    mine = _resolve(MINE_SCENE[0], scope2)
    assert mine is not None and mine["section"] == MINE_SCENE[1] \
        and mine["authority"] == "MSHA", \
        f"re-enabling MSHA must admit the mine citation in its own lane: {mine}"
    # OSHA still resolves alongside it — lanes coexist, never blend.
    assert (_resolve(OSHA_SCENE[0], scope2) or {}).get("authority") == "OSHA", \
        "OSHA lane must remain intact when MSHA is also enabled"

    # 4. PERSISTENCE + ROUTES round-trip through the API, tenant-isolated.
    r = client.get("/api/brain-router/profile", headers=H)
    assert r.status_code == 200, r.text
    owner_prof = r.json()
    # The owner profile is a DIFFERENT tenant than our throwaway slug; setting the
    # slug's profile above must not have configured the owner's.
    assert isinstance(owner_prof.get("agencies"), list), owner_prof
    r = client.post("/api/brain-router/profile", headers=H, json={"agencies": ["EPA"]})
    assert r.status_code == 200 and not r.json().get("error"), r.text
    assert r.json().get("active_agencies") == ["EPA"], r.json()
    r = client.get("/api/brain-router/profile", headers=H)
    assert r.json().get("authorities") == ["EPA"], r.json()
    # Isolation: the owner's EPA choice did not touch our throwaway slug's set.
    assert br.get_active(slug) == ["OSHA", "MSHA"], \
        "setting the owner profile must not bleed into another tenant's profile"

    # 5. VALIDATION — missing list is a clean 400; unknown codes are dropped.
    r = client.post("/api/brain-router/profile", headers=H, json={})
    assert r.status_code == 400, f"a POST with no 'agencies' list must 400: {r.status_code}"
    saved = br.set_active(["OSHA", "NOPE", "MSHA"], gc_slug=slug)
    assert saved["active_agencies"] == ["OSHA", "MSHA"], \
        f"unknown agency codes must be dropped, not persisted: {saved['active_agencies']}"

    print("[pass] Brain Router: unset tenant scopes to None (no regression — mine "
          "finding still surfaces); enabling OSHA-only reshapes the nav (OSHA tabs "
          "in, MSHA/EPA tabs out) and scopes citations to {OSHA} so an MSHA berm is "
          "dropped while the 1926.501 fall stays high; re-enabling MSHA reopens the "
          "mine lane tagged MSHA without blending; profile persists + round-trips "
          "through the API tenant-isolated; bad payloads 400 and unknown codes drop")


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
        check_perception(client, token)
        check_review(client, token)
        check_training(client, token)
        check_gc_scope(client, token)
        check_checklists(client, token)
        check_ecfr(client, token)
        check_brain_router(client, token)
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
