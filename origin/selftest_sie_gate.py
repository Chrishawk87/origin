"""Offline security self-test for the unified /sie sign-in gate.

Run from the repo root:  python -m origin.selftest_sie_gate
Exercises: master login, client login scope, magic-link redeem + expiry,
unauthenticated redirects, bad credentials, and cross-cookie rejection.
Uses a throwaway DATA_DIR and a fixed secret so nothing touches real data.
"""
from __future__ import annotations

import os
import tempfile
import time

# Isolate storage + pin the signing secret BEFORE importing the app.
_TMP = tempfile.mkdtemp(prefix="sie_gate_test_")
os.environ["ORIGIN_DATA_DIR"] = _TMP
os.environ["ORIGIN_PORTAL_SECRET"] = "test-secret-fixed-value-123456"
os.environ["SIE_OWNER_EMAIL"] = "boss@origin.test"
os.environ["SIE_OWNER_PASSWORD"] = "correct horse battery"

from starlette.testclient import TestClient  # noqa: E402

from origin.server import create_app  # noqa: E402
from origin import portal as _portal     # noqa: E402


def _seed_client(company: str, email: str, pin: str) -> str:
    slug = _portal.slugify(company) if hasattr(_portal, "slugify") else company.lower().replace(" ", "-")
    rec = {"slug": slug, "company": company, "email": email,
           "pin_hash": _portal.hash_pin(slug, pin), "client_type": "prequal",
           "requests": [], "docs": []}
    _portal.save_client(rec)
    return slug


PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name)


def main():
    # A real access token so the /api gate is live and /sie requires a session.
    app = create_app(token="engine-token-xyz")
    slug_a = _seed_client("Acme Welding", "acme@client.test", "1234")
    slug_b = _seed_client("Beta Rigging", "beta@client.test", "9876")

    print("· unauthenticated")
    c = TestClient(app, follow_redirects=False)
    r = c.get("/sie")
    check("/sie without session redirects to /sie/login",
          r.status_code == 302 and r.headers.get("location") == "/sie/login")
    r = c.get("/sie/login")
    check("/sie/login serves the sign-in page (200 HTML)",
          r.status_code == 200 and "Safety Intelligence Engine" in r.text)
    r = c.get("/api/company/list")
    check("/api/* refused without a session (401)", r.status_code == 401)

    print("· master (owner) login")
    c = TestClient(app, follow_redirects=False)
    r = c.post("/sie/api/login", json={"email": "boss@origin.test", "password": "wrong"})
    check("wrong master password rejected (401)", r.status_code == 401)
    r = c.post("/sie/api/login", json={"email": "boss@origin.test",
                                       "password": "correct horse battery"})
    check("correct master login ok + redirect /sie",
          r.status_code == 200 and r.json().get("ok") and r.json().get("redirect") == "/sie")
    check("master login sets admin cookie", _portal.ADMIN_COOKIE in c.cookies)
    r = c.get("/sie")
    check("owner reaches /sie console (200)",
          r.status_code == 200 and "Console page missing" not in r.text)
    r = c.get("/api/company/list")
    check("owner reaches /api/* (200)", r.status_code == 200)
    r = c.get("/sie/api/whoami")
    check("whoami = owner", r.json().get("role") == "owner")

    print("· client login is scoped to the portal, not the console")
    c = TestClient(app, follow_redirects=False)
    r = c.post("/sie/api/login", json={"email": "acme@client.test", "pin": "0000"})
    check("wrong client PIN rejected (401)", r.status_code == 401)
    r = c.post("/sie/api/login", json={"email": "acme@client.test", "pin": "1234"})
    check("correct client login ok + redirect /portal",
          r.status_code == 200 and r.json().get("redirect") == "/portal")
    check("client login sets client cookie (not admin)",
          _portal.CLIENT_COOKIE in c.cookies and _portal.ADMIN_COOKIE not in c.cookies)
    r = c.get("/sie")
    check("client visiting /sie is bounced to /portal",
          r.status_code == 302 and r.headers.get("location") == "/portal")
    r = c.get("/api/company/list")
    check("client cannot reach owner /api/* (401)", r.status_code == 401)
    r = c.get("/sie/api/whoami")
    check("whoami = client", r.json().get("role") == "client")

    print("· magic link (temporary owner access)")
    owner = TestClient(app, follow_redirects=False)
    owner.post("/sie/api/login", json={"email": "boss@origin.test",
                                       "password": "correct horse battery"})
    r = owner.post("/sie/api/magic", json={"minutes": 30})
    check("owner can mint a magic link", r.status_code == 200 and r.json().get("url"))
    url = r.json().get("url", "")
    token_q = url.split("t=", 1)[1] if "t=" in url else ""
    # A fresh visitor (no cookies) redeems the link.
    guest = TestClient(app, follow_redirects=False)
    r = guest.get(f"/sie/enter?t={token_q}")
    check("redeeming magic link redirects to /sie",
          r.status_code == 302 and r.headers.get("location") == "/sie")
    check("magic link grants an admin cookie", _portal.ADMIN_COOKIE in guest.cookies)
    r = guest.get("/sie")
    check("guest with magic session reaches the console (200)", r.status_code == 200)

    print("· a non-owner cannot mint links; tampered/expired tokens fail")
    stranger = TestClient(app, follow_redirects=False)
    r = stranger.post("/sie/api/magic", json={"hours": 1})
    check("stranger cannot mint a magic link (403)", r.status_code == 403)
    r = guest.get("/sie/enter?t=not-a-real-token")
    check("garbage magic token bounces to login",
          r.status_code == 302 and "/sie/login" in r.headers.get("location", ""))
    # Forge an already-expired magic token with the real secret.
    expired = _portal._sign({"kind": "sie_magic", "role": "admin",
                             "exp": time.time() - 10})
    r = guest.get(f"/sie/enter?t={expired}")
    check("expired magic token bounces to login",
          r.status_code == 302 and "/sie/login" in r.headers.get("location", ""))
    # A normal session token must NOT be accepted as a magic link (kind guard).
    not_magic = _portal._session("admin", "")
    r = guest.get(f"/sie/enter?t={not_magic}")
    check("a plain session token is not a valid magic link",
          r.status_code == 302 and "/sie/login" in r.headers.get("location", ""))

    print("· owner login not configured => fail closed")
    old = os.environ.pop("SIE_OWNER_PASSWORD", None)
    os.environ.pop("OWNER_PASSWORD", None)
    app2 = create_app(token="engine-token-xyz")
    c2 = TestClient(app2, follow_redirects=False)
    r = c2.post("/sie/api/login", json={"email": "boss@origin.test", "password": "x"})
    check("no owner password set => master login 503", r.status_code == 503)
    if old is not None:
        os.environ["SIE_OWNER_PASSWORD"] = old

    print()
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  ! " + f)
        raise SystemExit(1)
    print("ALL GREEN")


if __name__ == "__main__":
    main()
