"""Seed the multi-tenant platform with demo data (Phase 1).

Creates, if the tables are empty:
  * one OWNER (Chris) — the only account allowed near the AI engine,
  * two GC tenants with their own branding,
  * several subcontractors per GC, each with prequal grades, TRIR/EMR, a COI,
    a couple of documents, and a message thread,
  * one gc_admin login and one sub login per relevant company.

Idempotent: running it again does nothing once an owner exists. Passwords come
from env so nothing is hardcoded:
  OWNER_EMAIL / OWNER_PASSWORD      (owner login)
  DEMO_GC_PASSWORD                  (all demo GC-admin logins)
  DEMO_SUB_PASSWORD                 (all demo sub logins)
Sensible dev defaults are used only when a var is unset.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

from . import platform_db as db
from .platform_db import (
    Tenant, User, Subcontractor, ComplianceStatus, COI, Document, Message,
    ROLE_OWNER, ROLE_GC_ADMIN, ROLE_SUB,
)
from . import platform_auth as auth


def _health_from_grades(grades) -> str:
    reds = any((g.grade or "").upper() in ("F", "D") for g in grades)
    if reds:
        return "red"
    ambers = any((g.grade or "").upper() == "C" for g in grades)
    return "amber" if ambers else "green"


def seed(force: bool = False) -> dict:
    db.init_db()
    with db.session() as s:
        if auth.owner_exists(s) and not force:
            return {"seeded": False, "reason": "owner already exists"}

        owner_email = (os.environ.get("OWNER_EMAIL")
                       or "info@originmanagementsolutions.com").lower()
        owner_pw = os.environ.get("OWNER_PASSWORD") or "change-me-owner"
        gc_pw = os.environ.get("DEMO_GC_PASSWORD") or "demo-gc"
        sub_pw = os.environ.get("DEMO_SUB_PASSWORD") or "demo-sub"

        # ---- owner ----
        s.add(User(email=owner_email, password_hash=auth.hash_password(owner_pw),
                   role=ROLE_OWNER, name="Chris (Owner)"))

        # ---- demo GCs ----
        gcs = [
            dict(name="Redline Constructors", slug="redline",
                 primary="#1E7A46", text="#FFFFFF"),
            dict(name="Summit Industrial", slug="summit",
                 primary="#1F3864", text="#FFFFFF"),
        ]
        # sub blueprints per GC: (name, scope, {platform:grade}, trir, emr, coi_days)
        subs_by_gc = {
            "redline": [
                ("Rio Grande Welding", ["hot work", "hazcom"],
                 {"isn": "F", "avetta": "A"}, 4.2, 1.15, 40),
                ("Lone Star Electric", ["electrical", "loto"],
                 {"isn": "A", "veriforce": "B"}, 1.1, 0.92, 8),
                ("Gulf Coast Scaffolding", ["scaffolding", "fall protection"],
                 {"isn": "B", "avetta": "A"}, 2.0, 1.02, 200),
            ],
            "summit": [
                ("Apex Mechanical", ["mechanical", "confined space"],
                 {"isn": "A", "veriforce": "A"}, 0.9, 0.85, 150),
                ("Delta Excavation", ["excavation", "trenching"],
                 {"isn": "C", "avetta": "C"}, 3.4, 1.20, 25),
            ],
        }

        for g in gcs:
            t = Tenant(name=g["name"], slug=g["slug"],
                       brand_primary=g["primary"], brand_text=g["text"])
            s.add(t)
            s.flush()  # get t.id

            # a GC admin login
            s.add(User(email=f"admin@{g['slug']}.example.com",
                       password_hash=auth.hash_password(gc_pw),
                       role=ROLE_GC_ADMIN, gc_id=t.id,
                       name=f"{g['name']} Admin"))

            for (name, scope, grades, trir, emr, coi_days) in subs_by_gc[g["slug"]]:
                sub = Subcontractor(
                    gc_id=t.id, name=name, slug=auth.slugify(name),
                    contact_name="Site Contact",
                    contact_email=f"office@{auth.slugify(name)}.example.com",
                    scope_of_work=scope, trir=trir, emr=emr)
                s.add(sub)
                s.flush()

                grade_rows = []
                for platform, grade in grades.items():
                    gr = ComplianceStatus(
                        gc_id=t.id, sub_id=sub.id, platform=platform,
                        grade=grade, status="active", source="sub",
                        graded_on=date.today() - timedelta(days=30))
                    grade_rows.append(gr)
                    s.add(gr)
                sub.health = _health_from_grades(grade_rows)

                s.add(COI(gc_id=t.id, sub_id=sub.id, carrier="Acme Mutual",
                          coverage="$1M / $2M GL",
                          expiry=date.today() + timedelta(days=coi_days)))

                s.add(Document(gc_id=t.id, sub_id=sub.id,
                               name="Signed Master Service Agreement",
                               category="contract", source="upload"))

                # a first sub login for the flagged (red) subs
                s.add(User(email=f"user@{auth.slugify(name)}.example.com",
                           password_hash=auth.hash_password(sub_pw),
                           role=ROLE_SUB, gc_id=t.id, sub_id=sub.id,
                           name=f"{name} User"))

                # a short two-way thread on the worst sub
                if grades.get("isn") == "F":
                    s.add(Message(gc_id=t.id, sub_id=sub.id, sender_role=ROLE_GC_ADMIN,
                                  body="Your ISN grade dropped to F — I'm sending the "
                                       "hot-work program to fix the gap.",
                                  created_at=datetime.utcnow() - timedelta(hours=3)))
                    s.add(Message(gc_id=t.id, sub_id=sub.id, sender_role=ROLE_SUB,
                                  body="Got it, we'll get it signed and re-uploaded today.",
                                  created_at=datetime.utcnow() - timedelta(hours=2)))

        s.commit()

        counts = {
            "tenants": s.query(Tenant).count(),
            "users": s.query(User).count(),
            "subcontractors": s.query(Subcontractor).count(),
            "grades": s.query(ComplianceStatus).count(),
            "messages": s.query(Message).count(),
        }
    return {"seeded": True, "counts": counts, "owner_email": owner_email}


if __name__ == "__main__":  # pragma: no cover
    import json as _json
    print(_json.dumps(seed(), indent=2))
