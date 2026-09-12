"""Offline verification for the white-label SIE partner tenant (#772).

Run from the repo root (the dir that CONTAINS the `origin` package):

    ORIGIN_DATA_DIR=/tmp/origin_partner_test \
    python -m origin.selftest_sie_partners

Proves, with NO network and a throwaway data dir:
  1. Owner (admin cookie) can create a partner via /api/sie-partner/create.
  2. A partner session lands in /sie and reads only /api/sie-partner/self.
  3. Cross-tenant isolation: partner A's matter list never shows partner B's
     matters (the abatement store is scoped by firm_slug == the partner slug).
  4. A partner is refused owner-only routes and any non-abatement API path.
  5. sie_gate login with a partner's email+PIN mints the partner cookie.
"""

from __future__ import annotations

import os
import sys

_FAILS = []


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        _FAILS.append(name)


def main() -> int:
    os.environ.setdefault("ORIGIN_DATA_DIR", "/tmp/origin_partner_test")
    from starlette.testclient import TestClient

    from . import portal as P
    from . import sie_partners as SP
    from . import abatement_matter as MM
    from .server import create_app

    TOKEN = "test-access-token"          # forces the network-gated middleware path
    app = create_app(token=TOKEN)
    c = TestClient(app)

    # ── seed two partners directly on disk (owner would do this via the UI) ──
    for slug_name, email in (("Alpha Safety Group", "alpha@example.com"),
                             ("Bravo Compliance LLC", "bravo@example.com")):
        rec = SP._blank_partner(slug_name, email)
        rec["pin_hash"] = P.hash_pin(rec["slug"], "4321")
        SP.save_partner(rec)
    a_slug = P.slugify("Alpha Safety Group")
    b_slug = P.slugify("Bravo Compliance LLC")
    check("two partners seeded", bool(SP.load_partner(a_slug) and SP.load_partner(b_slug)))

    # ── give each partner one matter, stamped with its own firm_slug ──
    ma = MM.create_matter(client_name="Alpha Client One", firm_slug=a_slug)
    mb = MM.create_matter(client_name="Bravo Client One", firm_slug=b_slug)
    check("matters created under distinct firm slugs",
          ma.get("id") and mb.get("id") and ma["id"] != mb["id"])

    # ── cookie helpers (reuse portal's real signer) ──
    def partner_cookie(slug):
        return {P.SIE_PARTNER_COOKIE: P._session("sie_partner", slug)}

    def owner_cookie():
        return {P.ADMIN_COOKIE: P._session("admin", "")}

    # 1) owner creates a partner through the real route
    r = c.post("/api/sie-partner/create",
               headers={"x-origin-token": TOKEN},
               cookies=owner_cookie(),
               json={"name": "Charlie Env Services", "email": "charlie@example.com",
                     "pin": "9999", "brand_primary": "#0055aa"})
    check("owner create partner → 200", r.status_code == 200 and r.json().get("ok"))
    check("created partner persisted", bool(SP.load_partner(P.slugify("Charlie Env Services"))))

    # 2) partner reads its own record
    ra = c.get("/api/sie-partner/self", cookies=partner_cookie(a_slug))
    check("partner /self → 200", ra.status_code == 200 and ra.json()["partner"]["slug"] == a_slug)

    # 3) ISOLATION — partner A lists matters, sees only its own
    la = c.get("/api/abatement/matters", cookies=partner_cookie(a_slug))
    a_ids = {m.get("id") for m in (la.json().get("items") or [])}
    check("partner A sees its own matter", ma["id"] in a_ids)
    check("partner A does NOT see partner B's matter", mb["id"] not in a_ids)

    lb = c.get("/api/abatement/matters", cookies=partner_cookie(b_slug))
    b_ids = {m.get("id") for m in (lb.json().get("items") or [])}
    check("partner B sees its own matter", mb["id"] in b_ids)
    check("partner B does NOT see partner A's matter", ma["id"] not in b_ids)

    # 4a) partner refused the owner-only list route
    r = c.get("/api/sie-partner/list", cookies=partner_cookie(a_slug))
    check("partner blocked from owner /list → 403", r.status_code == 403)

    # 4b) partner refused an unrelated (non-abatement) API path
    r = c.get("/api/company/list", cookies=partner_cookie(a_slug))
    check("partner blocked from /api/company/list → 403", r.status_code == 403)

    # 4c) partner CAN restyle its own dashboard
    r = c.post("/api/sie-partner/self/branding", cookies=partner_cookie(a_slug),
               json={"brand_primary": "#123abc", "theme": "light", "brand_name": "Alpha SG"})
    ok = r.status_code == 200 and SP.load_partner(a_slug)["brand_primary"] == "#123abc"
    check("partner self-branding saved", ok)

    # 5) sie_gate login with partner email+PIN mints the partner cookie
    r = c.post("/sie/api/login", json={"email": "alpha@example.com", "secret": "4321"})
    ok = (r.status_code == 200 and r.json().get("role") == "sie_partner"
          and P.SIE_PARTNER_COOKIE in r.cookies)
    check("partner login mints partner cookie", ok)

    # 6) /sie console admits a partner session
    r = c.get("/sie", cookies=partner_cookie(a_slug))
    check("/sie admits partner session (200)", r.status_code == 200)

    # 7) anonymous still blocked from partner self + abatement
    #    (fresh client so the login cookie jar from step 5 can't leak in)
    anon = TestClient(app)
    r = anon.get("/api/sie-partner/self")
    check("anon /self blocked (401/403)", r.status_code in (401, 403))

    print()
    if _FAILS:
        print(f"{len(_FAILS)} FAILURE(S): " + "; ".join(_FAILS))
        return 1
    print("ALL PARTNER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
