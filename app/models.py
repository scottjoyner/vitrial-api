from __future__ import annotations

import uuid
from datetime import datetime
from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String,
    UniqueConstraint, JSON, func
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def uid() -> str:
    return str(uuid.uuid4())


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(256))
    authorization_revision: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=uid)
    display_name: Mapped[str] = mapped_column(String(256))
    email: Mapped[str | None] = mapped_column(String(320), unique=True)


class Membership(Base):
    __tablename__ = "memberships"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    all_customers: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    all_projects: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    customer_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    project_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    roles: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_membership_org_user"),)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    membership_id: Mapped[str] = mapped_column(ForeignKey("memberships.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    access_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CanonicalCustomer(Base):
    """Server-owned Customer identity used for authorization, never inferred from a child payload."""

    __tablename__ = "canonical_customers"
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    customer_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CanonicalProject(Base):
    """Immutable server-owned Project -> Customer relationship."""

    __tablename__ = "canonical_projects"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["canonical_customers.organization_id", "canonical_customers.customer_id"],
            name="fk_canonical_project_customer",
            ondelete="RESTRICT",
        ),
    )


class CanonicalItem(Base):
    """Immutable server-owned Item -> Project relationship."""

    __tablename__ = "canonical_items"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["canonical_projects.organization_id", "canonical_projects.project_id"],
            name="fk_canonical_item_project",
            ondelete="RESTRICT",
        ),
    )


class SyncEntity(Base):
    __tablename__ = "sync_entities"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    server_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncMutation(Base):
    __tablename__ = "sync_mutations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(String(128), index=True)
    client_mutation_id: Mapped[str] = mapped_column(String(256))
    submitted_by_user_id: Mapped[str] = mapped_column(String(128))
    membership_id: Mapped[str] = mapped_column(String(128))
    session_id: Mapped[str] = mapped_column(String(128))
    authorization_revision: Mapped[int] = mapped_column(BigInteger)
    device_id: Mapped[str] = mapped_column(String(256))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(256))
    base_server_revision: Mapped[int | None] = mapped_column(BigInteger)
    result_server_revision: Mapped[int | None] = mapped_column(BigInteger)
    result_status: Mapped[str] = mapped_column(String(32))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("organization_id", "client_mutation_id", name="uq_sync_mutation_org_client"),)


class SyncChangeLog(Base):
    __tablename__ = "sync_change_log"
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(String(128), index=True)
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(256))
    server_revision: Mapped[int] = mapped_column(BigInteger)
    client_mutation_id: Mapped[str | None] = mapped_column(String(256))
    operation: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvidenceBlob(Base):
    __tablename__ = "evidence_blobs"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(256), index=True)
    filename: Mapped[str] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(256))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    object_key: Mapped[str] = mapped_column(String(1024), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
