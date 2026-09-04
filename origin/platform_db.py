"""Multi-tenant PLATFORM data layer for Origin (Phase 1 foundation).

This is the new white-label backbone: one shared Postgres database that many
General Contractors (GCs) and their subcontractors live in at once. It sits
ALONGSIDE the existing file-based portal (portal.py) and does not touch it, so
a bug here can never break the working chat app, gap finder, or dashboard.

Tenancy model (the "best route" for dozens of GCs each with dozens of subs):
  * ONE shared database, not one-per-GC.
  * Every tenant-owned row carries `gc_id`. Every query for a GC admin or a sub
    is scoped to their gc_id, so no account can ever see another GC's data.
  * The Owner (Chris) is the only role with gc_id = NULL and sees everything.
  * Isolation is enforced in code on every request (see platform_auth.scoped()).

Engine:
  * Reads DATABASE_URL. On Railway that's the provisioned Postgres. Locally /
    in tests it can point at sqlite for a smoke run — the models are written
    with portable column types so behaviour is identical on both.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, date
from typing import Optional

from sqlalchemy import (
    create_engine, String, Integer, Float, Boolean, Text, DateTime, Date,
    ForeignKey, JSON, UniqueConstraint, Index,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, Session,
)


# ── roles ────────────────────────────────────────────────────────────────
ROLE_OWNER = "owner"        # Chris. gc_id is NULL. Sees everything. Only role
                            # allowed near the AI Origin engine.
ROLE_GC_ADMIN = "gc_admin"  # A GC's staff. Scoped to their own gc_id.
ROLE_SUB = "sub"            # A subcontractor user. Scoped to gc_id + sub_id.
ROLES = (ROLE_OWNER, ROLE_GC_ADMIN, ROLE_SUB)

# prequal databases we track a grade for
PLATFORMS = ("isn", "avetta", "veriforce", "pec")


def _uuid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


# ── the tenant: a General Contractor ─────────────────────────────────────
class Tenant(Base):
    """A GC. The unit of white-label branding and the isolation boundary."""
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    logo_url: Mapped[str] = mapped_column(String(500), default="")
    brand_primary: Mapped[str] = mapped_column(String(9), default="#1E7A46")
    brand_text: Mapped[str] = mapped_column(String(9), default="#FFFFFF")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    subs: Mapped[list["Subcontractor"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan")
    users: Mapped[list["User"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan")


# ── users across all three roles ─────────────────────────────────────────
class User(Base):
    """Owner, GC admin, or sub user. `role` + `gc_id` + `sub_id` decide scope."""
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200), default="")

    # NULL for the owner. Set for gc_admin and sub.
    gc_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    # Set only for a sub user (which subcontractor they are).
    sub_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("subcontractors.id", ondelete="CASCADE"), nullable=True, index=True)

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    tenant: Mapped[Optional["Tenant"]] = relationship(back_populates="users")


# ── a subcontractor (the company, not the login) ─────────────────────────
class Subcontractor(Base):
    __tablename__ = "subcontractors"
    __table_args__ = (
        Index("ix_sub_gc", "gc_id"),
        UniqueConstraint("gc_id", "slug", name="uq_sub_gc_slug"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    gc_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(120))
    contact_name: Mapped[str] = mapped_column(String(200), default="")
    contact_email: Mapped[str] = mapped_column(String(255), default="")
    # scope of work drives which library documents are "required" for this sub
    scope_of_work: Mapped[list] = mapped_column(JSON, default=list)
    # safety metrics shown next to grades
    trir: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    emr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # rolled-up health: 'green' | 'amber' | 'red' (computed, cached here)
    health: Mapped[str] = mapped_column(String(8), default="green")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    tenant: Mapped["Tenant"] = relationship(back_populates="subs")
    grades: Mapped[list["ComplianceStatus"]] = relationship(
        back_populates="sub", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(
        back_populates="sub", cascade="all, delete-orphan")
    cois: Mapped[list["COI"]] = relationship(
        back_populates="sub", cascade="all, delete-orphan")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="sub", cascade="all, delete-orphan")


# ── prequal grades, one row per platform per sub ─────────────────────────
class ComplianceStatus(Base):
    __tablename__ = "compliance_status"
    __table_args__ = (
        Index("ix_grade_gc", "gc_id"),
        UniqueConstraint("sub_id", "platform", name="uq_grade_sub_platform"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    gc_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    sub_id: Mapped[str] = mapped_column(
        ForeignKey("subcontractors.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(16))   # isn|avetta|veriforce|pec
    grade: Mapped[str] = mapped_column(String(8), default="")   # A, B, F, ...
    status: Mapped[str] = mapped_column(String(24), default="")  # active/expired/...
    # where the number came from: 'sub' | 'gc' | 'sync' (see architecture doc)
    source: Mapped[str] = mapped_column(String(8), default="sub")
    graded_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    sub: Mapped["Subcontractor"] = relationship(back_populates="grades")


# ── certificate of insurance ─────────────────────────────────────────────
class COI(Base):
    __tablename__ = "cois"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    gc_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    sub_id: Mapped[str] = mapped_column(
        ForeignKey("subcontractors.id", ondelete="CASCADE"), index=True)
    carrier: Mapped[str] = mapped_column(String(200), default="")
    coverage: Mapped[str] = mapped_column(String(200), default="")
    expiry: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    file_path: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    sub: Mapped["Subcontractor"] = relationship(back_populates="cois")


# ── a document that lives with a sub ─────────────────────────────────────
class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (Index("ix_doc_gc", "gc_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    gc_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    sub_id: Mapped[str] = mapped_column(
        ForeignKey("subcontractors.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(120), default="")
    source: Mapped[str] = mapped_column(String(12), default="upload")  # upload|library
    storage_path: Mapped[str] = mapped_column(String(500), default="")
    uploaded_by: Mapped[Optional[str]] = mapped_column(
        ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    sub: Mapped["Subcontractor"] = relationship(back_populates="documents")


# ── two-way message thread, scoped to one sub ────────────────────────────
class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_msg_sub", "sub_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    gc_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    sub_id: Mapped[str] = mapped_column(
        ForeignKey("subcontractors.id", ondelete="CASCADE"), index=True)
    sender_user_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("users.id"), nullable=True)
    sender_role: Mapped[str] = mapped_column(String(16))  # gc_admin | sub
    body: Mapped[str] = mapped_column(Text)
    read_by_gc: Mapped[bool] = mapped_column(Boolean, default=False)
    read_by_sub: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    sub: Mapped["Subcontractor"] = relationship(back_populates="messages")


# ── owner-owned master library program ───────────────────────────────────
class LibraryProgram(Base):
    """A master safety document you (owner) stock. `scope_tags` decides which
    subs it's 'required' for; a copy becomes a Document on a sub when pulled in."""
    __tablename__ = "library_programs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(120), default="")
    scope_tags: Mapped[list] = mapped_column(JSON, default=list)
    body_ref: Mapped[str] = mapped_column(String(500), default="")  # markdown master path/id
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ── activity log ─────────────────────────────────────────────────────────
class ActivityLog(Base):
    __tablename__ = "activity_log"
    __table_args__ = (Index("ix_log_gc", "gc_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    gc_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(120))
    target: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ── engine / session plumbing ────────────────────────────────────────────
_ENGINE = None
_SessionLocal = None


def _normalize_url(url: str) -> str:
    """Railway hands out `postgres://...`; SQLAlchemy 2.x wants an explicit
    driver. Point it at psycopg (v3)."""
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        # No Postgres configured yet — fall back to a local sqlite file so the
        # app still boots and the foundation can be smoke-tested. Production on
        # Railway always sets DATABASE_URL to the provisioned Postgres.
        from .paths import DATA_DIR
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{DATA_DIR / 'platform.db'}"
    return _normalize_url(url)


def get_engine():
    global _ENGINE, _SessionLocal
    if _ENGINE is None:
        url = database_url()
        kwargs = {"pool_pre_ping": True, "future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _ENGINE = create_engine(url, **kwargs)
        _SessionLocal = sessionmaker(bind=_ENGINE, expire_on_commit=False,
                                     class_=Session, future=True)
    return _ENGINE


def init_db() -> None:
    """Create every table if it doesn't exist. Safe to call on each boot."""
    Base.metadata.create_all(get_engine())


def session() -> Session:
    if _SessionLocal is None:
        get_engine()
    return _SessionLocal()  # type: ignore[misc]
