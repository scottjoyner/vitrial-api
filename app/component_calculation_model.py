"""Authoritative component calculation receipt storage.

Server-owned, immutable receipt linking an Item/Configuration to a
catalog/rules publication pair and the exact calculated quantities.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class ComponentCalculationReceipt(Base):
    """Authoritative server-issued component calculation for an Item Configuration.

    Only created via the authoritative endpoint by a principal with the
    `item.components.authorize` capability. The receipt is immutable once
    created and serves as evidence for quotations, deliveries, and audits.
    """

    __tablename__ = "component_calculation_receipts"

    organization_id: Mapped[str] = mapped_column(
        String(128), primary_key=True
    )
    receipt_id: Mapped[str] = mapped_column(
        String(256), primary_key=True
    )
    item_id: Mapped[str] = mapped_column(
        String(256), nullable=False, index=True
    )
    configuration_id: Mapped[str] = mapped_column(
        String(256), nullable=False
    )
    configuration_version: Mapped[int] = mapped_column(nullable=False)
    catalog_publication_id: Mapped[str] = mapped_column(
        String(256), nullable=False
    )
    catalog_version_id: Mapped[str] = mapped_column(
        String(256), nullable=False
    )
    catalog_content_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    rules_publication_id: Mapped[str] = mapped_column(
        String(256), nullable=False
    )
    rules_version_id: Mapped[str] = mapped_column(
        String(256), nullable=False
    )
    rules_content_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    input_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    components_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    issues_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    issued_by_user_id: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    authorization_revision: Mapped[int] = mapped_column(nullable=False)
