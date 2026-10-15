"""Add nullable refresh-token custody columns for paired sessions.

Revision ID: 0008
Revises: 0007

Non-destructive and additive: older access-token-only sessions stay valid
until their original expiry; no refresh tokens are fabricated for existing
rows. Production migration requires independent DBA review and is not
authorized by this research branch.
"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "auth_sessions",
        sa.Column("refresh_token_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "auth_sessions",
        sa.Column("refresh_token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_auth_sessions_refresh_token_hash",
        "auth_sessions",
        ["refresh_token_hash"],
        unique=True,
    )


def downgrade() -> None:
    # Downgrade destroys stored refresh-token custody. Never execute on a
    # production instance without separately authorized incident procedures.
    op.drop_index("ix_auth_sessions_refresh_token_hash", table_name="auth_sessions")
    op.drop_column("auth_sessions", "refresh_token_expires_at")
    op.drop_column("auth_sessions", "refresh_token_hash")
