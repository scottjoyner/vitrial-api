from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class ReferencePublication(Base):
    """Immutable organization-scoped reference-data publication.

    Catalog, compatibility-rule, and price-book publications are intentionally
    separate from mutable structured sync entities. Completed work may retain a
    publication/version ID indefinitely, while newer publications become current
    only by effective-date selection.
    """

    __tablename__ = "reference_publications"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    publication_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version_id: Mapped[str] = mapped_column(String(256), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    supersedes_publication_id: Mapped[str | None] = mapped_column(String(256))
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    published_by_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "kind", "version_id",
            name="uq_reference_publication_org_kind_version",
        ),
    )
