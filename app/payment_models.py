from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, uid


class Payment(Base):
    """Backend-owned settlement request.

    A Payment in ``pending`` state is only a request/reservation. It is not proof that
    money moved. This slice deliberately has no transition capable of producing a
    settled state.
    """

    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[str] = mapped_column(String(256), index=True, nullable=False)
    quotation_id: Mapped[str] = mapped_column(String(256), index=True, nullable=False)
    quotation_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(19, 4), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    membership_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    authorization_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_payment_org_idempotency_key",
        ),
    )


class PaymentAttempt(Base):
    """Backend-owned provider attempt/audit record.

    The settlement-contract scaffold records only ``blocked`` attempts with provider
    ``unconfigured``. A future processor integration must add the state machine that
    can produce pending/succeeded/failed processor outcomes.
    """

    __tablename__ = "payment_attempts"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    payment_id: Mapped[str] = mapped_column(
        ForeignKey("payments.id", ondelete="CASCADE"), index=True, nullable=False
    )
    operation: Mapped[str] = mapped_column(String(32), nullable=False, default="prepare")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="blocked")
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="unconfigured")
    amount: Mapped[Decimal] = mapped_column(Numeric(19, 4), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_by_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    membership_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    authorization_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
